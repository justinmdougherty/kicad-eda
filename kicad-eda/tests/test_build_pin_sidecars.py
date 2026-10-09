"""build_pin_sidecars — pin-coordinate sidecars from a symbol source (a
directory, a libtable.LibraryResolver or a library map; F2-10, ROADMAP D1),
and the CLI's --netlist mode, which resolves the netlist's project libraries
and writes their sidecars to <netlist dir>/pins (never the central server/pins).

Pure Python (sym_author writes the LG290P, TestLib is the stock fixture
library): nothing here needs KiCad.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import build_pin_sidecars as bps
import libtable
import sym_author

TESTS = Path(__file__).resolve().parent
FIXTURE_SYMBOLS = TESTS / "fixtures" / "symbols"
SCRIPT = TESTS.parent / "server" / "tools" / "build_pin_sidecars.py"
NICK = "SPS_parts"
LIB_ID = f"{NICK}:LG290P"
STOCK_ID = "TestLib:PosFileR"
LG290P_PADS = {str(n) for n in range(1, 80)}
CHILD_TIMEOUT_S = 120


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """libs/SPS_parts.kicad_sym (LG290P) declared in netlist.json's libraries
    block; R1 comes from the stock TestLib."""
    lib = tmp_path / "libs" / f"{NICK}.kicad_sym"
    lib.parent.mkdir()
    sym_author.author_symbol(sym_author.lg290p_spec(), lib)
    _write_netlist(tmp_path, _design(NICK))
    return tmp_path


def _design(nick: str, block: bool = True) -> dict:
    design = {"name": "gnss", "components": [
        {"ref": "U1", "lib_id": f"{nick}:LG290P", "value": "LG290P", "footprint": "SPS:LG290P"},
        {"ref": "R1", "lib_id": STOCK_ID, "value": "10k", "footprint": "R"},
        {"ref": "U2", "lib_id": f"{nick}:LG290P", "value": "LG290P", "footprint": "SPS:LG290P"}],
        "nets": {"GND": [["U1", "10"], ["R1", "2"]], "VCC": [["U1", "23"], ["R1", "1"]]}}
    if block:
        design["libraries"] = {"symbols": {nick: f"libs/{NICK}.kicad_sym"}}
    return design


def _write_netlist(project: Path, design: dict) -> Path:
    path = project / "netlist.json"
    path.write_text(json.dumps(design), encoding="utf-8")
    return path


def _resolver(project: Path, design: dict) -> libtable.LibraryResolver:
    return libtable.LibraryResolver.for_design(project, design, FIXTURE_SYMBOLS, None)


def _pad_keys(sidecar: dict) -> set[str]:
    return {k for k in sidecar if k.isdigit()}


def _last_json(text: str) -> dict:
    return json.loads(text.strip().splitlines()[-1])


# --- sidecar_map / build_all take a SymbolSource ---------------------------------------

def test_sidecar_map_keeps_the_directory_form_sym_author_uses(project: Path):
    """sym_author.write_pin_sidecar: a pre-filled cache plus the library's dir."""
    import symlib
    libs = project / "libs"
    cache = {NICK: symlib.load_lib(libs, NICK)}
    pins = bps.sidecar_map(cache, libs, LIB_ID)
    assert _pad_keys(pins) == LG290P_PADS
    assert pins["GND"] == pins["10"] and pins["RF_IN"] == pins["11"]
    assert set(pins["10"]) == {"x", "y"}
    assert bps.sidecar_map({}, str(libs), LIB_ID) == pins


def test_sidecar_map_resolves_a_nickname_through_a_resolver_or_a_map(project: Path):
    design = _design("GNSS")                           # libs/SPS_parts.kicad_sym as GNSS
    resolver = _resolver(project, design)
    by_resolver = bps.sidecar_map({}, resolver, "GNSS:LG290P")
    assert _pad_keys(by_resolver) == LG290P_PADS
    assert bps.sidecar_map({}, resolver.library_map(), "GNSS:LG290P") == by_resolver
    with pytest.raises(bps.SymbolLibError, match="symbol library not found"):
        bps.sidecar_map({}, project / "libs", "GNSS:LG290P")     # dir: naming convention


def test_the_three_sources_write_byte_identical_sidecars(project: Path):
    resolver = _resolver(project, _design(NICK))
    outs = []
    for i, source in enumerate((project / "libs", resolver, resolver.library_map())):
        out = project / f"out{i}"
        out.mkdir()
        assert bps.build_all(source, out, [LIB_ID])["ok"]
        outs.append((out / f"{NICK}_LG290P.pins.json").read_bytes())
    assert outs[0] == outs[1] == outs[2]


def test_build_all_reports_each_failure_and_keeps_going(project: Path):
    out = project / "pins"
    out.mkdir()
    summary = bps.build_all(_resolver(project, _design(NICK)), out,
                            [LIB_ID, "Missing:Chip", "no-colon", STOCK_ID])
    assert summary["ok"] is False
    assert summary["written"] == [str(out / f"{NICK}_LG290P.pins.json"),
                                  str(out / "TestLib_PosFileR.pins.json")]
    assert summary["pins"] == {LIB_ID: 79, STOCK_ID: 2}
    reasons = {f["symbol"]: f["reason"] for f in summary["failed"]}
    assert reasons["Missing:Chip"].startswith("symbol library not found: 'Missing': ")
    assert reasons["no-colon"] == "'no-colon' is not of the form Library:Symbol"


def test_build_all_reports_an_unwritable_out_dir(project: Path):
    blocker = project / "a_file"
    blocker.write_text("x", encoding="utf-8")
    summary = bps.build_all(project / "libs", blocker, [LIB_ID])
    assert summary["ok"] is False and summary["written"] == []
    assert summary["failed"][0]["reason"].startswith("cannot write ")


# --- CLI, legacy form: <symbols_dir> <out_dir> Lib:Sym ... ----------------------------

def test_legacy_cli_is_unchanged(tmp_path: Path, capsys: pytest.CaptureFixture):
    out = tmp_path / "made" / "here"
    assert bps.main([str(FIXTURE_SYMBOLS), str(out), STOCK_ID]) == 0
    summary = _last_json(capsys.readouterr().out)
    assert summary["ok"] is True and summary["failed"] == []
    assert summary["written"] == [str(out / "TestLib_PosFileR.pins.json")]
    assert bps.main([str(FIXTURE_SYMBOLS), str(out), "TestLib:Nope"]) == 1
    assert _last_json(capsys.readouterr().out)["failed"][0]["symbol"] == "TestLib:Nope"


def test_legacy_cli_takes_a_library_map_file(project: Path, capsys: pytest.CaptureFixture):
    lib_map = project / "libmap.json"
    lib_map.write_text(json.dumps(_resolver(project, _design("GNSS")).library_map()),
                       encoding="utf-8")
    out = project / "out"
    assert bps.main([str(lib_map), str(out), "GNSS:LG290P", STOCK_ID]) == 0
    assert _last_json(capsys.readouterr().out)["pins"] == {"GNSS:LG290P": 79, STOCK_ID: 2}


@pytest.mark.parametrize("argv", [
    [], ["syms"], ["syms", "out"], ["--netlist"], ["--netlist", ""],
    ["--netlist", "n.json", "--bogus", "x"], ["--netlist", "n.json", "--out"],
    ["--netlist", "a.json", "--netlist", "b.json"], ["--symbols", "syms", "Lib:Sym"]])
def test_usage_errors_exit_2_and_write_nothing(argv: list[str], tmp_path: Path,
                                                capsys: pytest.CaptureFixture,
                                                monkeypatch: pytest.MonkeyPatch):
    monkeypatch.chdir(tmp_path)
    assert bps.main(argv) == bps.USAGE_EXIT
    assert "Usage:" in capsys.readouterr().out and not any(tmp_path.iterdir())


# --- CLI, --netlist: the netlist's resolver, <netlist dir>/pins by default -----------

def test_netlist_mode_writes_project_parts_to_the_netlist_dir(project: Path,
                                                              capsys: pytest.CaptureFixture):
    assert bps.main(["--netlist", str(project / "netlist.json")]) == 0
    summary = _last_json(capsys.readouterr().out)
    path = project / "pins" / f"{NICK}_LG290P.pins.json"
    assert summary == {"ok": True, "written": [str(path)], "failed": [], "pins": {LIB_ID: 79},
                       "out_dir": str(project / "pins"),
                       "netlist": str(project / "netlist.json")}
    sidecar = json.loads(path.read_text(encoding="utf-8"))
    assert _pad_keys(sidecar) == LG290P_PADS          # one sidecar key per pad (79 pins)
    assert list((project / "pins").iterdir()) == [path]          # stock R1: not by default


def test_netlist_mode_with_stock_symbols_and_explicit_ids(project: Path,
                                                          capsys: pytest.CaptureFixture):
    netlist = str(project / "netlist.json")
    assert bps.main(["--netlist", netlist, "--symbols", str(FIXTURE_SYMBOLS),
                     STOCK_ID, LIB_ID]) == 0
    summary = _last_json(capsys.readouterr().out)
    assert summary["pins"] == {STOCK_ID: 2, LIB_ID: 79}
    assert summary["written"] == [str(project / "pins" / "TestLib_PosFileR.pins.json"),
                                  str(project / "pins" / f"{NICK}_LG290P.pins.json")]
    # without --symbols a stock part resolves nowhere: a failure naming why
    assert bps.main(["--netlist", netlist, STOCK_ID]) == 1
    reason = _last_json(capsys.readouterr().out)["failed"][0]["reason"]
    assert "no stock symbol directory is configured" in reason


def test_netlist_mode_out_override(project: Path, capsys: pytest.CaptureFixture):
    out = project / "elsewhere"
    assert bps.main(["--out", str(out), "--netlist", str(project / "netlist.json")]) == 0
    assert _last_json(capsys.readouterr().out)["written"] == [
        str(out / f"{NICK}_LG290P.pins.json")]
    assert not (project / "pins").exists()


def test_netlist_mode_reads_the_project_sym_lib_table(project: Path,
                                                      capsys: pytest.CaptureFixture):
    (project / "sym-lib-table").write_text(
        '(sym_lib_table\n  (version 7)\n  (lib (name "SPS_parts")(type "KiCad")'
        '(uri "${KIPRJMOD}/libs/SPS_parts.kicad_sym")(options "")(descr ""))\n)\n',
        encoding="utf-8")
    netlist = _write_netlist(project, _design(NICK, block=False))
    assert bps.main(["--netlist", str(netlist), "--symbols", str(FIXTURE_SYMBOLS)]) == 0
    assert _last_json(capsys.readouterr().out)["pins"] == {LIB_ID: 79}


def test_netlist_mode_reports_an_undeclared_nickname(project: Path,
                                                     capsys: pytest.CaptureFixture):
    design = _design(NICK)
    design["components"].append({"ref": "U3", "lib_id": "Typo_parts:LG290P", "value": "X",
                                 "footprint": "F"})
    netlist = _write_netlist(project, design)
    assert bps.main(["--netlist", str(netlist), "--symbols", str(FIXTURE_SYMBOLS)]) == 1
    summary = _last_json(capsys.readouterr().out)
    assert summary["pins"] == {LIB_ID: 79}
    assert [f["symbol"] for f in summary["failed"]] == ["Typo_parts:LG290P"]
    assert "symbol library not found: 'Typo_parts'" in summary["failed"][0]["reason"]


def test_netlist_mode_without_project_parts_writes_nothing(tmp_path: Path,
                                                           capsys: pytest.CaptureFixture):
    netlist = _write_netlist(tmp_path, {"name": "stock", "components": [
        {"ref": "R1", "lib_id": STOCK_ID, "value": "1k", "footprint": "R"}], "nets": {}})
    assert bps.main(["--netlist", str(netlist), "--symbols", str(FIXTURE_SYMBOLS)]) == 0
    summary = _last_json(capsys.readouterr().out)
    assert summary["ok"] is True and summary["written"] == [] and summary["failed"] == []
    assert "no project-library symbols" in summary["note"]
    assert not (tmp_path / "pins").exists()


def test_netlist_mode_refuses_project_parts_in_the_central_dir(
        project: Path, tmp_path_factory: pytest.TempPathFactory,
        capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch):
    central = tmp_path_factory.mktemp("central_pins")
    monkeypatch.setattr(bps, "CENTRAL_PINS_DIR", central)
    netlist = str(project / "netlist.json")
    assert bps.main(["--netlist", netlist, "--symbols", str(FIXTURE_SYMBOLS),
                     "--out", str(central / "."), LIB_ID, STOCK_ID]) == 1
    summary = _last_json(capsys.readouterr().out)
    assert summary["written"] == [str(central / "TestLib_PosFileR.pins.json")]   # stock: fine
    assert [f["symbol"] for f in summary["failed"]] == [LIB_ID]
    reason = summary["failed"][0]["reason"]
    assert "project library" in reason and str(project / "pins") in reason
    assert not (central / f"{NICK}_LG290P.pins.json").exists()


@pytest.mark.parametrize("content,needle", [
    (None, "cannot read netlist"),
    ("[1, 2]", "is not a JSON object"),
    ('{"components": [], "libraries": {"symbols": []}}', "libraries.symbols must be an object"),
])
def test_netlist_mode_bad_netlist_exits_1_and_writes_nothing(
        tmp_path: Path, capsys: pytest.CaptureFixture, content: str | None, needle: str):
    netlist = tmp_path / "netlist.json"
    if content is not None:
        netlist.write_text(content, encoding="utf-8")
    assert bps.main(["--netlist", str(netlist)]) == 1
    out = _last_json(capsys.readouterr().out)
    assert out["ok"] is False and needle in out["error"]
    assert not (tmp_path / "pins").exists()


def test_cli_script_netlist_mode_end_to_end(project: Path):
    """The real script under this venv, as the MCP tool runs it."""
    r = subprocess.run([sys.executable, str(SCRIPT), "--netlist", str(project / "netlist.json"),
                        "--symbols", str(FIXTURE_SYMBOLS)],
                       stdin=subprocess.DEVNULL, capture_output=True, encoding="utf-8",
                       errors="replace", timeout=CHILD_TIMEOUT_S)
    assert r.returncode == 0, r.stdout + r.stderr
    summary = _last_json(r.stdout)
    path = project / "pins" / f"{NICK}_LG290P.pins.json"
    assert summary["written"] == [str(path)] and summary["pins"] == {LIB_ID: 79}
    assert _pad_keys(json.loads(path.read_text(encoding="utf-8"))) == LG290P_PADS
