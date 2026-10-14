"""E2E 2a (D7-29a): the three stackup examples (stackup_2l_mixed, six_layer_mcu,
stackup_hybrid_4l), their run_all.json markers and pipeline/e2e_stackup_check.py, the
file-parsing checker a marker runs as a post_check.

Tiers:
  * contract (always): the example netlists carry the ADR D7.2.2 stackups and the D7-29a
    net_specs, validate, join pipeline/run_all.py through their markers (w/h, the post_check)
    and show up in the dry listing;
  * checker (always, synthetic inputs): every pure check of the module doc, PASS and FAIL
    both ways, and the CLI's exit codes;
  * live (KiCad 9/10 + Java + Freerouting; skipped without them): each example through
    run_pipeline on a fresh copy, then the checker, which must pass.
"""
from __future__ import annotations

import copy
import io
import json
import os
import shutil
import zipfile
from pathlib import Path
from typing import Any, Iterator

import pytest

import decisions
import e2e_stackup_check as ck
import fab_stackup as fs
import netlist_schema
import run_all
import stackup_kicad as sk
import stackup_model as sm
import toolchain as tc
from fixtures import pipeline_world

world = pipeline_world.world            # the fixture (an assignment: see pipeline_world)

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"
STACKUPS = ROOT / "tests" / "fixtures" / "stackups"
NAMES = ("stackup_2l_mixed", "six_layer_mcu", "stackup_hybrid_4l")
DIMS = {"stackup_2l_mixed": (40, 25), "six_layer_mcu": (50, 50), "stackup_hybrid_4l": (35, 30)}
BASES = {"stackup_2l_mixed": "rf_coupon_50r", "six_layer_mcu": "stm32f103_dev",
         "stackup_hybrid_4l": "rf_lna_gainblock"}
FIXTURE_OF = {"stackup_2l_mixed": "d722_2l_mixed", "six_layer_mcu": "d722_6l_2116",
              "stackup_hybrid_4l": "d722_4l_hybrid"}
CPWG = {"impedance": 50, "line": "cpwg", "ground_gap": 0.2, "layers": ["F.Cu"]}
MS = {"impedance": 50, "line": "microstrip", "layers": ["F.Cu"]}
C0603 = "Capacitor_SMD:C_0603_1608Metric"


def _example(name: str) -> dict:
    return json.loads((EXAMPLES / name / "netlist.json").read_text(encoding="utf-8"))


def _fixture_stackup(name: str) -> dict:
    return json.loads((STACKUPS / f"{name}.json").read_text(encoding="utf-8"))["design"]["stackup"]


# --- contract: the examples -------------------------------------------------------------------
@pytest.mark.parametrize("name", NAMES)
def test_example_validates_and_names_itself(name: str) -> None:
    design = _example(name)
    assert design["name"] == name
    assert netlist_schema.validate(design) == []
    assert design["board"] == {"w": DIMS[name][0], "h": DIMS[name][1]}
    assert design["copper_layers"] == ck.EXPECTATIONS[name].copper


@pytest.mark.parametrize("name", NAMES)
def test_example_records_its_assumptions(name: str) -> None:
    design = _example(name)
    assert decisions.validate_assumptions(design["assumptions"]) == []
    by_id = {a["id"]: a for a in design["assumptions"]}
    assert by_id["board.copper_layers"]["value"] == ck.EXPECTATIONS[name].copper
    assert by_id["board.copper_layers"]["override"] == "copper_layers"
    assert "h" not in design["stackup"], "the legacy shorthand is replaced by the model"


def test_2l_mixed_is_the_d722_2l_example_with_cpwg_feeds() -> None:
    """rf_coupon_50r on the D7.2.2 2L build: the recorded deviation is the 0603 DC
    blocks (a 0.87 mm line cannot end on 0402 pads 0.96 mm apart)."""
    design, base = _example("stackup_2l_mixed"), _example("rf_coupon_50r")
    assert design["stackup"] == _fixture_stackup("d722_2l_mixed")
    assert design["net_specs"] == {n: CPWG for n in ("RF_IN", "RF_MID", "RF_OUT")}
    assert design["nets"] == base["nets"] and design["no_connect"] == base["no_connect"]
    assert design["components"] == [
        {**c, "footprint": C0603 if c["ref"] in ("C1", "C2") else c["footprint"]}
        for c in base["components"]]
    assert "parts.dc_block_size" in {a["id"] for a in design["assumptions"]}


def test_six_layer_mcu_is_the_d722_6l_preset_with_usb_and_a_dual_stripline_gpio() -> None:
    """stm32f103_dev on the JLC06161H-2116 preset: the recorded deviation is the USB-C
    shield as a no_connect by pin NAME (KiCad 9 keys the legs S1, 10 and 11 SH)."""
    design, base = _example("six_layer_mcu"), _example("stm32f103_dev")
    assert design["stackup"] == _fixture_stackup("d722_6l_2116")
    assert design["components"] == base["components"]
    assert design["nets"] == {n: [p for p in pins if p != ["J1", "SH"]]
                              for n, pins in base["nets"].items()}
    assert design["no_connect"] == [*base["no_connect"], ["J1", "SHIELD"]]
    specs = design["net_specs"]
    assert specs["USB_DP"] == {"diff": 90, "diff_pair": "USB_DM", "layers": ["F.Cu"]}
    first_gpio = next(n for n, pins in base["nets"].items()
                      if {r for r, _ in pins} == {"U1", "J3"})
    assert first_gpio == "PA0"
    assert specs["PA0"] == {"impedance": 50, "layers": ["F.Cu", "In2.Cu"]}
    assert {k: v for k, v in specs.items() if k not in ("USB_DP", "PA0")} == base["net_specs"]
    assert "parts.usb_shield" in {a["id"] for a in design["assumptions"]}


def test_hybrid_4l_is_the_d722_hybrid_with_v5_on_in2() -> None:
    design, base = _example("stackup_hybrid_4l"), _example("rf_lna_gainblock")
    want = copy.deepcopy(_fixture_stackup("d722_4l_hybrid"))
    want["layers"][4] = {**want["layers"][4], "net": "V5"}
    assert design["stackup"] == want
    assert design["components"] == base["components"] and design["nets"] == base["nets"]
    assert design["no_connect"] == base["no_connect"]
    assert design["power_flags"] == base["power_flags"]
    specs = design["net_specs"]
    assert specs["RF_IN"] == specs["AMP_IN"] == MS
    assert specs["AMP_OUT"] == specs["RF_OUT"] == CPWG
    assert specs["V5"] == {"voltage": 5.0, "current": 0.035}


@pytest.mark.parametrize("name", NAMES)
def test_example_stackup_resolves_to_the_expected_build(name: str) -> None:
    rs, issues = sm.resolve(_example(name))
    exp = ck.EXPECTATIONS[name]
    assert rs is not None and rs.kicad_copper_count == exp.copper
    assert rs.board_thickness_computed == pytest.approx(exp.thickness, abs=1e-6)
    assert len(fs.expected_thickness_rows(rs.to_dict())) == exp.rows
    assert fs.ipc2581_needed(rs.to_dict()) is exp.ipc2581
    assert not [i for i in issues if i.severity == "error"]


# --- contract: the markers and the dry listing ------------------------------------------------
@pytest.mark.parametrize("name", NAMES)
def test_marker_has_the_stated_dims_and_the_checker_post_check(name: str) -> None:
    entry = run_all.read_entry(EXAMPLES / name / run_all.MARKER)
    assert entry.problems == () and entry.marker is not None
    assert (entry.marker.w, entry.marker.h) == DIMS[name]
    assert entry.marker.expect == "PASS"
    assert entry.marker.post_checks == (
        run_all.PostCheck("pipeline/e2e_stackup_check.py", "venv", ("{dir}",)),)


def test_markers_are_discovered_by_run_all() -> None:
    found = {e.name: e for e in run_all.discover(EXAMPLES)}
    assert set(NAMES) <= set(found)
    assert all(found[n].marker is not None for n in NAMES)


def test_dry_listing_shows_the_three_rows(capsys: pytest.CaptureFixture) -> None:
    run_all.main([run_all.LIST_FLAG, str(EXAMPLES)])
    lines = capsys.readouterr().out.splitlines()
    for name in NAMES:
        row = next(ln for ln in lines if name in ln)
        w, h = DIMS[name]
        assert f"{w:g} x {h:g}" in row and "1 post_check" in row


# --- checker: synthetic inputs ----------------------------------------------------------------
def track(net: str, layer: str, width: float, kind: str = "track") -> dict:
    return {"kind": kind, "layer": layer, "net": net, "start": (0.0, 0.0), "end": (1.0, 0.0),
            "width": width}


def via(net: str) -> dict:
    return {"kind": "via", "layers": ("F.Cu", "B.Cu"), "net": net, "center": (0.0, 0.0),
            "radius": 0.3, "drill": 0.3, "via_type": "through"}


def gate(name: str, passed: bool = True, warnings: tuple = ()) -> dict:
    return {"gate": name, "passed": passed, "blockers": [] if passed else ["x"],
            "warnings": list(warnings)}


def passing_report(**extra: Any) -> dict:
    return {"PASS": True, "gates": [gate(g) for g in ("G1", "G2", "G3", "G4", "G5")],
            "lint": {"ok": True, "errors": [], "warnings": []},
            "drc": {"errors": 0, "unconnected": 0},
            "dru": {"ok": True, "fired": 1, "rc": 0, "crash": False},
            "layer_roles": {"judged": True, "issues": [], "warnings": []},
            "dsn_policy": {"routed_layers": {}}, **extra}


ROLES_4L = {"F.Cu": {"role": "signal", "net": None}, "In1.Cu": {"role": "plane", "net": "GND"},
            "In2.Cu": {"role": "plane", "net": "V5"}, "B.Cu": {"role": "signal", "net": None}}


def test_parse_args() -> None:
    assert ck.parse_args(["x"]) == ck.Args("x", None)
    assert ck.parse_args(["x", "--expect", "six_layer_mcu"]) == ck.Args("x", "six_layer_mcu")
    assert ck.parse_args(["--expect=stackup_2l_mixed", "x"]) == ck.Args("x", "stackup_2l_mixed")
    for bad in ([], ["a", "b"], ["x", "--expect"], ["x", "--expect", "nope"], ["x", "--no"]):
        with pytest.raises(ck.UsageError):
            ck.parse_args(bad)


def test_expectations_cover_the_three_examples_only() -> None:
    assert set(ck.EXPECTATIONS) == set(NAMES)
    assert [ck.EXPECTATIONS[n].rows for n in NAMES] == [5, 16, 10]
    assert [ck.EXPECTATIONS[n].thickness for n in NAMES] == [1.641, 1.6554, 1.3685]
    assert [ck.EXPECTATIONS[n].ipc2581 for n in NAMES] == [False, True, True]
    assert ck.EXPECTATIONS["stackup_2l_mixed"].st_codes == ("ST-W3",)
    assert ck.EXPECTATIONS["six_layer_mcu"].st_codes == ("ST-W10",)
    assert ck.EXPECTATIONS["stackup_hybrid_4l"].st_codes == ("ST-W5", "ST-W6")
    two, hybrid = ck.EXPECTATIONS["stackup_2l_mixed"], ck.EXPECTATIONS["stackup_hybrid_4l"]
    assert two.fd_refs == {("RF_IN", "F.Cu"): 0.8664, ("RF_MID", "F.Cu"): 0.8664,
                           ("RF_OUT", "F.Cu"): 0.8664}
    assert hybrid.fd_refs == {("RF_IN", "F.Cu"): 0.4942, ("AMP_IN", "F.Cu"): 0.4942,
                              ("AMP_OUT", "F.Cu"): 0.3975, ("RF_OUT", "F.Cu"): 0.3975}
    assert ck.EXPECTATIONS["six_layer_mcu"].fd_refs == {}


def test_gate_problems() -> None:
    assert ck.gate_problems(passing_report()) == []
    failed = {**passing_report(), "PASS": False, "gates": [gate("G1"), gate("G2", False)]}
    found = ck.gate_problems(failed)
    assert any("G2" in p for p in found) and any("G3" in p and "never" in p for p in found)
    assert any("PASS" in p for p in ck.gate_problems({**passing_report(), "PASS": False}))
    assert ck.gate_problems({}) and ck.gate_problems({"PASS": True, "gates": []})


def test_lint_code_problems() -> None:
    rep = passing_report(lint={"warnings": ["ST-W3 at $.stackup.layers: floor F1", "LIB-W1 x"]})
    assert ck.lint_code_problems(rep, ("ST-W3",)) == []
    assert ck.lint_code_problems(rep, ("ST-W5", "ST-W3")) == [
        "G1 warnings carry no ST-W5 (ADR D7.2.7); the ST codes seen: ST-W3"]
    assert ck.lint_code_problems(rep, ()) == []
    assert ck.lint_code_problems({}, ("ST-W3",))


def test_stackup_problems_on_a_resolved_board() -> None:
    design = _example("stackup_hybrid_4l")
    rs, _ = sm.resolve(design)
    artifact = {"stackup": rs.to_dict()}
    text = _board_text(rs)
    assert ck.stackup_problems(design, artifact, text) == []
    assert ck.stackup_problems(design, {"stackup": {**rs.to_dict(), "fab": "jlcpcb"}}, text)
    broken = text.replace('"In2.Cu" power', '"In2.Cu" signal')
    assert any("In2.Cu" in p for p in ck.stackup_problems(design, artifact, broken))
    assert ck.stackup_problems(design, {}, text)
    assert ck.stackup_problems({**design, "stackup": {"preset": "nope:x"}}, artifact, text)


def _board_text(rs: sm.ResolvedStackup) -> str:
    """A minimal KiCad 9 board text that carries ``rs`` (the splice the builder does)."""
    layers = "\n".join(f'\t\t({c.kicad_id} "{c.name}" {c.kicad_type})' for c in rs.copper)
    head = (f"(kicad_pcb\n\t(version 20241229)\n\t(generator \"pcbnew\")\n\t(general\n"
            f"\t\t(thickness {rs.board_thickness_computed})\n\t)\n\t(layers\n{layers}\n"
            f"\t\t(25 \"Edge.Cuts\" user)\n\t)\n\t(setup\n\t\t(pad_to_mask_clearance 0)\n\t)\n)\n")
    return sk.splice(head, sk.emit(rs))


def test_rules_problems() -> None:
    rep = passing_report()
    dru = f"(version 1)\n{ck.dw.BEGIN_MARKER}\n{ck.dg.CANARY_RULE}\n{ck.dw.END_MARKER}\n"
    assert ck.rules_problems(rep, dru) == []
    assert ck.rules_problems({**rep, "dru": {"ok": False, "reason": "dropped"}}, dru)
    assert ck.rules_problems({**rep, "dru": {"ok": True, "fired": 0}}, dru)
    assert ck.rules_problems(rep, "(version 1)\n")
    assert ck.rules_problems(rep, None)


def test_drc_problems() -> None:
    rep, doc = passing_report(), {"violations": [], "unconnected_items": []}
    assert ck.drc_problems(rep, doc) == []
    assert ck.drc_problems({**rep, "drc": {"errors": 1, "unconnected": 0}}, doc)
    assert ck.drc_problems(rep, {"violations": [{"type": "clearance"}], "unconnected_items": []})
    assert ck.drc_problems(rep, {"violations": [], "unconnected_items": [{}]})
    assert ck.drc_problems(rep, None)


def test_foreign_plane_tracks_and_r8_problems() -> None:
    items = [track("GND", "In1.Cu", 0.2), track("SIG", "In1.Cu", 0.2), track("SIG", "F.Cu", 0.2),
             track("V5", "In2.Cu", 0.3, "arc"), via("SIG")]
    assert ck.foreign_plane_tracks(items, ROLES_4L) == {"In1.Cu": 1, "In2.Cu": 0}
    rep = passing_report()
    assert ck.r8_problems(rep, [track("GND", "In1.Cu", 0.2)], ROLES_4L) == []
    found = ck.r8_problems(rep, items, ROLES_4L)
    assert len(found) == 1 and "In1.Cu" in found[0] and "SIG" in found[0]
    assert ck.r8_problems({**rep, "layer_roles": {"judged": False}}, [], ROLES_4L)
    assert ck.r8_problems({**rep, "layer_roles": {"judged": True, "issues": ["R8 x"]}}, [],
                          ROLES_4L)


LINES = {"RF_IN": {"kind": "single", "layers": {"F.Cu": {"width": 0.41}}},
         "USB_DP": {"kind": "diff", "layers": {"F.Cu": {"width": 0.39, "pair_gap": 0.39}}}}
SPECS = {"RF_IN": CPWG, "USB_DP": {"diff": 90, "diff_pair": "USB_DM", "layers": ["F.Cu"]}}
ROUTED = {"RF_IN": ["F.Cu"], "USB_DP": ["F.Cu"], "USB_DM": ["F.Cu"]}


def test_line_widths_include_the_diff_partner() -> None:
    assert ck.line_widths({"net_specs": SPECS}, LINES) == {
        "RF_IN": {"F.Cu": 0.41}, "USB_DP": {"F.Cu": 0.39}, "USB_DM": {"F.Cu": 0.39}}


def test_width_problems() -> None:
    widths = ck.line_widths({"net_specs": SPECS}, LINES)
    good = [track("RF_IN", "F.Cu", 0.41), track("RF_IN", "F.Cu", 0.4105, "arc"),
            track("USB_DP", "F.Cu", 0.39), track("USB_DM", "F.Cu", 0.3899), track("GND", "B.Cu", 1)]
    assert ck.width_problems(good, widths, ROUTED) == []
    off = [*good, track("RF_IN", "F.Cu", 0.412)]
    assert any("0.412" in p and "RF_IN" in p for p in ck.width_problems(off, widths, ROUTED))
    elsewhere = [*good, track("USB_DM", "B.Cu", 0.39)]
    assert any("B.Cu" in p and "USB_DM" in p for p in ck.width_problems(elsewhere, widths, ROUTED))
    unrouted = [t for t in good if t["net"] != "USB_DP"]
    found = ck.width_problems(unrouted, widths, ROUTED)
    assert any("USB_DP" in p and "no track" in p for p in found)
    assert any("routed_layers" in p for p in ck.width_problems(good, widths, {}))


def test_width_summary_counts_tracks_per_net_and_layer() -> None:
    items = [track("RF_IN", "F.Cu", 0.41), track("RF_IN", "F.Cu", 0.41, "arc"), via("RF_IN")]
    assert ck.width_summary(items, {"RF_IN": {"F.Cu": 0.41}}, {"RF_IN": ["F.Cu"]}) == {
        "RF_IN": {"F.Cu": {"tracks": 2, "artifact_mm": 0.41, "widths_mm": [0.41]}}}


def test_fd_problems() -> None:
    lines = {"RF_IN": {"status": "fd", "layers": {"F.Cu": {"width": 0.87}}},
             "RF_OUT": {"status": "closed_form", "layers": {"F.Cu": {"width": 0.895}}}}
    refs = {("RF_IN", "F.Cu"): 0.8664, ("RF_OUT", "F.Cu"): 0.8664, ("X", "F.Cu"): 1.0}
    found = ck.fd_problems(lines, refs)
    assert len(found) == 2
    assert any("RF_OUT" in p and "3.3 %" in p for p in found)
    assert any("X" in p and "no line" in p for p in found)
    assert ck.fd_problems(lines, {}) == []


def _artifact_4l() -> dict:
    rs, _ = sm.resolve(_example("stackup_hybrid_4l"))
    return {"stackup": rs.to_dict(), "lines": {}}


def _job_text(artifact: dict, **specs: Any) -> str:
    """A job file carrying the artifact's stackup (as KiCad 10 writes it)."""
    rows = fs.expected_thickness_rows(artifact)
    st = artifact["stackup"]
    material = [{"Type": r["type"], "Name": r["name"], "Thickness": r["thickness"],
                 **({"Material": r["material"]} if r["material"] else {}),
                 **({"DielectricConstant": r["dk"]} if r["dk"] is not None else {}),
                 **({"LossTangent": r["df"]} if r["df"] is not None else {})} for r in rows]
    files = [{"Path": "x-F_Cu.gtl", "FileFunction": "Copper,L1,Top"},
             {"Path": "x-In1_Cu.g2", "FileFunction": "Copper,L2,Inr"},
             {"Path": "x-In2_Cu.g3", "FileFunction": "Copper,L3,Inr"},
             {"Path": "x-B_Cu.gbl", "FileFunction": "Copper,L4,Bot"}]
    general = {"LayerNumber": st["kicad_copper_count"], "BoardThickness":
               st["board_thickness_computed"], "ImpedanceControlled": True, "Finish": "ENIG",
               **specs}
    return json.dumps({"GeneralSpecs": general, "FilesAttributes": files,
                       "MaterialStackup": material})


def test_gbrjob_problems(tmp_path: Path) -> None:
    artifact, exp = _artifact_4l(), ck.EXPECTATIONS["stackup_hybrid_4l"]
    job = tmp_path / "x-job.gbrjob"
    job.write_text(_job_text(artifact), encoding="utf-8")
    assert ck.gbrjob_problems(job, artifact, exp) == []
    assert ck.gbrjob_problems(job, artifact, exp._replace(rows=9))
    assert ck.gbrjob_problems(job, artifact, exp._replace(thickness=1.6))
    job.write_text(_job_text(artifact, BoardThickness=1.6), encoding="utf-8")
    assert any("BoardThickness" in p for p in ck.gbrjob_problems(job, artifact, exp))
    job.write_text(_job_text(artifact).replace("Copper,L3,Inr", "Copper,L7,Inr"),
                   encoding="utf-8")
    assert any("L7" in p for p in ck.gbrjob_problems(job, artifact, exp))
    assert ck.gbrjob_problems(tmp_path / "missing.gbrjob", artifact, exp)


def test_ipc2581_problems(tmp_path: Path) -> None:
    artifact, exp = _artifact_4l(), ck.EXPECTATIONS["stackup_hybrid_4l"]
    path = tmp_path / "x_ipc2581.xml"
    assert ck.ipc2581_problems(path, artifact, exp)              # needed, missing
    path.write_text('<?xml version="1.0"?>\n<IPC-2581 revision="C"></IPC-2581>\n',
                    encoding="utf-8")
    assert ck.ipc2581_problems(path, artifact, exp) == []
    assert ck.ipc2581_problems(path, artifact, exp._replace(ipc2581=False))
    path.write_text("not xml", encoding="utf-8")
    assert ck.ipc2581_problems(path, artifact, exp)
    two, _ = sm.resolve(_example("stackup_2l_mixed"))
    exp2 = ck.EXPECTATIONS["stackup_2l_mixed"]
    assert ck.ipc2581_problems(tmp_path / "none.xml", {"stackup": two.to_dict()}, exp2) == []
    assert ck.ipc2581_problems(path, {"stackup": two.to_dict()}, exp2)   # present, not needed


def test_csv_and_notes_problems(tmp_path: Path) -> None:
    design = _example("stackup_hybrid_4l")
    rs, _ = sm.resolve(design)
    artifact = {"stackup": rs.to_dict(),
                "lines": {"RF_IN": {"kind": "single", "z_target": 50, "primary_layer": "F.Cu",
                                    "line": "microstrip", "status": "fd",
                                    "layers": {"F.Cu": {"width": 0.494, "ref": {"nets": ["GND"]},
                                                        "accuracy_class": "B"}}}}}
    out = fs.write_outputs(tmp_path, "netlist", artifact, design=design)
    assert ck.csv_problems(Path(out["csv"]), artifact) == []
    assert ck.notes_problems(Path(out["notes"]), artifact, design) == []
    (tmp_path / "bad.csv").write_text("layer,type\nF.Cu,copper\n", encoding="utf-8")
    assert ck.csv_problems(tmp_path / "bad.csv", artifact)
    assert ck.csv_problems(tmp_path / "none.csv", artifact)
    (tmp_path / "bad.md").write_text("# Fab notes\n", encoding="utf-8")
    found = ck.notes_problems(tmp_path / "bad.md", artifact, design)
    assert any("RF_IN" in p for p in found) and any("rationale" in p for p in found)
    assert ck.notes_problems(tmp_path / "none.md", artifact, design)


def test_zip_problems(tmp_path: Path) -> None:
    a, b = tmp_path / "a.txt", tmp_path / "b.txt"
    a.write_text("a", encoding="utf-8")
    b.write_text("b", encoding="utf-8")
    zpath = tmp_path / "fab.zip"
    with zipfile.ZipFile(zpath, "w") as z:
        z.write(a, "a.txt")
    assert ck.zip_problems(zpath, [a]) == []
    assert ck.zip_problems(zpath, [a, b]) == ["b.txt is not in the fab zip fab.zip"]
    assert ck.zip_problems(tmp_path / "none.zip", [a])
    assert ck.zip_problems(None, [a])


def test_expectation_for_picks_by_netlist_name_or_flag() -> None:
    assert ck.expectation_for({"name": "six_layer_mcu"}, None) is ck.EXPECTATIONS["six_layer_mcu"]
    assert ck.expectation_for({"name": "copy"}, "stackup_2l_mixed") is \
        ck.EXPECTATIONS["stackup_2l_mixed"]
    assert ck.expectation_for({"name": "copy"}, None) is None


def test_run_checks_needs_the_run_files(tmp_path: Path) -> None:
    with pytest.raises(ck.CheckError):
        ck.run_checks(ck.Args(str(tmp_path), None))
    (tmp_path / "netlist.json").write_text(json.dumps(_example("stackup_2l_mixed")),
                                           encoding="utf-8")
    with pytest.raises(ck.CheckError, match="pipeline report"):
        ck.run_checks(ck.Args(str(tmp_path), None))


def test_main_exit_codes(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    assert ck.main([]) == ck.EXIT_USAGE
    assert ck.main([str(tmp_path / "missing")]) == ck.EXIT_USAGE
    out = capsys.readouterr().out.strip().splitlines()
    assert all(json.loads(ln)["ok"] is False for ln in out)


# --- live: the three examples through the pipeline, then the checker -------------------------
def _live_missing() -> str | None:
    if not tc.kicad_python():
        return f"no KiCad pcbnew Python: {tc.kicad_python_error()}"
    if not tc.board_cli():
        return "kicad-cli not found"
    if not tc.find_java() or not tc.freerouting_jar(""):
        return "Java or the Freerouting jar is missing"
    return None


@pytest.fixture(scope="module")
def live_runs(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, Path]]:
    """Each example on a fresh copy through run_pipeline (route freerouting, as run_all
    does; KICAD_EDA_FD_BUDGET_S raised so the 1.5 mm CPWG synthesis finishes)."""
    missing = _live_missing()
    if missing:
        pytest.skip(missing)
    import run_pipeline as rp
    root = tmp_path_factory.mktemp("e2e_stackup")
    budget = os.environ.get("KICAD_EDA_FD_BUDGET_S")
    os.environ["KICAD_EDA_FD_BUDGET_S"] = os.environ.get("KICAD_EDA_FD_BUDGET_S", "900")
    dirs: dict[str, Path] = {}
    try:
        for name in NAMES:
            folder = root / name
            folder.mkdir()
            shutil.copy(EXAMPLES / name / "netlist.json", folder / "netlist.json")
            w, h = DIMS[name]
            rp.run(str(folder / "netlist.json"), w, h, route=run_all.ROUTE)
            dirs[name] = folder
    finally:
        if budget is None:
            os.environ.pop("KICAD_EDA_FD_BUDGET_S", None)
        else:
            os.environ["KICAD_EDA_FD_BUDGET_S"] = budget
    yield dirs


@pytest.mark.parametrize("name", NAMES)
def test_live_example_passes_the_pipeline_and_the_checker(live_runs: dict[str, Path],
                                                          name: str) -> None:
    result = ck.run_checks(ck.Args(str(live_runs[name]), None))
    assert result["problems"] == [], json.dumps(result, indent=1, default=str)[:4000]
    assert result["ok"] and result["example"] == name
    assert result["checks"]["fd_references"]["compared"] == len(ck.EXPECTATIONS[name].fd_refs)


@pytest.mark.parametrize("name", NAMES)
def test_live_checker_cli_exits_0(live_runs: dict[str, Path], name: str) -> None:
    buf = io.StringIO()
    code = ck.main([str(live_runs[name])], out=buf)
    assert code == ck.EXIT_OK, buf.getvalue()[:2000]
    assert json.loads(buf.getvalue().strip().splitlines()[-1])["ok"] is True
