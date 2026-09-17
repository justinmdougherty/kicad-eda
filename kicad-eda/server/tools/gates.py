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

Pure functions: each takes the stage artifacts already produced and returns
  {"gate", "name", "feeds", "passed", "blockers": [...], "warnings": [...],
   "tasks": [{"desc", "auto", "fix": {...}}]}
"""
from __future__ import annotations


def _verdict(gate: str, name: str, feeds: str,
             blockers: list, warnings: list, tasks: list | None = None) -> dict:
    return {"gate": gate, "name": name, "feeds": feeds,
            "passed": not blockers, "blockers": blockers, "warnings": warnings,
            "tasks": tasks or []}


def g1_requirements(lint_rep: dict, design: dict) -> dict:
    blockers = list(lint_rep.get("errors", []))
    warnings = list(lint_rep.get("warnings", []))
    if not design.get("nets"):
        blockers.append("netlist has no nets")
    if not design.get("net_specs"):
        warnings.append("no net_specs (voltage/current/impedance intent) — all "
                        "nets will use class-floor geometry")
    return _verdict("G1", "Requirements / netlist review", "SRR",
                    blockers, warnings)


def g2_schematic(sch_rep: dict, design_rep: dict,
                 thermal_rep: dict | None = None) -> dict:
    blockers, warnings, tasks = [], [], []
    for b in (thermal_rep or {}).get("blockers", []):
        blockers.append(f"thermal: {b}")
        # needs design intent: a hot part means re-spec, re-package, or
        # declared relief — never an automatic netlist edit
        tasks.append({"desc": f"resolve thermal violation: {b}",
                      "auto": False, "fix": {}})
    for w in (thermal_rep or {}).get("warnings", []):
        warnings.append(f"thermal: {w}")
    if not sch_rep.get("ok"):
        blockers.append("schematic generation failed")
        tasks.append({"desc": "fix schematic generation error", "auto": False,
                      "fix": {}})
    erc = sch_rep.get("erc_violations")
    if erc is None:
        warnings.append("ERC did not run")
    elif erc > 0:
        blockers.append(f"ERC reports {erc} violation(s)")
        tasks.append({"desc": f"resolve {erc} ERC violation(s)", "auto": False,
                      "fix": {}})

    # a symbol the design review could not read means its pin-type checks
    # (decoupling, power tree, floating pins) passed VACUOUSLY for that part —
    # the review did not run, so the gate cannot pass on its verdict
    for e in design_rep.get("errors", []):
        blockers.append(f"design review could not run: {e}")
        tasks.append({"desc": f"make the symbol readable so the design review "
                              f"covers it: {e}", "auto": False, "fix": {}})

    dec = design_rep.get("decoupling", {})
    for u in dec.get("uncovered", []):
        for rail in u["rails_without_cap"]:
            blockers.append(f"{u['ref']}: rail '{rail}' has no bypass cap")
            # unambiguous remedy — standard 100nF bypass at the IC, auto-applied
            tasks.append({"desc": f"add 100nF bypass on {rail} at {u['ref']}",
                          "auto": True,
                          "fix": {"type": "add_decoupling",
                                  "ref": u["ref"], "rail": rail}})
    flags = design_rep.get("flags", {})
    for p in flags.get("floating_pins", []):
        blockers.append(f"floating pin (not netted, not no_connect): {p}")
        # needs design intent: a floating input could be a MISSED connection,
        # so silently no_connect-ing it would mask a real error
        tasks.append({"desc": f"net pin {p} or declare it no_connect in the "
                              f"netlist", "auto": False, "fix": {}})
    for n in flags.get("nets_missing_pull", []):
        warnings.append(f"net '{n}' may need a pull resistor")
    for n in flags.get("rails_missing_testpoint", []):
        warnings.append(f"power rail '{n}' has no test point for bring-up")
    for a in design_rep.get("dfa", {}).get("array_candidates", []):
        warnings.append(f"DFA: {a['count']}x {a['value']} could consolidate "
                        f"into an array part")
    return _verdict("G2", "Schematic review (capture complete, pre-layout)",
                    "PDR", blockers, warnings, tasks)


def g3_placement(board_rep: dict, courtyard_drc: dict,
                 place_issues: list, place_warnings: list | None = None) -> dict:
    blockers, warnings, tasks = [], list(place_warnings or []), []
    if not board_rep.get("ok"):
        why = (board_rep.get("error") or board_rep.get("reason")
               or board_rep.get("stderr") or "(no detail)")
        blockers.append(f"board generation failed: {why}")
        tasks.append({"desc": f"fix board generation error: {why}",
                      "auto": False, "fix": {}})
    for m in board_rep.get("missing_pads", []):
        blockers.append(f"pad not assignable: {m}")
        tasks.append({"desc": f"correct netlist pad reference {m}",
                      "auto": False, "fix": {}})
    n_cy = courtyard_drc.get("courtyard_violations", 0)
    if n_cy:
        blockers.append(f"{n_cy} courtyard overlap(s) — parts collide")
        # remedy: re-place with looser spacing — the harness can do this itself
        tasks.append({"desc": "re-place with looser spacing (pack_scale+)",
                      "auto": True, "fix": {"type": "loosen_placement"}})
    for issue in place_issues:
        blockers.append(issue)
        tasks.append({"desc": f"fix placement: {issue}", "auto": False, "fix": {}})
    return _verdict("G3", "Placement review (placed, pre-routing)",
                    "pre-route hard gate", blockers, warnings, tasks)


def g4_layout(route_rep: dict, drc: dict, width_issues: list,
              check_warnings: list | None = None) -> dict:
    blockers, warnings, tasks = [], list(check_warnings or []), []
    unc = route_rep.get("unconnected")
    if unc is None:
        why = route_rep.get("error", "router produced no result")
        blockers.append(f"autoroute failed: {why}")
        tasks.append({"desc": f"fix autoroute failure: {why}",
                      "auto": False, "fix": {}})
    elif unc > 0:
        blockers.append(f"{unc} net(s) unrouted")
        tasks.append({"desc": "re-route with more effort / looser placement",
                      "auto": True, "fix": {"type": "reroute"}})
    errs = drc.get("errors", 0)
    if errs:
        types = drc.get("types", {})
        blockers.append(f"DRC: {errs} violation(s) "
                        f"({', '.join(f'{k}x{v}' for k, v in types.items())})")
        tasks.append({"desc": f"clear DRC violations: {types}", "auto": False,
                      "fix": {}})
    if drc.get("unconnected", 0):
        blockers.append(f"DRC: {drc['unconnected']} unconnected item(s)")
    for w in width_issues:
        blockers.append(w)
        tasks.append({"desc": f"fix net-class width violation: {w}",
                      "auto": False, "fix": {}})
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
