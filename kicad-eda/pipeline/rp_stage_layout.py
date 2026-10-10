"""Harness stage G4 (layout / board review): judge the routed board, run the
board-fix corrective-action loop, then the opt-in EMC stitching post-pass,
which is a transaction: a stitch that makes G4 fail, or whose board cannot be
judged, is undone byte for byte.
Feature hooks (rp_hooks): g4_inputs and g4_verdict at every judgement (the
first, the EMC re-judge and the re-judge of a rolled-back board), emc_args;
handoff_veto through rp_route_modes when a failed G4 would be handed off.

In a resumed run (--resume, rp_route_modes) a board the pcbnew install cannot
load (unloadable: tc.swig_readable, the agent's KiCad 11 board) is judged by
DRC (kicad-cli of the KiCad that wrote it) plus the board API routed gate
(board_route_tools.routed_gate, route_status folded into the SWIG check's
shape; a gate that cannot answer is a blocker naming why) instead of the SWIG
routed check; the SWIG board-fix loop and EMC stitching cannot run on it and
are skipped with a G4 warning. In a --resume routed run the unconnected count
is the DRC's (nothing routed it here). A fresh run is judged as always."""
from __future__ import annotations

from pathlib import Path

import rp_paths  # noqa: F401  first: puts server/ and server/tools/ on sys.path
import board_route_tools
import gates
import rp_context
import rp_hooks
import rp_kicad
import rp_route_modes
import toolchain as tc

G4_FIX_ROUNDS = 2                                # corrective-action rounds at G4
EMC_ROLLED_BACK = "EMC stitching rolled back: "
API_JUDGED = ("routed gate judged through the KiCad board API (route_status): {name} is a "
              "KiCad 11 board the SWIG gate scripts cannot load")
API_NOT_JUDGED = "routed gate not judged on {name} (a KiCad 11 board, board API): {why}"
API_NOT_RELEASED = "the board API session on {name} did not close cleanly: {why}"
FIX_LOOP_SKIPPED = ("G4 board-fix loop skipped: {name} is a KiCad 11 board the SWIG fixer "
                    "cannot load (repair it with the board API tools, then resume again)")
EMC_SKIPPED = ("EMC stitching skipped: {name} is a KiCad 11 board the SWIG stitcher cannot "
               "load (stitch it through the board API or in a KiCad 11 GUI before fab)")


@rp_context.stage
def stage_g4(ctx: rp_context.RunContext) -> bool:
    """G4: layout / board review (feeds CDR): judge, fix loop, EMC post-pass.
    A failed G4 in auto mode is then handed to the agent (or, on a feature's
    veto, fails with the reason as one more blocker): rp_route_modes."""
    judge_g4(ctx, refresh_unconnected=ctx.resume == rp_context.RESUME_ROUTED)
    stage_g4_fixes(ctx)
    stage_emc(ctx)
    passed = rp_context.record_gate(ctx, ctx.g4)
    if not passed:
        rp_route_modes.after_g4_failure(ctx)
    return passed


def judge_g4(ctx: rp_context.RunContext, refresh_unconnected: bool = False,
             notes: tuple[str, ...] = ()) -> None:
    """DRC + routed-width check + G4 verdict (with `notes` as warnings) on the
    current board."""
    measure_g4(ctx, refresh_unconnected)
    ctx.g4 = g4_verdict(ctx, notes)


def measure_g4(ctx: rp_context.RunContext, refresh_unconnected: bool = False) -> None:
    """The KiCad measurements G4 judges: DRC, then the routed-width check
    (routed_check; raises rp_kicad.CHILD_ERRORS when a child fails; no
    feature hook runs)."""
    ctx.drc_rep = rp_kicad.drc(ctx.pcb)
    ctx.report["drc"] = ctx.drc_rep
    if refresh_unconnected:
        ctx.route_rep = {**ctx.route_rep, "unconnected": ctx.drc_rep["unconnected"]}
        ctx.report["autoroute"] = ctx.route_rep
    ctx.routed_check = routed_check(ctx)


def unloadable(ctx: rp_context.RunContext) -> str | None:
    """Why the SWIG scripts cannot load the board of a resumed run (a KiCad 11
    board), else None (always None in a fresh run)."""
    return tc.swig_readable(ctx.pcb) if ctx.resume else None


def routed_check(ctx: rp_context.RunContext) -> dict:
    """{issues, warnings} of the routed board: the SWIG gate check, or the
    board API routed gate (api_routed_check) on an unloadable board."""
    if unloadable(ctx) is None:
        return rp_kicad.gate_check("routed", ctx.pcb, ctx.netlist)
    return api_routed_check(ctx.pcb, ctx.netlist)


def api_routed_check(pcb: str, netlist: str) -> dict:
    """board_route_tools.routed_gate on a KiCad 11 board, its board API
    session released right after (this process must not keep an api-server
    on the board). A gate that cannot answer is one issue naming why (never
    a skipped check); the answer carries a warning that the API judged it."""
    name = Path(pcb).name
    try:
        res = board_route_tools.routed_gate(pcb, netlist)
    finally:
        released = board_route_tools.release_for_rewrite(pcb)
    problems = [str(v) for key, v in released.items() if key == "reason" or "error" in key]
    notes = [API_NOT_RELEASED.format(name=name, why="; ".join(problems))] if problems else []
    if not res.get("ok"):
        why = res.get("reason") or "no reason given"
        return {"issues": [API_NOT_JUDGED.format(name=name, why=why)], "warnings": notes}
    return {"issues": list(res.get("issues") or []),
            "warnings": [*(res.get("warnings") or []), API_JUDGED.format(name=name), *notes]}


def with_warning(verdict: dict, warning: str) -> dict:
    return {**verdict, "warnings": [*verdict["warnings"], warning]}


def g4_verdict(ctx: rp_context.RunContext, notes: tuple[str, ...] = ()) -> dict:
    """gates.g4_layout on the current measurements (ctx.drc_rep and
    ctx.routed_check stay RAW: the g4_inputs hook amends a copy for this
    verdict only, so a re-judge never applies it twice), then g4_verdict."""
    drc, routed = rp_hooks.chain("g4_inputs", ctx, (ctx.drc_rep, ctx.routed_check))
    return rp_hooks.chain("g4_verdict", ctx, gates.g4_layout(
        ctx.route_rep, drc, routed["issues"], routed["warnings"] + list(notes)))


def stage_g4_fixes(ctx: rp_context.RunContext) -> None:
    """G4 corrective-action loop: board-level blockers with an unambiguous
    remedy (same-net micro-gaps, under-ampacity layer transitions) are FIXED
    on the routed board and the gate re-tested — re-routing is never required
    for them. Not on an unloadable board (a G4 warning says so)."""
    if not ctx.g4["passed"] and unloadable(ctx):
        ctx.g4 = with_warning(ctx.g4, FIX_LOOP_SKIPPED.format(name=Path(ctx.pcb).name))
        return
    for rnd in range(1, G4_FIX_ROUNDS + 1):
        if ctx.g4["passed"]:
            return
        fixes = rp_kicad.board_fix(ctx.pcb, ctx.netlist, "all")
        if not any(not f.startswith("OPEN:") for f in fixes):
            return
        ctx.log_remediation("G4", rnd, fixes)
        judge_g4(ctx, refresh_unconnected=True)


def emc_stitch(ctx: rp_context.RunContext, emc: dict) -> dict:
    """rp_kicad.emc_stitch plus the features' extra argv (emc_args); with
    none the call is exactly the plain two-argument one. A stitch child that
    fails (rp_kicad.CHILD_ERRORS: a timeout, a child that cannot start) is a
    failed stitch {ok: False, reason}, like one that reports a failure."""
    extra = rp_hooks.concat("emc_args", ctx)
    try:
        if extra:
            return rp_kicad.emc_stitch(ctx.pcb, emc, extra)
        return rp_kicad.emc_stitch(ctx.pcb, emc)
    except rp_kicad.CHILD_ERRORS as e:
        return {"ok": False, "reason": f"{type(e).__name__}: {e}"}


def stage_emc(ctx: rp_context.RunContext) -> None:
    """EMC post-pass (opt-in via netlist "emc": {"stitch": true, ...}):
    perimeter GND via fence + interior stitching grid (Ott/Montrose, lambda/20
    pitch from max_freq_hz), then DRC + the routed gate again — the pass is
    clearance-aware but the gate trusts measurements, not intentions. A
    transaction: the board is snapshotted first; a stitch that makes G4 fail,
    or whose board cannot be judged (a DRC / routed-check child fails), is
    undone byte for byte (the passing board is re-judged, with a warning),
    and a failed stitch leaves the judged board as it was. Never on a cached
    board; skipped with a G4 warning on an unloadable board."""
    emc = ctx.design.get("emc") or {}
    if not (ctx.g4["passed"] and emc.get("stitch") and not ctx.use_cache):
        return
    if unloadable(ctx):
        ctx.g4 = with_warning(ctx.g4, EMC_SKIPPED.format(name=Path(ctx.pcb).name))
        return
    board = Path(ctx.pcb)
    snapshot = board.read_bytes()
    er = emc_stitch(ctx, emc)
    ctx.report["emc"] = er
    teardrops = (teardrop_note(),) if emc.get("teardrops") else ()
    if not er.get("ok"):
        restore_board(board, snapshot)
        ctx.g4 = g4_verdict(ctx, (f"EMC stitching FAILED: {er.get('reason')}", *teardrops))
        return
    try:
        measure_g4(ctx)
    except rp_kicad.CHILD_ERRORS as e:
        rollback_emc(ctx, board, snapshot, f"the stitched board could not be judged "
                                           f"({type(e).__name__}: {e})", teardrops)
        return
    ctx.g4 = g4_verdict(ctx, (f"EMC stitching: {er.get('vias_added')} GND vias "
                              f"(fence {er.get('fence')}, interior {er.get('interior')}, "
                              f"pitch {er.get('pitch_mm')} mm)", *teardrops))
    if not ctx.g4["passed"]:
        broke = "; ".join(ctx.g4["blockers"])
        rollback_emc(ctx, board, snapshot, f"the stitched board failed G4 ({broke})", teardrops)


def rollback_emc(ctx: rp_context.RunContext, board: Path, snapshot: bytes, why: str,
                 notes: tuple[str, ...]) -> None:
    """The stitched board failed G4 or could not be judged (`why`): put the
    pre-stitch board back byte for byte and re-judge it (a fresh DRC report
    on disk; a failure now raises: the board is already restored), with a
    warning naming what went wrong."""
    restore_board(board, snapshot)
    ctx.report["emc"] = {**ctx.report["emc"], "rolled_back": True}
    judge_g4(ctx, notes=(f"{EMC_ROLLED_BACK}{why}; the pre-stitch board was restored "
                         f"byte for byte", *notes))


def restore_board(board: Path, snapshot: bytes) -> None:
    if not board.is_file() or board.read_bytes() != snapshot:
        board.write_bytes(snapshot)


def teardrop_note() -> str:
    return ("teardrops requested but KiCad's teardrop generator has no headless API — "
            "generate in the GUI (Edit > Edit Teardrops) before fab if required")
