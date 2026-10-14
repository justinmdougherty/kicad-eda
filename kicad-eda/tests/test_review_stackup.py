"""review_stackup: the 'Stackup and impedance' section of the hands-off review (D7-33).

Pure unit tier, no KiCad. The full case is a six_layer_mcu-shaped run: the artifact is the
real physics_resolve output (stdlib tier, FD off) of a 6L JLC06161H-2116 MCU board with a
USB pair and a 50 ohm clock on the inner layers, shaped into the pipeline report exactly as
stages_d7 shapes it (physics_summary / rules_record), plus the G4 role-check and G5 fab
records the harness writes. The section is rendered through review_report's _Review (the
renderer hook X-03 wires) and through a plain mapping.
"""
from __future__ import annotations

import ast
import copy
import json
import re
import sys
from pathlib import Path
from typing import Any

import pytest

import physics_resolve as pr
import review_report as rr
import review_stackup as rs
import stages_d7 as s7

TOOLS = Path(__file__).resolve().parents[1] / "server" / "tools"

SIX_LAYER_MCU = {
    "name": "six_layer_mcu", "title": "Six-layer MCU board", "mode": "napkin",
    "components": [
        {"ref": "J1", "lib_id": "Connector_Generic:Conn_01x04",
         "footprint": "Connector_PinHeader_2.54mm:PinHeader_1x04_P2.54mm_Vertical"},
        {"ref": "U1", "lib_id": "MCU_ST_STM32F1:STM32F103C8Tx",
         "footprint": "Package_QFP:LQFP-48_7x7mm_P0.5mm"}],
    "nets": {"GND": [["J1", "1"]], "+3V3": [["J1", "2"]],
             "USB_DP": [["J1", "3"], ["U1", "33"]], "USB_DM": [["J1", "4"], ["U1", "32"]],
             "CLK_50R": [["U1", "5"], ["U1", "6"]]},
    "stackup": {"preset": "jlcpcb:JLC06161H-2116", "finish": "ENIG",
                "impedance_control": {"required": True, "tolerance_pct": 10},
                "rationale": "6L: two inner routing layers for the MCU fan-out",
                "board_thickness": 1.6},
    "net_specs": {"USB_DP": {"diff": 90, "diff_pair": "USB_DM", "gap": 0.15},
                  "USB_DM": {"diff": 90, "diff_pair": "USB_DP", "gap": 0.15},
                  "CLK_50R": {"impedance": 50, "layers": ["In2.Cu", "In3.Cu"]}}}

LEGACY = {"name": "rf_coupon_50r", "components": [], "mode": "napkin",
          "nets": {"RF_IN": [], "RF_OUT": [], "GND": []},
          "net_specs": {"RF_IN": {"impedance": 50}, "RF_OUT": {"impedance": 50}},
          "stackup": {"h": 0.2, "er": 4.2}}

NETLIST = "/work/six_layer_mcu/netlist.json"
PCB = "/work/six_layer_mcu/netlist.kicad_pcb"
CLI = "/usr/bin/kicad-cli"

_GATE = {"feeds": "x", "passed": True, "blockers": [], "warnings": [], "tasks": []}
GATES = [{**_GATE, "gate": g, "name": n} for g, n in (
    ("G1", "Requirements / netlist review"), ("G2", "Schematic review"),
    ("G3", "Placement review"), ("G4", "Layout / board review"), ("G5", "Fabrication package"))]

ROLES = {"judged": True, "ok": True,
         "issues": [], "warnings": [
             "R27: USB_DP via at (12.0, 8.5) changes reference GND -> +3V3 (F.Cu -> In2.Cu) "
             "with no return path within 2 mm",
             "R28: CLK_50R on In2.Cu is a dual stripline and AUX on In3.Cu runs parallel over it "
             "for 7.2 mm (over 5 mm at under 30 deg): that copper changes the line's impedance"],
         "metrics": {"plane_layers": {"plane_layers": {"In1.Cu": "GND", "In4.Cu": "+3V3"},
                                      "foreign_tracks": 0, "foreign_length_mm": 0.0,
                                      "foreign_zones": 0, "by_layer": {}},
                     "ref_transitions": {"impedance_vias": 4, "reference_changes": 2,
                                         "unsupported": 1, "unjudged_vias": 0},
                     "dual_stripline": {"dual_stripline_lines": 3, "parallel_runs": [{}]}},
         "stackup": {"source": "artifact", "artifact": "/work/six_layer_mcu/netlist.stackup.json",
                     "note": None},
         "board_copper": ["F.Cu", "In1.Cu", "In2.Cu", "In3.Cu", "In4.Cu", "B.Cu"],
         "stackup_copper": ["F.Cu", "In1.Cu", "In2.Cu", "In3.Cu", "In4.Cu", "B.Cu"],
         "pcbnew": "9.0.4", "board_sha256": "ab" * 32}

FAB_STACKUP = {"dir": "/work/six_layer_mcu/netlist_gerbers",
               "gbrjob": {"path": "/work/six_layer_mcu/netlist_gerbers/netlist-job.gbrjob",
                          "rows": 16, "expected_rows": 16, "notes": []},
               "ipc2581": {"path": "/work/six_layer_mcu/netlist_gerbers/netlist_ipc2581.xml",
                           "reasons": ["6 copper layers", "impedance control required"]},
               "csv": "/work/six_layer_mcu/netlist_gerbers/netlist_stackup.csv",
               "notes": "/work/six_layer_mcu/netlist_gerbers/netlist_fab_notes.md",
               "zip": "/work/six_layer_mcu/netlist_JLCPCB.zip", "problems": []}


def _decision(stage: str, decision: str, override: str | None, ident: str,
              confidence: str = "med") -> dict:
    return {"id": ident, "ts": "2026-10-14T10:00:00+00:00", "stage": stage,
            "question": f"question for {decision}?", "decision": decision,
            "rationale": f"because {decision}", "alternatives": [], "confidence": confidence,
            "source": "policy:test", "reversible": True, "override": override}


DECISIONS = [_decision("G1", "In1.Cu carries GND", "planes.In1.Cu", "dec-plane1"),
             _decision("G1", "In4.Cu carries +3V3", "planes.In4.Cu", "dec-plane4"),
             _decision("G3", "use width 0.255 mm, status fallback_unverified",
                       "net_specs.USB_DP.width", "dec-usb", "low")]


def _artifact(design: dict) -> dict:
    """The real stdlib-tier artifact of `design` (no FD, no files written)."""
    return pr.resolve_design(design, NETLIST, board_path=PCB, fd="off")


def _written(artifact: dict) -> dict:
    """What write_artifact returns for `artifact` (the keys physics_summary reads)."""
    return {"ok": True, "path": "/work/six_layer_mcu/netlist.stackup.json", "dru_path": PCB,
            "changed": True, "decisions_written": 2, "decisions": ["dec-plane1", "dec-plane4"],
            "fd": {"mode": "off", "backend": None, "budget_s": None, "spent_s": 0.0,
                   "reused": 0, "computed": 0},
            "previous": None, "design_digest": artifact["design_digest"],
            "data_version": artifact["data_version"], "warnings": artifact["warnings"],
            "errors": artifact["errors"]}


def _full_report(design: dict = SIX_LAYER_MCU) -> dict:
    """A six_layer_mcu-shaped pipeline report after G1..G5 with every D7 key stages_d7
    writes (physics, dru, layer_roles, dsn_policy, fab.stackup)."""
    art = _artifact(design)
    canary = {"path": "/work/six_layer_mcu/netlist.kicad_dru", "ok": True, "kicad_cli": CLI,
              "rc": 0, "crash": False, "fired": 1, "canary_added": False,
              "types": {"track_width": 1}}
    policy = s7.expected_policy(art)
    return {"netlist": NETLIST, "gates": copy.deepcopy(GATES), "PASS": True,
            "layer_policy": {"applied": False, "reason": s7.DECLARED},
            "physics": s7.physics_summary(_written(art), art),
            "dru": s7.rules_record(canary, art), "layer_roles": copy.deepcopy(ROLES),
            "dsn_policy": {**policy, "artifact": "/work/six_layer_mcu/netlist.stackup.json",
                           "dsn": {"path": PCB[:-10] + ".dsn", "found": True, "applied": True,
                                   "variant": policy["variant"], "classes": 5}},
            "fab": {"ok": True, "zip": "netlist_JLCPCB.zip", "files": 40, "bom": True,
                    "cpl": True, "stackup": copy.deepcopy(FAB_STACKUP)}}


def _review(report: dict, design: dict = SIX_LAYER_MCU, decisions: list | None = None) -> Any:
    """review_report's _Review: the object the X-03 renderer hook passes."""
    gates = rr._check_report(report)
    return rr._Review(report, gates, design, None, list(decisions or []), [], None, [],
                      str(report.get("netlist") or ""))


@pytest.fixture(scope="module")
def full_report() -> dict:
    return _full_report()


@pytest.fixture
def full_md(full_report) -> str:
    return "\n".join(rs.render(_review(full_report, decisions=DECISIONS)))


def _rows(md: str, header_start: str) -> list[list[str]]:
    """The cells of the Markdown table whose header starts with `header_start`."""
    lines = md.splitlines()
    k = next(i for i, ln in enumerate(lines) if ln.startswith(header_start))
    rows = []
    for ln in lines[k + 2:]:
        if not ln.startswith("| "):
            break
        rows.append([c.strip() for c in ln.strip("|").split(" | ")])
    return rows


def _sub(md: str, title: str) -> str:
    m = re.search(rf"^### {re.escape(title)}\n(.*?)(?=^### |\Z)", md, re.S | re.M)
    assert m, f"sub-section '{title}' missing"
    return m.group(1)


# --- the full six_layer_mcu run -----------------------------------------------------------

def test_the_full_report_is_the_real_d7_shape(full_report):
    assert full_report["physics"]["stackup"]["copper_layers"] == 6
    assert set(full_report["physics"]["lines"]) == {"USB_DP", "USB_DM", "CLK_50R"}
    assert full_report["dru"]["rules"] == 15 and full_report["dru"]["ok"]


def test_six_layers_are_listed_with_their_roles(full_md):
    body = _sub(full_md, "Stackup")
    assert "- Build: preset `jlcpcb:JLC06161H-2116` (fab jlcpcb), 6 copper layers" in body
    rows = _rows(body, "| Layer | Type |")
    copper = [r for r in rows if r[1] == "copper"]
    assert [(r[0], r[2]) for r in copper] == [
        ("F.Cu", "signal"), ("In1.Cu", "plane"), ("In2.Cu", "signal"), ("In3.Cu", "signal"),
        ("In4.Cu", "plane"), ("B.Cu", "signal")]
    assert copper[1][3] == "GND (default, dec-plane1)"           # the recorded plane decision
    assert copper[4][3] == "+3V3 (default, dec-plane4)"
    assert copper[0][4] == "0.035 (1 oz)" and copper[1][4] == "0.0152 (0.5 oz)"
    assert rows[0][0] == "F.Mask" and rows[-1][0] == "B.Mask"      # the mask coats both sides
    dielectrics = [r for r in rows if r[1] == "dielectric"]
    assert len(dielectrics) == 8                                   # JLC06161H-2116: 8 plies
    assert dielectrics[0][0] == "dielectric 1 (1/2)" and dielectrics[0][2] == "prepreg"
    assert dielectrics[0][3] == "NP-155F 2116" and dielectrics[0][4] == "0.127"
    assert dielectrics[0][5] == "4.16 (fab @ 1 GHz)" and dielectrics[0][6] == "0.015"
    assert len(rows) == 6 + 8 + 2


def test_stackup_summary_lines_name_origin_dk_basis_thickness_and_mask(full_md):
    body = _sub(full_md, "Stackup")
    assert ("- Layer-count rationale: 6L: two inner routing layers for the MCU fan-out "
            "(origin: designer)") in body
    assert ("- Board thickness: ordered 1.6 mm, computed 1.6554 mm (copper + dielectrics + "
            "2 solder masks): +0.0554 mm vs ordered") in body
    assert "- Dk basis: fab at 1 GHz (dk_policy auto; design frequency 1 GHz)" in body
    assert ("- Solder mask: fab (lpi, Er 3.8 fab, 0.0305 mm over substrate, 0.0152 mm over "
            "trace; https://jlcpcb.com/impedance)") in body
    assert "- Finish: ENIG; impedance control: required, +/-10 %" in body
    assert "- Artifact: `/work/six_layer_mcu/netlist.stackup.json`" in body
    assert re.search(r"- Resolver: \d+ warning\(s\) \(ST-W8 x1, ST-W10 x8, ST-W11 x2, "
                     r"model notes x\d+\), 0 error\(s\); FD off", body)


def test_impedance_nets_per_layer_with_status(full_md):
    body = _sub(full_md, "Impedance")
    rows = _rows(body, "| Net | Layer |")
    assert [(r[0], r[1]) for r in rows] == [
        ("CLK_50R", "In2.Cu (primary)"), ("CLK_50R", "In3.Cu"),
        ("USB_DM (pair USB_DP)", "F.Cu (primary)"), ("USB_DM (pair USB_DP)", "In2.Cu"),
        ("USB_DM (pair USB_DP)", "In3.Cu"), ("USB_DM (pair USB_DP)", "B.Cu"),
        ("USB_DP (pair USB_DM)", "F.Cu (primary)"), ("USB_DP (pair USB_DM)", "In2.Cu"),
        ("USB_DP (pair USB_DM)", "In3.Cu"), ("USB_DP (pair USB_DM)", "B.Cu")]
    clk = rows[0]
    assert clk[2] == "dual_stripline (In3.Cu as dielectric)"
    assert clk[3] == "50 ohm Z0" and clk[4] == "0.355 [0.349675, 0.360325]" and clk[5] == "-"
    assert clk[6] == "49.92 (-0.159 %)"
    assert clk[7] == "In1.Cu GND / In4.Cu +3V3"
    assert clk[8] == "C (est. 1.6 %)" and clk[9] == "**UNVERIFIED** (class C fallback)"
    usb = rows[6]
    assert usb[2] == "microstrip" and usb[3] == "90 ohm Zdiff" and usb[5] == "0.15 pair"
    assert usb[7] == "In1.Cu GND"
    assert all(r[9] == "**UNVERIFIED** (class C fallback)" for r in rows)
    assert "- Decision (low): use width 0.255 mm, status fallback_unverified" in body


def test_freerouting_layers_come_from_the_real_dsn_policy(full_report, full_md):
    """The line names what dsn_layers.narrow really decided for this artifact."""
    routed = full_report["dsn_policy"]["routed_layers"]
    excluded = full_report["dsn_policy"]["excluded_layers"]
    expected = "; ".join(f"{net} {', '.join(routed[net])}"
                         + (f" (excluded {', '.join(excluded[net])})" if excluded.get(net) else "")
                         for net in sorted(routed))
    assert f"- Freerouting layers: {expected}" in _sub(full_md, "Impedance")
    rep = {**full_report, "dsn_policy": {"routed_layers": {"USB_DP": ["F.Cu", "B.Cu"]},
                                         "excluded_layers": {"USB_DP": ["In2.Cu", "In3.Cu"]},
                                         "layer_rule_verified": False}}
    assert ("- Freerouting layers: USB_DP F.Cu, B.Cu (excluded In2.Cu, In3.Cu); other layers are "
            "excluded until per-class layer_rule is verified (D7.9)") in \
        _sub("\n".join(rs.render(_review(rep))), "Impedance")


def test_rules_role_checks_and_fab_outputs(full_md):
    rules = _sub(full_md, "Design rules")
    assert ("- Managed rules: `/work/six_layer_mcu/netlist.kicad_dru` (15 rules: R8 x2, R7 x2, "
            "R6 x10; sha256 ") in rules
    assert "compiled on `/usr/bin/kicad-cli` (canary fired: track_width x1, rc 0)" in rules
    roles = _sub(full_md, "Layer-role checks (G4)")
    assert ("- R8 foreign copper on plane layers: 0 issue(s); planes In1.Cu GND, In4.Cu +3V3; "
            "foreign tracks 0 (0 mm), foreign zones 0") in roles
    assert ("- R27 reference transitions: 1 warning(s); impedance vias 4, reference changes 2, "
            "without a return path 1") in roles
    assert "- R28 dual-stripline parallel runs: 1 warning(s); 3 line(s) checked" in roles
    assert "- Judged by pcbnew 9.0.4 on the artifact stackup; board copper = stackup copper" in \
        roles
    fab = _sub(full_md, "Fab outputs (G5)")
    assert "- Job file: `netlist-job.gbrjob`: 16 thickness rows (expected 16)" in fab
    assert "- IPC-2581: `netlist_ipc2581.xml` (6 copper layers; impedance control required)" in fab
    assert "- Stackup CSV: `netlist_stackup.csv`; fab notes: `netlist_fab_notes.md`" in fab
    assert "- Zip: `/work/six_layer_mcu/netlist_JLCPCB.zip`; problems: none" in fab


def test_open_items_list_every_unverified_row_and_nothing_else(full_report):
    items = rs.open_items(_review(full_report, decisions=DECISIONS))
    assert len(items) == 10
    assert items[0] == ("Confirm the impedance of CLK_50R on In2.Cu with the fab's field solver: "
                        "dual_stripline width 0.355 mm is a class C fallback (est. error 1.6 %), "
                        "not FD-verified (install numpy + scipy and re-run the resolver, or "
                        "accept the recorded G3 decision)")
    assert all(item.startswith("Confirm the impedance of ") for item in items)


def test_closed_form_and_fd_rows(full_report):
    rep = copy.deepcopy(full_report)
    rec = rep["physics"]["lines"]["CLK_50R"]
    rec["layers"]["In2.Cu"].update(status="closed_form", accuracy_class="B", est_error_pct=1.2)
    rec["layers"]["In3.Cu"].update(status="fd", fd={"z": 50.1, "solver": "scipy",
                                                     "verified": True, "grid": "medium"})
    md = "\n".join(rs.render(_review(rep)))
    rows = _rows(_sub(md, "Impedance"), "| Net | Layer |")
    assert rows[0][9] == "closed form (class B, not FD-verified)"
    assert rows[1][9] == "FD verified (scipy, medium grid)"
    items = rs.open_items(_review(rep))
    assert ("CLK_50R on In2.Cu: the dual_stripline width 0.355 mm is a closed-form result "
            "(class B, est. error 1.2 %) that was not FD-verified") in items
    assert not any("In3.Cu" in i and "CLK_50R" in i for i in items)


def test_a_net_without_geometry_is_a_row_and_an_open_item(full_report):
    rep = copy.deepcopy(full_report)
    rep["physics"]["lines"]["BAD"] = {
        "mode": "single", "z0": 50, "kind": "single", "z_target": 50, "line": None,
        "primary_layer": None, "layers": {}, "status": "error", "accuracy_class": None,
        "issues": [{"id": "ST-E9", "severity": "error", "message": "In1.Cu: no reference",
                    "path": "$.net_specs.BAD"}], "warnings": []}
    md = "\n".join(rs.render(_review(rep)))
    rows = _rows(_sub(md, "Impedance"), "| Net | Layer |")
    assert rows[0][:4] == ["BAD", "-", "-", "50 ohm Z0"]
    assert rows[0][9] == "no geometry (ST-E9: In1.Cu: no reference)"
    assert "No impedance geometry for BAD: ST-E9: In1.Cu: no reference" in \
        rs.open_items(_review(rep))


# --- the napkin layer default -----------------------------------------------------------

def test_the_napkin_layer_default_shows_its_decision_and_is_an_open_item():
    design = {**copy.deepcopy(SIX_LAYER_MCU), "net_specs": {}}
    design["stackup"] = {"preset": "jlcpcb:JLC04161H-7628", "origin": "layer_policy",
                         "rationale": "napkin default: 4 layers (no impedance or RF nets)"}
    rep = _full_report(design)
    rep["layer_policy"] = {"applied": True, "copper_layers": 4,
                           "preset": "jlcpcb:JLC04161H-7628",
                           "rationale": design["stackup"]["rationale"], "floors": [],
                           "decision": "dec-policy"}
    decisions = [_decision("requirements", "4 layers, preset jlcpcb:JLC04161H-7628",
                           "stackup.preset", "dec-policy")]
    md = "\n".join(rs.render(_review(rep, design, decisions)))
    body = _sub(md, "Stackup")
    assert ("- Layer-count rationale: napkin default: 4 layers (no impedance or RF nets) "
            "(origin: layer_policy, the napkin default recorded as decision dec-policy)") in body
    assert "- Build: the napkin layer-policy default `jlcpcb:JLC04161H-7628` (fab jlcpcb), " \
           "4 copper layers" in body
    assert "- No controlled-impedance nets in this design." in _sub(md, "Impedance")
    items = rs.open_items(_review(rep, design, decisions))
    assert items == ["Confirm the napkin layer-count default: 4 layers, preset "
                     "jlcpcb:JLC04161H-7628 (decision dec-policy); declare copper_layers or a "
                     "stackup with a rationale to override it"]


# --- legacy shorthand -----------------------------------------------------------------------

def test_legacy_shorthand_run():
    rep = _full_report(LEGACY)
    rep["fab"]["stackup"]["gbrjob"] = {"path": "/w/x-job.gbrjob", "rows": 9,
                                       "expected_rows": None, "notes": ["legacy: not checked"]}
    rep["fab"]["stackup"]["ipc2581"] = None
    md = "\n".join(rs.render(_review(rep, LEGACY)))
    body = _sub(md, "Stackup")
    assert ("- Build: legacy shorthand h 0.2 mm, Er 4.2, t 0.035 mm (unmasked outer microstrip, "
            "ST-W7): no KiCad stackup block is written; 4 copper layers with the default roles"
            ) in body
    rows = _rows(body, "| Layer | Type |")
    assert [(r[0], r[2], r[3]) for r in rows if r[1] == "copper"] == [
        ("F.Cu", "signal", "-"), ("In1.Cu", "plane", "GND (default)"),
        ("In2.Cu", "plane", "GND (default)"), ("B.Cu", "signal", "-")]
    assert not any(r[0].endswith("Mask") for r in rows)
    assert "- Solder mask: none (legacy shorthand: the lines are computed unmasked)" in body
    imp = _sub(md, "Impedance")
    assert [(r[0], r[1]) for r in _rows(imp, "| Net | Layer |")] == [
        ("RF_IN", "F.Cu (primary)"), ("RF_IN", "B.Cu"), ("RF_OUT", "F.Cu (primary)"),
        ("RF_OUT", "B.Cu")]
    fab = _sub(md, "Fab outputs (G5)")
    assert "- Job file: `x-job.gbrjob`: 9 thickness rows (not checked: legacy: not checked)" in fab
    assert "- IPC-2581: not needed" in fab


def test_mask_null_is_an_open_item():
    design = copy.deepcopy(SIX_LAYER_MCU)
    design["stackup"]["mask"] = None
    rep = _full_report(design)
    md = "\n".join(rs.render(_review(rep, design)))
    assert "- Solder mask: none (mask: null, ST-W9)" in _sub(md, "Stackup")
    assert ("Open the solder mask over the impedance lines listed in the Impedance table: "
            "stackup.mask is null (ST-W9), the lines were computed without mask") in \
        rs.open_items(_review(rep, design))


# --- stopped early / recorded without the stage ------------------------------------------

def test_stopped_at_g1_says_not_produced_and_shows_the_declaration():
    g1 = {**GATES[0], "passed": False, "blockers": ["ST-E7: copper_layers disagrees"]}
    rep = {"netlist": NETLIST, "gates": [g1], "PASS": False}
    md = "\n".join(rs.render(_review(rep)))
    assert md.startswith("- Not produced: the run stopped before the physical-design stage "
                         "(after G2).")
    assert ("- Declared in the netlist: stackup preset `jlcpcb:JLC06161H-2116`, impedance "
            "control required, 3 impedance net(s): CLK_50R, USB_DM, USB_DP") in md
    assert "### " not in md
    assert rs.open_items(_review(rep)) == []


def test_a_run_without_any_declaration_stopped_at_g1():
    rep = {"netlist": NETLIST, "gates": [{**GATES[0], "passed": False, "blockers": ["x"]}]}
    md = "\n".join(rs.render(_review(rep, {"name": "plain", "components": []})))
    assert "- Declared in the netlist: no stackup and no copper_layers (the ADR D7.2.4 " \
           "4-layer default applies)" in md


def test_a_pre_d7_run_that_built_a_board_says_not_recorded():
    rep = {"netlist": NETLIST, "gates": copy.deepcopy(GATES), "PASS": True,
           "board": {"ok": True}, "fab": {"ok": True, "zip": "x.zip", "files": 3}}
    md = "\n".join(rs.render(_review(rep, {"copper_layers": 4, "components": []})))
    assert md.startswith("- Not recorded: the pipeline report has no physics section (the run "
                         "predates the D7 stage or ran without the d7 feature)")
    assert "- Declared in the netlist: copper_layers 4" in md


def test_physics_present_but_later_stages_not_reached(full_report):
    rep = {k: v for k, v in full_report.items() if k not in ("layer_roles", "dsn_policy", "fab")}
    rep["gates"] = full_report["gates"][:3]
    rep["gates"][2] = {**rep["gates"][2], "passed": False, "blockers": ["courtyards overlap"]}
    md = "\n".join(rs.render(_review(rep)))
    assert "- Layer-role checks (R8 / R27 / R28): not reached (the run stopped before G4)." in \
        _sub(md, "Layer-role checks (G4)")
    assert "- Not produced: the run stopped before G5." in _sub(md, "Fab outputs (G5)")
    assert "- Freerouting layers" not in md


def test_physics_failure_record_renders_its_errors():
    rep = {"netlist": NETLIST, "gates": GATES[:2], "PASS": False,
           "physics": s7.physics_failure(ValueError("bad stackup"))}
    md = "\n".join(rs.render(_review(rep)))
    assert "- Not resolved: the physical design was not written: ValueError: bad stackup" in md
    assert "- Managed rules: not checked (the run stopped before the canary)" in md


# --- rules, roles and fab variants ------------------------------------------------------

def test_rules_that_did_not_compile_and_a_second_cli(full_report):
    rep = copy.deepcopy(full_report)
    rep["dru"].update(ok=False, fired=0, rc=0, reason=f"{CLI}: silent drop",
                      others=[{"ok": True, "kicad_cli": "/opt/k11/kicad-cli", "rc": 0,
                               "fired": 1, "types": {"track_width": 1}}])
    body = _sub("\n".join(rs.render(_review(rep))), "Design rules")
    assert "did NOT compile on `/usr/bin/kicad-cli`: /usr/bin/kicad-cli: silent drop" in body
    assert "- Also checked on `/opt/k11/kicad-cli`: compiled (canary fired: track_width x1, rc 0)" \
        in body
    crashed = copy.deepcopy(full_report)
    crashed["dru"].update(ok=False, crash=True, rc=-1073740940, reason="kicad-cli crashed")
    body = _sub("\n".join(rs.render(_review(crashed))), "Design rules")
    assert "did NOT compile on `/usr/bin/kicad-cli`: kicad-cli crashed (rc -1073740940, crashed)" \
        in body


@pytest.mark.parametrize("roles, text", [
    ({"judged": False, "reason": s7.NO_ARTIFACT}, "- Layer-role checks: not judged: no physical"),
    ({"judged": False, "skipped": s7.NOTHING_TO_CHECK},
     "- Layer-role checks: skipped: no plane-role layer and no impedance line"),
    ({"judged": False, "ok": False, "error": "pcbnew not found"},
     "- Layer-role checks: not judged: pcbnew not found"),
    ({"judged": False, "reason": "KiCad 11 board", "needs": ["impedance-controlled nets USB_DP"]},
     "- Layer-role checks: not judged: KiCad 11 board; needs: impedance-controlled nets USB_DP"),
])
def test_unjudged_role_checks_say_why(full_report, roles, text):
    rep = {**full_report, "layer_roles": roles}
    assert text in _sub("\n".join(rs.render(_review(rep))), "Layer-role checks (G4)")


def test_r8_issues_and_a_copper_mismatch(full_report):
    rep = copy.deepcopy(full_report)
    rep["layer_roles"].update(
        ok=False, issues=["R8: In1.Cu is the GND plane but 3 track segment(s) of +3V3 cross it"],
        board_copper=["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"])
    rep["layer_roles"]["metrics"]["plane_layers"].update(foreign_tracks=3,
                                                         foreign_length_mm=12.345)
    body = _sub("\n".join(rs.render(_review(rep))), "Layer-role checks (G4)")
    assert "- R8 foreign copper on plane layers: 1 issue(s); planes In1.Cu GND, In4.Cu +3V3; " \
           "foreign tracks 3 (12.345 mm), foreign zones 0" in body
    assert "  - R8: In1.Cu is the GND plane but 3 track segment(s) of +3V3 cross it" in body
    assert "board copper F.Cu, In1.Cu, In2.Cu, B.Cu differs from the stackup copper" in body


def test_fab_stackup_variants(full_report):
    rep = copy.deepcopy(full_report)
    rep["fab"]["stackup"] = {"checked": False, "problems": [s7.NO_ARTIFACT]}
    body = _sub("\n".join(rs.render(_review(rep))), "Fab outputs (G5)")
    assert "- Fab stackup outputs: not checked: no physical-design artifact" in body
    rep["fab"]["stackup"] = {**FAB_STACKUP, "zip": None, "ipc2581": None,
                             "gbrjob": {"path": None},
                             "problems": ["netlist-job.gbrjob is missing from netlist_gerbers/",
                                          "netlist_stackup.csv is not in the fab zip"]}
    body = _sub("\n".join(rs.render(_review(rep))), "Fab outputs (G5)")
    assert "- Job file: not written (see the problems below)" in body
    assert "- Zip: none; problems: 2 (G5 blockers)" in body
    assert "  - netlist-job.gbrjob is missing from netlist_gerbers/" in body
    rep["fab"] = {"ok": True, "zip": "x.zip", "files": 3}
    body = _sub("\n".join(rs.render(_review(rep))), "Fab outputs (G5)")
    assert "- Fab stackup outputs: not recorded for this run (the export predates the D7 " \
           "stage or ran without the d7 feature)" in body


# --- contracts ----------------------------------------------------------------------------

def test_render_accepts_a_plain_mapping_and_the_review_tuple(full_report):
    as_tuple = rs.render(_review(full_report, decisions=DECISIONS))
    as_dict = rs.render({"report": full_report, "design": SIX_LAYER_MCU,
                         "decisions": DECISIONS})
    assert as_tuple == as_dict
    assert rs.open_items({"report": full_report, "design": SIX_LAYER_MCU}) == \
        rs.open_items(_review(full_report))


def test_every_line_is_one_markdown_line_and_cells_are_escaped(full_report):
    rep = copy.deepcopy(full_report)
    rep["physics"]["stackup"]["rationale"] = "6L\n## injected | pipe"
    rep["physics"]["stackup"]["copper"][1]["net"] = "GND|PWR\nX"
    rep["layer_roles"]["issues"] = ["R8: multi\nline ## heading"]
    rep["fab"]["stackup"]["problems"] = ["bad\n## zip"]
    lines = rs.render(_review(rep))
    assert all("\n" not in ln and "\r" not in ln for ln in lines)
    md = "\n".join(lines)
    assert not any(ln.startswith("## ") for ln in lines)
    assert "6L ## injected | pipe" in _sub(md, "Stackup")
    row = next(r for r in _rows(_sub(md, "Stackup"), "| Layer | Type |") if r[0] == "In1.Cu")
    assert row[3] == "GND\\|PWR X (default)" and len(row) == 7
    assert "R8: multi line ## heading" in md and "bad ## zip" in md
    assert all("\n" not in i for i in rs.open_items(_review(rep)))


def test_render_never_mutates_its_inputs(full_report):
    before = copy.deepcopy((full_report, SIX_LAYER_MCU, DECISIONS))
    review = _review(full_report, decisions=DECISIONS)
    rs.render(review)
    rs.open_items(review)
    assert (full_report, SIX_LAYER_MCU, DECISIONS) == before


def test_odd_shapes_render_without_raising():
    odd = {"netlist": NETLIST, "gates": [],
           "physics": {"ok": True, "stackup": {"copper": "nope", "dielectrics": None,
                                               "mask": "x"},
                       "lines": {"N": "junk", "M": {"layers": {"F.Cu": "junk"}}},
                       "rules": "x"},
           "dru": "x", "layer_roles": "x", "fab": {"stackup": "x"}, "dsn_policy": 5}
    lines = rs.render(_review(odd))
    assert any(ln.startswith("### Stackup") for ln in lines)
    assert isinstance(rs.open_items(_review(odd)), list)
    assert rs.render({"report": {}}) and rs.open_items({}) == []


def test_module_is_pure_small_and_stdlib_only():
    text = (TOOLS / "review_stackup.py").read_text(encoding="utf-8")
    assert len(text.splitlines()) < 800
    tree = ast.parse(text)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported <= set(sys.stdlib_module_names), imported
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            assert node.end_lineno - node.lineno < 50, node.name
    assert rs.SECTION_TITLE == "Stackup and impedance"
    assert json.dumps(rs.render({"report": {"netlist": "", "gates": []}}))
