"""sch_build — netlist.json -> .kicad_sch, exercised through the subprocess
contract the MCP server and pipeline use (JSON on stdout, exit code).

Real-library runs use examples/lm358_buffer with an EMPTY sidecar dir (C7: pin
offsets derived from the symbol itself) and skip without a KiCad install; the
synthetic-library runs (tests/fixtures/symbols) never skip.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from fixtures.kicad_install import KICAD, requires_kicad_cli

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent
SCH_BUILD = ROOT / "server" / "tools" / "sch_build.py"
FIXTURE_SYMBOLS = TESTS / "fixtures" / "symbols"
LM358_NETLIST = ROOT / "examples" / "lm358_buffer" / "netlist.json"
_VENV_PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
PYTHON = _VENV_PYTHON if _VENV_PYTHON.is_file() else Path(sys.executable)
PWR_FLAG = "power:PWR_FLAG"

# one placement block per (component, unit); lib_symbols entries are one-line
# and indented two tabs, so they never match these
PLACEMENT_RE = re.compile(r"^\t\(symbol$", re.M)
LIB_ID_RE = re.compile(r'^\t\t\(lib_id "([^"]+)"\)$', re.M)
UNIT_RE = re.compile(r"^\t\t\(unit (\d+)\)$", re.M)
EMBEDDED_RE = re.compile(r'^\t\t\(symbol "([^"]+)"', re.M)
LABEL_RE = re.compile(r'^\t\(label "([^"]+)" \(at (-?[\d.]+) (-?[\d.]+) 0\)(.*)$', re.M)
NO_CONNECT_RE = re.compile(r"^\t\(no_connect ", re.M)


@dataclass(frozen=True)
class Build:
    proc: subprocess.CompletedProcess
    result: dict | None
    path: Path
    text: str          # "" when nothing was written


def run_sch_build(netlist: Path, out: Path, symbols_dir: Path,
                  sidecars_dir: Path | None = None) -> Build:
    argv = [str(PYTHON), str(SCH_BUILD), str(netlist), str(out), str(symbols_dir)]
    if sidecars_dir is not None:
        argv.append(str(sidecars_dir))
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=120)
    lines = proc.stdout.strip().splitlines()
    try:
        result = json.loads(lines[-1]) if lines else None
    except ValueError:
        result = None
    text = out.read_text(encoding="utf-8") if out.is_file() else ""
    return Build(proc=proc, result=result, path=out, text=text)


def _placements(text: str) -> list[tuple[str, int]]:
    """(lib_id, unit) of every placed symbol, in file order."""
    lib_ids, units = LIB_ID_RE.findall(text), [int(u) for u in UNIT_RE.findall(text)]
    assert len(lib_ids) == len(units) == len(PLACEMENT_RE.findall(text))
    return list(zip(lib_ids, units))


def _labels(text: str) -> list[tuple[str, float, float, bool]]:
    """(net, x, y, is_pin_label); PWR_FLAG labels carry no justify clause."""
    return [(m.group(1), float(m.group(2)), float(m.group(3)), "justify" in m.group(4))
            for m in LABEL_RE.finditer(text)]


def _write_netlist(path: Path, design: dict) -> Path:
    path.write_text(json.dumps(design), encoding="utf-8")
    return path


def _lm358() -> dict:
    return json.loads(LM358_NETLIST.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def lm358_build(tmp_path_factory: pytest.TempPathFactory) -> Build:
    """examples/lm358_buffer built against the real libraries, no sidecars."""
    if KICAD is None:
        pytest.skip("KiCad symbol libraries not installed")
    work = tmp_path_factory.mktemp("lm358")
    empty_sidecars = work / "sidecars"
    empty_sidecars.mkdir()
    return run_sch_build(LM358_NETLIST, work / "lm358_buffer.kicad_sch",
                         KICAD.symbols, empty_sidecars)


@pytest.fixture
def lm358_work(tmp_path: Path) -> Path:
    if KICAD is None:
        pytest.skip("KiCad symbol libraries not installed")
    (tmp_path / "sidecars").mkdir()
    return tmp_path


# --- real library, empty sidecar dir -----------------------------------------

def test_exit_zero_and_ok_json(lm358_build: Build):
    assert lm358_build.proc.returncode == 0, lm358_build.proc.stderr
    assert lm358_build.result == {"ok": True, "sch": str(lm358_build.path),
                                  "components": 4, "nets": 6, "warnings": []}
    assert lm358_build.path.is_file()


def test_file_is_kicad10_format_with_required_closers(lm358_build: Build):
    text = lm358_build.text
    assert text.startswith("(kicad_sch\n\t(version 20250114)\n")
    assert '\t(generator "kicad-eda")' in text
    assert "\t(sheet_instances\n\t\t(path \"/\" (page \"1\"))\n\t)" in text
    assert text.rstrip().endswith("\t(embedded_fonts no)\n)")


def test_one_placement_symbol_per_unit_cell(lm358_build: Build):
    placed = _placements(lm358_build.text)
    parts = [p for p in placed if p[0] != PWR_FLAG]
    # U1 is a dual op-amp (2 amp units + power unit); the other 3 parts are single-unit
    assert sorted(parts) == [("Amplifier_Operational:LM358", 1), ("Amplifier_Operational:LM358", 2),
                             ("Amplifier_Operational:LM358", 3), ("Connector_Generic:Conn_01x06", 1),
                             ("Device:C", 1), ("Device:C", 1)]
    # P5V and GND each land on a power_in pin with no driver -> one PWR_FLAG each
    assert placed.count((PWR_FLAG, 1)) == 2


def test_each_used_symbol_embedded_once(lm358_build: Build):
    embedded = EMBEDDED_RE.findall(lm358_build.text)
    assert sorted(embedded) == ["Amplifier_Operational:LM358", "Connector_Generic:Conn_01x06",
                                "Device:C", PWR_FLAG]
    assert "(extends" not in lm358_build.text
    assert "in_pos_files" not in lm358_build.text


def test_label_per_net_pin_and_flag_per_undriven_power_net(lm358_build: Build):
    design = _lm358()
    labels = _labels(lm358_build.text)
    for net, conns in design["nets"].items():
        assert sum(1 for n, _x, _y, pin in labels if pin and n == net) == len(conns), net
    assert sorted(n for n, _x, _y, pin in labels if not pin) == ["GND", "P5V"]
    assert len(NO_CONNECT_RE.findall(lm358_build.text)) == len(design["no_connect"]) == 0


def test_no_connect_count_matches_netlist(lm358_work: Path):
    design = _lm358()
    design["no_connect"] = design["nets"].pop("OUT_B")           # 3 pins -> flags
    build = run_sch_build(_write_netlist(lm358_work / "nc.json", design),
                          lm358_work / "nc.kicad_sch", KICAD.symbols, lm358_work / "sidecars")
    assert build.result and build.result["ok"] is True
    assert len(NO_CONNECT_RE.findall(build.text)) == 3
    assert not any(n == "OUT_B" for n, *_ in _labels(build.text))


def test_bogus_pad_exits_1_lists_missing_pins_and_writes_nothing(lm358_work: Path):
    design = _lm358()
    design["nets"]["P5V"].append(["U1", "25"])                     # no such pin
    design["nets"]["GND"].append(["ZZ9", "1"])                     # no such ref
    design["no_connect"] = [["C1", "3"]]                           # no such pin
    out = lm358_work / "bogus.kicad_sch"
    build = run_sch_build(_write_netlist(lm358_work / "bogus.json", design), out,
                          KICAD.symbols, lm358_work / "sidecars")
    assert build.proc.returncode == 1
    assert build.result["ok"] is False
    assert build.result["error"] == "3 pins have no coordinates"
    assert build.result["missing_pins"] == [["U1", "25"], ["ZZ9", "1"], ["C1", "3"]]
    assert not out.exists()


def test_sidecar_overrides_derived_offset(lm358_work: Path):
    base = run_sch_build(LM358_NETLIST, lm358_work / "base.kicad_sch", KICAD.symbols,
                         lm358_work / "sidecars")
    override_dir = lm358_work / "override"
    override_dir.mkdir()
    # Device:C pin 1 derives to y=-3.81; move it to y=-12.7 (dy = -8.89)
    (override_dir / "Device_C.pins.json").write_text(
        json.dumps({"1": {"x": 0.0, "y": -12.7}}), encoding="utf-8")
    over = run_sch_build(LM358_NETLIST, lm358_work / "over.kicad_sch", KICAD.symbols, override_dir)
    assert over.result["ok"] is True and over.result["warnings"] == []
    before = sorted(set(_labels(base.text)) - set(_labels(over.text)))
    after = sorted(set(_labels(over.text)) - set(_labels(base.text)))
    assert len(before) == len(after) == 2                          # C1.1 and C2.1, both P5V
    for (net_b, x_b, y_b, _), (net_a, x_a, y_a, _) in zip(before, after):
        assert net_b == net_a == "P5V" and x_a == x_b
        assert y_a - y_b == pytest.approx(-8.89)


def test_sidecar_key_unknown_to_symbol_is_used_but_warned(lm358_work: Path):
    design = _lm358()
    design["nets"]["GND"].append(["C1", "3"])                      # Device:C has pins 1, 2
    override_dir = lm358_work / "override"
    override_dir.mkdir()
    (override_dir / "Device_C.pins.json").write_text(
        json.dumps({"3": {"x": 2.54, "y": 0.0}}), encoding="utf-8")
    build = run_sch_build(_write_netlist(lm358_work / "extra.json", design),
                          lm358_work / "extra.kicad_sch", KICAD.symbols, override_dir)
    assert build.proc.returncode == 0 and build.result["ok"] is True
    assert len(build.result["warnings"]) == 1
    assert build.result["warnings"][0].startswith("C1.3: not a pin of Device:C")
    assert sum(1 for n, *_ in _labels(build.text) if n == "GND") == 5 + 1   # 4 pins + C1.3 + flag


def test_malformed_sidecar_is_a_json_error(lm358_work: Path):
    bad_dir = lm358_work / "bad"
    bad_dir.mkdir()
    (bad_dir / "Device_C.pins.json").write_text("{not json", encoding="utf-8")
    build = run_sch_build(LM358_NETLIST, lm358_work / "bad.kicad_sch", KICAD.symbols, bad_dir)
    assert build.proc.returncode == 1
    assert build.result["ok"] is False
    assert build.result["error"].startswith("ValueError: malformed pin sidecar")
    assert build.text == ""


@requires_kicad_cli
def test_real_library_output_passes_erc(lm358_build: Build, tmp_path: Path):
    report = tmp_path / "lm358_erc.json"
    proc = subprocess.run([str(KICAD.cli), "sch", "erc", str(lm358_build.path),
                           "--format", "json", "--severity-error", "-o", str(report)],
                          capture_output=True, text=True, timeout=180)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    erc = json.loads(report.read_text(encoding="utf-8"))
    violations = [v for sheet in erc["sheets"] for v in sheet["violations"]]
    assert violations == [], json.dumps(violations, indent=1)
    assert erc["source"].endswith(lm358_build.path.name)


# --- synthetic library (no KiCad needed) -------------------------------------

def _synthetic_design() -> dict:
    return {"name": "synthetic",
            "components": [
                {"ref": "U1", "lib_id": "TestLib:DerivedAmp", "value": "amp", "footprint": "F:SO8"},
                {"ref": "R1", "lib_id": "TestLib:PosFileR", "value": "10k", "footprint": "F:R"}],
            "nets": {"VCC": [["U1", "8"], ["R1", "1"]],
                     "GND": [["U1", "V-"], ["R1", "2"]],          # pin NAME key
                     "LOOP": [["U1", "1"], ["U1", "2"]]},
            "no_connect": [["U1", "3"], ["U1", "5"], ["U1", "6"], ["U1", "7"]]}


def test_synthetic_library_build(tmp_path: Path):
    out = tmp_path / "synthetic.kicad_sch"
    build = run_sch_build(_write_netlist(tmp_path / "synthetic.json", _synthetic_design()),
                          out, FIXTURE_SYMBOLS)                    # sidecars arg omitted
    assert build.proc.returncode == 0, build.proc.stderr
    assert build.result == {"ok": True, "sch": str(out), "components": 2, "nets": 3, "warnings": []}
    placed = _placements(build.text)
    assert sorted(p for p in placed if p[0] != PWR_FLAG) == [
        ("TestLib:DerivedAmp", 1), ("TestLib:DerivedAmp", 2), ("TestLib:DerivedAmp", 3),
        ("TestLib:PosFileR", 1)]
    assert placed.count((PWR_FLAG, 1)) == 2                        # VCC, GND (power_in, undriven)
    labels = _labels(build.text)
    assert sum(1 for *_, pin in labels if pin) == 6
    assert len(NO_CONNECT_RE.findall(build.text)) == 4
    assert "(extends" not in build.text and "in_pos_files" not in build.text
    assert '\t\t(pin "8" (uuid "' in build.text                    # inherited pin instantiated


def test_synthetic_pin_name_key_lands_in_its_own_unit(tmp_path: Path):
    """"V-" lives in unit 3: its label must sit at unit 3's cell, not unit 1's."""
    build = run_sch_build(_write_netlist(tmp_path / "s.json", _synthetic_design()),
                          tmp_path / "s.kicad_sch", FIXTURE_SYMBOLS)
    gnd = [(x, y) for n, x, y, pin in _labels(build.text) if pin and n == "GND"]
    # cells: U1 units 1..3 at x = 50.8, 114.3, 177.8; R1 at 241.3 (y = 38.1);
    # "V-" offset (-2.54, +7.62) on unit 3, R1 pin 2 offset (0, +3.81)
    assert sorted(gnd) == [(175.26, 45.72), (241.3, 41.91)]


def test_missing_symbol_library_dir_is_a_json_error(tmp_path: Path):
    out = tmp_path / "x.kicad_sch"
    build = run_sch_build(_write_netlist(tmp_path / "s.json", _synthetic_design()),
                          out, tmp_path / "no_symbols_here")
    assert build.proc.returncode == 1
    assert build.result["ok"] is False
    assert build.result["error"].startswith("SymbolLibError: symbol library not found")
    assert not out.exists()


def test_usage_exit_without_arguments():
    proc = subprocess.run([str(PYTHON), str(SCH_BUILD)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 2
    assert "Usage" in proc.stdout


# --- in-process API (lint_netlist / design_report / older callers) -----------

def test_public_names_are_reexported_from_symlib():
    import sch_build
    import symlib
    for name in ("load_lib", "flatten", "embed", "pin_numbers", "pin_etypes", "unit_pins", "dumps"):
        assert getattr(sch_build, name) is getattr(symlib, name), name
        assert name in sch_build.__all__


def test_build_in_process_has_the_subprocess_contract(tmp_path: Path):
    import sch_build
    netlist = _write_netlist(tmp_path / "s.json", _synthetic_design())
    out = tmp_path / "s.kicad_sch"
    assert sch_build.build(str(netlist), str(out), str(FIXTURE_SYMBOLS)) == {
        "ok": True, "sch": str(out), "components": 2, "nets": 3, "warnings": []}
    assert out.is_file()
    design = {**_synthetic_design(), "no_connect": [["R1", "9"]]}
    missing = sch_build.build(str(_write_netlist(tmp_path / "m.json", design)),
                              str(tmp_path / "m.kicad_sch"), str(FIXTURE_SYMBOLS))
    assert missing == {"ok": False, "error": "1 pins have no coordinates", "missing_pins": [["R1", "9"]]}
    assert not (tmp_path / "m.kicad_sch").exists()
