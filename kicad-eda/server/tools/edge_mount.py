"""Edge-mount connector geometry from the footprint itself (F3-25), in footprint
coordinates (mm, Y down, rotation 0): the board-edge line, the courtyard overhang
beyond it (snap_to_edge's default, F3-12) and the G3 straddle text (F3-13b). Stock
footprints mark the edge with a 'PCB Edge' text on Dwgs.User / Cmts.User. Sources,
first match wins: 'marker_line' (a parallel Dwgs/Cmts line within 1.5 mm of the
text, or at the end of a leader from it, as in KiCad's DVI parts), 'marker_text'
(the text position; its rotation gives the axis), 'name' (EdgeMount / Edge_Mount /
'Edge Mount' in name or tags: the pad-bbox side the courtyard reaches furthest
beyond). ``outward`` points away from the pads, else toward the courtyard's longer
side, else (0, 0) = undetermined. Stdlib only, Python 3.11 (KiCad 9's Python imports
it); the .kicad_mod reader imports sexpr_read (sexpdata) lazily: venv side only.
"""
from __future__ import annotations

import math
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

MARKER_LAYERS = frozenset({"Dwgs.User", "Cmts.User"})
MARKER_REACH_MM = 1.5            # text -> marker line (or leader end) -> edge line
STRADDLE_TOL_MM = 0.25
COURTYARD_LAYERS = frozenset({"F.CrtYd", "B.CrtYd"})
_MARKER_RE = re.compile(r"\s*pcb[\s_-]*edge\s*", re.IGNORECASE)        # full match
_NAME_RE = re.compile(r"edge[\s_]?mount", re.IGNORECASE)
_AXIS_TOL = 1e-3                 # mm: this close to axis-aligned counts as axis-aligned
_EPS = 1e-6
_REQUIRED = {"line": ("x0", "y0", "x1", "y1"), "text": ("x", "y"), "pad": ("x", "y")}
_OPTIONAL = {"line": (), "text": ("rot",), "pad": ("w", "h")}           # default 0

BBox = tuple[float, float, float, float]
Point = tuple[float, float]


@dataclass(frozen=True)
class EdgeMount:
    edge_mount: bool
    axis: str | None                  # 'x': the edge is the line x = edge_coord_mm; 'y': y = ...
    edge_coord_mm: float
    outward: tuple[float, float]      # unit vector board -> overhang; (0, 0) = undetermined
    overhang_mm: float                # courtyard extent beyond the edge line (>= 0)
    source: str | None                # 'marker_line' | 'marker_text' | 'name' | None
    anchor_mm: Point | None = None    # a point on the edge line (the marker's middle)


NOT_EDGE_MOUNT = EdgeMount(False, None, 0.0, (0.0, 0.0), 0.0, None)


@dataclass(frozen=True)
class FootprintPrims:
    name: str
    tags: str
    prims: tuple[dict, ...]
    courtyard_bbox: BBox | None


def _normalise(prims: Iterable[Any]) -> list[dict]:
    """Validated copies of the known primitives (the caller's are never touched)."""
    out: list[dict] = []
    for p in prims:
        if not isinstance(p, dict) or "kind" not in p:
            raise ValueError(f"a primitive must be a dict with a 'kind' key, got {p!r}")
        if (kind := p["kind"]) not in _REQUIRED:
            continue
        q: dict = {"kind": kind, "layer": str(p.get("layer", ""))}
        for key in _REQUIRED[kind] + _OPTIONAL[kind]:
            raw = p.get(key, 0.0 if key in _OPTIONAL[kind] else None)
            q[key] = float(raw) if isinstance(raw, (int, float)) else math.nan
            if not math.isfinite(q[key]):
                raise ValueError(f"{kind} primitive: {key} must be a finite number, got {raw!r}")
        if kind == "text" and not isinstance(p.get("text"), str):
            raise ValueError(f"text primitive needs a 'text' string, got {p!r}")
        out.append({**q, "text": p["text"]} if kind == "text" else q)
    return out


def _bbox(box: Sequence[float] | None) -> BBox | None:
    if box is None:
        return None
    vals = [float(v) if isinstance(v, (int, float)) else math.nan for v in box]
    if len(vals) != 4 or not all(math.isfinite(v) for v in vals):
        raise ValueError(f"courtyard_bbox must be 4 finite numbers (x0, y0, x1, y1), got {box!r}")
    x0, y0, x1, y1 = vals
    return (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))


def _parallel(ln: dict, axis: str) -> bool:
    """Axis-aligned along the edge: a vertical line for axis 'x', else horizontal."""
    dx, dy = abs(ln["x1"] - ln["x0"]), abs(ln["y1"] - ln["y0"])
    return (dx, dy)[axis != "x"] <= _AXIS_TOL < (dy, dx)[axis != "x"]


def _seg_dist(px: float, py: float, ln: dict) -> float:
    dx, dy = ln["x1"] - ln["x0"], ln["y1"] - ln["y0"]
    den = dx * dx + dy * dy
    t = 0.0 if den == 0 else max(0.0, min(1.0, ((px - ln["x0"]) * dx + (py - ln["y0"]) * dy) / den))
    return math.hypot(ln["x0"] + t * dx - px, ln["y0"] + t * dy - py)


def _nearest_parallel(lines: list[dict], px: float, py: float, axis: str) -> dict | None:
    """The closest parallel line within reach; on a tie the longer one."""
    hits = [(_seg_dist(px, py, ln), -math.hypot(ln["x1"] - ln["x0"], ln["y1"] - ln["y0"]), i)
            for i, ln in enumerate(lines) if _parallel(ln, axis)]
    hits = [h for h in hits if h[0] <= MARKER_REACH_MM + _EPS]
    return lines[min(hits)[2]] if hits else None


def _edge_line(text: dict, axis: str, lines: list[dict]) -> dict | None:
    if (found := _nearest_parallel(lines, text["x"], text["y"], axis)) is not None:
        return found
    for ln in (ln for ln in lines if not _parallel(ln, axis)):   # a leader to the edge line
        ends = ((ln["x0"], ln["y0"]), (ln["x1"], ln["y1"]))
        for near, far in (ends, ends[::-1]):
            if math.dist(near, (text["x"], text["y"])) <= MARKER_REACH_MM + _EPS and (
                    found := _nearest_parallel(lines, far[0], far[1], axis)) is not None:
                return found
    return None


def _make(axis: str, coord: float, sign: int, overhang: float, source: str,
          anchor: Point) -> EdgeMount:
    out = (sign, 0) if axis == "x" else (0, sign)
    r = [round(v, 6) + 0.0 for v in (coord, *out, max(0.0, overhang), *anchor)]  # no -0.0
    return EdgeMount(True, axis, r[0], (r[1], r[2]), r[3], source, (r[4], r[5]))


def _from_marker(axis: str, coord: float, anchor: Point, pads: list[dict],
                 crt: BBox | None, source: str) -> EdgeMount:
    i = 0 if axis == "x" else 1
    centre = sum(p[axis] for p in pads) / len(pads) if pads else coord
    hi, lo = (crt[i + 2] - coord, coord - crt[i]) if crt is not None else (0.0, 0.0)
    longer = 0 if abs(hi - lo) <= _EPS else (1 if hi > lo else -1)   # the courtyard's longer side
    sign = (1 if centre < coord else -1) if abs(centre - coord) > _EPS else longer   # pads inboard
    return _make(axis, coord, sign, {1: hi, -1: lo, 0: 0.0}[sign], source, anchor)


def _from_pads(pads: list[dict], crt: BBox | None) -> EdgeMount:
    if not pads or crt is None:
        return NOT_EDGE_MOUNT
    x0, y0 = (min(p[k] - abs(p[s]) / 2 for p in pads) for k, s in (("x", "w"), ("y", "h")))
    x1, y1 = (max(p[k] + abs(p[s]) / 2 for p in pads) for k, s in (("x", "w"), ("y", "h")))
    sides = (("x", 1, x1, crt[2] - x1), ("x", -1, x0, x0 - crt[0]),
             ("y", 1, y1, crt[3] - y1), ("y", -1, y0, y0 - crt[1]))
    axis, sign, coord, reach = max(sides, key=lambda s: s[3])
    if reach <= _EPS:
        return NOT_EDGE_MOUNT
    anchor = (coord, (y0 + y1) / 2) if axis == "x" else ((x0 + x1) / 2, coord)
    return _make(axis, coord, sign, reach, "name", anchor)


def edge_from_primitives(prims: Iterable[Any], courtyard_bbox: Sequence[float] | None, *,
                         name: str = "", tags: str = "") -> EdgeMount:
    """Classify a footprint from its courtyard bbox (x0, y0, x1, y1) and primitives:
    {"kind": "line", "layer", "x0", "y0", "x1", "y1"}, {"kind": "text", "layer", "x", "y",
    "rot" (deg, default 0), "text"}, {"kind": "pad", "x", "y", "w", "h"} (centre,
    axis-aligned size); other kinds are ignored."""
    items, crt = _normalise(prims), _bbox(courtyard_bbox)
    pads = [p for p in items if p["kind"] == "pad"]
    lines = [p for p in items if p["kind"] == "line" and p["layer"] in MARKER_LAYERS]
    marks = [(p, "x" if 45.0 < p["rot"] % 180.0 < 135.0 else "y")   # vertical text: x = c
             for p in items if p["kind"] == "text" and p["layer"] in MARKER_LAYERS
             and _MARKER_RE.fullmatch(p["text"])]
    for text, axis in marks:
        line = _edge_line(text, axis, lines)
        if line is not None:
            mid = ((line["x0"] + line["x1"]) / 2, (line["y0"] + line["y1"]) / 2)
            return _from_marker(axis, mid[0 if axis == "x" else 1], mid, pads, crt, "marker_line")
    if marks:
        text, axis = marks[0]
        return _from_marker(axis, text[axis], (text["x"], text["y"]), pads, crt, "marker_text")
    if _NAME_RE.search(name or "") or _NAME_RE.search(tags or ""):
        return _from_pads(pads, crt)
    return NOT_EDGE_MOUNT


def straddle_issue(ref: str, marker_distance_mm: float, tol: float = STRADDLE_TOL_MM
                   ) -> str | None:
    """The G3 text when the edge marker is more than `tol` from the outline, else None."""
    d = abs(marker_distance_mm)
    if not (math.isfinite(d) and math.isfinite(tol) and tol >= 0):
        raise ValueError(f"straddle_issue needs a finite distance and tol >= 0: {d!r}, {tol!r}")
    return None if d <= tol else (f"{ref}: edge-mount connector does not straddle the board "
                                  f"edge (edge marker {d:.2f} mm from the outline)")


# --- .kicad_mod reader (venv side: sexpr_read.parse needs sexpdata) -------------
def _xy(sr: Any, node: list, name: str) -> Point:
    if (found := sr.child(node, name)) is None or len(found) < 3:
        raise ValueError(f"{sr.tag(node)} without ({name} x y): {node!r}"[:200])
    return (float(found[1]), float(found[2]))


def _sweep(c: Point, r: float, a0: float, sweep: float) -> list[Point]:
    """Both ends of an arc plus every axis extreme it passes."""
    angles = [a0, a0 + sweep] + [
        q for q in (k * math.pi / 2 for k in range(4))
        if ((q - a0) if sweep >= 0 else (a0 - q)) % math.tau <= abs(sweep)]
    return [(c[0] + r * math.cos(a), c[1] + r * math.sin(a)) for a in angles]


def _arc3(s: Point, m: Point, e: Point) -> list[Point]:
    (ax, ay), (bx, by), (cx, cy) = s, m, e
    d = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(d) < 1e-12:                        # collinear: a straight segment
        return [s, m, e]
    a2, b2, c2 = ax * ax + ay * ay, bx * bx + by * by, cx * cx + cy * cy
    ux = (a2 * (by - cy) + b2 * (cy - ay) + c2 * (ay - by)) / d
    uy = (a2 * (cx - bx) + b2 * (ax - cx) + c2 * (bx - ax)) / d
    a0, am, a1 = (math.atan2(p[1] - uy, p[0] - ux) for p in (s, m, e))
    sweep = (a1 - a0) % math.tau
    if (am - a0) % math.tau > sweep:          # the arc runs the other way round
        sweep -= math.tau
    return _sweep((ux, uy), math.hypot(ax - ux, ay - uy), a0, sweep)


def _bezier(p: list[Point]) -> list[Point]:
    """A cubic Bezier's ends plus each axis extreme (paired with p0's other coordinate)."""
    if len(p) != 4:
        raise ValueError(f"fp_curve needs 4 points (a cubic Bezier), got {len(p)}")
    out = [p[0], p[3]]
    for i in (0, 1):            # B(t) = p0 + 3a t + 3(b - a) t^2 + qa t^3, roots of B'(t)
        a, b, c = (p[k + 1][i] - p[k][i] for k in range(3))
        qa, r = a - 2 * b + c, b * b - a * c
        ts = [a / (2 * (a - b))] if abs(qa) <= 1e-12 and a != b else []
        if abs(qa) > 1e-12 and r >= 0:
            ts = [(a - b + s * math.sqrt(r)) / qa for s in (1.0, -1.0)]
        vs = [p[0][i] + t * (3 * a + t * (3 * (b - a) + t * qa)) for t in ts if 0 < t < 1]
        out += [(v, p[0][1]) if i == 0 else (p[0][0], v) for v in vs]
    return out


def _shape_points(sr: Any, node: list) -> list[Point]:
    """Points spanning a courtyard shape's bbox (KiCad uses shapes only, not texts/zones)."""
    head = sr.tag(node)
    if head in ("fp_line", "fp_rect"):
        return [_xy(sr, node, "start"), _xy(sr, node, "end")]
    if head == "fp_circle":
        c, e = _xy(sr, node, "center"), _xy(sr, node, "end")
        return _sweep(c, math.dist(c, e), 0.0, math.tau)
    if head == "fp_arc" and sr.child(node, "mid") is not None:
        return _arc3(_xy(sr, node, "start"), _xy(sr, node, "mid"), _xy(sr, node, "end"))
    if head == "fp_arc":                      # KiCad 5: start = centre, end, angle (deg)
        c, p0 = _xy(sr, node, "start"), _xy(sr, node, "end")
        a0 = math.atan2(p0[1] - c[1], p0[0] - c[0])
        return _sweep(c, math.dist(c, p0), a0, math.radians(float(sr.value(node, "angle"))))
    if head not in ("fp_poly", "fp_curve"):
        if head.startswith("fp_") and not head.startswith("fp_text"):
            raise ValueError(f"unsupported courtyard shape ({head} ...)")
        return []
    points: list[Point] = []
    for item in (sr.child(node, "pts") or [])[1:]:
        if sr.tag(item) == "xy":
            points.append((float(item[1]), float(item[2])))
        elif sr.tag(item) == "arc":
            points += _arc3(_xy(sr, item, "start"), _xy(sr, item, "mid"), _xy(sr, item, "end"))
    return _bezier(points) if head == "fp_curve" else points


def _primitive(sr: Any, node: list, layer: str) -> dict | None:
    head = sr.tag(node)
    if head == "fp_line":
        (x0, y0), (x1, y1) = _xy(sr, node, "start"), _xy(sr, node, "end")
        return {"kind": "line", "layer": layer, "x0": x0, "y0": y0, "x1": x1, "y1": y1}
    if head not in ("fp_text", "pad") or len(node) < 3:
        return None
    (x, y), at = _xy(sr, node, "at"), sr.child(node, "at")      # KiCad 5-7: (at x y unlocked)
    rot = float(at[3]) if len(at) > 3 and isinstance(at[3], (int, float)) else 0.0
    if head == "fp_text":
        return dict(kind="text", layer=layer, x=x, y=y, rot=rot, text=sr.atom(node[2]))
    w, h = _xy(sr, node, "size")
    c, s = abs(math.cos(math.radians(rot))), abs(math.sin(math.radians(rot)))
    return dict(kind="pad", layer=layer, x=x, y=y, w=w * c + h * s, h=w * s + h * c)


def primitives_from_kicad_mod(text: str) -> FootprintPrims:
    """Name, tags, primitives and F/B.CrtYd bbox of .kicad_mod text (KiCad 5 to 10)."""
    import sexpr_read as sr                   # lazy: KiCad's Python has no sexpdata
    if sr.head_of(text) not in ("footprint", "module"):
        raise ValueError("not a KiCad footprint (expected '(footprint ...' or '(module ...')")
    tree = sr.parse(sr.quote_bare_keys(sr.restore_teardrop_parens(text)))
    prims: list[dict] = []
    courtyard: list[Point] = []
    for node in (n for n in tree[2:] if isinstance(n, list)):
        layer = sr.value(node, "layer")
        if layer in COURTYARD_LAYERS:
            courtyard += _shape_points(sr, node)
        elif (prim := _primitive(sr, node, layer)) is not None:
            prims.append(prim)
    name = sr.atom(tree[1]) if len(tree) > 1 and not isinstance(tree[1], list) else ""
    xs, ys = [p[0] for p in courtyard], [p[1] for p in courtyard]
    box = (min(xs), min(ys), max(xs), max(ys)) if courtyard else None
    return FootprintPrims(name, sr.value(tree, "tags"), tuple(prims), box)


def edge_from_kicad_mod(text: str) -> EdgeMount:
    """edge_from_primitives for a .kicad_mod file's text (venv side)."""
    fp = primitives_from_kicad_mod(text)
    return edge_from_primitives(fp.prims, fp.courtyard_bbox, name=fp.name, tags=fp.tags)
