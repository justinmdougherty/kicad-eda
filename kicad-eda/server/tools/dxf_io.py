#!/usr/bin/env python3
"""Board-outline DXF in and out (ROADMAP_DECISIONS D2, the Fusion 360 round trip).

Reader: DXF -> closed loops -> outline.py's Outline / the D2 board block in the
board frame (mm, Y down, bbox top-left = 0, 0; ``datum`` = the DXF origin).
  * LINE, ARC (a 0..360 ARC is a circle), CIRCLE, LWPOLYLINE / 2D POLYLINE
    (bulges stay exact arcs), 3D POLYLINE, SPLINE / ELLIPSE (flattened within
    ``curve_tol``), INSERT and MINSERT (every array cell exploded; block
    entities on layer 0 take the INSERT's layer); anything else (POINT, TEXT,
    mesh POLYLINE, ...) is counted as skipped.
  * Units: ``$INSUNITS`` (Fusion writes 5 = cm) unless ``units`` overrides it.
  * Plane: XY, else the layer's ``NFusion_SketchTransform`` (Fusion writes an
    XZ-plane sketch in world coordinates), else the common arc normal, else
    (lines only) a plane fitted through the points, with a warning.
  * ``layer`` keeps one layer (any case); the skipped entities and warnings
    of the other layers are dropped with their geometry.
  * dxf_loops does the geometry: duplicates dropped with a warning, pieces
    chained within ``tol`` (an open loop raises DxfOutlineError listing every
    gap and open path), outer = largest |area|, the rest cutouts strictly
    inside it (else reported, or dropped with ``allow_stray``).
Writer: a D2 board block -> DXF R2010, mm, Y up, origin = ``datum``, closed
LWPOLYLINEs with bulges and CIRCLEs on a named layer.

Bulge convention as outline_geom (> 0 = math-CCW), so the Y flip negates it.
Venv only (ezdxf, server/requirements.txt); SWIG scripts get JSON loops.

Usage: <python> dxf_io.py read <file.dxf> [--layer L] [--units mm|cm|in|mil]
                           [--tol MM] [--curve-tol MM] [--allow-stray]
       <python> dxf_io.py write <board.json|netlist.json> <out.dxf> [--layer L]
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import ezdxf
from ezdxf import recover
from ezdxf.audit import AuditError
from ezdxf.lldxf.const import VTX_SPLINE_FRAME_CONTROL_POINT, DXFError
from ezdxf.lldxf.validator import INVALID_LAYER_NAME_CHARACTERS, is_valid_layer_name
from ezdxf.math import OCS, Vec3, arc_angle_span_deg

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import outline as ol  # noqa: E402
from dxf_loops import (Classified, DxfOutlineError, Gap, Loop, Piece,  # noqa: E402
                       chain_pieces, circle_loop, classify_loops, describe, drop_duplicates,
                       gap_error)
from netlist_schema import DXF_UNITS  # noqa: E402
from outline_geom import Point, dist  # noqa: E402

__all__ = ["BoardOutline", "Classified", "DxfLoops", "DxfOutlineError", "Gap", "Loop",
           "classify_loops", "outline_from_spec", "read_board_outline", "read_dxf_loops",
           "write_outline_dxf", "DEFAULT_JOIN_TOL_MM", "DEFAULT_CURVE_TOL_MM", "DEFAULT_LAYER"]

DEFAULT_JOIN_TOL_MM = 0.01        # endpoint chaining tolerance
DEFAULT_CURVE_TOL_MM = 0.02       # SPLINE / ELLIPSE flattening (max chord deviation)
DEFAULT_LAYER = "OUTLINE"         # the writer's layer
DXF_VERSION_OUT = "R2010"         # LWPOLYLINE needs R2000+; Fusion reads R12..R2018
FUSION_APPID = "NFusion_SketchTransform"
UNIT_MM = {"mm": 1.0, "cm": 10.0, "in": 25.4, "mil": 0.0254}   # override names
INSUNITS = {1: ("in", 25.4), 2: ("ft", 304.8), 4: ("mm", 1.0), 5: ("cm", 10.0),
            6: ("m", 1000.0), 8: ("uin", 25.4e-6), 9: ("mil", 0.0254),
            10: ("yd", 914.4), 13: ("um", 0.001), 14: ("dm", 100.0)}
INSUNITS_MM = 4
PLAUSIBLE_SIDE_MM = (1.0, 1000.0)  # bbox sides outside this suggest wrong units
# ezdxf's flattening checks the deviation at segment midpoints only and overshoots
# the requested distance by up to ~5 % on wiggly splines (0.0209 for 0.02 mm)
_FLATTEN_MARGIN = 0.8
_EPS = 1e-9
_MAX_INSERT_DEPTH = 8
_BAD_LAYER_CHARS = "".join(sorted(INVALID_LAYER_NAME_CHARACTERS))

if set(UNIT_MM) != set(DXF_UNITS):  # pragma: no cover - schema drift guard
    raise ImportError(f"dxf_io.UNIT_MM {sorted(UNIT_MM)} != netlist_schema.DXF_UNITS")


# --- DXF entities -> WCS pieces --------------------------------------------------------
@dataclass(frozen=True)
class _Raw:
    """One entity in WCS, before the drawing plane is known. ``bulges`` turn
    counter-clockwise about ``normal``; a CIRCLE has ``radius`` and one point."""
    points: tuple[Vec3, ...]
    bulges: tuple[float, ...]
    normal: Vec3 | None
    source: str
    layer: str
    radius: float | None = None


@dataclass(frozen=True)
class _Ctx:
    """Entity extraction settings (drawing units) and where it reports, per
    layer so that _collect keeps only what the layer filter picks."""
    curve_du: float                    # flattening distance
    tol_du: float                      # join tolerance
    skipped: list[tuple[str, str]]     # (entity kind, layer)
    notes: list[tuple[str, str]]       # (layer, warning)


def _closed(points: list[Vec3], bulges: list[float], closed: bool) -> tuple:
    if closed and points:
        return tuple(points + [points[0]]), tuple(bulges)
    return tuple(points), tuple(bulges[:-1])


def _polyline_raw(e: Any, source: str, layer: str) -> _Raw | None:
    if e.dxftype() == "LWPOLYLINE":
        ocs, z = e.ocs(), float(e.dxf.elevation)
        rows = [(float(x), float(y), float(b)) for x, y, b in e.get_points("xyb")]
        pts, bul = _closed([ocs.to_wcs(Vec3(x, y, z)) for x, y, _b in rows],
                           [b for _x, _y, b in rows], bool(e.closed))
        return _Raw(pts, bul, Vec3(e.dxf.extrusion).normalize(), source, layer)
    verts = [v for v in e.vertices if not v.dxf.flags & VTX_SPLINE_FRAME_CONTROL_POINT]
    if e.is_2d_polyline:
        ocs, z = e.ocs(), float(Vec3(e.dxf.elevation).z)
        pts, bul = _closed([ocs.to_wcs(Vec3(v.dxf.location.x, v.dxf.location.y, z)) for v in verts],
                           [float(v.dxf.bulge) for v in verts], e.is_closed)
        return _Raw(pts, bul, Vec3(e.dxf.extrusion).normalize(), source, layer)
    if e.is_3d_polyline:
        pts, bul = _closed([Vec3(v.dxf.location) for v in verts], [0.0] * len(verts),
                           e.is_closed)
        return _Raw(pts, bul, None, source, layer)
    return None                                   # polyface / polygon mesh


def _arc_raw(e: Any, layer: str, source: str, ctx: _Ctx) -> _Raw | None:
    """An ARC as one bulge edge; a full turn (0..360, 90..450, ...) as a circle."""
    span = arc_angle_span_deg(float(e.dxf.start_angle), float(e.dxf.end_angle))
    normal = Vec3(e.dxf.extrusion).normalize()
    start, end = Vec3(e.start_point), Vec3(e.end_point)
    if span <= _EPS:
        ctx.notes.append((layer, f"ignored {source}: zero sweep angle"))
        return None
    if span > 180.0 and start.isclose(end, abs_tol=ctx.tol_du):
        return _Raw((e.ocs().to_wcs(e.dxf.center),), (), normal, source, layer,
                    float(e.dxf.radius))
    return _Raw((start, end), (math.tan(math.radians(span) / 4.0),), normal, source, layer)


def _insert_raws(e: Any, layer: str, source: str, ctx: _Ctx, depth: int) -> Iterator[_Raw]:
    """Block entities of an INSERT; a MINSERT array explodes cell by cell
    (``virtual_entities`` alone only yields the first cell). Entities ezdxf
    cannot transform (e.g. a zero-radius CIRCLE under non-uniform scaling)
    are counted as skipped with a warning instead of vanishing."""
    cells = list(e.multi_insert()) if e.mcount > 1 else [e]
    for k, cell in enumerate(cells, 1):
        where = f"in block '{e.dxf.name}' ({source}{f' cell {k}' if len(cells) > 1 else ''})"

        def not_exploded(child: Any, reason: str, where: str = where) -> None:
            child_layer = layer if child.dxf.layer == "0" else child.dxf.layer
            ctx.skipped.append((f"{child.dxftype()} (not exploded)", child_layer))
            ctx.notes.append((child_layer,
                              f"ezdxf could not explode {child.dxftype()} {where}: {reason}"))

        for child in cell.virtual_entities(skipped_entity_callback=not_exploded):
            child_layer = layer if child.dxf.layer == "0" else child.dxf.layer
            yield from _entity_raws(child, child_layer, f"{child.dxftype()} {where}", ctx,
                                    depth + 1)


def _entity_raws(e: Any, layer: str, source: str, ctx: _Ctx, depth: int) -> Iterator[_Raw]:
    kind = e.dxftype()
    raw: _Raw | None = None
    if kind == "LINE":
        raw = _Raw((Vec3(e.dxf.start), Vec3(e.dxf.end)), (0.0,), None, source, layer)
    elif kind == "ARC":
        raw = _arc_raw(e, layer, source, ctx)
    elif kind == "CIRCLE":
        raw = _Raw((e.ocs().to_wcs(e.dxf.center),), (), Vec3(e.dxf.extrusion).normalize(),
                   source, layer, float(e.dxf.radius))
    elif kind in ("LWPOLYLINE", "POLYLINE"):
        raw = _polyline_raw(e, source, layer)
        if raw is None:
            ctx.skipped.append(("POLYLINE (mesh)", layer))
        elif len(raw.points) < 2:
            ctx.notes.append((layer, f"ignored {source}: fewer than 2 vertices"))
            raw = None
    elif kind in ("SPLINE", "ELLIPSE"):
        pts = tuple(Vec3(p) for p in e.flattening(ctx.curve_du))
        raw = _Raw(pts, (0.0,) * (len(pts) - 1), None, source, layer)
    elif kind == "INSERT" and depth < _MAX_INSERT_DEPTH:
        yield from _insert_raws(e, layer, source, ctx, depth)
    else:
        ctx.skipped.append((kind, layer))
    if raw is not None:
        yield raw


# --- drawing plane -----------------------------------------------------------------------
@dataclass(frozen=True)
class _Frame:
    """A right-handed 2D frame in WCS: point -> ((p - origin).u, (p - origin).v)."""
    origin: Vec3
    u: Vec3
    v: Vec3
    n: Vec3
    label: str

    def to2d(self, p: Vec3, scale: float) -> Point:
        d = p - self.origin
        return (d.dot(self.u) * scale, d.dot(self.v) * scale)

    def fits(self, raws: Sequence[_Raw], tol_du: float) -> bool:
        heights = [(p - self.origin).dot(self.n) for r in raws for p in r.points]
        return max(heights) - min(heights) <= tol_du and all(
            abs(r.normal.dot(self.n)) > 1.0 - 1e-6 for r in raws if r.normal is not None)


def _ocs_frame(normal: Vec3, label: str) -> _Frame:
    ocs = OCS(normal)
    return _Frame(Vec3(), Vec3(ocs.ux), Vec3(ocs.uy), Vec3(ocs.uz),
                  label + " ({:g}, {:g}, {:g})".format(*normal))


_XY = _Frame(Vec3(), Vec3(1, 0, 0), Vec3(0, 1, 0), Vec3(0, 0, 1), "XY")


def _fusion_frame(doc: Any, layers: set[str], warnings: list[str]) -> _Frame | None:
    """The sketch frame from the layers' NFusion_SketchTransform: 16 numbers,
    row-major 4x4, world = M . sketch (verified on real Fusion exports)."""
    found: dict[tuple[float, ...], list[str]] = {}
    for name in sorted(layers):
        layer = doc.layers.get(name) if doc.layers.has_entry(name) else None
        if layer is None or not layer.has_xdata(FUSION_APPID):
            continue
        text = next((str(t.value) for t in layer.get_xdata(FUSION_APPID) if t.code == 1000), "")
        try:
            m = tuple(float(v) for v in text.split(";"))
        except ValueError:
            m = ()
        if len(m) == 16:
            found.setdefault(m, []).append(name)
        else:
            warnings.append(f"layer '{name}': unreadable {FUSION_APPID} {text!r}; ignored")
    if len(found) > 1:
        raise DxfOutlineError("the geometry comes from Fusion sketches on different planes "
                              f"(layers {'; '.join(', '.join(v) for v in found.values())}); "
                              "pick one with layer=")
    if not found:
        return None
    (m, names), = found.items()
    u, v, n = Vec3(m[0], m[4], m[8]), Vec3(m[1], m[5], m[9]), Vec3(m[2], m[6], m[10])
    if not (abs(u.magnitude - 1) < 1e-4 and abs(v.magnitude - 1) < 1e-4
            and abs(u.dot(v)) < 1e-4 and u.cross(v).isclose(n, abs_tol=1e-4)):
        warnings.append(f"layer '{names[0]}': {FUSION_APPID} is not a rigid transform; ignored")
        return None
    return _Frame(Vec3(m[3], m[7], m[11]), u, v, n,
                  f"Fusion sketch transform (layer '{names[0]}')")


def _fitted_frame(raws: Sequence[_Raw]) -> _Frame | None:
    """The plane through three well-spread points (None when they are all on
    one line), its normal's largest component made positive: a Fusion XZ
    sketch faces +Y, so lines-only XZ geometry reads like one."""
    pts = [p for r in raws for p in r.points]
    p0 = pts[0]
    axis = max(pts, key=lambda p: (p - p0).magnitude) - p0
    normal = axis.cross(max(pts, key=lambda p: axis.cross(p - p0).magnitude) - p0)
    if normal.magnitude <= _EPS * axis.magnitude:      # |n| = |axis| x distance off the line
        return None
    normal = normal.normalize()
    if max(normal.xyz, key=abs) < 0:
        normal = -normal
    return _ocs_frame(Vec3(*(c + 0.0 for c in normal.round(9))), "fitted plane normal")


def _pick_frame(doc: Any, raws: Sequence[_Raw], tol_du: float,
                warnings: list[str]) -> _Frame:
    """XY, else the Fusion sketch transform, else the common arc normal, else
    (no arcs or circles) a plane fitted through the points."""
    fusion = _fusion_frame(doc, {r.layer for r in raws}, warnings)
    if fusion is not None:
        if fusion.fits(raws, tol_du):
            return fusion
        warnings.append(f"{fusion.label} does not match the geometry's plane; ignored")
    normals = [r.normal for r in raws if r.normal is not None]
    if all(abs(n.z) > 1.0 - 1e-6 for n in normals):
        if _XY.fits(raws, tol_du):
            return _XY
        fitted = None if normals else _fitted_frame(raws)
        if fitted is not None and fitted.fits(raws, tol_du):
            warnings.append(f"the geometry lies in the {fitted.label} plane, not XY, and has no "
                            "sketch transform or arc normal to orient it: projected viewed from "
                            "+normal; the outline may be mirrored or rotated against the original "
                            "sketch, so check it")
            return fitted
    else:
        frame = _ocs_frame(Counter(n.round(6) for n in normals).most_common(1)[0][0],
                           "plane normal")
        if frame.fits(raws, tol_du):
            warnings.append(f"the geometry lies in the {frame.label} plane, not XY: projected "
                            "viewed from +normal (not mirrored); the in-plane rotation may "
                            "differ from the original sketch axes")
            return frame
    raise DxfOutlineError("the outline geometry is not in one plane (checked XY, the Fusion "
                          "sketch transform, the arc normals and, for lines only, a plane fitted "
                          "through the points); export a single flat sketch")


# --- reader ------------------------------------------------------------------------------
@dataclass(frozen=True)
class DxfLoops:
    """Closed loops in the DXF frame (mm, Y up, DXF origin) plus provenance."""
    path: str
    loops: tuple[Loop, ...]
    units: str
    units_source: str                  # "override" | "$INSUNITS" | "default"
    scale: float                       # mm per drawing unit
    layers: tuple[str, ...]            # layers that contributed geometry
    plane: str
    skipped: tuple[tuple[str, int], ...]
    warnings: tuple[str, ...]


def _check_tol(value: float, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) \
            or not math.isfinite(value) or value <= 0:
        raise DxfOutlineError(f"{name} must be a positive number of mm, got {value!r}")
    return float(value)


def _open(path: Path, warnings: list[str]) -> Any:
    if not path.is_file():
        raise DxfOutlineError(f"DXF file not found: {path}")
    try:
        doc, auditor = recover.readfile(str(path))
    except (DXFError, OSError, UnicodeError, ValueError) as exc:
        raise DxfOutlineError(f"{path}: not a readable DXF file ({exc})") from exc
    # a layer name with a character DXF forbids does not touch the geometry
    named = [e for e in auditor.errors if e.code == AuditError.INVALID_LAYER_NAME]
    fatal = [e for e in auditor.errors if e.code != AuditError.INVALID_LAYER_NAME]
    if fatal:
        raise DxfOutlineError(f"{path}: unrecoverable DXF errors: "
                              + "; ".join(str(e.message) for e in fatal[:5]))
    if named:
        warnings.append(f"{len(named)} entit{'y has' if len(named) == 1 else 'ies have'} a "
                        f"layer name DXF does not allow (first: {named[0].message}); read anyway")
    if auditor.fixes:
        warnings.append(f"ezdxf repaired {len(auditor.fixes)} problem(s) in the file: "
                        + "; ".join(str(f.message) for f in auditor.fixes[:3]))
    return doc


def _units(doc: Any, override: str | None, warnings: list[str]) -> tuple[str, float, str]:
    if override is not None:
        key = str(override).strip().lower()
        if key not in UNIT_MM:
            raise DxfOutlineError(f"units {override!r} is not one of {', '.join(DXF_UNITS)}")
        return key, UNIT_MM[key], "override"
    raw = doc.header.get("$INSUNITS", 0)
    code = int(raw) if isinstance(raw, (int, float)) else 0
    if code == 0:
        warnings.append("the DXF has no $INSUNITS (unitless): read as mm; pass units= "
                        f"({', '.join(DXF_UNITS)}) if it is not")
        return "mm", 1.0, "default"
    if code not in INSUNITS:
        raise DxfOutlineError(f"$INSUNITS {code} ({ezdxf.units.unit_name(code)}) is not a "
                              f"unit this reader converts; pass units= ({', '.join(DXF_UNITS)})")
    return INSUNITS[code][0], INSUNITS[code][1], "$INSUNITS"


def _check_layer(layer: object) -> str | None:
    if layer is not None and (not isinstance(layer, str) or not layer.strip()):
        raise DxfOutlineError(f"layer must be a layer name, got {layer!r}")
    return layer


def _collect(doc: Any, layer: str | None, ctx: _Ctx, path: Path,
             warnings: list[str]) -> tuple[list[_Raw], Counter]:
    """The raws on ``layer`` (any case; None = all layers); the skipped
    entities and per-entity warnings of the other layers are dropped too."""
    raws: list[_Raw] = []
    for e in doc.modelspace():
        raws.extend(_entity_raws(e, e.dxf.layer, f"{e.dxftype()} #{e.dxf.handle}", ctx, 0))
    want = layer.casefold() if layer is not None else None

    def wanted(name: str) -> bool:
        return want is None or name.casefold() == want

    picked = [r for r in raws if wanted(r.layer)]
    skip = Counter(k for k, lay in ctx.skipped if wanted(lay))
    warnings.extend(note for lay, note in ctx.notes if wanted(lay))
    if not picked:
        have = ", ".join(f"{k} ({v})" for k, v in sorted(Counter(r.layer for r in raws).items()))
        raise DxfOutlineError(
            f"{path}: no outline geometry{f' on layer {layer!r}' if layer is not None else ''}; "
            f"layers with geometry: {have or 'none'}; skipped: "
            + (", ".join(f"{k} x{v}" for k, v in sorted(skip.items())) or "nothing"))
    if set(skip) - {"POINT"}:
        warnings.append("skipped entities that are not outline geometry: "
                        + ", ".join(f"{k} x{v}" for k, v in sorted(skip.items())))
    return picked, skip


def _project(raws: Sequence[_Raw], frame: _Frame, scale: float, tol: float,
             warnings: list[str]) -> tuple[list[Piece], list[Loop]]:
    pieces: list[Piece] = []
    circles: list[Loop] = []
    for r in raws:
        sign = -1.0 if r.normal is not None and r.normal.dot(frame.n) < 0 else 1.0
        if r.radius is not None:
            if r.radius * scale <= tol:
                warnings.append(f"ignored {r.source}: radius {r.radius * scale:g} mm is not "
                                f"above the {tol:g} mm join tolerance")
            else:
                circles.append(circle_loop(*frame.to2d(r.points[0], scale), r.radius * scale,
                                           r.source))
            continue
        pts = [frame.to2d(p, scale) for p in r.points]
        edges = tuple((*pts[i], *pts[i + 1], r.bulges[i] * sign if abs(r.bulges[i]) > 1e-12 else 0.0)
                      for i in range(len(pts) - 1) if dist(pts[i], pts[i + 1]) > _EPS)
        piece = Piece(edges, r.source, f"{r.source} start", f"{r.source} end") if edges else None
        if piece is None or piece.length() <= tol:
            warnings.append(f"ignored {r.source}: shorter than the {tol:g} mm join tolerance")
        else:
            pieces.append(piece)
    return pieces, circles


def read_dxf_loops(path: str | Path, *, layer: str | None = None, units: str | None = None,
                   tol: float = DEFAULT_JOIN_TOL_MM,
                   curve_tol: float = DEFAULT_CURVE_TOL_MM) -> DxfLoops:
    """Every closed loop of a DXF in its own frame (mm, Y up). ``layer`` (any
    case) keeps one layer's geometry. Raises DxfOutlineError (with ``gaps``)
    when the pieces do not all close."""
    tol, curve_tol = _check_tol(tol, "tol"), _check_tol(curve_tol, "curve_tol")
    layer = _check_layer(layer)
    path = Path(path)
    warnings: list[str] = []
    doc = _open(path, warnings)
    unit, scale, unit_source = _units(doc, units, warnings)
    ctx = _Ctx(curve_tol * _FLATTEN_MARGIN / scale, tol / scale, [], [])
    raws, skipped = _collect(doc, layer, ctx, path, warnings)
    frame = _pick_frame(doc, raws, tol / scale, warnings)
    pieces, circles = drop_duplicates(*_project(raws, frame, scale, tol, warnings), tol, warnings)
    chained, open_chains = chain_pieces(pieces, tol, warnings)
    if open_chains:
        raise gap_error(path, open_chains, tol, len(chained) + len(circles))
    return DxfLoops(str(path), tuple(circles + chained), unit, unit_source, scale,
                    tuple(dict.fromkeys(r.layer for r in raws)), frame.label,
                    tuple(sorted(skipped.items())), tuple(warnings))


# --- board frame (outline.py) -------------------------------------------------------------
def _model_ring(loop: Loop) -> ol.Loop | ol.Circle:
    return ol.Circle(*loop.circle) if loop.circle else ol.Loop(loop.vertices)


def _own_loop(ring: ol.Loop | ol.Circle, sources: tuple[str, ...], outer: bool) -> Loop:
    """A model ring with its source entities (a circle as two half arcs,
    oriented like the model: outer positive, cutouts negative)."""
    if isinstance(ring, ol.Loop):
        return Loop(ring.vertices, sources)
    s = 1.0 if outer else -1.0            # math-CCW / math-CW half circles
    return Loop(((ring.cx - s * ring.r, ring.cy, s), (ring.cx + s * ring.r, ring.cy, s)),
                sources, (ring.cx, ring.cy, ring.r))


@dataclass(frozen=True)
class BoardOutline:
    """A DXF outline in the board frame (mm, Y down, bbox top-left = 0, 0).
    ``model`` is outline.py's validated Outline; ``outline`` / ``cutouts`` are
    the same rings with the DXF entities they came from."""
    model: ol.Outline
    outline: Loop
    cutouts: tuple[Loop, ...]
    datum: Point                       # the DXF origin in the board frame
    size: Point                        # outline bbox (w, h)
    source: DxfLoops
    warnings: tuple[str, ...]

    def to_board_block(self) -> dict[str, Any]:
        """The D2 ``board`` keys via outline.to_board_block: ``w``/``h`` (the
        bbox), ``outline.loop``, ``cutouts``, ``datum``."""
        return ol.to_board_block(self.model)


def read_board_outline(path: str | Path, *, layer: str | None = None,
                       units: str | None = None, tol: float = DEFAULT_JOIN_TOL_MM,
                       curve_tol: float = DEFAULT_CURVE_TOL_MM,
                       allow_stray: bool = False) -> BoardOutline:
    """A DXF outline in the board frame (``.to_board_block()`` for the
    netlist). Loops that cannot be cutouts raise DxfOutlineError
    (``problems``) unless ``allow_stray`` drops them with a warning."""
    src = read_dxf_loops(path, layer=layer, units=units, tol=tol, curve_tol=curve_tol)
    cls = classify_loops(src.loops)
    warnings = list(src.warnings)
    if cls.stray:
        problems = [f"{describe(lp)}: {why}" for lp, why in cls.stray]
        if not allow_stray:
            raise DxfOutlineError(
                f"{path}: {len(problems)} loop(s) cannot be cutouts of the outer "
                f"{describe(cls.outer)} (DXF frame, mm, Y up):\n  " + "\n  ".join(problems)
                + "\nMove them to another layer and pass layer=, or pass allow_stray=True "
                  "to drop them.", problems=problems)
        warnings.extend(f"dropped {p}" for p in problems)
    try:                               # flip Y, bbox top-left to (0, 0), validate
        model, datum = ol.normalize_external([_model_ring(lp) for lp in (cls.outer, *cls.cutouts)])
    except ol.OutlineError as exc:
        raise DxfOutlineError(f"{path}: the loops are not a valid board outline: {exc}") from exc
    x0, y0, x1, y1 = ol.bbox(model)
    size = (x1 - x0, y1 - y0)
    if min(size) < PLAUSIBLE_SIDE_MM[0] or max(size) > PLAUSIBLE_SIDE_MM[1]:
        warnings.append(f"the outline is {size[0]:.3f} x {size[1]:.3f} mm, implausible for a "
                        f"PCB: check the units (read as {src.units}, from {src.units_source}); "
                        "pass units= to override")
    return BoardOutline(model, _own_loop(model.outer, cls.outer.sources, True),
                        tuple(_own_loop(r, lp.sources, False)
                              for r, lp in zip(model.cutouts, cls.cutouts)),
                        datum, size, src, tuple(warnings))


def outline_from_spec(spec: Mapping[str, Any], base_dir: str | Path, **kwargs: Any) -> BoardOutline:
    """Resolve a netlist ``board.outline`` DXF reference (D2: ``{"dxf":
    "mech/outline.dxf", "layer", "units"}``), the path relative to ``base_dir``
    (the netlist directory, also ``${KIPRJMOD}``)."""
    ref = spec.get("dxf") if isinstance(spec, Mapping) else None
    if not isinstance(ref, str) or not ref.strip():
        raise DxfOutlineError("an outline spec needs a 'dxf' path, e.g. {'dxf': "
                              "'mech/outline.dxf', 'layer': 'OUTLINE', 'units': 'mm'}")
    path = Path(ref.replace("${KIPRJMOD}", str(base_dir)))
    path = path if path.is_absolute() else Path(base_dir) / path
    return read_board_outline(path, layer=spec.get("layer"), units=spec.get("units"), **kwargs)


# --- writer ------------------------------------------------------------------------------
def write_outline_dxf(path: str | Path, board: Mapping[str, Any], *,
                      layer: str = DEFAULT_LAYER) -> Path:
    """Write a D2 board block (``outline.loop`` or ``w``/``h``/``corner_radius``,
    ``cutouts``, ``datum``; validated by outline.from_board_block) as a DXF for
    Fusion: mm, Y up, origin = ``datum`` (default: the outline's bottom-left),
    closed LWPOLYLINEs with bulges and CIRCLEs on ``layer`` (created unless the
    new document already has it, like "0"). Returns the path."""
    if not isinstance(layer, str) or not layer.strip() or not is_valid_layer_name(layer):
        raise DxfOutlineError(f"layer must be a DXF layer name (not blank, none of "
                              f"{_BAD_LAYER_CHARS}), got {layer!r}")
    try:
        model = ol.from_board_block(board)
    except ol.OutlineError as exc:
        raise DxfOutlineError(f"cannot write the board outline: {exc}") from exc
    x0, _y0, _x1, y1 = ol.bbox(model)
    datum = model.datum if model.datum is not None else (x0, y1)
    doc = ezdxf.new(DXF_VERSION_OUT, units=INSUNITS_MM)
    doc.header["$MEASUREMENT"] = 1                  # metric
    if not doc.layers.has_entry(layer):
        doc.layers.add(layer)
    msp = doc.modelspace()
    for ring in (model.outer, *model.cutouts):
        if isinstance(ring, ol.Circle):
            msp.add_circle(ol.board_to_external((ring.cx, ring.cy), datum), ring.r,
                           dxfattribs={"layer": layer})
        else:                          # the Y flip negates every bulge
            msp.add_lwpolyline([(*ol.board_to_external((x, y), datum), 0.0, 0.0, -b)
                                for x, y, b in ring.vertices],
                               format="xyseb", close=True, dxfattribs={"layer": layer})
    out = Path(path)
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        doc.saveas(out)
    except OSError as exc:
        raise DxfOutlineError(f"cannot write {out}: {exc}") from exc
    return out


# --- CLI ---------------------------------------------------------------------------------
def _cli(args: argparse.Namespace) -> dict[str, Any]:
    if args.cmd == "write":
        data = json.loads(Path(args.board_json).read_text(encoding="utf-8"))
        board = data.get("board", data) if isinstance(data, dict) else data
        return {"ok": True, "path": str(write_outline_dxf(args.out, board, layer=args.layer)),
                "layer": args.layer}
    res = read_board_outline(args.dxf, layer=args.layer, units=args.units, tol=args.tol,
                             curve_tol=args.curve_tol, allow_stray=args.allow_stray)
    return {"ok": True, "board": res.to_board_block(), "size": [round(s, 6) for s in res.size],
            "units": res.source.units, "units_source": res.source.units_source,
            "plane": res.source.plane, "layers": list(res.source.layers),
            "skipped": dict(res.source.skipped), "warnings": list(res.warnings)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dxf_io.py", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    rd = sub.add_parser("read", help="DXF -> D2 board block (JSON on stdout)")
    rd.add_argument("dxf")
    rd.add_argument("--layer")
    rd.add_argument("--units", choices=DXF_UNITS)
    rd.add_argument("--tol", type=float, default=DEFAULT_JOIN_TOL_MM)
    rd.add_argument("--curve-tol", type=float, default=DEFAULT_CURVE_TOL_MM)
    rd.add_argument("--allow-stray", action="store_true")
    wr = sub.add_parser("write", help="board block / netlist JSON -> outline DXF")
    wr.add_argument("board_json")
    wr.add_argument("out")
    wr.add_argument("--layer", default=DEFAULT_LAYER)
    args = parser.parse_args(argv)
    try:
        result = _cli(args)
    except (DxfOutlineError, OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, indent=2))
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
