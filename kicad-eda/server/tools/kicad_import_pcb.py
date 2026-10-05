#!/usr/bin/env python3
"""Board side of the KiCad project importer (ROADMAP_DECISIONS D4).

Reads a ``.kicad_pcb`` as text, no KiCad needed, any format from KiCad 5 to
11, into a frozen ``BoardDesign``: every footprint (reference, library id,
origin + rotation as KiCad displays it, side, attributes, value, numbered pads
with their nets), the copper layer count, the aux (drill/place) origin and the
Edge.Cuts primitives of the board AND of every footprint (USB slots,
mouse-bites), the latter moved into the board's sheet frame.

The pad numbering and net resolution are sexpr_read's helpers, the ones
``net_equivalence.pcb_pad_nets`` uses, so a pad key here is exactly the key
pcb_pad_nets produces: the converted netlist and the source board compare one
to one. KiCad 4/5 bare tokens keep their text as KiCad reads them (pad
``01``, reference ``007``: sexpr_read.quote_bare_keys).

Coordinates stay in KiCad's sheet frame (mm, Y down). Footprint children are
stored footprint-local and unrotated (a B-side footprint's children are
stored already mirrored), so ``to_sheet`` is a rotation by the footprint's
orientation plus a translation, whatever the side.

Only ``sexpdata`` (through sexpr_read) is needed, lazily. Extracting the
board's footprints as a library is kicad_import_fplib.py.
"""
from __future__ import annotations

import math
import re
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import net_equivalence as ne  # noqa: E402
import outline  # noqa: E402
import sexpr_read as sr  # noqa: E402

FOOTPRINT_HEADS = frozenset({"footprint", "module"})
EDGE_LAYER = "Edge.Cuts"
# board-level drawings and their footprint-level twins (KiCad 6+ "fp_*")
_SHAPES = {"gr_line": "line", "gr_arc": "arc", "gr_circle": "circle", "gr_rect": "rect",
           "gr_poly": "poly", "gr_curve": "curve",
           "fp_line": "line", "fp_arc": "arc", "fp_circle": "circle", "fp_rect": "rect",
           "fp_poly": "poly", "fp_curve": "curve"}
_TOP_KEEP = (FOOTPRINT_HEADS | {"layers", "setup", "net"}
             | {k for k in _SHAPES if k.startswith("gr_")})
_COPPER_TYPES = frozenset({"signal", "power", "mixed", "jumper"})
BEZIER_SEGMENTS = 16            # a gr_curve becomes this many chords (reported)


@dataclass(frozen=True)
class BoardPad:
    """A numbered copper pad: its number (pin number) and net (NO_NET = none)."""
    number: str
    net: str


@dataclass(frozen=True)
class BoardFootprint:
    """One placed footprint. ``x``/``y``: its origin in the sheet frame (mm);
    ``rot``: its orientation as KiCad displays it (degrees, [0, 360); for a
    B-side part the post-flip value KiCad stores); ``attrs``: the ``(attr)``
    tokens (smd, through_hole, board_only, dnp, exclude_from_bom, ...)."""
    index: int
    ref: str
    lib_id: str
    x: float
    y: float
    rot: float
    side: str
    value: str
    attrs: frozenset[str]
    pads: tuple[BoardPad, ...]
    uuid: str
    edge_cuts: tuple[Mapping[str, Any], ...] = ()

    @property
    def board_only(self) -> bool:
        return "board_only" in self.attrs

    @property
    def item_name(self) -> str:
        """The footprint name without its library nickname."""
        return self.lib_id.rpartition(":")[2]


@dataclass(frozen=True)
class BoardDesign:
    format_version: int | None
    generator_version: str | None
    copper_layers: tuple[str, ...]
    footprints: tuple[BoardFootprint, ...]
    edge_cuts: tuple[Mapping[str, Any], ...]      # board-level Edge.Cuts, sheet frame
    aux_origin: tuple[float, float] | None
    notes: tuple[str, ...] = field(default=())    # what the reader could not carry exactly


# ----------------------------------------------------------------------------
# Top-level scan
# ----------------------------------------------------------------------------
def _top_items(text: str) -> list[tuple[str, str]]:
    """(head, source) of every direct child of ``(kicad_pcb ...)`` whose head
    the importer reads; zone fills, tracks and the rest are skipped unparsed."""
    if sr.head_of(text) != "kicad_pcb":
        raise ValueError("not a KiCad board (expected '(kicad_pcb ...')")
    start = len(text) - len(text.lstrip(sr.LEADING))
    depth, opened, head = 0, 0, ""
    out: list[tuple[str, str]] = []
    for m in sr.TOKEN_RE.finditer(text, start):
        tok = m.group()
        if tok == "(":
            depth += 1
            if depth == 2:
                opened = m.start()
                found = sr.HEAD_RE.match(text, opened)
                head = found.group(1) if found else ""
        elif tok == ")":
            if depth == 0:
                raise ValueError(f"unbalanced parentheses: ')' at offset {m.start()} "
                                 "closes nothing")
            if depth == 2 and head in _TOP_KEEP:
                out.append((head, text[opened:m.end()]))
            depth -= 1
    if depth:
        raise ValueError("unbalanced parentheses: the board is truncated")
    return out


# ----------------------------------------------------------------------------
# Small readers
# ----------------------------------------------------------------------------
def _num(x: Any, where: str) -> float:
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        try:
            return float(sr.atom(x))
        except (TypeError, ValueError) as e:
            raise ValueError(f"{where}: {x!r} is not a number") from e
    return float(x)


def _xy(node: list | None, where: str) -> tuple[float, float]:
    if node is None or len(node) < 3:
        raise ValueError(f"{where}: missing coordinates")
    return (_num(node[1], where), _num(node[2], where))


def _norm_deg(rot: float) -> float:
    out = round(rot % 360.0, 6)
    return 0.0 if out in (0.0, 360.0) else out


def _placement(node: list, where: str) -> tuple[float, float, float, list[str]]:
    """(x, y, rot, notes) of a footprint: ``(at x y [rot])`` (KiCad <= 10) or
    ``(transform (translate x y) (rotate r) (scale sx sy))`` (KiCad 11)."""
    at = sr.child(node, "at")
    if at is not None:
        x, y = _xy(at, f"{where} at")
        return x, y, _num(at[3], where) if len(at) > 3 else 0.0, []
    tf = sr.child(node, "transform")
    if tf is None:
        raise ValueError(f"{where}: no (at ...) or (transform ...) placement")
    x, y = _xy(sr.child(tf, "translate"), f"{where} translate")
    rotate = sr.child(tf, "rotate")
    rot = _num(rotate[1], where) if rotate is not None and len(rotate) > 1 else 0.0
    notes = []
    scale = sr.child(tf, "scale")
    if scale is not None and any(abs(_num(v, where) - 1.0) > 1e-9 for v in scale[1:]):
        notes.append(f"{where}: transform scale {[sr.atom(v) for v in scale[1:]]} ignored")
    return x, y, rot, notes


def _text_field(node: list, name: str) -> str:
    """A footprint field: KiCad 8+ ``(property "Name" "v")``, else <= 7
    ``(fp_text name v)``."""
    legacy = ""
    for child in node[1:]:
        tag = sr.tag(child)
        if tag == "property" and len(child) >= 3 and sr.atom(child[1]) in (name, name.lower()):
            return sr.atom(child[2])
        if tag == "fp_text" and len(child) >= 3 and sr.atom(child[1]) == name.lower():
            legacy = sr.atom(child[2])
    return legacy


def _layer(node: list) -> str:
    layer = sr.child(node, "layer")
    return sr.atom(layer[1]) if layer is not None and len(layer) > 1 else ""


# ----------------------------------------------------------------------------
# Edge.Cuts primitives
# ----------------------------------------------------------------------------
Transform = tuple[float, float, float]           # x, y, rot (deg) of a footprint origin


def to_sheet(p: tuple[float, float], tf: Transform | None) -> tuple[float, float]:
    """A footprint-local point in the sheet frame (KiCad's rotation sign:
    Y down, positive orientation turns counter-clockwise on screen)."""
    if tf is None:
        return p
    x0, y0, rot = tf
    a = math.radians(rot)
    c, s = math.cos(a), math.sin(a)
    return (x0 + p[0] * c + p[1] * s, y0 - p[0] * s + p[1] * c)


def _prim(kind: str, src: str, **pts: Any) -> dict[str, Any]:
    return {"kind": kind, "source": src, **pts}


def _bezier(pts: list[tuple[float, float]], n: int) -> list[tuple[float, float]]:
    (x0, y0), (x1, y1), (x2, y2), (x3, y3) = pts
    out = []
    for i in range(n + 1):
        t = i / n
        u = 1.0 - t
        out.append((u ** 3 * x0 + 3 * u * u * t * x1 + 3 * u * t * t * x2 + t ** 3 * x3,
                    u ** 3 * y0 + 3 * u * u * t * y1 + 3 * u * t * t * y2 + t ** 3 * y3))
    return out


def _poly_prims(node: list, src: str, tf: Transform | None, where: str) -> list[dict]:
    """A closed polygon (``pts`` of ``xy`` points and KiCad 9 ``arc`` runs)."""
    pts = sr.child(node, "pts")
    if pts is None:
        raise ValueError(f"{where}: polygon without pts")
    items: list[tuple[str, tuple]] = []
    for item in pts[1:]:
        tag = sr.tag(item)
        if tag == "xy":
            items.append(("xy", (to_sheet(_xy(item, where), tf),)))
        elif tag == "arc":
            s, m, e = (to_sheet(_xy(sr.child(item, k), where), tf)
                       for k in ("start", "mid", "end"))
            items.append(("arc", (s, m, e)))
    out: list[dict] = []
    if not items:
        return out
    first = cur = items[0][1][0]
    for tag, pts_ in items:
        start = pts_[0]
        if _differ(cur, start):
            out.append(_prim("line", src, start=cur, end=start))
        if tag == "arc":
            out.append(_prim("arc", src, start=start, mid=pts_[1], end=pts_[2]))
        cur = pts_[-1]
    if _differ(cur, first):
        out.append(_prim("line", src, start=cur, end=first))
    return out


def _differ(a: tuple[float, float], b: tuple[float, float], tol: float = 1e-9) -> bool:
    return abs(a[0] - b[0]) > tol or abs(a[1] - b[1]) > tol


def shape_prims(node: list, src: str, tf: Transform | None,
                notes: list[str]) -> list[dict]:
    """Edge.Cuts primitives of one drawing (sheet frame): lines, 3-point arcs
    and circles, the shapes outline.from_kicad_primitives chains. Rectangles
    and polygons become lines (+ arcs); a Bezier curve is chorded (noted)."""
    kind = _SHAPES[sr.tag(node)]
    where = f"{src} {sr.tag(node)}"
    pt = lambda key: to_sheet(_xy(sr.child(node, key), where), tf)    # noqa: E731
    if kind == "line":
        return [_prim("line", src, start=pt("start"), end=pt("end"))]
    if kind == "arc":
        if sr.child(node, "mid") is None:
            notes.append(f"{where}: a KiCad 5 arc (center + angle) is not supported; "
                         "open and re-save the board in KiCad 6+")
            return []
        return [_prim("arc", src, start=pt("start"), mid=pt("mid"), end=pt("end"))]
    if kind == "circle":
        c, e = pt("center"), pt("end")
        return [_prim("circle", src, center=c, radius=math.dist(c, e))]
    if kind == "rect":
        (x0, y0), (x1, y1) = (_xy(sr.child(node, k), where) for k in ("start", "end"))
        corners = [to_sheet(p, tf) for p in ((x0, y0), (x1, y0), (x1, y1), (x0, y1))]
        return [_prim("line", src, start=corners[i], end=corners[(i + 1) % 4]) for i in range(4)]
    if kind == "poly":
        return _poly_prims(node, src, tf, where)
    pts = sr.child(node, "pts")
    ctrl = [to_sheet(_xy(p, where), tf) for p in (pts or [])[1:] if sr.tag(p) == "xy"]
    if len(ctrl) != 4:
        notes.append(f"{where}: a Bezier curve needs 4 control points, has {len(ctrl)}")
        return []
    chords = _bezier(ctrl, BEZIER_SEGMENTS)
    notes.append(f"{where}: Bezier curve approximated by {BEZIER_SEGMENTS} chords")
    return [_prim("line", src, start=chords[i], end=chords[i + 1]) for i in range(BEZIER_SEGMENTS)]


def _edge_prims(nodes: Iterable[list], src: str, tf: Transform | None,
                notes: list[str]) -> list[dict]:
    out: list[dict] = []
    for node in nodes:
        if sr.tag(node) in _SHAPES and _layer(node) == EDGE_LAYER:
            out += shape_prims(node, src, tf, notes)
    return out


# ----------------------------------------------------------------------------
# Footprints
# ----------------------------------------------------------------------------
def _attrs(node: list) -> frozenset[str]:
    """``(attr smd board_only dnp ...)`` tokens; KiCad 5 ``(attr virtual)``
    means "not in schematic" too."""
    out: set[str] = set()
    for attr in sr.children(node, "attr"):
        out |= {str(t) for t in attr[1:] if not isinstance(t, list)}
    if "virtual" in out:
        out.add("board_only")
    return frozenset(out)


def _footprint(index: int, src: str, table: Mapping[int, str],
               notes: list[str]) -> BoardFootprint:
    node = sr.parse(sr.quote_bare_keys(src))            # KiCad 4/5 (pad 01 ...) stays "01"
    lib_id = sr.atom(node[1]) if len(node) > 1 and not isinstance(node[1], list) else ""
    ref = _text_field(node, "Reference")
    where = f"footprint {ref or '(no reference)'} ({lib_id})"
    x, y, rot, more = _placement(node, where)
    notes += more
    layer = _layer(node)
    pads: list[BoardPad] = []
    for pad in sr.children(node, "pad"):
        number = sr.pad_number(pad)
        if number:
            pads.append(BoardPad(number, sr.pad_net(pad, table, f"{where} pad {number}")))
    uuid = sr.value(node, "uuid") or sr.value(node, "tstamp")
    edges = _edge_prims(node[1:], ref or lib_id, (x, y, rot), notes)
    return BoardFootprint(index=index, ref=ref, lib_id=lib_id, x=x, y=y, rot=_norm_deg(rot),
                          side="B" if layer.startswith("B.") else "F",
                          value=_text_field(node, "Value"), attrs=_attrs(node),
                          pads=tuple(pads), uuid=uuid, edge_cuts=tuple(edges))


def _copper_layers(src: str) -> tuple[str, ...]:
    node = sr.parse(src)
    out = []
    for entry in node[1:]:
        if isinstance(entry, list) and len(entry) >= 3:
            name, kind = sr.atom(entry[1]), sr.atom(entry[2])
            if name.endswith(".Cu") and kind in _COPPER_TYPES:
                out.append(name)
    return tuple(out)


def _aux_origin(src: str) -> tuple[float, float] | None:
    node = sr.child(sr.parse(src), "aux_axis_origin")
    if node is None:
        return None
    x, y = _xy(node, "setup aux_axis_origin")
    return None if (x, y) == (0.0, 0.0) else (x, y)


_GENERATOR_VERSION = re.compile(r'\(\s*generator_version\s+"?([^\s")]+)"?\s*\)')


def read_board(text: str) -> BoardDesign:
    """Parse a .kicad_pcb (as text) into a BoardDesign. Fails loudly
    (ValueError) on a file that is not a board or is malformed."""
    text = sr.restore_teardrop_parens(text)
    items = _top_items(text)
    table = sr.net_table(src for head, src in items if head == "net")
    notes: list[str] = []
    footprints: list[BoardFootprint] = []
    edges: list[dict] = []
    copper: tuple[str, ...] = ()
    aux = None
    for head, src in items:
        if head in FOOTPRINT_HEADS:
            footprints.append(_footprint(len(footprints), src, table, notes))
        elif head == "layers":
            copper = _copper_layers(src)
        elif head == "setup":
            aux = _aux_origin(src)
        elif head in _SHAPES:
            edges += _edge_prims([sr.parse(src)], "board", None, notes)
    gen = _GENERATOR_VERSION.search(text, 0, 4096)
    return BoardDesign(format_version=ne.pcb_format_version(text),
                       generator_version=gen.group(1) if gen else None,
                       copper_layers=copper, footprints=tuple(footprints),
                       edge_cuts=tuple(edges), aux_origin=aux, notes=tuple(notes))


# ----------------------------------------------------------------------------
# The board frame (D2: mm, Y down, origin = the outline's bbox top-left)
# ----------------------------------------------------------------------------
FALLBACK_MARGIN_MM = 5.0            # around the footprint origins of a board without Edge.Cuts
_ROUND = 6                          # decimals of a board block (1 nm)


@dataclass(frozen=True)
class BoardFrame:
    """The netlist ``board`` block of a source board and where its frame
    origin sits in the sheet (``x0``, ``y0``: subtract them from a sheet
    coordinate). ``info`` is the report entry; ``problem`` says why the
    Edge.Cuts outline could not be used (None when it was, or is absent)."""
    block: Mapping[str, Any]
    x0: float
    y0: float
    info: Mapping[str, Any]
    problem: str | None = None


def round_mm(v: float) -> float:
    """A board-block coordinate: rounded to 1 nm, never -0.0."""
    out = round(v, _ROUND)
    return 0.0 if out == 0 else out


def _prim_points(prims: Iterable[Mapping[str, Any]]) -> list[tuple[float, float]]:
    pts: list[tuple[float, float]] = []
    for p in prims:
        if p["kind"] == "circle":
            (cx, cy), r = p["center"], p["radius"]
            pts += [(cx - r, cy - r), (cx + r, cy + r)]
        else:
            pts += [p[k] for k in ("start", "mid", "end") if p.get(k) is not None]
    return pts


def _rect_block(pts: list[tuple[float, float]], margin: float, datum: tuple[float, float]
                ) -> tuple[dict, float, float]:
    x0, y0 = min(x for x, _ in pts) - margin, min(y for _, y in pts) - margin
    x1, y1 = max(x for x, _ in pts) + margin, max(y for _, y in pts) + margin
    return ({"w": round_mm(x1 - x0), "h": round_mm(y1 - y0),
             "datum": [round_mm(datum[0] - x0), round_mm(datum[1] - y0)]}, x0, y0)


def board_frame(board: BoardDesign) -> BoardFrame:
    """The Edge.Cuts outline (board and footprint shapes, arcs exact) moved
    so its bbox top-left is (0, 0), as a netlist board block whose ``datum``
    is the source aux origin (else its coordinate origin) in that frame. An
    outline that does not chain into valid loops gives its bounding
    rectangle (``problem`` says why); a board without Edge.Cuts the footprint
    origins' extent plus FALLBACK_MARGIN_MM. Raises ValueError for a board
    with neither."""
    datum = board.aux_origin or (0.0, 0.0)
    prims = list(board.edge_cuts) + [e for fp in board.footprints for e in fp.edge_cuts]
    info: dict[str, Any] = {
        "datum_source": "aux_axis_origin" if board.aux_origin else "file_origin",
        "footprint_edges": [fp.ref for fp in board.footprints if fp.edge_cuts]}
    problem = None
    if prims:
        try:
            o = outline.from_kicad_primitives(prims)
            x0, y0, _x1, _y1 = outline.bbox(o)
            moved = outline.translate(outline.Outline(o.outer, o.cutouts, datum), -x0, -y0)
            block = outline.to_board_block(moved)
            outline.from_board_block(block)            # the block must read back
            info.update(kind="edge_cuts", cutouts=len(o.cutouts),
                        frame_origin=[round_mm(x0), round_mm(y0)])
            return BoardFrame(block, x0, y0, info)
        except outline.OutlineError as e:
            problem = str(e)
            block, x0, y0 = _rect_block(_prim_points(prims), 0.0, datum)
    else:
        extent = origin_extent(board)
        if extent is None:
            raise ValueError("the board has neither an Edge.Cuts outline nor footprints")
        block, x0, y0 = _rect_block([extent[:2], extent[2:]], FALLBACK_MARGIN_MM, datum)
    info.update(kind="fallback_rectangle", frame_origin=[round_mm(x0), round_mm(y0)])
    return BoardFrame(block, x0, y0, info, problem)


def origin_extent(board: BoardDesign) -> tuple[float, float, float, float] | None:
    """(x0, y0, x1, y1) of every footprint origin: the fallback frame of a
    board without Edge.Cuts."""
    pts = [(fp.x, fp.y) for fp in board.footprints]
    if not pts:
        return None
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return (min(xs), min(ys), max(xs), max(ys))
