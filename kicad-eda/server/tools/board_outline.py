#!/usr/bin/env python3
"""The one adapter between the stdlib outline model (outline.py) and pcbnew.

Runs under KiCad's bundled Python (3.11 on 9.0.4; python3 in the 10.0.5 image):
stdlib + pcbnew only. Without pcbnew (the dev venv) ``pcbnew`` is None and only
the pure helpers work. Frames: board = mm, Y down, origin = outline bbox
top-left (outline.py); sheet (pcbnew) coordinates = board + (ox, oy).

pcbnew 9.0.4 / 10.0.5 facts absorbed here (each probed on both):
  * GetBoardPolygonOutlines: 10.x has a required 2nd parameter
    aInferOutlineIfNecessary, 9.x reads a positional 2nd argument as
    aErrorHandler: keyword or nothing. Success is the returned bool ONLY
    (9.0.4 fills a bbox fallback when it returns False).
  * KiCad closes Edge.Cuts gaps up to 10000 nm and fails from 10001 nm (9.0.4
    is not deterministic AT 10000 nm). draw_outline gives meeting primitives the
    identical VECTOR2I (per-call nm cache); outline_from_board chains at 1 um.
  * Zones are filled IN PLACE (zone.Outline()): SetOutline with a Python
    SHAPE_POLY_SET saves zero area on 9.0.4 and segfaults in Area() on 10.0.5.
  * Rule-area pour flag: CopperPour on 9.x, ZoneFills on 10.x (set_no_pour).
  * Courtyard caches are EMPTY after LoadBoard on 9.0.4 (courtyard()).
  * LoadBoard returns None for a missing/unreadable file; passing that None on
    segfaults (load_board()).
"""
from __future__ import annotations

import math
import os
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import kicad_quiet  # noqa: E402,F401  (before pcbnew: no modal wx dialogs)

try:
    import pcbnew  # noqa: E402
except ImportError:          # the dev venv: pure helpers only
    pcbnew = None

import outline as ol  # noqa: E402

NM_PER_MM = 1_000_000
EDGE_WIDTH_MM = 0.1
AREA_ABS_TOL_MM2 = 0.05          # draw_outline: KiCad's area vs the model's
AREA_REL_TOL = 0.001
DEFLATE_MAX_ERROR_MM = 0.005     # plane_polyset corner rounding error
BEZIER_MAX_ERROR_MM = 0.005      # outline_from_board bezier flattening
_ZERO_MM = 1e-9                  # shorter edges are dropped from records
CLOSED_MSG = "Edge.Cuts does not form one closed outline"
_POUR_FLAG = ("DoNotAllowZoneFills", "DoNotAllowCopperPour")   # 10.x, 9.x

Point = tuple[float, float]


class OutlineBuildError(RuntimeError):
    """The board's Edge.Cuts (or a shape derived from it) cannot be used."""


def _pcb() -> Any:
    if pcbnew is None:
        raise RuntimeError("pcbnew is not importable: this helper runs under KiCad's "
                           "bundled Python")
    return pcbnew


# --- pure helpers -------------------------------------------------------------------------
def nm(v_mm: float) -> int:
    """mm -> nm rounded half away from zero (KiROUND). pcbnew.FromMM truncates,
    so 169.18499999999997 mm would become 169184999 nm."""
    scaled = abs(float(v_mm)) * NM_PER_MM
    return int(math.copysign(math.floor(scaled + 0.5), v_mm))


def make_snapper(factory: Callable[[int, int], Any] | None = None
                 ) -> Callable[[float, float], Any]:
    """A per-call point cache: snap(x_mm, y_mm) returns ONE object per nm-rounded
    point (factory(x_nm, y_nm), default pcbnew.VECTOR2I), so primitives that
    meet share the identical end point."""
    make = factory if factory is not None else _pcb().VECTOR2I
    cache: dict[tuple[int, int], Any] = {}

    def snap(x_mm: float, y_mm: float) -> Any:
        key = (nm(x_mm), nm(y_mm))
        if key not in cache:
            cache[key] = make(*key)
        return cache[key]

    return snap


def area_matches(model_mm2: float, kicad_mm2: float) -> bool:
    """KiCad's polygon area agrees with the model's within 0.05 mm^2 + 0.1 %."""
    return abs(kicad_mm2 - model_mm2) <= AREA_ABS_TOL_MM2 + AREA_REL_TOL * abs(model_mm2)


def infer_keyword(doc: str | None) -> bool:
    """Whether a GetBoardPolygonOutlines signature takes aInferOutlineIfNecessary
    (present on 10.0.5, absent on 9.0.4)."""
    return "aInferOutlineIfNecessary" in (doc or "")


# --- records -> from_kicad_primitives input (pure) -------------------------------------------
def _pair(v: object, n: int, key: str) -> Point:
    if not isinstance(v, (list, tuple)) or len(v) != 2:
        raise ol.OutlineError(f"record {n} {key} must be [x, y], got {v!r}")
    return (_num(v[0], n, key), _num(v[1], n, key))


def _xy(rec: Mapping, key: str, n: int) -> Point:
    return _pair(rec.get(key), n, key)


def _num(v: object, n: int, key: str) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        raise ol.OutlineError(f"record {n} {key} must be a finite number, got {v!r}")
    return float(v)


def _points(rec: Mapping, n: int, least: int) -> list[Point]:
    raw = rec.get("points")
    if not isinstance(raw, (list, tuple)):
        raise ol.OutlineError(f"record {n} points must be a list of [x, y], got {raw!r}")
    pts = [_pair(p, n, "points") for p in raw]
    if len(pts) < least:
        raise ol.OutlineError(f"record {n} ({rec.get('shape')}) needs at least {least} points, "
                              f"got {len(pts)}")
    return pts


def _line(a: Point, b: Point) -> list[dict]:
    return [] if ol.dist(a, b) <= _ZERO_MM else [{"kind": "line", "start": a, "end": b}]


def _polyline(pts: Sequence[Point]) -> list[dict]:
    return [p for a, b in zip(pts[:-1], pts[1:], strict=True) for p in _line(a, b)]


def _loop_prims(loop: ol.Loop) -> list[dict]:
    v, n, out = loop.vertices, len(loop.vertices), []
    for i in range(n):
        s, e = v[i][:2], v[(i + 1) % n][:2]
        prim = ol.prim_from_bulge(s, e, v[i][2])
        out.append({"kind": "line", "start": s, "end": e} if isinstance(prim, ol.Line)
                   else {"kind": "arc", "start": s, "mid": prim.mid, "end": e})
    return out


def _rect(rec: Mapping, n: int) -> list[dict]:
    (ax, ay), (bx, by) = _xy(rec, "start", n), _xy(rec, "end", n)
    x0, y0, w, h = min(ax, bx), min(ay, by), abs(bx - ax), abs(by - ay)
    if w <= _ZERO_MM or h <= _ZERO_MM:
        raise ol.OutlineError(f"record {n} rect has zero width or height")
    r = min(_num(rec.get("corner_radius") or 0.0, n, "corner_radius"), w / 2.0, h / 2.0)
    return _loop_prims(ol.rect_loop(w, h, max(r, 0.0), x0, y0))


def _poly(rec: Mapping, n: int) -> list[dict]:
    pts = _points(rec, n, 3)
    if ol.dist(pts[0], pts[-1]) <= _ZERO_MM:
        pts = pts[:-1]
    return _polyline([*pts, pts[0]])


_CONVERTERS: dict[str, Callable[[Mapping, int], list[dict]]] = {
    "segment": lambda r, n: _line(_xy(r, "start", n), _xy(r, "end", n)),
    "arc": lambda r, n: [{"kind": "arc", "start": _xy(r, "start", n), "mid": _xy(r, "mid", n),
                          "end": _xy(r, "end", n)}],
    "circle": lambda r, n: [{"kind": "circle", "center": _xy(r, "center", n),
                             "radius": _num(r.get("radius"), n, "radius")}],
    "rect": _rect,
    "poly": _poly,
    "bezier": lambda r, n: _polyline(_points(r, n, 2)),
}


def shape_records_to_prims(records: Iterable[Mapping]) -> list[dict]:
    """Plain shape records {shape: segment|arc|circle|rect|poly|bezier, start,
    end, mid, center, radius, points, corner_radius} (mm) -> the input of
    outline.from_kicad_primitives. rect = 4 lines (+ 4 arcs when
    corner_radius > 0, clamped to half the short side); poly = closed lines;
    bezier = lines through its points. Zero-length edges are dropped."""
    out: list[dict] = []
    for n, rec in enumerate(records):
        conv = _CONVERTERS.get(rec.get("shape")) if isinstance(rec, Mapping) else None
        if conv is None:
            raise ol.OutlineError(f"record {n} must be an object with a 'shape' key in "
                                  f"{sorted(_CONVERTERS)}, got {rec!r}")
        out.extend(conv(rec, n))
    return out


# --- Edge.Cuts out of and into a board ------------------------------------------------------
def polygon_outlines(board: Any) -> Any:
    """KiCad's own Edge.Cuts polygon (a new SHAPE_POLY_SET). Never infers an
    outline; raises OutlineBuildError when pcbnew reports failure."""
    pcb = _pcb()
    sp = pcb.SHAPE_POLY_SET()
    forms = [lambda: board.GetBoardPolygonOutlines(sp, aInferOutlineIfNecessary=False),
             lambda: board.GetBoardPolygonOutlines(sp)]
    method = getattr(pcb.BOARD, "GetBoardPolygonOutlines", None)
    if not infer_keyword(getattr(method, "__doc__", None)):
        forms.reverse()
    try:
        ok = forms[0]()
    except TypeError:
        ok = forms[1]()
    if not ok:
        raise OutlineBuildError(CLOSED_MSG)
    return sp


def _new_edge(board: Any, kind: int, width_nm: int) -> Any:
    pcb = _pcb()
    shape = pcb.PCB_SHAPE(board)
    shape.SetShape(kind)
    shape.SetLayer(pcb.Edge_Cuts)
    shape.SetWidth(width_nm)
    return shape


def _draw_prim(board: Any, prim: Mapping, at: Callable[[Point], Any], width_nm: int) -> Any:
    pcb = _pcb()
    if prim["kind"] == "circle":
        (cx, cy), r = prim["center"], prim["radius"]
        shape = _new_edge(board, pcb.SHAPE_T_CIRCLE, width_nm)
        shape.SetCenter(at((cx, cy)))
        shape.SetEnd(at((cx + r, cy)))
    elif prim["kind"] == "arc":
        shape = _new_edge(board, pcb.SHAPE_T_ARC, width_nm)
        shape.SetArcGeometry(at(prim["start"]), at(prim["mid"]), at(prim["end"]))
    else:
        shape = _new_edge(board, pcb.SHAPE_T_SEGMENT, width_nm)
        shape.SetStart(at(prim["start"]))
        shape.SetEnd(at(prim["end"]))
    return shape


def draw_outline(board: Any, outline: ol.Outline, ox: float, oy: float,
                 width_mm: float = EDGE_WIDTH_MM) -> list:
    """Draw `outline` (board frame) on Edge.Cuts at sheet offset (ox, oy) with
    exact lines / arcs / circles, then check KiCad's polygon: OutlineBuildError
    when it is not closed or its area differs from the model's (0.05 mm^2 +
    0.1 %). The board must have no other Edge.Cuts; on failure the drawn
    shapes stay on it. Returns the PCB_SHAPEs in to_kicad_primitives order."""
    snap = make_snapper()

    def at(p: Point) -> Any:
        return snap(p[0] + ox, p[1] + oy)

    shapes = []
    for prim in ol.to_kicad_primitives(outline):
        shape = _draw_prim(board, prim, at, nm(width_mm))
        board.Add(shape)
        shapes.append(shape)
    got = polygon_outlines(board).Area() / NM_PER_MM ** 2
    want = ol.area(outline)
    if not area_matches(want, got):
        raise OutlineBuildError(f"KiCad's Edge.Cuts polygon has area {got:.3f} mm^2 but the "
                                f"outline model has {want:.3f} mm^2 (another Edge.Cuts shape "
                                "on the board, or a ring KiCad could not chain)")
    return shapes


def _mm_pt(v: Any) -> Point:
    to_mm = _pcb().ToMM
    return (to_mm(v.x), to_mm(v.y))


def _poly_records(shape: Any) -> list[dict]:
    ps, out = shape.GetPolyShape(), []
    for i in range(ps.OutlineCount()):
        for chain in (ps.COutline(i), *(ps.CHole(i, h) for h in range(ps.HoleCount(i)))):
            out.append({"shape": "poly", "points": [_mm_pt(chain.CPoint(j))
                                                    for j in range(chain.PointCount())]})
    return out


def _bezier_record(shape: Any) -> dict:
    shape.RebuildBezierToSegmentsPointsList(nm(BEZIER_MAX_ERROR_MM))
    return {"shape": "bezier", "points": [_mm_pt(v) for v in shape.GetBezierPoints()]}


def shape_records(shape: Any) -> list[dict]:
    """The plain records (sheet mm) of one Edge.Cuts PCB_SHAPE."""
    pcb, kind = _pcb(), shape.GetShape()
    if kind == pcb.SHAPE_T_SEGMENT:
        return [{"shape": "segment", "start": _mm_pt(shape.GetStart()),
                 "end": _mm_pt(shape.GetEnd())}]
    if kind == pcb.SHAPE_T_ARC:
        return [{"shape": "arc", "start": _mm_pt(shape.GetStart()),
                 "mid": _mm_pt(shape.GetArcMid()), "end": _mm_pt(shape.GetEnd())}]
    if kind == pcb.SHAPE_T_CIRCLE:
        return [{"shape": "circle", "center": _mm_pt(shape.GetCenter()),
                 "radius": pcb.ToMM(shape.GetRadius())}]
    if kind == getattr(pcb, "SHAPE_T_RECTANGLE", getattr(pcb, "SHAPE_T_RECT", None)):
        radius = pcb.ToMM(shape.GetCornerRadius()) if hasattr(shape, "GetCornerRadius") else 0.0
        return [{"shape": "rect", "start": _mm_pt(shape.GetStart()),
                 "end": _mm_pt(shape.GetEnd()), "corner_radius": radius}]
    if kind == pcb.SHAPE_T_POLY:
        return _poly_records(shape)
    if kind == pcb.SHAPE_T_BEZIER:
        return [_bezier_record(shape)]
    raise OutlineBuildError(f"unsupported Edge.Cuts shape type {kind!r}")


def edge_shapes(board: Any, include_footprints: bool = True) -> list:
    """Every Edge.Cuts PCB_SHAPE: board drawings, then (optionally) the
    footprints' own Edge.Cuts graphics (e.g. a mid-mount USB-C notch). Exact
    type, as KiCad's outline builder: a PCB_TEXTBOX subclasses PCB_SHAPE."""
    pcb = _pcb()
    items = list(board.GetDrawings())
    if include_footprints:
        items += [g for fp in board.GetFootprints() for g in fp.GraphicalItems()]
    return [it for it in items if type(it) is pcb.PCB_SHAPE and it.GetLayer() == pcb.Edge_Cuts]


def outline_from_board(board: Any, include_footprints: bool = True,
                       tol: float = ol.CHAIN_TOL_MM) -> tuple[ol.Outline, Point]:
    """The board's Edge.Cuts as a board-frame Outline plus its sheet origin
    (ox, oy) = the outline bbox top-left. End points within `tol` mm chain.
    datum = the aux origin in the board frame, None while it is (0, 0).
    Raises OutlineBuildError for missing, open or invalid edges."""
    records = [r for s in edge_shapes(board, include_footprints) for r in shape_records(s)]
    if not records:
        raise OutlineBuildError(f"no Edge.Cuts shapes on the board: {CLOSED_MSG}")
    aux = get_aux_origin(board)
    datum = aux if any(abs(c) > _ZERO_MM for c in aux) else None
    try:
        sheet = ol.from_kicad_primitives(shape_records_to_prims(records), tol, datum)
    except ol.OutlineError as e:
        raise OutlineBuildError(f"{CLOSED_MSG}: {e}") from e
    ox, oy = ol.bbox(sheet)[:2]
    return ol.translate(sheet, -ox, -oy), (ox, oy)


# --- design settings ------------------------------------------------------------------------
def get_aux_origin(board: Any) -> Point:
    """The aux (drill / place-file) origin in sheet mm."""
    return _mm_pt(board.GetDesignSettings().GetAuxOrigin())


def set_datum(board: Any, x_sheet: float, y_sheet: float) -> None:
    """Make (x, y) sheet mm the aux AND grid origin; both persist in the file
    and drive --drill-origin / --use-drill-file-origin exports."""
    ds, v = board.GetDesignSettings(), _pcb().VECTOR2I(nm(x_sheet), nm(y_sheet))
    ds.SetAuxOrigin(v)
    ds.SetGridOrigin(v)


def edge_clearance_mm(board: Any) -> float:
    """The board's copper-to-edge clearance (mm)."""
    return _pcb().ToMM(board.GetDesignSettings().m_CopperEdgeClearance)


# --- planes and rule areas ------------------------------------------------------------------
def _round_all_corners() -> Any:
    """CORNER_STRATEGY_ROUND_ALL_CORNERS (9.0.4 and 10.0.5), or a scoped enum."""
    pcb = _pcb()
    value = getattr(pcb, "CORNER_STRATEGY_ROUND_ALL_CORNERS",
                    getattr(getattr(pcb, "CORNER_STRATEGY", None), "ROUND_ALL_CORNERS", None))
    if value is None:
        raise RuntimeError("this pcbnew exposes no CORNER_STRATEGY ROUND_ALL_CORNERS")
    return value


def plane_polyset(board: Any, inset_mm: float) -> Any:
    """A copy of the Edge.Cuts polygon inset by `inset_mm` (round corners).
    A narrow neck may split it into several outlines: all are kept."""
    if not (isinstance(inset_mm, (int, float)) and math.isfinite(inset_mm) and inset_mm >= 0):
        raise ValueError(f"plane inset must be a finite number >= 0 mm, got {inset_mm!r}")
    ps = _pcb().SHAPE_POLY_SET(polygon_outlines(board))
    if inset_mm > 0:
        ps.Deflate(nm(inset_mm), _round_all_corners(), nm(DEFLATE_MAX_ERROR_MM))
    if ps.OutlineCount() == 0:
        raise OutlineBuildError(f"insetting the board outline by {inset_mm:g} mm leaves no "
                                "outline")
    return ps


def fill_zone_outline(zone: Any, polyset: Any) -> Any:
    """Copy `polyset` (outlines + holes) into zone.Outline() IN PLACE and return
    the zone; raises OutlineBuildError when the result has no area."""
    o = zone.Outline()
    o.RemoveAllContours()
    for i in range(polyset.OutlineCount()):
        idx = o.AddOutline(polyset.COutline(i))
        for h in range(polyset.HoleCount(i)):
            o.AddHole(polyset.CHole(i, h), idx)
    if not zone.Outline().Area() > 0:
        raise OutlineBuildError("the zone outline has zero area after filling it in place")
    return zone


def _pour_accessor(zone: Any, prefix: str) -> Callable:
    for name in _POUR_FLAG:
        fn = getattr(zone, prefix + name, None)
        if fn is not None:
            return fn
    raise AttributeError(f"{type(zone).__name__} has neither {prefix}DoNotAllowZoneFills "
                         f"(KiCad 10) nor {prefix}DoNotAllowCopperPour (KiCad 9)")


def set_no_pour(zone: Any, value: bool = True) -> None:
    """Set the rule area's "no copper pour" flag (ZoneFills on 10.x, CopperPour on 9.x)."""
    _pour_accessor(zone, "Set")(bool(value))


def blocks_pour(zone: Any) -> bool:
    """True when `zone` is a rule area whose "no copper pour" flag is set."""
    return bool(zone.GetIsRuleArea()) and bool(_pour_accessor(zone, "Get")())


def copper_lset(board: Any) -> Any:
    """LSET of the board's enabled copper layers."""
    return _pcb().LSET.AllCuMask(board.GetCopperLayerCount())


def courtyard(fp: Any, layer: int) -> Any:
    """fp's courtyard polygon on `layer`, caches rebuilt first (EMPTY after
    LoadBoard and after FootprintLoad + SetPosition + Add on 9.0.4)."""
    fp.BuildCourtyardCaches()
    return fp.GetCourtyard(layer)


def load_board(path: str | os.PathLike) -> Any:
    """pcbnew.LoadBoard with the two failure modes made loud: FileNotFoundError
    before pcbnew is called, RuntimeError when it fails or returns None."""
    p = os.fspath(path)
    if not os.path.isfile(p):
        raise FileNotFoundError(f"board file not found: {p}")
    pcb = _pcb()
    try:
        board = pcb.LoadBoard(p)
    except Exception as e:
        raise RuntimeError(f"pcbnew could not load {p}: {type(e).__name__}: {e}") from e
    if board is None:
        raise RuntimeError(f"pcbnew.LoadBoard returned None for {p} (unreadable or a newer "
                           "file format)")
    return board
