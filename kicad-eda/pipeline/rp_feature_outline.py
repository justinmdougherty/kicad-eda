"""Pipeline feature 'outline' (F3, ROADMAP D2): non-rectangular boards in the
gated harness, through rp_hooks hooks only (no stage module is edited).

  ROUTE_HASH_COMPONENT_KEYS  'edge': the board edge a connector is placed on
                    (X-02) moves copper (place_engine conn_edges)
  route_hash_extra  {'outline_fp': the outline_resolve fingerprint} for a
                    non-legacy board (a DXF outline's file bytes included),
                    {} for a legacy one, {'outline_error': why} when the board
                    block cannot be resolved (never a cache hit then)
  board_dims        (w, h, 'netlist board outline') for a board block given
                    as an outline only (no w/h): the outline bbox, not the
                    false 'auto-sized' label; an unresolvable block is a Halt,
                    which rp_cli turns into a usage error
  g4_inputs         a board pcbnew cannot load (tc.swig_readable: a resumed
                    KiCad 11 board) gets the containment check F3-13b's SWIG
                    gate check gives the others: its tracks / arcs / vias read
                    from the text (board_text_copper), moved into the board
                    frame (the Edge.Cuts bbox top-left), judged against the
                    resolved outline by outline_audit.copper_issues; each
                    issue is a G4 blocker. kicad-cli DRC misses copper deep
                    inside an Edge.Cuts cutout. A board it cannot judge gets
                    one blocker saying why
  g5_verdict        g5_mechanical -> report['mechanical']; its problems are G5
                    WARNINGS only (a STEP export fails without 3D models)

g5_mechanical writes <pcb stem>_mech/ (git-ignored): <stem>.step (board only,
--drill-origin), <stem>_outline.dxf (read back against the board's Edge.Cuts:
hausdorff_mm) and <stem>_datum_cpl.csv (the mechanical CPL relative to the
datum; the fab CPL stays absolute), through mechanical_tools. Legacy boards
(no board block, or a plain w x h rectangle without cutouts or datum) are
skipped: {'skipped': 'rectangular board without datum'}."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import rp_paths  # noqa: F401  first: puts server/ and server/tools/ on sys.path
import board_text_copper as btc
import mechanical_tools as mt
import outline as ol
import outline_audit as oa
import outline_resolve as orv
import rp_hooks
import toolchain as tc

ORDER = 20
ROUTE_HASH_COMPONENT_KEYS: tuple[str, ...] = ("edge",)
BOARD_DIMS_LABEL = "netlist board outline"
LEGACY_SKIP = "rectangular board without datum"
MECH_SUFFIX = "_mech"
# containment only: kicad-cli DRC (it does read KiCad 11 boards) judges the edge clearance band
TEXT_CHECK_EDGE_CLEARANCE_MM = 0.0
FRAME_MATCH_TOL_MM = 0.01        # board Edge.Cuts bbox vs the netlist outline bbox
MAX_COPPER_BLOCKERS = 20
NOT_JUDGED = "copper containment not judged"
TEXT_CHECK = ("board-text check of {name}, which pcbnew cannot load: KiCad DRC misses copper "
              "inside an Edge.Cuts cutout")
# (report key, what), in export order
EXPORTS: tuple[tuple[str, str], ...] = (
    ("step", "STEP"), ("dxf", "outline DXF"), ("datum_cpl", "datum CPL"))


# --- route hash ------------------------------------------------------------------------
def route_hash_extra(design: dict, netlist_dir: Path | None) -> dict[str, str]:
    """The outline fingerprint for a non-legacy board ({} for a legacy one)."""
    try:
        resolved = orv.resolve_board(design, netlist_dir)
    except orv.ResolveError as e:
        return {"outline_error": str(e)}
    return {} if resolved.is_legacy else {"outline_fp": resolved.fingerprint}


# --- board dims ------------------------------------------------------------------------
def board_dims_outline(design: dict, base_dir: Path | str | None
                       ) -> tuple[float, float, str] | None:
    """(w, h, 'netlist board outline') of a board block (its w/h, else the
    outline bbox); None without one (the builder sizes the board). Raises
    orv.ResolveError for a block that cannot be resolved."""
    resolved = orv.resolve_board(design, base_dir)
    if resolved.source == "none":
        return None
    w, h = orv.outline_bbox_dims(resolved)
    return (w, h, BOARD_DIMS_LABEL)


def board_dims(design: dict, netlist_dir: Path | None) -> tuple[float, float, str] | None:
    """The board_dims hook: board_dims_outline, a resolve failure a G1 Halt."""
    try:
        return board_dims_outline(design, netlist_dir)
    except orv.ResolveError as e:
        raise rp_hooks.Halt("G1", f"the netlist board block cannot be resolved: {e}") from e


# --- G4: copper containment on a board pcbnew cannot load ------------------------------
def g4_inputs(ctx: Any, drc_rep: dict, routed: dict) -> tuple[dict, dict] | None:
    """The g4_inputs hook: the board-text copper blockers appended to a NEW
    routed record (the raw measurements are never changed)."""
    if tc.swig_readable(ctx.pcb) is None:
        return None                     # the SWIG routed gate check judges it
    blockers = text_copper_blockers(ctx.pcb, ctx.design, ctx.netlist_dir)
    if not blockers:
        return None
    return drc_rep, {**routed, "issues": [*routed.get("issues", []), *blockers]}


def text_copper_blockers(pcb: str, design: dict, netlist_dir: Path | None) -> list[str]:
    """G4 blockers for the copper of `pcb` (read as text) off the board
    material; one 'not judged' blocker naming why when it cannot be checked."""
    name = Path(pcb).name
    try:
        issues = text_copper_issues(pcb, design, netlist_dir)
    except (OSError, ValueError) as e:  # unreadable board, no usable Edge.Cuts, bad item
        return [f"{NOT_JUDGED} on {name}, which pcbnew cannot load: {e}"]
    except Exception as e:              # the hook never crashes a run: the reason blocks G4
        return [f"{NOT_JUDGED} on {name}, which pcbnew cannot load: {type(e).__name__}: {e}"]
    note = TEXT_CHECK.format(name=name)
    lines = [f"{i.code}: {i.message} [{note}]" for i in issues[:MAX_COPPER_BLOCKERS]]
    more = len(issues) - MAX_COPPER_BLOCKERS
    if more > 0:
        lines.append(f"copper_outside: ... and {more} more copper item(s) off the board "
                     f"material [{note}]")
    return lines


def text_copper_issues(pcb: str, design: dict, netlist_dir: Path | None) -> list[oa.Issue]:
    """outline_audit.copper_issues of the board text's tracks, arcs and vias,
    in the board frame, against the outline judged_outline picks."""
    import kicad_import_pcb as kip      # the Edge.Cuts frame of any KiCad 6..11 board
    text = Path(pcb).read_text(encoding="utf-8", errors="replace")
    frame = kip.board_frame(kip.read_board(text))
    outline = judged_outline(design, netlist_dir, frame)
    items = btc.translated(btc.read_copper(text).items, -frame.x0, -frame.y0)
    return oa.copper_issues(outline, items, TEXT_CHECK_EDGE_CLEARANCE_MM)


def _span(o: ol.Outline) -> tuple[float, float]:
    x0, y0, x1, y1 = ol.bbox(o)
    return (x1 - x0, y1 - y0)


def judged_outline(design: dict, netlist_dir: Path | None, frame: Any) -> ol.Outline:
    """The resolved netlist outline, after checking that the board's Edge.Cuts
    (which place the board frame on the sheet) span the same bbox; the
    board's own Edge.Cuts outline when the netlist has no board block.
    Raises ValueError when the board cannot be judged."""
    if frame.problem or frame.info.get("kind") != "edge_cuts":
        raise ValueError("its Edge.Cuts do not form a board outline "
                         f"({frame.problem or 'no Edge.Cuts shapes on the board'})")
    board = ol.from_board_block(frame.block)
    resolved = orv.resolve_board(design, netlist_dir)
    if resolved.outline is None:
        return board
    have, want = _span(board), _span(resolved.outline)
    if any(abs(a - b) > FRAME_MATCH_TOL_MM for a, b in zip(have, want, strict=True)):
        raise ValueError(f"its Edge.Cuts span {have[0]:.3f} x {have[1]:.3f} mm but the netlist "
                         f"outline spans {want[0]:.3f} x {want[1]:.3f} mm, so the board frame "
                         "cannot be placed on it")
    return resolved.outline


# --- G5: the mechanical exports --------------------------------------------------------
def mech_dir(pcb: str) -> Path:
    """<pcb stem>_mech/ beside the board."""
    return Path(pcb).with_name(Path(pcb).stem + MECH_SUFFIX)


def mech_paths(pcb: str) -> dict[str, Path]:
    stem, out = Path(pcb).stem, mech_dir(pcb)
    return {"step": out / f"{stem}.step", "dxf": out / f"{stem}_outline.dxf",
            "datum_cpl": out / f"{stem}_datum_cpl.csv"}


def _fresh(paths: dict[str, Path]) -> None:
    """Make the folder and remove an older run's exports (a failed export
    must not leave a stale file looking current)."""
    for path in paths.values():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.unlink(missing_ok=True)


def _export(key: str, pcb: str, out: Path) -> dict:
    """One mechanical_tools export; whatever it raises becomes {ok: false}."""
    try:
        if key == "step":
            return mt.export_step(pcb, str(out), board_only=True)
        if key == "dxf":
            return mt.export_outline_dxf(pcb, str(out))
        return mt.export_pos_datum(pcb, str(out))
    except Exception as e:              # reported as a G5 warning, never raised
        return {"ok": False, "reason": f"{type(e).__name__}: {e}"}


def _mech_report(pcb: str, paths: dict[str, Path], results: dict[str, dict],
                 problems: list[str]) -> dict:
    warnings, done = list(problems), {}
    for key, what in EXPORTS:
        res = results.get(key)
        if res is None:
            continue
        if res.get("ok"):
            done[key] = str(paths[key])
        else:
            warnings.append(f"{what} export failed: {res.get('reason') or 'no reason given'}")
        warnings += [f"{what}: {w}" for w in res.get("warnings") or []]
    step, dxf = results.get("step") or {}, results.get("dxf") or {}
    return {"ok": not problems and len(done) == len(EXPORTS), "dir": str(mech_dir(pcb)),
            "files": list(done.values()), **{key: done.get(key) for key, _what in EXPORTS},
            "hausdorff_mm": dxf.get("hausdorff_mm"), "step_bbox": step.get("bbox"),
            "warnings": warnings}


def g5_mechanical(pcb: str, netlist: str, design: dict) -> dict:
    """The Fusion round-trip exports of a non-legacy board into
    <pcb stem>_mech/ (module doc): {ok, dir, files, step, dxf, datum_cpl,
    hausdorff_mm, step_bbox, warnings}; never raises (problems are
    warnings); {'skipped': ...} for a legacy board."""
    try:
        resolved = orv.resolve_board(design, Path(netlist).resolve().parent)
    except orv.ResolveError as e:
        return _mech_report(pcb, {}, {}, [f"no mechanical exports: the board outline cannot "
                                          f"be resolved: {e}"])
    if resolved.is_legacy:
        return {"skipped": LEGACY_SKIP}
    paths = mech_paths(pcb)
    try:
        _fresh(paths)
    except OSError as e:
        return _mech_report(pcb, paths, {}, [f"no mechanical exports: cannot prepare "
                                             f"{mech_dir(pcb)}: {e}"])
    results = {key: _export(key, pcb, paths[key]) for key, _what in EXPORTS}
    return _mech_report(pcb, paths, results, [])


def g5_verdict(ctx: Any, verdict: dict) -> dict | None:
    """The g5_verdict hook: report['mechanical'], its problems as G5 warnings."""
    mech = g5_mechanical(ctx.pcb, ctx.netlist, ctx.design)
    ctx.report["mechanical"] = mech
    extra = [f"mechanical: {w}" for w in mech.get("warnings", [])]
    return {**verdict, "warnings": [*verdict["warnings"], *extra]} if extra else None
