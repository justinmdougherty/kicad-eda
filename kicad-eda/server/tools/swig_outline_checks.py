#!/usr/bin/env python3
"""The outline gates of swig_gate_checks (F3-13b): a pcbnew board -> outline_audit
records in the BOARD FRAME (mm, Y down, origin = the Edge.Cuts bbox top-left; sheet =
board + origin) -> findings with stable codes.

KiCad's Python (stdlib + pcbnew, 3.11); in the dev venv pcbnew is None and only the
pure helpers work. swig_gate_checks imports this module inside its functions, never
at import time: tests import swig_gate_checks with a stub pcbnew for the import only,
and this module (like board_outline) binds pcbnew when first imported.

Codes (swig_gate_checks.main prints them in "codes" / "warning_codes", parallel to
"issues" / "warnings"; a connector's longer outline text goes to "details"):
  outline_open        Edge.Cuts shapes KiCad cannot close into the board polygon (blocker)
  outline_missing     no Edge.Cuts at all: the outline rules are skipped (warning)
  outline_unmodelled  KiCad closes it, outline_from_board cannot: skipped (warning)
  courtyard_outside   a courtyard off the board / in a cutout / past its overhang: the
                      netlist "overhang", else the one an edge-mount footprint derives
                      from its 'PCB Edge' marker (edge_mount, F3-25)
  pad_edge_clearance  a pad off the board or nearer an edge than the board's
                      copper-to-edge clearance (an edge-mount part's pads: clearance 0,
                      they run to the edge the part straddles)
  hole_edge           a mounting-hole drill (refs H<digit>, or footprints whose pads are
                      all NPTH) off the board (blocker) or nearer the edge than the fab's
                      hole_to_edge (warning); never judged by its courtyard
  fab_outline         outline_audit.fab_issues for FAB_PROFILE: errors block, warnings warn
  connector_edge      connector_verdict (swig_gate_checks supplies _world_mouth's mouth):
                      the mouth face's nearest edge, else the edge it faces along the mouth
  edge_mount_off_edge an edge-mount part's marker > 0.25 mm from the outline (edge_mount):
                      a blocker, a warning naming the "edge" key when the netlist leaves
                      the part to the auto placer (edge_mount_severity)
  plane_islands       a plane that fills as > 1 island on a board with cutouts or a
                      non-convex outline (warning, GND included)
  plane_islands_unread pcbnew could not hand a zone layer's fill back (warning)
  copper_outside      G4: a track / arc / via / pad off the material - KiCad DRC does not
                      flag copper lying wholly inside an Edge.Cuts hole
"""
from __future__ import annotations

import math
import os
import re
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import kicad_quiet  # noqa: E402,F401  (before pcbnew: no modal wx dialogs)

try:
    import pcbnew  # noqa: E402
except ImportError:          # the dev venv: the pure helpers only
    pcbnew = None

import board_outline as bo  # noqa: E402
import edge_mount as em  # noqa: E402
import outline as ol  # noqa: E402
import outline_audit as oa  # noqa: E402

Point = tuple[float, float]
Box = tuple[float, float, float, float]

SEVERITIES = ("error", "warning")
OUTLINE_OPEN = "outline_open"
OUTLINE_MISSING = "outline_missing"
OUTLINE_UNMODELLED = "outline_unmodelled"
COURTYARD_OUTSIDE = "courtyard_outside"
PAD_EDGE_CLEARANCE = "pad_edge_clearance"
HOLE_EDGE = "hole_edge"
FAB_OUTLINE = "fab_outline"
CONNECTOR_EDGE = "connector_edge"
EDGE_MOUNT_OFF_EDGE = "edge_mount_off_edge"
EDGE_MOUNT_UNREAD = "edge_mount_unread"
COPPER_OUTSIDE = "copper_outside"
PLANE_ISLANDS = "plane_islands"
PLANE_ISLANDS_UNREAD = "plane_islands_unread"
# netlist keys that say who chose a part's position (edge_mount_severity)
PLACEMENT_KEYS = ("edge", "overhang", "at", "x", "y")
_AUDIT_CODES = (("courtyard_", COURTYARD_OUTSIDE), ("pad_", PAD_EDGE_CLEARANCE),
                ("copper_", COPPER_OUTSIDE), ("hole_", HOLE_EDGE), ("fab_", FAB_OUTLINE),
                ("connector_edge", CONNECTOR_EDGE))
DEFAULT_FAB = "JLCPCB"
PAD_MAX_ERROR_MM = 0.005        # pad polygons: chord error, inside the true shape
MIN_ISLAND_MM2 = 5.0            # smaller fill slivers are not islands (as check_plane_integrity)
_HOLE_REF = re.compile(r"H\d")
_MARKER_LAYERS = (("Dwgs_User", "Dwgs.User"), ("Cmts_User", "Cmts.User"))
_CONVEX_TOL = 1e-9
MISSING_TEXT = ("board: no Edge.Cuts outline; the outline checks (courtyards, pads, mounting "
                "holes, edge-mount parts, copper containment) were skipped and the connector "
                "rule used the board's bounding box")


# --- findings ----------------------------------------------------------------------------
@dataclass(frozen=True)
class Finding:
    """One gate finding: stable code, 'error' (a blocker) | 'warning', the text that
    goes in issues / warnings, and an optional longer detail (a connector's
    outline_audit message naming the nearest edge)."""
    code: str
    severity: str
    text: str
    detail: str | None = None

    def __post_init__(self) -> None:
        if self.severity not in SEVERITIES:
            raise ValueError(f"Finding severity must be one of {SEVERITIES}, "
                             f"got {self.severity!r}")


def gate_code(audit_code: str, routed: bool = False) -> str:
    """The swig_gate_checks code of an outline_audit code; a pad off the board is
    copper_outside at the routed stage."""
    if routed and audit_code.startswith("pad_"):
        return COPPER_OUTSIDE
    for prefix, code in _AUDIT_CODES:
        if audit_code.startswith(prefix):
            return code
    raise ValueError(f"no gate code for outline_audit code {audit_code!r}")


def from_audit(issues: Iterable[oa.Issue], routed: bool = False) -> list[Finding]:
    """outline_audit Issues as findings (severity and message kept)."""
    return [Finding(gate_code(i.code, routed), i.severity, i.message) for i in issues]


def tagged(code: str, severity: str, texts: Iterable[str]) -> list[Finding]:
    """Plain texts of one legacy check as findings with one code."""
    return [Finding(code, severity, t) for t in texts]


def split(findings: Iterable[Finding]) -> tuple[list[str], list[str]]:
    """(issue texts, warning texts) in order."""
    found = list(findings)
    return ([f.text for f in found if f.severity == "error"],
            [f.text for f in found if f.severity == "warning"])


def to_json(findings: Iterable[Finding]) -> dict:
    """main()'s JSON: {issues, warnings} as before plus parallel codes, warning_codes
    and details (None where an issue has no longer text)."""
    found = list(findings)
    errors = [f for f in found if f.severity == "error"]
    warnings = [f for f in found if f.severity == "warning"]
    return {"issues": [f.text for f in errors], "warnings": [f.text for f in warnings],
            "codes": [f.code for f in errors], "warning_codes": [f.code for f in warnings],
            "details": [f.detail for f in errors]}


# --- frames -----------------------------------------------------------------------------
@dataclass(frozen=True)
class Placement:
    """A footprint's origin (sheet mm), displayed rotation (deg, CCW on screen) and side."""
    x: float
    y: float
    rot: float
    flipped: bool


def _turn(rot_deg: float) -> tuple[float, float]:
    a = math.radians(rot_deg)
    return math.cos(a), math.sin(a)


def to_world(p: Sequence[float], pl: Placement) -> Point:
    """A footprint-frame point (rotation 0, front side) on the sheet: mirrored in Y
    when the part is flipped, turned by its displayed rotation, moved to its origin
    (build_layout.world_dir's convention, measured on 9.0.4 and 10.0.5)."""
    c, s = _turn(pl.rot)
    x, y = float(p[0]), (-float(p[1]) if pl.flipped else float(p[1]))
    return (pl.x + x * c + y * s, pl.y - x * s + y * c)


def to_local(p: Sequence[float], pl: Placement) -> Point:
    """The inverse of to_world: a sheet point in the footprint's own frame."""
    c, s = _turn(pl.rot)
    dx, dy = float(p[0]) - pl.x, float(p[1]) - pl.y
    x, y = dx * c - dy * s, dx * s + dy * c
    return (x, -y if pl.flipped else y)


def _bbox(pts: Sequence[Point]) -> Box:
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return (min(xs), min(ys), max(xs), max(ys))


def _in_board(pts: Iterable[Point], origin: Point) -> list[list[float]]:
    return [[x - origin[0], y - origin[1]] for x, y in pts]


def face_point(box: Sequence[float], mouth: Sequence[float]) -> Point:
    """The middle of the courtyard box (x0, y0, x1, y1) face an axis mouth opens through."""
    x0, y0, x1, y1 = (float(v) for v in box)
    mx, my = mouth
    if mx and not my:
        return (x1 if mx > 0 else x0, (y0 + y1) / 2.0)
    if my and not mx:
        return ((x0 + x1) / 2.0, y1 if my > 0 else y0)
    raise ValueError(f"a connector mouth must point along an axis, got {mouth!r}")


def is_shaped(outline: ol.Outline) -> bool:
    """Cutouts or a non-convex outer ring: where a plane's fill may split."""
    return bool(outline.cutouts) or not _convex(ol.flatten(outline)[0])


def _convex(pts: Sequence[Point]) -> bool:
    """A closed math-CCW polyline (outline.flatten's outer ring) never turns right."""
    ring = [p for i, p in enumerate(pts) if i == 0 or math.dist(p, pts[i - 1]) > 1e-9]
    if len(ring) > 1 and math.dist(ring[0], ring[-1]) <= 1e-9:
        ring = ring[:-1]
    for i in range(len(ring)):
        a, b, c = ring[i - 1], ring[i], ring[(i + 1) % len(ring)]
        u, v = (b[0] - a[0], b[1] - a[1]), (c[0] - b[0], c[1] - b[1])
        if u[0] * v[1] - u[1] * v[0] < -_CONVEX_TOL * math.hypot(*u) * math.hypot(*v):
            return False
    return True


# --- connectors and edge-mount parts (pure) -----------------------------------------------
@dataclass(frozen=True)
class ConnectorVerdict:
    """kind 'inward' | 'far', the distance its text reports (mm: to the mouth
    face's nearest edge, or along the mouth to the edge it faces) and the longer
    message naming those edges (outline_audit's, or _faced_detail's)."""
    kind: str
    dist: float
    detail: str


def connector_verdict(outline: ol.Outline, ref: str, box: Sequence[float],
                      mouth: Sequence[float], panel_cutouts: bool = False
                      ) -> ConnectorVerdict | None:
    """The G3 connector rule for a courtyard box (board frame) and its axis mouth:
    outline_audit.connector_edge_issue judges the mouth face against its NEAREST
    edge. A face already off the material (the body hangs past the edge, e.g. an
    edge SMA) is at the edge: only its facing is judged. When the nearest edge does
    not face the mouth, the edge the mouth looks at (faced_edge) still seats it
    within 3 mm, so a part tucked into a corner is judged by the side it faces, as
    the old bbox rule did; past 3 mm it is 'far' when the mouth points away from
    the outline's centre (the old rule's facing test), else 'inward'."""
    face = face_point(box, mouth)
    on = ol.contains(outline, face)
    limit = oa.CONNECTOR_MAX_DIST_MM if on else math.inf
    issue = oa.connector_edge_issue(outline, ref, face, mouth, limit, panel_cutouts)
    if issue is None:
        return None
    nearest = ol.nearest_edge(outline, face).distance
    if issue.message.startswith(f"{ref}: mating face is "):
        return ConnectorVerdict("far", nearest, issue.message)
    hit = faced_edge(outline, face, mouth, panel_cutouts) if on else None
    if hit is not None and hit.distance <= limit + 1e-9:
        return None
    if hit is not None and _away_from_centre(outline, box, mouth):
        return ConnectorVerdict("far", hit.distance, _faced_detail(outline, ref, face, mouth, hit))
    return ConnectorVerdict("inward", nearest, issue.message)


def faced_edge(outline: ol.Outline, face: Point, mouth: Sequence[float],
               panel_cutouts: bool = False) -> Any:
    """The first edge the ray from the mouth face along the mouth crosses
    (outline.EdgeHit), when it counts (the outer ring, or a cutout with
    board.panel_cutouts) and its outward normal is within 45 deg of the mouth;
    else None."""
    hit = ol.ray_to_edge(outline, face, mouth)
    if hit is None or not (hit.ring == 0 or panel_cutouts):
        return None
    norm = math.hypot(float(mouth[0]), float(mouth[1]))
    cos = (mouth[0] * hit.normal[0] + mouth[1] * hit.normal[1]) / norm
    return hit if cos >= oa.FACING_MIN_COS - 1e-9 else None


def _away_from_centre(outline: ol.Outline, box: Sequence[float], mouth: Sequence[float]) -> bool:
    """The old rule's facing test: seen from the courtyard centre, the mouth points
    away from the centre of the outline's bounding box."""
    x0, y0, x1, y1 = ol.bbox(outline)
    dx = (float(box[0]) + float(box[2])) / 2.0 - (x0 + x1) / 2.0
    dy = (float(box[1]) + float(box[3])) / 2.0 - (y0 + y1) / 2.0
    return mouth[0] * dx + mouth[1] * dy > 0


def _xy(p: Sequence[float]) -> str:
    return "({:.3f}, {:.3f})".format(*(round(float(v), 3) + 0.0 for v in p))   # no '-0.000'


def _faced_detail(outline: ol.Outline, ref: str, face: Point, mouth: Sequence[float],
                  hit: Any) -> str:
    """The far-from-its-edge text plus the edge the mouth faces and the nearest one."""
    near = ol.nearest_edge(outline, face)
    ring = "the board outline" if hit.ring == 0 else f"cutout {hit.ring}"
    return (f"{ref}: mating face is {hit.distance:.1f} mm from the board edge it faces (must "
            f"be at the edge, <={oa.CONNECTOR_MAX_DIST_MM:g} mm); the edge it faces: {ring} at "
            f"{_xy(hit.point)}, outward normal {_xy(hit.normal)}, along the mouth {_xy(mouth)} "
            f"from {_xy(face)}; nearest edge: {_xy(near.point)}, {near.distance:.1f} mm away "
            f"({oa.FRAME})")


def edge_mount_severity(comp: Mapping | None) -> str:
    """How an edge-mount part off the edge is judged. 'error' when the netlist
    chose its position or asked for an edge ("edge", "overhang", "at", "x" / "y"
    set) or does not list it; 'warning' when the netlist leaves it to the auto
    placer, which packs it as an ordinary part unless an "edge" key asks for an
    edge (build_layout): the warning names that key (F3-13b review, round 1)."""
    if comp is None or any(comp.get(k) is not None for k in PLACEMENT_KEYS):
        return "error"
    return "warning"


def edge_mount_verdict(ref: str, info: em.EdgeMount, pl: Placement, outline: ol.Outline,
                       origin: Point, severity: str = "error") -> Finding | None:
    """edge_mount_off_edge when an edge-mount part's marker (EdgeMount.anchor_mm,
    footprint frame) lies more than 0.25 mm from the outline: the straddle text,
    plus (as a warning, edge_mount_severity) the netlist key that seats it."""
    if severity not in SEVERITIES:
        raise ValueError(f"edge_mount_verdict severity must be one of {SEVERITIES}, "
                         f"got {severity!r}")
    if not info.edge_mount or info.anchor_mm is None:
        return None
    sx, sy = to_world(info.anchor_mm, pl)
    at = (sx - origin[0], sy - origin[1])
    text = em.straddle_issue(ref, ol.distance_to_edge(outline, at))
    if text is None:
        return None
    seat = (f"seat the marker on the board edge with the body overhanging it by "
            f"{info.overhang_mm:g} mm")
    if severity == "warning":
        return Finding(EDGE_MOUNT_OFF_EDGE, "warning", (
            f"{text}; it is auto-placed (no \"edge\", \"at\" or x/y in the netlist): give it "
            f"\"edge\": \"top\" | \"bottom\" | \"left\" | \"right\" so the builder can {seat}"))
    return Finding(EDGE_MOUNT_OFF_EDGE, "error", text,
                   f"{text}: its {info.source} edge marker is at ({at[0]:.3f}, {at[1]:.3f}) "
                   f"(board frame, mm); {seat}")


def part_overhang(comp: Mapping | None, info: em.EdgeMount | None) -> float | None:
    """How far a courtyard may hang past the OUTER edge: the netlist "overhang" (a
    number >= 0), else an edge-mount footprint's derived overhang, else None."""
    given = (comp or {}).get("overhang")
    if isinstance(given, (int, float)) and not isinstance(given, bool) \
            and math.isfinite(given) and given >= 0:
        return float(given)
    if info is not None and info.edge_mount and info.overhang_mm > 0:
        return info.overhang_mm
    return None


def pad_clearance(info: em.EdgeMount | None, clearance: float) -> float:
    """The pad-to-edge clearance a part is judged with at G3: 0 for an edge-mount
    part (an edge-launch footprint's pads run to the edge it straddles, e.g.
    SMA_Amphenol_132289's end at it; they must still lie on the material), else
    the board's copper-to-edge clearance."""
    return 0.0 if info is not None and info.edge_mount else clearance


def fab_profile() -> tuple[str, list[Finding]]:
    """FAB_PROFILE's outline limits, else JLCPCB's with a warning."""
    name = os.environ.get("FAB_PROFILE", DEFAULT_FAB).upper()
    if name in ol.FAB_LIMITS:
        return name, []
    return DEFAULT_FAB, [Finding(FAB_OUTLINE, "warning", f"fab profile {name!r} has no outline "
                                 f"limits; the outline was judged as {DEFAULT_FAB}")]


# --- reading the board (pcbnew) ------------------------------------------------------------
@dataclass(frozen=True)
class BoardShape:
    """The Edge.Cuts outline (board frame), its sheet origin, the copper-to-edge
    clearance (mm) and what reading it found. outline None: the outline rules are
    skipped and the connector rule uses the Edge.Cuts bounding box."""
    outline: ol.Outline | None
    origin: Point | None
    clearance: float = 0.0
    findings: tuple[Finding, ...] = ()


NO_SHAPE = BoardShape(None, None)


def read_shape(board: Any) -> BoardShape:
    """The board's outline, or why there is none. A stand-in board without
    GetDrawings (the unit tests' fakes), or no pcbnew, gives NO_SHAPE silently."""
    if pcbnew is None or not hasattr(board, "GetDrawings"):
        return NO_SHAPE
    if not bo.edge_shapes(board):
        return BoardShape(None, None, 0.0, (Finding(OUTLINE_MISSING, "warning", MISSING_TEXT),))
    try:
        bo.polygon_outlines(board)
    except bo.OutlineBuildError as e:
        return BoardShape(None, None, 0.0, (Finding(
            OUTLINE_OPEN, "error", f"board outline: {e}: KiCad cannot close Edge.Cuts into the "
            f"board polygon (a gap over 0.01 mm, an overlap or a stray Edge.Cuts shape); close "
            f"it, then re-run the outline checks"),))
    try:
        outline, origin = bo.outline_from_board(board)
    except bo.OutlineBuildError as e:
        return BoardShape(None, None, 0.0, (Finding(
            OUTLINE_UNMODELLED, "warning", f"board outline: KiCad closes Edge.Cuts but the "
            f"outline model cannot ({e}); the outline checks were skipped and the connector "
            f"rule used the board's bounding box"),))
    return BoardShape(outline, origin, bo.edge_clearance_mm(board))


def _mm(v: Any) -> Point:
    return (pcbnew.ToMM(v.x), pcbnew.ToMM(v.y))


def placement_of(fp: Any) -> Placement:
    x, y = _mm(fp.GetPosition())
    return Placement(x, y, float(fp.GetOrientationDegrees()), bool(fp.IsFlipped()))


def _chains(ps: Any) -> list[list[Point]]:
    """Every outline of a SHAPE_POLY_SET in sheet mm (holes ignored)."""
    out = []
    for i in range(ps.OutlineCount()):
        ch = ps.COutline(i)
        pts = [_mm(ch.CPoint(j)) for j in range(ch.PointCount())]
        if len(pts) >= 3:
            out.append(pts)
    return out


def courtyard_chains(fp: Any) -> list[list[Point]]:
    """fp's courtyard on its own side (sheet mm); [] when it has none."""
    return _chains(bo.courtyard(fp, pcbnew.B_CrtYd if fp.IsFlipped() else pcbnew.F_CrtYd))


def pad_chains(pad: Any) -> list[list[Point]]:
    """A pad's copper on its first copper layer (sheet mm); [] for an NPTH hole."""
    layers = list(pad.GetLayerSet().CuStack())
    if not layers or pad.GetAttribute() == pcbnew.PAD_ATTRIB_NPTH:
        return []
    ps = pcbnew.SHAPE_POLY_SET()
    pad.TransformShapeToPolygon(ps, layers[0], 0, bo.nm(PAD_MAX_ERROR_MM), pcbnew.ERROR_INSIDE)
    return _chains(ps)


def _pad_records(fp: Any, origin: Point) -> list[dict]:
    return [{"ref": fp.GetReference(), "pad": str(pad.GetNumber()),
             "polygon": _in_board(chain, origin)} for pad in fp.Pads() for chain in pad_chains(pad)]


def is_mounting_hole(fp: Any) -> bool:
    """A ref H<digit> (build_features' H1..Hn), or every pad an NPTH."""
    if _HOLE_REF.match(fp.GetReference()):
        return True
    pads = list(fp.Pads())
    return bool(pads) and all(p.GetAttribute() == pcbnew.PAD_ATTRIB_NPTH for p in pads)


def hole_records(fp: Any, origin: Point) -> list[dict]:
    """{ref, center (board frame), drill} of every drilled pad (an oval: its long side)."""
    out = []
    for pad in fp.Pads():
        size = pad.GetDrillSize()
        drill = max(pcbnew.ToMM(size.x), pcbnew.ToMM(size.y))
        if drill > 0:
            out.append({"ref": fp.GetReference(), "drill": drill,
                        "center": _in_board([_mm(pad.GetPosition())], origin)[0]})
    return out


def _local_angle(text: Any, pl: Placement) -> float:
    world = (text.GetTextAngleDegrees() if hasattr(text, "GetTextAngleDegrees")
             else text.GetTextAngle().AsDegrees())
    local = float(world) - pl.rot
    return (-local if pl.flipped else local) % 360.0


def _marker_prims(fp: Any, pl: Placement) -> list[dict]:
    """Dwgs.User / Cmts.User segments and texts in the footprint's frame."""
    layers = {getattr(pcbnew, attr): name for attr, name in _MARKER_LAYERS}
    out = []
    for it in fp.GraphicalItems():
        layer = layers.get(it.GetLayer())
        if layer is None:
            continue
        if it.GetClass() == "PCB_SHAPE" and it.GetShape() == pcbnew.SHAPE_T_SEGMENT:
            (x0, y0), (x1, y1) = (to_local(_mm(v), pl) for v in (it.GetStart(), it.GetEnd()))
            out.append({"kind": "line", "layer": layer, "x0": x0, "y0": y0, "x1": x1, "y1": y1})
        elif it.GetClass() == "PCB_TEXT":
            x, y = to_local(_mm(it.GetPosition()), pl)
            out.append({"kind": "text", "layer": layer, "x": x, "y": y,
                        "rot": _local_angle(it, pl), "text": str(it.GetText())})
    return out


def _pad_prims(fp: Any, pl: Placement) -> list[dict]:
    """Every pad's bounding box, read back in the footprint's frame."""
    out = []
    for pad in fp.Pads():
        bb = pad.GetBoundingBox()
        x0, y0, x1, y1 = (pcbnew.ToMM(v) for v in (bb.GetLeft(), bb.GetTop(), bb.GetRight(),
                                                    bb.GetBottom()))
        lx0, ly0, lx1, ly1 = _bbox([to_local(p, pl) for p in ((x0, y0), (x1, y0), (x1, y1),
                                                               (x0, y1))])
        out.append({"kind": "pad", "x": (lx0 + lx1) / 2, "y": (ly0 + ly1) / 2,
                    "w": lx1 - lx0, "h": ly1 - ly0})
    return out


def edge_mount_info(fp: Any, pl: Placement) -> em.EdgeMount:
    """edge_mount.edge_from_primitives of a PLACED footprint read back in its own
    frame (marker graphics, pad boxes, courtyard bbox through to_local). Raises
    ValueError on primitives edge_mount refuses."""
    pts = [to_local(p, pl) for chain in courtyard_chains(fp) for p in chain]
    return em.edge_from_primitives([*_marker_prims(fp, pl), *_pad_prims(fp, pl)],
                                   _bbox(pts) if pts else None,
                                   name=str(fp.GetFPID().GetLibItemName()),
                                   tags=str(fp.GetKeywords()))


def _edge_mount(fp: Any, pl: Placement) -> tuple[em.EdgeMount, list[Finding]]:
    try:
        return edge_mount_info(fp, pl), []
    except ValueError as e:
        return em.NOT_EDGE_MOUNT, [Finding(EDGE_MOUNT_UNREAD, "warning", f"{fp.GetReference()}: "
                                           f"edge-mount detection failed ({e}); judged as not "
                                           f"edge-mount")]


# --- the G3 / G4 rules ---------------------------------------------------------------------
@dataclass(frozen=True)
class _Survey:
    parts: tuple
    pads: tuple             # (clearance, records) per footprint
    holes: tuple
    findings: tuple


def _survey(board: Any, design: Mapping, shape: BoardShape) -> _Survey:
    """Every footprint's courtyard, pad and drill records plus its edge-mount verdict."""
    comps = {c["ref"]: c for c in design.get("components") or []
             if isinstance(c, Mapping) and "ref" in c}
    parts, pads, holes, found = [], [], [], []
    for fp in board.GetFootprints():
        if is_mounting_hole(fp):
            holes += hole_records(fp, shape.origin)
            continue
        ref, pl = fp.GetReference(), placement_of(fp)
        info, notes = _edge_mount(fp, pl)
        hit = edge_mount_verdict(ref, info, pl, shape.outline, shape.origin,
                                 edge_mount_severity(comps.get(ref)))
        found += notes + ([] if hit is None else [hit])
        overhang = part_overhang(comps.get(ref), info)
        parts += [{"ref": ref, "polygon": _in_board(chain, shape.origin), "overhang": overhang}
                  for chain in courtyard_chains(fp)]
        pads.append((pad_clearance(info, shape.clearance), _pad_records(fp, shape.origin)))
    return _Survey(tuple(parts), tuple(pads), tuple(holes), tuple(found))


def placement_findings(board: Any, design: Mapping, shape: BoardShape) -> list[Finding]:
    """The G3 outline rules: edge-mount straddle, courtyards, pads, mounting-hole
    drills and the fab outline rules; [] without an outline (connectors:
    connector_verdict, from swig_gate_checks)."""
    if shape.outline is None:
        return []
    survey = _survey(board, design, shape)
    profile, found = fab_profile()
    pads = [f for clearance, records in survey.pads
            for f in from_audit(oa.pad_issues(shape.outline, records, clearance))]
    return [*survey.findings,
            *from_audit(oa.courtyard_issues(shape.outline, survey.parts)), *pads,
            *from_audit(oa.mounting_hole_issues(shape.outline, survey.holes, profile)),
            *from_audit(oa.fab_issues(shape.outline, None, profile)), *found]


def _collinear(a: Sequence[float], b: Sequence[float], c: Sequence[float]) -> bool:
    return abs((b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])) < 1e-9


def _item_record(t: Any, origin: Point) -> dict | None:
    """A track / arc / via as an outline_audit copper record (None: no copper)."""
    from swig_copper import via_w        # here: swig_copper imports pcbnew unguarded
    net = str(t.GetNetname())
    if t.GetClass() == "PCB_VIA":
        r = pcbnew.ToMM(via_w(t)) / 2.0
        return {"kind": "via", "net": net, "radius": r,
                "center": _in_board([_mm(t.GetPosition())], origin)[0]} if r > 0 else None
    width = pcbnew.ToMM(t.GetWidth())
    if width <= 0:
        return None
    start, end = _in_board([_mm(t.GetStart()), _mm(t.GetEnd())], origin)
    rec = {"kind": "track", "net": net, "start": start, "end": end, "width": width}
    if t.GetClass() != "PCB_ARC":
        return rec
    mid = _in_board([_mm(t.GetMid())], origin)[0]
    return {**rec, "kind": "arc", **({} if _collinear(start, mid, end) else {"mid": mid})}


def routed_findings(board: Any, shape: BoardShape) -> list[Finding]:
    """G4 copper containment: every track / arc / via / pad on the material. Edge
    clearance 0 here: kicad-cli DRC owns the copper-to-edge clearance."""
    if shape.outline is None:
        return []
    items = [r for r in (_item_record(t, shape.origin) for t in board.GetTracks()) if r]
    pads = [r for fp in board.GetFootprints() for r in _pad_records(fp, shape.origin)]
    return (from_audit(oa.copper_issues(shape.outline, items, 0.0), routed=True)
            + from_audit(oa.pad_issues(shape.outline, pads, 0.0), routed=True))


def _islands(filled: Any) -> int:
    """Islands of at least MIN_ISLAND_MM2 in a zone layer's fill (an unfilled
    layer's fill is an empty SHAPE_POLY_SET: 0)."""
    polys = pcbnew.SHAPE_POLY_SET(filled)
    return sum(1 for i in range(polys.OutlineCount())
               if abs(polys.Outline(i).Area()) / 1e12 >= MIN_ISLAND_MM2)


def _zone_islands(board: Any, zone: Any, layer: int) -> Finding | None:
    """The plane_islands warning of one zone layer; plane_islands_unread naming
    the error when pcbnew cannot hand its fill back (never a silent 0)."""
    net, name = zone.GetNetname() or "(no net)", board.GetLayerName(layer)
    try:
        filled = zone.GetFilledPolysList(layer)
    except Exception as e:  # noqa: BLE001 - SWIG maps C++ throws to several types: reported
        return Finding(PLANE_ISLANDS_UNREAD, "warning", f"{net}: the fill on {name} could not "
                       f"be read ({type(e).__name__}: {e}); its islands were not counted")
    n = _islands(filled)
    if n <= 1:
        return None
    return Finding(PLANE_ISLANDS, "warning", (
        f"{net}: plane on {name} fills as {n} separate islands on this board's outline "
        f"(cutouts or a non-convex edge split it); join them with a corridor preroute or "
        f"stitching, or check every island reaches its pads"))


def plane_island_findings(board: Any, shape: BoardShape) -> list[Finding]:
    """A warning per copper zone layer filling as more than one island when the
    outline has cutouts or is non-convex. Never a blocker (GND included):
    check_plane_integrity blocks a split that strands pads."""
    if shape.outline is None or not is_shaped(shape.outline):
        return []
    found = (_zone_islands(board, zone, layer) for zone in board.Zones()
             if not zone.GetIsRuleArea() for layer in zone.GetLayerSet().CuStack())
    return [f for f in found if f is not None]
