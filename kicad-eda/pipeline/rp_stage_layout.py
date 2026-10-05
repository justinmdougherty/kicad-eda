"""Harness stage G4 (layout / board review): judge the routed board, run the
board-fix corrective-action loop, then the opt-in EMC stitching post-pass.
Feature hooks (rp_hooks): g4_inputs and g4_verdict at both judgements (the
first and the EMC re-judge), emc_args."""
from __future__ import annotations

import rp_paths  # noqa: F401  first: puts server/ and server/tools/ on sys.path
import gates
import rp_context
import rp_hooks
import rp_kicad

G4_FIX_ROUNDS = 2                                # corrective-action rounds at G4


@rp_context.stage
def stage_g4(ctx: rp_context.RunContext) -> bool:
    """G4: layout / board review (feeds CDR): judge, fix loop, EMC post-pass."""
    judge_g4(ctx)
    stage_g4_fixes(ctx)
    stage_emc(ctx)
    return rp_context.record_gate(ctx, ctx.g4)


def judge_g4(ctx: rp_context.RunContext, refresh_unconnected: bool = False) -> None:
    """DRC + routed-width check + G4 verdict on the current board."""
    ctx.drc_rep = rp_kicad.drc(ctx.pcb)
    ctx.report["drc"] = ctx.drc_rep
    if refresh_unconnected:
        ctx.route_rep = {**ctx.route_rep, "unconnected": ctx.drc_rep["unconnected"]}
        ctx.report["autoroute"] = ctx.route_rep
    ctx.routed_check = rp_kicad.gate_check("routed", ctx.pcb, ctx.netlist)
    ctx.g4 = g4_verdict(ctx)


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
    for them."""
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
    none the call is exactly the plain two-argument one."""
    extra = rp_hooks.concat("emc_args", ctx)
    if extra:
        return rp_kicad.emc_stitch(ctx.pcb, emc, extra)
    return rp_kicad.emc_stitch(ctx.pcb, emc)


def stage_emc(ctx: rp_context.RunContext) -> None:
    """EMC post-pass (opt-in via netlist "emc": {"stitch": true, ...}):
    perimeter GND via fence + interior stitching grid (Ott/Montrose, lambda/20
    pitch from max_freq_hz), then re-DRC — the pass is clearance-aware but
    the gate trusts measurements, not intentions. Never on a cached board."""
    emc = ctx.design.get("emc") or {}
    if not (ctx.g4["passed"] and emc.get("stitch") and not ctx.use_cache):
        return
    er = emc_stitch(ctx, emc)
    ctx.report["emc"] = er
    notes = []
    if er.get("ok"):
        notes.append(f"EMC stitching: {er.get('vias_added')} GND vias "
                     f"(fence {er.get('fence')}, interior "
                     f"{er.get('interior')}, pitch {er.get('pitch_mm')} mm)")
        ctx.drc_rep = rp_kicad.drc(ctx.pcb)
        ctx.report["drc"] = ctx.drc_rep
    else:
        notes.append(f"EMC stitching FAILED: {er.get('reason')}")
    if emc.get("teardrops"):
        notes.append("teardrops requested but KiCad's teardrop generator "
                     "has no headless API — generate in the GUI "
                     "(Edit > Edit Teardrops) before fab if required")
    ctx.g4 = g4_verdict(ctx, tuple(notes))
