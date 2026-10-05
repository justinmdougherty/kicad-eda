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


def test_empty_reading_against_a_full_one_scores_zero() -> None:
    """Review regression 3: with parts on a side but none shared, no field
    agrees (wave 1 scored the uncompared fields 1.0, i.e. 0.25 overall)."""
    empty = {"components": []}
    for a, b in ((design(), empty), (empty, design())):
        res = vc.crosscheck(a, b)
        assert dict(res.scores) == {"components": 0.0, "fields": 0.0, "connectivity": 0.0}
        assert res.score == 0.0 and not res.agree
        assert res.counts["fields_compared"] == 0


def test_two_empty_readings_agree_fully() -> None:
    res = vc.crosscheck({"components": []}, {"components": []})
    assert res.agree and res.score == 1.0 and res.scores["fields"] == 1.0


def test_no_shared_parts_scores_no_field_agreement() -> None:
    a = {"components": [{"ref": "R1", "value": "1k"}], "nets": {}}
    b = {"components": [{"ref": "R2", "value": "1k"}], "nets": {}}
    res = vc.crosscheck(a, b)
    assert res.scores["components"] == 0.0 and res.scores["fields"] == 0.0


def test_shared_parts_without_any_field_still_agree() -> None:
    """Identical readings score 1.0 even when no part carries a field."""
    bare = {"components": [{"ref": "R1"}, {"ref": "R2"}],
            "nets": {"N": [["R1", "1"], ["R2", "1"]]}}
    res = vc.crosscheck(bare, copy.deepcopy(bare))
    assert res.agree and res.score == 1.0 and res.scores["fields"] == 1.0


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


@pytest.mark.parametrize("a, b, same", [
    # review regression 4: a unit letter is an RKM decimal mark like R
    ("3.3V", "3V3", True), ("3V3", "3.3", True), ("2A5", "2.5A", True), ("1W5", "1.5W", True),
    ("4F7", "4.7F", True), ("2H2", "2.2H", True), ("5V0", "5V", True),
    ("10uF 6V3", "10uF/6.3V", True),
    ("3V3", "3.3A", False), ("3V3", "33V", False), ("2A5", "25A", False), ("3V3", "5V", False),
    ("1M", "1m", False), ("1M", "1mV", False),
])
def test_values_agree_unit_letter_as_decimal_mark(a: str, b: str, same: bool) -> None:
    assert vc.values_agree(a, b) is same and vc.values_agree(b, a) is same


@pytest.mark.parametrize("text, expected", [
    ("4k7", (4700.0, "")), ("100nF", (1e-7, "F")), ("2R2", (2.2, "ohm")), ("10kΩ", (1e4, "ohm")),
    ("1.5 mH", (1.5e-3, "H")), ("12V", (12.0, "V")), ("LM358", None), ("", None),
    ("3V3", (3.3, "V")), ("2A5", (2.5, "A")), ("1W5", (1.5, "W")), ("4F7", (4.7, "F")),
    ("2H2", (2.2, "H")), ("2N3904", None), ("74HC14", None),
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


def _with_and_without_j2() -> tuple[dict[str, Any], dict[str, Any]]:
    """A = {R1, J2} with J2.3 marked no-connect; B = {R1} (no J2 at all)."""
    r1 = {"ref": "R1", "lib_id": "Device:R", "value": "10k", "footprint": "R:R_0603"}
    j2 = {"ref": "J2", "lib_id": "Connector_Generic:Conn_01x03", "value": "DBG",
          "footprint": "Connector:Conn_01x03"}
    a = {"components": [dict(r1), j2], "nets": {"N1": [["R1", "1"], ["J2", "1"]],
                                                 "N2": [["R1", "2"], ["J2", "2"]]},
         "no_connect": [["J2", "3"]]}
    b = {"components": [dict(r1)], "nets": {"N1": [["R1", "1"]], "N2": [["R1", "2"]]},
         "no_connect": []}
    return a, b


def test_no_connect_on_a_part_the_other_reading_lacks_is_not_repeated() -> None:
    """Review regression 1 (wave 1 swapped gone_a / gone_b): a no-connect pin
    of a part only one reading has is covered by its component row."""
    a, b = _with_and_without_j2()
    res = vc.crosscheck(a, b)
    assert kinds(res) == ["component_missing"], res.markdown()
    assert res.disagreements[0].subject == "J2"
    reverse = vc.crosscheck(b, a)
    assert kinds(reverse) == ["component_extra"], reverse.markdown()
    assert reverse.disagreements[0].subject == "J2"


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
    broken = tmp_path / "broken.json"
    broken.write_text("{nope", encoding="utf-8")
    assert vc.main(["compare", str(a), str(broken)]) == 2
    assert "not valid JSON" in capsys.readouterr().err
    assert vc.main(["compare", str(a), str(a), "--ref-map", "[1]"]) == 2
    assert "--ref-map" in capsys.readouterr().err


# ----------------------------------------------------------------------------
# File-level helpers (what the MCP layer wraps, F46-13)
# ----------------------------------------------------------------------------
def renamed_d1() -> dict[str, Any]:
    d = design()
    comp(d, "D1")["ref"] = "LED1"
    for pads in d["nets"].values():
        for pad in pads:
            if pad[0] == "D1":
                pad[0] = "LED1"
    return d


def test_crosscheck_files_is_crosscheck_on_the_loaded_files(tmp_path: Path) -> None:
    b_design = design()
    comp(b_design, "R1")["value"] = "47k"
    a, b = write(tmp_path / "a.json", design()), write(tmp_path / "b.json", b_design)
    res = vc.crosscheck_files(a, b, labels=("r1", "r2"))
    assert isinstance(res, vc.CrosscheckResult)
    assert res.as_dict() == vc.crosscheck(design(), b_design, labels=("r1", "r2")).as_dict()
    assert res.markdown().startswith("# Vision cross-check: r1 vs r2")
    assert vc.crosscheck_files(str(a), str(a), ref_map={}, labels=["A", "B"]).agree


@pytest.mark.parametrize("form", ["mapping", "inline", "file", "path"])
def test_crosscheck_files_ref_map_forms(tmp_path: Path, form: str) -> None:
    a, b = write(tmp_path / "a.json", design()), write(tmp_path / "b.json", renamed_d1())
    map_file = write(tmp_path / "map.json", {"D1": "LED1"})
    ref_map = {"mapping": {"D1": "LED1"}, "inline": ' {"D1": "LED1"}', "file": str(map_file),
               "path": map_file}[form]
    assert not vc.crosscheck_files(a, b).agree
    assert vc.crosscheck_files(a, b, ref_map=ref_map).agree


def test_crosscheck_files_errors(tmp_path: Path) -> None:
    a = write(tmp_path / "a.json", design())
    with pytest.raises(FileNotFoundError):
        vc.crosscheck_files(a, tmp_path / "missing.json")
    with pytest.raises(ValueError, match="ref-map|ref_map"):
        vc.crosscheck_files(a, a, ref_map="[1, 2]")
    with pytest.raises(ValueError, match="not valid JSON"):
        vc.crosscheck_files(a, a, ref_map="{nope")
    assert vc.load_ref_map("  ") is None and vc.crosscheck_files(a, a, ref_map="").agree
    with pytest.raises(TypeError, match="ref_map"):
        vc.crosscheck_files(a, a, ref_map=5)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="labels"):
        vc.crosscheck_files(a, a, labels=("same", "same"))


def test_pin_check_file_anchors_project_libraries_at_the_netlist(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = tmp_path / "proj"
    (project / "libs").mkdir(parents=True)
    shutil.copy(FIXTURE_SYMBOLS / "TestLib.kicad_sym", project / "libs" / "my_parts.kicad_sym")
    d = amp_design()
    d["libraries"] = {"symbols": {"MyParts": "libs/my_parts.kicad_sym"}}
    for c in d["components"]:
        if c.get("lib_id", "").startswith("TestLib:"):
            c["lib_id"] = c["lib_id"].replace("TestLib:", "MyParts:")
    netlist = write(project / "netlist.json", d)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)                       # relative paths follow the netlist
    out = vc.pin_check_file(str(netlist))
    assert json.loads(json.dumps(out)) == out
    assert out["ok"] is True and out["issues"] == 0, out
    assert out["netlist"] == str(netlist.resolve()) and out["summary"].startswith("pins ok")
    assert "MyParts:BaseAmp" in out["symbols"]


def test_pin_check_file_reports_issues(tmp_path: Path) -> None:
    d = amp_design()
    d["no_connect"].remove(["U2", "6"])
    out = vc.pin_check_file(write(tmp_path / "n.json", d), symbols_dir=FIXTURE_SYMBOLS)
    assert out["ok"] is False and out["issues"] == 1 and "U2.6" in out["summary"]
    with pytest.raises(FileNotFoundError):
        vc.pin_check_file(tmp_path / "missing.json", symbols_dir=FIXTURE_SYMBOLS)


# ----------------------------------------------------------------------------
# Edge paths
# ----------------------------------------------------------------------------
def test_summary_of_a_disagreement() -> None:
    b = design()
    comp(b, "R1")["value"] = "47k"
    text = vc.crosscheck(design(), b).summary()
    assert text.startswith("REVIEW: 1 disagreement(s)") and "1 high" in text


def test_design_that_is_not_an_object_is_named_by_its_label() -> None:
    with pytest.raises(ValueError, match="design first"):
        vc.crosscheck([], design(), labels=("first", "second"))  # type: ignore[arg-type]


def test_stock_symbols_are_looked_up_only_for_undeclared_libraries(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import kicad_env
    monkeypatch.setattr(kicad_env, "pick_cli", lambda: None)
    assert vc._stock_symbols_dir() is None              # no KiCad: no stock library
    d = {"components": [{"ref": "R1", "lib_id": "TestLib:PosFileR"}],
         "nets": {"N": [["R1", "1"], ["R1", "2"]]}}
    rep = vc.pin_check(d, netlist_dir=tmp_path, symbols_dir=None)
    assert [u["ref"] for u in rep.unresolved] == ["R1"]
    monkeypatch.setattr(vc, "_stock_symbols_dir", lambda: FIXTURE_SYMBOLS)
    rep = vc.pin_check(d, netlist_dir=tmp_path, symbols_dir=None)
    assert rep.ok, rep.as_dict()


def test_module_doc_records_the_open_conventions() -> None:
    """The conventions D4 does not fix yet (the reader prompt is checked in
    test_rasterize.py)."""
    doc = vc.__doc__ or ""
    for text in ("0.25 components", "0.25 fields", "0.5 connectivity", "VIS-<n>",
                 "'schematic-reader (Claude vision) + pymupdf <ver> @ <dpi> dpi'",
                 "\"rebuild\": \"reflow\"", "lib_id / footprint med", "no-connect low"):
        assert text in doc, text
    assert dict(vc.WEIGHTS) == {"components": 0.25, "fields": 0.25, "connectivity": 0.5}


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


NC_LIB = """(kicad_symbol_lib
  (version 20241209)
  (generator "kicad-eda-tests")
  (symbol "LDO_NC"
    (in_bom yes)
    (on_board yes)
    (property "Reference" "U" (at 0 5.08 0) (effects (font (size 1.27 1.27))))
    (property "Value" "LDO_NC" (at 0 -5.08 0) (effects (font (size 1.27 1.27))))
    (symbol "LDO_NC_1_1"
      (pin power_in line (at -7.62 2.54 0) (length 2.54) (name "VIN" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))
      (pin power_in line (at 0 -7.62 90) (length 2.54) (name "GND" (effects (font (size 1.27 1.27)))) (number "2" (effects (font (size 1.27 1.27)))))
      (pin input line (at -7.62 0 0) (length 2.54) (name "EN" (effects (font (size 1.27 1.27)))) (number "3" (effects (font (size 1.27 1.27)))))
      (pin no_connect line (at 7.62 0 180) (length 2.54) (name "NC" (effects (font (size 1.27 1.27)))) (number "4" (effects (font (size 1.27 1.27)))))
      (pin power_out line (at 7.62 2.54 180) (length 2.54) (name "VOUT" (effects (font (size 1.27 1.27)))) (number "5" (effects (font (size 1.27 1.27)))))))
)
"""  # noqa: E501


def nc_design() -> dict[str, Any]:
    """An LDO whose symbol pin 4 has electrical type no_connect (like the stock
    Regulator_Linear:AP2112K-3.3 'NC'); the reading never mentions pin 4."""
    return {"components": [{"ref": "U1", "lib_id": "NcLib:LDO_NC", "value": "LDO",
                            "footprint": "X:Y"}],
            "nets": {"VIN": [["U1", "1"], ["U1", "3"]], "GND": [["U1", "2"]],
                     "VOUT": [["U1", "5"]]},
            "no_connect": []}


@pytest.fixture
def nc_symbols(tmp_path: Path) -> Path:
    folder = tmp_path / "nc_symbols"
    folder.mkdir()
    (folder / "NcLib.kicad_sym").write_text(NC_LIB, encoding="utf-8")
    return folder


def test_pin_check_nc_type_pin_is_informational(nc_symbols: Path) -> None:
    """Review regression 2: a symbol pin of electrical type no_connect is not
    an unread pin (wave 1 counted it as 'unlisted')."""
    rep = vc.pin_check(nc_design(), symbols_dir=nc_symbols)
    assert rep.ok and rep.issues == 0, rep.as_dict()
    (u1,) = rep.parts
    assert u1["unlisted"] == [] and u1["unknown"] == []
    assert u1["nc_pins"] == [{"number": "4", "name": "NC", "type": "no_connect"}]
    assert rep.as_dict()["ok"] is True
    assert "U1.4" not in rep.summary() and "1 NC-type pin" in rep.summary()


def test_pin_check_nc_type_pin_named_by_the_reading(nc_symbols: Path) -> None:
    d = nc_design()
    d["no_connect"] = [["U1", "4"]]
    (u1,) = vc.pin_check(d, symbols_dir=nc_symbols).parts
    assert u1["nc_pins"] == [] and u1["unlisted"] == []


def test_pin_check_other_unread_pins_still_count(nc_symbols: Path) -> None:
    d = nc_design()
    d["nets"]["VIN"] = [["U1", "1"]]              # EN (input) read nowhere
    rep = vc.pin_check(d, symbols_dir=nc_symbols)
    (u1,) = rep.parts
    assert [p["number"] for p in u1["unlisted"]] == ["3"]
    assert [p["number"] for p in u1["nc_pins"]] == ["4"]
    assert rep.issues == 1 and "U1.3" in rep.summary() and "U1.4" not in rep.summary()


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


@requires_symbols
def test_live_stock_nc_pin_is_not_an_issue() -> None:
    """The shipped examples' AP2112K-3.3: pin 4 'NC' is of type no_connect."""
    d = {"components": [{"ref": "U1", "lib_id": "Regulator_Linear:AP2112K-3.3",
                         "value": "AP2112K-3.3", "footprint": "Package_TO_SOT_SMD:SOT-23-5"}],
         "nets": {"VIN": [["U1", "1"], ["U1", "3"]], "GND": [["U1", "2"]],
                  "+3V3": [["U1", "5"]]}}
    rep = vc.pin_check(d, symbols_dir=KICAD.symbols)
    assert rep.ok, rep.as_dict()
    (u1,) = rep.parts
    assert [(p["number"], p["type"]) for p in u1["nc_pins"]] == [("4", "no_connect")]
