#!/usr/bin/env python3
"""Formal design-review gates for the headless pipeline.

Maps the IEEE 15288.2 review structure onto the concept->Gerbers flow. Each gate
is a go/no-go decision point with explicit blockers: the pipeline MUST NOT
proceed past a failing gate (an error caught at the schematic gate costs
nothing; the same error surviving to fab costs a respin).

  G1 REQUIREMENTS  (feeds SRR)  netlist structurally sound, intent captured
  G2 SCHEMATIC     (feeds PDR)  ERC clean + design-intent review: decoupling
                                coverage, no floating pins, power tree sound
  G3 PLACEMENT     (pre-route)  no courtyard overlap, connectors mouth-out at
                                the board edge, antenna keepout present
  G4 LAYOUT        (feeds CDR)  fully routed, DRC clean, routed widths honor
                                the IPC net classes
  G5 FAB PACKAGE   (handoff)    complete, verified output package

The process FEEDS ITSELF: a failing gate does not just halt — every blocker
translates into a corrective TASK. Tasks marked auto=True can be applied by
`remediate.py` (e.g. add the missing bypass cap, loosen placement), after which
the stage re-runs and the gate is re-tested. Tasks needing design intent
(auto=False) stop the flow WITH the task list, so the designer knows exactly
what to complete before the gate is retried.

Napkin mode (D5, ``napkin=True`` on G1-G3): the cases napkin_policy allows
become auto tasks carrying their ``policy`` id — floating IC/module pins
(declare_no_connect / net_same_name_pin for a stacked SUPPLY pin only; an
op-amp's unused '+' never joins unit A's net), connectors facing the interior or
far from their edge (face_connector_out), a fractured inner plane
(loosen_placement) and a short EP via farm (swap_thermal_footprint); G1 warns
when a napkin netlist records no ``assumptions``. With napkin=False (the
default) every verdict, task text and auto flag is the strict behaviour.

Pure functions: each takes the stage artifacts already produced and returns
  {"gate", "name", "feeds", "passed", "blockers": [...], "warnings": [...],
   "tasks": [{"desc", "auto", "fix": {...}, "policy": str | None}]}
"""
from __future__ import annotations

import decisions
import napkin_policy as npol

NAPKIN_NO_ASSUMPTIONS = ("napkin mode: the netlist records no requirements-level "
                         "'assumptions' (D5) — every default taken must be listed with "
                         "its rationale and confidence")


def _verdict(gate: str, name: str, feeds: str,
             blockers: list, warnings: list, tasks: list | None = None) -> dict:
    return {"gate": gate, "name": name, "feeds": feeds,
            "passed": not blockers, "blockers": blockers, "warnings": warnings,
            "tasks": tasks or []}


def _task(desc: str, auto: bool, fix: dict | None = None,
          policy: str | None = None) -> dict:
    return {"desc": desc, "auto": auto, "fix": fix or {}, "policy": policy}


def g1_requirements(lint_rep: dict, design: dict, *, napkin: bool = False) -> dict:
    blockers = list(lint_rep.get("errors", []))
    warnings = list(lint_rep.get("warnings", []))
    if not design.get("nets"):
        blockers.append("netlist has no nets")
    if not design.get("net_specs"):
        warnings.append("no net_specs (voltage/current/impedance intent) — all "
                        "nets will use class-floor geometry")
    # the requirements-level assumptions block (D5): a malformed one blocks in
    # every mode (lint does not check it); napkin mode must record one
    blockers += decisions.validate_assumptions(design.get("assumptions"))
    if napkin and not design.get("assumptions"):
        warnings.append(NAPKIN_NO_ASSUMPTIONS)
    return _verdict("G1", "Requirements / netlist review", "SRR",
                    blockers, warnings)


def _floating_task(pin: str, detail: dict | None) -> dict:
    """The task for one floating pin: today's designer task, or (napkin, when
    design_report's floating_detail has the pin) the D5 policy's auto task."""
    designer = _task(f"net pin {pin} or declare it no_connect in the netlist", False)
    if not detail or not detail.get("ref") or detail.get("pad") is None:
        return designer
    ref, pad, name = detail["ref"], str(detail["pad"]), detail.get("name")
    comp = {"ref": ref, "lib_id": detail.get("lib_id"), "footprint": detail.get("footprint")}
    action = npol.floating_pin_action(detail, comp)
    if action[0] == "net_same_name_pin":
        fix = {"type": "net_same_name_pin", "ref": ref, "pad": pad, "net": action[1]}
        return _task(f"join {pin} ('{name}') to net {action[1]} (same-name pin on {ref})",
                     True, fix, npol.POLICY_FLOATING_PIN)
    if action[0] == "no_connect":
        fix = {"type": "declare_no_connect", "ref": ref, "pad": pad,
               "pin_type": detail.get("type"), "pin_name": name}
        return _task(f"declare {pin} no_connect ({npol.describe_pin(detail.get('type'), name)}; "
                     f"{action[1]} confidence: {action[2]})", True, fix,
                     npol.POLICY_FLOATING_PIN)
    return designer


def _g2_design_review(design_rep: dict, napkin: bool) -> tuple[list, list, list]:
    """(blockers, warnings, tasks) from design_report.analyze()'s result."""
    blockers, warnings, tasks = [], [], []
    # a symbol the design review could not read means its pin-type checks
    # (decoupling, power tree, floating pins) passed VACUOUSLY for that part —
    # the review did not run, so the gate cannot pass on its verdict
    for e in design_rep.get("errors", []):
        blockers.append(f"design review could not run: {e}")
        tasks.append(_task(f"make the symbol readable so the design review "
                           f"covers it: {e}", False))
    dec = design_rep.get("decoupling", {})
    for u in dec.get("uncovered", []):
        for rail in u["rails_without_cap"]:
            blockers.append(f"{u['ref']}: rail '{rail}' has no bypass cap")
            # unambiguous remedy — standard 100nF bypass at the IC, auto-applied
            tasks.append(_task(f"add 100nF bypass on {rail} at {u['ref']}", True,
                               {"type": "add_decoupling", "ref": u["ref"], "rail": rail}))
    flags = design_rep.get("flags", {})
    detail = ({d["pin"]: d for d in flags.get("floating_detail") or []
               if isinstance(d, dict) and d.get("pin")} if napkin else {})
    for p in flags.get("floating_pins", []):
        blockers.append(f"floating pin (not netted, not no_connect): {p}")
        # strict: needs design intent — a floating input could be a MISSED
        # connection, so silently no_connect-ing it would mask a real error.
        # napkin: only the pins D5 allows become auto tasks
        tasks.append(_floating_task(p, detail.get(p)))
    for n in flags.get("nets_missing_pull", []):
        warnings.append(f"net '{n}' may need a pull resistor")
    for n in flags.get("rails_missing_testpoint", []):
        warnings.append(f"power rail '{n}' has no test point for bring-up")
    for a in design_rep.get("dfa", {}).get("array_candidates", []):
        warnings.append(f"DFA: {a['count']}x {a['value']} could consolidate "
                        f"into an array part")
    return blockers, warnings, tasks


def g2_schematic(sch_rep: dict, design_rep: dict,
                 thermal_rep: dict | None = None, *, napkin: bool = False) -> dict:
    blockers, warnings, tasks = [], [], []
    for b in (thermal_rep or {}).get("blockers", []):
        blockers.append(f"thermal: {b}")
        # needs design intent: a hot part means re-spec, re-package, or
        # declared relief — never an automatic netlist edit
        tasks.append(_task(f"resolve thermal violation: {b}", False))
    for w in (thermal_rep or {}).get("warnings", []):
        warnings.append(f"thermal: {w}")
    if not sch_rep.get("ok"):
        blockers.append("schematic generation failed")
        tasks.append(_task("fix schematic generation error", False))
    erc = sch_rep.get("erc_violations")
    if erc is None:
        warnings.append("ERC did not run")
    elif erc > 0:
        blockers.append(f"ERC reports {erc} violation(s)")
        tasks.append(_task(f"resolve {erc} ERC violation(s)", False))
    b, w, t = _g2_design_review(design_rep, napkin)
    return _verdict("G2", "Schematic review (capture complete, pre-layout)",
                    "PDR", blockers + b, warnings + w, tasks + t)


def _g3_desc(fix: dict) -> str:
    kind = fix["type"]
    if kind == "face_connector_out" and fix.get("issue") == "far":
        return (f"move {fix['ref']} flush to the nearest board edge (mating face "
                f"{fix['dist_mm']} mm away)")
    if kind == "face_connector_out":
        return f"turn {fix['ref']} to face out at the nearest board edge (mating face inward)"
    if kind == "loosen_placement":
        return f"re-place with looser spacing: {fix['reason']}"
    return f"swap {fix['ref']} to its ...ThermalVias footprint variant"


def _placement_task(issue: str, napkin: bool) -> dict:
    """A placement issue's task: designer in strict mode; napkin turns the
    issues napkin_policy classifies into auto tasks."""
    fix = npol.classify_g3_issue(issue) if napkin else None
    if fix is None:
        return _task(f"fix placement: {issue}", False)
    return _task(_g3_desc(fix), True, fix, npol.POLICY_FOR[fix["type"]])


def g3_placement(board_rep: dict, courtyard_drc: dict,
                 place_issues: list, place_warnings: list | None = None,
                 *, napkin: bool = False) -> dict:
    blockers, warnings, tasks = [], list(place_warnings or []), []
    if not board_rep.get("ok"):
        why = (board_rep.get("error") or board_rep.get("reason")
               or board_rep.get("stderr") or "(no detail)")
        blockers.append(f"board generation failed: {why}")
        tasks.append(_task(f"fix board generation error: {why}", False))
    for m in board_rep.get("missing_pads") or []:
        blockers.append(f"pad not assignable: {m}")
        tasks.append(_task(f"correct netlist pad reference {m}", False))
    n_cy = courtyard_drc.get("courtyard_violations", 0)
    if n_cy:
        blockers.append(f"{n_cy} courtyard overlap(s) — parts collide")
        # remedy: re-place with looser spacing — the harness can do this itself
        tasks.append(_task("re-place with looser spacing (pack_scale+)", True,
                           {"type": "loosen_placement"}))
    for issue in place_issues:
        blockers.append(issue)
        tasks.append(_placement_task(issue, napkin))
    return _verdict("G3", "Placement review (placed, pre-routing)",
                    "pre-route hard gate", blockers, warnings, tasks)


def g4_layout(route_rep: dict, drc: dict, width_issues: list,
              check_warnings: list | None = None) -> dict:
    blockers, warnings, tasks = [], list(check_warnings or []), []
    unc = route_rep.get("unconnected")
    if unc is None:
        why = route_rep.get("error", "router produced no result")
        blockers.append(f"autoroute failed: {why}")
        tasks.append(_task(f"fix autoroute failure: {why}", False))
    elif unc > 0:
        blockers.append(f"{unc} net(s) unrouted")
        tasks.append(_task("re-route with more effort / looser placement", True,
                           {"type": "reroute"}))
    errs = drc.get("errors", 0)
    if errs:
        types = drc.get("types", {})
        blockers.append(f"DRC: {errs} violation(s) "
                        f"({', '.join(f'{k}x{v}' for k, v in types.items())})")
        tasks.append(_task(f"clear DRC violations: {types}", False))
    if drc.get("unconnected", 0):
        blockers.append(f"DRC: {drc['unconnected']} unconnected item(s)")
    for w in width_issues:
        blockers.append(w)
        tasks.append(_task(f"fix net-class width violation: {w}", False))
    return _verdict("G4", "Layout / board review (routed, pre-release)",
                    "CDR", blockers, warnings, tasks)


def g5_fab(fab_rep: dict, min_files: int = 10,
           require_assembly: bool = False,
           sourcing: dict | None = None) -> dict:
    blockers, warnings = [], []
    if not fab_rep.get("ok"):
        blockers.append("Gerber/drill export failed")
    n = fab_rep.get("files", 0)
    if isinstance(n, int) and n < min_files:
        blockers.append(f"fab package has only {n} file(s) (expected >= {min_files})")
    if require_assembly:
        if not fab_rep.get("bom"):
            blockers.append("assembly BOM.csv missing from the fab package")
        if not fab_rep.get("cpl"):
            blockers.append("assembly CPL.csv missing from the fab package")
    if sourcing:
        # a BOM without orderable part numbers is decorative, not a handoff.
        # Warning by default; netlist "require_sourcing": true makes it a
        # release blocker (contract-assembly posture).
        hard = sourcing.get("required")
        miss = sourcing.get("missing", [])
        if miss:
            msg = (f"{len(miss)} BOM part(s) have neither an LCSC number nor "
                   f"an MPN: {miss[:8]}{'...' if len(miss) > 8 else ''}")
            (blockers if hard else warnings).append(msg)
        for b in sourcing.get("bad", []):
            (blockers if hard else warnings).append(b)
    return _verdict("G5", "Fabrication package (handoff)", "release to fab",
                    blockers, warnings)


def summarize(gate_list: list) -> str:
    lines = []
    for g in gate_list:
        mark = "PASS" if g["passed"] else "FAIL"
        lines.append(f"[{mark}] {g['gate']} {g['name']} (feeds {g['feeds']})")
        for b in g["blockers"]:
            lines.append(f"    BLOCKER: {b}")
        for t in g.get("tasks", []):
            kind = "auto" if t["auto"] else "DESIGNER TASK"
            lines.append(f"    -> [{kind}] {t['desc']}")
        for w in g["warnings"]:
            lines.append(f"    warning: {w}")
    return "\n".join(lines)
