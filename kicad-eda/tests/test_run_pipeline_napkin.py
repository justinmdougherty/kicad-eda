"""Napkin mode in the harness (ROADMAP D5, F5-12): run() defaults to napkin
(--strict / napkin=False turns it off; a netlist "mode": "napkin" always means
napkin), the G1-G3 policies of gates.py / remediate.py applied with one decision
per remedy, the G3 policy rounds at the same pack_scale (connector face-out
through the connector probe, thermal footprint swap), the open-editor hard stop
(napkin: sibling outputs; strict: halt), the preservation of a board SWIG cannot
read before G2 rewrites its project, and the schematic api -> file fallback that
kicad_server records (F2-14) carried into the report.

Every KiCad-facing call is a scripted stand-in (tests/fixtures/pipeline_world.py);
World.run pins napkin=False, so napkin runs pass napkin=True.
"""
from __future__ import annotations

import copy
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

import decisions as dec
import gates
import kicad_ipc as ipc
import napkin_policy as npol
import rp_cli
import rp_napkin
import rp_route_hash
import run_pipeline as rp
import toolchain as tc
from fixtures import pipeline_world
from fixtures.pipeline_world import CLEAN_DRC, DESIGN, K9_HEADER, K11_HEADER, OK_ANALYSIS, World

world = pipeline_world.world        # the fixture (an assignment: see pipeline_world)
REAL_PROBE = rp_napkin.connector_probe   # World replaces it; the wrapper tests use the real one

MCU = {"ref": "U1", "lib_id": "MCU_Test:MCU", "value": "MCU",
       "footprint": "Package_QFP:LQFP-32_7x7mm_P0.8mm"}
USB = "Connector_USB:USB_C_Receptacle_GCT_USB4105-xx-A_16P_TopMnt_Horizontal"
QFN = "Package_DFN_QFN:QFN-16-1EP_3x3mm_P0.5mm_EP1.7x1.7mm"
INWARD = "J1: mating face points INTO the board interior (unusable in an enclosure)"
EP_SHORT = "U1: dissipating exposed-pad part has only 0 thermal via(s) under the EP (needs 5)"
FRACTURED = ("+3V3: plane on In2.Cu is fractured into separate islands, splitting the net's "
             "pads into 2 groups (largest: ['C1'], stranded: ['C2'])")


def _design(*extra: dict, board: bool = True) -> dict:
    d = copy.deepcopy(DESIGN)
    d["components"] = [*d["components"], *copy.deepcopy(list(extra))]
    if board:
        d["board"] = {"w": 30, "h": 30}
    return d


def _probe(courtyard: list[float], rot: float = 270, mouth: list[int] | None = None) -> dict:
    """A swig_connector_probe result with one J1 entry on a 30 x 30 board."""
    centre = [(courtyard[0] + courtyard[2]) / 2, (courtyard[1] + courtyard[3]) / 2]
    return {"ok": True, "kicad": "9.0.4", "board_bbox": [0, 0, 30, 30], "sheet_origin": [100, 100],
            "connectors": [{"ref": "J1", "footprint": USB, "mouth": mouth or [-1, 0],
                            "courtyard": courtyard, "courtyard_source": "courtyard",
                            "centre": centre, "origin": centre, "rot": rot, "side": "F",
                            "interior": False,
                            "pads": {"count": 16, "tht": False, "rows": 2, "pitch_mm": 0.5}}]}


J1 = {"ref": "J1", "lib_id": "Connector:USB_C_Receptacle", "value": "USB-C", "footprint": USB,
      "x": 22, "y": 13, "rot": 270}
PROBE = _probe([18, 10, 26, 16])          # mouth inward, nearest edge right -> x 26, y 13, rot 90


def _floating(pad: str, ptype: str, name: str, same: str | None = None) -> dict:
    pin = f"U1.{pad}"
    return {"decoupling": {"uncovered": []}, "errors": [],
            "flags": {"floating_pins": [pin], "floating_detail": [
                {"pin": pin, "ref": "U1", "pad": pad, "type": ptype, "name": name,
                 "same_name_net": same, "lib_id": MCU["lib_id"], "footprint": MCU["footprint"]}]}}


def _gate(rep: dict, name: str) -> dict:
    return next(g for g in rep["gates"] if g["gate"] == name)


def _log(world: World) -> list[dict]:
    return dec.read_decisions(world.netlist)


def _on_disk(world: World) -> dict:
    return json.loads(world.netlist.read_text(encoding="utf-8"))


def _comp(design: dict, ref: str) -> dict:
    return next(c for c in design["components"] if c["ref"] == ref)


def _review(world: World) -> str:
    return (world.dir / "netlist_review.md").read_text(encoding="utf-8")


def _builds(world: World) -> list[float]:
    return [args[3] for args in world.args_of("build")]


# --- the mode ------------------------------------------------------------------------

def test_run_defaults_to_napkin_and_records_why_once_at_g1(world):
    rep = rp.run(str(world.netlist), 30, 30)
    assert rep["PASS"] is True
    assert rep["mode"] == {"name": "napkin", "why": rp_napkin.WHY_DEFAULT}
    mode = [e for e in _log(world) if e["question"] == rp_napkin.MODE_QUESTION]
    assert len(mode) == 1 and mode[0]["stage"] == "G1" and mode[0]["confidence"] == "high"
    assert mode[0]["rationale"] == rp_napkin.WHY_DEFAULT
    assert any("--strict" in alt for alt in mode[0]["alternatives"])
    assert gates.NAPKIN_NO_ASSUMPTIONS in _gate(rep, "G1")["warnings"]


def test_strict_records_no_mode_decision_and_no_napkin_warning(world):
    rep = world.run()
    assert rep["mode"] == {"name": "strict", "why": rp_napkin.WHY_STRICT}
    assert [e for e in _log(world) if e["stage"] == "G1"] == []
    assert gates.NAPKIN_NO_ASSUMPTIONS not in _gate(rep, "G1")["warnings"]


def test_a_napkin_netlist_is_napkin_even_when_strict_was_asked(world):
    world.write_design({**DESIGN, "mode": "napkin"})
    rep = world.run()                                   # napkin=False
    assert rep["mode"] == {"name": "napkin", "why": rp_napkin.WHY_NETLIST}
    mode = next(e for e in _log(world) if e["question"] == rp_napkin.MODE_QUESTION)
    assert mode["override"] == "mode" and mode["rationale"] == rp_napkin.WHY_NETLIST


def _record_napkin_flags(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, bool]]:
    seen: list[tuple[str, bool]] = []
    for name in ("g1_requirements", "g2_schematic", "g3_placement"):
        real = getattr(gates, name)

        def wrapper(*args: Any, _real=real, _name=name, **kw: Any) -> dict:
            seen.append((_name, kw.get("napkin", False)))
            return _real(*args, **kw)
        monkeypatch.setattr(gates, name, wrapper)
    return seen


@pytest.mark.parametrize("scenario", ["pass", "g3-escalation", "manual-g3", "g2-designer"])
def test_strict_gives_every_gate_napkin_false_and_runs_no_policy(world, monkeypatch, scenario):
    """Strict = today's harness: every gate judged with napkin=False (gates.py's
    strict verdicts) and no napkin policy code reached."""
    seen = _record_napkin_flags(monkeypatch)
    for name in ("apply_policies", "g3_policy_round", "decide_mode", "sibling_outputs"):
        monkeypatch.setattr(rp_napkin, name, lambda *a, _n=name, **k: pytest.fail(f"{_n} ran"))
    if scenario == "g3-escalation":
        world.drcs = [{**CLEAN_DRC, "types": {"courtyards_overlap": 2}}, CLEAN_DRC]
    elif scenario == "manual-g3":
        world.gate_checks["placement"] = [{"issues": [INWARD], "warnings": []}]
    elif scenario == "g2-designer":
        world.analyses = [_floating("5", "output", "PA3")]
    world.run()
    assert seen and all(flag is False for _, flag in seen)
    assert "probe" not in world.names()


def test_napkin_gives_every_gate_napkin_true(world, monkeypatch):
    seen = _record_napkin_flags(monkeypatch)
    world.run(napkin=True)
    assert {name for name, _ in seen} == {"g1_requirements", "g2_schematic", "g3_placement"}
    assert all(flag is True for _, flag in seen)


# --- G2: floating pins -----------------------------------------------------------------

def test_napkin_g2_declares_a_floating_output_no_connect_with_its_own_decision(world):
    world.write_design(_design(MCU))
    world.analyses = [_floating("5", "output", "PA3"), OK_ANALYSIS]
    rep = world.run(napkin=True)
    assert _gate(rep, "G2")["passed"] is True
    assert _on_disk(world)["no_connect"] == [["U1", "5"]]
    text = "declared U1.5 no_connect (output pin 'PA3')"
    assert rep["remediation"] == [{"gate": "G2", "round": 1, "applied": [text]}]
    g2 = [e for e in _log(world) if e["stage"] == "G2"]
    assert len(g2) == 1                                   # one per remedy, no duplicate
    confidence, rationale = npol.no_connect_verdict("output", "PA3")
    assert (g2[0]["decision"], g2[0]["confidence"], g2[0]["rationale"]) == (
        text, confidence, rationale)
    assert g2[0]["override"] == "no_connect" and len(g2[0]["alternatives"]) == 2
    assert npol.POLICY_FLOATING_PIN in g2[0]["source"]
    assert world.names().count("schematic") == 2          # rebuilt from the rewritten netlist


def test_napkin_g2_joins_a_stacked_supply_pin_to_its_net(world):
    world.analyses = [_floating("7", "passive", "GND", same="GND"), OK_ANALYSIS]
    world.write_design({**_design(MCU), "nets": {**DESIGN["nets"], "GND": [["U1", "4"]]}})
    rep = world.run(napkin=True)
    assert rep["PASS"] is True
    assert _on_disk(world)["nets"]["GND"] == [["U1", "4"], ["U1", "7"]]
    g2 = next(e for e in _log(world) if e["stage"] == "G2")
    assert g2["confidence"] == "high" and g2["override"] == "nets.GND"


def test_strict_g2_leaves_a_floating_output_to_the_designer(world):
    world.write_design(_design(MCU))
    world.analyses = [_floating("5", "output", "PA3")]
    rep = world.run()
    g2 = _gate(rep, "G2")
    assert g2["passed"] is False and [t["auto"] for t in g2["tasks"]] == [False]
    assert "remediation" not in rep and "no_connect" not in _on_disk(world)


def test_napkin_g2_floating_input_stays_a_blocker(world):
    world.write_design(_design(MCU))
    world.analyses = [_floating("5", "input", "PA3")]
    rep = world.run(napkin=True)
    assert _gate(rep, "G2")["passed"] is False and "remediation" not in rep
    assert [e for e in _log(world) if e["stage"] == "G2"] == []
    assert world.names().count("schematic") == 1


def test_napkin_g2_task_an_applier_declines_is_an_open_item(world):
    netted = {**DESIGN["nets"], "N1": [*DESIGN["nets"]["N1"], ["U1", "1"]]}
    world.write_design({**_design(MCU), "nets": netted})
    world.analyses = [_floating("1", "output", "PA0")]      # a stale review: U1.1 is on N1
    rep = world.run(napkin=True)
    assert _gate(rep, "G2")["passed"] is False
    [entry] = rep["remediation"]
    assert entry["gate"] == "G2" and len(entry["applied"]) == 1
    assert entry["applied"][0].startswith("OPEN: declare U1.1 no_connect")
    assert "U1.1 is netted to N1" in entry["applied"][0]
    assert [e for e in _log(world) if e["stage"] == "G2"] == []
    assert "Unfixed G2 issue: declare U1.1 no_connect" in _review(world)


def test_napkin_g2_bypass_cap_remedy_carries_its_rationale(world):
    world.write_design(_design(MCU))
    world.analyses = [{"decoupling": {"uncovered": [{"ref": "U1", "rails_without_cap": ["N1"]}]},
                       "flags": {}, "errors": []}, OK_ANALYSIS]
    rep = world.run(napkin=True)
    assert rep["PASS"] is True
    g2 = next(e for e in _log(world) if e["stage"] == "G2")
    assert g2["decision"].startswith("added C1 (100nF 0603) bypass: N1 -> GND")
    assert "every IC supply rail needs a local bypass capacitor" in g2["rationale"]
    assert g2["source"] == "run_pipeline G2 napkin policy G2.add_decoupling"


# --- G2: the schematic api -> file fallback (kicad_server records it, F2-14) -------------

FALLBACK = {"from": "api", "to": "file",
            "reason": "SchematicApiError: KiCad 11 headless finds no stock libraries"}
LIBRARIES = {"symbols": {"Device": "stock"}, "footprints": {"Resistor_SMD": "stock"}}


@pytest.mark.parametrize("napkin", [True, False], ids=["napkin", "strict"])
def test_the_schematic_fallback_and_libraries_reach_the_report_and_review(world, monkeypatch,
                                                                          napkin):
    def build(netlist: str, sch: str) -> dict:          # kicad_server records the decision
        world.calls.append(("schematic", (netlist, sch)))
        dec.append_decision(netlist, stage="G2", question="Which backend builds netlist.kicad_sch?",
                            decision="the file backend", rationale="ROADMAP D5 fallback",
                            alternatives=["copy the template tables"], confidence="high",
                            source="auto")
        return {"ok": True, "backend": "file", "components": 2, "nets": 2, "erc_ok": True,
                "erc_violations": 0, "fallback": FALLBACK, "libraries": LIBRARIES}
    monkeypatch.setattr(rp.k, "build_schematic_from_netlist", build)
    rep = world.run(napkin=napkin)
    assert rep["PASS"] is True
    assert rep["schematic"]["fallback"] == FALLBACK and rep["schematic"]["libraries"] == LIBRARIES
    assert world.names().count("schematic") == 1          # the harness never retries itself
    assert "### G2" in _review(world) and "the file backend" in _review(world)


def test_another_schematic_failure_is_not_retried(world):
    world.schematics = [{"ok": False, "backend": "api", "reason": "SchematicApiError: boom"}]
    rep = world.run(napkin=True)
    assert _gate(rep, "G2")["blockers"] == ["schematic generation failed"]
    assert rep["schematic"]["fallback"] is None
    assert world.names().count("schematic") == 1


# --- G3: policy rounds at the same pack_scale ------------------------------------------

def test_napkin_g3_turns_an_inward_connector_out_and_rebuilds_at_the_same_scale(world):
    world.write_design(_design(J1))
    world.gate_checks["placement"] = [{"issues": [INWARD], "warnings": []}, {"issues": [],
                                                                              "warnings": []}]
    world.probes = [PROBE]
    rep = world.run(napkin=True)
    assert rep["PASS"] is True and _gate(rep, "G3")["blockers"] == []
    assert _builds(world) == [1.0, 1.0]                   # the same pack_scale, rebuilt once
    assert world.names().count("probe") == 1
    j1 = _comp(_on_disk(world), "J1")
    assert (j1["x"], j1["y"], j1["rot"]) == (26.0, 13.0, 90)
    text = "J1: turned to face out of the right board edge (x=26.0 y=13.0 rot=90)"
    assert rep["remediation"][0] == {"gate": "G3", "round": 1, "applied": [text]}
    g3 = next(e for e in _log(world) if e["stage"] == "G3")
    assert g3["decision"] == text and g3["confidence"] == "med"
    assert len(g3["alternatives"]) == 2 and g3["override"] == "components[J1].x/y/rot"
    manifest = json.loads((world.dir / "netlist.cache.json").read_text(encoding="utf-8"))
    assert manifest["route_hash"] == rp_route_hash.route_hash(_on_disk(world), world.dir)
    assert "### G3" in _review(world)


def test_the_g3_hooks_judge_every_policy_rebuild(world):
    feature = pipeline_world.recording_feature()
    world.use_features(feature)
    world.write_design(_design(J1))
    world.gate_checks["placement"] = [{"issues": [INWARD], "warnings": []},
                                      {"issues": [], "warnings": []}]
    world.probes = [PROBE]
    world.run(napkin=True)
    hooks = [h for h, _ in feature.seen]
    assert hooks.count("g3_inputs") == 2 and hooks.count("g3_verdict") == 2


def test_g3_policy_rounds_stop_at_the_cap_per_scale(world):
    world.write_design(_design(J1))
    world.gate_checks["placement"] = [{"issues": [INWARD], "warnings": []}]
    world.probes = [PROBE, _probe([24, 10, 30, 16]), _probe([20, 10, 28, 16])]
    rep = world.run(napkin=True)
    assert _builds(world) == [1.0] * (rp_napkin.G3_POLICY_ROUNDS + 1)
    assert world.names().count("probe") == rp_napkin.G3_POLICY_ROUNDS
    assert rep["PASS"] is False and INWARD in _gate(rep, "G3")["blockers"]
    assert "route" not in world.names()                   # a face-out task is never a scale fix


def test_a_policy_round_that_changes_nothing_rebuilds_nothing(world):
    world.write_design(_design({**J1, "x": 26, "y": 13, "rot": 90}))
    world.gate_checks["placement"] = [{"issues": [INWARD], "warnings": []}]
    world.probes = [PROBE]
    rep = world.run(napkin=True)
    assert _builds(world) == [1.0]
    [entry] = rep["remediation"]
    assert entry["applied"][0].startswith("OPEN: turn J1 to face out")
    assert "changed nothing" in entry["applied"][0]
    assert [e for e in _log(world) if e["stage"] == "G3"] == []


def test_a_failed_probe_leaves_the_connector_task_open(world):
    world.write_design(_design(J1))
    world.gate_checks["placement"] = [{"issues": [INWARD], "warnings": []}]
    world.probes = [{"ok": False, "reason": "board file not found"}]
    rep = world.run(napkin=True)
    assert _builds(world) == [1.0] and rep["PASS"] is False
    assert "connector probe failed: board file not found" in rep["remediation"][0]["applied"][0]


def test_strict_g3_inward_connector_stops_without_a_probe(world):
    world.write_design(_design(J1))
    world.gate_checks["placement"] = [{"issues": [INWARD], "warnings": []}]
    rep = world.run()
    assert _builds(world) == [1.0] and "probe" not in world.names()
    assert [t["auto"] for t in _gate(rep, "G3")["tasks"]] == [False]


def _thermal_libs(world: World, monkeypatch: pytest.MonkeyPatch, *, variant: bool) -> None:
    pretty = world.dir / "fps" / "Package_DFN_QFN.pretty"
    pretty.mkdir(parents=True)
    names = [QFN.split(":")[1]] + ([QFN.split(":")[1] + "_ThermalVias"] if variant else [])
    for name in names:
        (pretty / f"{name}.kicad_mod").write_text(f'(footprint "{name}")\n', encoding="utf-8")
    monkeypatch.setattr(tc, "footprints_dir", lambda explicit="": str(world.dir / "fps"))
    monkeypatch.setattr(tc, "symbols_dir", lambda explicit="": None)


def test_napkin_g3_swaps_to_the_thermal_vias_footprint(world, monkeypatch):
    _thermal_libs(world, monkeypatch, variant=True)
    world.write_design(_design({**MCU, "footprint": QFN}))
    world.gate_checks["placement"] = [{"issues": [EP_SHORT], "warnings": []},
                                      {"issues": [], "warnings": []}]
    rep = world.run(napkin=True)
    assert rep["PASS"] is True and _builds(world) == [1.0, 1.0]
    assert "probe" not in world.names()                   # no connector task: no probe
    assert _comp(_on_disk(world), "U1")["footprint"] == QFN + "_ThermalVias"
    g3 = next(e for e in _log(world) if e["stage"] == "G3")
    assert g3["confidence"] == "high" and g3["override"] == "components[U1].footprint"


def test_a_missing_thermal_variant_is_an_open_item_not_a_scale_change(world, monkeypatch):
    _thermal_libs(world, monkeypatch, variant=False)
    world.write_design(_design({**MCU, "footprint": QFN}))
    world.gate_checks["placement"] = [{"issues": [EP_SHORT], "warnings": []}]
    rep = world.run(napkin=True)
    assert _builds(world) == [1.0] and rep["PASS"] is False
    assert "not found" in rep["remediation"][0]["applied"][0]


@pytest.mark.parametrize("napkin, builds", [(True, [1.0, 1.5]), (False, [1.0])],
                         ids=["napkin", "strict"])
def test_a_fractured_plane_escalates_pack_scale_only_in_napkin(world, napkin, builds):
    world.gate_checks["placement"] = [{"issues": [FRACTURED], "warnings": []},
                                      {"issues": [], "warnings": []}]
    rep = world.run(napkin=napkin)
    assert _builds(world) == builds
    assert rep["PASS"] is napkin
    if napkin:
        assert rep["remediation"] == [{"gate": "G3", "round": 1,
                                       "applied": ["re-placed at pack_scale 1.5"]}]


# --- the connector probe and the footprint lookup ---------------------------------------

def _capture(monkeypatch: pytest.MonkeyPatch, *, stdout: str = "", exc: Exception | None = None,
             kpy: str | None = "K:/python.exe") -> list[tuple[list, dict]]:
    calls: list[tuple[list, dict]] = []

    def fake(args: list[str], **kw: Any) -> subprocess.CompletedProcess:
        calls.append((list(args), kw))
        if exc is not None:
            raise exc
        return subprocess.CompletedProcess(args, 0, stdout, "Traceback: boom")
    monkeypatch.setattr(tc, "run", fake)
    monkeypatch.setattr(rp.k, "_kicad_python", lambda: kpy)
    return calls


def test_the_probe_runs_kicads_python_with_stdin_closed_and_a_timeout(monkeypatch):
    calls = _capture(monkeypatch, stdout='noise\n{"ok": true, "connectors": []}\n')
    assert REAL_PROBE("b.kicad_pcb", "n.json") == {"ok": True, "connectors": []}
    [(argv, kw)] = calls
    assert argv[0] == "K:/python.exe" and argv[1].endswith("swig_connector_probe.py")
    assert argv[2:] == ["b.kicad_pcb", "n.json"]
    assert kw["stdin"] is subprocess.DEVNULL and kw["timeout"] == rp_napkin.PROBE_TIMEOUT_S == 300


@pytest.mark.parametrize("kw, reason", [
    ({"stdout": "no json here"}, "produced no result"),
    ({"exc": subprocess.TimeoutExpired("python", 300)}, "TimeoutExpired"),
    ({"kpy": None}, "unavailable"),
], ids=["no-result", "timeout", "no-kicad-python"])
def test_a_probe_that_cannot_answer_is_a_reason_not_a_crash(monkeypatch, kw, reason):
    _capture(monkeypatch, **kw)
    res = REAL_PROBE("b.kicad_pcb", "n.json")
    assert res["ok"] is False and reason in res["reason"]


def test_the_footprint_lookup_follows_the_d1_resolution_order(tmp_path, monkeypatch):
    stock = tmp_path / "stock" / "Package_SO.pretty"
    stock.mkdir(parents=True)
    (stock / "SOIC-8_ThermalVias.kicad_mod").write_text("(footprint)\n", encoding="utf-8")
    own = tmp_path / "libs" / "mine.pretty"
    own.mkdir(parents=True)
    (own / "X_ThermalVias.kicad_mod").write_text("(footprint)\n", encoding="utf-8")
    monkeypatch.setattr(tc, "footprints_dir", lambda explicit="": str(tmp_path / "stock"))
    monkeypatch.setattr(tc, "symbols_dir", lambda explicit="": None)
    design = {"libraries": {"footprints": {"mine": "libs/mine.pretty"}}}
    exists = rp_napkin.footprint_lookup(tmp_path, design)
    assert exists("Package_SO:SOIC-8_ThermalVias") and exists("mine:X_ThermalVias")
    assert not exists("Package_SO:SOIC-8_ThermalVias2") and not exists("Nope:X")
    assert not exists("no-colon")


# --- the hard stop: a file open in a KiCad editor --------------------------------------

def _open(world: World, *paths: Path, kind: str = "pcb") -> None:
    world.open_documents = [{"type": kind, "path": str(p)} for p in paths]


def test_napkin_writes_sibling_outputs_when_the_board_is_open(world):
    world.pcb.write_text(K9_HEADER + "(the user's open board)\n", encoding="utf-8")
    _open(world, world.pcb)
    rep = world.run(napkin=True)
    sch, pcb = world.dir / "netlist.napkin.kicad_sch", world.dir / "netlist.napkin.kicad_pcb"
    assert rep["PASS"] is True
    assert world.pcb.read_text(encoding="utf-8") == K9_HEADER + "(the user's open board)\n"
    assert world.args_of("schematic") == [(str(world.netlist), str(sch))]
    assert pcb.read_text(encoding="utf-8").startswith(K9_HEADER + "(built 1.0)")
    assert (world.dir / "netlist.napkin.cache.json").is_file()
    assert not (world.dir / "netlist.cache.json").exists()
    assert rep["pipeline_report"] == str(world.dir / "netlist_pipeline_report.json")
    assert rep["review"] == str(world.dir / "netlist_review.md")
    assert rep["board_file"]["path"] == str(pcb)
    assert rep["hard_stop"]["action"] == "sibling" and rep["hard_stop"]["board"] == str(pcb)
    stop = next(e for e in _log(world) if e["source"] == "D5 hard stop")
    assert stop["stage"] == "G1" and stop["confidence"] == "high"
    assert ipc.write_refusal(str(world.pcb), "pcb")["reason"] in stop["rationale"]


def test_a_second_napkin_run_reuses_the_sibling_cache(world):
    _open(world, world.pcb)
    world.run(napkin=True)
    world.calls.clear()
    second = world.run(napkin=True)
    assert second["route_cache"] == "hit" and "build" not in world.names()


def test_strict_halts_at_g1_with_the_refusal_reason(world):
    _open(world, world.dir / "netlist.kicad_sch", kind="schematic")
    rep = world.run()
    reason = ipc.write_refusal(str(world.dir / "netlist.kicad_sch"), "schematic")["reason"]
    assert rep["PASS"] is False
    assert [(g["gate"], g["passed"]) for g in rep["gates"]] == [("G1", False)]
    assert any(reason in b for b in rep["gates"][0]["blockers"])
    assert world.names() == [] and _log(world) == []
    assert rep["hard_stop"]["action"] == "halt"


def test_napkin_halts_when_the_sibling_is_open_too(world):
    _open(world, world.pcb, world.dir / "netlist.napkin.kicad_pcb")
    rep = world.run(napkin=True)
    assert [(g["gate"], g["passed"]) for g in rep["gates"]] == [("G1", False)]
    assert "netlist.napkin.kicad_pcb" in rep["gates"][0]["blockers"][0]
    assert world.names() == []


def test_no_kicad_gui_means_no_hard_stop(world):
    rep = world.run(napkin=True)
    assert rep["PASS"] is True and "hard_stop" not in rep and world.connects >= 1


# --- preservation of a board SWIG cannot read, at load -----------------------------------

def _k11_board(world: World) -> dict[str, bytes]:
    files = {".kicad_pcb": (K11_HEADER + "(routed by the agent)\n").encode(),
             ".kicad_pro": b'{"agent": true}', ".kicad_prl": b'{"ui": 1}'}
    for ext, data in files.items():
        (world.dir / f"netlist{ext}").write_bytes(data)
    return files


@pytest.mark.parametrize("napkin", [True, False], ids=["napkin", "strict"])
def test_an_unreadable_board_is_kept_byte_for_byte_before_g2(world, monkeypatch, napkin):
    files = _k11_board(world)
    seen: dict[str, bytes] = {}
    real = world.fake_schematic

    def schematic(netlist: str, sch: str) -> dict:     # G2 re-saves the project
        seen.update({ext: (world.dir / f"netlist.agent{ext}").read_bytes() for ext in files})
        (world.dir / "netlist.kicad_pro").write_text('{"rewritten": true}', encoding="utf-8")
        return real(netlist, sch)
    monkeypatch.setattr(rp.k, "build_schematic_from_netlist", schematic)
    rep = world.run(napkin=napkin)
    assert seen == files
    assert rep["agent_board_preserved"] == str(world.dir / "netlist.agent.kicad_pcb")
    assert rep["PASS"] is True and world.pcb.read_text(encoding="utf-8").startswith(K9_HEADER)


def test_a_failed_preservation_stops_the_run_before_anything_is_built(world, monkeypatch):
    _k11_board(world)

    def fail(pcb: str, move: bool) -> dict:
        raise OSError("disk full")
    monkeypatch.setattr(tc, "keep_agent_copy", fail)
    with pytest.raises(RuntimeError, match="could not be preserved"):
        world.run(napkin=True)
    saved = json.loads((world.dir / "netlist_pipeline_report.json").read_text(encoding="utf-8"))
    assert saved["PASS"] is False and saved["error"].startswith("RuntimeError: ")
    assert "disk full" in saved["error"]
    assert world.names() == []                             # no lint, no schematic, no build
    assert (world.dir / "netlist_review.md").is_file()
    assert world.pcb.read_text(encoding="utf-8").startswith(K11_HEADER)


def test_a_board_already_kept_is_not_copied_again(world):
    files = _k11_board(world)
    for ext, data in files.items():
        (world.dir / f"netlist.agent{ext}").write_bytes(data)
    world.lint = [{"ok": False, "errors": ["E3"], "warnings": []}]   # stops before the build
    rep = world.run(napkin=True)
    assert rep["agent_board_preserved"] == str(world.dir / "netlist.agent.kicad_pcb")
    assert not (world.dir / "netlist.agent.1.kicad_pcb").exists()


def test_an_older_different_agent_copy_is_rotated_not_lost(world):
    _k11_board(world)
    (world.dir / "netlist.agent.kicad_pcb").write_text(K11_HEADER + "(older)\n", encoding="utf-8")
    rep = world.run(napkin=True)
    older = world.dir / "netlist.agent.1.kicad_pcb"
    assert older.read_text(encoding="utf-8") == K11_HEADER + "(older)\n"
    assert rep["agent_board_previous"] == str(older)


# --- the command line, main() and run_all ------------------------------------------------

@pytest.mark.parametrize("argv, expected", [
    (["n.json"], rp_cli.CliArgs("n.json", None, None, "", True)),
    (["n.json", "--strict"], rp_cli.CliArgs("n.json", None, None, "", False)),
    (["--strict", "n.json", "30", "40", "p.json"], rp_cli.CliArgs("n.json", 30.0, 40.0, "p.json",
                                                                  False)),
    (["n.json", "30", "--strict", "40"], rp_cli.CliArgs("n.json", 30.0, 40.0, "", False)),
])
def test_parse_cli_strict_flag(argv, expected):
    assert rp_cli.parse_cli(argv) == expected


def test_parse_cli_rejects_an_unknown_option():
    with pytest.raises(rp_cli.UsageError, match="unknown option --fast"):
        rp_cli.parse_cli(["n.json", "--fast"])


@pytest.mark.parametrize("extra, kw", [([], {}), (["--strict"], {"napkin": False})],
                         ids=["napkin-default", "strict"])
def test_main_passes_napkin_false_only_for_strict(tmp_path, monkeypatch, extra, kw):
    calls = []
    monkeypatch.setattr(rp, "run", lambda *a, **k: calls.append((a, k)) or {
        "PASS": True, "gates": [], "gate_summary": ""})
    netlist = str(tmp_path / "n.json")
    assert rp.main([netlist, "30", "30", *extra]) == 0
    assert calls == [((netlist, 30.0, 30.0, ""), kw)]
    assert "--strict" in rp_cli.USAGE


def test_run_all_keeps_the_g3_fixture_strict(tmp_path, monkeypatch):
    import run_all
    src = Path(rp.__file__).resolve().parents[1] / "examples" / "cp2102n_uart" / "netlist.json"
    (tmp_path / "cp2102n_uart").mkdir()
    (tmp_path / "cp2102n_uart" / "netlist.json").write_bytes(src.read_bytes())
    monkeypatch.setattr(run_all, "HERE", tmp_path)
    seen = []
    monkeypatch.setattr(rp, "run", lambda netlist, w, h, **kw: seen.append((w, h, kw)) or {
        "gates": []})
    run_all._fixture_g3()
    assert seen == [(56, 40, {"napkin": False})]


def test_world_run_pins_strict():
    assert pipeline_world.RUN_DEFAULTS["napkin"] is False
