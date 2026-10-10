#!/usr/bin/env python3
"""Placement for the headless board builder (swig_build_board.py, F3-12). KiCad's
Python (stdlib + pcbnew, 3.11 syntax); in the venv only the pure helpers work
(place_engine, which imports pcbnew, is imported lazily). Board frame: mm, Y
down, origin = outline bbox top-left; sheet = board frame + (ox, oy).

x/y parts keep their meaning (courtyard-bbox centre); D3 "at": [x, y, rot]
parts are placed by origin and final rotation (swig_anchor.place_at). Both are
fixed; auto parts pack beside them as before, into one field per side:
board_field.legacy_fields on w/h boards (bit-identical to the pre-F3-12
builder), auto_fields on outline boards. A component "edge" goes to
functional_place as conn_edges; after placement such an AUTO part is faced to
its edge and slid onto the OUTER board edge (board_field.snap_to_edge), hanging
its "overhang", else the overhang derived from an edge-mount footprint's 'PCB
Edge' marker, past it. Every courtyard is then checked with
outline_audit.courtyard_issues (builder warnings; G3 blocks them).
"""
from __future__ import annotations

import dataclasses
import math
import os
import sys
from collections.abc import Mapping, Sequence
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

import board_field as bfield  # noqa: E402
import build_features as feat  # noqa: E402
import edge_mount as em  # noqa: E402
import outline as ol  # noqa: E402
import outline_audit  # noqa: E402
import swig_anchor  # noqa: E402
from build_features import BuildError  # noqa: E402
from place_conn import EDGES, MOUTHLESS, NORMALS  # noqa: E402

Box = tuple[float, float, float, float]
Polys = list[list[tuple[float, float]]]
SIDES = bfield.SIDES
NM_PER_MM = 1e6
FACING_MIN_COS = outline_audit.FACING_MIN_COS - 1e-9    # an edge-mount part faces its edge


@dataclasses.dataclass(frozen=True)
class Layout:
    """Board-frame bbox-centre targets of the x/y and auto parts ("at" parts are
    placed by origin), the packer's rotations, board size, auto fields, warnings."""
    targets: dict
    rots: dict
    w: float
    h: float
    fields: tuple = ()
    warnings: tuple = ()


# --- design (pure) -------------------------------------------------------------------------
def anchored(c: Mapping) -> bool:
    return c.get("at") is not None                                      # a D3 "at" part


def fixed_refs(design: Mapping) -> set[str]:
    """Parts the netlist places: "at", or both "x" and "y" keys (as before)."""
    return {c["ref"] for c in design["components"] if anchored(c) or ("x" in c and "y" in c)}


def anchor_refs(design: Mapping) -> list[str]:
    return [c["ref"] for c in design["components"] if anchored(c)]     # result "anchors"


def conn_edges(design: Mapping) -> dict[str, str]:
    """{ref: edge} of every component with an "edge" (null means the default top)."""
    return {c["ref"]: c["edge"] for c in design["components"] if c.get("edge") is not None}


def check_design(design: Mapping) -> None:
    """BuildError for an unknown "edge", a non-boolean board_only / dnp / in_bom, an
    "at" part that also has x / y / rot, or a malformed "at" or side on an "at" part."""
    for c in design["components"]:
        ref = c.get("ref", "?")
        if c.get("edge") is not None and c["edge"] not in EDGES:
            raise BuildError(f"{ref}: 'edge' must be one of {', '.join(EDGES)} (null = top), "
                             f"got {c['edge']!r}")
        try:
            swig_anchor.attr_setters(c)               # D4 flags; the message names the ref
        except ValueError as e:
            raise BuildError(str(e)) from e
        if not anchored(c):
            continue
        both = [k for k in ("x", "y", "rot") if c.get(k) is not None]
        if both:
            raise BuildError(f"{ref}: a component is placed by 'at' or by 'x'/'y'/'rot', never "
                             f"both (it has 'at' and {', '.join(both)})")
        try:
            swig_anchor.parse_at(c["at"])
            swig_anchor.norm_side(c.get("side"))
        except ValueError as e:
            raise BuildError(f"{ref}: {e}") from e


def apply_overrides(design: Mapping, placement: Mapping) -> dict:
    """`design` with placement overrides (ref -> [x, y, rot]) merged into NEW
    component dicts; an override replaces a part's "at" (it re-places the part
    by its courtyard centre)."""
    comps = [{**{k: v for k, v in c.items() if k != "at"},
              **dict(zip(("x", "y", "rot"), placement[c["ref"]], strict=True))}
             if c["ref"] in placement else c for c in design["components"]]
    return {**design, "components": comps}


def _outline_wh(outline: ol.Outline | None) -> tuple[float, float] | None:
    if outline is None:
        return None
    x0, y0, x1, y1 = ol.bbox(outline)
    return (x1 - x0, y1 - y0)


def _has_conn(comps: Sequence[Mapping]) -> bool:
    return any(c["ref"][:1] == "J" and not c.get("interior") for c in comps)


# --- targets -------------------------------------------------------------------------------
def pack_scale() -> float:
    """PACK_SCALE loosens spacing on a retry (more room for the router)."""
    return float(os.environ.get("PACK_SCALE", "1.0"))


def _functional_place(design: Mapping, parts: tuple, edges: Mapping[str, str],
                      **kw: Any) -> tuple[dict, dict, float, float]:
    from place_engine import functional_place     # imports pcbnew (KiCad Python, or a stub)
    extents, pincount, mouth = parts
    try:
        return functional_place(design, extents, pincount, mouth, conn_edges=edges or None, **kw)
    except ValueError as e:
        raise BuildError(f"connector edges: {e}") from e


def _side_edges(comps: Sequence[Mapping], edges: Mapping[str, str]) -> set[str]:
    """The edges a side's connectors mate through (unnamed: top)."""
    named = {edges[c["ref"]] for c in comps if c["ref"] in edges}
    unnamed = any((c["ref"][:1] == "J" or c.get("edge_row")) and not c.get("interior")
                  for c in comps if c["ref"] not in edges)
    return named | ({"top"} if unnamed else set())


def cluster_origin(field: bfield.Field, used_w: float, used_h: float,
                   side_edges: set[str]) -> tuple[float, float]:
    """Top-left of a used_w x used_h cluster in `field`: board_field.cluster_origin
    (centred across the top), except that a cluster with left (right) connectors
    only hugs the field's left (right) side, with bottom but no top ones its
    bottom, and with side columns only it is centred vertically (clear of rounded
    corners), so the edge snap moves its connectors as little as possible."""
    x0, y0 = bfield.cluster_origin(field, used_w)
    horiz = side_edges & {"left", "right"}
    if horiz == {"left"}:
        x0 = field.x0
    elif horiz == {"right"}:
        x0 = field.x1 - used_w
    if "bottom" in side_edges and "top" not in side_edges:
        y0 = field.y1 - used_h
    elif horiz and side_edges <= {"left", "right"}:
        y0 = field.y0 + (field.h - used_h) / 2.0
    return x0, y0


def hole_positions(design: Mapping) -> list[tuple]:
    """Mounting holes the fields keep clear of: explicit positions, else the
    default corners of a w/h (rect / rounded rect) board block."""
    mh, board = design.get("mounting_holes"), design.get("board") or {}
    spec = mh if isinstance(mh, Mapping) else {}
    if spec.get("positions"):
        return [tuple(p) for p in spec["positions"]]
    if not mh or board.get("outline") is not None:
        return []
    return list(feat.corner_positions(float(board["w"]), float(board["h"]),
                                      float(spec.get("inset", feat.DEFAULT_HOLE_INSET_MM))))


def side_fields(design: Mapping, outline: ol.Outline | None, auto: Sequence[Mapping],
                board_wh: tuple[float, float], scale: float,
                clearance: float) -> tuple[dict, list[str]]:
    """({side: Field}, warnings): legacy_fields on a w/h board, else auto_fields."""
    board_spec = design.get("board") or {}
    has_conn = _has_conn(auto)
    try:
        if outline is None:
            return ({f.side: f for f in bfield.legacy_fields(
                board_spec, board_wh[0], board_wh[1], scale=scale, has_conn=has_conn)}, [])
        fields, warnings = bfield.auto_fields(board_spec, outline, clearance,
                                              hole_positions(design), scale=scale,
                                              has_conn=has_conn)
    except ValueError as e:
        raise BuildError(f"auto placement field: {e}") from e
    return {f.side: f for f in fields}, list(warnings)


def pack_side(design: Mapping, side: str, comps: list[dict], field: bfield.Field, parts: tuple,
              scale: float, board_wh: tuple[float, float]) -> tuple[dict, dict]:
    """One side's auto parts functionally packed into its field: (targets,
    rotations). BuildError when the cluster does not fit."""
    edges = conn_edges(design)
    packed, rots, used_w, used_h = _functional_place(
        {**design, "components": comps}, parts, edges, margin=1.5 * scale, width_limit=field.w)
    if not bfield.fits(field, used_w, used_h):
        where = (f"the free field {field.w}x{field.h} mm" if field.legacy is not None else
                 f"the {field.source} field ({field.x0:.3f}, {field.y0:.3f})-({field.x1:.3f}, "
                 f"{field.y1:.3f}), {field.w:.2f}x{field.h:.2f} mm,")
        raise BuildError(f"{side}-side auto cluster {used_w}x{used_h} mm does not fit {where} "
                         f"of the fixed board {board_wh[0]}x{board_wh[1]} mm at pack_scale {scale}")
    x0, y0 = cluster_origin(field, used_w, used_h, _side_edges(comps, edges))
    return {ref: (x0 + bx, y0 + by) for ref, (bx, by) in packed.items()}, rots


def _lift_overhangs(comps: Sequence[Mapping], targets: Mapping, fixed: set[str]) -> dict:
    """An auto "overhang" part without an "edge" moves UP by it, so its radiator
    extends past Edge.Cuts (as before); an "edge" part is snapped instead."""
    out = dict(targets)
    for c in comps:
        ov = float(c.get("overhang", 0) or 0)
        if ov and c["ref"] in out and c["ref"] not in fixed and c.get("edge") is None:
            tx, ty = out[c["ref"]]
            out[c["ref"]] = (tx, ty - ov)
    return out


def full_targets(design: Mapping, outline: ol.Outline | None,
                 dims: tuple[float, float]) -> Layout:
    """Every part fixed: x/y verbatim. The board is the outline bbox, else the
    board block's w x h, else argv / the default size."""
    board_spec = design.get("board") or {}
    targets = {c["ref"]: (c["x"], c["y"]) for c in design["components"] if not anchored(c)}
    w, h = _outline_wh(outline) or ((float(board_spec["w"]), float(board_spec["h"]))
                                    if board_spec else dims)
    return Layout(targets, {}, w, h)


def mixed_targets(design: Mapping, outline: ol.Outline | None, fixed: set[str], parts: tuple,
                  clearance: float) -> Layout:
    """MIXED placement: fixed parts where the netlist puts them, the rest packed
    into its side's field. Needs a fixed board size: a w/h block or an outline."""
    board_spec = design.get("board") or {}
    if outline is None and not board_spec:
        raise BuildError('mixed placement requires "board": {"w": mm, "h": mm}')
    board_wh = _outline_wh(outline) or (float(board_spec["w"]), float(board_spec["h"]))
    scale, comps = pack_scale(), design["components"]
    auto = [c for c in comps if c["ref"] not in fixed]
    fields, warnings = side_fields(design, outline, auto, board_wh, scale, clearance)
    b_refs = {c["ref"] for c in auto if str(c.get("side", "F")).upper() == "B"}
    targets, rots = {}, {}
    for side in SIDES:
        side_comps = [c for c in auto if (c["ref"] in b_refs) == (side == "B")]
        if not side_comps:
            continue
        if side not in fields:
            raise BuildError(f"no {side}-side auto placement field on this board"
                             + (f": {'; '.join(warnings)}" if warnings else ""))
        side_targets, side_rots = pack_side(design, side, side_comps, fields[side], parts,
                                            scale, board_wh)
        targets, rots = {**targets, **side_targets}, {**rots, **side_rots}
    targets = {**targets, **{c["ref"]: (c["x"], c["y"]) for c in comps
                             if c["ref"] in fixed and not anchored(c)}}
    return Layout(_lift_overhangs(comps, targets, fixed), rots, *board_wh,
                  tuple(fields[s] for s in SIDES if s in fields), tuple(warnings))


def auto_targets(design: Mapping, parts: tuple) -> Layout:
    """FUNCTIONAL placement, then the board sized to fit: top connectors stay
    flush; corner mounting holes need a clear ring (inset 4 + ~6.4 mm courtyard)."""
    scale, comps = pack_scale(), design["components"]
    packed, rots, used_w, used_h = _functional_place(design, parts, conn_edges(design),
                                                     margin=1.5 * scale)
    edge = 2.0 * scale  # board margin around the placed cluster
    edge_top = 1.2 if _has_conn(comps) else edge
    if design.get("mounting_holes"):
        edge = max(edge, 7.5)
    board_w = round(used_w + 2 * edge, 2)
    board_h = round(used_h + edge_top + edge, 2)
    targets = {ref: (bx + edge, by + edge_top) for ref, (bx, by) in packed.items()}
    return Layout(_lift_overhangs(comps, targets, set()), rots, board_w, board_h)


def compute_targets(design: Mapping, outline: ol.Outline | None, dims: tuple[float, float],
                    parts: tuple, edge_clearance: float) -> Layout:
    """Every part fixed: full; some fixed, or an outline board: mixed; else auto.
    `parts` = (extents, pincount, mouth) of every component; `dims` = argv size."""
    check_design(design)
    fixed, comps = fixed_refs(design), design["components"]
    notes = tuple(f"{c['ref']}: 'edge' is ignored on a fixed part ('at' or x/y place it)"
                  for c in comps if c.get("edge") is not None and c["ref"] in fixed)
    if all(c["ref"] in fixed for c in comps):
        layout = full_targets(design, outline, dims)
    elif fixed or outline is not None:
        layout = mixed_targets(design, outline, fixed, parts, edge_clearance)
    else:
        layout = auto_targets(design, parts)
    return dataclasses.replace(layout, warnings=notes + layout.warnings)


# --- placement (pcbnew) -------------------------------------------------------------------
def _vec(x: float, y: float) -> Any:
    return pcbnew.VECTOR2I(pcbnew.FromMM(float(x)), pcbnew.FromMM(float(y)))


def _place_centre(board: Any, fp: Any, c: Mapping, extent: tuple, target: tuple, rots: Mapping,
                  ox: float, oy: float) -> None:
    """The bbox centre on its target (the rot-0 offset turns with the part, KiCad
    clockwise-positive); side B flips about it after Add (parentless Flip segfaults)."""
    rot = float(c.get("rot", rots.get(c["ref"], 0)))
    if rot:
        fp.SetOrientationDegrees(rot)
    _w, _h, cxo, cyo = extent
    tx, ty = target
    a = math.radians(rot)
    rx = cxo * math.cos(a) + cyo * math.sin(a)
    ry = -cxo * math.sin(a) + cyo * math.cos(a)
    fp.SetPosition(_vec(tx - rx + ox, ty - ry + oy))
    board.Add(fp)
    if str(c.get("side", "F")).upper() == "B":
        fp.Flip(_vec(tx + ox, ty + oy), True)


def place_footprints(board: Any, design: Mapping, fps: Mapping, extents: Mapping,
                     layout: Layout, ox: float, oy: float) -> dict:
    """Add every footprint: "at" parts by origin (swig_anchor.place_at), the rest
    by bbox centre on the layout's targets. Returns {ref: footprint}."""
    placed = {}
    for c in design["components"]:
        ref, fp = c["ref"], fps[c["ref"]]
        if anchored(c):
            swig_anchor.place_at(board, fp, c["at"], c.get("side"), ox, oy, pcbnew)
        else:
            _place_centre(board, fp, c, extents[ref], layout.targets[ref], layout.rots, ox, oy)
        placed[ref] = fp
    return placed


def courtyard_polys(fp: Any, ox: float, oy: float, fallback: bool = True) -> Polys:
    """fp's courtyard outlines on its own side (board frame, mm); its bounding box
    (no texts) when it has none and `fallback`."""
    import board_outline as bo
    ps = bo.courtyard(fp, pcbnew.B_CrtYd if fp.IsFlipped() else pcbnew.F_CrtYd)
    polys = []
    for i in range(ps.OutlineCount()):
        ch = ps.COutline(i)
        pts = [(ch.CPoint(j).x / NM_PER_MM - ox, ch.CPoint(j).y / NM_PER_MM - oy)
               for j in range(ch.PointCount())]
        if len(pts) >= 3:
            polys.append(pts)
    if polys or not fallback:
        return polys
    try:
        bb = fp.GetBoundingBox(False, False)              # no texts
    except (TypeError, NotImplementedError):              # a build without that overload
        bb = fp.GetBoundingBox()
    x0, y0 = bb.GetLeft() / NM_PER_MM - ox, bb.GetTop() / NM_PER_MM - oy
    x1, y1 = bb.GetRight() / NM_PER_MM - ox, bb.GetBottom() / NM_PER_MM - oy
    return [[(x0, y0), (x1, y0), (x1, y1), (x0, y1)]]


def box_of(polys: Polys) -> Box:
    xs = [p[0] for poly in polys for p in poly]
    ys = [p[1] for poly in polys for p in poly]
    return (min(xs), min(ys), max(xs), max(ys))


def _centre(box: Box) -> tuple[float, float]:
    return ((box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0)


def _move(fp: Any, dx: float, dy: float) -> None:
    fp.Move(pcbnew.VECTOR2I(int(round(dx * NM_PER_MM)), int(round(dy * NM_PER_MM))))


# --- connector snapping and containment ------------------------------------------------
def world_dir(v: Sequence[float], rot_deg: float, flipped: bool) -> tuple[float, float]:
    """A footprint-frame (rot 0, front) direction on the board: mirrored in Y when
    the part is flipped, then turned by its displayed rotation (clockwise-positive)."""
    x, y = float(v[0]), (-float(v[1]) if flipped else float(v[1]))
    c, s = math.cos(math.radians(rot_deg)), math.sin(math.radians(rot_deg))
    return (x * c + y * s, -x * s + y * c)


def part_overhang(c: Mapping, info: em.EdgeMount | None) -> tuple[float, dict | None]:
    """(overhang mm, derived record or None): the component's "overhang" when
    given, else an edge-mount footprint's courtyard overhang beyond its marker."""
    if c.get("overhang") is not None:
        return float(c["overhang"]), None
    if info is not None and info.edge_mount and info.overhang_mm > 0:
        return info.overhang_mm, {"overhang_mm": info.overhang_mm, "source": info.source}
    return 0.0, None


def snap_target(outline: ol.Outline, box: Box, edge: str,
                overhang: float) -> tuple[float, float] | None:
    """The courtyard-bbox centre that puts `box` against the OUTER ring toward
    `edge` (board_field.snap_to_edge; cutouts ignored), `overhang` past it."""
    half = (box[2] - box[0]) / 2.0 if edge in ("left", "right") else (box[3] - box[1]) / 2.0
    return bfield.snap_to_edge(ol.Outline(outline.outer), _centre(box), edge, half, overhang)


def world_mouth(fp: Any, ox: float, oy: float) -> tuple[float, float] | None:
    """The placed part's mouth (pad centroid -> courtyard centre, snapped to an
    axis as place_engine.fp_mouth does), or None for a mouthless part."""
    polys, pads = courtyard_polys(fp, ox, oy, fallback=False), list(fp.Pads())
    if not polys or not pads:
        return None
    cx, cy = _centre(box_of(polys))
    dx = cx - (sum(p.GetPosition().x for p in pads) / len(pads) / NM_PER_MM - ox)
    dy = cy - (sum(p.GetPosition().y for p in pads) / len(pads) / NM_PER_MM - oy)
    if max(abs(dx), abs(dy)) <= MOUTHLESS:
        return None
    return (math.copysign(1.0, dx), 0.0) if abs(dx) >= abs(dy) else (0.0, math.copysign(1.0, dy))


def face_edge(fp: Any, edge: str, ox: float, oy: float) -> bool:
    """Turn fp 180 deg about its courtyard centre when its mouth points away from
    `edge` (the B-side flip mirrored the packer's rotation). True when turned."""
    mouth, (nx, ny) = world_mouth(fp, ox, oy), NORMALS[edge]
    if mouth is None or mouth[0] * nx + mouth[1] * ny >= 0.0:
        return False
    before = _centre(box_of(courtyard_polys(fp, ox, oy)))
    fp.SetOrientationDegrees(swig_anchor.normalize_rot(fp.GetOrientationDegrees() + 180.0))
    after = _centre(box_of(courtyard_polys(fp, ox, oy)))
    _move(fp, before[0] - after[0], before[1] - after[1])
    return True


def _snap_overhang(c: Mapping, fp: Any, edge: str, info: em.EdgeMount | None
                   ) -> tuple[float, list[str]]:
    """The overhang to snap with: a derived one only when the edge-mount part's
    outward side faces `edge` after placement (else flush, with a warning)."""
    overhang, derived = part_overhang(c, info)
    if derived is None:
        return overhang, []
    out = world_dir(info.outward, fp.GetOrientationDegrees(), fp.IsFlipped())
    if out[0] * NORMALS[edge][0] + out[1] * NORMALS[edge][1] >= FACING_MIN_COS:
        return overhang, []
    return 0.0, [f"{c['ref']}: its edge-mount overhang ({overhang:g} mm, {derived['source']}) "
                 f"points ({out[0]:.2f}, {out[1]:.2f}), not toward the '{edge}' edge; "
                 f"snapped flush"]


def snap_connectors(design: Mapping, placed: Mapping, outline: ol.Outline, edge_info: Mapping,
                    ox: float, oy: float) -> list[str]:
    """Every AUTO "edge" part faced to and slid onto its outer board edge: warnings."""
    fixed, warnings = fixed_refs(design), []
    for c in design["components"]:
        ref, edge = c["ref"], c.get("edge")
        if edge is None or ref in fixed:
            continue
        fp = placed[ref]
        face_edge(fp, edge, ox, oy)
        overhang, notes = _snap_overhang(c, fp, edge, edge_info.get(ref))
        box = box_of(courtyard_polys(fp, ox, oy))
        new = snap_target(outline, box, edge, overhang)
        if new is None:
            notes = [*notes, f"{ref}: no outer board edge ahead of it toward '{edge}'; left "
                             "where the packer put it"]
        else:
            centre = _centre(box)
            _move(fp, new[0] - centre[0], new[1] - centre[1])
        warnings = [*warnings, *notes]
    return warnings


def containment_issues(outline: ol.Outline, polys: Mapping[str, Polys],
                       overhangs: Mapping[str, float]) -> list[dict]:
    """outline_audit.courtyard_issues of every courtyard polygon (Issue dicts)."""
    records = [{"ref": ref, "polygon": [list(p) for p in poly],
                "overhang": overhangs.get(ref) or None}
               for ref, rings in polys.items() for poly in rings]
    return [i.to_dict() for i in outline_audit.courtyard_issues(outline, records)]


def containment(design: Mapping, placed: Mapping, outline: ol.Outline, edge_info: Mapping,
                ox: float, oy: float) -> tuple[list[dict], dict, list[str]]:
    """(issues, derived overhangs {ref: {overhang_mm, source}}, warnings): every
    placed courtyard against the outline."""
    polys, overhangs, derived = {}, {}, {}
    for c in design["components"]:
        ref = c["ref"]
        polys[ref] = courtyard_polys(placed[ref], ox, oy)
        overhangs[ref], rec = part_overhang(c, edge_info.get(ref))
        if rec is not None:
            derived[ref] = rec
    issues = containment_issues(outline, polys, overhangs)
    return issues, derived, [f"containment: {i['message']}" for i in issues]
