#!/usr/bin/env python3
"""Board outline lint rules (EO*/WO*), ORDER 30 (F3-09): the netlist ``board`` block
(ROADMAP_DECISIONS D2) and what sits on it, checked at G1 before any board is built.

  EO1  the board block does not resolve: outline_resolve.ResolveError's text (corner_radius
       > min(w, h) / 2, an open / self-crossing loop, a cutout off or crossing the outline,
       overlapping cutouts, an unreadable DXF, ...)
  EO2  a fixed part's point (x/y = courtyard-bbox centre, ``at`` = footprint ORIGIN, D3; no
       footprints here, G3 judges courtyards) inside a cutout (unless board.panel_cutouts)
       or > OFF_BOARD_REACH_MM off the material: error. Nearer past an edge (a mid-mount
       connector in a notch, as KiCad's tiny_tapeout USB-C) or on the material within
       NEAR_EDGE_MM of an edge (not an ``overhang`` part at the outer edge): warning
  EO3  mounting holes (positions, else the builder's corner insets on a rect / rounded rect;
       drill from 'MountingHole_<d>mm', else 3.2) via mounting_hole_issues: drill off the
       material (error), nearer an edge than hole_to_edge (warning); corner holes on any
       other outline (error)
  EO4  a preroute ["xy", x, y] point, the copper between two xy points or a via off the
       board or in a cutout (pad points are only known after placement)
  EO5  outline fab errors (outline_audit.fab_issues; its hole rules are EO3's). A minimum
       missed by <= outline_audit.TOL_MM is rounding (KiCad's 1 nm grid), not an error
  WO1  outline fab warnings (same slack); an assumption rule keeps its 'ASSUMPTION' marker
  WO2  auto-placed parts (no x/y, no at) and board_field.auto_fields gives no field, or an
       inscribed one under MIN_FIELD_SHARE of the board area; its notes (an explicit auto_*
       field clipped or dropped) pass through. An explicit field's size is not judged
  WO3  an antenna_keepout or a height zone reaching outside the outline bbox
  WO4  DXF reader warnings, passed through
  WO5  a DXF outline but no base_dir (the netlist directory): nothing is checked

Legacy boards (outline_resolve's is_legacy) yield ([], []): the builder keeps today's path.
Board frame (mm, Y down, origin = outline bbox top-left). Never mutates the design or
raises on bad input (that would be a LINT-INTERNAL error): a malformed point, hole, via,
keep-out or zone is reported; a non-object entry is left to the schema / core rules.
"""
from __future__ import annotations

import math
import os
import re
import sys
from collections.abc import Mapping, Sequence
from itertools import pairwise
from pathlib import Path
from typing import Any

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import board_field as bfl  # noqa: E402
import build_features as bfe  # noqa: E402  (its pure helpers; pcbnew is optional there)
import libtable  # noqa: E402
import outline as ol  # noqa: E402
import outline_audit as oa  # noqa: E402
import outline_resolve as orz  # noqa: E402
from lint_context import LintContext  # noqa: E402

__all__ = ["MIN_FIELD_SHARE", "NEAR_EDGE_MM", "NON_RECT_HOLES", "NO_BASE_DIR",
           "OFF_BOARD_REACH_MM", "ORDER", "check", "outline_lint"]

ORDER = 30
FAB_PROFILE = "JLCPCB"
NEAR_EDGE_MM = 1.0             # EO2: a fixed point this close to an edge is warned
OFF_BOARD_REACH_MM = 10.0      # EO2: an off-material point farther than this is an error
MIN_FIELD_SHARE = 0.20         # WO2: an inscribed field below this share of the board area
FIELD_CLEARANCE_MM = 0.3       # the builder's m_CopperEdgeClearance (swig_build_board)
DEFAULT_VIA_MM = 1.2           # build_features.add_preroutes' via_size default diameter
NO_BASE_DIR = "DXF outline not checked: lint ran without base_dir"
NON_RECT_HOLES = "give mounting_holes.positions for a non-rectangular outline"
FRAME = "board frame, mm"
_HOLE_NAME = re.compile(r"MountingHole_(\d+(?:\.\d+)?)mm")
_DXF_SPEC_KEYS = frozenset({"dxf", "layer", "units"})
_EO3_FAB_CODES = ("fab_hole_inside", "fab_hole_to_edge")   # mounting_hole_issues has them
_MIN_RULES = ("board_size", "slot_width", "internal_corner_radius", "feature_width",
              "hole_size", "hole_to_edge")      # validate_fab rules: a value vs a minimum

Findings = tuple[list[str], list[str]]
Point = tuple[float, float]


# --- small helpers -----------------------------------------------------------------------
def _num(v: object) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        return None
    return float(v)


def _xy(raw: object) -> Point | None:
    """(x, y) of [x, y, ...] when both are finite numbers, else None."""
    if not isinstance(raw, (list, tuple)) or len(raw) < 2:
        return None
    x, y = _num(raw[0]), _num(raw[1])
    return None if x is None or y is None else (x, y)


def _f(p: Sequence[float]) -> str:
    return "({:.3f}, {:.3f})".format(*(round(v, 3) + 0.0 for v in p))   # no '-0.000'


def _box(r: Sequence[float]) -> str:
    return "[" + ", ".join(f"{round(float(v), 4) + 0.0:g}" for v in r) + "]"


def _ring(k: int) -> str:
    return "the board outline" if k == 0 else f"cutout {k}"


def _off_material(o: ol.Outline, p: Point) -> str | None:
    """None when p is on the board (touching an edge counts), else where it is."""
    if ol.contains(o, p):
        return None
    k = ol.nearest_edge(o, p).ring
    return "off the board" if k == 0 else f"inside cutout {k}"


def _components(design: Mapping) -> list[Mapping]:
    comps = design.get("components")
    return [c for c in comps if isinstance(c, Mapping)] if isinstance(comps, list) else []


def _fixed_point(c: Mapping) -> tuple[str, object] | None:
    """(what, raw point) of a fixed part, None for an auto-placed one."""
    if c.get("at") is not None:
        return "footprint origin 'at'", c["at"]
    if "x" in c and "y" in c:
        return "courtyard-bbox centre x/y", [c["x"], c["y"]]
    return None


# --- EO2 fixed parts -------------------------------------------------------------------------
def _point_verdict(o: ol.Outline, p: Point, c: Mapping, panel: bool) -> tuple[bool, str] | None:
    """(is_error, text) for a fixed part's point; None when it is fine."""
    hit = ol.nearest_edge(o, p)
    if ol.contains(o, p):
        hangs = hit.ring == 0 and (_num(c.get("overhang")) or 0.0) > 0
        return None if hit.distance >= NEAR_EDGE_MM or hangs else (
            False, f"is {hit.distance:.3f} mm from {_ring(hit.ring)}; its courtyard may cross "
                   "it (G3 checks the courtyard)")
    if hit.ring and not panel:
        return True, f"is inside cutout {hit.ring}"
    if hit.distance > OFF_BOARD_REACH_MM:
        where = f"inside cutout {hit.ring}" if hit.ring else "off the board"
        return True, f"is {where}, {hit.distance:.3f} mm from its material"
    return False, (f"is off the material, {hit.distance:.3f} mm past {_ring(hit.ring)}: only a "
                   "part straddling that edge (a mid-mount connector in a notch, an overhang) "
                   "may sit there; G3 checks the courtyard")


def _part_findings(o: ol.Outline, design: Mapping, panel: bool) -> Findings:
    found: list[tuple[bool, str]] = []
    for c in _components(design):
        fixed = _fixed_point(c)
        if fixed is None:
            continue
        ref, (what, raw) = c.get("ref", "?"), fixed
        p = _xy(raw)
        verdict = (True, "") if p is None else _point_verdict(o, p, c, panel)
        if verdict is not None:
            found.append((verdict[0], f"EO2 {ref}: {what} {raw!r} is not a point [x, y] (mm)"
                          if p is None else f"EO2 {ref}: fixed part's {what} {_f(p)} "
                          f"{verdict[1]} ({FRAME})"))
    return [t for bad, t in found if bad], [t for bad, t in found if not bad]


# --- EO3 mounting holes ----------------------------------------------------------------------
def _hole_drill(spec: Mapping) -> float:
    name = spec.get("footprint") or libtable.DEFAULT_MOUNTING_HOLE_FOOTPRINT
    m = _HOLE_NAME.search(str(name))
    drill = float(m.group(1)) if m else 0.0
    return drill if drill > 0 else bfe.DEFAULT_HOLE_DRILL_MM


def _hole(i: int, p: Sequence[float], drill: float) -> dict:
    return {"ref": f"H{i + 1}", "center": [float(p[0]), float(p[1])], "drill": drill}


def _given_holes(given: object, drill: float) -> tuple[list[dict], list[str]]:
    if not isinstance(given, (list, tuple)):
        return [], [f"EO3 mounting_holes.positions must be a list of [x, y] (mm), "
                    f"got {given!r}"]
    points = [(i, raw, _xy(raw)) for i, raw in enumerate(given)]
    return ([_hole(i, p, drill) for i, _raw, p in points if p is not None],
            [f"EO3 mounting_holes.positions[{i}] is not [x, y] (mm): {raw!r}"
             for i, raw, p in points if p is None])


def _hole_specs(design: Mapping, source: str, w: float,
                h: float) -> tuple[list[dict], list[str], bool]:
    """(hole records {ref, center, drill}, EO3 errors, defaulted corner holes?)."""
    mh = design.get("mounting_holes")
    if not mh:
        return [], [], False
    spec = mh if isinstance(mh, Mapping) else {}
    drill = _hole_drill(spec)
    if spec.get("positions"):
        return (*_given_holes(spec["positions"], drill), False)
    if source not in bfe.RECT_SOURCES:
        return [], [f"EO3 mounting_holes has no positions: {NON_RECT_HOLES} (default corner "
                    f"holes fit a w x h or rounded-rect board; this outline is {source!r})"], False
    inset = _num(spec.get("inset", bfe.DEFAULT_HOLE_INSET_MM))
    if inset is None:
        return [], [f"EO3 mounting_holes.inset {spec.get('inset')!r} is not a number (mm)"], True
    return [_hole(i, p, drill) for i, p in enumerate(bfe.corner_positions(w, h, inset))], [], True


def _hole_findings(o: ol.Outline, holes: list[dict], defaulted: bool) -> Findings:
    hint = "; a default corner hole: give mounting_holes.positions or another inset" \
        if defaulted else ""
    issues = oa.mounting_hole_issues(o, holes, FAB_PROFILE)
    return ([f"EO3 mounting hole {i.message}{hint}" for i in issues if i.severity == "error"],
            [f"EO3 mounting hole {i.message}" for i in issues if i.severity == "warning"])


# --- EO4 preroutes ---------------------------------------------------------------------------
def _seg_width(raw: Sequence, default: object) -> float | None:
    """The width of the segment STARTING at this point (build_features._preroute_point)."""
    w = _num(raw[3]) if len(raw) > 3 else _num(default)
    return w if w is not None and w > 0 else None


def _path_points(o: ol.Outline, head: str, pr: Mapping) -> tuple[list, list[str]]:
    """([(point, width) | None per path entry: None = a pad or a bad point], errors)."""
    path = pr.get("path")
    out: list = []
    errors: list[str] = []
    for j, raw in enumerate(path if isinstance(path, (list, tuple)) else []):
        if not (isinstance(raw, (list, tuple)) and raw and raw[0] == "xy"):
            out.append(None)                    # ["pad", ref, num]: placed later
            continue
        p = _xy(raw[1:3])
        where = 'not an ["xy", x, y] point' if p is None else _off_material(o, p)
        if where is None:
            out.append((p, _seg_width(raw, pr.get("width"))))
            continue
        errors.append(f"{head} path[{j}] {_f(p) if p else repr(raw)} is {where} ({FRAME})")
        out.append(None)
    return out, errors


def _via_records(head: str, pr: Mapping, net: str) -> tuple[list[dict], list[str]]:
    size = pr.get("via_size")
    dia = _num(size[0]) if isinstance(size, (list, tuple)) and size else None
    radius = (dia if dia is not None and dia > 0 else DEFAULT_VIA_MM) / 2.0
    vias = pr.get("vias")
    points = [(k, raw, _xy(raw)) for k, raw in enumerate(vias if isinstance(vias, list) else [])]
    return ([{"kind": "via", "net": net, "center": list(p), "radius": radius}
             for _k, _raw, p in points if p is not None],
            [f"{head} vias[{k}] {raw!r} is not [x, y] (mm)" for k, raw, p in points if p is None])


def _preroute_findings(o: ol.Outline, design: Mapping) -> list[str]:
    preroutes = design.get("preroutes")
    errors: list[str] = []
    for i, pr in enumerate(preroutes if isinstance(preroutes, list) else []):
        if not isinstance(pr, Mapping):
            continue
        net = str(pr.get("net") or "")
        head = f"EO4 preroutes[{i}] (net {net!r})"
        points, bad_points = _path_points(o, head, pr)
        vias, bad_vias = _via_records(head, pr, net)
        tracks = [{"kind": "track", "net": net, "start": list(a[0]), "end": list(b[0]),
                   "width": a[1]} for a, b in pairwise(points) if a and b and a[1] is not None]
        copper = oa.copper_issues(o, tracks + vias, 0.0)
        errors += bad_points + bad_vias + [f"EO4 preroutes[{i}] {c.message}" for c in copper]
    return errors


# --- EO5 / WO1 fab rules ---------------------------------------------------------------------
def _grid_noise(prob: Mapping) -> bool:
    """A measured minimum missed by no more than outline_audit.TOL_MM (validate_fab
    compares to 1e-9 mm; KiCad's 1 nm grid and 6-decimal JSON leave ~1e-6 mm)."""
    value, limit = _num(prob.get("value")), _num(prob.get("limit"))
    return (prob.get("rule") in _MIN_RULES and value is not None and limit is not None
            and 0.0 < limit and limit - value <= oa.TOL_MM)


def _fab_findings(o: ol.Outline, holes: list[dict]) -> Findings:
    issues = oa.fab_issues(o, holes, FAB_PROFILE)       # one Issue per problem, in order
    probs = ol.validate_fab(o, FAB_PROFILE, [(*h["center"], h["drill"]) for h in holes])
    paired = len(probs) == len(issues) and all(
        i.code == f"fab_{p['rule']}" for i, p in zip(issues, probs, strict=True))
    noise = [_grid_noise(p) for p in probs] if paired else [False] * len(issues)
    kept = [i for i, n in zip(issues, noise, strict=True)
            if not n and i.code not in _EO3_FAB_CODES]
    return ([f"EO5 {i.message}" for i in kept if i.severity == "error"],
            [f"WO1 {i.message}" for i in kept if i.severity == "warning"])


# --- WO2 auto-placement fields ---------------------------------------------------------------
def _side(c: Mapping) -> str:
    return "B" if str(c.get("side", "F")).upper() == "B" else "F"


def _relevant(notes: Sequence[str], sides: set[str]) -> list[str]:
    """auto_fields' notes, minus those about the explicit field of a side without parts."""
    idle = tuple(f"explicit {s} " for s in bfl.SIDES if s not in sides)
    return [n for n in notes if not (idle and n.startswith(idle))]


def _field_findings(resolved: orz.ResolvedBoard, design: Mapping, holes: list[dict]) -> list[str]:
    autos = [c for c in _components(design) if _fixed_point(c) is None]
    if not autos:
        return []
    has_conn = any(str(c.get("ref", ""))[:1] == "J" and not c.get("interior") for c in autos)
    rows = [(h["center"][0], h["center"][1], h["drill"]) for h in holes]
    try:
        fields, notes = bfl.auto_fields(resolved.block, resolved.outline, FIELD_CLEARANCE_MM,
                                        rows, has_conn=has_conn)
    except (TypeError, ValueError) as e:                 # OutlineError is a ValueError
        return [f"WO2 auto-placement fields not computed: {e}"]
    notes = _relevant(notes, {_side(c) for c in autos})
    if not fields:
        return [f"WO2 {len(autos)} auto-placed part(s) but no auto-placement field: "
                + ("; ".join(notes) or bfl.NO_FIELD)]
    area = ol.area(resolved.outline)
    small = [f for f in fields if f.source == "inscribed" and f.area < MIN_FIELD_SHARE * area]
    return [f"WO2 {note}" for note in notes] + [
        f"WO2 the inscribed auto-placement field {_box(f.rect)} is "
        f"{100.0 * f.area / area:.0f}% of the {area:.1f} mm^2 board (under "
        f"{MIN_FIELD_SHARE:.0%}) for {len(autos)} auto-placed part(s): fix more parts or give "
        f"board.auto_* ({FRAME})" for f in small[:1]]


# --- WO3 keep-outs and height zones ----------------------------------------------------------
def _zone_rect(z: object) -> tuple | None:
    if not isinstance(z, Mapping):
        return None
    rect = tuple(_num(z.get(k)) for k in ("x", "y", "w", "h"))
    return None if None in rect else rect


def _bbox_findings(design: Mapping, w: float, h: float) -> list[str]:
    tail = f"extends outside the outline bbox 0..{w:g} x 0..{h:g} mm (board frame)"
    out: list[str] = []
    ako = design.get("antenna_keepout")
    if isinstance(ako, Mapping) and ako:
        try:
            rect = bfe.keepout_rect(ako, w, h)
            leaves = bfe.leaves_bbox(rect, w, h)
        except (TypeError, ValueError) as e:
            out.append(f"WO3 antenna_keepout {ako!r} not checked: {e}")
        else:
            out += [f"WO3 antenna_keepout {_box(rect)} {tail}"] if leaves else []
    zones = design.get("height_zones")
    for i, z in enumerate(zones if isinstance(zones, list) else []):
        rect = _zone_rect(z)
        if rect is None:
            out.append(f"WO3 height_zones[{i}] {z!r} not checked: it needs numeric x, y, w, h")
        elif bfe.leaves_bbox(rect, w, h):
            name = f" {z['name']!r}" if z.get("name") else ""
            out.append(f"WO3 height_zones[{i}]{name} {_box(rect)} {tail}")
    return out


# --- entry points ----------------------------------------------------------------------------
def _dxf_without_base(design: Mapping, base_dir: object) -> str | None:
    """WO5 for a well-formed dxf outline spec and no base_dir (anything else: resolve)."""
    board = design.get("board")
    spec = board.get("outline") if isinstance(board, Mapping) else None
    if base_dir is not None or not isinstance(spec, Mapping) or "dxf" not in spec \
            or not set(spec) <= _DXF_SPEC_KEYS:
        return None
    return (f"WO5 {NO_BASE_DIR} (board.outline.dxf {spec['dxf']!r} is relative to the "
            "netlist directory)")


def _resolved_findings(design: Mapping, resolved: orz.ResolvedBoard) -> Findings:
    o = resolved.outline
    w, h = orz.outline_bbox_dims(resolved)
    holes, hole_errors, defaulted = _hole_specs(design, resolved.source, w, h)
    panel = bool(resolved.block.get("panel_cutouts"))
    parts, placed_holes, fab = (_part_findings(o, design, panel),
                                _hole_findings(o, holes, defaulted), _fab_findings(o, holes))
    errors = parts[0] + hole_errors + placed_holes[0] + _preroute_findings(o, design) + fab[0]
    warnings = (parts[1] + placed_holes[1] + fab[1] + _field_findings(resolved, design, holes)
                + _bbox_findings(design, w, h)
                + [f"WO4 DXF {resolved.dxf_path}: {note}" for note in resolved.warnings])
    return errors, warnings


def outline_lint(design: Mapping, base_dir: str | os.PathLike | None = None) -> Findings:
    """(errors, warnings) of the outline rules (module doc) for `design`; `base_dir`
    is the netlist directory (a DXF outline resolves against it). Never mutates."""
    if isinstance(design, Mapping):
        skipped = _dxf_without_base(design, base_dir)
        if skipped is not None:
            return [], [skipped]
    try:
        resolved = orz.resolve_board(design, base_dir)
    except orz.ResolveError as e:
        return [f"EO1 board outline: {e}"], []
    if resolved.is_legacy:
        return [], []
    return _resolved_findings(design, resolved)


def check(design: Mapping[str, Any], ctx: LintContext) -> Findings:
    """lint_netlist's rule entry point: the base_dir is ctx.netlist_dir."""
    return outline_lint(design, base_dir=ctx.netlist_dir or None)
