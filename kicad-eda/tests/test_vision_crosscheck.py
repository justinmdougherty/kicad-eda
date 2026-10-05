"""server/tools/vision_crosscheck.py: compare two independent vision
extractions of one schematic (ROADMAP_DECISIONS D4) and check a reading's
pin numbers against its KiCad symbols.

Unit tier (no KiCad): component-level diffs (refs, value, lib_id,
footprint), connectivity disagreements through net_equivalence (split,
merge, missing / extra pins, pad conflicts, no-connect mismatches), the
agreement score, the reviewer report and the CLI; the pin check against the
committed tests/fixtures/symbols libraries, stock and project-local.

Live tier: the pin check against KiCad's own stock symbols; skipped without
KiCad.
"""
from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path
from typing import Any

import pytest

import vision_crosscheck as vc
from fixtures.kicad_install import KICAD, requires_symbols

FIXTURE_SYMBOLS = Path(__file__).resolve().parent / "fixtures" / "symbols"

BASE: dict[str, Any] = {
    "name": "ldo",
    "components": [
        {"ref": "J1", "lib_id": "Connector_Generic:Conn_01x02", "value": "PWR",
         "footprint": "Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical"},
        {"ref": "U1", "lib_id": "Regulator_Linear:AMS1117-3.3", "value": "AMS1117-3.3",
         "footprint": "Package_TO_SOT_SMD:SOT-223-3_TabPin2"},
        {"ref": "C1", "lib_id": "Device:C", "value": "10uF",
         "footprint": "Capacitor_SMD:C_0805_2012Metric"},
        {"ref": "C2", "lib_id": "Device:C", "value": "100nF",
         "footprint": "Capacitor_SMD:C_0603_1608Metric"},
        {"ref": "R1", "lib_id": "Device:R", "value": "4k7",
         "footprint": "Resistor_SMD:R_0603_1608Metric"},
        {"ref": "D1", "lib_id": "Device:LED", "value": "RED",
         "footprint": "LED_SMD:LED_0603_1608Metric"},
    ],
    "nets": {
        "VIN": [["J1", "1"], ["U1", "3"], ["C1", "1"]],
        "GND": [["J1", "2"], ["U1", "1"], ["C1", "2"], ["C2", "2"], ["D1", "1"]],
        "+3V3": [["U1", "2"], ["C2", "1"], ["R1", "1"]],
        "LED_A": [["R1", "2"], ["D1", "2"]],
    },
    "no_connect": [],
}
CONNECTED_PADS = 13


def design() -> dict[str, Any]:
    return copy.deepcopy(BASE)


def comp(d: dict[str, Any], ref: str) -> dict[str, Any]:
    return next(c for c in d["components"] if c["ref"] == ref)


def kinds(res: vc.CrosscheckResult) -> list[str]:
    return [x.kind for x in res.disagreements]


def move_pad(d: dict[str, Any], pad: list[str], src: str, dst: str) -> None:
    d["nets"][src].remove(pad)
    d["nets"][dst].append(pad)


# ----------------------------------------------------------------------------
# Agreement
# ----------------------------------------------------------------------------
def test_identical_readings_agree() -> None:
    res = vc.crosscheck(design(), design())
    assert res.agree and res.disagreements == ()
    assert res.score == 1.0 and dict(res.scores) == {"components": 1.0, "fields": 1.0,
                                                     "connectivity": 1.0}
    assert res.counts["connected_pads"] == res.counts["pads_agreeing"] == CONNECTED_PADS
    assert res.counts["fields_compared"] == 18 and res.counts["common_refs"] == 6
    assert res.as_dict()["verdict"] == "agree"
    assert "agree" in res.summary()


def test_net_names_do_not_matter() -> None:
    b = design()
    b["nets"]["3V3"] = b["nets"].pop("+3V3")
    b["nets"]["Net-(D1-A)"] = b["nets"].pop("LED_A")
    res = vc.crosscheck(design(), b)
    assert res.agree, res.markdown()
    assert res.equivalence["details"]["net_name_map"]["+3V3"] == "3V3"


def test_component_order_and_pad_order_do_not_matter() -> None:
    b = design()
    b["components"].reverse()
    for pads in b["nets"].values():
        pads.reverse()
    assert vc.crosscheck(design(), b).agree


def test_ref_map_matches_renamed_references() -> None:
    b = design()
    comp(b, "R1")["ref"] = "R10"
    for pads in b["nets"].values():
        for pad in pads:
            if pad[0] == "R1":
                pad[0] = "R10"
    assert not vc.crosscheck(design(), b).agree
    res = vc.crosscheck(design(), b, ref_map={"R1": "R10"})
    assert res.agree, res.markdown()


# ----------------------------------------------------------------------------
# Component-level disagreements (criterion 3)
# ----------------------------------------------------------------------------
def test_value_mismatch_is_high_severity() -> None:
    b = design()
    comp(b, "R1")["value"] = "47k"
    res = vc.crosscheck(design(), b, labels=("reader1", "reader2"))
    (d,) = res.disagreements
    assert (d.kind, d.severity, d.subject, d.a, d.b) == ("field_mismatch", "high", "R1",
                                                         "4k7", "47k")
    assert d.field == "value"
    assert "reader1" in d.message and "reader2" in d.message and "4k7" in d.message
    assert not res.agree and res.as_dict()["verdict"] == "review"
    assert res.scores["fields"] == pytest.approx(17 / 18, abs=1e-4)
    assert res.score == pytest.approx(0.25 + 0.25 * 17 / 18 + 0.5, abs=1e-4)


@pytest.mark.parametrize("field", ["lib_id", "footprint"])
def test_symbol_and_footprint_mismatch_is_medium(field: str) -> None:
    b = design()
    comp(b, "C2")[field] = comp(b, "C2")[field] + "_Alt"
    (d,) = vc.crosscheck(design(), b).disagreements
    assert (d.kind, d.severity, d.subject, d.field) == ("field_mismatch", "med", "C2", field)


def test_field_missing_on_one_side() -> None:
    b = design()
    del comp(b, "D1")["footprint"]
    (d,) = vc.crosscheck(design(), b).disagreements
    assert (d.kind, d.field, d.b) == ("field_mismatch", "footprint", None)
    assert "has none" in d.message


def test_field_absent_on_both_sides_is_not_compared() -> None:
    a, b = design(), design()
    del comp(a, "D1")["footprint"], comp(b, "D1")["footprint"]
    res = vc.crosscheck(a, b)
    assert res.agree and res.counts["fields_compared"] == 17


def test_component_only_in_one_reading() -> None:
    b = design()
    b["components"] = [c for c in b["components"] if c["ref"] != "D1"]
    b["nets"]["GND"].remove(["D1", "1"])
    b["nets"]["LED_A"].remove(["D1", "2"])
    res = vc.crosscheck(design(), b)
    assert kinds(res) == ["component_missing"]          # its pins are not repeated as pin_missing
    (d,) = res.disagreements
    assert (d.severity, d.subject) == ("high", "D1")
    assert "GND" in d.message and "LED_A" in d.message
    assert res.scores["components"] == pytest.approx(5 / 6, abs=1e-4)
    # D1.1, D1.2 absent from B and R1.2 left alone there: 10 of 13 pins agree
    assert res.scores["connectivity"] == pytest.approx(10 / 13, abs=1e-4)

    reverse = vc.crosscheck(b, design())
    assert kinds(reverse) == ["component_extra"]


def test_listed_part_without_wiring_still_reports_its_pins() -> None:
    """B lists D1 but wires none of its pins: no component diff, so the pins
    themselves are the disagreement."""
    b = design()
    b["nets"]["GND"].remove(["D1", "1"])
    b["nets"]["LED_A"].remove(["D1", "2"])
    res = vc.crosscheck(design(), b)
    assert "component_missing" not in kinds(res)
    missing = [d for d in res.disagreements if d.kind == "pin_missing"]
    assert {d.subject for d in missing} == {"GND", "LED_A"}
    assert all("D1" in d.message for d in missing)


def test_duplicate_reference_is_reported() -> None:
    b = design()
    b["components"].append(dict(comp(b, "C2"), value="1uF"))
    res = vc.crosscheck(design(), b)
    assert kinds(res) == ["duplicate_ref"]
    (d,) = res.disagreements
    assert (d.subject, d.severity, d.b) == ("C2", "high", 2)


# ----------------------------------------------------------------------------
# Values compare as quantities
# ----------------------------------------------------------------------------
@pytest.mark.parametrize("a, b", [
    ("100nF", "0.1uF"), ("100n", "100nF"), ("4k7", "4.7k"), ("4K7", "4700"), ("10k", "10K"),
    ("2R2", "2.2"), ("0R", "0"), ("1µF", "1uF"), ("1μF", "1u"), ("LM358", "lm358"),
    ("10uF 25V", "10µF/25V"), ("100 nF", "100nF"), ("2.2Ω", "2R2"), ("10kΩ", "10k"),
    ("470R", "470"), ("0.1", "100m"), ("1u0", "1uF"), (" RED ", "red"),
])
def test_values_agree(a: str, b: str) -> None:
    assert vc.values_agree(a, b) and vc.values_agree(b, a)


@pytest.mark.parametrize("a, b", [
    ("1M", "1m"), ("10k", "1k"), ("100nF", "100uF"), ("1uF", "1uH"), ("LM358", "LM324"),
    ("10uF 25V", "10uF 16V"), ("10uF", "10uF 25V"), ("4k7", "47k"),
])
def test_values_disagree(a: str, b: str) -> None:
    assert not vc.values_agree(a, b) and not vc.values_agree(b, a)


@pytest.mark.parametrize("text, expected", [
    ("4k7", (4700.0, "")), ("100nF", (1e-7, "F")), ("2R2", (2.2, "ohm")), ("10kΩ", (1e4, "ohm")),
    ("1.5 mH", (1.5e-3, "H")), ("12V", (12.0, "V")), ("LM358", None), ("", None),
])
def test_parse_value(text: str, expected: tuple[float, str] | None) -> None:
    got = vc.parse_value(text)
    if expected is None:
        assert got is None
    else:
        assert got is not None and got[0] == pytest.approx(expected[0]) and got[1] == expected[1]


# ----------------------------------------------------------------------------
# Connectivity disagreements (criterion 3)
# ----------------------------------------------------------------------------
def test_pin_on_the_wrong_net_is_a_split_and_a_merge() -> None:
    b = design()
    move_pad(b, ["C2", "1"], "+3V3", "VIN")
    res = vc.crosscheck(design(), b)
    assert kinds(res) == ["net_split", "net_merge"]
    split, merge = res.disagreements
    assert (split.subject, split.severity) == ("+3V3", "high")
    assert "C2.1" in split.message and "VIN" in split.message
    assert merge.subject == "VIN" and "+3V3" in merge.message
    assert split.a == {"net": "+3V3", "pads": [["C2", "1"], ["R1", "1"], ["U1", "2"]]}
    # only C2.1 sits on the wrong (best-matching) net
    assert res.scores["connectivity"] == pytest.approx(12 / 13, abs=1e-4)
    assert res.counts["pads_agreeing"] == 12


def test_connected_pin_missing_from_the_other_reading() -> None:
    b = design()
    b["nets"]["+3V3"].remove(["R1", "1"])
    res = vc.crosscheck(design(), b)
    assert kinds(res) == ["pin_missing"]
    (d,) = res.disagreements
    assert (d.subject, d.severity, d.a) == ("+3V3", "high", [["R1", "1"]])
    assert "R1.1" in d.message
    assert res.scores["connectivity"] == pytest.approx(12 / 13, abs=1e-4)
    assert kinds(vc.crosscheck(b, design())) == ["pin_extra"]


def test_two_nets_joined_into_one() -> None:
    b = design()
    b["nets"]["VIN"] += b["nets"].pop("+3V3")
    res = vc.crosscheck(design(), b)
    assert kinds(res) == ["net_merge"]
    (d,) = res.disagreements
    assert d.subject == "VIN" and "+3V3" in d.message
    assert res.scores["connectivity"] == pytest.approx(10 / 13, abs=1e-4)


def test_pad_listed_on_two_nets_is_a_conflict() -> None:
    b = design()
    b["nets"]["LED_A"].append(["U1", "2"])           # U1.2 also stays on +3V3
    res = vc.crosscheck(design(), b)
    conflict = [d for d in res.disagreements if d.kind == "pad_conflict"]
    assert len(conflict) == 1 and conflict[0].subject == "U1.2"
    assert conflict[0].b == ["+3V3", "LED_A"] and conflict[0].severity == "high"
    assert "net_merge" in kinds(res)                 # B's joined net is reported too


def test_no_connect_marked_by_one_reading_only() -> None:
    a, b = design(), design()
    for d in (a, b):
        d["components"].append({"ref": "J2", "lib_id": "Connector_Generic:Conn_01x03",
                                "value": "DBG", "footprint": "Connector:Conn_01x03"})
        d["nets"]["GND"].append(["J2", "1"])
        d["nets"]["+3V3"].append(["J2", "2"])
    a["no_connect"] = [["J2", "3"]]
    res = vc.crosscheck(a, b)
    (d,) = res.disagreements
    assert (d.kind, d.severity, d.subject) == ("no_connect_mismatch", "low", "J2.3")
    assert "does not list" in d.message
    assert res.scores["connectivity"] == 1.0           # an unconnected pin connects nothing

    b["nets"]["DBG"] = [["J2", "3"]]                   # B: alone on a net of its own
    (d,) = vc.crosscheck(a, b).disagreements
    assert d.kind == "no_connect_mismatch" and "DBG" in d.message


def test_no_connect_against_a_real_connection_is_a_merge() -> None:
    a, b = design(), design()
    a["nets"]["LED_A"].remove(["D1", "2"])
    a["nets"]["LED_A"].append(["U1", "2"])           # keep LED_A a 2-pin net in A
    a["nets"]["+3V3"].remove(["U1", "2"])
    a["no_connect"] = [["D1", "2"]]
    res = vc.crosscheck(a, b)
    assert "no_connect_mismatch" not in kinds(res)
    assert {"net_split", "net_merge"} <= set(kinds(res))


# ----------------------------------------------------------------------------
# Report shape
# ----------------------------------------------------------------------------
def test_disagreements_are_ordered_by_severity() -> None:
    b = design()
    comp(b, "C2")["footprint"] = "Capacitor_SMD:C_0402_1005Metric"     # med
    comp(b, "R1")["value"] = "47k"                                      # high
    b["no_connect"] = [["U1", "4"]]                                     # low (A has no U1.4)
    move_pad(b, ["C2", "1"], "+3V3", "VIN")                             # high
    res = vc.crosscheck(design(), b)
    severities = [d.severity for d in res.disagreements]
    assert severities == sorted(severities, key=vc.SEVERITIES.index)
    assert res.as_dict()["by_severity"] == {"high": 3, "med": 1, "low": 1}


def test_as_dict_is_json_ready_and_markdown_lists_every_disagreement() -> None:
    b = design()
    comp(b, "R1")["value"] = "47k|x"
    move_pad(b, ["C2", "1"], "+3V3", "VIN")
    res = vc.crosscheck(design(), b, labels=("first", "second"))
    data = json.loads(json.dumps(res.as_dict()))
    assert data["labels"] == ["first", "second"] and len(data["disagreements"]) == 3
    assert data["weights"] == {"components": 0.25, "fields": 0.25, "connectivity": 0.5}
    assert data["equivalence"]["equal"] is False
    md = res.markdown()
    assert md.startswith("# Vision cross-check: first vs second")
    assert "REVIEW" in md and "47k\\|x" in md                 # table cells are escaped
    for d in res.disagreements:
        assert f"| {d.kind} |" in md


@pytest.mark.parametrize("mutate, message", [
    (lambda d: d.update(nets=[]), "B"),
    (lambda d: d.update(components="U1"), "components"),
    (lambda d: d["components"].append({"value": "x"}), "ref"),
    (lambda d: d["nets"]["GND"].append(["U1"]), "ref, pad"),
])
def test_malformed_design_fails_loudly(mutate: Any, message: str) -> None:
    b = design()
    mutate(b)
    with pytest.raises(ValueError, match=message):
        vc.crosscheck(design(), b)


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------
def write(path: Path, data: dict[str, Any]) -> Path:
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_cli_compare_exit_codes_and_outputs(tmp_path: Path,
                                            capsys: pytest.CaptureFixture[str]) -> None:
    a = write(tmp_path / "a.json", design())
    assert vc.main(["compare", str(a), str(a)]) == 0
    assert "AGREE" in capsys.readouterr().out

    b_design = design()
    comp(b_design, "R1")["value"] = "47k"
    b = write(tmp_path / "b.json", b_design)
    out, md = tmp_path / "x" / "report.json", tmp_path / "x" / "report.md"
    rc = vc.main(["compare", str(a), str(b), "--label-a", "r1", "--label-b", "r2",
                  "--out", str(out), "--md", str(md), "--json"])
    assert rc == 1
    printed = json.loads(capsys.readouterr().out)
    assert printed == json.loads(out.read_text(encoding="utf-8"))
    assert printed["labels"] == ["r1", "r2"] and printed["disagreements"][0]["subject"] == "R1"
    assert md.read_text(encoding="utf-8").startswith("# Vision cross-check: r1 vs r2")


def test_cli_compare_ref_map(tmp_path: Path) -> None:
    b_design = design()
    comp(b_design, "D1")["ref"] = "LED1"
    for pads in b_design["nets"].values():
        for pad in pads:
            if pad[0] == "D1":
                pad[0] = "LED1"
    a, b = write(tmp_path / "a.json", design()), write(tmp_path / "b.json", b_design)
    assert vc.main(["compare", str(a), str(b)]) == 1
    assert vc.main(["compare", str(a), str(b), "--ref-map", '{"D1": "LED1"}']) == 0
    ref_map = write(tmp_path / "map.json", {"D1": "LED1"})
    assert vc.main(["compare", str(a), str(b), "--ref-map", str(ref_map)]) == 0


def test_cli_compare_errors_exit_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    a = write(tmp_path / "a.json", design())
    bad = tmp_path / "bad.json"
    bad.write_text("[1, 2]", encoding="utf-8")
    assert vc.main(["compare", str(a), str(bad)]) == 2
    assert "bad.json" in capsys.readouterr().err
    assert vc.main(["compare", str(a), str(tmp_path / "missing.json")]) == 2


# ----------------------------------------------------------------------------
# Pin check against the chosen KiCad symbols
# ----------------------------------------------------------------------------
def amp_design() -> dict[str, Any]:
    return {
        "components": [
            {"ref": "U1", "lib_id": "TestLib:BaseAmp", "value": "AMP", "footprint": "X:Y"},
            {"ref": "U2", "lib_id": "TestLib:DerivedAmp", "value": "AMP2", "footprint": "X:Y"},
            {"ref": "R1", "lib_id": "TestLib:PosFileR", "value": "1k", "footprint": "X:Y"},
            {"ref": "H1", "footprint": "MountingHole:MountingHole_3.2mm_M3",
             "board_only": True},
        ],
        "nets": {
            "OUT": [["U1", "1"], ["R1", "1"], ["U2", "1"]],
            "INN": [["U1", "2"], ["R1", "2"], ["U2", "2"]],
            "GND": [["U1", "3"], ["U1", "4"], ["U2", "3"], ["U2", "4"]],
            "VCC": [["U1", "8"], ["U2", "8"]],
        },
        "no_connect": [["U1", "5"], ["U1", "6"], ["U1", "7"],
                       ["U2", "5"], ["U2", "6"], ["U2", "7"]],
    }


def test_pin_check_clean_design() -> None:
    rep = vc.pin_check(amp_design(), symbols_dir=FIXTURE_SYMBOLS)
    assert rep.ok, rep.as_dict()
    assert [p["ref"] for p in rep.parts] == ["U1", "U2", "R1"]          # H1 is board-only
    pins = rep.symbols["TestLib:BaseAmp"]
    assert {"number": "8", "name": "V+", "type": "power_in"} in pins
    assert [p["number"] for p in pins] == ["1", "2", "3", "4", "5", "6", "7", "8"]
    assert rep.symbols["TestLib:DerivedAmp"] == pins                    # `extends` resolved
    assert rep.symbols["TestLib:PosFileR"][0] == {"number": "1", "name": "~",
                                                  "type": "passive"}


def test_pin_check_unknown_and_unlisted_pins() -> None:
    d = amp_design()
    d["nets"]["VCC"].append(["U1", "9"])          # no pin 9 on the symbol
    d["no_connect"].remove(["U1", "7"])           # pin 7 read nowhere
    rep = vc.pin_check(d, symbols_dir=FIXTURE_SYMBOLS)
    assert not rep.ok
    (u1,) = [p for p in rep.parts if p["ref"] == "U1"]
    assert u1["unknown"] == ["9"]
    assert u1["unlisted"] == [{"number": "7", "name": "~", "type": "output"}]
    assert rep.as_dict()["issues"] == 2
    assert "U1.9" in rep.summary() and "U1.7" in rep.summary()


def test_pin_check_unresolved_symbols() -> None:
    d = amp_design()
    d["components"] += [{"ref": "U9", "lib_id": "TestLib:Nope", "value": "?", "footprint": "X:Y"},
                        {"ref": "U8", "lib_id": "NoSuchLib:R", "value": "?", "footprint": "X:Y"},
                        {"ref": "U7", "lib_id": "NotALibId", "value": "?", "footprint": "X:Y"}]
    rep = vc.pin_check(d, symbols_dir=FIXTURE_SYMBOLS)
    assert not rep.ok
    reasons = {u["ref"]: u["reason"] for u in rep.unresolved}
    assert set(reasons) == {"U9", "U8", "U7"}
    assert "Nope" in reasons["U9"] and "NoSuchLib" in reasons["U8"]
    assert "Library:Symbol" in reasons["U7"]


def test_pin_check_project_library_from_the_libraries_block(tmp_path: Path) -> None:
    libs = tmp_path / "libs"
    libs.mkdir()
    shutil.copy(FIXTURE_SYMBOLS / "TestLib.kicad_sym", libs / "my_parts.kicad_sym")
    d = amp_design()
    d["libraries"] = {"symbols": {"MyParts": "libs/my_parts.kicad_sym"}}
    for c in d["components"]:
        if c.get("lib_id", "").startswith("TestLib:"):
            c["lib_id"] = c["lib_id"].replace("TestLib:", "MyParts:")
    rep = vc.pin_check(d, netlist_dir=tmp_path, symbols_dir=None)
    assert rep.ok, rep.as_dict()
    assert "MyParts:BaseAmp" in rep.symbols


def test_cli_pins(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    good = write(tmp_path / "good.json", amp_design())
    assert vc.main(["pins", str(good), "--symbols-dir", str(FIXTURE_SYMBOLS)]) == 0
    assert "ok" in capsys.readouterr().out
    d = amp_design()
    d["no_connect"].remove(["U2", "6"])
    bad = write(tmp_path / "bad.json", d)
    assert vc.main(["pins", str(bad), "--symbols-dir", str(FIXTURE_SYMBOLS), "--json"]) == 1
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is False and data["issues"] == 1
    assert vc.main(["pins", str(bad), "--symbols-dir", str(tmp_path / "none")]) == 2


@requires_symbols
def test_live_pin_check_against_stock_symbols() -> None:
    d = design()
    d["components"] = [c for c in d["components"] if c["lib_id"].startswith("Device:")]
    d["nets"] = {"A": [["C1", "1"], ["C2", "1"], ["R1", "1"], ["D1", "2"]],
                 "B": [["C1", "2"], ["C2", "2"], ["R1", "2"], ["D1", "1"]]}
    rep = vc.pin_check(d, symbols_dir=KICAD.symbols)
    assert rep.ok, rep.as_dict()
    led = {p["number"]: p["name"] for p in rep.symbols["Device:LED"]}
    assert led == {"1": "K", "2": "A"}                     # Device:LED pin 1 = cathode
