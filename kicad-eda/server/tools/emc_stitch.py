#!/usr/bin/env python3
"""EMC ground stitching — add a GND via fence + interior stitching grid to a
routed board, DRC-safely. Runs under KiCad's bundled Python (needs pcbnew).

Why: a perimeter via fence contains edge fields, an interior grid bonds the GND
planes so the return path follows the signal (Ott; Montrose). Pitch: lambda_g / 20
at `max_freq_hz`, else a fixed pitch. Each via clears all non-GND copper and every
hole, and lies ON the board (board_outline.outline_from_board; its copper keeps
the copper-to-edge clearance from every outline and cutout edge). The fence walks
the outline inset by the fence distance along its exact lines and arcs: a via on
every corner, evenly by arc length between, at most one pitch apart;
--fence-cutouts also fences the grown cutout rings. A plain axis-aligned rectangle
keeps the legacy bbox walk (the same vias as before). With a warning: an Edge.Cuts
that is not one closed outline falls back to the bbox; an outline that cannot be
inset (a neck under twice the fence distance) to the bbox fence, filtered by it.

Usage:
  <kicad-python> emc_stitch.py <pcb> [max_freq_hz=0] [mode=grid|fence]
                               [gnd_net=GND] [er=4.2] [via_dia_mm=0.6] [via_drill_mm=0.3]
                               [--fence-cutouts]
Arguments after <pcb> are positional and optional; the last stdout line is the JSON.
"""
from __future__ import annotations

import json
import math
import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import kicad_quiet  # noqa: F401  (before pcbnew: no modal wx dialogs)

try:
    import pcbnew
except ImportError:          # the dev venv: only the pure fence / grid helpers work
    pcbnew = None

import board_outline as bo  # noqa: E402
import outline as ol  # noqa: E402

C0 = 299_792_458.0  # m/s
DEFAULT_GND_NET = "GND"
DEFAULT_ER = 4.2            # FR-4 relative permittivity
DEFAULT_VIA_DIA_MM = 0.6
DEFAULT_VIA_DRILL_MM = 0.3
USAGE_EXIT = 2
FENCE_CUTOUTS_FLAG = "--fence-cutouts"
FENCE_MARGIN_MM = 0.1       # the fence never sits closer than the via keep-out + this
EDGE_MARGIN_MM = 0.01       # containment margin over copper-edge clearance + via radius
OWN_SPACING = 0.7           # our own vias stay at least this many pitches apart
CORNER_RAD = math.radians(5.0)   # a fence-ring joint turning more than this gets a via
_RECT_TOL_MM = 1e-6

Point = tuple[float, float]
NmPoint = tuple[int, int]
Box = tuple[int, int, int, int]       # x0, y0, x1, y1 (nm)
Shape = tuple[ol.Outline, Point]      # board-frame outline, its sheet origin (mm)


def _pitch_mm(max_freq_hz: float, er: float, default: float) -> float:
    if max_freq_hz and max_freq_hz > 0:
        lam_g = C0 / (max_freq_hz * math.sqrt(er)) * 1000.0  # mm, guided
        return max(1.0, round(lam_g / 20.0, 2))
    return default


def rect_fence_points(box: Box, pitch: int) -> list[NmPoint]:
    """The legacy fence on the inset box, in its placement order: the top and
    bottom rows from x0 every pitch, then both sides from y0 + pitch."""
    x0, y0, x1, y1 = box
    return ([(x, y) for x in range(x0, x1 + 1, pitch) for y in (y0, y1)]
            + [(x, y) for y in range(y0 + pitch, y1, pitch) for x in (x0, x1)])


def grid_points(box: Box, pitch: int) -> list[NmPoint]:
    """The interior grid inside the inset box, one pitch in from its sides."""
    x0, y0, x1, y1 = box
    return [(x, y) for y in range(y0 + pitch, y1, pitch) for x in range(x0 + pitch, x1, pitch)]


def is_axis_rect(o: ol.Outline) -> bool:
    """A plain axis-aligned rectangle: 4 straight edges, no cutouts."""
    ring = o.outer
    if o.cutouts or not isinstance(ring, ol.Loop) or len(ring.vertices) != 4:
        return False
    v = ring.vertices
    return all(abs(b) < ol.BULGE_EPS and (abs(x - v[i - 1][0]) <= _RECT_TOL_MM
                                          or abs(y - v[i - 1][1]) <= _RECT_TOL_MM)
               for i, (x, y, b) in enumerate(v))


def _key(p: Point) -> tuple[float, float]:
    """Canonical order of points: lowest y, then lowest x (to 1 nm)."""
    return (round(p[1], 6), round(p[0], 6))


def _runs(prims: Sequence) -> list[list]:
    """The closed ring split at its corners (the tangent turns > CORNER_RAD),
    each run starting at one; without corners, one run from the lowest (y, x)
    primitive start, so the points never depend on where a file began the ring."""
    n, first = len(prims), min(range(len(prims)), key=lambda j: _key(prims[j].start))
    prims = list(prims[first:]) + list(prims[:first])
    joints = [(prims[j - 1].tangent(1.0), prims[j].tangent(0.0)) for j in range(n)]
    corners = [j for j, ((ax, ay), (bx, by)) in enumerate(joints)
               if abs(math.atan2(ax * by - ay * bx, ax * bx + ay * by)) > CORNER_RAD]
    if not corners:
        return [list(prims)]
    ring = list(prims[corners[0]:]) + list(prims[:corners[0]])
    cuts = [c - corners[0] for c in corners] + [n]
    return [ring[a:b] for a, b in zip(cuts, cuts[1:], strict=False)]


def _along(run: Sequence, count: int) -> list[Point]:
    """`count` points evenly by arc length along `run`, start in, end out."""
    lengths = [p.length for p in run]
    step = sum(lengths) / count
    out, i, acc = [], 0, 0.0
    for k in range(count):
        while i < len(run) - 1 and acc + lengths[i] <= k * step:
            acc, i = acc + lengths[i], i + 1
        t = (k * step - acc) / lengths[i] if lengths[i] > 0 else 0.0
        out.append(run[i].point_at(min(max(t, 0.0), 1.0)))
    return out


def fence_ring_points(prims: Sequence, pitch_mm: float) -> list[Point]:
    """Fence points on one closed ring of exact lines / arcs: every corner, and
    each run between corners evenly by arc length, neighbours <= pitch_mm apart."""
    return [p for run in _runs(prims)
            for p in _along(run, max(1, math.ceil(sum(q.length for q in run) / pitch_mm - 1e-9)))]


def outline_fence_points(o: ol.Outline, inset_mm: float, pitch_mm: float,
                         cutouts: bool = False) -> list[Point]:
    """Board-frame fence points on `o` inset by `inset_mm`: its outer ring, then
    (with `cutouts`) each grown cutout ring, lowest (y, x) first. Raises
    ol.OutlineError when the outline cannot be inset (e.g. a neck narrower than
    2 * inset_mm splits it)."""
    inner = ol.inset(o, inset_mm)
    holes = sorted(inner.rings[1:], key=lambda r: min(_key(p.start) for p in r))
    rings = [inner.rings[0], *holes] if cutouts else inner.rings[:1]
    return [p for prims in rings for p in fence_ring_points(prims, pitch_mm)]


def _gnd_netcode(board: Any, gnd_name: str) -> int | None:
    return next((code for code, ni in board.GetNetInfo().NetsByNetcode().items()
                 if ni.GetNetname() == gnd_name), None)


def _obstacles(board, gnd_code: int):
    """(x,y,clear_radius_nm) circles for every non-GND pad/track to avoid, plus
    (x,y,drill_radius_nm) holes for hole-to-hole spacing."""
    ds = board.GetDesignSettings()
    base = ds.GetSmallestClearanceValue() if hasattr(ds, "GetSmallestClearanceValue") \
        else pcbnew.FromMM(0.2)
    # conservative: >= 0.2 mm + a margin; better to drop a via than add one that violates
    clr = max(base, pcbnew.FromMM(0.2)) + pcbnew.FromMM(0.1)
    circles, holes = [], []
    for fp in board.GetFootprints():
        for pad in fp.Pads():
            pos = pad.GetPosition()
            r = max(pad.GetBoundingRadius(), 1)
            if pad.GetNetCode() != gnd_code:
                circles.append((pos.x, pos.y, r + clr))
            d = pad.GetDrillSize()
            if d.x > 0:
                holes.append((pos.x, pos.y, d.x // 2))
    for t in board.GetTracks():
        if t.GetClass() == "PCB_VIA":
            pos = t.GetPosition()
            holes.append((pos.x, pos.y, t.GetDrillValue() // 2))
            if t.GetNetCode() != gnd_code:
                # KiCad 10's GetWidth needs a layer; a through via is uniform: F.Cu
                try:
                    vw = t.GetWidth(pcbnew.F_Cu)
                except TypeError:
                    vw = t.GetWidth()
                circles.append((pos.x, pos.y, vw // 2 + clr))
        elif t.GetClass() == "PCB_TRACK" and t.GetNetCode() != gnd_code:
            # sampled every 0.5 mm so a via can't slip between samples by a thin trace
            a, b = t.GetStart(), t.GetEnd()
            n = max(1, int(math.hypot(b.x - a.x, b.y - a.y) // pcbnew.FromMM(0.5)))
            for i in range(n + 1):
                circles.append((a.x + (b.x - a.x) * i // n, a.y + (b.y - a.y) * i // n,
                                t.GetWidth() // 2 + clr))
    return circles, holes, clr


@dataclass(frozen=True)
class _Rules:
    """Placement rules (nm): keepout = via centre to board edge (copper-edge
    clearance + via radius), inset = the fence distance, box = Edge.Cuts bbox."""
    pitch: int
    via_d: int
    via_r: int
    drill: int
    h2h: int
    keepout: int
    inset: int
    box: Box

    def inset_box(self) -> Box:
        return (self.box[0] + self.inset, self.box[1] + self.inset,
                self.box[2] - self.inset, self.box[3] - self.inset)


def _rule_nm(ds: Any, getter: str, attr: str, floor_nm: int) -> int:
    """A design rule: its getter where pcbnew has one (as before); 9.0.4 and
    10.0.5 have none, so the legacy floor, raised to the board's m_ value."""
    fn = getattr(ds, getter, None)
    return int(fn()) if fn is not None else max(floor_nm, int(getattr(ds, attr, 0)))


def _rules(board: Any, via_d_mm: float, via_drill_mm: float, fence_inset_mm: float,
           pitch_mm: float) -> _Rules:
    """Via keep-out from the real edge clearance; hole spacing = the real minimum +
    0.1 mm; the fence at the caller's inset, never inside the keep-out."""
    ds, mm = board.GetDesignSettings(), pcbnew.FromMM
    via_r = mm(via_d_mm) // 2
    keepout = _rule_nm(ds, "GetCopperEdgeClearance", "m_CopperEdgeClearance", mm(0.5)) + via_r
    bb = board.GetBoardEdgesBoundingBox()
    return _Rules(pitch=mm(pitch_mm), via_d=mm(via_d_mm), via_r=via_r, drill=mm(via_drill_mm),
                  h2h=_rule_nm(ds, "GetHoleToHoleMin", "m_HoleToHoleMin", mm(0.25)) + mm(0.1),
                  keepout=keepout, inset=max(mm(fence_inset_mm), keepout + mm(FENCE_MARGIN_MM)),
                  box=(bb.GetLeft(), bb.GetTop(), bb.GetRight(), bb.GetBottom()))


@dataclass
class _Stitcher:
    """Adds GND through vias where they clear the non-GND copper (circles), the
    holes, the bbox keep-out and the outline (shape; None: bbox only), keeping
    our own vias OWN_SPACING pitches apart; counts them by kind."""
    board: Any
    gnd_code: int
    rules: _Rules
    circles: list
    holes: list
    shape: Shape | None
    placed: dict = field(default_factory=lambda: {"fence": 0, "interior": 0})
    new_holes: list = field(default_factory=list)

    def clears(self, x: int, y: int) -> bool:
        r, (x0, y0, x1, y1) = self.rules, self.rules.box
        k = r.keepout
        if not (x0 + k <= x <= x1 - k and y0 + k <= y <= y1 - k):
            return False
        if any((x - cx) ** 2 + (y - cy) ** 2 < (cr + r.via_r) ** 2
               for cx, cy, cr in self.circles):
            return False
        if any((x - cx) ** 2 + (y - cy) ** 2 < (cr + r.drill // 2 + r.h2h) ** 2
               for cx, cy, cr in self.holes):
            return False
        if self.shape is None:
            return True
        (o, (ox, oy)), mm = self.shape, bo.NM_PER_MM
        return ol.contains(o, (x / mm - ox, y / mm - oy), k / mm + EDGE_MARGIN_MM)

    def place(self, x: int, y: int, kind: str) -> None:
        lim = (self.rules.pitch * OWN_SPACING) ** 2
        if not self.clears(x, y) or any((x - hx) ** 2 + (y - hy) ** 2 < lim
                                        for hx, hy in self.new_holes):
            return
        v = pcbnew.PCB_VIA(self.board)
        v.SetPosition(pcbnew.VECTOR2I(x, y))
        v.SetWidth(self.rules.via_d)
        v.SetDrill(self.rules.drill)
        v.SetNetCode(self.gnd_code)
        v.SetViaType(pcbnew.VIATYPE_THROUGH)
        self.board.Add(v)
        self.new_holes.append((x, y))
        self.placed[kind] += 1


def _fence_points(rules: _Rules, shape: Shape | None,
                  fence_cutouts: bool) -> tuple[list[NmPoint], str, list[str]]:
    """(fence points in sheet nm, outline kind bbox|rect|polygon, warnings)."""
    legacy = rect_fence_points(rules.inset_box(), rules.pitch)
    if shape is None:
        return legacy, "bbox", []
    o, (ox, oy) = shape
    if is_axis_rect(o):
        return legacy, "rect", []
    inset_mm = rules.inset / bo.NM_PER_MM
    try:
        pts = outline_fence_points(o, inset_mm, rules.pitch / bo.NM_PER_MM, fence_cutouts)
    except ol.OutlineError as e:
        return legacy, "polygon", [f"fence: the outline cannot be inset by {inset_mm:g} mm "
                                   f"({e}); using the bbox fence, filtered by the outline"]
    return [(bo.nm(x + ox), bo.nm(y + oy)) for x, y in pts], "polygon", []


def add_stitching(board, gnd_name=DEFAULT_GND_NET, via_d_mm=DEFAULT_VIA_DIA_MM,
                  via_drill_mm=DEFAULT_VIA_DRILL_MM, fence_inset_mm=0.5,
                  max_freq_hz=0.0, er=DEFAULT_ER, default_pitch_mm=3.0,
                  interior=True, fence_cutouts: bool = False) -> dict:
    gnd_code = _gnd_netcode(board, gnd_name)
    if gnd_code is None:
        return {"ok": False, "reason": f"no '{gnd_name}' net on board"}
    rules = _rules(board, via_d_mm, via_drill_mm, fence_inset_mm,
                   _pitch_mm(max_freq_hz, er, default_pitch_mm))
    try:
        shape, warnings = bo.outline_from_board(board), []
    except bo.OutlineBuildError as e:
        shape, warnings = None, [f"Edge.Cuts outline unusable ({e}): vias are checked against "
                                 "its bounding box only (they may land in cutouts)"]
    circles, holes, _clr = _obstacles(board, gnd_code)
    placer = _Stitcher(board, gnd_code, rules, circles, holes, shape)
    fence, kind, fence_warnings = _fence_points(rules, shape, fence_cutouts)
    for x, y in fence:                       # perimeter fence first
        placer.place(x, y, "fence")
    if interior:                             # then the grid, filtered the same way
        for x, y in grid_points(rules.inset_box(), rules.pitch):
            placer.place(x, y, "interior")
    return {"ok": True, "gnd": gnd_name, "pitch_mm": round(pcbnew.ToMM(rules.pitch), 2),
            "via_dia_mm": via_d_mm, "via_drill_mm": via_drill_mm, "er": er,
            "vias_added": sum(placer.placed.values()), **placer.placed,
            "outline": kind, "fence_cutouts": bool(fence_cutouts),
            "warnings": warnings + fence_warnings}


def parse_args(argv: list) -> dict:
    """<pcb> [max_freq_hz] [mode] [gnd_net] [er] [via_dia_mm] [via_drill_mm]
    [--fence-cutouts] — positional, optional, validated; the flag may stand
    anywhere after the script. Raises ValueError with a clear message."""
    flags = {a for a in argv[1:] if a.startswith("--")}
    if flags - {FENCE_CUTOUTS_FLAG}:
        raise ValueError(f"unknown option(s) {sorted(flags - {FENCE_CUTOUTS_FLAG})}; the "
                         f"only option is {FENCE_CUTOUTS_FLAG}")
    argv = argv[:1] + [a for a in argv[1:] if not a.startswith("--")]
    if len(argv) < 2:
        raise ValueError("usage: emc_stitch.py <pcb> [max_freq_hz] [grid|fence] "
                         "[gnd_net] [er] [via_dia_mm] [via_drill_mm] [--fence-cutouts]")

    def num(i, default, name):
        if len(argv) <= i or argv[i] == "":
            return default
        try:
            return float(argv[i])
        except ValueError as e:
            raise ValueError(f"{name} must be a number, got {argv[i]!r}") from e

    if not os.path.isfile(argv[1]):
        raise ValueError(f"board file not found: {argv[1]}")
    args = {
        "pcb": argv[1], "max_freq_hz": num(2, 0.0, "max_freq_hz"),
        "interior": (argv[3].lower() != "fence") if len(argv) > 3 else True,
        "gnd_net": argv[4] if len(argv) > 4 and argv[4] else DEFAULT_GND_NET,
        "er": num(5, DEFAULT_ER, "er"),
        "via_d_mm": num(6, DEFAULT_VIA_DIA_MM, "via_dia_mm"),
        "via_drill_mm": num(7, DEFAULT_VIA_DRILL_MM, "via_drill_mm"),
        "fence_cutouts": FENCE_CUTOUTS_FLAG in flags,
    }
    if args["er"] <= 0:
        raise ValueError(f"er must be > 0, got {args['er']}")
    if args["via_drill_mm"] <= 0 or args["via_d_mm"] <= args["via_drill_mm"]:
        raise ValueError(f"need via_dia_mm > via_drill_mm > 0, got "
                         f"{args['via_d_mm']} / {args['via_drill_mm']}")
    return args


def _refill_and_save(board: Any, pcb: str, res: dict) -> dict:
    """Refill zones (a non-GND plane must open a clearance around each new GND via
    or the via shorts it), then save."""
    board.BuildConnectivity()
    warnings = list(res["warnings"])
    try:
        pcbnew.ZONE_FILLER(board).Fill(board.Zones())
    except Exception as e:
        warnings.append(f"zone refill failed ({type(e).__name__}: {e}) — power planes may "
                        f"not clear the new vias; DRC will report it")
    pcbnew.SaveBoard(pcb, board)
    return {**res, "warnings": warnings}


def main() -> int:
    try:
        a = parse_args(sys.argv)
    except ValueError as e:
        print(json.dumps({"ok": False, "reason": str(e)}))
        return USAGE_EXIT
    try:                      # FileNotFoundError, or LoadBoard failing / returning None
        board = bo.load_board(a["pcb"])
    except (OSError, RuntimeError) as e:
        print(json.dumps({"ok": False, "reason": f"cannot load board: {e}"}))
        return 1
    res = add_stitching(board, gnd_name=a["gnd_net"], via_d_mm=a["via_d_mm"],
                        via_drill_mm=a["via_drill_mm"], max_freq_hz=a["max_freq_hz"],
                        er=a["er"], interior=a["interior"], fence_cutouts=a["fence_cutouts"])
    if res.get("ok"):
        res = _refill_and_save(board, a["pcb"], res)
    print(json.dumps(res))
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
