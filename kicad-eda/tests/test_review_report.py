"""review_report — the D5 human review report (<stem>_review.md), including
its HUMAN REVIEW REQUIRED banner (the detector itself is tested in
test_safety_screen.py).

Pure unit tier, no KiCad. The golden-ish fixture is the pipeline report the
gated harness printed for examples/buck_5v3a in the kicad-eda Docker image
(KiCad 10.0, 2026-10-04 rerun), embedded verbatim below.
"""
# BUCK_REPORT_JSON is embedded verbatim, so its long lines cannot be wrapped:
# ruff: noqa: E501
from __future__ import annotations

import ast
import copy
import hashlib
import json
import re
import sys
from pathlib import Path

import pytest

import decisions as dec
import design_report
import gates
import review_report as rr
import safety_screen

TOOLS = Path(__file__).resolve().parents[1] / "server" / "tools"

# run_pipeline's JSON report for examples/buck_5v3a (verbatim from run.log)
BUCK_REPORT_JSON = r"""{
"netlist": "/work/netlist.json",
"gates": [{"gate": "G1", "name": "Requirements / netlist review", "feeds": "SRR", "passed": true, "blockers": [], "warnings": [], "tasks": []}, {"gate": "G2", "name": "Schematic review (capture complete, pre-layout)", "feeds": "PDR", "passed": true, "blockers": [], "warnings": ["power rail 'SW' has no test point for bring-up", "power rail 'V5' has no test point for bring-up", "power rail 'VIN' has no test point for bring-up", "power rail 'VIN_RAW' has no test point for bring-up"], "tasks": []}, {"gate": "G3", "name": "Placement review (placed, pre-routing)", "feeds": "pre-route hard gate", "passed": true, "blockers": [], "warnings": [], "tasks": []}, {"gate": "G4", "name": "Layout / board review (routed, pre-release)", "feeds": "CDR", "passed": true, "blockers": [], "warnings": [], "tasks": []}, {"gate": "G5", "name": "Fabrication package (handoff)", "feeds": "release to fab", "passed": true, "blockers": [], "warnings": ["10 BOM part(s) have neither an LCSC number nor an MPN: ['J1', 'F1', 'U1', 'L1', 'D1', 'C1', 'C2', 'C3']..."], "tasks": []}],
"lint": {"ok": true, "errors": [], "warnings": []},
"schematic": {"ok": true, "backend": "file", "components": 10, "nets": 5, "erc_ok": true, "erc_violations": 0},
"design_report": {"decoupling_uncovered": [], "flags": {"floating_pins": [], "nets_missing_pull": [], "rails_missing_testpoint": ["SW", "V5", "VIN", "VIN_RAW"]}, "errors": []},
"thermal": {"blockers": [], "warnings": [], "ep_via_parts": ["U1"]},
"route_cache": "miss",
"remediation": [{"gate": "G3", "round": 1, "applied": ["U1: 36 thermal via(s) 0.3 mm drill under the EP on net GND (IPC-7093 via farm)"]}],
"renders_placement": ["/work/netlist_place_top.png", "/work/netlist_place_bottom.png"],
"board": {"ok": true, "footprints": 10, "pads_assigned": 24, "zones": ["In1.Cu:GND", "In2.Cu:VIN_RAW"], "antenna_keepout": null, "mounting_holes": [], "missing_pads": [], "error": null, "reason": null, "stderr": null},
"autoroute": {"attempt": 1, "pack_scale": 1.0, "tracks": 59, "unconnected": 0},
"drc": {"errors": 0, "unconnected": 0, "types": {}, "kicad_cli": "/usr/bin/kicad-cli"},
"fab": {"ok": true, "zip": "netlist_JLCPCB.zip", "files": 31, "bom": true, "cpl": true},
"renders_final": ["/work/netlist_final_top.png", "/work/netlist_final_bottom.png"],
"sourcing": {"missing": ["J1", "F1", "U1", "L1", "D1", "C1", "C2", "C3", "C4", "J2"], "bad": [], "required": false},
"PASS": true,
"gate_summary": "[PASS] G1 Requirements / netlist review (feeds SRR)\n[PASS] G2 Schematic review (capture complete, pre-layout) (feeds PDR)\n    warning: power rail 'SW' has no test point for bring-up\n    warning: power rail 'V5' has no test point for bring-up\n    warning: power rail 'VIN' has no test point for bring-up\n    warning: power rail 'VIN_RAW' has no test point for bring-up\n[PASS] G3 Placement review (placed, pre-routing) (feeds pre-route hard gate)\n[PASS] G4 Layout / board review (routed, pre-release) (feeds CDR)\n[PASS] G5 Fabrication package (handoff) (feeds release to fab)\n    warning: 10 BOM part(s) have neither an LCSC number nor an MPN: ['J1', 'F1', 'U1', 'L1', 'D1', 'C1', 'C2', 'C3']..."
}"""


def _entry(stage: str, confidence: str, decision: str, ts: str = "2026-10-04T10:00:00+00:00",
           **over) -> dict:
    digest = hashlib.sha1(f"{stage}|{decision}".encode()).hexdigest()[:12]
    entry = {"id": f"dec-{digest}", "ts": ts,
             "stage": stage, "question": f"question for {decision}?", "decision": decision,
             "rationale": f"because {decision}", "alternatives": [], "confidence": confidence,
             "source": "policy:test", "reversible": True, "override": None}
    entry.update(over)
    return entry


@pytest.fixture
def buck_report() -> dict:
    return json.loads(BUCK_REPORT_JSON)


@pytest.fixture
def buck_napkin(buck_design) -> dict:
    """buck_5v3a as napkin mode wrote it: mode + a requirements assumption block."""
    return {**buck_design, "mode": "napkin", "assumptions": [
        {"id": "A1", "topic": "input supply", "value": "12 V DC bench supply",
         "rationale": "the concept says 12 V in", "confidence": "high", "override": None},
        {"id": "A2", "topic": "ambient", "value": [0, 70], "rationale": "napkin default",
         "confidence": "low", "override": "thermal.t_ambient"},
        {"id": "A3", "topic": "quantity", "value": 5, "rationale": "prototype run",
         "confidence": "med", "override": None}]}


@pytest.fixture
def buck_decisions() -> list[dict]:
    return [
        _entry("G3", "high", "dropped an IPC-7093 via farm under U1's EP"),
        _entry("routing", "high", "Freerouting first (deterministic, no tokens)",
               alternatives=["agent routing"]),
        _entry("parts", "med", "LM2596S-5 fixed 5 V buck",
               source="datasheet:ti.com/lit/ds/symlink/lm2596.pdf"),
        _entry("parts", "low", "33uH 12x12 shielded inductor",
               ts="2026-10-04T10:05:00+00:00", override="components[L1].footprint",
               reversible=False),
        _entry("requirements", "med", "5 V 3 A output"),
    ]


@pytest.fixture
def buck_sources() -> dict:
    sources = {ref: "stock" for ref in ("J1", "F1", "U1", "D1", "C1", "C2", "C3", "C4", "J2")}
    sources["L1"] = {"symbol": "stock", "footprint": "generated"}
    return sources


def _section(md: str, title: str) -> str:
    """The body of '## <title>' up to the next '## ' heading."""
    m = re.search(rf"^## {re.escape(title)}\n(.*?)(?=^## |\Z)", md, re.S | re.M)
    assert m, f"section '{title}' missing"
    return m.group(1)


# --- the fixture is the real report shape ---------------------------------------

def test_fixture_is_a_real_pipeline_report(buck_report, buck_design):
    assert gates.summarize(buck_report["gates"]) == buck_report["gate_summary"]
    assert buck_design["name"] == "buck_5v3a"              # the netlist that report came from


# --- golden-ish: every D5 section on the buck report ----------------------------

def test_buck_review_has_every_d5_section_in_order(buck_report, buck_napkin, buck_decisions,
                                                    buck_sources):
    analysis = design_report.analyze(buck_napkin)
    md = rr.render_review(buck_report, buck_decisions, buck_napkin, analysis, buck_sources)
    headings = re.findall(r"^## (.+)$", md, re.M)
    assert tuple(headings) == rr.SECTIONS
    assert rr.SECTIONS == ("Summary", "Assumptions", "Decisions", "Parts", "Gate verdicts",
                           "Auto-remediations", "Accepted risks and warnings", "Routing",
                           "Board file", "Renders", "Fab package", "Open items")
    assert md.startswith("# Design review: LM2596 12V->5V 3A Buck Regulator\n")
    assert rr.HUMAN_REVIEW not in md                     # 12 V buck: not safety-relevant
    assert md.endswith("\n")


def test_buck_summary(buck_report, buck_napkin, buck_decisions, buck_sources):
    md = rr.render_review(buck_report, buck_decisions, buck_napkin, None, buck_sources)
    body = _section(md, "Summary")
    assert "- **Verdict: PASS**: all 5 gates passed (G1-G5)." in body
    assert "- Netlist: `/work/netlist.json`" in body
    assert "- Mode: napkin (hands-off)" in body
    assert "- Design: 10 components, 5 nets, IPC class 3" in body
    assert "- Schematic: `/work/netlist.kicad_sch` (backend file, ERC 0 violation(s))" in body
    assert "Pipeline report:" not in body               # the printed report has no path key
    rep = {**buck_report, "pipeline_report": "/work/netlist_pipeline_report.json"}
    assert "- Pipeline report: `/work/netlist_pipeline_report.json`" in \
        _section(rr.render_review(rep, [], buck_napkin), "Summary")


def test_buck_assumptions_low_confidence_first(buck_report, buck_napkin):
    md = rr.render_review(buck_report, [], buck_napkin)
    body = _section(md, "Assumptions")
    rows = [line for line in body.splitlines() if line.startswith("| ")]
    assert rows[0] == "| Confidence | ID | Topic | Value | Rationale | Override |"
    assert [r.split(" | ")[1] for r in rows[1:]] == ["A2", "A3", "A1"]
    assert "| low | A2 | ambient | [0, 70] | napkin default | `thermal.t_ambient` |" in rows


def test_buck_decisions_grouped_by_stage_low_first(buck_report, buck_napkin, buck_decisions):
    md = rr.render_review(buck_report, buck_decisions, buck_napkin)
    body = _section(md, "Decisions")
    assert "5 decision(s): 1 low, 2 med, 2 high confidence." in body
    stages = re.findall(r"^### (\S+)", body, re.M)
    assert stages == ["requirements", "parts", "G3", "routing"]   # D5 stage order
    parts = body.split("### parts")[1].split("###")[0]
    assert parts.index("33uH 12x12 shielded inductor") < parts.index("LM2596S-5 fixed 5 V buck")
    assert "**[low]**" in parts and "Reversible: no" in parts
    assert "Override: `components[L1].footprint`" in parts
    assert "Alternatives: agent routing" in body


def test_buck_parts_table_with_library_sources(buck_report, buck_napkin, buck_sources):
    analysis = design_report.analyze(buck_napkin)
    md = rr.render_review(buck_report, [], buck_napkin, analysis, buck_sources)
    body = _section(md, "Parts")
    rows = [line for line in body.splitlines() if line.startswith("| ")]
    assert rows[0] == "| Ref | Value | Symbol | Footprint | LCSC / MPN | Library source |"
    refs = [r.split(" | ")[0].lstrip("| ") for r in rows[1:]]
    assert refs == ["C1", "C2", "C3", "C4", "D1", "F1", "J1", "J2", "L1", "U1"]
    assert ("| U1 | LM2596S-5 | `Regulator_Switching:LM2596S-5` | "
            "`Package_TO_SOT_SMD:TO-263-5_TabPin3` | - | stock |") in rows
    assert "symbol stock, footprint generated |" in next(r for r in rows if r.startswith("| L1 "))
    assert "Library sources: stock 9, generated 1." in body
    assert "DFA: 10 placements, 9 unique line items." in body


def test_buck_gate_verdicts_reuse_gates_summarize(buck_report, buck_napkin):
    md = rr.render_review(buck_report, [], buck_napkin)
    body = _section(md, "Gate verdicts")
    assert f"```text\n{gates.summarize(buck_report['gates'])}\n```" in body


def test_multi_line_text_cannot_inject_headings_or_close_the_fence(buck_report, buck_design):
    """run_pipeline._stage_emc puts 'EMC stitching FAILED: <stderr tail>' into
    the G4 warnings: multi-line text, possibly with ``` or '## ' in it."""
    rep = copy.deepcopy(buck_report)
    rep["gates"][3]["warnings"] = ["EMC stitching FAILED: Traceback\n## Injected\n```\ntail"]
    rep["remediation"][0]["applied"].append("healed\n## Remediation injected")
    rep["sourcing"]["bad"] = ["C9: lcsc\n## Sourcing injected"]
    design = copy.deepcopy(buck_design)
    design["title"] = "Buck\n## Title injected"
    design["components"][0]["footprint"] = "Fuse:F\n## Footprint injected"
    md = rr.render_review(rep, [], design)
    headings = re.findall(r"^(#+) (.*)$", md, re.M)
    assert headings == ([("#", "Design review: Buck ## Title injected")]
                        + [("##", s) for s in rr.SECTIONS])
    body = _section(md, "Gate verdicts").strip("\n").splitlines()
    assert body[0] == "````text" and body[-1] == "````"   # longer than any run inside
    assert not any(line.startswith("````") for line in body[1:-1])
    assert "    warning: EMC stitching FAILED: Traceback ## Injected ``` tail" in body
    assert "- G4: EMC stitching FAILED: Traceback ## Injected ``` tail" in \
        _section(md, "Accepted risks and warnings")
    assert "- G3 round 1: healed ## Remediation injected" in _section(md, "Auto-remediations")
    assert "- Sourcing problem: C9: lcsc ## Sourcing injected" in _section(md, "Fab package")


def test_inline_code_survives_backticks(buck_report, buck_design):
    design = copy.deepcopy(buck_design)
    design["components"][0]["footprint"] = "Lib:`odd``name`"
    rows = _section(rr.render_review(buck_report, [], design), "Parts").splitlines()
    assert "| ``` Lib:`odd``name` ``` |" in next(r for r in rows if r.startswith("| J1 "))


def test_buck_remediations_risks_routing_board_renders_fab(buck_report, buck_napkin):
    md = rr.render_review(buck_report, [], buck_napkin)
    assert ("- G3 round 1: U1: 36 thermal via(s) 0.3 mm drill under the EP on net GND "
            "(IPC-7093 via farm)") in _section(md, "Auto-remediations")
    risks = _section(md, "Accepted risks and warnings")
    for rail in ("SW", "V5", "VIN", "VIN_RAW"):
        assert f"- G2: power rail '{rail}' has no test point for bring-up" in risks
    assert "- G5: 10 BOM part(s) have neither an LCSC number nor an MPN" in risks
    routing = _section(md, "Routing")
    assert "- Method: Freerouting" in routing
    assert "attempt 1 at pack_scale 1.0: 59 track(s), 0 unconnected" in routing
    assert "- Route cache: miss" in routing
    assert "- DRC: 0 error(s), 0 unconnected item(s) (`/usr/bin/kicad-cli`)" in routing
    board = _section(md, "Board file")
    assert "- Board: `/work/netlist.kicad_pcb`" in board
    assert "- Format: not recorded in the pipeline report" in board
    renders = _section(md, "Renders")
    assert "![placement, top](netlist_place_top.png)" in renders
    assert "![final, bottom](netlist_final_bottom.png)" in renders
    fab = _section(md, "Fab package")
    assert "- Zip: `netlist_JLCPCB.zip` (31 file(s), BOM yes, CPL yes)" in fab
    assert ("- Sourcing: 10 part(s) without an LCSC number or MPN: C1, C2, C3, C4, D1, F1, "
            "J1, J2, L1, U1") in fab


def test_mode_not_set_is_labelled_as_the_harness_default(buck_report, buck_design):
    assert "mode" not in buck_design
    body = _section(rr.render_review(buck_report, [], buck_design), "Summary")
    assert "- Mode: not set (the pipeline harness defaults to napkin behaviour, D5)" in body
    body = _section(rr.render_review(buck_report, [], {**buck_design, "mode": "gui"}), "Summary")
    assert "- Mode: gui" in body


@pytest.mark.parametrize("netlist, png, link", [
    # run_pipeline builds the PNG through pathlib: backslashes on Windows
    ("examples/buck_5v3a/netlist.json", r"examples\buck_5v3a\netlist_place_top.png",
     "netlist_place_top.png"),
    ("C:/work/buck/netlist.json", r"C:\work\buck\netlist_place_top.png", "netlist_place_top.png"),
    (r"C:\work\buck\netlist.json", r"C:\work\buck\netlist_place_top.png", "netlist_place_top.png"),
    ("c:/work/buck/netlist.json", r"C:\work\buck\netlist_place_top.png", "netlist_place_top.png"),
    (r".\buck\netlist.json", r"buck\netlist_place_top.png", "netlist_place_top.png"),
    ("../boards/netlist.json", r"..\boards\netlist_place_top.png", "netlist_place_top.png"),
    ("netlist.json", "netlist_place_top.png", "netlist_place_top.png"),
    ("/work/netlist.json", "/work/renders/netlist_place_top.png", "renders/netlist_place_top.png"),
    ("/work/a/netlist.json", "/work/b/netlist_place_top.png", "../b/netlist_place_top.png"),
    ("C:/my boards/netlist.json", r"C:\my boards\v1 (rev A)#2_place_top.png",
     "v1%20%28rev%20A%29%232_place_top.png"),
    ("C:/work/netlist.json", r"D:\renders\netlist_place_top.png",
     "file:///D:/renders/netlist_place_top.png"),
    ("work/netlist.json", "/abs/netlist_place_top.png", "file:///abs/netlist_place_top.png"),
    ("", "/abs/netlist_place_top.png", "file:///abs/netlist_place_top.png"),
    ("a/netlist.json", "../netlist_place_top.png", "../../netlist_place_top.png"),
    ("/../work/netlist.json", "/work/netlist_place_top.png", "netlist_place_top.png"),
])
def test_render_links_are_relative_to_the_review(netlist, png, link):
    """The review is written beside the netlist, so each link must resolve
    from the netlist folder whatever separators the two paths use."""
    rep = {"netlist": netlist, "gates": [], "renders_placement": [png]}
    body = _section(rr.render_review(rep, [], {}), "Renders")
    assert f"![placement, top]({link})" in body


@pytest.mark.parametrize("netlist, png", [
    ("../a/netlist.json", "b/netlist_place_top.png"),      # the review folder keeps a '..'
    ("/work/netlist.json", "renders/netlist_place_top.png"),   # relative to the run's cwd
    ("", "netlist_place_top.png"),                            # no netlist recorded
])
def test_render_paths_the_review_cannot_locate_are_shown_not_linked(netlist, png):
    """A relative render path is relative to the run's working directory;
    when the netlist path does not pin that directory down, any link would
    point at the wrong file, so the path is shown as recorded instead."""
    rep = {"netlist": netlist, "gates": [], "renders_placement": [png]}
    body = _section(rr.render_review(rep, [], {}), "Renders")
    assert "![" not in body
    assert (f"- placement, top: `{png}` (recorded relative to the run's working directory, "
            f"which the review cannot locate)") in body


def test_buck_open_items(buck_report, buck_napkin, buck_decisions):
    md = rr.render_review(buck_report, buck_decisions, buck_napkin)
    items = _section(md, "Open items")
    assert "Confirm low-confidence assumption A2 (ambient): [0, 70]" in items
    assert "Confirm low-confidence decision (parts): 33uH 12x12 shielded inductor" in items
    assert "Add an LCSC number or MPN for 10 part(s)" in items
    assert "Library sources were not supplied" in items
    assert "Design review" not in items                 # nothing failed


def test_parts_without_library_sources_say_not_supplied(buck_report, buck_design):
    """'unknown' means a library resolved nowhere; without library_sources
    nothing was looked up, which the column must not confuse with that."""
    body = _section(rr.render_review(buck_report, [], buck_design), "Parts")
    rows = [r for r in body.splitlines() if r.startswith("| ")][1:]
    assert len(rows) == 10 and all(r.endswith("| not supplied |") for r in rows)
    assert "unknown" not in body
    assert "Library sources were not supplied to the report." in body


# --- failure / crash paths --------------------------------------------------------

def test_failed_run_lists_blockers_tasks_and_unreached_gates(buck_report, buck_design):
    rep = copy.deepcopy(buck_report)
    g3 = gates.g3_placement({"ok": True, "missing_pads": ["U1.9"]},
                            {"courtyard_violations": 2}, ["J1: connector faces inward"])
    rep["gates"] = rep["gates"][:2] + [g3]
    rep["PASS"] = False
    for key in ("autoroute", "drc", "fab", "renders_final", "sourcing"):
        del rep[key]
    md = rr.render_review(rep, [], buck_design)
    summary = _section(md, "Summary")
    assert ("- **Verdict: FAIL**: stopped at G3 Placement review (placed, pre-routing) with "
            "3 blocker(s). Not reached: G4, G5.") in summary
    items = _section(md, "Open items")
    assert "G3 blocker: pad not assignable: U1.9" in items
    assert "G3 task [designer]: correct netlist pad reference U1.9" in items
    assert "G3 task [auto]: re-place with looser spacing (pack_scale+)" in items
    assert "Gates not reached: G4, G5" in items
    assert "- Not reached: the run stopped before routing." in _section(md, "Routing")
    assert "Not produced: the run stopped before G5." in _section(md, "Fab package")


def test_crash_before_load_still_renders(tmp_path):
    rep = {"netlist": "/w/n.json", "gates": [], "error": "RuntimeError: boom", "PASS": False}
    md = rr.render_review(rep, [], {})
    assert tuple(re.findall(r"^## (.+)$", md, re.M)) == rr.SECTIONS
    assert "- **Verdict: FAIL**: the run crashed: RuntimeError: boom" in md
    assert "Run error: RuntimeError: boom" in _section(md, "Open items")
    assert "No components" in _section(md, "Parts")


def test_report_write_errors_and_open_fixes_are_open_items(buck_report, buck_design):
    rep = {**buck_report, "pipeline_report_error": "could not write x: denied",
           "route_cache_error": "could not write y: denied",
           "remediation": buck_report["remediation"] + [
               {"gate": "G4", "round": 1, "applied": ["healed 2 gap(s)",
                                                      "OPEN: GND gap at (3, 4) too wide"]}]}
    md = rr.render_review(rep, [], buck_design)
    assert "- G4 round 1: OPEN (not fixed): GND gap at (3, 4) too wide" in \
        _section(md, "Auto-remediations")
    items = _section(md, "Open items")
    assert "Unfixed G4 issue: GND gap at (3, 4) too wide" in items
    assert "could not write x: denied" in items and "could not write y: denied" in items


def test_malformed_decisions_and_assumptions_are_reported(buck_report, buck_design):
    design = {**buck_design, "assumptions": [{"id": "A1", "topic": "t", "value": 1,
                                              "rationale": "r", "confidence": "maybe"}]}
    bad = _entry("G9", "high", "nope")
    md = rr.render_review(buck_report, [bad, "junk"], design,
                          decision_errors=["log:3: not JSON"])
    decisions_body = _section(md, "Decisions")
    assert "3 malformed log entry(ies) not shown" in decisions_body   # 2 entries + 1 line
    assert "empty or absent" not in decisions_body
    items = _section(md, "Open items")
    assert "Fix the decision log: log:3: not JSON" in items
    assert "stage must be one of" in items
    assert "assumptions[0] (id 'A1'): confidence must be one of" in items


def test_a_null_alternatives_entry_is_an_open_item_not_a_crash(buck_report, buck_design):
    null = _entry("G2", "high", "added C5", alternatives=None)
    md = rr.render_review(buck_report, [null, _entry("G3", "med", "via farm")], buck_design)
    decisions_body = _section(md, "Decisions")
    assert "via farm" in decisions_body and "added C5" not in decisions_body
    assert "1 malformed log entry(ies) not shown" in decisions_body
    assert ("Fix the decision log: decision entry 1: alternatives must be a list of strings, "
            "got null") in _section(md, "Open items")


def test_napkin_mode_without_assumptions_is_an_open_item(buck_report, buck_design):
    md = rr.render_review(buck_report, [], {**buck_design, "mode": "napkin"})
    assert "napkin mode but the netlist records no assumptions" in _section(md, "Open items")


def test_bad_report_shape_fails_loudly(buck_design):
    with pytest.raises(ValueError, match="pipeline report"):
        rr.render_review([], [], buck_design)
    with pytest.raises(ValueError, match=r"gates\[0\]"):
        rr.render_review({"gates": [{"gate": "G1"}]}, [], buck_design)


_G1 = {"gate": "G1", "name": "Requirements / netlist review", "feeds": "SRR", "passed": True,
       "blockers": [], "warnings": [], "tasks": []}


@pytest.mark.parametrize("report, decisions, design, match", [
    ({"gates": {"G1": _G1}}, [], {}, "'gates' must be a list"),
    ({"gates": [{**_G1, "tasks": ["redo it"]}]}, [], {}, r"gates\[0\]\.tasks\[0\]"),
    ({"gates": [{**_G1, "warnings": "one string"}]}, [], {}, r"gates\[0\]\.warnings"),
    ({"gates": []}, {"id": "dec-1"}, {}, "decisions must be a list"),
    ({"gates": []}, [], ["not", "a", "netlist"], "design must be the netlist JSON object"),
])
def test_bad_input_shapes_fail_loudly(report, decisions, design, match):
    with pytest.raises(ValueError, match=match):
        rr.render_review(report, decisions, design)


def test_verdict_without_a_pass_key_or_with_pass_false(buck_report, buck_design):
    no_key = {k: v for k, v in buck_report.items() if k != "PASS"}
    assert "- **Verdict: PASS**: all 5 gates passed (G1-G5)." in \
        _section(rr.render_review(no_key, [], buck_design), "Summary")
    false = {**buck_report, "PASS": False}
    assert "- **Verdict: FAIL**: the report says PASS is false." in \
        _section(rr.render_review(false, [], buck_design), "Summary")
    no_gates = {"netlist": "/w/netlist.json", "gates": []}
    assert "- **Verdict: FAIL**: no gate ran." in \
        _section(rr.render_review(no_gates, [], buck_design), "Summary")


def _handoff_report(buck_report: dict, **extra) -> dict:
    """buck_5v3a as an auto-mode run that handed routing to the agent after G4 failed."""
    rep = copy.deepcopy(buck_report)
    g4 = gates.g4_layout({"unconnected": 3}, {"errors": 0, "unconnected": 3}, [])
    rep["gates"] = rep["gates"][:3] + [g4]
    for key in ("fab", "renders_final", "sourcing"):
        del rep[key]
    return {**rep, "PASS": False, "status": "handoff",
            "handoff": "/w/netlist_route_handoff.json", **extra}


def test_handoff_verdict_names_the_resume_command(buck_report, buck_design):
    cmd = "/usr/bin/python3 /p/run_pipeline.py /w/netlist.json --resume routed"
    rep = _handoff_report(buck_report, handoff_resume={"routed": cmd, "stalled": "x"})
    summary = _section(rr.render_review(rep, [], buck_design), "Summary")
    assert (f"- **Verdict: HANDOFF**: routing handed to the agent; resume with `{cmd}`"
            in summary)
    assert "Verdict: FAIL" not in summary


def test_handoff_verdict_without_commands_points_at_the_handoff_report(buck_report,
                                                                       buck_design):
    md = rr.render_review(_handoff_report(buck_report), [], buck_design)
    assert ("- **Verdict: HANDOFF**: routing handed to the agent; resume with the command in "
            "`/w/netlist_route_handoff.json`") in _section(md, "Summary")
    crashed = {**_handoff_report(buck_report), "error": "RuntimeError: boom"}
    assert "- **Verdict: FAIL**: the run crashed: RuntimeError: boom" in \
        rr.render_review(crashed, [], buck_design)


def test_fab_export_failure_and_sourcing_problems(buck_report, buck_design):
    rep = {**buck_report, "fab": {"ok": False, "zip": None, "files": 0, "bom": False,
                                  "cpl": False},
           "sourcing": {"missing": [], "bad": ["C9: lcsc 'X12' is not Cxxxxx format"],
                        "required": False}}
    md = rr.render_review(rep, [], buck_design)
    body = _section(md, "Fab package")
    assert "- Zip: `(none)` (0 file(s), BOM no, CPL no)" in body
    assert "- Export FAILED (see G5)" in body
    assert "- Sourcing: every part has an LCSC number or MPN" in body
    assert "- Sourcing problem: C9: lcsc 'X12' is not Cxxxxx format" in body
    assert "Add an LCSC number" not in _section(md, "Open items")


def test_emc_stitching_result_is_shown_with_routing(buck_report, buck_design):
    ok = {**buck_report, "emc": {"ok": True, "vias_added": 42}}
    assert "- EMC stitching: 42 GND via(s)" in \
        _section(rr.render_review(ok, [], buck_design), "Routing")
    bad = {**buck_report, "emc": {"ok": False, "reason": "no GND zone\non In1.Cu"}}
    assert "- EMC stitching failed: no GND zone on In1.Cu" in \
        _section(rr.render_review(bad, [], buck_design), "Routing")


def test_uncovered_decoupling_from_the_analysis_is_an_open_item(buck_report, buck_design):
    analysis = {"decoupling": {"checked": [], "uncovered": [
        {"ref": "U1", "rails_without_cap": ["VIN", "V5"]}]}, "flags": {}, "errors": []}
    no_g2 = {"netlist": "/w/netlist.json", "gates": []}
    items = _section(rr.render_review(no_g2, [], buck_design, analysis), "Open items")
    assert "U1: rail 'VIN' has no bypass cap" in items
    assert "U1: rail 'V5' has no bypass cap" in items
    # a G2 verdict already carries the design review: nothing is listed twice
    items = _section(rr.render_review(buck_report, [], buck_design, analysis), "Open items")
    assert "bypass cap" not in items


# --- library sources ------------------------------------------------------------

def test_library_source_forms(buck_report, buck_design):
    sources = {"U1": "netlist", "C1": {"symbol": "project", "footprint": "project"}}
    md = rr.render_review(buck_report, [], buck_design, None, sources)
    rows = _section(md, "Parts")
    assert next(r for r in rows.splitlines() if r.startswith("| U1 ")).endswith("| project |")
    assert next(r for r in rows.splitlines() if r.startswith("| C1 ")).endswith("| project |")
    assert next(r for r in rows.splitlines() if r.startswith("| J1 ")).endswith("| unknown |")
    assert "Confirm the library of 8 part(s) with an unknown source" in _section(md, "Open items")
    with pytest.raises(ValueError, match="library source"):
        rr.render_review(buck_report, [], buck_design, None, {"U1": "vendor"})
    with pytest.raises(ValueError, match="unknown keys"):
        rr.render_review(buck_report, [], buck_design, None,
                         {"U1": {"symbol": "stock", "model": "stock"}})


def test_library_sources_for_uses_the_resolver(buck_design):
    table = {("symbol", "Regulator_Switching"): "netlist",
             ("footprint", "Inductor_SMD"): "generated"}

    def resolve(kind: str, nick: str):
        return table.get((kind, nick), "stock" if nick != "Fuse" else None)

    got = rr.library_sources_for(buck_design, resolve)
    assert got["U1"] == {"symbol": "project", "footprint": "stock"}
    assert got["L1"] == {"symbol": "stock", "footprint": "generated"}
    assert got["F1"] == {"symbol": "stock", "footprint": "unknown"}


# --- board file / routing variants -----------------------------------------------

def test_board_file_kicad9_dialect(buck_report, buck_design):
    rep = {**buck_report, "board_file": {"path": "/work/netlist.kicad_pcb",
                                         "format_version": 20241229, "generator": "pcbnew",
                                         "generator_version": "9.0"}}
    body = _section(rr.render_review(rep, [], buck_design), "Board file")
    assert "- Format: 20241229, written by pcbnew 9.0 (pre-KiCad 11 dialect: the SWIG " \
           "scripts and KiCad 9/10 can open it)" in body
    assert "- Pre-KiCad 11 snapshot: none needed" in body


def test_board_file_kicad11_with_and_without_snapshot(buck_report, buck_design):
    info = {"format_version": 20260901, "generator": "pcbnew", "generator_version": "10.99"}
    rep = {**buck_report, "board_file": info, "pre_k11_snapshot": "/work/netlist.k9.kicad_pcb",
           "agent_board_preserved": "/work/netlist.agent.kicad_pcb"}
    md = rr.render_review(rep, [], buck_design)
    body = _section(md, "Board file")
    assert "KiCad 11 dialect" in body
    assert "- Pre-KiCad 11 snapshot: `/work/netlist.k9.kicad_pcb`" in body
    assert "- Agent-routed KiCad 11 board preserved as `/work/netlist.agent.kicad_pcb`" in body
    md = rr.render_review({**rep, "pre_k11_snapshot": None}, [], buck_design)
    assert "KiCad 11 format and no pre-KiCad 11 snapshot was recorded" in _section(md, "Open items")


def test_routing_method_from_the_report_and_cache_hit(buck_report, buck_design):
    rep = {**buck_report, "routing": {"method": "agent", "requested": "auto",
                                      "why": "Freerouting left 3 nets unrouted",
                                      "handoff": "/work/netlist_route_handoff.md"}}
    body = _section(rr.render_review(rep, [], buck_design), "Routing")
    assert "- Method: agent (requested: auto)" in body
    assert "- Why: Freerouting left 3 nets unrouted" in body
    assert "- Handoff report: `/work/netlist_route_handoff.md`" in body
    rep = {**buck_report, "route_cache": "hit",
           "autoroute": {"cached": True, "pack_scale": 1.5, "tracks": 70, "unconnected": 0}}
    body = _section(rr.render_review(rep, [], buck_design), "Routing")
    assert "- Method: route cache hit" in body and "pack_scale 1.5" in body


def test_analysis_fills_in_when_the_report_has_no_g2(buck_design):
    rep = {"netlist": "/w/netlist.json", "gates": []}
    analysis = {"title": "x", "dfa": {"placements": 1, "unique_line_items": 1},
                "flags": {"floating_pins": ["U1.7"], "nets_missing_pull": ["I2C_SDA"],
                          "rails_missing_testpoint": []},
                "errors": ["pin types unavailable for X:Y (U9): SymbolLibError: gone"]}
    md = rr.render_review(rep, [], buck_design, analysis)
    items = _section(md, "Open items")
    assert "Design review could not run: pin types unavailable for X:Y (U9)" in items
    assert "Floating pin (not netted, not no_connect): U1.7" in items
    assert "- design review: net 'I2C_SDA' may need a pull resistor" in \
        _section(md, "Accepted risks and warnings")


def test_table_cells_are_escaped(buck_report, buck_design):
    design = copy.deepcopy(buck_design)
    design["components"][0]["value"] = "12V|IN\nraw"
    rows = _section(rr.render_review(buck_report, [], design), "Parts").splitlines()
    j1 = next(r for r in rows if r.startswith("| J1 "))
    assert "12V\\|IN raw" in j1 and j1.count(" | ") == 5


# --- banner + G5 warning -----------------------------------------------------------

def test_banner_is_at_the_very_top_and_repeated_in_open_items(buck_report, buck_design):
    design = copy.deepcopy(buck_design)
    design["net_specs"]["VIN_RAW"]["voltage"] = 170.0
    md = rr.render_review(buck_report, [], design)
    assert md.startswith(f"> **{rr.HUMAN_REVIEW}**")
    banner, title = md.split("\n# Design review: ", 1)
    assert "net 'VIN_RAW' is specified at 170 V (net_specs)" in banner
    assert "mains or > 50 V" in banner
    assert f"{rr.HUMAN_REVIEW}: mains or > 50 V" in _section(md, "Open items")
    assert "built anyway (D5)" in _section(md, "Accepted risks and warnings")


def test_charger_banner_and_the_re_exported_safety_api(buck_report, buck_design):
    """Callers (run_pipeline G5, review_tools) use review_report's names."""
    for name in ("detect_safety", "safety_warnings", "safety_lines", "SafetyFlag",
                 "HUMAN_REVIEW", "SAFETY_MAINS", "SAFETY_LIION", "SAFETY_LABELS"):
        assert getattr(rr, name) is getattr(safety_screen, name), name
    design = copy.deepcopy(buck_design)
    design["components"].append({"ref": "U9", "lib_id": "Battery_Management:MCP73831-2-OT",
                                 "value": "MCP73831", "footprint": "Package_TO_SOT_SMD:SOT-23-5"})
    md = rr.render_review(buck_report, [], design)
    banner = md.split("\n# Design review: ", 1)[0]
    assert "> - Li-ion charging: U9 (MCP73831)" in banner
    assert rr.safety_warnings(design)[0].startswith(f"{rr.HUMAN_REVIEW}: Li-ion charging: U9")


# --- files + CLI --------------------------------------------------------------------

def test_review_path():
    assert rr.review_path("w/netlist.json") == Path("w/netlist_review.md")
    assert rr.review_path("w/netlist.kicad_pcb") == Path("w/netlist_review.md")
    assert rr.review_path("w/netlist") == Path("w/netlist_review.md")


def test_cli_writes_the_review_beside_the_netlist(tmp_path, buck_report, buck_napkin, capsys):
    netlist = tmp_path / "netlist.json"
    netlist.write_text(json.dumps(buck_napkin), encoding="utf-8")
    report = {**buck_report, "netlist": str(netlist)}
    (tmp_path / "netlist_pipeline_report.json").write_text(json.dumps(report), encoding="utf-8")
    dec.append_decision(netlist, stage="routing", question="router?", decision="Freerouting",
                        rationale="deterministic", confidence="high", source="policy:route-auto")
    with open(dec.decisions_path(netlist), "a", encoding="utf-8") as f:
        f.write("garbage\n")
    assert rr.main([str(netlist)]) == 0
    out = (tmp_path / "netlist_review.md").read_text(encoding="utf-8")
    assert tuple(re.findall(r"^## (.+)$", out, re.M)) == rr.SECTIONS
    assert "Freerouting" in _section(out, "Decisions")
    assert ":2: not JSON" in _section(out, "Open items")
    assert str(tmp_path / "netlist_review.md") in capsys.readouterr().out


def test_cli_survives_a_torn_multibyte_decision_log(tmp_path, buck_report, buck_design):
    """A crash that split a UTF-8 character must not stop every later review."""
    netlist = tmp_path / "netlist.json"
    netlist.write_text(json.dumps(buck_design), encoding="utf-8")
    (tmp_path / "netlist_pipeline_report.json").write_text(
        json.dumps({**buck_report, "netlist": str(netlist)}), encoding="utf-8")
    dec.append_decision(netlist, stage="G3", question="via farm?", decision="36 vias under U1",
                        rationale="IPC-7093", confidence="high", source="policy:ep_vias")
    with open(dec.decisions_path(netlist), "ab") as f:
        f.write('{"rationale": "125 °C'.encode("utf-8")[:-2])     # cut inside the degree sign
    assert rr.main([str(netlist)]) == 0
    out = (tmp_path / "netlist_review.md").read_text(encoding="utf-8")
    assert "36 vias under U1" in _section(out, "Decisions")
    assert ":2: not UTF-8 text" in _section(out, "Open items")


def test_cli_survives_a_log_line_with_null_alternatives(tmp_path, buck_report, buck_design):
    """A hand edit or another writer may store "alternatives": null."""
    netlist = tmp_path / "netlist.json"
    netlist.write_text(json.dumps(buck_design), encoding="utf-8")
    (tmp_path / "netlist_pipeline_report.json").write_text(
        json.dumps({**buck_report, "netlist": str(netlist)}), encoding="utf-8")
    dec.decisions_path(netlist).write_text(
        json.dumps(_entry("G2", "high", "added C5", alternatives=None)) + "\n", encoding="ascii")
    dec.append_decision(netlist, stage="G3", question="via farm?", decision="36 vias under U1",
                        rationale="IPC-7093", confidence="high", source="policy:ep_vias")
    assert rr.main([str(netlist)]) == 0
    out = (tmp_path / "netlist_review.md").read_text(encoding="utf-8")
    assert "36 vias under U1" in _section(out, "Decisions")
    assert ":1: alternatives must be a list of strings, got null" in _section(out, "Open items")


def test_cli_without_a_pipeline_report_fails_clearly(tmp_path, buck_design, capsys):
    netlist = tmp_path / "netlist.json"
    netlist.write_text(json.dumps(buck_design), encoding="utf-8")
    assert rr.main([str(netlist)]) == 1
    assert "netlist_pipeline_report.json" in capsys.readouterr().err
    assert rr.main([]) == 2


# --- purity -----------------------------------------------------------------------

def test_module_is_pure():
    tree = ast.parse((TOOLS / "review_report.py").read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    local = {"gates", "decisions", "netlist_schema", "safety_screen"}
    assert imported - local <= set(sys.stdlib_module_names), imported


def test_render_does_not_mutate_its_inputs(buck_report, buck_napkin, buck_decisions,
                                           buck_sources):
    before = copy.deepcopy((buck_report, buck_napkin, buck_decisions, buck_sources))
    rr.render_review(buck_report, buck_decisions, buck_napkin, None, buck_sources)
    assert (buck_report, buck_napkin, buck_decisions, buck_sources) == before
