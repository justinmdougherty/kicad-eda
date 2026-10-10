"""Pipeline feature 'd7' (ADR D7 wave 2a, D7-24a): stackups, layer roles and controlled
impedance in the gated harness, through rp_hooks hooks only (no stage module is edited). The
logic is pipeline/stages_d7.py; this module wires it to the hooks, keeps the feature state in
ctx.ext['d7'] and writes the report keys.

  g1_prepare        a napkin netlist ("mode": "napkin") that declares no layer count gets
                    the ADR D7.11 default (layer_policy.napkin_default) written into the
                    netlist atomically: one G1 remediation-log entry and its decision
                    (stage requirements, confidence med, override stackup.preset);
                    report['layer_policy'] {applied, ...} or {applied: false, reason}
  after_g2          stage_physics: physics_resolve.write_artifact(netlist, board_path=pcb)
                    writes <netlist stem>.stackup.json and the managed <board stem>.kicad_dru
                    before G3 builds the board -> report['physics']; its errors are a G2 Halt.
                    Then the canary: drc_guard.rules_compile on the board's rules file with
                    each board kicad-cli -> report['dru']; a file that does not compile is a G2
                    Halt (R23). The guarded DRC itself is rp_kicad.drc (F5-11, drc_guard)
  route_hash_extra  {'physics_data_version': ...}: the materials / fab / tline data the
                    artifact is resolved from; the netlist keys rf and layer_policy are
                    ROUTE_HASH_DESIGN_KEYS; a part's clock_hz stays unhashed (it moves no copper)
  handoff_veto      a board with impedance-controlled nets, R27 / R28 role rules the DRU cannot
                    express or RF chains is never handed to the KiCad 11 agent (ADR D7.19): the
                    reason becomes a G4 FAIL (stages_d7.VETO_REASON ...)
  g4_verdict        swig_layer_role_checks.py on the routed board (once per board content):
                    R8 issues are G4 blockers, R27 / R28 G4 warnings -> report['layer_roles'];
                    a board pcbnew cannot load (a resumed KiCad 11 board) gets the blocker
                    'D7 layer-role checks cannot run on a KiCad 11 board' when the design has
                    such rules, else a warning that R8 was judged by the managed DRU only; a
                    check that cannot run is a blocker naming why. report['dsn_policy']: the
                    DSN layer policy of the artifact and what the routed DSN carries (a DSN
                    without it, or with another one, is a G4 warning)
  emc_args          --stackup=<artifact> for emc_stitch.py (roles, single-sided guard)
  g5_verdict        the job file, IPC-2581, stackup CSV and fab notes export_fab_package wrote
                    (R24), checked on disk and in the zip -> report['fab']['stackup']; each
                    problem is a G5 blocker
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import rp_paths  # noqa: F401  first: puts server/ and server/tools/ on sys.path
import physics_resolve as pr
import rp_hooks
import stages_d7 as s7

ORDER = 40
EXT_KEY = "d7"                                # ctx.ext key = the feature name (rp_hooks)
ROUTE_HASH_DESIGN_KEYS: tuple[str, ...] = ("rf", "layer_policy")
# the report keys this feature writes (string literals of an rp_*.py: tests/test_pipeline_doc.py)
LAYER_POLICY_KEY = "layer_policy"
PHYSICS_KEY = "physics"
DRU_KEY = "dru"
ROLES_KEY = "layer_roles"
DSN_KEY = "dsn_policy"
FAB_KEY = "fab"
FAB_STACKUP_KEY = "stackup"
HASH_KEY = "physics_data_version"
G1_ROUND = 1


# --- feature state ------------------------------------------------------------------------
def _state(ctx: Any) -> dict:
    return ctx.ext.get(EXT_KEY) or {}


def _update(ctx: Any, **values: Any) -> None:
    ctx.ext[EXT_KEY] = {**_state(ctx), **values}


def _artifact(ctx: Any) -> tuple[str | None, dict | None]:
    """(path, content) of the run's artifact: the one stage_physics wrote, else the one on
    disk beside the netlist (a stage driven on its own); (None, None) without one."""
    state = _state(ctx)
    if state.get("doc") is not None:
        return state["artifact"], state["doc"]
    path = pr.artifact_path(ctx.netlist)
    doc = s7.load_artifact(path)
    return (str(path), doc) if doc is not None else (None, None)


def _design(ctx: Any) -> dict:
    return ctx.design if isinstance(ctx.design, dict) else {}


# --- G1: the napkin layer default ------------------------------------------------------------
def g1_prepare(ctx: Any) -> None:
    """The g1_prepare hook: stage_layer_policy (module doc)."""
    done = s7.apply_layer_default(ctx.netlist, ctx.design)
    if done is None:
        ctx.report[LAYER_POLICY_KEY] = {"applied": False,
                                        "reason": s7.not_applied_reason(ctx.design)}
        return
    ctx.design, nd = done
    ctx.log_remediation("G1", G1_ROUND, [s7.remediation_note(nd)], record=False)
    d = nd.decision_draft
    entry = ctx.decide(d["stage"], d["question"], d["decision"], d["rationale"],
                       confidence=d["confidence"], source=d["source"],
                       alternatives=d["alternatives"], reversible=d["reversible"],
                       override=d["override"])
    ctx.report[LAYER_POLICY_KEY] = s7.policy_record(nd, entry)


# --- after G2: stage_physics and the canary -----------------------------------------------------
def after_g2(ctx: Any) -> None:
    """The after_g2 hook: stage_physics, then the canary (module doc)."""
    stage_physics(ctx)
    stage_rules(ctx)


def stage_physics(ctx: Any) -> None:
    """The artifact and the managed rules of the board this run builds; errors halt G2."""
    try:
        written = s7.resolve_physics(ctx.netlist, ctx.design, ctx.pcb)
    except (OSError, ValueError, TypeError) as exc:
        ctx.report[PHYSICS_KEY] = s7.physics_failure(exc)
        raise rp_hooks.Halt("G2", f"{s7.PHYSICS_HALT}: the artifact was not written: "
                                  f"{type(exc).__name__}: {exc}") from exc
    doc = s7.load_artifact(written.get("path"))
    ctx.report[PHYSICS_KEY] = s7.physics_summary(written, doc)
    _update(ctx, artifact=written.get("path") if doc is not None else None, doc=doc,
            roles={}, dsn=None)
    problems = s7.physics_problems(written)
    if problems:
        raise rp_hooks.Halt("G2", f"{s7.PHYSICS_HALT}: {'; '.join(problems)}")


def stage_rules(ctx: Any) -> None:
    """The canary on the board's rules file (R23): report['dru']; a G2 Halt when it fails."""
    compiled = s7.compile_rules(ctx.pcb)
    ctx.report[DRU_KEY] = s7.rules_record(compiled, _artifact(ctx)[1])
    if not compiled["ok"]:
        raise rp_hooks.Halt("G2", f"{s7.RULES_HALT}: {Path(compiled['path']).name} does not "
                                  f"compile: {compiled.get('reason')}")


# --- route hash and the handoff veto ---------------------------------------------------------
def route_hash_extra(design: dict, netlist_dir: Path | None) -> dict[str, str]:
    """The route_hash_extra hook: the physics data version (the netlist keys are hashed by
    the core registry and ROUTE_HASH_DESIGN_KEYS)."""
    return {HASH_KEY: s7.physics_data_version()}


def handoff_veto(ctx: Any) -> str | None:
    """The handoff_veto hook: stages_d7.veto of the run's artifact (else the design)."""
    return s7.veto(_artifact(ctx)[1], _design(ctx))


# --- G4 ----------------------------------------------------------------------------------------
def g4_verdict(ctx: Any, verdict: dict) -> dict | None:
    """The g4_verdict hook: the layer-role judgement and the DSN policy (module doc)."""
    path, doc = _artifact(ctx)
    state = _state(ctx)
    policy = state.get("dsn") or s7.dsn_policy_report(doc, path, s7.dsn_path(ctx.pcb))
    roles = s7.judge_roles(ctx.pcb, ctx.netlist, path, doc, _design(ctx),
                           state.get("roles") or {})
    _update(ctx, dsn=policy, roles={**(state.get("roles") or {}), **roles.checked})
    ctx.report[DSN_KEY] = policy[0]
    ctx.report[ROLES_KEY] = roles.report
    return s7.amend(verdict, roles.blockers, [*roles.warnings, *policy[1]])


def emc_args(ctx: Any) -> list[str]:
    """The emc_args hook: the artifact for emc_stitch.py ([] without one)."""
    return s7.emc_args(_artifact(ctx)[0])


# --- G5 ----------------------------------------------------------------------------------------
def _board_artifact(ctx: Any) -> tuple[str | None, dict | None]:
    """The artifact export_fab_package used: <board stem>.stackup.json, else the netlist's."""
    path = s7.board_artifact_path(ctx.pcb)
    doc = s7.load_artifact(path) if path.is_file() else None
    return (str(path), doc) if doc is not None else _artifact(ctx)


def g5_verdict(ctx: Any, verdict: dict) -> dict | None:
    """The g5_verdict hook: the fab outputs that carry the stackup (R24) -> report['fab']
    ['stackup']; each problem is a G5 blocker."""
    _path, doc = _board_artifact(ctx)
    fab = ctx.report.get(FAB_KEY) or {}
    zip_path = str(Path(ctx.pcb).with_name(fab["zip"])) if fab.get("zip") else None
    check = (s7.fab_stackup_check(ctx.pcb, doc, zip_path) if doc is not None
             else {"checked": False, "problems": [s7.NO_ARTIFACT]})
    ctx.report[FAB_KEY] = {**fab, FAB_STACKUP_KEY: check}
    return s7.amend(verdict, s7.fab_blockers(check), [])
