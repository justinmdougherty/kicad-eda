"""sch_file_model with project libraries (F2-17, ROADMAP D1): the interactive
file backend resolves each symbol the way KiCad does for the schematic's
directory (its project sym-lib-table, ``${KIPRJMOD}`` = that directory, then
the stock library), through symlib's version-keyed parse cache (no stale
per-process cache), writes a minimal ``<stem>.kicad_pro`` beside the saved
.kicad_sch so kicad-cli ERC reads that table, and quotes every string through
sch_build.q.

tests/fixtures/symbols is the stock library and sym_author writes the LG290P
into the project library, so only the live ERC check needs KiCad.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

import kicad_ipc as ipc
import kicad_server as k
import libtable
import sch_build
import sch_file_model as sfm
import sym_author
import symlib
import toolchain as tc
from fixtures.kicad_install import KICAD, requires_kicad_cli

TESTS = Path(__file__).resolve().parent
FIXTURE_SYMBOLS = TESTS / "fixtures" / "symbols"
NICK = "SPS_parts"
LG290P = f"{NICK}:LG290P"
STOCK_R = "TestLib:PosFileR"
LG290P_PADS = {str(n) for n in range(1, 80)}
UUID_RE = re.compile(r'"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"')
ERC_TIMEOUT_S = 300


def _table_row(nick: str, uri: str, extra: str = "") -> str:
    return f'  (lib (name "{nick}")(type "KiCad")(uri "{uri}")(options "")(descr ""){extra})\n'


def _write_table(directory: Path, *rows: str) -> Path:
    path = directory / "sym-lib-table"
    path.write_text("(sym_lib_table\n  (version 7)\n" + "".join(rows) + ")\n", encoding="utf-8")
    return path


@pytest.fixture
def stock(monkeypatch: pytest.MonkeyPatch) -> list[str | None]:
    """tc.symbols_dir -> the fixture stock dir (an explicit dir wins, as in
    toolchain); set stock[0] = None for a machine without KiCad libraries."""
    default: list[str | None] = [str(FIXTURE_SYMBOLS)]
    monkeypatch.setattr(tc, "symbols_dir", lambda explicit="": explicit or default[0])
    return default


@pytest.fixture
def proj(tmp_path: Path, stock: list[str | None]) -> Path:
    """A schematic directory whose project sym-lib-table declares the LG290P
    library at ${KIPRJMOD}/libs/SPS_parts.kicad_sym."""
    project = tmp_path / "proj"
    lib = project / "libs" / f"{NICK}.kicad_sym"
    lib.parent.mkdir(parents=True)
    sym_author.author_symbol(sym_author.lg290p_spec(), lib)
    _write_table(project, _table_row(NICK, f"${{KIPRJMOD}}/libs/{NICK}.kicad_sym"))
    return project


def _lg290p_offsets(project: Path) -> dict:
    flat = symlib.flatten({}, project / "libs", NICK, "LG290P")
    return symlib.pin_offsets(flat)


def _no_uuids(text: str) -> str:
    return UUID_RE.sub('"<uuid>"', text)


# --- resolution through the schematic directory's project table -------------------------

def test_a_project_sym_lib_table_resolves_the_lg290p_symbol_and_pin_map(proj: Path):
    sch = str(proj / "gnss.kicad_sch")
    fs = sfm.FileSchematic(sch)
    res = fs.place_symbol(LG290P, "U1", 100.0, 100.0)
    assert "pins_note" not in res, res.get("pins_note")
    assert {p["number"] for p in res["pins"]} == LG290P_PADS
    offsets = _lg290p_offsets(proj)
    vcc = offsets["23"]
    assert fs.pin_xy("U1", "23") == (round(100.0 + vcc["x"], 4), round(100.0 + vcc["y"], 4))
    assert fs.pin_xy("U1", "VCC") == fs.pin_xy("U1", "23")             # by pin name too
    pin_map = sfm.pin_map(LG290P, sch)
    assert LG290P_PADS <= set(pin_map) and pin_map["23"] == vcc
    assert sfm.library_pin_map(LG290P, sch) == offsets


def test_the_same_symbol_without_a_project_table_does_not_resolve(proj: Path):
    (proj / "sym-lib-table").unlink()
    fs = sfm.FileSchematic(str(proj / "gnss.kicad_sch"))
    res = fs.place_symbol(LG290P, "U1", 100.0, 100.0)
    assert res["pins"] == [] and f"'{NICK}'" in res["pins_note"]
    with pytest.raises(sfm.FileModelError, match=NICK):
        fs.pin_xy("U1", "23")


def test_stock_symbols_resolve_beside_the_project_library(proj: Path):
    fs = sfm.FileSchematic(str(proj / "gnss.kicad_sch"))
    pins = fs.place_symbol(STOCK_R, "R1", 50.0, 50.0)["pins"]
    assert {(p["number"], p["x_mm"], p["y_mm"]) for p in pins} == {
        ("1", 50.0, 46.19), ("2", 50.0, 53.81)}


def test_a_project_row_overrides_the_stock_library_of_the_same_nickname(proj: Path):
    """KiCad's order: the project table beats the stock library."""
    local = proj / "libs" / "TestLib.kicad_sym"
    text = (FIXTURE_SYMBOLS / "TestLib.kicad_sym").read_text(encoding="utf-8")
    local.write_text(text.replace("(at 0 3.81 270)", "(at 0 7.62 270)"), encoding="utf-8")
    _write_table(proj, _table_row(NICK, f"${{KIPRJMOD}}/libs/{NICK}.kicad_sym"),
                 _table_row("TestLib", "${KIPRJMOD}/libs/TestLib.kicad_sym"))
    fs = sfm.FileSchematic(str(proj / "gnss.kicad_sch"))
    fs.place_symbol(STOCK_R, "R1", 50.0, 50.0)
    assert fs.pin_xy("R1", "1") == (50.0, 42.38)


def test_a_disabled_project_row_hides_the_library(proj: Path):
    _write_table(proj, _table_row(NICK, f"${{KIPRJMOD}}/libs/{NICK}.kicad_sym", "(disabled)"))
    fs = sfm.FileSchematic(str(proj / "gnss.kicad_sch"))
    fs.place_symbol(LG290P, "U1", 100.0, 100.0)
    res = fs.save()
    assert res["ok"] is False and res["unresolved_symbols"] == [LG290P]


def test_without_any_pin_source_the_note_names_every_place_looked(tmp_path: Path,
                                                                 stock: list[str | None]):
    stock[0] = None
    fs = sfm.FileSchematic(str(tmp_path / "a.kicad_sch"))
    note = fs.place_symbol(STOCK_R, "R1", 0.0, 0.0)["pins_note"]
    assert note.startswith(f"Pin coordinates for {STOCK_R} are unknown"), note
    assert "KICAD_PATH" in note and "sym-lib-table beside the schematic" in note
    assert sfm.library_pin_map(STOCK_R) is None                  # no schematic: stock only
    stock[0] = str(FIXTURE_SYMBOLS)
    assert sfm.library_pin_map(STOCK_R)["1"] == {"x": 0.0, "y": -3.81, "unit": 1,
                                                 "number": "1"}


def test_a_project_pins_sidecar_still_overrides_the_project_symbol(proj: Path):
    (proj / "pins").mkdir()
    (proj / "pins" / f"{NICK}_LG290P.pins.json").write_text(
        json.dumps({"23": {"x": 1.27, "y": 2.54}}), encoding="utf-8")
    fs = sfm.FileSchematic(str(proj / "gnss.kicad_sch"))
    fs.place_symbol(LG290P, "U1", 100.0, 100.0)
    assert fs.pin_xy("U1", "23") == (101.27, 102.54)
    vdd = _lg290p_offsets(proj)["9"]                       # not in the sidecar: the symbol's
    assert fs.pin_xy("U1", "9") == (round(100 + vdd["x"], 4), round(100 + vdd["y"], 4))


def test_a_regenerated_project_library_is_read_again(proj: Path):
    """No per-process library cache: a rewritten .kicad_sym is what the next
    lookup sees (symlib keys its parse cache by file version)."""
    lib = proj / "libs" / "Proj_parts.kicad_sym"
    body = ('(kicad_symbol_lib (version 20241209) (generator "kicad-eda-tests")\n'
            '  (symbol "X" (in_bom yes) (on_board yes)\n'
            '    (property "Reference" "U" (at 0 0 0) (effects (font (size 1.27 1.27))))\n'
            '    (property "Value" "X" (at 0 0 0) (effects (font (size 1.27 1.27))))\n'
            '    (symbol "X_1_1"\n'
            '      (pin passive line (at 0 {y} 270) (length 1.27) (name "A") (number "1")))))\n')
    lib.write_text(body.format(y="2.54"), encoding="utf-8")
    _write_table(proj, _table_row("Proj_parts", "${KIPRJMOD}/libs/Proj_parts.kicad_sym"))
    fs = sfm.FileSchematic(str(proj / "gnss.kicad_sch"))
    fs.place_symbol("Proj_parts:X", "U1", 10.0, 10.0)
    assert fs.pin_xy("U1", "1") == (10.0, 7.46)
    lib.write_text(body.format(y="5.08"), encoding="utf-8")
    assert fs.pin_xy("U1", "1") == (10.0, 4.92)
    assert not hasattr(sfm, "_SYM_CACHE")


def test_a_malformed_project_table_is_an_error_naming_it(proj: Path):
    (proj / "sym-lib-table").write_text("(sym_lib_table (lib (name", encoding="utf-8")
    sch = proj / "gnss.kicad_sch"
    fs = sfm.FileSchematic(str(sch))
    fs.place_symbol(STOCK_R, "R1", 50.0, 50.0)
    with pytest.raises(sfm.FileModelError, match="sym-lib-table"):
        fs.save()
    assert not sch.exists()


# --- symbol sources -------------------------------------------------------------------------

def test_flat_symbol_takes_a_directory_a_resolver_or_a_library_map(proj: Path):
    resolver = libtable.LibraryResolver.for_directory(proj, FIXTURE_SYMBOLS, None)
    by_dir = sfm.flat_symbol(STOCK_R, str(FIXTURE_SYMBOLS))
    assert sfm.flat_symbol(STOCK_R, resolver) == by_dir
    assert sfm.flat_symbol(STOCK_R, resolver.library_map()) == by_dir
    assert sfm.flat_symbol(LG290P, resolver)[1] == "LG290P"
    with pytest.raises(sfm.FileModelError, match="must be 'Library:Symbol'"):
        sfm.flat_symbol("LG290P", resolver)
    with pytest.raises(sfm.FileModelError, match="Ghost"):
        sfm.flat_symbol("Ghost:R", resolver)


def test_symbol_source_is_none_only_without_any_library(tmp_path: Path):
    sch = str(tmp_path / "a.kicad_sch")
    assert sfm.symbol_source(sch, None) is None
    stock_only = sfm.symbol_source(sch, str(FIXTURE_SYMBOLS))
    assert stock_only.symbol_lib_path("TestLib") == FIXTURE_SYMBOLS / "TestLib.kicad_sym"
    _write_table(tmp_path, _table_row("P", "${KIPRJMOD}/p.kicad_sym"))
    project_only = sfm.symbol_source(sch, None)
    assert project_only.symbol_lib_path("P") == tmp_path / "p.kicad_sym"


# --- save: embedded symbols, unresolved ids, the .kicad_pro -----------------------------------

def test_save_embeds_project_and_stock_symbols_and_writes_a_kicad_pro(proj: Path):
    sch = proj / "gnss.kicad_sch"
    fs = sfm.FileSchematic(str(sch))
    fs.place_symbol(LG290P, "U1", 100.0, 100.0)
    fs.place_symbol(STOCK_R, "R1", 50.0, 50.0)
    res = fs.save()
    pro = proj / "gnss.kicad_pro"
    assert res["ok"] is True and "unresolved_symbols" not in res, res
    assert res["kicad_pro"] == str(pro) and res["kicad_pro_written"] is True
    text = sch.read_text(encoding="utf-8")
    assert f'\t\t(symbol "{LG290P}"' in text and f'\t\t(symbol "{STOCK_R}"' in text
    assert '\t\t(pin "79" (uuid' in text
    assert json.loads(pro.read_text(encoding="utf-8")) == {
        "meta": {"filename": "gnss.kicad_pro", "version": 1}}


def test_an_existing_kicad_pro_is_never_overwritten(proj: Path):
    pro = proj / "gnss.kicad_pro"
    pro.write_text('{"user": "settings"}\n', encoding="utf-8")
    fs = sfm.FileSchematic(str(proj / "gnss.kicad_sch"))
    fs.place_symbol(STOCK_R, "R1", 50.0, 50.0)
    res = fs.save()
    assert res["ok"] is True and res["kicad_pro_written"] is False
    assert pro.read_text(encoding="utf-8") == '{"user": "settings"}\n'


@pytest.mark.parametrize("lib_id,unresolved", [(STOCK_R, None), ("Ghost:R", ["Ghost:R"])])
def test_a_kicad_pro_that_cannot_be_written_is_an_ok_false(
        proj: Path, monkeypatch: pytest.MonkeyPatch, lib_id: str, unresolved: list | None):
    def refuse(path: object) -> bool:
        raise PermissionError(13, "Access is denied", str(path))
    monkeypatch.setattr(libtable, "write_minimal_kicad_pro", refuse)
    sch = proj / "gnss.kicad_sch"
    fs = sfm.FileSchematic(str(sch))
    fs.place_symbol(lib_id, "R1", 50.0, 50.0)
    res = fs.save()
    assert res["ok"] is False and res["written"] is True and sch.is_file()
    assert res["kicad_pro_written"] is False and "Access is denied" in res["kicad_pro_error"]
    assert res["reason"].endswith(res["kicad_pro_error"])
    assert res.get("unresolved_symbols") == unresolved
    if unresolved:
        assert res["reason"].startswith("file written but lib_symbols is incomplete")


def test_save_as_writes_the_kicad_pro_of_the_new_stem(proj: Path):
    fs = sfm.FileSchematic(str(proj / "gnss.kicad_sch"))
    fs.place_symbol(LG290P, "U1", 100.0, 100.0)
    sub = proj / "sub"
    sub.mkdir()
    _write_table(sub, _table_row(NICK, f"${{KIPRJMOD}}/../libs/{NICK}.kicad_sym"))
    res = fs.save_as(str(sub / "copy.kicad_sch"))
    assert res["ok"] is True and (sub / "copy.kicad_pro").is_file()
    assert not (proj / "gnss.kicad_pro").exists()


def test_without_a_project_table_save_reports_the_unresolved_project_symbol(proj: Path):
    (proj / "sym-lib-table").unlink()
    sch = proj / "gnss.kicad_sch"
    fs = sfm.FileSchematic(str(sch))
    fs.place_symbol(LG290P, "U1", 100.0, 100.0)
    fs.place_symbol(STOCK_R, "R1", 50.0, 50.0)
    res = fs.save()
    assert res["ok"] is False and res["written"] is True
    assert res["unresolved_symbols"] == [LG290P]
    assert str(FIXTURE_SYMBOLS) in res["reason"]
    assert f'\t\t(symbol "{STOCK_R}"' in sch.read_text(encoding="utf-8")


def test_a_project_table_without_stock_libraries_embeds_the_project_symbols(
        proj: Path, stock: list[str | None]):
    stock[0] = None
    sch = proj / "gnss.kicad_sch"
    fs = sfm.FileSchematic(str(sch))
    fs.place_symbol(LG290P, "U1", 100.0, 100.0)
    fs.place_symbol(STOCK_R, "R1", 50.0, 50.0)
    res = fs.save()
    assert res["ok"] is False and res["unresolved_symbols"] == [STOCK_R]
    assert "sym-lib-table" in res["reason"]
    assert f'\t\t(symbol "{LG290P}"' in sch.read_text(encoding="utf-8")


def test_an_explicit_symbols_dir_is_the_stock_behind_the_project_table(
        proj: Path, stock: list[str | None]):
    stock[0] = None
    fs = sfm.FileSchematic(str(proj / "gnss.kicad_sch"))
    fs.place_symbol(LG290P, "U1", 100.0, 100.0)
    fs.place_symbol(STOCK_R, "R1", 50.0, 50.0)
    assert fs.save(str(FIXTURE_SYMBOLS))["ok"] is True


def test_no_library_at_all_keeps_the_empty_lib_symbols_warning(tmp_path: Path,
                                                               stock: list[str | None]):
    stock[0] = None
    sch = tmp_path / "bare.kicad_sch"
    fs = sfm.FileSchematic(str(sch))
    fs.place_symbol(STOCK_R, "R1", 50.0, 50.0)
    res = fs.save()
    assert res["ok"] is True and res["warning"].startswith("lib_symbols empty")
    assert "\t(lib_symbols\n\t)\n" in sch.read_text(encoding="utf-8")
    assert (tmp_path / "bare.kicad_pro").is_file()


def test_stock_only_text_is_the_same_through_a_directory_and_a_resolver(tmp_path: Path):
    """A schematic without project libraries embeds exactly what the stock
    directory alone gives (the pre-F2-17 behaviour)."""
    fs = sfm.FileSchematic(str(tmp_path / "s.kicad_sch"))
    fs.place_symbol(STOCK_R, "R1", 50.0, 50.0)
    fs.place_symbol("TestLib:DerivedAmp", "U1", 80.0, 50.0, unit=2)
    fs.add_label("N1", 50.0, 46.19)
    by_dir, missing_dir = sfm.emit(fs.model, str(FIXTURE_SYMBOLS))
    resolver = sfm.symbol_source(fs.path, str(FIXTURE_SYMBOLS))
    by_resolver, missing_resolver = sfm.emit(fs.model, resolver)
    assert missing_dir == missing_resolver == []
    assert _no_uuids(by_dir) == _no_uuids(by_resolver)
    assert '\t\t(symbol "TestLib:DerivedAmp"' in by_dir


# --- quoting through sch_build.q ---------------------------------------------------------------

@pytest.mark.parametrize("value", ['1/4"', "a\\b", "two\nlines", "tab\there", "cr\rx", "µF"])
def test_q_is_sch_build_q(value: str):
    assert sfm._q(value) == sch_build.q(value)


def test_a_raw_newline_is_escaped_in_the_saved_file(proj: Path):
    sch = proj / "gnss.kicad_sch"
    fs = sfm.FileSchematic(str(sch))
    fs.place_symbol(STOCK_R, "R1", 50.0, 50.0, value="10k\n1%")
    assert fs.save()["ok"] is True
    assert '(property "Value" "10k\\n1%"' in sch.read_text(encoding="utf-8")


def test_a_nul_character_is_a_file_model_error_and_nothing_is_written(proj: Path):
    sch = proj / "gnss.kicad_sch"
    fs = sfm.FileSchematic(str(sch))
    fs.place_symbol(STOCK_R, "R1", 50.0, 50.0, value="A\x00B")
    with pytest.raises(sfm.FileModelError, match="NUL"):
        fs.save()
    assert not sch.exists()


# --- through the MCP schematic tools ------------------------------------------------------------

@pytest.fixture
def tools(monkeypatch: pytest.MonkeyPatch) -> None:
    """kicad_server on the file backend: no GUI, no API, an empty registry."""
    monkeypatch.setattr(ipc, "connect", lambda: {"ipc_connected": False, "reason": "test"})
    monkeypatch.setattr(k, "_API", {"checked": True, "module": None, "reason": "test: no api"})
    monkeypatch.setattr(k, "_api_available", lambda: False)
    monkeypatch.setattr(k, "_SESSIONS", {})


def test_the_schematic_tools_wire_and_save_the_project_lg290p(proj: Path, tools: None):
    sch = str(proj / "gnss.kicad_sch")
    assert k.create_schematic(sch)["backend"] == "file"
    placed = k.add_component(sch, "U1", LG290P, "LG290P", 100, 100)
    assert placed["ok"] and {p["number"] for p in placed["pins"]} == LG290P_PADS
    assert k.add_component(sch, "R1", STOCK_R, "10k", 40, 60)["ok"]
    assert k.wire_by_ref(sch, "R1", "1", "U1", "VCC")["ok"]
    assert k.label_pin(sch, "U1", "23", "VCC_3V3")["ok"]
    nets = k.schematic_netlist(sch)["nets"]
    assert nets["VCC_3V3"] == [["R1", "1"], ["U1", "23"]]
    saved = k.save_schematic(sch)
    assert saved["ok"] is True and saved["kicad_pro_written"] is True, saved
    assert (proj / "gnss.kicad_pro").is_file()


# --- live: kicad-cli ERC reads the project table through the .kicad_pro ------------------------

def _erc(sch: Path, report: Path) -> dict:
    proc = subprocess.run([str(KICAD.cli), "sch", "erc", str(sch), "--format", "json",
                           "-o", str(report)], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=ERC_TIMEOUT_S,
                          stdin=subprocess.DEVNULL)
    log = proc.stdout + proc.stderr
    assert proc.returncode == 0 and not re.search(r"(^|\s)Error:", log), log
    return json.loads(report.read_text(encoding="utf-8"))


@requires_kicad_cli
def test_live_erc_finds_the_project_lg290p_with_no_lib_symbol_issues(proj: Path,
                                                                     tmp_path: Path):
    sch = proj / "gnss.kicad_sch"
    fs = sfm.FileSchematic(str(sch))
    fs.place_symbol(LG290P, "U1", 101.6, 101.6)
    fs.power_pin("U1", "23", "VCC_3V3")
    fs.power_pin("U1", "10", "GND")
    res = fs.save(str(KICAD.symbols))
    assert res["ok"] is True and res["kicad_pro_written"] is True, res
    erc = _erc(sch, tmp_path / "erc.json")
    types = [v["type"] for sheet in erc["sheets"] for v in sheet["violations"]]
    assert "lib_symbol_issues" not in types and "lib_symbol_mismatch" not in types, types
