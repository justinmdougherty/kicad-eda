#!/usr/bin/env python3
"""Shared SWIG copper geometry helpers — runs under KiCad's Python.

Moved out of swig_board_fixes.py (F5-03) with unchanged bodies so the healer,
the via adders and later board-fix modules share one implementation:
unit conversion, same-net attach points, via templates, foreign-copper
clearance probes (spot / straight path) and the coarse BFS maze router.
swig_board_fixes keeps the old underscore names as aliases.

All coordinates are (x, y) tuples in mm on the sheet (pcbnew coordinates);
layer ids are pcbnew layer constants (None = a through item, every copper
layer).

On-board guard (F3-16): BoardEdge is the board's Edge.Cuts outline, built
once per run by board_edge(); edge(p, clearance) = outline.contains of the
sheet point p in the board frame. clear_spot / clear_path / bfs_path take an
optional `on_board` point predicate, edge.keep(radius): copper of that radius
at p stays on the board (outside every cutout) by the copper-to-edge
clearance. Without one they behave exactly as before.
"""
from __future__ import annotations

import math
from collections import deque
from collections.abc import Callable
from typing import NamedTuple

import kicad_quiet  # noqa: F401  (before pcbnew: no modal wx dialogs)
import pcbnew

import outline as ol

XY = tuple[float, float]               # (x, y) mm, sheet
Cell = tuple[int, int]                 # bfs_path grid cell
OnBoard = Callable[[XY], bool]         # copper at this point stays on the board
EDGE_MARGIN_MM = 0.01                  # over the edge clearance: covers path sampling


class BoardEdge(NamedTuple):
    """The on-board guard: `outline` in the board frame, whose origin sits at
    sheet `origin`, and the board's copper-to-edge clearance (mm)."""
    outline: ol.Outline
    origin: XY
    clearance: float

    def __call__(self, p: XY, clearance: float) -> bool:
        """_on_board(point, clearance): sheet point p is on the board (inside
        the outer ring, outside every cutout) and >= clearance mm from edges."""
        return ol.contains(self.outline, (p[0] - self.origin[0], p[1] - self.origin[1]),
                           clearance)

    def keep(self, radius: float) -> OnBoard:
        """The point predicate for copper of `radius` mm (a via's radius, half
        a track width): radius + the edge clearance + EDGE_MARGIN_MM."""
        need = radius + self.clearance + EDGE_MARGIN_MM
        return lambda p: self(p, need)


def edge_keep(edge: BoardEdge | None, radius: float) -> OnBoard | None:
    """edge.keep(radius), or None (no guard) without an edge."""
    return None if edge is None else edge.keep(radius)


def board_edge(board: pcbnew.BOARD) -> tuple[BoardEdge | None, list[str]]:
    """The board's guard, built once: its Edge.Cuts (board_outline.
    outline_from_board) and m_CopperEdgeClearance. Edge.Cuts that cannot be
    chained fall back to their bounding box with a warning; no bounding box
    at all means no guard (and a warning)."""
    import board_outline as bo      # here, so pure imports never bind it to a stub pcbnew
    clearance = bo.edge_clearance_mm(board)
    try:
        outline, origin = bo.outline_from_board(board)
        return BoardEdge(outline, origin, clearance), []
    except bo.OutlineBuildError as e:
        why = str(e)
    bb = board.GetBoardEdgesBoundingBox()
    w, h = to(bb.GetWidth()), to(bb.GetHeight())
    if w <= 0 or h <= 0:
        return None, [f"on-board guard off: {why}, and Edge.Cuts has no bounding box"]
    rect = ol.Outline(ol.rect_loop(w, h))
    return (BoardEdge(rect, (to(bb.GetLeft()), to(bb.GetTop())), clearance),
            [f"on-board guard uses the Edge.Cuts bounding box: {why}"])


def to(v: int) -> float:
    return pcbnew.ToMM(v)


def mm(v: float) -> int:
    return pcbnew.FromMM(v)


def dist(a: XY, b: XY) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def min_clearance(board: pcbnew.BOARD) -> float:
    """The board's real minimum copper clearance (mm) — margins derived from
    an assumed 0.15 mm fail DRC on a 0.2 mm-rule project."""
    try:
        return max(to(board.GetDesignSettings().GetSmallestClearanceValue()),
                   0.15)
    except Exception:
        return 0.2


def via_w(v: pcbnew.PCB_VIA) -> int:
    """PCB_VIA pad width. KiCad 10's padstack API asserts (and, headless on
    Windows, WEDGES the process in a wx dialog) when GetWidth() is called
    with no layer — a through via is uniform, so F_Cu is always right."""
    try:
        return v.GetWidth(pcbnew.F_Cu)
    except TypeError:
        return v.GetWidth()


def net_tracks(board: pcbnew.BOARD, netcode: int) -> list[pcbnew.PCB_TRACK]:
    return [t for t in board.GetTracks()
            if t.GetClass() == "PCB_TRACK" and t.GetNetCode() == netcode]


def attach_points(board: pcbnew.BOARD, netcode: int, near: XY,
                  reach: float = 1.5) -> list[tuple[float, float, int | None, float]]:
    """Same-net copper attach points within `reach` mm of `near`:
    track endpoints, via centers, pad centers. Returns [(x, y, layer_id, w)]
    (layer_id None for pads/vias = any copper layer)."""
    pts = []
    for t in board.GetTracks():
        if t.GetNetCode() != netcode:
            continue
        if t.GetClass() == "PCB_TRACK":
            for p in (t.GetStart(), t.GetEnd()):
                xy = (to(p.x), to(p.y))
                if dist(xy, near) <= reach:
                    pts.append((xy[0], xy[1], t.GetLayer(), to(t.GetWidth())))
        elif t.GetClass() == "PCB_VIA":
            xy = (to(t.GetPosition().x), to(t.GetPosition().y))
            if dist(xy, near) <= reach:
                pts.append((xy[0], xy[1], None, to(via_w(t))))
    for fp in board.GetFootprints():
        for pad in fp.Pads():
            if pad.GetNetCode() != netcode:
                continue
            xy = (to(pad.GetPosition().x), to(pad.GetPosition().y))
            if dist(xy, near) > reach:
                continue
            if pad.GetAttribute() == pcbnew.PAD_ATTRIB_PTH:
                layer = None                  # barrel — reachable on any layer
            elif pad.IsOnLayer(pcbnew.F_Cu):
                layer = pcbnew.F_Cu
            else:
                layer = pcbnew.B_Cu
            pts.append((xy[0], xy[1], layer, 0.25))
    return pts


def via_template(board: pcbnew.BOARD, netcode: int) -> tuple[int, int]:
    """(width, drill) for a stitching via: copy a same-net via, else the
    smallest via on the board, else the 0.6/0.3 signal default."""
    same = [t for t in board.GetTracks()
            if t.GetClass() == "PCB_VIA" and t.GetNetCode() == netcode]
    pool = same or [t for t in board.GetTracks() if t.GetClass() == "PCB_VIA"]
    if pool:
        v = min(pool, key=via_w)
        return via_w(v), v.GetDrill()
    return mm(0.6), mm(0.3)


def add_via(board: pcbnew.BOARD, net: pcbnew.NETINFO_ITEM, xy: XY, w: int,
            drill: int) -> pcbnew.PCB_VIA:
    v = pcbnew.PCB_VIA(board)
    v.SetPosition(pcbnew.VECTOR2I(mm(xy[0]), mm(xy[1])))
    v.SetWidth(w)
    v.SetDrill(drill)
    v.SetViaType(pcbnew.VIATYPE_THROUGH)
    v.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
    v.SetNet(net)
    board.Add(v)
    return v


def seg_dist(p: XY, a: XY, b: XY) -> float:
    """Distance from point p to segment a-b (all (x,y) mm tuples)."""
    ax, ay = a
    vx, vy = b[0] - ax, b[1] - ay
    L2 = vx * vx + vy * vy
    if L2 < 1e-12:
        return dist(p, a)
    t = max(0.0, min(1.0, ((p[0] - ax) * vx + (p[1] - ay) * vy) / L2))
    return dist(p, (ax + t * vx, ay + t * vy))


def clear_spot(board: pcbnew.BOARD, netcode: int, xy: XY, keep_out: float,
               layer: int | None = None, on_board: OnBoard | None = None) -> bool:
    """True when nothing FOREIGN (other-net copper) comes within keep_out mm
    of xy. layer=None means a through-item (via) — copper on EVERY layer
    blocks; a specific layer tests a track path, so foreign tracks and SMD
    pads on other layers are ignored (through-items always block). Tracks
    are tested as SEGMENTS, not endpoints. With `on_board`, xy must also pass
    it (copper there stays on the board, clear of cutouts and edges)."""
    if on_board is not None and not on_board(xy):
        return False
    for t in board.GetTracks():
        if t.GetNetCode() == netcode:
            continue
        if t.GetClass() == "PCB_VIA":
            p = t.GetPosition()
            if dist((to(p.x), to(p.y)), xy) < keep_out + to(via_w(t)) / 2:
                return False
        else:
            if layer is not None and t.GetLayer() != layer:
                continue
            s, e = t.GetStart(), t.GetEnd()
            d = seg_dist(xy, (to(s.x), to(s.y)), (to(e.x), to(e.y)))
            if d < keep_out + to(t.GetWidth()) / 2:
                return False
    for fp in board.GetFootprints():
        for pad in fp.Pads():
            if pad.GetNetCode() == netcode:
                continue
            if (layer is not None
                    and pad.GetAttribute() != pcbnew.PAD_ATTRIB_PTH
                    and not pad.IsOnLayer(layer)):
                continue
            bb = pad.GetBoundingBox()
            # rectangle distance, not circumscribed circle — a circle test
            # around a large inductor pad vetoes everything within ~3 mm
            dx = max(abs(xy[0] - to(bb.GetCenter().x)) - to(bb.GetWidth()) / 2, 0)
            dy = max(abs(xy[1] - to(bb.GetCenter().y)) - to(bb.GetHeight()) / 2, 0)
            if math.hypot(dx, dy) < keep_out:
                return False
    return True


def clear_path(board: pcbnew.BOARD, netcode: int, a: XY, b: XY, keep_out: float,
               layer: int | None = None, step: float = 0.3,
               on_board: OnBoard | None = None) -> bool:
    """True when the whole a->b segment stays keep_out clear of foreign
    copper on `layer` (sampled every `step` mm), each sample passing
    `on_board` when given."""
    n = max(1, int(dist(a, b) / step))
    for i in range(n + 1):
        f = i / n
        if not clear_spot(board, netcode,
                          (a[0] + (b[0] - a[0]) * f,
                           a[1] + (b[1] - a[1]) * f), keep_out, layer, on_board):
            return False
    return True


def nearest_net_via(board: pcbnew.BOARD, netcode: int, pos: XY,
                    reach: float = 2.0) -> XY | None:
    """Center (x, y) of the closest same-net via within reach, else None."""
    best, bd = None, reach
    for t in board.GetTracks():
        if t.GetClass() != "PCB_VIA" or t.GetNetCode() != netcode:
            continue
        xy = (to(t.GetPosition().x), to(t.GetPosition().y))
        d = dist(xy, pos)
        if d < bd:
            best, bd = xy, d
    return best


def net_vias_near(board: pcbnew.BOARD, netcode: int, pos: XY,
                  reach: float = 3.0) -> list[XY]:
    """Same-net via centers within reach of pos, nearest first."""
    out = []
    for t in board.GetTracks():
        if t.GetClass() != "PCB_VIA" or t.GetNetCode() != netcode:
            continue
        xy = (to(t.GetPosition().x), to(t.GetPosition().y))
        if dist(xy, pos) <= reach:
            out.append(xy)
    return sorted(out, key=lambda xy: dist(xy, pos))


def bfs_path(board: pcbnew.BOARD, nc: int, a: XY, b: XY, layer: int, keep: float,
             step: float = 0.4, on_board: OnBoard | None = None) -> list[XY] | None:
    """Coarse maze route from a to b on one layer: BFS over a step-mm grid,
    cells blocked by foreign copper (and, with `on_board`, cells it rejects:
    off the board, in a cutout or within the edge clearance). Returns a
    rectilinear polyline (incl. endpoints) or None. This is the escape when
    every fixed path shape is blocked — e.g. threading a via fanout field
    under an MCU."""
    grid = _maze_grid(board, a, b, step)
    if grid is None:
        return None
    x0, y0, nx, ny = grid

    def cell(p: XY) -> Cell:
        return (min(nx - 1, max(0, int(round((p[0] - x0) / step)))),
                min(ny - 1, max(0, int(round((p[1] - y0) / step)))))

    def pt(c: Cell) -> XY:
        return (x0 + c[0] * step, y0 + c[1] * step)

    blocked_cache: dict[Cell, bool] = {}

    def is_blocked(c: Cell) -> bool:
        if c not in blocked_cache:
            blocked_cache[c] = not clear_spot(board, nc, pt(c), keep, layer, on_board)
        return blocked_cache[c]

    cells = _bfs_cells(cell(a), cell(b), nx, ny, is_blocked)
    if cells is None:
        return None
    # merge colinear runs into corner points
    pts = [a]
    for i in range(1, len(cells) - 1):
        dp = (cells[i][0] - cells[i - 1][0], cells[i][1] - cells[i - 1][1])
        dn = (cells[i + 1][0] - cells[i][0], cells[i + 1][1] - cells[i][1])
        if dp != dn:
            pts.append(pt(cells[i]))
    pts.append(b)
    return pts


def _maze_grid(board: pcbnew.BOARD, a: XY, b: XY,
               step: float) -> tuple[float, float, int, int] | None:
    """(x0, y0, nx, ny) of bfs_path's grid: the a-b box grown by 5 mm,
    clamped 0.5 mm inside the Edge.Cuts bbox; None above 40000 cells."""
    bb = board.GetBoardEdgesBoundingBox()
    x0 = max(min(a[0], b[0]) - 5.0, to(bb.GetLeft()) + 0.5)
    y0 = max(min(a[1], b[1]) - 5.0, to(bb.GetTop()) + 0.5)
    x1 = min(max(a[0], b[0]) + 5.0, to(bb.GetRight()) - 0.5)
    y1 = min(max(a[1], b[1]) + 5.0, to(bb.GetBottom()) - 0.5)
    nx = int((x1 - x0) / step) + 1
    ny = int((y1 - y0) / step) + 1
    return None if nx * ny > 40000 else (x0, y0, nx, ny)


def _bfs_cells(start: Cell, goal: Cell, nx: int, ny: int,
               is_blocked: Callable[[Cell], bool]) -> list[Cell] | None:
    """The BFS cell path start -> goal on an nx x ny grid (the goal cell is
    always enterable), or None when the goal cannot be reached."""
    came: dict[Cell, Cell | None] = {start: None}
    q = deque([start])
    while q:
        c = q.popleft()
        if c == goal:
            break
        for d in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            n = (c[0] + d[0], c[1] + d[1])
            if (0 <= n[0] < nx and 0 <= n[1] < ny and n not in came
                    and (n == goal or not is_blocked(n))):
                came[n] = c
                q.append(n)
    if goal not in came:
        return None
    cells = []
    c = goal
    while c is not None:
        cells.append(c)
        c = came[c]
    return cells[::-1]
