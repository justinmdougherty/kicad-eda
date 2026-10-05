"""Harness stages up to the schematic: load the netlist, G1 (requirements /
netlist lint) and G2 (schematic build + ERC + design and thermal review, with
its corrective-action loop). Feature hooks (rp_hooks): on_load, g1_prepare,
lint_kwargs, g1_verdict, g2_policy, analysis_kwargs, g2_verdict, after_g2."""
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
import thermal_check

G2_ROUNDS = 3                                    # corrective-action rounds at G2


@rp_context.stage
def stage_load(ctx: rp_context.RunContext) -> None:
    ctx.design = json.loads(Path(ctx.netlist).read_text(encoding="utf-8"))
    share = k._kicad_share()
    ctx.syms = str(share / "symbols") if share else ""
    rp_hooks.notify("on_load", ctx)


@rp_context.stage
def stage_g1(ctx: rp_context.RunContext) -> bool:
    """G1: requirements / netlist review (feeds SRR)."""
    rp_hooks.notify("g1_prepare", ctx)
    lrep = lint.lint(ctx.design, ctx.syms, **rp_hooks.merged("lint_kwargs", ctx))
    ctx.report["lint"] = {"ok": lrep["ok"], "errors": lrep["errors"],
                          "warnings": lrep["warnings"]}
    verdict = rp_hooks.chain("g1_verdict", ctx, gates.g1_requirements(lrep, ctx.design))
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
        fixed, log = remediate.apply(ctx.design, g2["tasks"])
        if not log:
            break                       # only designer tasks remain — stop here
        ctx.design = fixed
        Path(ctx.netlist).write_text(json.dumps(fixed, indent=2), encoding="utf-8")
        ctx.log_remediation("G2", rnd, log)
    passed = rp_context.record_gate(ctx, rp_hooks.chain("g2_verdict", ctx, g2))
    if passed:
        rp_hooks.notify("after_g2", ctx)
    return passed


def g2_review(ctx: rp_context.RunContext) -> dict:
    """One G2 round: build the schematic (+ ERC), design review, thermal review."""
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
    return gates.g2_schematic(r, dr, ctx.trep)
