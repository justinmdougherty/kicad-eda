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
    silk stroke keeps >= 0.2 mm from pad copper, and a pin-1 triangle;
  * F.CrtYd: one rectangle around the max-material body and all pad copper,
    grown by the courtyard excess and rounded outward to a 0.01 mm grid. The
    excess is 0.25 mm: IPC-7351B ("Generic Requirements for Surface Mount
    Design and Land Pattern Standard", 2010), density level B "nominal"
    courtyard excess; the KiCad Library Convention (F5.3) uses the same value;
  * Reference (F.SilkS, above the courtyard) and Value (F.Fab, below it).

Also here: a stdlib .kicad_mod reader; static checks (dialect, courtyard
excess, silk clearance, copper overlaps); ``validate(path)`` = those checks
plus ``kicad-cli fp upgrade`` (oldest install) into a temp dir, whose
re-serialised pads must equal the source's; ``pcbnew_load(path)``
(FootprintLoad in a KiCad-python child); ``compare_footprints(a, b)``.
Templates are in fp_author_templates.py. Units: mm, footprint frame (+Y
down). Stdlib-only, except that validate / pcbnew_load find KiCad through kicad_env.
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
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

from fp_author_templates import (  # noqa: F401  (the spec vocabulary, re-exported)
    ATTR_FLAGS, ATTR_TYPES, COURTYARD_EXCESS_MM, DEFAULT_LAYERS, EPS, PAD_SHAPES, PAD_TYPES,
    SIDES, Body, FootprintSpec, Model, Pad, has_copper)

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
TEXT_MARGIN_MM = 0.7                # courtyard edge to Reference / Value text centre
KICAD_TIMEOUT_S = 120
SERVER_DIR = Path(__file__).resolve().parents[1]

Point = tuple[float, float]
Rect = tuple[float, float, float, float]        # x0, y0, x1, y1


class FootprintParseError(ValueError):
    """A .kicad_mod file could not be read as a KiCad footprint."""


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


def pad_bbox(pad: Any) -> Rect:
    """Axis-aligned extent of a pad's copper (any rotation)."""
    a = math.radians(pad.rotation)
    c, s = abs(math.cos(a)), abs(math.sin(a))
    hw, hh = (pad.w * c + pad.h * s) / 2, (pad.w * s + pad.h * c) / 2
    return (pad.x - hw, pad.y - hh, pad.x + hw, pad.y + hh)


def _union(rects: Iterable[Rect]) -> Rect:
    rs = list(rects)
    return (min(r[0] for r in rs), min(r[1] for r in rs),
            max(r[2] for r in rs), max(r[3] for r in rs))


def _grow(r: Rect, d: float) -> Rect:
    return (r[0] - d, r[1] - d, r[2] + d, r[3] + d)


def courtyard_rect(spec: FootprintSpec) -> Rect:
    """Max-material body and pad copper, grown by the excess, rounded outward
    to COURTYARD_GRID_MM."""
    copper = [pad_bbox(p) for p in spec.pads if has_copper(p) or p.type == "np_thru_hole"]
    r = _grow(_union([spec.body.max_rect(), *copper]), spec.courtyard_excess)
    g = COURTYARD_GRID_MM
    return (_r6(math.floor(r[0] / g + EPS) * g), _r6(math.floor(r[1] / g + EPS) * g),
            _r6(math.ceil(r[2] / g - EPS) * g), _r6(math.ceil(r[3] / g - EPS) * g))


def _r6(v: float) -> float:
    return round(v, 6) + 0.0          # + 0.0 turns -0.0 into 0.0


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


def silk_segments(spec: FootprintSpec) -> list[tuple[Point, Point]]:
    """The body outline SILK_FAB_OFFSET_MM outside the fab line, with every
    part that would come within SILK_PAD_CLEARANCE_MM of pad copper cut out."""
    x0, y0, x1, y1 = spec.body.rect(SILK_FAB_OFFSET_MM)
    keep = _keepouts(spec.pads, SILK_WIDTH_MM)
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


def _pin1_pad(spec: FootprintSpec) -> Pad | None:
    if spec.pin1 is None:
        return None
    return next(p for p in spec.pads if p.number == spec.pin1 and has_copper(p))


def _pin1_side(spec: FootprintSpec, pad: Pad) -> str:
    if spec.pin1_side in SIDES:
        return spec.pin1_side
    bx0, by0, bx1, by1 = spec.body.rect()
    dist = {"L": pad.x - bx0, "T": pad.y - by0, "R": bx1 - pad.x, "B": by1 - pad.y}
    return min(SIDES, key=lambda s: dist[s])


def pin1_marker(spec: FootprintSpec) -> list[Point] | None:
    """A silk triangle beyond pin 1's ``pin1_side`` edge pointing at the pad,
    clear of all copper (SILK_PAD_CLEARANCE_MM) and of the silk outline."""
    pad = _pin1_pad(spec)
    if pad is None or spec.pin1_side == "none":
        return None
    side = _pin1_side(spec, pad)
    base, height = PIN1_MARKER_MM
    nx, ny = {"L": (-1, 0), "R": (1, 0), "T": (0, -1), "B": (0, 1)}[side]
    px0, py0, px1, py1 = pad_bbox(pad)
    edge = {"L": -px0, "R": px1, "T": -py0, "B": py1}[side]   # pad's outer edge along the normal
    outline = silk_segments(spec)
    d = SILK_PAD_CLEARANCE_MM + SILK_WIDTH_MM / 2 + 0.01
    for _ in range(400):
        tip = edge + d
        if nx:
            tri = [(nx * tip, pad.y), (nx * (tip + height), pad.y - base / 2),
                   (nx * (tip + height), pad.y + base / 2)]
        else:
            tri = [(pad.x, ny * tip), (pad.x - base / 2, ny * (tip + height)),
                   (pad.x + base / 2, ny * (tip + height))]
        tri = [(_r6(x), _r6(y)) for x, y in tri]
        edges = list(zip(tri, tri[1:] + tri[:1]))
        pad_ok = all(_pad_clearance(a, b, p) - SILK_WIDTH_MM / 2 >= SILK_PAD_CLEARANCE_MM - EPS
                     for a, b in edges for p in spec.pads if _front_copper(p))
        silk_ok = all(_seg_seg_dist(a, b, c, e) >= SILK_WIDTH_MM + SILK_GAP_MM - EPS
                      for a, b in edges for c, e in outline)
        if pad_ok and silk_ok:
            return tri
        d += 0.05
    raise ValueError(f"{spec.name}: no room for a pin-1 marker on side {side}")


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


def _seg_seg_dist(a: Point, b: Point, c: Point, d: Point) -> float:
    def orient(p: Point, q: Point, s: Point) -> float:
        return (q[0] - p[0]) * (s[1] - p[1]) - (q[1] - p[1]) * (s[0] - p[0])
    if orient(a, b, c) * orient(a, b, d) < 0 and orient(c, d, a) * orient(c, d, b) < 0:
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
    cx0, cy0, cx1, cy1 = courtyard_rect(spec)
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
    for a, b in silk_segments(spec):
        tree += (("fp_line", ("start", *a), ("end", *b), _stroke(SILK_WIDTH_MM),
                  ("layer", Q("F.SilkS"))),)
    marker = pin1_marker(spec)
    if marker:
        tree += (_poly(marker, SILK_WIDTH_MM, "F.SilkS", fill=True),)
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
    numbers = sorted({p.number for p in spec.pads if p.number and has_copper(p)}, key=_natural)
    return {"name": spec.name, "pads": len(numbers), "pad_numbers": numbers,
            "attr": spec.attr, "body": [spec.body.w, spec.body.h], "height": spec.body.height,
            "courtyard": list(courtyard_rect(spec))}


def _natural(s: str) -> list:
    return [(0, int(t), "") if t.isdigit() else (1, 0, t) for t in re.findall(r"\d+|\D+", s)]


# ---------------------------------------------------------------------------
# Reading .kicad_mod files (stdlib s-expressions)
# ---------------------------------------------------------------------------
_TOKEN = re.compile(r'\s*(?:(\()|(\))|"((?:[^"\\]|\\.)*)"|([^\s()"]+))', re.S)
_NUMBER = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")


def parse_sexpr(text: str) -> list:
    """Nested lists of string atoms (quotes removed, escapes resolved)."""
    stack: list[list] = [[]]
    pos = 0
    text = text.rstrip()
    while pos < len(text):
        m = _TOKEN.match(text, pos)
        if not m:
            raise FootprintParseError(f"unreadable text at offset {pos}: {text[pos:pos + 30]!r}")
        pos = m.end()
        if m.group(1):
            stack.append([])
        elif m.group(2):
            if len(stack) < 2:
                raise FootprintParseError(f"unbalanced ')' at offset {pos}")
            stack[-2].append(stack.pop())
        elif m.group(3) is not None:
            stack[-1].append(re.sub(r"\\(.)", lambda e: "\n" if e.group(1) == "n" else e.group(1),
                                    m.group(3)))
        else:
            stack[-1].append(m.group(4))
    if len(stack) != 1 or len(stack[0]) != 1 or not isinstance(stack[0][0], list):
        raise FootprintParseError("expected exactly one balanced top-level expression")
    return stack[0][0]


def _kids(node: list, tag: str) -> list[list]:
    return [c for c in node if isinstance(c, list) and c and c[0] == tag]


def _first(node: list, tag: str) -> list | None:
    found = _kids(node, tag)
    return found[0] if found else None


def _floats(node: list | None, n: int) -> list[float]:
    vals = [float(v) for v in (node or [])[1:] if isinstance(v, str) and _NUMBER.fullmatch(v)]
    return vals[:n] + [0.0] * (n - len(vals[:n]))


@dataclass(frozen=True)
class ParsedPad:
    number: str
    type: str
    shape: str
    x: float
    y: float
    w: float
    h: float
    rotation: float
    drill: float | None
    pad_layers: tuple[str, ...]


@dataclass(frozen=True)
class ParsedFootprint:
    name: str
    version: int | None
    generator: str | None
    attr: tuple[str, ...]
    pads: tuple[ParsedPad, ...]
    strokes: dict[str, tuple[tuple[Point, Point, float], ...]] = field(default_factory=dict)


def _arc_points(s: Point, m: Point, e: Point, steps: int = 12) -> list[Point]:
    """Points along the circular arc start -> mid -> end."""
    ax, ay, bx, by, cx, cy = *s, *m, *e
    d = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(d) < 1e-12:
        return [s, e]
    sa, sb, sc = ax**2 + ay**2, bx**2 + by**2, cx**2 + cy**2
    ux = (sa * (by - cy) + sb * (cy - ay) + sc * (ay - by)) / d
    uy = (sa * (cx - bx) + sb * (ax - cx) + sc * (bx - ax)) / d
    r = math.hypot(ax - ux, ay - uy)
    a0, am, a1 = (math.atan2(p[1] - uy, p[0] - ux) for p in (s, m, e))
    sweep = (a1 - a0) % (2 * math.pi)
    if (am - a0) % (2 * math.pi) > sweep:           # mid is on the other way round
        sweep -= 2 * math.pi
    return [(ux + r * math.cos(a0 + sweep * i / steps), uy + r * math.sin(a0 + sweep * i / steps))
            for i in range(steps + 1)]


def _graphic_segments(node: list) -> list[tuple[Point, Point]]:
    tag = node[0]
    if tag == "fp_line":
        s, e = _floats(_first(node, "start"), 2), _floats(_first(node, "end"), 2)
        return [((s[0], s[1]), (e[0], e[1]))]
    if tag in ("fp_rect", "fp_poly", "fp_circle", "fp_arc"):
        if tag == "fp_rect":
            (x0, y0), (x1, y1) = _floats(_first(node, "start"), 2), _floats(_first(node, "end"), 2)
            pts = [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]
        elif tag == "fp_poly":
            pts = [tuple(_floats(xy, 2)) for xy in _kids(_first(node, "pts") or [], "xy")]
            pts = pts + pts[:1]
        elif tag == "fp_circle":
            c, e = _floats(_first(node, "center"), 2), _floats(_first(node, "end"), 2)
            r = math.hypot(e[0] - c[0], e[1] - c[1])
            pts = [(c[0] + r * math.cos(math.pi * i / 12), c[1] + r * math.sin(math.pi * i / 12))
                   for i in range(25)]
        else:
            s, m, e = (tuple(_floats(_first(node, k), 2)) for k in ("start", "mid", "end"))
            pts = _arc_points(s, m, e)
        return list(zip(pts, pts[1:]))
    return []


def _parse_pad(p: list) -> ParsedPad:
    if len(p) < 4:
        raise FootprintParseError(f"pad with too few fields: {p[:4]}")
    x, y, rot = _floats(_first(p, "at"), 3)
    w, h = _floats(_first(p, "size"), 2)
    drill_node = _first(p, "drill")
    drill = next((float(v) for v in (drill_node or [])[1:]
                  if isinstance(v, str) and _NUMBER.fullmatch(v)), None)
    layers = tuple(v for v in (_first(p, "layers") or [])[1:] if isinstance(v, str))
    return ParsedPad(str(p[1]), str(p[2]), str(p[3]), x, y, w, h, rot, drill, layers)


def parse_footprint(text: str) -> ParsedFootprint:
    root = parse_sexpr(text)
    if not root or root[0] not in ("footprint", "module") or len(root) < 2:
        raise FootprintParseError(f"not a footprint: top-level ({root[0] if root else ''} ...)")
    version_node, generator_node = _first(root, "version"), _first(root, "generator")
    strokes: dict[str, list] = {}
    for node in root:
        if isinstance(node, list) and node and str(node[0]).startswith("fp_"):
            layer_node = _first(node, "layer")
            width_node = _first(_first(node, "stroke") or [], "width") or _first(node, "width")
            width = _floats(width_node, 1)[0]
            for a, b in _graphic_segments(node):
                strokes.setdefault(layer_node[1] if layer_node else "", []).append((a, b, width))
    try:
        version = int(version_node[1]) if version_node else None
    except (IndexError, ValueError) as e:
        raise FootprintParseError(f"unreadable (version ...): {version_node}") from e
    return ParsedFootprint(name=str(root[1]), version=version,
                           generator=str(generator_node[1]) if generator_node else None,
                           attr=tuple(str(a) for a in (_first(root, "attr") or [])[1:]),
                           pads=tuple(_parse_pad(p) for p in _kids(root, "pad")),
                           strokes={k: tuple(v) for k, v in strokes.items()})


def read_footprint(path: str | os.PathLike) -> ParsedFootprint:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        raise FootprintParseError(f"cannot read {path}: {e}") from e
    return parse_footprint(text)


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------
def geometry_problems(fp: ParsedFootprint, excess: float = COURTYARD_EXCESS_MM) -> list[str]:
    """Courtyard present with >= ``excess`` around all copper, an F.Fab
    drawing, silk >= SILK_PAD_CLEARANCE_MM from front copper, and no copper
    overlap between differently numbered pads."""
    problems: list[str] = []
    copper = [p for p in fp.pads if has_copper(p) or p.type == "np_thru_hole"]
    crtyd = fp.strokes.get("F.CrtYd", ())
    if not crtyd:
        problems.append("no F.CrtYd courtyard")
    elif copper:
        pts = [pt for a, b, _ in crtyd for pt in (a, b)]
        cx0, cy0 = min(x for x, _ in pts), min(y for _, y in pts)
        cx1, cy1 = max(x for x, _ in pts), max(y for _, y in pts)
        tight = []
        for p in copper:
            x0, y0, x1, y1 = pad_bbox(p)
            margin = min(x0 - cx0, y0 - cy0, cx1 - x1, cy1 - y1)
            if margin < excess - EPS:
                tight.append((margin, p.number))
        if tight:
            worst = min(tight)
            problems.append(f"courtyard leaves {worst[0]:.3f} mm around pad {worst[1]!r} "
                            f"({len(tight)} pad(s) under the IPC-7351B nominal {excess} mm excess)")
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
                        f"{sorted(hits, key=_natural)[:10]} (closest {min(hits.values()):.3f} mm)")
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


def kicad_parse_problems(path: str | os.PathLike, kicad_cli: str | None = None) -> list[str]:
    """Load the file with ``kicad-cli fp upgrade`` (a .pretty copy in a temp
    dir) and require KiCad's re-serialised pads to equal the source's."""
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
            r = subprocess.run([cli, "fp", "upgrade", "--force", "-o", str(out), str(src)],
                               capture_output=True, text=True, stdin=subprocess.DEVNULL,
                               timeout=KICAD_TIMEOUT_S)
        except (OSError, subprocess.TimeoutExpired) as e:
            return [f"{label} fp upgrade could not run: {e}"]
        console = (r.stdout + r.stderr).strip()
        if r.returncode != 0:
            return [f"{label} fp upgrade could not load {path.name} (rc={r.returncode}): "
                    f"{console[-400:] or 'no output'}"]
        problems = [f"{label} fp upgrade reported: {ln.strip()}"
                    for ln in console.splitlines() if "error" in ln.lower()]
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


# The KiCad-python child for pcbnew_load. The wx preamble (kicad_quiet's,
# inline) comes before pcbnew: no modal assert / log dialog can block (D0).
LOAD_CHILD = """\
import wx
wx.DisableAsserts()
wx.Log.SetLogLevel(wx.LOG_Info)
_LOG_TARGET = wx.LogStderr()   # kept referenced: wx holds only a raw pointer to it
wx.Log.SetActiveTarget(_LOG_TARGET)
import json
import sys
import pcbnew

out = {"kicad_version": pcbnew.Version()}
try:
    fp = pcbnew.FootprintLoad(sys.argv[1], sys.argv[2])
except Exception as e:          # KiCad's IO_ERROR on a file it cannot parse
    fp, out["error"] = None, f"{type(e).__name__}: {e}"
out["loaded"] = fp is not None
if fp is not None:
    numbers = [str(p.GetNumber()) for p in fp.Pads()]
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
            r = subprocess.run([python, "-c", LOAD_CHILD, str(lib), path.stem],
                               capture_output=True, text=True, stdin=subprocess.DEVNULL,
                               timeout=KICAD_TIMEOUT_S)
        except (OSError, subprocess.TimeoutExpired) as e:
            return {"ok": False, "error": f"KiCad python could not run: {e}"}
    lines = [ln for ln in r.stdout.strip().splitlines() if ln.startswith("{")]
    if r.returncode != 0 or not lines:
        return {"ok": False, "error": f"rc={r.returncode}: {(r.stdout + r.stderr).strip()[-600:]}"}
    result = json.loads(lines[-1])
    if not result.get("loaded") and "error" not in result:
        result["error"] = f"pcbnew.FootprintLoad returned None for {path.name}"
    return {"ok": bool(result.get("loaded")), **result}


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------
def _extent(p: Any) -> tuple[float, float]:
    x0, y0, x1, y1 = pad_bbox(p)
    return x1 - x0, y1 - y0


def compare_footprints(a_path: str | os.PathLike, b_path: str | os.PathLike, *,
                       tolerance: float = 0.001, match_radius: float = 1.0) -> dict:
    """Pair the copper pads of A and B by position (nearest first, within
    ``match_radius`` mm) and report per-pad position / size / shape deltas,
    pads whose numbers differ, duplicate numbers and unmatched pads.
    ``identical`` is True only when nothing differs beyond ``tolerance``."""
    fa, fb = read_footprint(a_path), read_footprint(b_path)
    pa = [p for p in fa.pads if has_copper(p)]
    pb = [p for p in fb.pads if has_copper(p)]
    cands = sorted((math.hypot(p.x - q.x, p.y - q.y), set(p.pad_layers) != set(q.pad_layers),
                    p.number != q.number, i, j)
                   for i, p in enumerate(pa) for j, q in enumerate(pb)
                   if abs(p.x - q.x) <= match_radius and abs(p.y - q.y) <= match_radius)
    used_a: set[int] = set()
    used_b: set[int] = set()
    deviations, renumbered, max_offset = [], 0, 0.0
    for dist, _, _, i, j in cands:
        if dist > match_radius or i in used_a or j in used_b:
            continue
        used_a.add(i)
        used_b.add(j)
        p, q = pa[i], pb[j]
        (wa, ha), (wb, hb) = _extent(p), _extent(q)
        issues = [name for name, hit in (
            ("moved", dist > tolerance), ("resized", max(abs(wb - wa), abs(hb - ha)) > tolerance),
            ("shape", p.shape != q.shape), ("type", p.type != q.type),
            ("layers", set(p.pad_layers) != set(q.pad_layers)),
            ("renumbered", p.number != q.number)) if hit]
        max_offset = max(max_offset, dist)
        renumbered += p.number != q.number
        if issues:
            deviations.append({"a": p.number, "b": q.number, "at": [p.x, p.y],
                               "dx": _r6(q.x - p.x), "dy": _r6(q.y - p.y), "offset": _r6(dist),
                               "dw": _r6(wb - wa), "dh": _r6(hb - ha),
                               "shape": [p.shape, q.shape], "issues": issues})
    unmatched_a = [{"number": p.number, "at": [p.x, p.y]}
                   for i, p in enumerate(pa) if i not in used_a]
    unmatched_b = [{"number": q.number, "at": [q.x, q.y]}
                   for j, q in enumerate(pb) if j not in used_b]
    na, nb = Counter(p.number for p in pa), Counter(q.number for q in pb)
    numbering = {"duplicates_a": {n: c for n, c in na.items() if c > 1 and n},
                 "duplicates_b": {n: c for n, c in nb.items() if c > 1 and n},
                 "only_in_a": sorted(set(na) - set(nb), key=_natural),
                 "only_in_b": sorted(set(nb) - set(na), key=_natural),
                 "renumbered": renumbered}
    identical = not (deviations or unmatched_a or unmatched_b or numbering["only_in_a"]
                     or numbering["only_in_b"]
                     or numbering["duplicates_a"] != numbering["duplicates_b"])
    dup_b = ", ".join(f'"{n}" x{c}' for n, c in numbering["duplicates_b"].items())
    text = (f"{len(deviations)} of {len(used_a)} matched pads differ "
            f"(max offset {max_offset:.3f} mm, {renumbered} renumbered); "
            f"{len(unmatched_a)} pads only in A, {len(unmatched_b)} only in B"
            + (f"; B repeats pad numbers {dup_b}" if dup_b else ""))
    return {"a": str(a_path), "b": str(b_path), "pads": [len(pa), len(pb)],
            "matched": len(used_a), "identical": identical, "tolerance": tolerance,
            "max_offset": _r6(max_offset), "deviations": deviations, "numbering": numbering,
            "unmatched_a": unmatched_a, "unmatched_b": unmatched_b,
            "summary": "identical" if identical else text}
