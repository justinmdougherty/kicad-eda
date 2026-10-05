"""symlib — shared .kicad_sym parsing: `extends` flattening, lib_symbols
embedding, pin maps and the symbol-derived pin offsets (the sidecar math, C7).

Synthetic cases use tests/fixtures/symbols/TestLib.kicad_sym; the real-library
cases (Device:R, Amplifier_Operational:LM358, sidecar parity) skip when no
KiCad install is present.
"""
from __future__ import annotations

import json
import os
import shutil
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
import sexpdata

import libtable
import symlib
from fixtures.kicad_install import KICAD, requires_symbols

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent
FIXTURE_SYMBOLS = TESTS / "fixtures" / "symbols"
SIDECARS = ROOT / "server" / "pins"
S = symlib.S

# committed sidecars to check against symlib.pin_offsets. The lib_id is spelled
# out because "Amplifier_Operational_LM358" cannot be split back by name alone.
PARITY_SIDECARS = [
    ("Device:R", "Device_R"),
    ("Device:C", "Device_C"),
    ("Amplifier_Operational:LM358", "Amplifier_Operational_LM358"),
    ("Regulator_Switching:LM2596S-5", "Regulator_Switching_LM2596S-5"),
    ("Connector_Generic:Conn_01x06", "Connector_Generic_Conn_01x06"),
]


def _flat(name: str, lib: str = "TestLib", symbols_dir: Path = FIXTURE_SYMBOLS) -> list:
    return symlib.flatten({}, symbols_dir, lib, name)


def _sub_names(flat: list) -> list[str]:
    return [sub[1] for sub in symlib.kids(flat, "symbol")]


def _props(flat: list) -> dict[str, str]:
    return {p[1]: p[2] for p in symlib.kids(flat, "property")}


# --- node helpers / load_lib -------------------------------------------------

def test_node_helpers():
    node = [S("symbol"), "X", [S("pin"), S("input")], [S("pin"), S("output")], [S("at"), 0, 0]]
    assert symlib.is_(node, "symbol") and not symlib.is_(node, "pin")
    assert not symlib.is_([], "symbol")
    assert not symlib.is_("symbol", "symbol")
    assert not symlib.is_(["symbol"], "symbol")       # bare str head, not a Symbol
    assert symlib.kids(node, "pin") == node[2:4]
    assert symlib.first(node, "at") == [S("at"), 0, 0]
    assert symlib.first(node, "nope") is None


def test_load_lib_keys_symbols_by_name():
    lib = symlib.load_lib(FIXTURE_SYMBOLS, "TestLib")
    assert set(lib) == {"BaseAmp", "DerivedAmp", "PosFileR", "CommonPins", "DupNumber"}
    assert all(symlib.is_(node, "symbol") and node[1] == name for name, node in lib.items())


def test_load_lib_missing_file_is_symbol_lib_error(tmp_path: Path):
    with pytest.raises(symlib.SymbolLibError, match="symbol library not found"):
        symlib.load_lib(tmp_path, "Nope")


def test_load_lib_unparseable_file_is_symbol_lib_error(tmp_path: Path):
    (tmp_path / "Broken.kicad_sym").write_text('(kicad_symbol_lib (symbol "X"', encoding="utf-8")
    with pytest.raises(symlib.SymbolLibError, match="cannot parse"):
        symlib.load_lib(tmp_path, "Broken")


# --- flatten -----------------------------------------------------------------

def test_flatten_base_symbol_returns_the_library_node():
    cache: dict = {}
    flat = symlib.flatten(cache, FIXTURE_SYMBOLS, "TestLib", "BaseAmp")
    assert flat is cache["TestLib"]["BaseAmp"]
    assert symlib.first(flat, "extends") is None


def test_flatten_unknown_symbol_is_symbol_lib_error():
    with pytest.raises(symlib.SymbolLibError, match="TestLib:Nope not found"):
        _flat("Nope")
    with pytest.raises(symlib.SymbolLibError, match="symbol library not found"):
        _flat("BaseAmp", lib="NoSuchLib")


def test_flatten_resolves_extends():
    flat = _flat("DerivedAmp")
    assert flat[1] == "DerivedAmp"
    assert symlib.first(flat, "extends") is None
    # unit sub-symbols are inherited and renamed onto the derived symbol
    assert _sub_names(flat) == ["DerivedAmp_0_1", "DerivedAmp_1_1",
                                "DerivedAmp_2_1", "DerivedAmp_3_1"]
    assert symlib.pin_numbers(flat) == symlib.pin_numbers(_flat("BaseAmp"))
    # header tags the derived symbol lacks come from the base
    assert symlib.first(flat, "pin_names") == [S("pin_names"), [S("offset"), 1.016]]
    assert symlib.first(flat, "in_bom") is not None
    # properties: derived overrides, base-only inherited, derived-only appended
    props = _props(flat)
    assert props["Value"] == "DerivedAmp"
    assert props["Datasheet"] == "https://example.invalid/baseamp.pdf"
    assert props["ki_keywords"] == "derived test"
    assert list(props) == ["Reference", "Value", "Footprint", "Datasheet", "ki_keywords"]


def test_flatten_does_not_mutate_the_base_symbol():
    cache: dict = {}
    symlib.flatten(cache, FIXTURE_SYMBOLS, "TestLib", "DerivedAmp")
    base = cache["TestLib"]["BaseAmp"]
    assert _sub_names(base) == ["BaseAmp_0_1", "BaseAmp_1_1", "BaseAmp_2_1", "BaseAmp_3_1"]
    assert "ki_keywords" not in _props(base)


@requires_symbols
def test_flatten_real_library_lm358_extends_lm2904():
    flat = _flat("LM358", lib="Amplifier_Operational", symbols_dir=KICAD.symbols)
    assert flat[1] == "LM358"
    assert symlib.first(flat, "extends") is None
    assert _sub_names(flat) == ["LM358_1_1", "LM358_2_1", "LM358_3_1"]
    assert set(symlib.pin_numbers(flat)) == {"1", "2", "3", "4", "5", "6", "7", "8"}
    assert _props(flat)["Value"] == "LM358"


@requires_symbols
def test_flatten_real_library_device_r_is_a_base_symbol():
    cache: dict = {}
    flat = symlib.flatten(cache, KICAD.symbols, "Device", "R")
    assert flat is cache["Device"]["R"]
    assert symlib.pin_numbers(flat) == ["1", "2"]
    assert symlib.pin_etypes(flat) == {"1": "passive", "2": "passive"}


# --- pin maps ----------------------------------------------------------------

def test_pin_numbers_and_etypes():
    flat = _flat("BaseAmp")
    assert symlib.pin_numbers(flat) == ["3", "2", "1", "5", "6", "7", "8", "4"]
    assert symlib.pin_etypes(flat) == {"3": "input", "2": "input", "1": "output",
                                       "5": "input", "6": "input", "7": "output",
                                       "8": "power_in", "4": "power_in"}


def test_unit_pins_multi_unit_and_unit0_folding():
    assert symlib.unit_pins(_flat("BaseAmp")) == {1: ["3", "2", "1"], 2: ["5", "6", "7"],
                                                  3: ["8", "4"]}
    assert symlib.unit_pins(_flat("DerivedAmp")) == symlib.unit_pins(_flat("BaseAmp"))
    # a pin in the common (unit 0) sub-symbol belongs to unit 1
    assert symlib.unit_pins(_flat("CommonPins")) == {1: ["1", "2"], 2: ["3"]}
    assert symlib.unit_pins([S("symbol"), "Empty"]) == {1: []}


@requires_symbols
def test_unit_pins_real_library_lm358():
    units = symlib.unit_pins(_flat("LM358", lib="Amplifier_Operational", symbols_dir=KICAD.symbols))
    assert {u: set(p) for u, p in units.items()} == {1: {"1", "2", "3"}, 2: {"5", "6", "7"},
                                                     3: {"4", "8"}}


# --- pin_offsets -------------------------------------------------------------

def test_pin_offsets_negates_y_and_keys_by_number_and_name():
    off = symlib.pin_offsets(_flat("BaseAmp"))
    # symbol (x, y) with +Y up  ->  schematic (x, -y) with +Y down
    assert off["3"] == {"x": -7.62, "y": -2.54, "unit": 1, "number": "3"}
    assert off["8"] == {"x": -2.54, "y": -7.62, "unit": 3, "number": "8"}
    assert off["4"] == {"x": -2.54, "y": 7.62, "unit": 3, "number": "4"}
    assert off["1"] == {"x": 7.62, "y": 0.0, "unit": 1, "number": "1"}
    # every named pin is reachable by name too, resolving back to its number
    assert off["+"] == off["3"] and off["-"] == off["2"]
    assert off["V+"] == off["8"] and off["V-"] == off["4"]
    # anonymous names never become keys
    assert "~" not in off and "" not in off
    assert set(off) == {"1", "2", "3", "4", "5", "6", "7", "8", "+", "-", "V+", "V-"}


def test_pin_offsets_name_first_wins_number_last_wins():
    off = symlib.pin_offsets(_flat("BaseAmp"))
    assert off["+"]["number"] == "3" and off["+"]["unit"] == 1     # not unit 2's "5"
    dup = symlib.pin_offsets(_flat("DupNumber"))
    assert dup["1"] == {"x": 2.54, "y": 1.27, "unit": 2, "number": "1"}     # last
    assert dup["IN"] == {"x": -2.54, "y": -1.27, "unit": 1, "number": "1"}  # first


def test_pin_offsets_folds_unit0_and_handles_pins_on_the_symbol_itself():
    assert symlib.pin_offsets(_flat("CommonPins"))["VCC"] == {"x": 0.0, "y": -5.08, "unit": 1,
                                                              "number": "1"}
    bare = [S("symbol"), "X",
            [S("pin"), S("passive"), S("line"), [S("at"), 1.27, -2.54, 0], [S("name"), "A"],
             [S("number"), "1"]],
            [S("pin"), S("passive"), S("line"), [S("name"), "B"],
             [S("number"), "2"]]]                                              # no (at ...)
    assert symlib.pin_offsets(bare) == {"1": {"x": 1.27, "y": 2.54, "unit": 1, "number": "1"},
                                        "A": {"x": 1.27, "y": 2.54, "unit": 1, "number": "1"}}


@requires_symbols
@pytest.mark.parametrize("lib_id,stem", PARITY_SIDECARS)
def test_pin_offsets_match_committed_sidecar(lib_id: str, stem: str):
    sidecar = json.loads((SIDECARS / f"{stem}.pins.json").read_text(encoding="utf-8"))
    lib, _, name = lib_id.partition(":")
    derived = symlib.pin_offsets(_flat(name, lib=lib, symbols_dir=KICAD.symbols))
    assert set(derived) == set(sidecar), lib_id
    for key, want in sidecar.items():
        assert derived[key]["x"] == pytest.approx(want["x"], abs=1e-6), f"{lib_id} {key}"
        assert derived[key]["y"] == pytest.approx(want["y"], abs=1e-6), f"{lib_id} {key}"


# --- embed / dumps -----------------------------------------------------------

def test_embed_strips_in_pos_files_and_renames_top_symbol_only():
    raw = symlib.load_lib(FIXTURE_SYMBOLS, "TestLib")["PosFileR"]
    assert symlib.first(raw, "in_pos_files") is not None          # fixture sanity
    emb = symlib.embed(raw, "TestLib:PosFileR")
    assert emb[1] == "TestLib:PosFileR"
    assert symlib.first(emb, "in_pos_files") is None
    assert _sub_names(emb) == ["PosFileR_0_1", "PosFileR_1_1"]       # bare, not prefixed
    for tag in ("pin_numbers", "pin_names", "exclude_from_sim", "in_bom", "on_board"):
        assert symlib.first(emb, tag) == symlib.first(raw, tag)
    assert _props(emb) == _props(raw)
    text = symlib.dumps(emb)
    assert text.startswith('(symbol "TestLib:PosFileR"')
    assert "in_pos_files" not in text
    assert raw[1] == "PosFileR"                                     # input untouched


def test_embed_of_flattened_derived_symbol_carries_inherited_pins():
    emb = symlib.embed(_flat("DerivedAmp"), "TestLib:DerivedAmp")
    assert emb[1] == "TestLib:DerivedAmp"
    assert symlib.pin_numbers(emb) == ["3", "2", "1", "5", "6", "7", "8", "4"]
    assert "(extends" not in symlib.dumps(emb)


def test_dumps_round_trips_through_sexpdata():
    flat = _flat("BaseAmp")
    assert sexpdata.loads(symlib.dumps(flat)) == flat


# --- symbol sources: a directory, a LibraryResolver, a library map (F2-09, D1) ----

PROJ_LIB = "libs/my_parts.kicad_sym"          # nickname "Parts": the file is named otherwise


def _proj_lib(root: Path, rel: str = PROJ_LIB) -> Path:
    """A project library (a copy of TestLib) at ``root/rel``."""
    lib = root / rel
    lib.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(FIXTURE_SYMBOLS / "TestLib.kicad_sym", lib)
    return lib


def _resolver(root: Path, block: dict) -> libtable.LibraryResolver:
    """A netlist in ``root`` with ``block`` as libraries.symbols; stock = fixtures."""
    return libtable.LibraryResolver(root, {"symbols": block}, FIXTURE_SYMBOLS, None, env={})


def _sym_lib_table(root: Path, *rows: str) -> None:
    (root / "sym-lib-table").write_text(
        "(sym_lib_table\n  (version 7)\n" + "".join(f"  {r}\n" for r in rows) + ")\n",
        encoding="utf-8")


def test_lib_path_of_a_directory_is_the_naming_convention(tmp_path: Path):
    assert symlib.lib_path(tmp_path, "X") == tmp_path / "X.kicad_sym"     # need not exist
    assert symlib.lib_path(str(tmp_path), "X") == tmp_path / "X.kicad_sym"


def test_lib_path_through_a_resolver_block_then_stock(tmp_path: Path):
    lib = _proj_lib(tmp_path)
    r = _resolver(tmp_path, {"Parts": PROJ_LIB})
    assert symlib.lib_path(r, "Parts") == lib
    assert symlib.lib_path(r, "TestLib") == FIXTURE_SYMBOLS / "TestLib.kicad_sym"


def test_lib_path_through_a_project_sym_lib_table(tmp_path: Path):
    lib = _proj_lib(tmp_path)
    _sym_lib_table(tmp_path, '(lib (name "Parts")(type "KiCad")'
                             '(uri "${KIPRJMOD}/libs/my_parts.kicad_sym")(options "")(descr ""))')
    assert symlib.lib_path(_resolver(tmp_path, {}), "Parts") == lib


def test_lib_path_unknown_nickname_through_a_resolver_names_it(tmp_path: Path):
    with pytest.raises(symlib.SymbolLibError,
                       match=r"^symbol library not found: 'Nope': it is not in the netlist "
                             r"libraries block, not in .*sym-lib-table, and .*Nope\.kicad_sym "
                             r"does not exist$"):
        symlib.lib_path(_resolver(tmp_path, {}), "Nope")


def test_lib_path_through_a_library_map(tmp_path: Path):
    lib = _proj_lib(tmp_path)
    lib_map = json.loads(json.dumps(_resolver(tmp_path, {"Parts": PROJ_LIB}).library_map()))
    assert symlib.lib_path(lib_map, "Parts") == lib
    assert symlib.lib_path(lib_map, "TestLib") == FIXTURE_SYMBOLS / "TestLib.kicad_sym"
    with pytest.raises(symlib.SymbolLibError,
                       match=r"^symbol library not found: 'Nope': not in the library map's "
                             r"symbols, and .*Nope\.kicad_sym does not exist$"):
        symlib.lib_path(lib_map, "Nope")


def test_lib_path_library_map_edge_cases(tmp_path: Path):
    with pytest.raises(symlib.SymbolLibError, match="the map has no stock symbol directory"):
        symlib.lib_path({"symbols": {}}, "Nope")
    disabled = {"symbols": {"Off": {"path": str(tmp_path / "off.kicad_sym"), "disabled": True,
                                    "origin": "the project sym-lib-table"}}}
    with pytest.raises(symlib.SymbolLibError,
                       match="^symbol library not found: 'Off': disabled in the project"):
        symlib.lib_path(disabled, "Off")
    for bad, match in [({"symbols": ["Parts"]}, "'symbols' must be an object"),
                       ({"stock": "C:/syms"}, "'stock' must be an object"),
                       ({"symbols": {"Parts": "libs/p.kicad_sym"}}, "must be an object with a "
                                                                     "'path' string")]:
        with pytest.raises(symlib.SymbolLibError, match=match):
            symlib.lib_path(bad, "Parts")


def test_non_kicad_library_type_is_a_clear_error_naming_nick_and_type(tmp_path: Path):
    (tmp_path / "old.lib").write_text("EESchema-LIBRARY Version 2.4\n", encoding="utf-8")
    _sym_lib_table(tmp_path, '(lib (name "Old")(type "Legacy")(uri "${KIPRJMOD}/old.lib")'
                             '(options "")(descr ""))')
    r = libtable.LibraryResolver.for_directory(tmp_path, FIXTURE_SYMBOLS, None, env={})
    want = r"symbol library 'Old' \(.*old\.lib, from .*sym-lib-table\) has type 'Legacy'"
    for source in (r, r.library_map()):
        with pytest.raises(symlib.SymbolLibError, match=want):
            symlib.lib_path(source, "Old")
        with pytest.raises(symlib.SymbolLibError, match=want):
            symlib.flatten({}, source, "Old", "X")


def test_an_unsupported_source_is_a_type_error():
    for source in (None, 42, ["C:/syms"]):
        with pytest.raises(TypeError, match="symbol source must be"):
            symlib.lib_path(source, "X")


def test_flatten_and_load_lib_through_a_resolver_and_a_map(tmp_path: Path):
    lib = _proj_lib(tmp_path)
    r = _resolver(tmp_path, {"Parts": PROJ_LIB})
    for source in (r, r.library_map()):
        flat = symlib.flatten({}, source, "Parts", "DerivedAmp")
        assert flat == _flat("DerivedAmp")
        assert symlib.load_lib(source, "Parts").path == lib
        with pytest.raises(symlib.SymbolLibError,
                           match=r"^symbol Parts:Nope not found in my_parts\.kicad_sym$"):
            symlib.flatten({}, source, "Parts", "Nope")


def test_a_declared_library_whose_file_is_missing(tmp_path: Path):
    r = _resolver(tmp_path, {"Gone": "libs/gone.kicad_sym"})
    with pytest.raises(symlib.SymbolLibError,
                       match=r"^symbol library not found: .*gone\.kicad_sym \(library 'Gone'\)$"):
        symlib.load_lib(r, "Gone")


def test_flatten_from_a_prefilled_cache_needs_no_source():
    cache = {"TestLib": symlib.load_lib(FIXTURE_SYMBOLS, "TestLib")}
    assert symlib.flatten(cache, None, "TestLib", "DerivedAmp") == _flat("DerivedAmp")
    with pytest.raises(symlib.SymbolLibError, match="TestLib:Nope not found in TestLib.kicad_sym"):
        symlib.flatten(cache, None, "TestLib", "Nope")
    plain = {"Alias": dict(cache["TestLib"])}       # a plain dict carries no file name
    with pytest.raises(symlib.SymbolLibError, match="Alias:Nope not found in Alias.kicad_sym"):
        symlib.flatten(plain, None, "Alias", "Nope")


# --- the parse cache: (path, mtime_ns, size) -----------------------------------

def _aged(path: Path, seconds_ago: float = 3600.0) -> Path:
    """Backdate ``path`` past the just-written window, so it is cacheable."""
    t = time.time() - seconds_ago
    os.utime(path, (t, t))
    return path


@pytest.fixture
def parses(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[int]]:
    """Counts sexpdata.loads calls, starting from an empty parse cache."""
    calls: list[int] = []
    real = sexpdata.loads

    def counting(text: str, **kw: object) -> object:
        calls.append(len(text))
        return real(text, **kw)

    symlib.clear_cache()
    monkeypatch.setattr(sexpdata, "loads", counting)
    yield calls
    symlib.clear_cache()


def test_parse_cache_reuses_an_unchanged_file(tmp_path: Path, parses: list[int]):
    _aged(_proj_lib(tmp_path, "Cached.kicad_sym"))
    (tmp_path / "sub").mkdir()
    a = symlib.load_lib(tmp_path, "Cached")
    b = symlib.load_lib(os.path.join(tmp_path, "sub", ".."), "Cached")   # another spelling
    assert len(parses) == 1
    assert a == b and a is not b and a["BaseAmp"] is b["BaseAmp"]
    a.pop("BaseAmp")                                  # a caller's dict is its own
    assert "BaseAmp" in symlib.load_lib(tmp_path, "Cached")
    assert len(parses) == 1


def test_parse_cache_rereads_a_changed_file(tmp_path: Path, parses: list[int]):
    lib = _aged(_proj_lib(tmp_path, "Cached.kicad_sym"))
    assert "Extra" not in symlib.load_lib(tmp_path, "Cached")
    text = lib.read_text(encoding="utf-8")
    lib.write_text(text.rstrip()[:-1] + '  (symbol "Extra")\n)\n', encoding="utf-8")
    _aged(lib, 1800.0)                                # another mtime and another size
    assert "Extra" in symlib.load_lib(tmp_path, "Cached")
    lib.write_text(text.replace("BaseAmp", "BaseAm2"), encoding="utf-8")    # same size
    _aged(lib, 900.0)
    assert "BaseAm2" in symlib.load_lib(tmp_path, "Cached")
    assert len(parses) == 3


def test_parse_cache_never_trusts_a_file_written_moments_ago(tmp_path: Path, parses: list[int]):
    # a same-size rewrite inside one mtime tick would look unchanged by (mtime, size)
    lib = _proj_lib(tmp_path, "Fresh.kicad_sym")
    symlib.load_lib(tmp_path, "Fresh")
    lib.write_text(lib.read_text(encoding="utf-8").replace("BaseAmp", "BaseAm2"),
                   encoding="utf-8")
    assert "BaseAm2" in symlib.load_lib(tmp_path, "Fresh")
    assert len(parses) == 2


def test_parse_cache_is_lru_within_a_byte_budget(tmp_path: Path, parses: list[int],
                                                 monkeypatch: pytest.MonkeyPatch):
    libs = {n: _aged(_proj_lib(tmp_path, f"{n}.kicad_sym")) for n in ("A", "B", "C")}
    size = libs["A"].stat().st_size
    monkeypatch.setattr(symlib, "PARSE_CACHE_BYTES", 2 * size)       # room for two
    for name in ("A", "B", "A", "C"):                  # A is used again: B is the LRU
        symlib.load_lib(tmp_path, name)
    assert len(parses) == 3
    assert symlib.cache_info() == {"entries": 2, "bytes": 2 * size, "limit": 2 * size}
    symlib.load_lib(tmp_path, "A")
    symlib.load_lib(tmp_path, "C")
    assert len(parses) == 3                            # both still cached
    symlib.load_lib(tmp_path, "B")
    assert len(parses) == 4                            # B was evicted


def test_parse_cache_keeps_neither_oversize_nor_superseded_versions(
        tmp_path: Path, parses: list[int], monkeypatch: pytest.MonkeyPatch):
    lib = _aged(_proj_lib(tmp_path, "Big.kicad_sym"))
    monkeypatch.setattr(symlib, "PARSE_CACHE_BYTES", lib.stat().st_size - 1)
    symlib.load_lib(tmp_path, "Big")
    symlib.load_lib(tmp_path, "Big")
    assert len(parses) == 2 and symlib.cache_info()["entries"] == 0
    monkeypatch.setattr(symlib, "PARSE_CACHE_BYTES", 10 * lib.stat().st_size)
    symlib.load_lib(tmp_path, "Big")
    _aged(lib, 1800.0)                                 # a new version of the same file
    symlib.load_lib(tmp_path, "Big")
    assert len(parses) == 4 and symlib.cache_info()["entries"] == 1


def test_a_parse_error_is_not_cached(tmp_path: Path, parses: list[int]):
    lib = tmp_path / "Broken.kicad_sym"
    lib.write_text('(kicad_symbol_lib (symbol "X"', encoding="utf-8")
    _aged(lib)
    with pytest.raises(symlib.SymbolLibError, match="cannot parse"):
        symlib.load_lib(tmp_path, "Broken")
    shutil.copyfile(FIXTURE_SYMBOLS / "TestLib.kicad_sym", lib)
    _aged(lib, 1800.0)
    assert "BaseAmp" in symlib.load_lib(tmp_path, "Broken")
