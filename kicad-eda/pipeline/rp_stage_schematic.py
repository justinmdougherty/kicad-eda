"""Harness stages up to the schematic: load the netlist, G1 (requirements /
netlist lint) and G2 (schematic build + ERC + design and thermal review, with
its corrective-action loop). Feature hooks (rp_hooks): on_load, g1_prepare,
lint_kwargs, g1_verdict, g2_policy, analysis_kwargs, g2_verdict, after_g2.

Napkin mode (ROADMAP D5, rp_napkin): stage_load settles the mode and applies
the open-editor hard stop; G1 and G2 are judged with napkin=ctx.napkin, and a
napkin G2 round applies the policy remedies with one decision each. Strict
mode is the harness as it was. A board SWIG cannot read (the agent's KiCad 11
board) is kept as <stem>.agent.* at load, in both modes, before G2 re-saves
its project."""
from __future__ import annotations

import json
from pathlib import Path

import rp_paths  # noqa: F401  first: puts server/ and server/tools/ on sys.path
import design_report as drpt
import gates
import kicad_server as k
import lint_netlist as lint
import remediate
import rp_context
import rp_hooks
import rp_napkin
import thermal_check
import toolchain as tc

G2_ROUNDS = 3                                    # corrective-action rounds at G2


@rp_context.stage
def stage_load(ctx: rp_context.RunContext) -> None:
    """Read the netlist, settle the mode (rp_napkin.resolve_mode), apply the
    D5 hard stop (rp_napkin.hard_stop: napkin siblings, or a G1 halt) and
    keep a board SWIG cannot read before anything rewrites it; then the stock
    symbols and the on_load hook."""
    ctx.design = json.loads(Path(ctx.netlist).read_text(encoding="utf-8"))
    rp_napkin.resolve_mode(ctx)
    rp_napkin.hard_stop(ctx)
    preserve_unreadable_board(ctx)
    share = k._kicad_share()
    ctx.syms = str(share / "symbols") if share else ""
    rp_hooks.notify("on_load", ctx)


def preserve_unreadable_board(ctx: rp_context.RunContext) -> None:
    """A board saved by a KiCad newer than the pcbnew install (agent routing
    through the KiCad 11 API) is the agent's work; it always misses the route
    cache, so G3 rebuilds over it. Keep it -- board, .kicad_pro, .kicad_prl
    (and .kicad_dru) byte for byte, tc.keep_agent_copy(move=False), an older
    agent copy rotated to <stem>.agent.<n>.* -- before G2 re-saves the
    project; a copy that cannot be made stops the run (RuntimeError) before
    anything is built. A set already kept with the same bytes is not copied
    again."""
    pcb = Path(ctx.pcb)
    if not pcb.is_file() or tc.swig_readable(ctx.pcb) is None:
        return
    keep = tc.agent_copy_path(ctx.pcb)
    if not already_kept(ctx.pcb):
        try:
            kept = tc.keep_agent_copy(ctx.pcb, move=False)
        except (OSError, ValueError) as e:
            raise RuntimeError(f"{pcb.name} was saved by a newer KiCad and could not be "
                               f"preserved as {Path(keep).name} before the rebuild: {e}") from e
        if kept["previous_agent_copy"]:
            ctx.report["agent_board_previous"] = kept["previous_agent_copy"]
    ctx.report["agent_board_preserved"] = keep


def already_kept(pcb: str) -> bool:
    """True when <stem>.agent.* holds exactly the files of the canonical set
    (tc.AGENT_SET) with the same bytes."""
    base = str(Path(pcb).with_suffix(""))
    for ext in tc.AGENT_SET:
        src, dst = Path(base + ext), Path(base + tc.AGENT_TAG + ext)
        if src.is_file() != dst.is_file():
            return False
        if src.is_file() and src.read_bytes() != dst.read_bytes():
            return False
    return True


@rp_context.stage
def stage_g1(ctx: rp_context.RunContext) -> bool:
    """G1: requirements / netlist review (feeds SRR)."""
    rp_hooks.notify("g1_prepare", ctx)
    lrep = lint.lint(ctx.design, ctx.syms, **rp_hooks.merged("lint_kwargs", ctx))
    ctx.report["lint"] = {"ok": lrep["ok"], "errors": lrep["errors"],
                          "warnings": lrep["warnings"]}
    verdict = rp_hooks.chain("g1_verdict", ctx,
                             gates.g1_requirements(lrep, ctx.design, napkin=ctx.napkin))
    return rp_context.record_gate(ctx, verdict)


def g2_policy(ctx: rp_context.RunContext) -> tuple[int, bool]:
    """(rounds, remediate) of the G2 loop: G2_ROUNDS with remediation unless a
    feature's g2_policy says otherwise (F4 faithful: one round, none)."""
    policy = rp_hooks.first("g2_policy", ctx) or {}
    return policy.get("max_rounds", G2_ROUNDS), policy.get("remediate", True)


@rp_context.stage
def stage_g2(ctx: rp_context.RunContext) -> bool:
    """G2: schematic review (feeds PDR), with a corrective-action loop. A
    failing gate translates its blockers into tasks; the auto-resolvable ones
    (e.g. a missing bypass cap) are APPLIED to the design, the netlist is
    rewritten, the stage re-runs, and the gate is re-tested. Only tasks
    needing design intent stop the flow — with the open task list attached."""
    rounds, fix = g2_policy(ctx)
    g2: dict = {}
    for rnd in range(1, rounds + 1):
        g2 = g2_review(ctx)
        if g2["passed"] or not fix:
            break
        if not remediate_g2(ctx, g2["tasks"], rnd):
            break                       # only designer tasks remain — stop here
    passed = rp_context.record_gate(ctx, rp_hooks.chain("g2_verdict", ctx, g2))
    if passed:
        rp_hooks.notify("after_g2", ctx)
    return passed


def remediate_g2(ctx: rp_context.RunContext, tasks: list, rnd: int) -> bool:
    """Apply the auto tasks of a failed G2 round; True when the netlist was
    rewritten. Napkin: rp_napkin.apply_policies (one decision per remedy);
    strict: remediate.apply, each applied item a decision (log_remediation)."""
    if ctx.napkin:
        return rp_napkin.apply_policies(ctx, "G2", rnd, tasks)
    fixed, log = remediate.apply(ctx.design, tasks)
    if not log:
        return False
    ctx.design = fixed
    Path(ctx.netlist).write_text(json.dumps(fixed, indent=2), encoding="utf-8")
    ctx.log_remediation("G2", rnd, log)
    return True


def g2_review(ctx: rp_context.RunContext) -> dict:
    """One G2 round: build the schematic (+ ERC), design review, thermal review.
    kicad_server falls back from the API to the file backend itself when
    KiCad 11 has no library tables (ROADMAP D5) and records that decision;
    report['schematic'] carries its fallback and libraries."""
    r = k.build_schematic_from_netlist(ctx.netlist, ctx.sch)
    ctx.report["schematic"] = {kk: r.get(kk) for kk in rp_context.SCHEMATIC_REP_KEYS}
    dr = drpt.analyze(ctx.design, ctx.syms, **rp_hooks.merged("analysis_kwargs", ctx))
    ctx.analysis = dr
    ctx.report["design_report"] = {
        "decoupling_uncovered": dr["decoupling"]["uncovered"],
        "flags": dr["flags"],
        "errors": dr.get("errors", [])}
    ctx.trep = thermal_check.check(ctx.design)
    ctx.report["thermal"] = ctx.trep
    return gates.g2_schematic(r, dr, ctx.trep, napkin=ctx.napkin)
