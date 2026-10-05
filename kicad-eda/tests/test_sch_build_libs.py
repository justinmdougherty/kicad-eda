"""sch_build with project libraries (F2-09, ROADMAP D1): the symbol source (a
stock directory, a LibraryResolver, a library map) and the pin-sidecar search
path (a list of directories searched in order).

The synthetic runs use tests/fixtures/symbols as the stock library directory
and a copy of its TestLib as the project library, so they never skip; the live
ERC check needs kicad-cli.
"""
from __future__ import annotations

import itertools
import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

import libtable
import sch_build
import sym_author
from fixtures.kicad_install import KICAD, requires_kicad_cli

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent
SCH_BUILD = ROOT / "server" / "tools" / "sch_build.py"
CENTRAL_PINS = ROOT / "server" / "pins"
FIXTURE_SYMBOLS = TESTS / "fixtures" / "symbols"
_VENV_PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
PYTHON = _VENV_PYTHON if _VENV_PYTHON.is_file() else Path(sys.executable)
PROJ_NICK = "Proj_parts"
PROJ_REL = f"libs/{PROJ_NICK}.kicad_sym"
LABEL_RE = re.compile(r'^\t\(label "([^"]+)" \(at (-?[\d.]+) (-?[\d.]+) 0\)', re.M)

# A stand-in "Device:C" whose pins sit at y = +/-7.62 (the real one, and the
# central server/pins/Device_C.pins.json, have +/-3.81): which offset a label
# lands on tells which sidecar, if any, was used.
DEVICE_LIB = """(kicad_symbol_lib (version 20241209) (generator "kicad-eda-tests")
  (symbol "C" (in_bom yes) (on_board yes)
    (property "Reference" "C" (at 0 0 0) (effects (font (size 1.27 1.27))))
    (property "Value" "C" (at 0 0 0) (effects (font (size 1.27 1.27))))
    (symbol "C_1_1"
      (pin passive line (at 0 7.62 270) (length 1.27) (name "~") (number "1"))
      (pin passive line (at 0 -7.62 90) (length 1.27) (name "~") (number "2")))))
"""
C_CELL = (50.8, 38.1)                 # sch_build's first grid cell


def _design(*, block: bool = True) -> dict:
    """U1 from the project library, R1 from the stock TestLib, PWR_FLAGs from
    the stock power library."""
    design = {"name": "proj",
              "components": [
                  {"ref": "U1", "lib_id": f"{PROJ_NICK}:DerivedAmp", "value": "amp",
                   "footprint": "F:SO8"},
                  {"ref": "R1", "lib_id": "TestLib:PosFileR", "value": "10k", "footprint": "F:R"}],
              "nets": {"VCC": [["U1", "8"], ["R1", "1"]], "GND": [["U1", "V-"], ["R1", "2"]],
                       "LOOP": [["U1", "1"], ["U1", "2"]]},
              "no_connect": [["U1", "3"], ["U1", "5"], ["U1", "6"], ["U1", "7"]]}
    if block:
        design["libraries"] = {"symbols": {PROJ_NICK: PROJ_REL}}
    return design


def _project(root: Path, design: dict) -> Path:
    """``root/netlist.json`` plus the project library it declares; returns the netlist."""
    lib = root / PROJ_REL
    lib.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(FIXTURE_SYMBOLS / "TestLib.kicad_sym", lib)
    netlist = root / "netlist.json"
    netlist.write_text(json.dumps(design), encoding="utf-8")
    return netlist


def _cli(*args: object) -> tuple[subprocess.CompletedProcess, dict | None]:
    proc = subprocess.run([str(PYTHON), str(SCH_BUILD), *map(str, args)], capture_output=True,
                          text=True, timeout=120, stdin=subprocess.DEVNULL)
    lines = proc.stdout.strip().splitlines()
    try:
        return proc, (json.loads(lines[-1]) if lines else None)
    except ValueError:
        return proc, None


@pytest.fixture
def fixed_uids(monkeypatch: pytest.MonkeyPatch) -> Callable[[], None]:
    """Deterministic uuids so two builds of one design compare byte for byte;
    call the fixture's value to restart the sequence before each build."""
    def install() -> None:
        counter = itertools.count()
        monkeypatch.setattr(sch_build, "uid",
                            lambda: f"00000000-0000-4000-8000-{next(counter):012d}")
    install()
    return install


def _labels(text: str) -> dict[str, list[tuple[float, float]]]:
    out: dict[str, list[tuple[float, float]]] = {}
    for net, x, y in LABEL_RE.findall(text):
        out.setdefault(net, []).append((float(x), float(y)))
    return out


# --- symbol sources through build() ---------------------------------------------

def test_build_resolves_a_block_library_through_a_resolver(tmp_path: Path):
    netlist = _project(tmp_path, _design())
    resolver = libtable.LibraryResolver.for_netlist(netlist, FIXTURE_SYMBOLS, None, env={})
    out = tmp_path / "proj.kicad_sch"
    assert sch_build.build(str(netlist), str(out), resolver) == {
        "ok": True, "sch": str(out), "components": 2, "nets": 3, "warnings": []}
    text = out.read_text(encoding="utf-8")
    assert f'\t\t(symbol "{PROJ_NICK}:DerivedAmp"' in text
    assert f'\t\t(lib_id "{PROJ_NICK}:DerivedAmp")' in text
    assert '\t\t(symbol "power:PWR_FLAG"' in text                  # stock, by convention


def test_resolver_map_and_directory_sources_write_identical_files(tmp_path: Path, fixed_uids):
    plain = _design(block=False)
    plain["components"][0]["lib_id"] = "TestLib:DerivedAmp"
    netlist = _project(tmp_path, plain)
    resolver = libtable.LibraryResolver.for_netlist(netlist, FIXTURE_SYMBOLS, None, env={})
    texts = []
    for i, source in enumerate((str(FIXTURE_SYMBOLS), resolver, resolver.library_map())):
        fixed_uids()
        out = tmp_path / f"s{i}.kicad_sch"
        assert sch_build.build(str(netlist), str(out), source)["ok"] is True
        texts.append(out.read_text(encoding="utf-8"))
    assert texts[0] == texts[1] == texts[2]


def test_a_map_source_builds_what_its_resolver_builds(tmp_path: Path, fixed_uids):
    netlist = _project(tmp_path, _design())
    resolver = libtable.LibraryResolver.for_netlist(netlist, FIXTURE_SYMBOLS, None, env={})
    texts = []
    for i, source in enumerate((resolver, json.loads(json.dumps(resolver.library_map())))):
        fixed_uids()
        out = tmp_path / f"m{i}.kicad_sch"
        assert sch_build.build(str(netlist), str(out), source)["ok"] is True
        texts.append(out.read_text(encoding="utf-8"))
    assert texts[0] == texts[1]


# --- symbol sources through the CLI ------------------------------------------------

def test_cli_symbols_dir_resolves_the_netlist_libraries_block(tmp_path: Path):
    netlist = _project(tmp_path, _design())
    out = tmp_path / "proj.kicad_sch"
    proc, res = _cli(netlist, out, FIXTURE_SYMBOLS)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert res == {"ok": True, "sch": str(out), "components": 2, "nets": 3, "warnings": []}
    assert f'\t\t(symbol "{PROJ_NICK}:DerivedAmp"' in out.read_text(encoding="utf-8")


def test_cli_symbols_dir_resolves_a_project_sym_lib_table(tmp_path: Path):
    netlist = _project(tmp_path, _design(block=False))
    (tmp_path / "sym-lib-table").write_text(
        f'(sym_lib_table\n  (version 7)\n  (lib (name "{PROJ_NICK}")(type "KiCad")'
        f'(uri "${{KIPRJMOD}}/{PROJ_REL}")(options "")(descr ""))\n)\n', encoding="utf-8")
    proc, res = _cli(netlist, tmp_path / "proj.kicad_sch", FIXTURE_SYMBOLS)
    assert proc.returncode == 0 and res["ok"] is True, proc.stdout + proc.stderr


def test_cli_json_library_map(tmp_path: Path):
    netlist = _project(tmp_path, _design())
    lib_map = libtable.LibraryResolver.for_netlist(netlist, FIXTURE_SYMBOLS, None).library_map()
    map_path = tmp_path / "libmap.JSON"                             # suffix: any case
    map_path.write_text(json.dumps(lib_map), encoding="utf-8")
    out = tmp_path / "proj.kicad_sch"
    proc, res = _cli(netlist, out, map_path)
    assert proc.returncode == 0 and res["ok"] is True, proc.stdout + proc.stderr
    assert f'\t\t(symbol "{PROJ_NICK}:DerivedAmp"' in out.read_text(encoding="utf-8")


@pytest.mark.parametrize("content,error", [
    (None, "SymbolLibError: cannot read library map "),
    ("{not json", "SymbolLibError: library map .* is not valid JSON"),
    ('["a"]', r"SymbolLibError: library map .* must be a JSON object \(LibraryResolver"),
    ('{"version": 99}', "SymbolLibError: library map .* has version 99; this sch_build "
                        "reads version 1"),
], ids=["missing", "malformed", "not-an-object", "version"])
def test_cli_library_map_errors(tmp_path: Path, content: str | None, error: str):
    netlist = _project(tmp_path, _design())
    map_path = tmp_path / "libmap.json"
    if content is not None:
        map_path.write_text(content, encoding="utf-8")
    out = tmp_path / "proj.kicad_sch"
    proc, res = _cli(netlist, out, map_path)
    assert proc.returncode == 1 and res["ok"] is False
    assert re.match(error, res["error"]), res["error"]
    assert not out.exists()


def test_cli_unknown_nickname_names_it_and_writes_nothing(tmp_path: Path):
    design = _design()
    design["components"][1]["lib_id"] = "Ghost:R"
    netlist = _project(tmp_path, design)
    out = tmp_path / "proj.kicad_sch"
    proc, res = _cli(netlist, out, FIXTURE_SYMBOLS)
    assert proc.returncode == 1 and res["ok"] is False
    assert res["error"].startswith("SymbolLibError: symbol library not found: 'Ghost': it is "
                                   "not in the netlist libraries block"), res["error"]
    assert not out.exists()


def test_cli_non_kicad_project_library_names_nick_and_type(tmp_path: Path):
    netlist = _project(tmp_path, _design(block=False))
    (tmp_path / "old.lib").write_text("EESchema-LIBRARY Version 2.4\n", encoding="utf-8")
    (tmp_path / "sym-lib-table").write_text(
        f'(sym_lib_table\n  (version 7)\n  (lib (name "{PROJ_NICK}")(type "Legacy")'
        '(uri "${KIPRJMOD}/old.lib")(options "")(descr ""))\n)\n', encoding="utf-8")
    proc, res = _cli(netlist, tmp_path / "proj.kicad_sch", FIXTURE_SYMBOLS)
    assert proc.returncode == 1
    assert re.match(rf"SymbolLibError: symbol library '{PROJ_NICK}' .* has type 'Legacy'",
                    res["error"]), res["error"]


def test_cli_malformed_libraries_block_is_a_json_error(tmp_path: Path):
    design = {**_design(), "libraries": {"symbols": {PROJ_NICK: ""}}}
    proc, res = _cli(_project(tmp_path, design), tmp_path / "x.kicad_sch", FIXTURE_SYMBOLS)
    assert proc.returncode == 1
    assert res["error"].startswith("LibTableError: libraries.symbols 'Proj_parts': the path"), res


# --- the sidecar search path ---------------------------------------------------------

def _sidecar(directory: Path, stem: str, offsets: dict) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{stem}.pins.json"
    path.write_text(json.dumps(offsets), encoding="utf-8")
    return path


def test_load_sidecars_takes_the_first_directory_that_has_the_file(tmp_path: Path):
    a, b = tmp_path / "a", tmp_path / "b"
    _sidecar(a, "Device_C", {"1": {"x": 1.0, "y": 1.0}})
    _sidecar(b, "Device_C", {"1": {"x": 2.0, "y": 2.0}, "2": {"x": 2.0, "y": 2.0}})
    _sidecar(b, "Device_R", {"1": {"x": 3.0, "y": 3.0}})
    ids = ["Device:C", "Device:R", "Device:L"]
    got = sch_build.load_sidecars([tmp_path / "missing", a, "", str(b)], ids)
    assert got == {"Device:C": {"1": {"x": 1.0, "y": 1.0}},      # a's file, not merged with b's
                   "Device:R": {"1": {"x": 3.0, "y": 3.0}}, "Device:L": {}}
    assert sch_build.load_sidecars(str(b), ids)["Device:C"]["2"] == {"x": 2.0, "y": 2.0}
    assert sch_build.load_sidecars("", ids) == sch_build.load_sidecars([], ids) == {
        i: {} for i in ids}


@pytest.mark.parametrize("content,match", [("{not json", "malformed pin sidecar"),
                                           ('[["1", 0, 0]]', "must be a JSON object")])
def test_load_sidecars_rejects_a_malformed_first_hit(tmp_path: Path, content: str, match: str):
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "Device_C.pins.json").write_text(content, encoding="utf-8")
    _sidecar(tmp_path / "b", "Device_C", {})
    with pytest.raises(ValueError, match=match):
        sch_build.load_sidecars([tmp_path / "a", tmp_path / "b"], ["Device:C"])


def test_default_sidecar_dirs_are_project_pins_then_the_central_dir(tmp_path: Path):
    netlist = tmp_path / "netlist.json"
    assert sch_build.default_sidecar_dirs(str(netlist)) == [str(tmp_path / "pins"),
                                                            str(CENTRAL_PINS)]
    assert sch_build.CENTRAL_PINS_DIR == CENTRAL_PINS
    assert (CENTRAL_PINS / "Device_C.pins.json").is_file()


def _c_project(root: Path) -> Path:
    """A netlist placing one "Device:C" from a stand-in stock ``root/stock``."""
    (root / "stock").mkdir(parents=True)
    (root / "stock" / "Device.kicad_sym").write_text(DEVICE_LIB, encoding="utf-8")
    shutil.copyfile(FIXTURE_SYMBOLS / "power.kicad_sym", root / "stock" / "power.kicad_sym")
    netlist = root / "netlist.json"
    netlist.write_text(json.dumps({"name": "c", "components": [
        {"ref": "C1", "lib_id": "Device:C", "value": "1u", "footprint": ""}],
        "nets": {"A": [["C1", "1"]], "B": [["C1", "2"]]}}), encoding="utf-8")
    return netlist


def _c_labels(root: Path, *sidecar_arg: object) -> dict:
    out = root / "c.kicad_sch"
    proc, res = _cli(root / "netlist.json", out, root / "stock", *sidecar_arg)
    assert proc.returncode == 0 and res["ok"] is True, proc.stdout + proc.stderr
    return _labels(out.read_text(encoding="utf-8"))


def _at(dy: float) -> list[tuple[float, float]]:
    return [(C_CELL[0], round(C_CELL[1] + dy, 2))]


def test_cli_default_sidecars_project_pins_beat_the_central_dir(tmp_path: Path):
    _c_project(tmp_path)
    # no project sidecar: the central Device_C.pins.json (y = -/+3.81) applies
    assert _c_labels(tmp_path) == {"A": _at(-3.81), "B": _at(3.81)}
    # a project pins/ file wins over the central one, as a whole file (B falls
    # back to the symbol's own +7.62, not to the central +3.81)
    _sidecar(tmp_path / "pins", "Device_C", {"1": {"x": 0.0, "y": -12.7}})
    assert _c_labels(tmp_path) == {"A": _at(-12.7), "B": _at(7.62)}
    # an explicit empty list: no sidecars at all, symbol-derived offsets
    assert _c_labels(tmp_path, "") == {"A": _at(-7.62), "B": _at(7.62)}


def test_cli_sidecar_dirs_are_an_os_pathsep_list_searched_in_order(tmp_path: Path):
    _c_project(tmp_path)
    first, second = tmp_path / "first", tmp_path / "second"
    _sidecar(second, "Device_C", {"1": {"x": 0.0, "y": -5.08}, "2": {"x": 0.0, "y": 5.08}})
    assert _c_labels(tmp_path, os.pathsep.join([str(first), str(second)])) == {
        "A": _at(-5.08), "B": _at(5.08)}
    _sidecar(first, "Device_C", {"1": {"x": 0.0, "y": -2.54}, "2": {"x": 0.0, "y": 2.54}})
    assert _c_labels(tmp_path, os.pathsep.join([str(first), str(second)])) == {
        "A": _at(-2.54), "B": _at(2.54)}


# --- live: a project library through a library map, ERC by kicad-cli ------------------

def _erc(sch: Path, report: Path) -> dict:
    proc = subprocess.run([str(KICAD.cli), "sch", "erc", str(sch), "--format", "json",
                           "-o", str(report)], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=300,
                          stdin=subprocess.DEVNULL)
    log = proc.stdout + proc.stderr
    assert proc.returncode == 0 and not re.search(r"(^|\s)Error:", log), log
    return json.loads(report.read_text(encoding="utf-8"))


@requires_kicad_cli
def test_lg290p_project_library_via_a_map_has_no_lib_symbol_issues(tmp_path: Path):
    """D1 end to end on the file backend: author the LG290P into a project
    library, build through a library map, materialize the project tables and
    a minimal .kicad_pro beside the schematic, and let kicad-cli's ERC find the
    project symbol (no lib_symbol_issues / lib_symbol_mismatch)."""
    proj, out_dir = tmp_path / "proj", tmp_path / "out"
    out_dir.mkdir()
    lib = proj / "libs" / "SPS_parts.kicad_sym"
    sym_author.author_symbol(sym_author.lg290p_spec(), lib)
    # no user/global table needed: the power library is a project one too, copied
    # from the same install as kicad-cli (the fixture's is KiCad 10 dialect, 9 cannot
    # load it)
    shutil.copyfile(KICAD.symbols / "power.kicad_sym", proj / "libs" / "power.kicad_sym")
    netlist = proj / "netlist.json"
    netlist.write_text(json.dumps({
        "name": "lg290p_map",
        "libraries": {"symbols": {"SPS_parts": "libs/SPS_parts.kicad_sym",
                                  "power": "libs/power.kicad_sym"}},
        "components": [{"ref": "U1", "lib_id": "SPS_parts:LG290P", "value": "LG290P",
                        "footprint": ""}],
        "nets": {"VCC_3V3": [["U1", "23"], ["U1", "22"]],
                 "GND": [["U1", p] for p in ["10", "12", "13"] + [str(n) for n in range(24, 80)]]},
        "no_connect": [["U1", n] for n in ["9", "11", "8", "4", "20", "21", "6", "7", "16",
                                           "15", "18", "19", "3", "14"]]}), encoding="utf-8")
    resolver = libtable.LibraryResolver.for_netlist(netlist, KICAD.symbols, None)
    map_path = tmp_path / "libmap.json"
    map_path.write_text(json.dumps(resolver.library_map()), encoding="utf-8")
    sch = out_dir / "lg290p_map.kicad_sch"
    proc, res = _cli(netlist, sch, map_path)
    assert proc.returncode == 0 and res["ok"] is True, proc.stdout + proc.stderr
    resolver.materialize(out_dir, **libtable.design_nicknames(json.loads(
        netlist.read_text(encoding="utf-8"))))
    assert libtable.write_minimal_kicad_pro(sch.with_suffix(".kicad_pro")) is True
    erc = _erc(sch, tmp_path / "erc.json")
    types = [v["type"] for sheet in erc["sheets"] for v in sheet["violations"]]
    assert "lib_symbol_issues" not in types and "lib_symbol_mismatch" not in types, types
