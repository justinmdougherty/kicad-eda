#!/usr/bin/env python3
"""Freerouting outline rewrite of KiCad's Specctra DSN export (F3-02).

Why (wave-1 T10 spike, verified): Freerouting keeps copper only its
clearance-class distance from the DSN ``(boundary ...)`` and from every hole
keepout, while KiCad's DRC wants the board's copper-to-edge clearance, which
is usually larger (0.3 mm against a 0.2 mm class). On a rounded board with a
slot and a 5 mm hole that left copper 0.21-0.27 mm from the hole on 4 of 5
builds. Deflating the boundary and inflating every Edge.Cuts hole keepout by

    delta = edge_clearance - min(untyped DSN clearance) + margin
          = 0.3 - 0.2 + 0.02 = 0.12 mm

(together with D7-19's class split) passed 5/5 on KiCad 9 and 10. The class
split (use_layer, plane nets) is NOT here: D7-19's dsn_rules owns it.

``rewrite_outline(dsn_text, outline, origin_mm, edge_clearance_mm)`` does the
deflate / inflate on the text KiCad exported and returns ``(new_text,
report)``. Every byte outside the rewritten lists is left as it was; the
s-expressions are read only through dsn_sexpr's span API (a regex stops at
the first '(' inside "Net-(D1-A)" and corrupts the file).

Frames: ``outline`` is an outline.Outline in the board frame (mm, Y down,
origin = bbox top-left); ``origin_mm`` = (ox, oy) is that origin in KiCad's
sheet frame (mm). A DSN coordinate is x = (bx + ox) * k, y = -(by + oy) * k
with k DSN units per mm from ``(unit ...)`` (KiCad: um, k = 1000), snapped to
the file's ``(resolution ...)`` (KiCad: um 10, i.e. 0.1 um).

Steps:
  1. delta as above; delta <= 0 returns the text unchanged with a reason.
  2. inner = outline.inset(outline, delta) (exact: the outer ring shrinks,
     holes grow, holes that meet merge, a hole that meets the edge becomes a
     bay). Its errors ("consumes the whole board", "splits the board into N
     separate regions", "doubles back on itself", "is degenerate") raise
     DsnOutlineError(kind='inset'); one starting "internal error" is a bug in
     outline_inset and is re-raised unchanged.
  3. inner is flattened (``max_error`` chords, vertices on the exact rings).
  4. The bbox of the boundary already in the file must equal the original
     outline's (within FRAME_TOL_MM + PRINT_TOL_MM), else DsnOutlineError
     (kind='frame', 'frame mismatch ...'): nothing is rewritten blindly. A
     boundary whose bbox already equals the one this module would write is
     'already rewritten' (a no-op), so a second call never shrinks again.
  5. The boundary ``(path ...)`` list is replaced (layer and width kept).
  6. Each hole keepout KiCad wrote, ``(keepout "" (polygon signal 0 ...))``,
     is matched one to one to the outline cutout whose centroid is nearest
     (within MATCH_TOL_MM) and gets the grown hole around that cutout. Where
     growing merged several cutouts into one hole, the first of their
     keepouts (text order) gets the merged ring and the rest are deleted.
     Unmatched keepouts, and keepouts of cutouts that grew into the board
     edge (the deflated boundary already excludes them), stay as written. A
     grown hole with no keepout gets none added. All of it is reported.

Written rings follow KiCad's form: closed (first point repeated), the
boundary clockwise and holes counter-clockwise in DSN coordinates (what the
Y flip of the Outline's orientation gives), wrapped like KiCad's
PATH::Format, with the file's own newline (KiCad 9 on Windows writes CRLF).

Report: {changed, reason, delta, min_clearance_mm, edge_clearance_mm,
boundary_points (points written, closing point included), holes_replaced,
holes_unmatched, keepouts_deleted, warnings}.

Errors: DsnOutlineError (a ValueError) with ``kind`` 'inset' (the caller may
keep KiCad's raw export; G4 still checks copper containment), 'frame' (a
caller bug) or 'structure' (no boundary, no untyped clearance, a malformed
path). Malformed s-expressions raise dsn_sexpr.DsnError unchanged.

Stdlib only and Python 3.11 compatible: swig_export_dsn.py runs it under
KiCad 9's bundled Python (server/tools is its sys.path[0]).
"""
from __future__ import annotations

import math
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from dsn_sexpr import ATOM, Node, Token, child, find_all, parse_nodes, replace_spans  # noqa: E402
from outline import Circle, Outline, OutlineError, bbox, contains, flatten, inset  # noqa: E402

Point = tuple[float, float]
Box = tuple[float, float, float, float]          # x0, y0, x1, y1
Edit = tuple[int, int, str]

MARGIN_MM = 0.02           # added to delta (the T10 spike's margin)
MAX_ERROR_MM = 0.005       # chord error of the written polylines (KiCad's default arc error)
MATCH_TOL_MM = 0.5         # a hole keepout belongs to a cutout whose centroid is this close
FRAME_TOL_MM = 0.005       # existing boundary bbox vs the outline's: KiCad's arc error ...
PRINT_TOL_MM = 0.001       # ... plus KiCad's %.6g print step (1 um at 100-999 mm)
REWRITTEN_TOL_MM = 0.001   # existing bbox vs the one written here (at most delta / 2)
DEFAULT_STEP_MM = 1e-4     # 0.1 um when the file declares no (resolution ...)
WRAP_COLUMN = 70           # KiCad's PATH::Format wraps past this column ...
WRAP_INDENT = 12           # ... onto a line indented 6 nest levels
HOLE_LAYER = "signal"      # KiCad: each Edge.Cuts hole is (keepout "" (polygon signal 0 ...))
INSET, FRAME, STRUCTURE = "inset", "frame", "structure"
_SHAPES = frozenset({"path", "polygon"})
_UNITS_PER_MM = {"um": 1000.0, "mm": 1.0, "cm": 0.1, "mil": 1.0 / 0.0254, "inch": 1.0 / 25.4}
_SPACE = " \t\r\n\f\v"


class DsnOutlineError(ValueError):
    """The outline rewrite cannot be done; ``kind`` is 'inset' (the outline
    cannot be shrunk by delta), 'frame' (the DSN boundary does not match the
    outline at origin_mm) or 'structure' (the DSN lacks what the rewrite
    needs)."""

    def __init__(self, message: str, kind: str) -> None:
        super().__init__(message)
        self.kind = kind

    def __reduce__(self) -> tuple:
        return (type(self), (str(self), self.kind))


# ----------------------------------------------------------------------------
# Reading the DSN
# ----------------------------------------------------------------------------
def _describe(x: object) -> str:
    if isinstance(x, Node):
        return f"({x.name} ...)"
    return repr(x.value) if isinstance(x, Token) else repr(x)


def _number(x: object, what: str) -> float:
    if not isinstance(x, Token) or x.kind != ATOM:
        raise DsnOutlineError(f"{what}: expected a number, got {_describe(x)}", STRUCTURE)
    try:
        value = float(x.value)
    except ValueError:
        raise DsnOutlineError(f"{what}: {x.value!r} is not a number", STRUCTURE) from None
    if not math.isfinite(value):
        raise DsnOutlineError(f"{what}: {x.value!r} is not a finite number", STRUCTURE)
    return value


def _root(nodes: Node | Sequence[Node]) -> Node:
    if isinstance(nodes, Node):
        return nodes
    roots = [n for n in nodes if isinstance(n, Node) and n.name == "pcb"]
    if len(roots) != 1:
        raise DsnOutlineError(f"expected one top-level (pcb ...) list, found {len(roots)}",
                              STRUCTURE)
    return roots[0]


def _per_mm(x: object, what: str) -> float:
    name = x.value.lower() if isinstance(x, Token) else None
    if name not in _UNITS_PER_MM:
        raise DsnOutlineError(f"{what}: unknown unit {_describe(x)}; expected one of "
                              f"{', '.join(_UNITS_PER_MM)}", STRUCTURE)
    return _UNITS_PER_MM[name]


def _units(root: Node) -> tuple[float, float]:
    """(DSN units per mm, resolution step in mm) from ``(unit U)`` and
    ``(resolution U N)``; the unit defaults to the resolution's."""
    unit, res = child(root, "unit"), child(root, "resolution")
    res_per_mm = step_mm = None
    if res is not None:
        if len(res.items) != 2:
            raise DsnOutlineError("(resolution ...) must be (resolution unit N)", STRUCTURE)
        res_per_mm = _per_mm(res.items[0], "(resolution ...)")
        n = _number(res.items[1], "(resolution ...)")
        if n <= 0:
            raise DsnOutlineError(f"(resolution ...) N must be > 0, got {n:g}", STRUCTURE)
        step_mm = 1.0 / (res_per_mm * n)
    if unit is not None and unit.items:
        per_mm = _per_mm(unit.items[0], "(unit ...)")
    elif res_per_mm is not None:
        per_mm = res_per_mm
    else:
        raise DsnOutlineError("the DSN declares neither (unit ...) nor (resolution ...)",
                              STRUCTURE)
    return per_mm, DEFAULT_STEP_MM if step_mm is None else step_mm


def _plain_clearances(owner: Node, per_mm: float) -> list[float]:
    """mm of each untyped ``(rule (clearance N))`` directly in ``owner``. A
    typed one, ``(clearance 50 (type smd_smd))``, holds between particular
    objects (pad to pad), never against the boundary: it does not count."""
    found = []
    for rule in find_all(owner, "rule"):
        for c in find_all(rule, "clearance"):
            if any(isinstance(x, Node) for x in c.items):
                continue
            if len(c.items) != 1:
                raise DsnOutlineError(f"({owner.name} ... (rule (clearance ...))) must hold one "
                                      f"number, got {len(c.items)} items", STRUCTURE)
            found.append(_number(c.items[0], f"({owner.name} ... (rule (clearance N)))") / per_mm)
    return found


def min_class_clearance(nodes: Node | Sequence[Node]) -> float:
    """The smallest untyped rule clearance of a DSN, in mm: over the
    structure-level ``(rule ...)`` and the rule of every ``(class ...)`` and
    ``(net ...)`` of the network, in the file's ``(unit ...)``. ``nodes`` is
    parse_nodes(text) or its (pcb ...) Node. Raises DsnOutlineError
    ('structure') when there is no untyped clearance at all."""
    root = _root(nodes)
    per_mm = _units(root)[0]
    owners = [n for n in (child(root, "structure"),) if n is not None]
    network = child(root, "network")
    if network is not None:
        owners += find_all(network, "class") + find_all(network, "net")
    found = [v for owner in owners for v in _plain_clearances(owner, per_mm)]
    if not found:
        raise DsnOutlineError("the DSN has no untyped (rule (clearance N)) in its structure, "
                              "classes or nets", STRUCTURE)
    return min(found)


@dataclass(frozen=True)
class _Shape:
    """A ``(path|polygon layer width x1 y1 x2 y2 ...)`` list as written."""
    node: Node
    layer: str
    width: str
    points: tuple[Point, ...]          # DSN units

    @property
    def head(self) -> str:
        return f"({self.node.name} {self.layer} {self.width}"


def _shape(node: Node, what: str) -> _Shape:
    items = node.items
    if len(items) < 8 or len(items) % 2 or any(isinstance(x, Node) for x in items):
        raise DsnOutlineError(f"{what}: expected ({node.name} layer width x1 y1 x2 y2 x3 y3 "
                              f"...), got {len(items)} items", STRUCTURE)
    _number(items[1], f"{what} width")
    nums = [_number(x, what) for x in items[2:]]
    return _Shape(node, items[0].value, items[1].value,
                  tuple(zip(nums[::2], nums[1::2], strict=True)))


def _boundary(structure: Node) -> _Shape:
    found = [x for b in find_all(structure, "boundary") for x in b.items]
    if len(found) != 1 or not isinstance(found[0], Node) or found[0].name not in _SHAPES:
        listed = ", ".join(_describe(x) for x in found) or "nothing"
        raise DsnOutlineError(f"expected one (boundary (path ...)) in the structure, found "
                              f"{listed}: a multi-outline or (rect ...) boundary is not "
                              "rewritten", STRUCTURE)
    return _shape(found[0], "(boundary (path ...))")


def _hole_keepouts(structure: Node) -> list[tuple[Node, _Shape]]:
    """The structure's Edge.Cuts hole keepouts in text order (KiCad writes
    each as ``(keepout "" (polygon signal 0 ...))``); rule-area keepouts name
    copper layers and are not touched."""
    out = []
    for k in find_all(structure, "keepout"):
        shape = next((x for x in k.items if isinstance(x, Node) and x.name in _SHAPES), None)
        if shape is not None and shape.items and isinstance(shape.items[0], Token) \
                and shape.items[0].value == HOLE_LAYER:
            out.append((k, _shape(shape, "(keepout (polygon signal ...))")))
    return out


# ----------------------------------------------------------------------------
# Frames and formatting
# ----------------------------------------------------------------------------
@dataclass(frozen=True)
class _Frame:
    """Board-frame mm <-> DSN units: x = (bx + ox) * per_mm and
    y = -(by + oy) * per_mm, written snapped to the file's resolution."""
    per_mm: float
    step_mm: float
    ox: float
    oy: float

    @property
    def step(self) -> float:
        return self.step_mm * self.per_mm

    def snap(self, v: float) -> float:
        return round(v / self.step) * self.step + 0.0      # + 0.0: no -0.0

    def to_dsn(self, p: Point) -> Point:
        return (self.snap((p[0] + self.ox) * self.per_mm),
                self.snap(-(p[1] + self.oy) * self.per_mm))

    def to_board(self, q: Point) -> Point:
        return (q[0] / self.per_mm - self.ox, -q[1] / self.per_mm - self.oy)

    def outline_box(self, o: Outline) -> Box:
        x0, y0, x1, y1 = bbox(o)
        k = self.per_mm
        return ((x0 + self.ox) * k, -(y1 + self.oy) * k, (x1 + self.ox) * k, -(y0 + self.oy) * k)

    def text(self, v: float) -> str:
        decimals = max(0, math.ceil(-math.log10(self.step) - 1e-9))
        s = f"{v:.{decimals}f}"
        if "." in s:
            s = s.rstrip("0").rstrip(".")
        return "0" if s == "-0" else s


def _dsn_ring(frame: _Frame, ring: Sequence[Point]) -> tuple[Point, ...]:
    """A flattened ring in DSN units: snapped, consecutive duplicates
    dropped, closed by repeating its first point (as KiCad writes it)."""
    pts: list[Point] = []
    for p in ring:
        q = frame.to_dsn(p)
        if not pts or q != pts[-1]:
            pts.append(q)
    while len(pts) > 1 and pts[-1] == pts[0]:
        pts.pop()
    return (*pts, pts[0])


def _format(text: str, shape: _Shape, pts: Sequence[Point], frame: _Frame) -> str:
    """``shape`` rewritten with ``pts``, wrapped like KiCad's PATH::Format,
    with the file's own newline (that of its first line)."""
    first = text.find("\n")
    newline = "\r\n" if first > 0 and text[first - 1] == "\r" else "\n"
    column = shape.node.start - (text.rfind("\n", 0, shape.node.start) + 1)
    parts, width = [shape.head], column + len(shape.head)
    for x, y in pts:
        if width > WRAP_COLUMN:
            parts.append(newline + " " * WRAP_INDENT)
            width = WRAP_INDENT
        else:
            parts.append("  ")
            width += 2
        word = f"{frame.text(x)} {frame.text(y)}"
        parts.append(word)
        width += len(word)
    return "".join(parts) + ")"


def _deletion(text: str, node: Node) -> Edit:
    """Removes ``node`` with the whitespace before it (its line break)."""
    start = node.start
    while start > 0 and text[start - 1] in _SPACE:
        start -= 1
    return (start, node.end, "")


def _box(pts: Sequence[Point]) -> Box:
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return (min(xs), min(ys), max(xs), max(ys))


def _gap_mm(a: Box, b: Box, per_mm: float) -> float:
    return max(abs(u - v) for u, v in zip(a, b, strict=True)) / per_mm


def _centroid(pts: Sequence[Point]) -> Point:
    """Area centroid of a closed polygon (a repeated closing point is fine);
    the vertex mean when it has no area."""
    a = cx = cy = 0.0
    for (x0, y0), (x1, y1) in zip(pts, (*pts[1:], pts[0]), strict=True):
        c = x0 * y1 - x1 * y0
        a, cx, cy = a + c, cx + (x0 + x1) * c, cy + (y0 + y1) * c
    if abs(a) < 1e-12:
        return (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))
    return (cx / (3.0 * a), cy / (3.0 * a))


def _fmt(p: Point) -> str:
    return f"({p[0]:.3f}, {p[1]:.3f}) mm"


# ----------------------------------------------------------------------------
# Inset and frame checks
# ----------------------------------------------------------------------------
def _inset(o: Outline, delta: float) -> Outline:
    try:
        return inset(o, delta)
    except OutlineError as e:
        if str(e).startswith("internal error"):
            raise
        raise DsnOutlineError(f"cannot deflate the DSN boundary by delta = {delta:.4f} mm: {e}",
                              INSET) from e


def _mismatch(have: Box, original: Box, frame: _Frame, delta: float) -> DsnOutlineError:
    def mm(b: Box) -> str:
        return "(" + ", ".join(f"{v / frame.per_mm:.4f}" for v in b) + ")"

    return DsnOutlineError(
        f"frame mismatch: the DSN boundary bbox {mm(have)} matches neither the outline's bbox "
        f"{mm(original)} at origin_mm ({frame.ox:g}, {frame.oy:g}) nor its inset by delta = "
        f"{delta:.4f} mm (DSN frame, mm, tolerance {FRAME_TOL_MM + PRINT_TOL_MM:g} mm): a wrong "
        "origin_mm, another board's outline, or a boundary rewritten with another delta; the "
        "DSN is not rewritten blindly", FRAME)


def _inset_in_frame(o: Outline, delta: float, have: Box, frame: _Frame) -> Outline:
    """The inset; when it fails, a wrong frame is reported first (a caller
    bug must never hide behind an inset error)."""
    try:
        return _inset(o, delta)
    except DsnOutlineError:
        original = frame.outline_box(o)
        if _gap_mm(have, original, frame.per_mm) > FRAME_TOL_MM + PRINT_TOL_MM:
            raise _mismatch(have, original, frame, delta) from None
        raise


# ----------------------------------------------------------------------------
# Hole keepouts
# ----------------------------------------------------------------------------
def _match(keepouts: Sequence[Point], cutouts: Sequence[Point]) -> dict[int, int]:
    """keepout index -> cutout index, one to one, nearest centroids first,
    each pair within MATCH_TOL_MM."""
    pairs = sorted((math.dist(k, c), i, j) for i, k in enumerate(keepouts)
                   for j, c in enumerate(cutouts) if math.dist(k, c) <= MATCH_TOL_MM)
    out: dict[int, int] = {}
    for _d, i, j in pairs:
        if i not in out and j not in out.values():
            out[i] = j
    return out


def _hosts(o: Outline, inner: Outline) -> list[int | None]:
    """For each cutout of ``o``, the index of the grown hole of ``inner``
    around it; None when it grew into the board edge (a bay of the outer
    ring). A point on the cutout lies delta inside its grown hole."""
    holes = [Outline(h) for h in inner.cutouts]
    found: list[int | None] = []
    for c in o.cutouts:
        p = (c.cx + c.r, c.cy) if isinstance(c, Circle) else c.vertices[0][:2]
        found.append(next((h for h, hole in enumerate(holes) if contains(hole, p)), None))
    return found


def _assign(keepouts: list[tuple[Node, _Shape]], o: Outline, host: Sequence[int | None],
            frame: _Frame, max_error: float) -> tuple[dict[int, list[int]], list[str]]:
    """{grown hole -> its keepouts in text order} and warnings for the
    keepouts left as written; ``host`` is _hosts(o, inner)."""
    centroids = [_centroid([frame.to_board(q) for q in s.points]) for _k, s in keepouts]
    match = _match(centroids, [_centroid(r) for r in flatten(o, max_error)[1:]])
    groups: dict[int, list[int]] = {}
    warnings = []
    for i, c in enumerate(centroids):
        j = match.get(i)
        if j is None:
            warnings.append(f"hole keepout #{i + 1} at {_fmt(c)} (board frame) matches no "
                            f"cutout within {MATCH_TOL_MM:g} mm: left as exported")
        elif host[j] is None:
            warnings.append(f"hole keepout #{i + 1} (cutout {j + 1}) grew into the board edge, "
                            "which the deflated boundary already excludes: left as exported")
        else:
            groups.setdefault(host[j], []).append(i)
    return groups, warnings


def _merge_notes(host: Sequence[int | None], holes: int,
                 groups: dict[int, list[int]]) -> list[str]:
    """What happened to each of the ``holes`` grown holes: merged cutouts
    (and which keepout got the ring) and holes the DSN has no keepout for."""
    hosted: dict[int, list[int]] = {}
    for j, h in enumerate(host):
        if h is not None:
            hosted.setdefault(h, []).append(j + 1)
    notes = []
    for h in range(holes):
        members, cuts = groups.get(h, []), hosted.get(h, [])
        if len(cuts) > 1:
            kept = (f"; keepout #{members[0] + 1} gets the merged ring" if members else "")
            gone = (f", keepout(s) {', '.join(f'#{i + 1}' for i in members[1:])} deleted"
                    if len(members) > 1 else "")
            notes.append(f"cutouts {', '.join(map(str, cuts))} merged into one hole when grown"
                         f"{kept}{gone}")
        if not members:
            notes.append(f"grown hole {h + 1} (cutouts {', '.join(map(str, cuts)) or 'none'}) "
                         "has no keepout in the DSN: none added")
    return notes


def _hole_edits(text: str, structure: Node, o: Outline, inner: Outline,
                rings: Sequence[Sequence[Point]], frame: _Frame,
                max_error: float) -> tuple[list[Edit], dict]:
    keepouts = _hole_keepouts(structure)
    host = _hosts(o, inner)
    groups, warnings = _assign(keepouts, o, host, frame, max_error)
    edits: list[Edit] = []
    for h, members in sorted(groups.items()):
        shape = keepouts[members[0]][1]
        edits.append((shape.node.start, shape.node.end, _format(text, shape, rings[h + 1], frame)))
        edits += [_deletion(text, keepouts[i][0]) for i in members[1:]]
    deleted = sum(len(m) - 1 for m in groups.values())
    return edits, {"holes_replaced": len(groups), "keepouts_deleted": deleted,
                   "holes_unmatched": len(keepouts) - len(groups) - deleted,
                   "warnings": [*_merge_notes(host, len(inner.cutouts), groups), *warnings]}


# ----------------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------------
def _finite(v: object, what: str) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        raise ValueError(f"{what} must be a finite number, got {v!r}")
    return float(v)


def _check_args(outline: object, origin_mm: object, edge_clearance_mm: object,
                margin: object, max_error: object) -> Point:
    if not isinstance(outline, Outline):
        raise TypeError(f"outline must be an outline.Outline, got {type(outline).__name__}")
    if not isinstance(origin_mm, (list, tuple)) or len(origin_mm) != 2:
        raise ValueError(f"origin_mm must be (x, y) in sheet mm, got {origin_mm!r}")
    if _finite(edge_clearance_mm, "edge_clearance_mm") < 0:
        raise ValueError(f"edge_clearance_mm must be >= 0, got {edge_clearance_mm!r}")
    if _finite(margin, "margin") < 0:
        raise ValueError(f"margin must be >= 0, got {margin!r}")
    if not _finite(max_error, "max_error") > 0:
        raise ValueError(f"max_error must be > 0 mm, got {max_error!r}")
    return (_finite(origin_mm[0], "origin_mm x"), _finite(origin_mm[1], "origin_mm y"))


def _report(delta: float, clearance: float, edge: float) -> dict:
    return {"changed": False, "reason": None, "delta": round(delta, 9),
            "min_clearance_mm": round(clearance, 9), "edge_clearance_mm": edge,
            "boundary_points": 0, "holes_replaced": 0, "holes_unmatched": 0,
            "keepouts_deleted": 0, "warnings": []}


def rewrite_outline(dsn_text: str, outline: Outline, origin_mm: Sequence[float],
                    edge_clearance_mm: float, margin: float = MARGIN_MM,
                    max_error: float = MAX_ERROR_MM) -> tuple[str, dict]:
    """``dsn_text`` with its boundary deflated and its hole keepouts inflated
    by delta = edge_clearance_mm - min_class_clearance + margin, and the
    report (see the module doc). The text comes back unchanged (the same
    object) when delta <= 0 or the boundary is already rewritten."""
    ox, oy = _check_args(outline, origin_mm, edge_clearance_mm, margin, max_error)
    root = _root(parse_nodes(dsn_text))
    clearance = min_class_clearance(root)
    delta = edge_clearance_mm - clearance + margin
    report = _report(delta, clearance, float(edge_clearance_mm))
    if delta <= 0:
        return dsn_text, {**report, "reason": (
            f"delta = {delta:.4f} mm <= 0: the edge clearance {edge_clearance_mm:g} mm does not "
            f"exceed the smallest DSN clearance {clearance:g} mm minus the margin {margin:g} mm")}
    structure = child(root, "structure")
    if structure is None:
        raise DsnOutlineError("the DSN has no (structure ...)", STRUCTURE)
    boundary = _boundary(structure)
    frame = _Frame(*_units(root), ox, oy)
    have = _box(boundary.points)
    inner = _inset_in_frame(outline, delta, have, frame)
    rings = [_dsn_ring(frame, r) for r in flatten(inner, max_error)]
    if _gap_mm(have, _box(rings[0]), frame.per_mm) <= min(REWRITTEN_TOL_MM, delta / 2):
        return dsn_text, {**report, "reason": "already rewritten"}
    original = frame.outline_box(outline)
    if _gap_mm(have, original, frame.per_mm) > FRAME_TOL_MM + PRINT_TOL_MM:
        raise _mismatch(have, original, frame, delta)
    edits, holes = _hole_edits(dsn_text, structure, outline, inner, rings, frame, max_error)
    node = boundary.node
    edits.append((node.start, node.end, _format(dsn_text, boundary, rings[0], frame)))
    return replace_spans(dsn_text, edits), {**report, **holes, "changed": True,
                                            "boundary_points": len(rings[0])}
