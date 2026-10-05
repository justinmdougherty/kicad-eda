"""route_planner — pure geometry for agent-directed routing. No kipy here.

Input is the plain-dict board model ``backends.pcb_items`` produces (mm floats):

    pad   {"id", "ref", "pad", "net", "x", "y", "layers": ["F.Cu", ...], "through_hole",
           "shape", "w", "h", "rotation", "drill"}
    track {"id", "net", "layer", "start": [x, y], "end": [x, y], "width", "length"}
          (+ "arc": true, "mid": [x, y] for an arc track)
    via   {"id", "net", "x", "y", "diameter", "drill"}

Every function is pure and deterministic: it never mutates an argument and
answers with new lists/dicts. A contract violation is a ValueError whose
message names the offending path element and what to change; the MCP tool
layer turns that into ``{"ok": false, "reason": ...}``.

``width_compliance`` and ``via_ampacity`` are ports of the SWIG gate checks in
``server/tools/swig_gate_checks.py`` (``check_routed_widths`` open-field rule
and ``check_via_ampacity``) onto this dict model, so the routed gate gives the
same verdicts whether the board was routed by Freerouting or by the agent.
"""
from __future__ import annotations

import math
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

_TOOLS_DIR = Path(__file__).resolve().parent / "tools"
if str(_TOOLS_DIR) not in sys.path:       # server/tools holds the pure IPC-2221 engine
    sys.path.insert(0, str(_TOOLS_DIR))

import ipc_rules  # noqa: E402
import route_outline as ro  # noqa: E402  (board edge / cutouts; pure)

Point = Sequence[float]
Item = Mapping[str, Any]

EPS_MM = 0.001                 # "the same point" (via placement, distinct points)
WIDTH_TOL_MM = 1e-3            # width / size comparison slack (matches the SWIG gate)
BEND_STEP_DEG = 45.0
BEND_TOL_DEG = 0.5
ACUTE_TURN_DEG = 90.0 + BEND_TOL_DEG
NECK_REACH_MIN_MM = 3.0        # entry-neck exemption: reach >= 3 mm ...
NECK_REACH_EXTRA_MM = 1.0      # ... and >= the entry's long dimension + 1 mm
PREROUTE_FAT_MIN_MM = 1.0      # a prerouted fat path must keep >= min(this, ...
PREROUTE_FAT_SURVIVAL = 0.5    # ... half its authored length) of copper
DEFAULT_AUTHORED_MM = 2.0      # authored length assumed when the netlist gives none
AMPACITY_MIN_CURRENT_A = 2.0   # via ampacity is judged on nets carrying >= 2 A
DEFAULT_PREROUTE_VIA = (1.2, 0.9)   # [diameter, drill] when a preroute names none
DEFAULT_LAYERS = ("F.Cu", "B.Cu")
RECT_LIKE = frozenset({"rect", "roundrect", "chamfered", "trapezoid"})  # inside their w x h box
ROUND = 4


# ----------------------------------------------------------------------------
# geometry primitives
# ----------------------------------------------------------------------------
def _r(v: float) -> float:
    return round(float(v), ROUND)


def _xy(item: Item) -> tuple[float, float]:
    return float(item["x"]), float(item["y"])


def _point_segment(p: Point, a: Point, b: Point) -> tuple[float, float]:
    """Distance from p to segment a-b and the parameter t (0..1) of the
    closest point on a-b."""
    ax, ay, bx, by = float(a[0]), float(a[1]), float(b[0]), float(b[1])
    px, py = float(p[0]), float(p[1])
    dx, dy = bx - ax, by - ay
    len2 = dx * dx + dy * dy
    if len2 <= 0.0:
        return math.hypot(px - ax, py - ay), 0.0
    t = min(1.0, max(0.0, ((px - ax) * dx + (py - ay) * dy) / len2))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy)), t


def _intersection_t(a: Point, b: Point, c: Point, d: Point) -> float | None:
    """t on a-b where it crosses c-d, or None (parallel or no crossing)."""
    rx, ry = b[0] - a[0], b[1] - a[1]
    sx, sy = d[0] - c[0], d[1] - c[1]
    denom = rx * sy - ry * sx
    if abs(denom) < 1e-12:
        return None
    qx, qy = c[0] - a[0], c[1] - a[1]
    t = (qx * sy - qy * sx) / denom
    u = (qx * ry - qy * rx) / denom
    return t if 0.0 <= t <= 1.0 and 0.0 <= u <= 1.0 else None


def _segment_segment(a: Point, b: Point, c: Point, d: Point) -> tuple[float, float]:
    """Distance between segments a-b and c-d and the t on a-b of the closest
    approach (0 when they cross)."""
    t = _intersection_t(a, b, c, d)
    if t is not None:
        return 0.0, t
    return min((_point_segment(c, a, b), _point_segment(d, a, b),
                (_point_segment(a, c, d)[0], 0.0), (_point_segment(b, c, d)[0], 1.0)))


def _track_chords(tr: Item) -> list[tuple[Point, Point]]:
    """An arc track is judged by its two chords through 'mid' (the chord
    start-end alone would miss the bulge)."""
    start, end = tr["start"], tr["end"]
    mid = tr.get("mid") if tr.get("arc") else None
    if mid is None:
        return [(start, end)]
    return [(start, mid), (mid, end)]


def _track_length(tr: Item) -> float:
    if tr.get("length") is not None:
        return float(tr["length"])
    return math.dist(tr["start"], tr["end"])


# ----------------------------------------------------------------------------
# connections — what is still unrouted, net by net
# ----------------------------------------------------------------------------
def _pads_by_net(pads: Iterable[Item]) -> dict[str, list[Item]]:
    out: dict[str, list[Item]] = {}
    for p in pads:
        out.setdefault(str(p.get("net") or ""), []).append(p)
    return out


def _net_clusters(net_pads: Sequence[Item],
                  listed: Sequence[Sequence[str]] | None) -> list[list[Item]]:
    """Clusters as lists of pad dicts. A pad in no listed cluster is its own
    cluster; a net with no listing is fully unrouted (every pad alone)."""
    by_id = {str(p["id"]): p for p in net_pads}
    clusters: list[list[Item]] = []
    seen: set[str] = set()
    for group in listed or []:
        members = [by_id[str(i)] for i in group if str(i) in by_id and str(i) not in seen]
        seen.update(str(p["id"]) for p in members)
        if members:
            clusters.append(members)
    singles = [[p] for p in net_pads if str(p["id"]) not in seen]
    return clusters + singles


def _closest_pair(tree: Sequence[Item], cluster: Sequence[Item]) -> tuple[float, Item, Item]:
    best: tuple[float, str, str, Item, Item] | None = None
    for p in tree:
        for q in cluster:
            cand = (math.dist(_xy(p), _xy(q)), str(p["id"]), str(q["id"]), p, q)
            if best is None or cand[:3] < best[:3]:
                best = cand
    assert best is not None
    return best[0], best[3], best[4]


def _prim_links(net: str, clusters: Sequence[Sequence[Item]]) -> list[dict[str, Any]]:
    """Prim over clusters: N clusters -> N-1 links, each the closest pad pair
    between the tree so far and the nearest cluster outside it."""
    tree = list(clusters[0])
    remaining = list(range(1, len(clusters)))
    links: list[dict[str, Any]] = []
    while remaining:
        best_len, best_pair, best_idx = math.inf, None, remaining[0]
        for ci in remaining:
            d, p, q = _closest_pair(tree, clusters[ci])
            if d < best_len:
                best_len, best_pair, best_idx = d, (p, q), ci
        assert best_pair is not None
        links.append({"net": net, "from": dict(best_pair[0]), "to": dict(best_pair[1]),
                      "length": _r(best_len)})
        tree.extend(clusters[best_idx])
        remaining.remove(best_idx)
    return links


def connections(pads: Sequence[Item],
                clusters_by_net: Mapping[str, Sequence[Sequence[str]]]) -> list[dict[str, Any]]:
    """The connections still to route: for every net with >= 2 clusters, N-1
    links {"net", "from": pad, "to": pad, "length"} joining the clusters by
    their closest pad pairs. A net missing from clusters_by_net is treated as
    fully unrouted. Ordered by net name, then length, then pad ids."""
    out: list[dict[str, Any]] = []
    for net, net_pads in _pads_by_net(pads).items():
        if not net or len(net_pads) < 2:
            continue
        clusters = _net_clusters(net_pads, clusters_by_net.get(net))
        if len(clusters) >= 2:
            out.extend(_prim_links(net, clusters))
    return sorted(out, key=lambda c: (c["net"], c["length"], c["from"]["id"], c["to"]["id"]))


# ----------------------------------------------------------------------------
# obstacles — foreign copper near a straight segment
# ----------------------------------------------------------------------------
def _axes(angle_deg: float) -> tuple[tuple[float, float], tuple[float, float]]:
    """Unit vectors of a pad's w and h axes at ``angle_deg``."""
    c, s = math.cos(math.radians(angle_deg)), math.sin(math.radians(angle_deg))
    return (c, s), (-s, c)


def _rect_corners(cx: float, cy: float, w: float, h: float,
                  angle_deg: float) -> list[tuple[float, float]]:
    (ux, uy), (vx, vy) = _axes(angle_deg)
    hw, hh = w / 2.0, h / 2.0
    return [(cx + sx * hw * ux + sy * hh * vx, cy + sx * hw * uy + sy * hh * vy)
            for sx, sy in ((-1, -1), (1, -1), (1, 1), (-1, 1))]


def _inside_convex(p: Point, poly: Sequence[tuple[float, float]]) -> bool:
    sign = 0
    for i, (x0, y0) in enumerate(poly):
        x1, y1 = poly[(i + 1) % len(poly)]
        cross = (x1 - x0) * (p[1] - y0) - (y1 - y0) * (p[0] - x0)
        if cross == 0:
            continue
        if sign == 0:
            sign = 1 if cross > 0 else -1
        elif (cross > 0) != (sign > 0):
            return False
    return True


def _segment_polygon(a: Point, b: Point,
                     poly: Sequence[tuple[float, float]]) -> tuple[float, float]:
    """(distance, t on a-b) between segment a-b and a convex polygon; 0 when
    they touch or the segment lies inside."""
    if _inside_convex(a, poly):
        return 0.0, 0.0
    if _inside_convex(b, poly):
        return 0.0, 1.0
    return min(_segment_segment(a, b, poly[i], poly[(i + 1) % len(poly)])
               for i in range(len(poly)))


def _rect_gap(a: Point, b: Point, p: Item, angle_deg: float) -> tuple[float, float]:
    """(gap, t) of a rectangular pad: exact edge distance; when the line
    runs through the copper, how deep across the line (negative)."""
    cx, cy = _xy(p)
    w, h = float(p.get("w") or 0.0), float(p.get("h") or 0.0)
    dist, t = _segment_polygon(a, b, _rect_corners(cx, cy, w, h, angle_deg))
    if dist > 0:
        return dist, t
    length = math.dist(a, b)
    nx, ny = (a[1] - b[1]) / length, (b[0] - a[0]) / length        # unit normal of a-b
    (ux, uy), (vx, vy) = _axes(angle_deg)
    extent = abs(nx * ux + ny * uy) * w / 2.0 + abs(nx * vx + ny * vy) * h / 2.0
    perp = abs(nx * (cx - a[0]) + ny * (cy - a[1]))
    return min(0.0, perp - extent), _point_segment((cx, cy), a, b)[1]


def _oval_gap(a: Point, b: Point, p: Item, angle_deg: float) -> tuple[float, float]:
    """(gap, t) of an oval (stadium) pad: distance to its axis segment minus
    its radius - exact."""
    cx, cy = _xy(p)
    w, h = float(p.get("w") or 0.0), float(p.get("h") or 0.0)
    (ux, uy), (vx, vy) = _axes(angle_deg)
    ax, ay = (ux, uy) if w >= h else (vx, vy)
    half, radius = (max(w, h) - min(w, h)) / 2.0, min(w, h) / 2.0
    dist, t = _segment_segment(a, b, (cx - ax * half, cy - ay * half),
                               (cx + ax * half, cy + ay * half))
    return dist - radius, t


def _pad_gap(a: Point, b: Point, p: Item) -> tuple[float, float]:
    """(distance from the centreline a-b to the pad's copper edge, t): exact
    for a circle, an oval and a rectangle at any rotation (both rotation
    senses are tried and the smaller gap kept, so a rotated pad is never
    under-estimated); roundrect / chamfered / trapezoid pads are bounded by
    their rectangle; a custom or unknown shape by half its diagonal.
    Negative when the line runs through the copper."""
    shape, angle = str(p.get("shape") or ""), float(p.get("rotation") or 0.0)
    w, h = float(p.get("w") or 0.0), float(p.get("h") or 0.0)
    d, t = _point_segment(_xy(p), a, b)
    if shape == "circle":
        return d - max(w, h) / 2.0, t
    if math.dist(a, b) <= EPS_MM or (shape != "oval" and shape not in RECT_LIKE):
        return d - math.hypot(w, h) / 2.0, t
    exact = _oval_gap if shape == "oval" else _rect_gap
    return min(exact(a, b, p, angle), exact(a, b, p, -angle))


def _pad_obstacles(a: Point, b: Point, layer: str, pads: Iterable[Item], net: str,
                   limit: float) -> list[dict[str, Any]]:
    out = []
    for p in pads:
        if str(p.get("net") or "") == net or layer not in (p.get("layers") or []):
            continue
        gap, t = _pad_gap(a, b, p)
        if gap < limit:
            out.append({"kind": "pad", "id": str(p["id"]), "ref": p.get("ref"),
                        "pad": p.get("pad"), "net": str(p.get("net") or ""),
                        "distance": _r(gap), "t": _r(t)})
    return out


def _track_obstacles(a: Point, b: Point, layer: str, tracks: Iterable[Item], net: str,
                     limit: float) -> list[dict[str, Any]]:
    out = []
    for tr in tracks:
        if str(tr.get("net") or "") == net or tr.get("layer") != layer:
            continue
        d, t = min(_segment_segment(a, b, s, e) for s, e in _track_chords(tr))
        gap = d - float(tr.get("width") or 0) / 2.0
        if gap < limit:
            out.append({"kind": "track", "id": str(tr["id"]), "net": str(tr.get("net") or ""),
                        "distance": _r(gap), "t": _r(t)})
    return out


def _via_obstacles(a: Point, b: Point, vias: Iterable[Item], net: str,
                   limit: float) -> list[dict[str, Any]]:
    out = []
    for v in vias:                       # a via is copper on every layer
        if str(v.get("net") or "") == net:
            continue
        d, t = _point_segment(_xy(v), a, b)
        gap = d - float(v.get("diameter") or 0) / 2.0
        if gap < limit:
            out.append({"kind": "via", "id": str(v["id"]), "net": str(v.get("net") or ""),
                        "distance": _r(gap), "t": _r(t)})
    return out


def obstacles(a: Point, b: Point, layer: str, pads: Sequence[Item], tracks: Sequence[Item],
              vias: Sequence[Item], net: str, clearance: float,
              width: float) -> list[dict[str, Any]]:
    """Items of OTHER nets whose copper on `layer` comes within clearance +
    width/2 of the centreline a-b. "distance" is from the centreline to the
    item's copper edge — negative means the item sits on the line; "t" is
    where along a-b (0..1) the closest approach is. A pad counts on each of
    its layers by its true outline (circle, oval, rectangle at its rotation;
    roundrect / chamfered / trapezoid by their rectangle; a custom shape by
    half its diagonal — see _pad_gap), a via on every layer, a track by
    segment-to-segment distance minus half its width. Sorted by t."""
    limit = float(clearance) + float(width) / 2.0
    found = (_pad_obstacles(a, b, layer, pads, net, limit)
             + _track_obstacles(a, b, layer, tracks, net, limit)
             + _via_obstacles(a, b, vias, net, limit))
    return sorted(found, key=lambda o: (o["t"], o["kind"], o["id"]))


def corridor(connection: Item, layers: Sequence[str] = DEFAULT_LAYERS, *,
             pads: Sequence[Item] = (), tracks: Sequence[Item] = (), vias: Sequence[Item] = (),
             clearance: float = 0.0, width: float = 0.0, outline: Any = None,
             origin: Point = (0, 0), edge_clearance: float = 0.0
             ) -> dict[str, list[dict[str, Any]]]:
    """{layer: obstacles(...)} along the straight line of one connection, per
    requested layer (default F.Cu and B.Cu) — the tool's per-connection view.
    With an outline (route_outline.outline_from_geometry: outline, origin) the
    board edge and each cutout, grown by edge_clearance, join as "edge" items."""
    a, b = _xy(connection["from"]), _xy(connection["to"])
    net = str(connection["net"])
    edges = ({} if outline is None else
             ro.edge_obstacles(outline, origin, edge_clearance, layers, a, b, width))
    return {layer: sorted(obstacles(a, b, layer, pads, tracks, vias, net, clearance, width)
                          + edges.get(layer, []), key=lambda o: (o["t"], o["kind"], o["id"]))
            for layer in layers}


# ----------------------------------------------------------------------------
# validate_path — the path contract, one rule per helper
# ----------------------------------------------------------------------------
def _num(value: Any, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{where} must be a finite number, got {value!r}")
    return float(value)


def _point(value: Any, where: str) -> tuple[float, float]:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError(f"{where} must be [x, y] in mm, got {value!r}")
    return _num(value[0], where + "[0]"), _num(value[1], where + "[1]")


def _distinct_points(points: Sequence[tuple[float, float]]) -> list[tuple[float, float]]:
    """Drop zero-length steps (consecutive points closer than EPS_MM)."""
    kept: list[tuple[float, float]] = []
    for pt in points:
        if not kept or math.dist(kept[-1], pt) > EPS_MM:
            kept.append(pt)
    return kept


def _parse_polyline(i: int, el: Mapping[str, Any]) -> dict[str, Any]:
    where = f"path[{i}]"
    layer = el.get("layer")
    if not isinstance(layer, str) or not layer:
        raise ValueError(f"{where}: a polyline needs a copper layer name, e.g. 'F.Cu'")
    raw = el.get("points")
    if not isinstance(raw, (list, tuple)):
        raise ValueError(f"{where} on {layer}: 'points' must be a list of [x, y]")
    pts = _distinct_points([_point(p, f"{where}.points[{k}]") for k, p in enumerate(raw)])
    if len(pts) < 2:
        raise ValueError(f"{where} on {layer}: a polyline needs at least 2 distinct points "
                         f"(more than {EPS_MM} mm apart), got {len(pts)}")
    width = el.get("width")
    if width is not None:
        width = _num(width, f"{where}.width")
    return {"index": i, "kind": "polyline", "layer": layer, "points": pts, "width": width}


def _parse_via(i: int, el: Mapping[str, Any]) -> dict[str, Any]:
    where = f"path[{i}]"
    x, y = _point(el.get("via"), where + ".via")
    dia, drill = el.get("diameter"), el.get("drill")
    return {"index": i, "kind": "via", "x": x, "y": y,
            "diameter": None if dia is None else _num(dia, where + ".diameter"),
            "drill": None if drill is None else _num(drill, where + ".drill")}


def _parse_path(path: Any) -> list[dict[str, Any]]:
    if not isinstance(path, (list, tuple)):
        raise ValueError("path must be a list of {'layer', 'points'} polylines and "
                         "{'via': [x, y]} elements")
    elements: list[dict[str, Any]] = []
    for i, el in enumerate(path):
        if not isinstance(el, Mapping):
            raise ValueError(f"path[{i}] must be a dict, got {type(el).__name__}")
        if "via" in el:
            elements.append(_parse_via(i, el))
        elif "points" in el or "layer" in el:
            elements.append(_parse_polyline(i, el))
        else:
            raise ValueError(f"path[{i}] is neither a polyline {{'layer', 'points'}} nor a via "
                             f"{{'via': [x, y]}}")
    if not any(e["kind"] == "polyline" for e in elements):
        raise ValueError("path needs at least one polyline "
                         "{'layer': 'F.Cu', 'points': [[x, y], ...]}")
    return elements


def _fmt(pt: Sequence[float]) -> str:
    return f"[{_r(pt[0])}, {_r(pt[1])}]"


def _check_join(prev: Mapping[str, Any], cur: Mapping[str, Any]) -> str | None:
    """Placement rule for one adjacent pair; returns a warning or None."""
    if prev["kind"] == "polyline" and cur["kind"] == "polyline":
        raise ValueError(f"path[{cur['index']}] ({cur['layer']}) follows path[{prev['index']}] "
                         f"({prev['layer']}) without a via: insert {{'via': "
                         f"{_fmt(prev['points'][-1])}}} between them, or route them as two calls")
    if prev["kind"] == "via" and cur["kind"] == "via":
        raise ValueError(f"path[{cur['index']}]: two vias in a row; a via must join two "
                         f"polylines or end one (a stub to a plane)")
    if prev["kind"] == "polyline":              # polyline -> via
        end = prev["points"][-1]
        if math.dist(end, (cur["x"], cur["y"])) > EPS_MM:
            raise ValueError(f"path[{cur['index']}] via at {_fmt((cur['x'], cur['y']))} is not "
                             f"at the end of path[{prev['index']}] ({prev['layer']}) which ends "
                             f"at {_fmt(end)}: move the via or extend the polyline to it")
        return None
    start = cur["points"][0]                    # via -> polyline
    if math.dist(start, (prev["x"], prev["y"])) > EPS_MM:
        raise ValueError(f"path[{prev['index']}] via at {_fmt((prev['x'], prev['y']))} is not "
                         f"at the start of path[{cur['index']}] ({cur['layer']}) which starts "
                         f"at {_fmt(start)}: move the via or start the polyline at it")
    return None


def _check_joins(elements: Sequence[Mapping[str, Any]]) -> list[str]:
    warnings: list[str] = []
    for k in range(1, len(elements)):
        _check_join(elements[k - 1], elements[k])
    for k in range(1, len(elements) - 1):       # polyline, via, polyline on one layer
        prev, v, nxt = elements[k - 1], elements[k], elements[k + 1]
        if v["kind"] == "via" and prev["kind"] == nxt["kind"] == "polyline" \
                and prev["layer"] == nxt["layer"]:
            warnings.append(f"path[{v['index']}] via at {_fmt((v['x'], v['y']))} joins two "
                            f"polylines on the same layer {prev['layer']}")
    return warnings


def _resolve_width(explicit: float | None, width: float, net_class: Mapping[str, Any],
                   rules: Mapping[str, Any], allow_narrow: bool, where: str) -> float:
    class_w, min_w = net_class.get("track_width"), rules.get("min_track_width")
    w = explicit if explicit is not None else _num(width, "width")
    if w < 0:
        raise ValueError(f"{where}: width {w} mm is negative")
    if w == 0:
        w = class_w or min_w or 0.0
    if w <= 0:
        raise ValueError(f"{where}: no track width: pass width=... (the net class has no "
                         f"track_width and the board rules no min_track_width)")
    if min_w and w + WIDTH_TOL_MM < min_w:
        raise ValueError(f"{where}: width {w} mm is below the board minimum track width "
                         f"{min_w} mm")
    if class_w and w + WIDTH_TOL_MM < class_w and not allow_narrow:
        raise ValueError(f"{where}: width {w} mm is narrower than the "
                         f"{net_class.get('name') or 'net'} class track width {class_w} mm; "
                         f"pass allow_narrow=true only for a short pad-entry neck")
    return w


def _bend_warnings(poly: Mapping[str, Any]) -> list[str]:
    pts, out = poly["points"], []
    for k in range(1, len(pts) - 1):
        (x0, y0), (x1, y1), (x2, y2) = pts[k - 1], pts[k], pts[k + 1]
        ux, uy, vx, vy = x1 - x0, y1 - y0, x2 - x1, y2 - y1
        turn = abs(math.degrees(math.atan2(ux * vy - uy * vx, ux * vx + uy * vy)))
        rem = turn % BEND_STEP_DEG
        where = f"path[{poly['index']}] on {poly['layer']}"
        if min(rem, BEND_STEP_DEG - rem) > BEND_TOL_DEG:
            out.append(f"{where}: bend of {turn:.1f} deg at {_fmt(pts[k])} is not a multiple "
                       f"of 45 deg")
        elif turn > ACUTE_TURN_DEG:
            out.append(f"{where}: acute angle (turn of {turn:.1f} deg) at {_fmt(pts[k])}; "
                       f"use 45 deg bends")
    return out


def _build_segments(elements: Sequence[Mapping[str, Any]], net_class: Mapping[str, Any],
                    rules: Mapping[str, Any], width: float,
                    allow_narrow: bool) -> tuple[list[dict[str, Any]], list[str]]:
    segments, warnings = [], []
    for poly in (e for e in elements if e["kind"] == "polyline"):
        w = _resolve_width(poly["width"], width, net_class, rules, allow_narrow,
                           f"path[{poly['index']}]")
        pts = poly["points"]
        segments.extend({"layer": poly["layer"], "start": [_r(s[0]), _r(s[1])],
                         "end": [_r(e[0]), _r(e[1])], "width": _r(w)}
                        for s, e in zip(pts, pts[1:], strict=False))
        warnings.extend(_bend_warnings(poly))
    return segments, warnings


def _resolve_via(v: Mapping[str, Any], via_diameter: float, via_drill: float,
                 net_class: Mapping[str, Any], rules: Mapping[str, Any]) -> dict[str, Any]:
    where = f"path[{v['index']}] via"
    d = v["diameter"] if v["diameter"] is not None else _num(via_diameter, "via_diameter")
    h = v["drill"] if v["drill"] is not None else _num(via_drill, "via_drill")
    d = d or net_class.get("via_diameter") or rules.get("min_via_diameter") or 0.0
    h = h or net_class.get("via_drill") or rules.get("min_via_drill") or 0.0
    if d <= 0 or h <= 0:
        raise ValueError(f"{where}: no via size: pass via_diameter and via_drill (the net "
                         f"class and the board rules give none)")
    if h >= d:
        raise ValueError(f"{where}: drill {h} mm >= diameter {d} mm; the drill must leave an "
                         f"annular ring")
    min_d, min_h = rules.get("min_via_diameter"), rules.get("min_via_drill")
    if min_d and d + WIDTH_TOL_MM < min_d:
        raise ValueError(f"{where}: diameter {d} mm is below the board minimum via diameter "
                         f"{min_d} mm")
    if min_h and h + WIDTH_TOL_MM < min_h:
        raise ValueError(f"{where}: drill {h} mm is below the board minimum via drill {min_h} mm")
    ring, min_ring = (d - h) / 2.0, rules.get("min_via_annular")
    if min_ring and ring + WIDTH_TOL_MM < min_ring:
        raise ValueError(f"{where}: annular ring {ring:.3f} mm is below the board minimum "
                         f"{min_ring} mm; use a diameter >= {_r(h + 2 * min_ring)} mm")
    return {"x": _r(v["x"]), "y": _r(v["y"]), "diameter": _r(d), "drill": _r(h)}


def _outline_errors(elements: Sequence[Mapping[str, Any]], plan: Mapping[str, Any], outline: Any,
                    origin: Point, clear: float) -> list[str]:
    """route_outline.path_outline_errors per element: polylines at their width, vias as pads."""
    vias, first, out = iter(plan["vias"]), 0, []    # first: this polyline's 1st plan segment
    for e in elements:
        if e["kind"] == "via":
            v = next(vias)
            pts, w, where = [(v["x"], v["y"])], v["diameter"], "via"
        else:                       # one segment per point pair, all at the polyline's width
            pts, w, where = e["points"], plan["segments"][first]["width"], f"on {e['layer']}"
            first += len(pts) - 1
        errors = ro.path_outline_errors(outline, origin, pts, w, clear)
        out += [f"path[{e['index']}] {where}: {m}" for m in errors]
    return out


def validate_path(path: Sequence[Mapping[str, Any]], net_class: Mapping[str, Any],
                  rules: Mapping[str, Any], width: float = 0.0, allow_narrow: bool = False,
                  via_diameter: float = 0.0, via_drill: float = 0.0, outline: Any = None,
                  origin: Point = (0, 0), edge_clearance: float | None = None) -> dict[str, Any]:
    """Check an agent's path against the path contract and expand it into the
    segments and vias BoardApi.add_tracks / add_vias take.

    path elements: {"layer": "F.Cu", "points": [[x, y], ...], "width"?: mm} and
    {"via": [x, y], "diameter"?, "drill"?}. Two polylines must be joined by a via
    at the join point (a trailing/leading via is a stub to a plane). Width 0 ->
    the class track_width, else rules.min_track_width; sizes below the board
    rules are errors, below the class an error unless allow_narrow. Returns
    {"segments": [{"layer", "start", "end", "width"}], "vias": [{"x", "y",
    "diameter", "drill"}], "warnings": [...]} — non-45 bends, acute turns and
    same-layer vias warn. With an outline (route_outline.outline_from_geometry), copper
    off the board or nearer an edge than edge_clearance (None: the rules'
    copper_edge_clearance) is an error. Raises ValueError naming the element to fix."""
    elements = _parse_path(path)
    join_warnings = _check_joins(elements)
    segments, bend_warnings = _build_segments(elements, net_class, rules, width, allow_narrow)
    vias = [_resolve_via(e, via_diameter, via_drill, net_class, rules)
            for e in elements if e["kind"] == "via"]
    plan = {"segments": segments, "vias": vias, "warnings": join_warnings + bend_warnings}
    if outline is not None:
        clear = rules.get("copper_edge_clearance") if edge_clearance is None else edge_clearance
        errors = _outline_errors(elements, plan, outline, origin, float(clear or 0.0))
        if errors:
            raise ValueError("copper must stay on the board: " + "; ".join(errors))
    return plan


def route_length(segments: Iterable[Item]) -> float:
    """Total length (mm) of segments/tracks with "start" and "end"."""
    return _r(sum(math.dist(s["start"], s["end"]) for s in segments))


# ----------------------------------------------------------------------------
# preroutes — what the netlist authored (input to width_compliance)
# ----------------------------------------------------------------------------
def _fat_length(pre: Item, fat_width: float) -> float:
    """Length of the preroute's xy -> xy segments authored at >= ``fat_width``
    (a point's 4th element is the width of the segment it starts, else the
    preroute's width) - the SWIG gate's "authored fat length"."""
    points = [p for p in (pre.get("path") or []) if isinstance(p, (list, tuple)) and p]
    total = 0.0
    for a, b in zip(points, points[1:], strict=False):
        width = float(a[3]) if len(a) > 3 and a[3] is not None else float(pre["width"])
        if a[0] == "xy" and b[0] == "xy" and width + WIDTH_TOL_MM >= fat_width:
            total += math.hypot(float(b[1]) - float(a[1]), float(b[2]) - float(a[2]))
    return total


def preroutes(design: Item) -> tuple[dict[str, float], dict[str, float]]:
    """({net: widest authored preroute width}, {net: authored fat length mm})
    from a netlist.json's ``preroutes``. A net whose preroutes hold no xy fat
    segment gets no length entry (width_compliance then applies its
    default), matching swig_gate_checks.check_routed_widths."""
    pres = [p for p in design.get("preroutes") or []
            if isinstance(p, dict) and p.get("net") and p.get("width") is not None]
    widths: dict[str, float] = {}
    for pre in pres:
        widths[str(pre["net"])] = max(widths.get(str(pre["net"]), 0.0), float(pre["width"]))
    authored: dict[str, float] = {}
    for pre in pres:
        net = str(pre["net"])
        length = _fat_length(pre, widths[net])
        if length > 0:
            authored[net] = authored.get(net, 0.0) + length
    return widths, authored


# ----------------------------------------------------------------------------
# width_compliance — port of swig_gate_checks.check_routed_widths (open field)
# ----------------------------------------------------------------------------
def _near_narrow_entry(tr: Item, target: float, entries: Iterable[Sequence[float]]) -> bool:
    """A sub-width segment is an accepted PAD-ENTRY NECK when an endpoint lies
    within reach of a same-net entry (pad/via bbox) that is itself narrower
    than the class trace; reach = max(3 mm, the entry's long dimension + 1 mm)."""
    for x0, y0, x1, y1 in entries:
        ew, eh = abs(x1 - x0), abs(y1 - y0)
        if min(ew, eh) >= target:              # wide entry — no neck needed
            continue
        cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
        reach = max(NECK_REACH_MIN_MM, max(ew, eh) + NECK_REACH_EXTRA_MM)
        for pt in (tr["start"], tr["end"]):
            if math.dist(pt, (cx, cy)) <= reach:
                return True
    return False


def _preroute_issues(tracks_by_net: Mapping[str, Sequence[Item]],
                     preroute_widths: Mapping[str, float],
                     preroute_authored: Mapping[str, float]) -> list[str]:
    """The designer-authored fat path must survive routing: copper at the
    preroute width totalling >= min(1 mm, half the authored fat length) must
    remain on the net (the router may trim a fat run where it enters a wide
    pad; only substantial removal is a violation - SWIG check_routed_widths).
    A net without an authored length is judged at the 1 mm cap."""
    issues = []
    for net in sorted(preroute_widths):
        w_req = float(preroute_widths[net])
        fat = sum((_track_length(t) for t in tracks_by_net.get(net, [])
                   if float(t["width"]) + WIDTH_TOL_MM >= w_req), 0.0)
        authored = preroute_authored.get(net)
        need = min(PREROUTE_FAT_MIN_MM, PREROUTE_FAT_SURVIVAL * float(
            authored if authored is not None else DEFAULT_AUTHORED_MM))
        if fat + WIDTH_TOL_MM < need:
            known = (f"authored {round(float(authored), 2)} mm" if authored is not None
                     else "authored length not in the netlist")
            issues.append(f"{net}: prerouted {w_req} mm power path missing from the routed "
                          f"board ({round(fat, 2)} mm of fat copper survives, {known} — removed?)")
    return issues


def width_compliance(tracks_by_net: Mapping[str, Sequence[Item]],
                     class_widths: Mapping[str, float],
                     entries_by_net: Mapping[str, Sequence[Sequence[float]]],
                     preroute_widths: Mapping[str, float] | None = None,
                     preroute_authored: Mapping[str, float] | None = None) -> list[str]:
    """Every classed net must be routed at >= its class width, except for short
    entry necks next to a same-net pad/via narrower than the class trace.
    class_widths = {net: class track width}; entries_by_net = {net: [[x0, y0,
    x1, y1] bboxes of pads and vias]}; preroute_widths = {net: authored width}
    whose fat path must survive, preroute_authored = {net: authored fat length
    mm} (half of it, capped at 1 mm, must survive; 1 mm when unknown). One
    issue string per net, SWIG wording."""
    issues = _preroute_issues(tracks_by_net, preroute_widths or {}, preroute_authored or {})
    for net in sorted(tracks_by_net):
        target = float(class_widths.get(net) or 0)
        if target <= 0:
            continue
        entries = entries_by_net.get(net, [])
        for tr in tracks_by_net[net]:
            w = float(tr["width"])
            if w + WIDTH_TOL_MM < target and not _near_narrow_entry(tr, target, entries):
                issues.append(f"{net}: open-field segment routed at {w:.3f} mm but the net "
                              f"class requires {target:.3f} mm")
                break
    return issues


# ----------------------------------------------------------------------------
# via_ampacity — port of swig_gate_checks.check_via_ampacity
# ----------------------------------------------------------------------------
def _preroute_via_issue(net: str, current: float, preroutes: Sequence[Item]) -> str | None:
    """A prerouted net's current path is judged by its AUTHORED via cluster
    (router-added vias on it are signal taps); single-layer paths pass."""
    mine = [p for p in preroutes if p.get("net") == net]
    if len({p.get("layer", "F.Cu") for p in mine}) < 2:
        return None
    n_vias, cap = 0, 0.0
    for p in mine:
        drill = float((p.get("via_size") or DEFAULT_PREROUTE_VIA)[1])
        count = len(p.get("vias") or [])
        n_vias += count
        cap += count * ipc_rules.via_ampacity_a(drill)
    if cap + 1e-6 < current:
        return (f"{net}: prerouted layer transition carries {current} A but the authored via "
                f"cluster ({n_vias} via(s)) totals only {round(cap, 2)} A (IPC-2221) — add "
                f"parallel vias")
    return None


def via_ampacity(vias_by_net: Mapping[str, Sequence[Item]],
                 pth_drills_by_net: Mapping[str, Sequence[float]],
                 net_specs: Mapping[str, Mapping[str, Any]],
                 preroutes: Sequence[Item]) -> list[str]:
    """High-current nets (>= 2 A in net_specs) must not bottleneck at a layer
    transition: the summed IPC-2221 barrel ampacity of the net's vias plus its
    PTH pad barrels (pth_drills_by_net = {net: [drill mm, ...]}) must carry the
    specified current. Prerouted nets are judged by their authored via cluster.
    Returns issue strings in SWIG wording; empty when everything passes."""
    pre_nets = {p.get("net") for p in preroutes}
    issues: list[str] = []
    for net in sorted(net_specs):
        current = float(net_specs[net].get("current", 0) or 0)
        if current < AMPACITY_MIN_CURRENT_A:
            continue
        if net in pre_nets:
            issue = _preroute_via_issue(net, current, preroutes)
            if issue:
                issues.append(issue)
            continue
        drills = [float(v.get("drill") or 0) for v in vias_by_net.get(net, [])]
        if not drills:
            continue                          # the net never changes layers
        barrels = [float(d) for d in pth_drills_by_net.get(net, []) if d and float(d) > 0]
        cap = sum(ipc_rules.via_ampacity_a(d) for d in drills + barrels)
        if cap + 1e-6 < current:
            issues.append(f"{net}: {current} A crosses layers through {len(drills)} via(s) + "
                          f"{len(barrels)} PTH barrel(s) totaling {round(cap, 2)} A (IPC-2221) "
                          f"— add parallel vias at the transition")
    return issues
