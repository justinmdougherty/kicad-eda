#!/usr/bin/env python3
"""Footprint authoring: datasheet land-pattern dimensions -> KiCad .kicad_mod.

F2 (docs/ROADMAP_DECISIONS.md D1). A FootprintSpec (pads, body, attributes,
optional 3D model; frozen dataclasses defined in fp_author_templates and
re-exported here) renders to a footprint in the D0 dialect,
``(version 20241229)`` / ``(generator "kicad-eda")``, which KiCad 9's pcbnew
(the oldest pcbnew the SWIG path supports) loads. The renderer adds:

  * F.Fab: the nominal body outline with a chamfered pin-1 corner, and a
    ``${REFERENCE}`` text;
  * F.SilkS: the body outline 0.11 mm outside the fab line, clipped so every
    silk stroke keeps >= 0.2 mm from pad copper, and a pin-1 triangle
    (place_pin1_marker): inside the courtyard when it fits there, cutting a
    notch into the outline if it must;
  * F.CrtYd: one rectangle around the max-material body and all pad copper,
    grown by the courtyard excess and rounded outward to a 0.01 mm grid. The
    excess is 0.25 mm: IPC-7351B ("Generic Requirements for Surface Mount
    Design and Land Pattern Standard", 2010), density level B "nominal"
    courtyard excess; the KiCad Library Convention (F5.3) uses the same value.
    A pin-1 marker with no room inside it grows it to cover the marker;
  * Reference (F.SilkS, above the courtyard) and Value (F.Fab, below it).

Also here: static checks (dialect, courtyard excess, silk clearance, copper
overlaps); ``validate(path)`` = those checks plus ``kicad-cli fp upgrade``
(oldest install) into a temp dir, whose re-serialised pads must equal the
source's; ``pcbnew_load(path)`` (FootprintLoad in a KiCad-python child that
imports kicad_quiet first). The stdlib .kicad_mod reader lives in fp_reader,
compare_footprints in fp_compare and the templates in fp_author_templates;
this module re-exports them (``__all__``). Units: mm, footprint frame (+Y
down). Stdlib-only, except that validate / pcbnew_load find KiCad through
kicad_env.
"""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from fp_author_templates import (
    ATTR_FLAGS, ATTR_TYPES, COURTYARD_EXCESS_MM, DEFAULT_LAYERS, EPS, PAD_SHAPES, PAD_TYPES,
    SIDES, Body, FootprintSpec, Model, Pad, has_copper, natural_key, pad_bbox)
from fp_author_templates import r6 as _r6
from fp_compare import compare_footprints
from fp_reader import (
    FootprintParseError, ParsedFootprint, ParsedPad, parse_footprint, parse_sexpr, read_footprint)

__all__ = [
    # the spec vocabulary (fp_author_templates)
    "ATTR_FLAGS", "ATTR_TYPES", "COURTYARD_EXCESS_MM", "DEFAULT_LAYERS", "EPS", "PAD_SHAPES",
    "PAD_TYPES", "SIDES", "Body", "FootprintSpec", "Model", "Pad", "has_copper", "pad_bbox",
    # the reader (fp_reader) and the comparison (fp_compare)
    "FootprintParseError", "ParsedFootprint", "ParsedPad", "parse_footprint", "parse_sexpr",
    "read_footprint", "compare_footprints",
    # this module
    "FORMAT_VERSION", "Pin1Marker", "body_courtyard", "courtyard_rect", "silk_segments",
    "place_pin1_marker", "pin1_marker", "fab_outline", "footprint_tree", "render",
    "write_footprint", "summary", "geometry_problems", "static_problems", "oldest_kicad_cli",
    "kicad_parse_problems", "validate", "LOAD_CHILD", "oldest_kicad_python", "pcbnew_load",
]

FORMAT_VERSION = 20241229           # D0: the newest footprint dialect KiCad 9 pcbnew reads
GENERATOR = "kicad-eda"
COURTYARD_GRID_MM = 0.01
SILK_PAD_CLEARANCE_MM = 0.2
SILK_WIDTH_MM = 0.12
SILK_FAB_OFFSET_MM = 0.11           # silk line centre outside the fab line (KLC F5.1)
FAB_WIDTH_MM = 0.10
CRTYD_WIDTH_MM = 0.05
MIN_SILK_SEGMENT_MM = 0.2           # clipped silk stubs shorter than this are dropped
PIN1_MARKER_MM = (0.5, 0.4)         # pin-1 triangle base, height
SILK_GAP_MM = 0.05                  # pin-1 marker to silk outline
SILK_TO_SILK_MM = SILK_WIDTH_MM + SILK_GAP_MM      # marker to outline, centre line to centre line
PIN1_MIN_STANDOFF_MM = SILK_PAD_CLEARANCE_MM + SILK_WIDTH_MM / 2 + 0.01   # marker tip to pad
PIN1_STEP_MM = 0.05                 # the marker search walks outward in these steps ...
PIN1_MAX_STEPS = 400                # ... at most this many (20 mm)
TEXT_MARGIN_MM = 0.7                # courtyard edge to Reference / Value text centre
KICAD_TIMEOUT_S = 120
SERVER_DIR = Path(__file__).resolve().parents[1]
TOOLS_DIR = Path(__file__).resolve().parent
_CLI_ERROR = re.compile(r"(?:^|\s)error:", re.IGNORECASE)   # D0: rc 0 is not proof
_NORMALS = {"L": (-1, 0), "R": (1, 0), "T": (0, -1), "B": (0, 1)}

Point = tuple[float, float]
Rect = tuple[float, float, float, float]        # x0, y0, x1, y1


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------
def _cu_layers(pad: Any) -> set[str]:
    """A pad's copper layers; "*.Cu" stands for both outer layers."""
    cu = {x for x in pad.pad_layers if x.endswith(".Cu")}
    return cu - {"*.Cu"} | ({"F.Cu", "B.Cu"} if "*.Cu" in cu else set())


def _front_copper(pad: Any) -> bool:
    """Copper (or an NPTH hole) the front silk must keep clear of."""
    return any(layer in ("F.Cu", "*.Cu") for layer in pad.pad_layers) or pad.type == "np_thru_hole"


def _union(rects: Iterable[Rect]) -> Rect:
    rs = list(rects)
    return (min(r[0] for r in rs), min(r[1] for r in rs),
            max(r[2] for r in rs), max(r[3] for r in rs))


def _grow(r: Rect, d: float) -> Rect:
    return (r[0] - d, r[1] - d, r[2] + d, r[3] + d)


def _bbox(points: Sequence[Point]) -> Rect:
    return _union((x, y, x, y) for x, y in points)


def _round_out(r: Rect) -> Rect:
    """``r`` rounded outward to COURTYARD_GRID_MM."""
    g = COURTYARD_GRID_MM
    return (_r6(math.floor(r[0] / g + EPS) * g), _r6(math.floor(r[1] / g + EPS) * g),
            _r6(math.ceil(r[2] / g - EPS) * g), _r6(math.ceil(r[3] / g - EPS) * g))


def body_courtyard(spec: FootprintSpec) -> Rect:
    """Max-material body and pad copper, grown by the excess, rounded outward
    to COURTYARD_GRID_MM (the IPC-7351B courtyard, before the pin-1 marker)."""
    copper = [pad_bbox(p) for p in spec.pads if has_copper(p) or p.type == "np_thru_hole"]
    return _round_out(_grow(_union([spec.body.max_rect(), *copper]), spec.courtyard_excess))


def _keepouts(pads: Iterable[Any], stroke: float) -> list[Rect]:
    """Where a silk line CENTRE may not go: front copper grown by clearance + half width."""
    grow = SILK_PAD_CLEARANCE_MM + stroke / 2
    return [_grow(pad_bbox(p), grow) for p in pads if _front_copper(p)]


def _subtract(lo: float, hi: float, cuts: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """[lo, hi] minus the open intervals in cuts."""
    spans = [(lo, hi)]
    for c0, c1 in cuts:
        spans = [piece for a, b in spans
                 for piece in ((a, min(b, c0)), (max(a, c1), b)) if piece[1] - piece[0] > EPS]
    return spans


def _outline(spec: FootprintSpec, extra: Sequence[Rect]) -> list[tuple[Point, Point]]:
    """The body outline SILK_FAB_OFFSET_MM outside the fab line, with every
    part within SILK_PAD_CLEARANCE_MM of pad copper (or inside ``extra``) cut out."""
    x0, y0, x1, y1 = spec.body.rect(SILK_FAB_OFFSET_MM)
    keep = _keepouts(spec.pads, SILK_WIDTH_MM) + list(extra)
    out: list[tuple[Point, Point]] = []
    for y in (y0, y1):                                  # horizontal sides
        cuts = [(k[0], k[2]) for k in keep if k[1] < y - EPS and y + EPS < k[3]]
        out += [((a, y), (b, y)) for a, b in _subtract(x0, x1, cuts)
                if b - a >= MIN_SILK_SEGMENT_MM]
    for x in (x0, x1):                                  # vertical sides
        cuts = [(k[1], k[3]) for k in keep if k[0] < x - EPS and x + EPS < k[2]]
        out += [((x, a), (x, b)) for a, b in _subtract(y0, y1, cuts)
                if b - a >= MIN_SILK_SEGMENT_MM]
    return [((_r6(a[0]), _r6(a[1])), (_r6(b[0]), _r6(b[1]))) for a, b in out]


# ---------------------------------------------------------------------------
# The pin-1 marker
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Pin1Marker:
    """The silk triangle pointing at pin 1 from beyond its ``side`` edge.
    ``inside``: within body_courtyard (else courtyard_rect grows to cover it);
    ``notch``: the silk outline is cut around it, because inside the
    courtyard it had no room clear of the outline."""
    points: tuple[Point, ...]
    side: str
    inside: bool
    notch: bool


def _pin1_pad(spec: FootprintSpec) -> Pad | None:
    if spec.pin1 is None:
        return None
    return next(p for p in spec.pads if p.number == spec.pin1 and has_copper(p))


def _marker_sides(spec: FootprintSpec, pad: Pad) -> tuple[str, ...]:
    """``pin1_side`` when given; for "auto" the body sides pin 1 lies toward
    (its centre on that side of the body centre), the nearest body edge first."""
    if spec.pin1_side in SIDES:
        return (spec.pin1_side,)
    bx0, by0, bx1, by1 = spec.body.rect()
    dist = {"L": pad.x - bx0, "T": pad.y - by0, "R": bx1 - pad.x, "B": by1 - pad.y}
    toward = {"L": pad.x <= spec.body.x + EPS, "T": pad.y <= spec.body.y + EPS,
              "R": pad.x >= spec.body.x - EPS, "B": pad.y >= spec.body.y - EPS}
    return tuple(sorted((s for s in SIDES if toward[s]), key=lambda s: dist[s]))


def _triangle(pad: Pad, side: str, standoff: float) -> tuple[Point, ...]:
    """The marker with its tip ``standoff`` beyond the pad's ``side`` edge."""
    base, height = PIN1_MARKER_MM
    nx, ny = _NORMALS[side]
    px0, py0, px1, py1 = pad_bbox(pad)
    tip = {"L": -px0, "R": px1, "T": -py0, "B": py1}[side] + standoff
    if nx:
        tri = ((nx * tip, pad.y), (nx * (tip + height), pad.y - base / 2),
               (nx * (tip + height), pad.y + base / 2))
    else:
        tri = ((pad.x, ny * tip), (pad.x - base / 2, ny * (tip + height)),
               (pad.x + base / 2, ny * (tip + height)))
    return tuple((_r6(x), _r6(y)) for x, y in tri)


def _edges(points: Sequence[Point]) -> list[tuple[Point, Point]]:
    return list(zip(points, [*points[1:], *points[:1]], strict=True))


def _clear_of_copper(tri: Sequence[Point], pads: Iterable[Any]) -> bool:
    return all(_pad_clearance(a, b, p) - SILK_WIDTH_MM / 2 >= SILK_PAD_CLEARANCE_MM - EPS
               for a, b in _edges(tri) for p in pads if _front_copper(p))


def _clear_of_silk(tri: Sequence[Point], segments: Iterable[tuple[Point, Point]]) -> bool:
    return all(_seg_seg_dist(a, b, c, d) >= SILK_TO_SILK_MM - EPS
               for a, b in _edges(tri) for c, d in segments)


def _inside(tri: Sequence[Point], box: Rect) -> bool:
    """Every vertex, stroke included, within ``box``."""
    x0, y0, x1, y1 = _grow(box, EPS - SILK_WIDTH_MM / 2)
    return all(x0 <= x <= x1 and y0 <= y <= y1 for x, y in tri)


def _marker_walk(spec: FootprintSpec, pad: Pad, side: str) -> Iterator[tuple[Point, ...]]:
    """Copper-clear markers beyond the pad's ``side`` edge, nearest first."""
    for k in range(PIN1_MAX_STEPS):
        tri = _triangle(pad, side, PIN1_MIN_STANDOFF_MM + k * PIN1_STEP_MM)
        if _clear_of_copper(tri, spec.pads):
            yield tri


def _first_inside(spec: FootprintSpec, pad: Pad, side: str, box: Rect,
                  outline: list[tuple[Point, Point]] | None) -> tuple[Point, ...] | None:
    """The nearest copper-clear marker on ``side`` inside ``box`` that also
    clears ``outline`` (None: the outline will yield); None once the walk
    leaves the box."""
    for tri in _marker_walk(spec, pad, side):
        if not _inside(tri, box):
            return None
        if outline is None or _clear_of_silk(tri, outline):
            return tri
    return None


def place_pin1_marker(spec: FootprintSpec) -> Pin1Marker | None:
    """Where the pin-1 marker goes (None: no pin 1, or ``pin1_side`` "none").

    Inside body_courtyard when it fits: on the candidate sides (the given
    ``pin1_side``, or for "auto" the sides pin 1 faces, nearest first), the
    nearest spot clear of copper (SILK_PAD_CLEARANCE_MM) and of the silk
    outline; failing that, the nearest spot clear of copper, and the outline
    yields (a notch). When nothing fits inside, the first side's nearest spot
    clear of copper and outline, and courtyard_rect grows to cover it."""
    pad = _pin1_pad(spec)
    if pad is None or spec.pin1_side == "none":
        return None
    sides, box, outline = _marker_sides(spec, pad), body_courtyard(spec), _outline(spec, ())
    for notch in (False, True):
        for side in sides:
            tri = _first_inside(spec, pad, side, box, None if notch else outline)
            if tri is not None:
                return Pin1Marker(tri, side, inside=True, notch=notch)
    tri = next((t for t in _marker_walk(spec, pad, sides[0]) if _clear_of_silk(t, outline)), None)
    if tri is None:
        raise ValueError(f"{spec.name}: no room for a pin-1 marker on side {sides[0]}")
    return Pin1Marker(tri, sides[0], inside=False, notch=False)


def _notch(marker: Pin1Marker | None) -> list[Rect]:
    """The outline cut-out around a notched marker (SILK_TO_SILK_MM clear)."""
    if marker is None or not marker.notch:
        return []
    return [_grow(_bbox(marker.points), SILK_TO_SILK_MM)]


def _courtyard(spec: FootprintSpec, marker: Pin1Marker | None) -> Rect:
    box = body_courtyard(spec)
    if marker is None or marker.inside:
        return box
    return _round_out(_union([box, _grow(_bbox(marker.points), SILK_WIDTH_MM / 2)]))


def courtyard_rect(spec: FootprintSpec) -> Rect:
    """body_courtyard, grown to cover the pin-1 marker when it has no room
    inside (rounded outward to COURTYARD_GRID_MM)."""
    return _courtyard(spec, place_pin1_marker(spec))


def silk_segments(spec: FootprintSpec) -> list[tuple[Point, Point]]:
    """The silk outline: the body outline SILK_FAB_OFFSET_MM outside the fab
    line, cut where it would come within SILK_PAD_CLEARANCE_MM of pad copper
    and around a notched pin-1 marker."""
    return _outline(spec, _notch(place_pin1_marker(spec)))


def pin1_marker(spec: FootprintSpec) -> list[Point] | None:
    """The pin-1 triangle (tip first), or None (see place_pin1_marker)."""
    marker = place_pin1_marker(spec)
    return list(marker.points) if marker else None


def fab_outline(spec: FootprintSpec) -> list[Point]:
    """Nominal body rectangle with the corner nearest pin 1 chamfered by
    min(1 mm, 25 % of the shorter side)."""
    x0, y0, x1, y1 = spec.body.rect()
    corners = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    pad = _pin1_pad(spec)
    if pad is None:
        return corners
    c = min(1.0, 0.25 * min(spec.body.w, spec.body.h))
    i = min(range(4), key=lambda k: math.hypot(corners[k][0] - pad.x, corners[k][1] - pad.y))
    (cx, cy), prev, nxt = corners[i], corners[i - 1], corners[(i + 1) % 4]

    def toward(p: Point) -> Point:
        n = math.hypot(p[0] - cx, p[1] - cy)
        return (_r6(cx + (p[0] - cx) * c / n), _r6(cy + (p[1] - cy) * c / n))
    return corners[:i] + [toward(prev), toward(nxt)] + corners[i + 1:]


# --- distances (exact for segments vs rectangles / circles) ----------------
def _seg_point_dist(a: Point, b: Point, p: Point) -> float:
    dx, dy = b[0] - a[0], b[1] - a[1]
    n = dx * dx + dy * dy
    t = 0.0 if n == 0 else max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / n))
    return math.hypot(a[0] + t * dx - p[0], a[1] + t * dy - p[1])


def _seg_rect_dist(a: Point, b: Point, r: Rect) -> float:
    """Exact gap between segment ab and rectangle r (0 when they touch): an
    endpoint inside, else the distance to the nearest rectangle edge."""
    if any(r[0] <= p[0] <= r[2] and r[1] <= p[1] <= r[3] for p in (a, b)):
        return 0.0
    c = ((r[0], r[1]), (r[2], r[1]), (r[2], r[3]), (r[0], r[3]))
    return min(_seg_seg_dist(a, b, c[k], c[(k + 1) % 4]) for k in range(4))


def _orient(p: Point, q: Point, s: Point) -> float:
    return (q[0] - p[0]) * (s[1] - p[1]) - (q[1] - p[1]) * (s[0] - p[0])


def _seg_seg_dist(a: Point, b: Point, c: Point, d: Point) -> float:
    if _orient(a, b, c) * _orient(a, b, d) < 0 and _orient(c, d, a) * _orient(c, d, b) < 0:
        return 0.0
    return min(_seg_point_dist(a, b, c), _seg_point_dist(a, b, d),
               _seg_point_dist(c, d, a), _seg_point_dist(c, d, b))


def _pad_clearance(a: Point, b: Point, pad: Any) -> float:
    """Distance from segment ab to a pad's copper (circles exact, else bbox)."""
    if pad.shape == "circle":
        return max(0.0, _seg_point_dist(a, b, (pad.x, pad.y)) - pad.w / 2)
    return _seg_rect_dist(a, b, pad_bbox(pad))


# ---------------------------------------------------------------------------
# Rendering (KiCad 9 serialisation order and layout)
# ---------------------------------------------------------------------------
class Q(str):
    """An atom written as a quoted string."""


def _num(v: float) -> str:
    s = f"{_r6(float(v)):.6f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def _atom(a: Any) -> str:
    if isinstance(a, Q):
        return '"' + a.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'
    if isinstance(a, (int, float)) and not isinstance(a, bool):
        return _num(a)
    return str(a)


def _emit(node: tuple, depth: int = 0) -> list[str]:
    tab = "\t" * depth
    head = " ".join(_atom(a) for a in node if not isinstance(a, tuple))
    kids = [k for k in node if isinstance(k, tuple)]
    if not kids:
        return [f"{tab}({head})"]
    return [f"{tab}({head}", *(line for k in kids for line in _emit(k, depth + 1)), f"{tab})"]


def _stroke(width: float) -> tuple:
    return ("stroke", ("width", width), ("type", "solid"))


def _font(size: float) -> tuple:
    return ("effects", ("font", ("size", size, size), ("thickness", _r6(size * 0.15))))


def _text_prop(key: str, value: str, at: Point, layer: str, hide: bool = False) -> tuple:
    node: tuple = ("property", Q(key), Q(value), ("at", at[0], at[1], 0))
    if hide:
        node += (("unlocked", "yes"), ("layer", Q(layer)), ("hide", "yes"))
    else:
        node += (("layer", Q(layer)),)
    return node + (_font(1.0 if not hide else 1.27),)


def _pad_node(p: Pad) -> tuple:
    at = ("at", p.x, p.y) + ((p.rotation,) if abs(p.rotation) > EPS else ())
    node: tuple = ("pad", Q(p.number), p.type, p.shape, at, ("size", p.w, p.h))
    if p.drill is not None:
        node += (("drill", p.drill),)
    if p.heatsink:
        node += (("property", "pad_prop_heatsink"),)
    node += (("layers", *(Q(layer) for layer in p.pad_layers)),)
    if p.type == "thru_hole":
        node += (("remove_unused_layers", "no"),)
    if p.shape == "roundrect":
        node += (("roundrect_rratio", p.roundrect_ratio),)
    return node


def _poly(points: Sequence[Point], width: float, layer: str, fill: bool) -> tuple:
    return ("fp_poly", ("pts", *(("xy", x, y) for x, y in points)), _stroke(width),
            ("fill", "yes" if fill else "no"), ("layer", Q(layer)))


def footprint_tree(spec: FootprintSpec) -> tuple:
    """The footprint as a nested tuple tree (render() prints it)."""
    marker = place_pin1_marker(spec)
    cx0, cy0, cx1, cy1 = _courtyard(spec, marker)
    bx, by = spec.body.x, spec.body.y
    tree: tuple = ("footprint", Q(spec.name), ("version", FORMAT_VERSION),
                   ("generator", Q(GENERATOR)), ("layer", Q("F.Cu")))
    if spec.descr:
        tree += (("descr", Q(spec.descr)),)
    if spec.tags:
        tree += (("tags", Q(spec.tags)),)
    tree += (_text_prop("Reference", "REF**", (bx, _r6(cy0 - TEXT_MARGIN_MM)), "F.SilkS"),
             _text_prop("Value", spec.name, (bx, _r6(cy1 + TEXT_MARGIN_MM)), "F.Fab"))
    if spec.datasheet:
        tree += (_text_prop("Datasheet", spec.datasheet, (bx, by), "F.Fab", hide=True),)
    tree += (("attr", spec.attr, *spec.attr_flags),)
    for a, b in _outline(spec, _notch(marker)):
        tree += (("fp_line", ("start", *a), ("end", *b), _stroke(SILK_WIDTH_MM),
                  ("layer", Q("F.SilkS"))),)
    if marker:
        tree += (_poly(marker.points, SILK_WIDTH_MM, "F.SilkS", fill=True),)
    tree += (("fp_rect", ("start", cx0, cy0), ("end", cx1, cy1), _stroke(CRTYD_WIDTH_MM),
              ("fill", "no"), ("layer", Q("F.CrtYd"))),
             _poly(fab_outline(spec), FAB_WIDTH_MM, "F.Fab", fill=False))
    size = _r6(max(0.5, min(1.0, 0.25 * min(spec.body.w, spec.body.h))))
    tree += (("fp_text", "user", Q("${REFERENCE}"), ("at", bx, by, 0), ("layer", Q("F.Fab")),
              _font(size)),)
    tree += tuple(_pad_node(p) for p in spec.pads) + (("embedded_fonts", "no"),)
    if spec.model:
        m = spec.model
        tree += (("model", Q(m.path), ("offset", ("xyz", *m.offset)),
                  ("scale", ("xyz", *m.scale)), ("rotate", ("xyz", *m.rotate))),)
    return tree


def render(spec: FootprintSpec) -> str:
    """The .kicad_mod text (D0 dialect, tab-indented like KiCad's own writer)."""
    return "\n".join(_emit(footprint_tree(spec))) + "\n"


def write_footprint(spec: FootprintSpec, pretty_dir: str | os.PathLike) -> Path:
    """Write ``<pretty_dir>/<name>.kicad_mod`` (atomically); returns its path."""
    lib = Path(pretty_dir)
    if lib.suffix != ".pretty":
        raise ValueError(f"{lib} is not a KiCad footprint library (a *.pretty directory)")
    lib.mkdir(parents=True, exist_ok=True)
    path = lib / f"{spec.name}.kicad_mod"
    tmp = path.with_suffix(".kicad_mod.tmp")
    tmp.write_text(render(spec), encoding="utf-8", newline="\n")
    os.replace(tmp, path)
    return path


def summary(spec: FootprintSpec) -> dict:
    """JSON-friendly facts for a tool result / the netlist."""
    numbers = sorted({p.number for p in spec.pads if p.number and has_copper(p)}, key=natural_key)
    return {"name": spec.name, "pads": len(numbers), "pad_numbers": numbers,
            "attr": spec.attr, "body": [spec.body.w, spec.body.h], "height": spec.body.height,
            "courtyard": list(courtyard_rect(spec))}


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------
def _courtyard_problems(fp: ParsedFootprint, copper: list[Any], excess: float) -> list[str]:
    crtyd = fp.strokes.get("F.CrtYd", ())
    if not crtyd:
        return ["no F.CrtYd courtyard"]
    pts = [pt for a, b, _ in crtyd for pt in (a, b)]
    cx0, cy0, cx1, cy1 = _bbox(pts)
    tight = []
    for p in copper:
        x0, y0, x1, y1 = pad_bbox(p)
        margin = min(x0 - cx0, y0 - cy0, cx1 - x1, cy1 - y1)
        if margin < excess - EPS:
            tight.append((margin, p.number))
    if not tight:
        return []
    worst = min(tight)
    return [f"courtyard leaves {worst[0]:.3f} mm around pad {worst[1]!r} "
            f"({len(tight)} pad(s) under the IPC-7351B nominal {excess} mm excess)"]


def geometry_problems(fp: ParsedFootprint, excess: float = COURTYARD_EXCESS_MM) -> list[str]:
    """Courtyard present with >= ``excess`` around all copper, an F.Fab
    drawing, silk >= SILK_PAD_CLEARANCE_MM from front copper, and no copper
    overlap between differently numbered pads."""
    copper = [p for p in fp.pads if has_copper(p) or p.type == "np_thru_hole"]
    problems = _courtyard_problems(fp, copper, excess)
    if not fp.strokes.get("F.Fab"):
        problems.append("no F.Fab body outline")
    hits: dict[str, float] = {}
    for a, b, width in fp.strokes.get("F.SilkS", ()):
        for p in copper:
            if _front_copper(p):
                gap = _pad_clearance(a, b, p) - width / 2
                if gap < SILK_PAD_CLEARANCE_MM - EPS:
                    hits[p.number] = min(gap, hits.get(p.number, gap))
    if hits:
        problems.append(f"silk within {SILK_PAD_CLEARANCE_MM} mm of pad(s) "
                        f"{sorted(hits, key=natural_key)[:10]} "
                        f"(closest {min(hits.values()):.3f} mm)")
    boxes = [(pad_bbox(p), _cu_layers(p), p) for p in copper if p.number and has_copper(p)]
    overlaps = sorted({(p.number, q.number) for i, (r, lr, p) in enumerate(boxes)
                       for s, ls, q in boxes[i + 1:] if p.number != q.number and lr & ls
                       and min(r[2], s[2]) - max(r[0], s[0]) > EPS
                       and min(r[3], s[3]) - max(r[1], s[1]) > EPS})
    if overlaps:
        problems.append(f"copper of differently numbered pads overlaps: {overlaps[:10]}")
    return problems


def static_problems(fp: ParsedFootprint, path: str | os.PathLike | None = None,
                    expected_pads: int | None = None) -> list[str]:
    """Dialect, naming, attribute and pad-count checks plus geometry_problems."""
    problems: list[str] = []
    if fp.version is None:
        problems.append("no (version ...) header")
    elif fp.version > FORMAT_VERSION:
        problems.append(f"format version {fp.version} is newer than {FORMAT_VERSION}: "
                        f"KiCad 9 pcbnew cannot load it (ROADMAP D0)")
    if path is not None and Path(path).stem != fp.name:
        problems.append(f"footprint name {fp.name!r} differs from the file name "
                        f"{Path(path).stem!r}")
    if not set(fp.attr) & set(ATTR_TYPES):
        problems.append("no (attr smd|through_hole)")
    numbered = {p.number for p in fp.pads if p.number and has_copper(p)}
    if not numbered:
        problems.append("no numbered copper pads")
    if expected_pads is not None and len(numbered) != expected_pads:
        problems.append(f"{len(numbered)} distinct pad numbers, expected {expected_pads}")
    return problems + geometry_problems(fp)


# ---------------------------------------------------------------------------
# KiCad itself: kicad-cli parse check, pcbnew FootprintLoad
# ---------------------------------------------------------------------------
def _kicad_installs() -> list:
    if str(SERVER_DIR) not in sys.path:
        sys.path.insert(0, str(SERVER_DIR))
    import kicad_env as env
    return env.discover()


def oldest_kicad_cli() -> tuple[str | None, str]:
    """(kicad-cli path, label) of the oldest installed KiCad: the D0 dialect
    floor is what the oldest supported pcbnew reads. (None, "") when none."""
    found = _kicad_installs()
    if not found:
        return None, ""
    inst = min(found, key=lambda i: i.version_tuple)
    return str(inst.kicad_cli), f"kicad-cli {inst.version}"


def _run_kicad(args: list[str]) -> subprocess.CompletedProcess:
    """A KiCad child (D0): stdin DEVNULL, utf-8 with replacement, a timeout."""
    return subprocess.run(args, capture_output=True, stdin=subprocess.DEVNULL,
                          encoding="utf-8", errors="replace", timeout=KICAD_TIMEOUT_S)


def kicad_parse_problems(path: str | os.PathLike, kicad_cli: str | None = None) -> list[str]:
    """Load the file with ``kicad-cli fp upgrade`` (a .pretty copy in a temp
    dir) and require KiCad's re-serialised pads (copper and paste windows) to
    equal the source's. rc 0 alone is not proof: "Error:" lines count too."""
    path = Path(path)
    cli, label = (kicad_cli, f"kicad-cli ({kicad_cli})") if kicad_cli else oldest_kicad_cli()
    if not cli:
        return ["no kicad-cli found (install KiCad 9 or set KICAD_PATH): "
                "the KiCad parse check did not run"]
    with tempfile.TemporaryDirectory(prefix="fp_author_") as tmp:
        src, out = Path(tmp) / "check.pretty", Path(tmp) / "upgraded.pretty"
        src.mkdir()
        shutil.copyfile(path, src / path.name)
        try:
            r = _run_kicad([cli, "fp", "upgrade", "--force", "-o", str(out), str(src)])
        except (OSError, subprocess.TimeoutExpired) as e:
            return [f"{label} fp upgrade could not run: {e}"]
        console = ((r.stdout or "") + (r.stderr or "")).strip()
        if r.returncode != 0:
            return [f"{label} fp upgrade could not load {path.name} (rc={r.returncode}): "
                    f"{console[-400:] or 'no output'}"]
        problems = [f"{label} fp upgrade reported: {ln.strip()}"
                    for ln in console.splitlines() if _CLI_ERROR.search(ln)]
        produced = out / path.name
        if not produced.is_file():
            return problems + [f"{label} fp upgrade wrote no {path.name}"]
        diff = compare_footprints(path, produced)
        if not diff["identical"]:
            problems.append(f"{label} re-serialised the pads differently: {diff['summary']}")
        return problems


def validate(path: str | os.PathLike, *, expected_pads: int | None = None,
             kicad_cli: str | None = None) -> list[str]:
    """Problems with a .kicad_mod ([] = good): static_problems, then the KiCad
    parse check (kicad_parse_problems; oldest installed kicad-cli unless one
    is given). A missing kicad-cli is a problem, never a silent pass."""
    path = Path(path)
    if not path.is_file():
        return [f"{path}: no such file"]
    try:
        fp = read_footprint(path)
    except FootprintParseError as e:
        return [f"{path.name}: {e}"]
    return static_problems(fp, path, expected_pads) + kicad_parse_problems(path, kicad_cli)


# The KiCad-python child for pcbnew_load: argv = [tools dir, .pretty dir, name].
# kicad_quiet (from the tools dir) comes before pcbnew: no modal assert / log
# dialog can block (D0). 9.0.4 leaves GetCourtyard empty until
# BuildCourtyardCaches() ran.
LOAD_CHILD = """\
import json
import sys

sys.path.insert(0, sys.argv[1])
import kicad_quiet  # noqa: E402,F401  (before pcbnew: no modal wx dialogs)
import pcbnew  # noqa: E402

out = {"kicad_version": pcbnew.Version()}
try:
    fp = pcbnew.FootprintLoad(sys.argv[2], sys.argv[3])
except Exception as e:          # KiCad's IO_ERROR on a file it cannot parse
    fp, out["error"] = None, f"{type(e).__name__}: {e}"
out["loaded"] = fp is not None
if fp is not None:
    numbers = [str(p.GetNumber()) for p in fp.Pads()]
    if hasattr(fp, "BuildCourtyardCaches"):
        fp.BuildCourtyardCaches()
    out.update(pads=len(numbers), numbers=numbers,
               courtyard_outlines=fp.GetCourtyard(pcbnew.F_CrtYd).OutlineCount())
print(json.dumps(out), flush=True)
"""


def oldest_kicad_python() -> str | None:
    """The oldest install's bundled Python verified to import pcbnew."""
    found = [i for i in _kicad_installs() if i.python is not None]
    return str(min(found, key=lambda i: i.version_tuple).python) if found else None


def pcbnew_load(path: str | os.PathLike, *, python: str | None = None) -> dict:
    """Load the footprint with pcbnew.FootprintLoad (oldest install's KiCad
    Python unless ``python`` is given) in a child with stdin DEVNULL, from a
    temp .pretty copy (FootprintLoad picks its plugin from the library path):
    {"ok", "loaded", "pads", "numbers", "courtyard_outlines", "kicad_version"},
    or {"ok": False, "error"}."""
    path = Path(path)
    if not path.is_file():
        return {"ok": False, "error": f"{path}: no such file"}
    python = python or oldest_kicad_python()
    if not python:
        return {"ok": False, "error": "no KiCad bundled Python with pcbnew found"}
    with tempfile.TemporaryDirectory(prefix="fp_author_") as tmp:
        lib = Path(tmp) / "check.pretty"
        lib.mkdir()
        shutil.copyfile(path, lib / path.name)
        try:
            r = _run_kicad([python, "-c", LOAD_CHILD, str(TOOLS_DIR), str(lib), path.stem])
        except (OSError, subprocess.TimeoutExpired) as e:
            return {"ok": False, "error": f"KiCad python could not run: {e}"}
    console = ((r.stdout or "") + (r.stderr or "")).strip()
    lines = [ln for ln in (r.stdout or "").strip().splitlines() if ln.startswith("{")]
    try:
        result = json.loads(lines[-1]) if r.returncode == 0 and lines else None
    except ValueError:
        result = None
    if not isinstance(result, dict):
        return {"ok": False, "error": f"rc={r.returncode}: {console[-600:]}"}
    if not result.get("loaded") and "error" not in result:
        result = {**result, "error": f"pcbnew.FootprintLoad returned None for {path.name}"}
    return {"ok": bool(result.get("loaded")), **result}
