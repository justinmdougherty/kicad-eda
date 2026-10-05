#!/usr/bin/env python3
"""Read KiCad footprints (.kicad_mod) with the stdlib (F2, ROADMAP D1).

``parse_sexpr`` turns s-expression text into nested lists of string atoms
(quotes removed, escapes resolved). ``read_footprint`` / ``parse_footprint``
return a ParsedFootprint: name, format version, generator, attributes, the
pads (ParsedPad: number, type, shape, position, size, rotation, drill,
layers) and every graphic stroke as straight segments per layer (arcs and
circles flattened), which is what fp_author's geometry checks and
fp_compare's pad comparison need. fp_author re-exports these names.

Units: mm, footprint frame (+Y down). Stdlib-only (Python 3.11), so code that
runs in KiCad's bundled Python may import it.
"""
from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

Point = tuple[float, float]

_TOKEN = re.compile(r'\s*(?:(\()|(\))|"((?:[^"\\]|\\.)*)"|([^\s()"]+))', re.S)
_NUMBER = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")
_ESCAPE = re.compile(r"\\(.)")


class FootprintParseError(ValueError):
    """A .kicad_mod file could not be read as a KiCad footprint."""


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
            stack[-1].append(_ESCAPE.sub(lambda e: "\n" if e.group(1) == "n" else e.group(1),
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


def _point(node: list | None) -> Point:
    x, y = _floats(node, 2)
    return (x, y)


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


def _outline_points(node: list) -> list[Point]:
    """The closed or open polyline an fp_rect / fp_poly / fp_circle / fp_arc draws."""
    tag = node[0]
    if tag == "fp_rect":
        (x0, y0), (x1, y1) = _point(_first(node, "start")), _point(_first(node, "end"))
        return [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]
    if tag == "fp_poly":
        pts = [_point(xy) for xy in _kids(_first(node, "pts") or [], "xy")]
        return pts + pts[:1]
    if tag == "fp_circle":
        (cx, cy), (ex, ey) = _point(_first(node, "center")), _point(_first(node, "end"))
        r = math.hypot(ex - cx, ey - cy)
        return [(cx + r * math.cos(math.pi * i / 12), cy + r * math.sin(math.pi * i / 12))
                for i in range(25)]
    s, m, e = (_point(_first(node, k)) for k in ("start", "mid", "end"))
    return _arc_points(s, m, e)


def _graphic_segments(node: list) -> list[tuple[Point, Point]]:
    if node[0] == "fp_line":
        return [(_point(_first(node, "start")), _point(_first(node, "end")))]
    if node[0] in ("fp_rect", "fp_poly", "fp_circle", "fp_arc"):
        pts = _outline_points(node)
        return list(zip(pts, pts[1:], strict=False))
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


def _strokes(root: list) -> dict[str, tuple[tuple[Point, Point, float], ...]]:
    """{layer: ((a, b, stroke width), ...)} of every fp_* graphic."""
    strokes: dict[str, list] = {}
    for node in root:
        if isinstance(node, list) and node and str(node[0]).startswith("fp_"):
            layer_node = _first(node, "layer")
            width_node = _first(_first(node, "stroke") or [], "width") or _first(node, "width")
            width = _floats(width_node, 1)[0]
            for a, b in _graphic_segments(node):
                strokes.setdefault(layer_node[1] if layer_node else "", []).append((a, b, width))
    return {k: tuple(v) for k, v in strokes.items()}


def parse_footprint(text: str) -> ParsedFootprint:
    """A ParsedFootprint from .kicad_mod text (FootprintParseError when it is not one)."""
    root = parse_sexpr(text)
    if not root or root[0] not in ("footprint", "module") or len(root) < 2:
        raise FootprintParseError(f"not a footprint: top-level ({root[0] if root else ''} ...)")
    version_node, generator_node = _first(root, "version"), _first(root, "generator")
    try:
        version = int(version_node[1]) if version_node else None
    except (IndexError, ValueError) as e:
        raise FootprintParseError(f"unreadable (version ...): {version_node}") from e
    return ParsedFootprint(name=str(root[1]), version=version,
                           generator=str(generator_node[1]) if generator_node else None,
                           attr=tuple(str(a) for a in (_first(root, "attr") or [])[1:]),
                           pads=tuple(_parse_pad(p) for p in _kids(root, "pad")),
                           strokes=_strokes(root))


def read_footprint(path: str | os.PathLike) -> ParsedFootprint:
    """parse_footprint of a file (FootprintParseError when it cannot be read)."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        raise FootprintParseError(f"cannot read {path}: {e}") from e
    return parse_footprint(text)
