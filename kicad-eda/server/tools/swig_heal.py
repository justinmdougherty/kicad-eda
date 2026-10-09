#!/usr/bin/env python3
"""G4 micro-gap healer at the net-class width — runs under KiCad's Python.

swig_board_fixes.py (modes heal / all) calls heal_micro_gaps(board, drc,
class_widths) for the DRC "unconnected items" pairs a route left behind:

  bridge  the nearest same-net anchors <= MAX_GAP apart on a shared layer get
          one straight track (router / SES artifact);
  link    else the two islands are linked anchor to anchor: straight, then L,
          then a coarse BFS maze route (threads via fanout fields; on a plane
          net it repairs the fill void the DSN plane abstraction hid);
  stitch  else a same-net via on a layer-locked anchor buys layer freedom and
          the link is laid from it (the via is taken back when nothing lands).

Widths: on a net bound to a net class every heal track is max(attach width,
class width) — check_routed_widths blocks sub-width open-field copper — and
an unclassed net keeps the old widths (bridge = narrowest attach, link 0.3
mm). Keep-out = width / 2 + the board's min clearance + 0.05 mm. A break with
no clear path at that width gets NO copper and an open entry instead
('<net>: no clear path at the <w> mm class width'), so the harness escalates.

Islands: pcbnew cannot list a connectivity cluster from Python on both
supported versions (probed: 9.0.4 CONNECTIVITY_DATA.GetConnectedItems and
GetNetItems need a std::vector<KICAD_T> SWIG cannot build; 10.0.5 only has
the default-types overload), so a net's copper is grouped by a geometric
union-find: a track end, pad centre or via centre lying on another item's
copper (pad bbox, track body, via disc) on a shared layer joins the two; a
filled zone outline joins the pads, track ends and vias it covers and the
same-net outlines it overlaps. Each DRC item (by uuid, else by position)
maps to its island; DRC pairs naming the same two islands are one break,
healed at most once, and a break an earlier link already joined is skipped.

Determinism: island pairs sort by (net, island key = min (x, y) over member
items), anchors by (x, y, layer), anchor pairs by (distance, coordinates);
nothing depends on board iteration order or on the DRC listing order (the
board's random uuids reorder both). Every path is re-validated with
swig_copper.clear_path at the link keep-out before anything is added — a
maze path's off-grid first and last segments included; a bad connector is
re-routed onto the grid line the maze validated, else the path is rejected.
"""
from __future__ import annotations

import itertools
import math
import re
from typing import Callable, Iterable, NamedTuple

import kicad_quiet  # noqa: F401  (before pcbnew: no modal wx dialogs)
import pcbnew

import netclass_read
import swig_copper as cu

MAX_GAP = 2.0          # mm — a gap beyond this is linked, not bridged
TOL = 0.02             # mm — coincidence tolerance (track ends, anchors)
LINK_W = 0.3           # mm — link width on an unclassed net (unchanged)
PAD_W = 0.25           # mm — attach width of a pad anchor (swig_copper.attach_points)
KEEP_MARGIN = 0.05     # mm — over the board's min clearance
SCREEN_STEP = 0.3      # mm — clear_path sampling while searching
VALIDATE_STEP = 0.1    # mm — clear_path sampling of the path that is laid
VIA_KEEP = 0.25        # mm — keep-out around a stitched via, beyond its pad
STRAIGHT_PAIRS, MAZE_PAIRS, STITCH_SPOTS = 10, 6, 6
NEAREST_ANCHORS = 64   # per side, when two big islands meet
FILL_SAMPLES = 64      # outline vertices kept for fill-to-fill joins
NET_IN_DESC = re.compile(r"\[(.+?)\]")

XY = tuple[float, float]
Box = tuple[float, float, float, float]     # left, top, right, bottom (mm)


class Anchor(NamedTuple):
    x: float
    y: float
    layer: int | None          # None: a through item, reachable on any copper layer
    w: float                   # attach width (mm)


class Element(NamedTuple):
    kind: str                  # "pad" | "track" | "via" | "fill"
    uuid: str
    box: Box
    layers: frozenset | None   # copper layer ids; None = every copper layer
    pts: tuple                 # pad: (centre,), track: (start, end), via: (centre,)
    w: float = 0.0             # track / via width
    contains: Callable | None = None    # fill: (xy, accuracy mm) -> bool
    samples: tuple = ()        # fill: outline vertices for fill-to-fill joins


class Island(NamedTuple):
    key: XY                    # min (x, y) over the member items
    anchors: tuple             # Anchor, sorted by (x, y, layer, w)
    members: tuple             # element indices


class Break(NamedTuple):
    net: str
    items: tuple               # ((uuid, (x, y)), (uuid, (x, y)))


# ---------------------------------------------------------------------------
# DRC report -> same-net breaks
# ---------------------------------------------------------------------------
def drc_breaks(drc: dict) -> list[Break]:
    """Same-net two-item 'unconnected items' pairs of a kicad-cli DRC report."""
    out = []
    for entry in drc.get("unconnected_items") or []:
        items = entry.get("items") or []
        nets = [NET_IN_DESC.search(i.get("description", "")) for i in items]
        if len(items) != 2 or not all(nets) or nets[0].group(1) != nets[1].group(1):
            continue
        ends = tuple((str(i.get("uuid", "")), _pos(i)) for i in items)
        out.append(Break(nets[0].group(1), ends))
    return out


def _pos(item: dict) -> XY:
    p = item.get("pos") or {}
    return float(p.get("x", 0)), float(p.get("y", 0))


# ---------------------------------------------------------------------------
# Elements (pure geometry)
# ---------------------------------------------------------------------------
def pad_element(uuid: str, centre: XY, box: Box, layers: frozenset,
                pth: bool = False) -> Element:
    return Element("pad", uuid, tuple(box), None if pth else frozenset(layers),
                   (tuple(centre),))


def track_element(uuid: str, a: XY, b: XY, w: float, layer: int) -> Element:
    r = w / 2
    box = (min(a[0], b[0]) - r, min(a[1], b[1]) - r, max(a[0], b[0]) + r, max(a[1], b[1]) + r)
    return Element("track", uuid, box, frozenset({layer}), (tuple(a), tuple(b)), w)


def via_element(uuid: str, c: XY, w: float) -> Element:
    r = w / 2
    return Element("via", uuid, (c[0] - r, c[1] - r, c[0] + r, c[1] + r), None, (tuple(c),), w)


def fill_element(uuid: str, layer: int, box: Box, contains: Callable,
                 samples: Iterable = ()) -> Element:
    return Element("fill", uuid, tuple(box), frozenset({layer}), (), 0.0, contains,
                   tuple(samples))


def _in_box(box: Box, p: XY, reach: float) -> bool:
    return (box[0] - reach <= p[0] <= box[2] + reach
            and box[1] - reach <= p[1] <= box[3] + reach)


def _boxes_meet(a: Box, b: Box) -> bool:
    return (a[0] <= b[2] + 2 * TOL and b[0] <= a[2] + 2 * TOL
            and a[1] <= b[3] + 2 * TOL and b[1] <= a[3] + 2 * TOL)


def _layers_meet(a: frozenset | None, b: frozenset | None) -> bool:
    return a is None or b is None or bool(a & b)


def covers(e: Element, p: XY, reach: float = TOL) -> bool:
    """True when point p lies on e's copper (within reach mm)."""
    if e.kind == "pad":
        return _in_box(e.box, p, reach)
    if e.kind == "track":
        return cu.seg_dist(p, e.pts[0], e.pts[1]) <= e.w / 2 + reach
    if e.kind == "via":
        return cu.dist(p, e.pts[0]) <= e.w / 2 + reach
    return bool(e.contains(p, reach))


def _pad_samples(box: Box) -> tuple:
    """Centre, edge midpoints and the inscribed ellipse at 45 deg of a pad:
    where a zone's solid fill or thermal spokes meet it."""
    cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    hw, hh = (box[2] - box[0]) / 2, (box[3] - box[1]) / 2
    k = math.sqrt(0.5)
    return ((cx, cy), (cx - hw, cy), (cx + hw, cy), (cx, cy - hh), (cx, cy + hh),
            (cx - k * hw, cy - k * hh), (cx + k * hw, cy - k * hh),
            (cx - k * hw, cy + k * hh), (cx + k * hw, cy + k * hh))


def _fill_joined(fill: Element, other: Element) -> bool:
    if other.kind == "fill":
        return (any(fill.contains(p, TOL) for p in other.samples)
                or any(other.contains(p, TOL) for p in fill.samples))
    if other.kind == "pad":
        return any(fill.contains(p, TOL) for p in _pad_samples(other.box))
    return any(fill.contains(p, other.w / 2) for p in other.pts)   # via disc / track end


def joined(e: Element, f: Element) -> bool:
    """Same-net copper e and f touch (KiCad anchor rule: a track end, pad
    centre or via centre of one lies on the other's copper)."""
    if not _layers_meet(e.layers, f.layers):
        return False
    if e.kind == "fill" or f.kind == "fill":
        return _fill_joined(e, f) if e.kind == "fill" else _fill_joined(f, e)
    return any(covers(f, p) for p in e.pts) or any(covers(e, p) for p in f.pts)


def island_roots(elements: list) -> list[int]:
    """Union-find root index per element (sweep over box left edges)."""
    parent = list(range(len(elements)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    active: list[int] = []
    for i in sorted(range(len(elements)), key=lambda k: elements[k].box[0]):
        box = elements[i].box
        active = [j for j in active if elements[j].box[2] + 2 * TOL >= box[0]]
        for j in active:
            if _boxes_meet(elements[j].box, box) and joined(elements[i], elements[j]):
                parent[find(i)] = find(j)
        active.append(i)
    return [find(i) for i in range(len(elements))]


def _key_point(e: Element) -> XY:
    if e.kind == "fill":
        return e.box[0], e.box[1]
    return min(e.pts)


def anchor_key(a: Anchor) -> tuple:
    return a.x, a.y, -1 if a.layer is None else a.layer, a.w


def _pad_layer(e: Element) -> int | None:
    if e.layers is None:
        return None                       # PTH barrel: any layer
    for layer in (pcbnew.F_Cu, pcbnew.B_Cu):
        if layer in e.layers:
            return layer
    return min(e.layers) if e.layers else -1


def element_anchors(e: Element) -> list[Anchor]:
    if e.kind == "track":
        return [Anchor(p[0], p[1], next(iter(e.layers)), e.w) for p in e.pts]
    if e.kind == "via":
        return [Anchor(e.pts[0][0], e.pts[0][1], None, e.w)]
    if e.kind == "pad" and _pad_layer(e) != -1:
        return [Anchor(e.pts[0][0], e.pts[0][1], _pad_layer(e), PAD_W)]
    return []


def _anchor_set(elements: Iterable) -> tuple:
    best: dict = {}
    for a in itertools.chain.from_iterable(element_anchors(e) for e in elements):
        k = anchor_key(a)[:3]
        if k not in best or a.w < best[k].w:
            best[k] = a
    return tuple(sorted(best.values(), key=anchor_key))


def islands(elements: list) -> list[Island]:
    """The net's copper islands sorted by key (then anchors)."""
    groups: dict = {}
    for i, root in enumerate(island_roots(elements)):
        groups.setdefault(root, []).append(i)
    out = [Island(min(_key_point(elements[i]) for i in members),
                  _anchor_set(elements[i] for i in members), tuple(members))
           for members in groups.values()]
    return sorted(out, key=lambda isl: (isl.key, isl.anchors))


def locate(elements: list, uuid: str, pos: XY) -> int | None:
    """The element a DRC item names: by uuid (a zone: its outline under pos),
    else the first element under pos."""
    same = [i for i, e in enumerate(elements) if uuid and e.uuid == uuid]
    if len(same) == 1:
        return same[0]
    hits = [i for i in (same or range(len(elements))) if covers(elements[i], pos)]
    if hits:
        return hits[0]
    if same:
        return min(same, key=lambda i: (_box_gap(elements[i].box, pos), i))
    return None


def _box_gap(box: Box, p: XY) -> float:
    dx = max(box[0] - p[0], 0.0, p[0] - box[2])
    dy = max(box[1] - p[1], 0.0, p[1] - box[3])
    return math.hypot(dx, dy)


# ---------------------------------------------------------------------------
# Ordering and widths (pure)
# ---------------------------------------------------------------------------
def _d(a: Anchor, b: Anchor) -> float:
    return math.hypot(a.x - b.x, a.y - b.y)


def _nearest(side: tuple, other: tuple, k: int) -> tuple:
    if len(side) <= k:
        return side
    scored = sorted(side, key=lambda a: (min(_d(a, b) for b in other), anchor_key(a)))
    return tuple(scored[:k])


def anchor_pairs(a: tuple, b: tuple) -> list:
    """(anchor of a, anchor of b) sorted by (distance, a's key, b's key)."""
    a2, b2 = _nearest(a, b, NEAREST_ANCHORS), _nearest(b, a, NEAREST_ANCHORS)
    return sorted(itertools.product(a2, b2),
                  key=lambda ab: (_d(*ab), anchor_key(ab[0]), anchor_key(ab[1])))


def maze_order(pairs: list) -> list:
    """Maze candidates: most layer freedom first (a via reaches any layer;
    the nearest pairs are usually layer-locked stubs that already failed)."""
    return sorted(pairs, key=lambda ab: ((ab[0].layer is not None) + (ab[1].layer is not None),
                                         _d(*ab), anchor_key(ab[0]), anchor_key(ab[1])))


def _attach_width(a: Anchor, b: Anchor) -> float:
    return min((w for w in (a.w, b.w) if w), default=PAD_W)


def bridge_width(cls_w: float | None, a: Anchor, b: Anchor) -> float:
    w = _attach_width(a, b)
    return w if cls_w is None else max(w, cls_w)


def link_width(cls_w: float | None, a: Anchor, b: Anchor) -> float:
    return LINK_W if cls_w is None else max(_attach_width(a, b), cls_w)


def width_note(cls_w: float | None, w: float) -> str:
    if cls_w is None:
        return f" at {w:.3f} mm"
    if abs(w - cls_w) < 1e-9:
        return f" at the {w:.3f} mm class width"
    return f" at {w:.3f} mm (class width {cls_w:.3f} mm)"


def _dedupe(path: list) -> list:
    out = [path[0]]
    for p in path[1:]:
        if cu.dist(p, out[-1]) >= 1e-6:
            out.append(p)
    return out


def connector_variants(raw: list) -> list:
    """A maze polyline and its re-routes: the first / last segment (off the
    grid the maze validated) replaced by a jog onto the grid line, then the
    other L. The raw path comes first."""
    if len(raw) < 3:
        return [list(raw)]
    a, p1, pn, b = raw[0], raw[1], raw[-2], raw[-1]
    heads = ([a], [a, (a[0], p1[1])], [a, (p1[0], a[1])])
    tails = ([b], [(b[0], pn[1]), b], [(pn[0], b[1]), b])
    return [_dedupe(h + list(raw[1:-1]) + t) for t in tails for h in heads]


# ---------------------------------------------------------------------------
# Board adapter (pcbnew)
# ---------------------------------------------------------------------------
def _xy(v) -> XY:
    return cu.to(v.x), cu.to(v.y)


def _box(bb) -> Box:
    return cu.to(bb.GetLeft()), cu.to(bb.GetTop()), cu.to(bb.GetRight()), cu.to(bb.GetBottom())


def _uuid(item) -> str:
    try:
        return item.m_Uuid.AsString()
    except AttributeError:
        return ""


def _fill_elements(zone, uuid: str) -> list[Element]:
    out = []
    for layer in zone.GetLayerSet().CuStack():
        try:
            has = zone.HasFilledPolysForLayer(layer)
        except AttributeError:
            has = zone.IsFilled()
        if not has:
            continue
        polys = zone.GetFilledPolysList(layer)
        for i in range(polys.OutlineCount()):
            ol = polys.COutline(i)
            n = ol.PointCount()
            samples = [_xy(ol.CPoint(k)) for k in range(0, n, max(1, n // FILL_SAMPLES))]
            out.append(fill_element(uuid, layer, _box(ol.BBox()),
                                    _contains(polys, i), samples))
    return out


def _contains(polys, index: int) -> Callable:
    def contains(p: XY, acc: float) -> bool:
        return polys.Contains(pcbnew.VECTOR2I(cu.mm(p[0]), cu.mm(p[1])), index, cu.mm(acc))
    return contains


def net_elements(board: pcbnew.BOARD, nc: int) -> list[Element]:
    """Every copper item of net nc, sorted by geometry (never board order)."""
    copper = list(board.GetEnabledLayers().CuStack())
    out = []
    for t in board.GetTracks():
        if t.GetNetCode() != nc:
            continue
        if t.GetClass() == "PCB_VIA":
            out.append(via_element(_uuid(t), _xy(t.GetPosition()), cu.to(cu.via_w(t))))
        else:
            out.append(track_element(_uuid(t), _xy(t.GetStart()), _xy(t.GetEnd()),
                                     cu.to(t.GetWidth()), t.GetLayer()))
    for fp in board.GetFootprints():
        for pad in fp.Pads():
            if pad.GetNetCode() == nc:
                out.append(pad_element(
                    _uuid(pad), _xy(pad.GetPosition()), _box(pad.GetBoundingBox()),
                    frozenset(lay for lay in copper if pad.IsOnLayer(lay)),
                    pad.GetAttribute() == pcbnew.PAD_ATTRIB_PTH))
    for z in board.Zones():
        if not z.GetIsRuleArea() and z.GetNetCode() == nc:
            out.extend(_fill_elements(z, _uuid(z)))
    return sorted(out, key=lambda e: (e.kind, e.box, e.pts, e.w, sorted(e.layers or ())))


class NetCtx(NamedTuple):
    name: str
    net: object                # pcbnew.NETINFO_ITEM
    nc: int
    cls_w: float | None        # class track width, None = unclassed
    plane: bool                # the net owns a (non rule-area) zone
    zlayers: tuple             # the net's zone copper layers, sorted
    clearance: float           # board min clearance + KEEP_MARGIN
    elements: tuple
    islands: tuple
    island_of: dict            # element index -> island index


def net_ctx(board: pcbnew.BOARD, name: str, class_widths: dict) -> NetCtx | None:
    net = board.FindNet(name)
    if net is None:
        return None
    nc = net.GetNetCode()
    elements = tuple(net_elements(board, nc))
    isl = tuple(islands(list(elements)))
    zones = [z for z in board.Zones() if not z.GetIsRuleArea() and z.GetNetCode() == nc]
    zlayers = tuple(sorted({lay for z in zones for lay in z.GetLayerSet().CuStack()}))
    return NetCtx(name, net, nc, netclass_read.class_width(class_widths, name), bool(zones),
                  zlayers, cu.min_clearance(board) + KEEP_MARGIN, elements, isl,
                  {i: k for k, island in enumerate(isl) for i in island.members})


def _clear(board: pcbnew.BOARD, ctx: NetCtx, path: list, keep: float, layer: int,
           step: float) -> bool:
    return all(cu.clear_path(board, ctx.nc, p, q, keep, layer, step=step)
               for p, q in zip(path, path[1:]))


def _lay(board: pcbnew.BOARD, ctx: NetCtx, path: list, w: float, layer: int) -> None:
    for p, q in zip(path, path[1:]):
        if cu.dist(p, q) < 1e-3:
            continue
        t = pcbnew.PCB_TRACK(board)
        t.SetStart(pcbnew.VECTOR2I(cu.mm(p[0]), cu.mm(p[1])))
        t.SetEnd(pcbnew.VECTOR2I(cu.mm(q[0]), cu.mm(q[1])))
        t.SetWidth(cu.mm(w))
        t.SetLayer(layer)
        t.SetNet(ctx.net)
        board.Add(t)


def _layers_for(ctx: NetCtx, la: int | None, lb: int | None) -> tuple:
    if la is None and lb is None:
        return ctx.zlayers + tuple(x for x in (pcbnew.F_Cu, pcbnew.B_Cu) if x not in ctx.zlayers)
    if la is None or lb is None:
        return (lb if la is None else la,)
    return (la,) if la == lb else ()


# ---------------------------------------------------------------------------
# Remedies
# ---------------------------------------------------------------------------
def _route(board: pcbnew.BOARD, ctx: NetCtx, a: Anchor, b: Anchor, layer: int, w: float,
           maze: bool) -> tuple | None:
    """(path, used maze) for a w-wide same-net link a -> b on layer, every
    segment validated at the link keep-out; None when nothing is clear."""
    keep = w / 2 + ctx.clearance
    pa, pb = (a.x, a.y), (b.x, b.y)
    for shape in ([pa, pb], [pa, (pa[0], pb[1]), pb], [pa, (pb[0], pa[1]), pb]):
        if (_clear(board, ctx, shape, keep, layer, SCREEN_STEP)
                and _clear(board, ctx, shape, keep, layer, VALIDATE_STEP)):
            return _dedupe(shape), False
    raw = cu.bfs_path(board, ctx.nc, pa, pb, layer, keep) if maze else None
    for path in connector_variants(raw) if raw else ():
        if _clear(board, ctx, path, keep, layer, VALIDATE_STEP):
            return path, True
    return None


def _try_bridge(board: pcbnew.BOARD, ctx: NetCtx, a: Anchor, b: Anchor) -> str | None:
    gap = _d(a, b)
    if gap > MAX_GAP or not (a.layer is None or b.layer is None or a.layer == b.layer):
        return None
    layer = next((x for x in (a.layer, b.layer) if x is not None), pcbnew.F_Cu)
    w = bridge_width(ctx.cls_w, a, b)
    path = [(a.x, a.y), (b.x, b.y)]
    keep = w / 2 + ctx.clearance
    if not (_clear(board, ctx, path, keep, layer, SCREEN_STEP)
            and _clear(board, ctx, path, keep, layer, VALIDATE_STEP)):
        return None
    _lay(board, ctx, path, w, layer)
    return (f"healed {gap:.2f} mm same-net gap on {ctx.name} "
            f"({board.GetLayerName(layer)}, {w:.2f} mm bridge)" + width_note(ctx.cls_w, w))


def _try_links(board: pcbnew.BOARD, ctx: NetCtx, pairs: list) -> str | None:
    for maze, cands in ((False, pairs[:STRAIGHT_PAIRS]), (True, maze_order(pairs)[:MAZE_PAIRS])):
        for a, b in cands:
            for layer in _layers_for(ctx, a.layer, b.layer):
                w = link_width(ctx.cls_w, a, b)
                found = _route(board, ctx, a, b, layer, w, maze)
                if found is None:
                    continue
                _lay(board, ctx, found[0], w, layer)
                return (f"{ctx.name}: linked disconnected clusters ({_d(a, b):.2f} mm apart) "
                        f"on {board.GetLayerName(layer)}"
                        + (" via maze route" if found[1] else "") + width_note(ctx.cls_w, w)
                        + (" — DSN plane abstraction hid the fill void" if ctx.plane else ""))
    return None


def via_template(board: pcbnew.BOARD, nc: int) -> tuple[int, int]:
    """(width, drill) nm of a stitching via: the smallest same-net via, else
    the smallest via on the board (ties by drill), else 0.6 / 0.3 mm."""
    vias = [t for t in board.GetTracks() if t.GetClass() == "PCB_VIA"]
    pool = [v for v in vias if v.GetNetCode() == nc] or vias
    if not pool:
        return cu.mm(0.6), cu.mm(0.3)
    return min((cu.via_w(v), v.GetDrill()) for v in pool)


def _link_from(board: pcbnew.BOARD, ctx: NetCtx, va: Anchor, other: tuple) -> tuple | None:
    targets = sorted(other, key=lambda p: (_d(va, p), anchor_key(p)))[:MAZE_PAIRS]
    for maze in (False, True):
        for pb in targets:
            for layer in _layers_for(ctx, None, pb.layer):
                w = link_width(ctx.cls_w, va, pb)
                found = _route(board, ctx, va, pb, layer, w, maze)
                if found is not None:
                    return found[0], found[1], layer, w
    return None


def _try_stitch(board: pcbnew.BOARD, ctx: NetCtx, a: tuple, b: tuple) -> str | None:
    w_nm, drill = via_template(board, ctx.nc)
    for side, other in ((a, b), (b, a)):
        spots = sorted((p for p in side if p.layer is not None),
                       key=lambda p: (min(_d(p, q) for q in other), anchor_key(p)))
        for pa in spots[:STITCH_SPOTS]:
            if not cu.clear_spot(board, ctx.nc, (pa.x, pa.y), cu.to(w_nm) / 2 + VIA_KEEP):
                continue
            via = cu.add_via(board, ctx.net, (pa.x, pa.y), w_nm, drill)
            found = _link_from(board, ctx, Anchor(pa.x, pa.y, None, cu.to(w_nm)), other)
            if found is None:
                board.Remove(via)          # the via bought nothing — take it back
                continue
            path, maze, layer, w = found
            _lay(board, ctx, path, w, layer)
            return (f"{ctx.name}: stitched a via at ({pa.x:.2f},{pa.y:.2f}) and linked "
                    f"clusters on {board.GetLayerName(layer)}"
                    + (" via maze route" if maze else "") + width_note(ctx.cls_w, w))
    return None


def open_entry(ctx: NetCtx) -> str:
    if ctx.cls_w is None:
        return f"{ctx.name}: no clear path at the {LINK_W:.3f} mm default width"
    return f"{ctx.name}: no clear path at the {ctx.cls_w:.3f} mm class width"


def heal_pair(board: pcbnew.BOARD, ctx: NetCtx, a: Island, b: Island) -> tuple:
    """(applied message, None) or (None, open entry) for one island pair."""
    if not (a.anchors and b.anchors):
        return None, f"{ctx.name}: no pad, track end or via to anchor a heal link"
    pairs = anchor_pairs(a.anchors, b.anchors)
    msg = (_try_bridge(board, ctx, *pairs[0]) or _try_links(board, ctx, pairs)
           or _try_stitch(board, ctx, a.anchors, b.anchors))
    return (msg, None) if msg else (None, open_entry(ctx))


def _island_pairs(board: pcbnew.BOARD, drc: dict, class_widths: dict) -> tuple:
    """({net: NetCtx}, sorted [(net, island a, island b)], [open entries])."""
    ctxs: dict = {}
    todo, inside = set(), set()
    for brk in drc_breaks(drc):
        if brk.net not in ctxs:
            ctxs[brk.net] = net_ctx(board, brk.net, class_widths)
        ctx = ctxs[brk.net]
        found = [locate(list(ctx.elements), u, p) for u, p in brk.items] if ctx else [None]
        if None in found:
            continue
        ia, ib = sorted(ctx.island_of[i] for i in found)
        if ia == ib:
            inside.add(f"{brk.net}: DRC break inside one copper island (not healed)")
        else:
            todo.add((brk.net, ia, ib))
    return ctxs, sorted(todo), sorted(inside)


def heal_micro_gaps(board: pcbnew.BOARD, drc: dict, class_widths: dict) -> tuple:
    """Heal the DRC report's same-net breaks: (applied messages, open entries).
    class_widths: netclass_read.net_class_widths() of the board's project."""
    ctxs, todo, open_ = _island_pairs(board, drc, class_widths)
    applied: list[str] = []
    parent: dict = {}

    def find(k: tuple) -> tuple:
        while parent.get(k, k) != k:
            k = parent[k]
        return k

    for net, ia, ib in todo:
        if find((net, ia)) == find((net, ib)):
            continue                       # an earlier link already joined them
        ctx = ctxs[net]
        msg, why = heal_pair(board, ctx, ctx.islands[ia], ctx.islands[ib])
        if msg is None:
            open_.append(why)
            continue
        applied.append(msg)
        parent[find((net, ib))] = find((net, ia))
    return applied, open_
