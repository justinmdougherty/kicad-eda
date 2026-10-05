#!/usr/bin/env python3
"""Board features of the headless builder (swig_build_board.py, F3-11).

KiCad's bundled Python (3.11 on 9.0.4; python3 in the 10.0.5 image): stdlib +
pcbnew. Without pcbnew (the dev venv) only the pure helpers work.

Outline source (ROADMAP_DECISIONS D2), resolved before placement:
  (a) env KICAD_EDA_OUTLINE: an outline_resolve sidecar; its ``board``
      replaces design['board'] in a NEW design dict;
  (b) a board block with outline.loop / corner_radius / cutouts / datum;
  (c) outline.dxf without a sidecar: refused (DXF_UNRESOLVED);
  (d) otherwise LEGACY: unchanged (rectangle Edge.Cuts and planes, no aux
      origin, A4 sheet), so existing boards keep their geometry.
Frames: board = mm, Y down, origin = outline bbox top-left; sheet = board +
(ox, oy). Each feature is ONE function taking (board, design, ...) and
returning (result, warnings); D7-14 replaces add_planes and add_antenna_keepout.
"""
from __future__ import annotations

import hashlib
import math
import os
import sys
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
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
import outline_audit  # noqa: E402
import outline_resolve as orz  # noqa: E402

DXF_UNRESOLVED = ("board.outline.dxf must be resolved first (build_board_from_netlist / "
                  "outline_resolve.py does this)")
HOLES_NEED_POSITIONS = "mounting_holes needs explicit positions on a non-rectangular outline"
LEGACY_SHEET = ("A4", 297.0, 210.0)        # CreateEmptyBoard's page, saved as (paper "A4")
PAPER_SIZES = (LEGACY_SHEET, ("A3", 420.0, 297.0), ("A2", 594.0, 420.0),
               ("A1", 841.0, 594.0), ("A0", 1189.0, 841.0))       # landscape, mm
SHEET_MARGIN_MM = 20.0
LEGACY_PAPER = b'(paper "A4")'
RECT_SOURCES = ("rect", "rounded_rect")
DEFAULT_HOLE_INSET_MM = 4.0
DEFAULT_HOLE_DRILL_MM = 3.2
DEFAULT_KEEPOUT_DEPTH_MM = 8.0


class BuildError(RuntimeError):
    """A board the builder refuses; reported as {"ok": false, "error": message}."""


@dataclass(frozen=True)
class OutlineSource:
    """The design the builder uses (a new dict when a sidecar replaced its
    board) and where its board block came from."""
    design: dict
    source: str                       # none | rect | rounded_rect | loop | dxf
    legacy: bool
    outline: ol.Outline | None        # board frame; None on the legacy path
    sidecar: str | None
    sidecar_bytes: bytes = b""


@dataclass(frozen=True)
class Frame:
    """Board size (the outline bbox) and its sheet offset."""
    w: float
    h: float
    ox: float
    oy: float
    outline: ol.Outline | None = None     # None: legacy rectangle
    source: str = "none"


# --- outline source (pure) ----------------------------------------------------------------
def is_legacy_block(board: Mapping | None) -> bool:
    """outline_resolve's is_legacy, read from the keys alone: no outline, no
    corner_radius, no cutouts, no datum (so odd legacy blocks build as today)."""
    if not board:
        return True
    return (board.get("outline") is None and not board.get("corner_radius")
            and not board.get("cutouts") and board.get("datum") is None)


def _read_sidecar(path: str) -> tuple[dict, bytes]:
    try:
        return orz.read_sidecar(path), Path(path).read_bytes()
    except (orz.ResolveError, OSError) as e:
        raise BuildError(f"{orz.ENV_VAR}={path}: {e}") from e


def resolve_outline_source(design: Mapping, env: Mapping[str, str]) -> OutlineSource:
    """The board block to build (module doc, sources a-d). Raises BuildError
    for an unresolved DXF, a bad sidecar or an invalid outline. Never mutates
    `design`."""
    path = env.get(orz.ENV_VAR) or None
    raw, source = b"", None
    if path:
        side, raw = _read_sidecar(path)
        design, source = {**design, "board": side["board"]}, side["source"]
    board = design.get("board") or {}
    if not isinstance(board, Mapping):
        raise BuildError(f"the board block must be an object, got {board!r}")
    spec = board.get("outline")
    if isinstance(spec, Mapping) and "dxf" in spec:
        raise BuildError(DXF_UNRESOLVED)
    if is_legacy_block(board):
        legacy = source or ("rect" if board else "none")
        return OutlineSource(dict(design), legacy, True, None, path, raw)
    try:
        resolved = orz.resolve_board(design, None)
    except orz.ResolveError as e:
        raise BuildError(f"board outline: {e}") from e
    return OutlineSource(dict(design), source or resolved.source, False, resolved.outline,
                         path, raw)


def outline_dims(src: OutlineSource) -> tuple[float, float] | None:
    """(w, h) of a non-legacy outline (its bbox); None on the legacy path."""
    if src.outline is None:
        return None
    x0, y0, x1, y1 = ol.bbox(src.outline)
    return (x1 - x0, y1 - y0)


def kiid_seed(netlist_bytes: bytes, sidecar_bytes: bytes = b"") -> int:
    """The KIID.SeedGenerator seed: sha256(netlist + sidecar), first 32 bits."""
    return int(hashlib.sha256(netlist_bytes + sidecar_bytes).hexdigest()[:8], 16)


# --- sheet / paper (pure + file splice) -----------------------------------------------------
def choose_sheet(w: float, h: float) -> tuple[str, float, float]:
    """The smallest landscape A-size holding the board + SHEET_MARGIN_MM on
    every side, else a "User" sheet of that size (whole mm)."""
    need_w, need_h = w + 2 * SHEET_MARGIN_MM, h + 2 * SHEET_MARGIN_MM
    for name, sw, sh in PAPER_SIZES:
        if need_w <= sw and need_h <= sh:
            return (name, sw, sh)
    return ("User", float(math.ceil(need_w)), float(math.ceil(need_h)))


def paper_sexpr(sheet: tuple[str, float, float]) -> bytes:
    """The board file's paper token for `sheet`."""
    name, w, h = sheet
    return (f'(paper "User" {w:g} {h:g})' if name == "User" else f'(paper "{name}")').encode()


def splice_paper_bytes(data: bytes, sheet: tuple[str, float, float]) -> bytes:
    """`data` with CreateEmptyBoard's (paper "A4") replaced by `sheet`'s token
    (unchanged when it is already there). pcbnew 9.0.4 / 10.0.5 expose page
    settings only as an opaque object, so the saved text is the one handle."""
    want = paper_sexpr(sheet)
    if want in data:
        return data
    found = data.count(LEGACY_PAPER)
    if found != 1:
        raise BuildError(f"cannot set the sheet to {want.decode()}: the saved board has "
                         f"{found} {LEGACY_PAPER.decode()} tokens (expected exactly one)")
    return data.replace(LEGACY_PAPER, want)


def splice_paper(pcb_path: str, sheet: tuple[str, float, float]) -> None:
    """Rewrite the saved board's paper token in place (temp file + os.replace)."""
    path = Path(pcb_path)
    data = path.read_bytes()
    if (new := splice_paper_bytes(data, sheet)) == data:
        return
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(new)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def apply_page(board: Any, paper: str) -> list[str]:
    """Set the page type where pcbnew has a real SetType (9.0.4 / 10.0.5 do not:
    CreateEmptyBoard is A4 and splice_paper sets the rest). Returns warnings."""
    ps = board.GetPageSettings()
    if not hasattr(ps, "SetType"):
        return []
    try:
        ps.SetType(paper)
        board.SetPageSettings(ps)
    except Exception as e:
        return [f"page settings not applied ({type(e).__name__}: {e}); sheet stays at the "
                "pcbnew default"]
    return []


# --- outline on the board ---------------------------------------------------------------------
def datum_board(outline: ol.Outline) -> tuple[float, float]:
    """outline.datum, else the bbox bottom-left (dxf_io.write_outline_dxf's default)."""
    if outline.datum is not None:
        return (float(outline.datum[0]), float(outline.datum[1]))
    x0, _y0, _x1, y1 = ol.bbox(outline)
    return (x0, y1)


def _r6(values: tuple | list) -> list[float]:
    return [round(float(v), 6) + 0.0 for v in values]


def add_board_outline(board: Any, design: Mapping, frame: Frame,
                      sheet: tuple[str, float, float]) -> tuple[dict, list[str]]:
    """Edge.Cuts (+ the aux/grid origin for an outline board). Legacy: the
    w x h rectangle (board_shapes.add_outline), no aux origin. Returns the
    result's "outline" record and warnings; BuildError when KiCad cannot use
    the outline (board_outline.OutlineBuildError)."""
    import board_outline as bo
    import board_shapes
    base = {"source": frame.source, "legacy": frame.outline is None, "paper": sheet[0],
            "sheet_mm": [sheet[1], sheet[2]], "origin_sheet": [frame.ox, frame.oy]}
    if frame.outline is None:
        board_shapes.add_outline(board, frame.w, frame.h, frame.ox, frame.oy)
        return ({**base, "bbox_mm": [0.0, 0.0, frame.w, frame.h],
                 "area_mm2": round(frame.w * frame.h, 4), "cutouts": 0, "datum_board": None,
                 "aux_origin_sheet": None}, [])
    try:
        bo.draw_outline(board, frame.outline, frame.ox, frame.oy)
    except bo.OutlineBuildError as e:
        raise BuildError(f"board outline ({frame.source}): {e}") from e
    dx, dy = datum_board(frame.outline)
    bo.set_datum(board, dx + frame.ox, dy + frame.oy)
    return ({**base, "bbox_mm": _r6(ol.bbox(frame.outline)),
             "area_mm2": round(ol.area(frame.outline), 4),
             "cutouts": len(frame.outline.cutouts), "datum_board": _r6((dx, dy)),
             "aux_origin_sheet": _r6(bo.get_aux_origin(board))}, [])


# --- preroutes ---------------------------------------------------------------------------------
def _mm(v: float) -> int:
    return pcbnew.FromMM(float(v))


def _vec(x: float, y: float) -> Any:
    return pcbnew.VECTOR2I(_mm(x), _mm(y))


def _preroute_point(pt: list, placed: Mapping, ox: float, oy: float) -> tuple[Any, float | None]:
    """["pad", ref, num, (width)] or ["xy", x, y, (width)] -> (sheet point,
    width of the segment STARTING here or None)."""
    w = float(pt[3]) if len(pt) > 3 else None
    if pt[0] == "pad":
        pad = next(p for p in placed[pt[1]].Pads() if p.GetNumber() == str(pt[2]))
        return pad.GetPosition(), w
    return _vec(float(pt[1]) + ox, float(pt[2]) + oy), w


def add_preroutes(board: Any, design: Mapping, placed: Mapping, get_net: Callable[[str], Any],
                  ox: float, oy: float) -> tuple[int, list[str]]:
    """Designer-authored power copper laid BEFORE the router (a deterministic
    router cannot neck down; the designer routes the fat current path, the
    autorouter connects the taps). Spec: "preroutes": [{"net", "width",
    "layer", "path": [point...], "vias": [[x, y], ...], "via_size": [dia,
    drill]}], point = ["pad", ref, num(, width)] or ["xy", x, y(, width)]
    (board frame). Returns (tracks + vias added, warnings)."""
    count = 0
    for pr in design.get("preroutes", []):
        ni = get_net(pr["net"])
        layer = getattr(pcbnew, pr.get("layer", "F.Cu").replace(".", "_"))
        pts = [_preroute_point(p, placed, ox, oy) for p in pr["path"]]
        for (a, wa), (b, _) in zip(pts, pts[1:], strict=False):   # consecutive pairs
            t = pcbnew.PCB_TRACK(board)
            t.SetStart(a)
            t.SetEnd(b)
            t.SetWidth(_mm(wa if wa is not None else pr["width"]))
            t.SetLayer(layer)
            t.SetNet(ni)
            board.Add(t)
            count += 1
        via_d, via_drill = pr.get("via_size", [1.2, 0.9])
        for vx, vy in pr.get("vias", []):
            v = pcbnew.PCB_VIA(board)
            v.SetPosition(_vec(float(vx) + ox, float(vy) + oy))
            v.SetWidth(_mm(via_d))
            v.SetDrill(_mm(via_drill))
            v.SetViaType(pcbnew.VIATYPE_THROUGH)
            v.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
            v.SetNet(ni)
            board.Add(v)
            count += 1
    return count, []


# --- mounting holes ----------------------------------------------------------------------------
def corner_positions(w: float, h: float, inset: float) -> list[tuple[float, float]]:
    """The four default corner holes, inset from a w x h board (board frame)."""
    return [(inset, inset), (w - inset, inset), (w - inset, h - inset), (inset, h - inset)]


def _hole_drill_mm(fp: Any) -> float:
    drills = [pcbnew.ToMM(p.GetDrillSize().x) for p in fp.Pads() if p.GetDrillSize().x > 0]
    return max(drills) if drills else DEFAULT_HOLE_DRILL_MM


def _hole_positions(spec: Mapping, frame: Frame) -> tuple[list, bool]:
    """(positions, defaulted). Legacy: today's corner insets. An outline board
    takes explicit positions, else corners on a rect / rounded rect only."""
    inset = float(spec.get("inset", DEFAULT_HOLE_INSET_MM))
    given = spec.get("positions")
    if given:
        return list(given), False
    if frame.outline is not None and frame.source not in RECT_SOURCES:
        raise BuildError(HOLES_NEED_POSITIONS)
    return corner_positions(frame.w, frame.h, inset), True


def _hole_issues(frame: Frame, holes: list[dict], defaulted: bool) -> list[str]:
    """Explicit holes off the material / too close to an edge are warnings (G3
    blocks them); a DEFAULT corner hole off the material is a BuildError."""
    issues = outline_audit.mounting_hole_issues(frame.outline, holes)
    off = [i.message for i in issues if i.severity == "error"]
    if defaulted and off:
        raise BuildError("default corner mounting holes do not fit the outline: "
                         + "; ".join(off) + " (give mounting_holes.positions or an inset)")
    return [f"mounting hole: {i.message}" for i in issues]


def add_mounting_holes(board: Any, design: Mapping, load: Callable[[str], Any], frame: Frame,
                       default_footprint: str) -> tuple[dict[str, str], list[str]]:
    """netlist "mounting_holes": true or {"footprint", "inset", "positions"}:
    NPTH holes H1..Hn (footprint default `default_footprint`, M3). `load` is
    the builder's load_footprint(lib_id). Returns ({ref: fpid}, warnings)."""
    mh = design.get("mounting_holes")
    if not mh:
        return {}, []
    spec = mh if isinstance(mh, dict) else {}
    lib_id = spec.get("footprint", default_footprint)
    positions, defaulted = _hole_positions(spec, frame)
    fpids, records = {}, []
    for i, (hx, hy) in enumerate(positions, 1):
        fp = load(lib_id)
        fp.SetReference(f"H{i}")
        fp.SetPosition(_vec(hx + frame.ox, hy + frame.oy))
        board.Add(fp)
        fpids[f"H{i}"] = str(fp.GetFPID().GetUniStringLibId())
        records.append({"ref": f"H{i}", "center": [float(hx), float(hy)],
                        "drill": _hole_drill_mm(fp)})
    if frame.outline is None:
        return fpids, []
    return fpids, _hole_issues(frame, records, defaulted)


# --- copper planes -----------------------------------------------------------------------------
def default_planes(design: Mapping) -> dict[str, str]:
    """netlist "planes" ({"<layer>": "<net>"}), else In1.Cu GND + In2.Cu the
    first power rail (a regulated "+..." rail before a raw "V" input)."""
    planes = design.get("planes")
    if planes is not None:
        return dict(planes)
    nets_avail = [n for n in design.get("nets", {}) if n != "GND"]
    power = (next((n for n in nets_avail if n.startswith("+")), None)
             or next((n for n in nets_avail if "V" in n.upper()), None))
    return {"In1.Cu": "GND", **({"In2.Cu": power} if power else {})}


def add_planes(board: Any, design: Mapping, netmap: Mapping, frame: Frame,
               inset_mm: float) -> tuple[list[str], list[str]]:
    """Copper pours, one per plane layer (4-layer Sig/GND/PWR/Sig by default;
    signal layers stay un-poured). Legacy: the inset w x h rectangle
    (board_shapes.add_plane); outline boards follow Edge.Cuts inset by
    `inset_mm`, cutouts included (add_plane_outline). A layer that is unknown
    or not enabled is skipped (a warning for a disabled one). Returns
    (["<layer>:<net>", ...], warnings)."""
    import board_outline as bo
    import board_shapes
    zones, warnings = [], []
    for layer_name, net_name in default_planes(design).items():
        if net_name not in netmap:
            continue
        lid = board.GetLayerID(layer_name)
        if lid < 0:
            continue
        if not board.IsLayerEnabled(lid):
            warnings.append(f"plane {layer_name}:{net_name} skipped: {layer_name} is not "
                            f"enabled on this {board.GetCopperLayerCount()}-layer board")
            continue
        if frame.outline is None:
            board_shapes.add_plane(board, lid, netmap[net_name], frame.w, frame.h,
                                   frame.ox, frame.oy)
        else:
            try:
                board_shapes.add_plane_outline(board, lid, netmap[net_name], inset_mm)
            except bo.OutlineBuildError as e:
                raise BuildError(f"plane {layer_name}:{net_name}: {e}") from e
        zones.append(f"{layer_name}:{net_name}")
    return zones, warnings


# --- antenna keep-out --------------------------------------------------------------------------
def keepout_rect(spec: Mapping, w: float, h: float, ox: float = 0.0,
                 oy: float = 0.0) -> tuple[float, float, float, float]:
    """The keep-out (x, y, w, h) at sheet offset (ox, oy) (0, 0: the board
    frame): {"rect": [x, y, w, h]} or an edge strip {"edge": top|bottom|left|
    right, "depth": mm} of the w x h outline bbox. The expressions are the
    legacy builder's, so a saved corner does not move by even 1 nm."""
    if "rect" in spec:
        kx, ky, kw, kh = spec["rect"]
        return (ox + kx, oy + ky, kw, kh)
    depth = float(spec.get("depth", DEFAULT_KEEPOUT_DEPTH_MM))
    return {"top": (ox, oy, w, depth),
            "bottom": (ox, oy + h - depth, w, depth),
            "left": (ox, oy, depth, h),
            "right": (ox + w - depth, oy, depth, h),
            }.get(spec.get("edge", "top"), (ox, oy, w, depth))


def leaves_bbox(rect: tuple, w: float, h: float) -> bool:
    """A board-frame (x, y, w, h) rectangle reaching outside 0..w x 0..h."""
    kx, ky, kw, kh = (float(v) for v in rect)
    tol = ol.FRAME_TOL_MM
    return kx < -tol or ky < -tol or kx + kw > w + tol or ky + kh > h + tol


def add_antenna_keepout(board: Any, design: Mapping, frame: Frame) -> tuple[dict | None,
                                                                             list[str]]:
    """netlist "antenna_keepout" ({"edge", "depth"} or {"rect"}, "name"): a
    rule area with no pour / vias / pads on every copper layer so the planes
    clip around the radiator (board_shapes.add_keepout). Returns (the spec
    when added, warnings: a rect that leaves the outline bbox). D7-14
    replaces this function with an all-copper rule area."""
    import board_shapes
    ako = design.get("antenna_keepout")
    if not ako:
        return None, []
    local = keepout_rect(ako, frame.w, frame.h)
    warnings = []
    if leaves_bbox(local, frame.w, frame.h):
        warnings.append("antenna keep-out [{:g}, {:g}, {:g}, {:g}] leaves the board outline "
                        "bbox 0..{:g} x 0..{:g} mm (board frame)".format(*local, frame.w,
                                                                         frame.h))
    kx, ky, kw, kh = keepout_rect(ako, frame.w, frame.h, frame.ox, frame.oy)
    zone = board_shapes.add_keepout(board, kx, ky, kw, kh, ako.get("name", "ANTENNA"))
    return (ako if zone else None), warnings
