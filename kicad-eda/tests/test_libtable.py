"""libtable — KiCad sym-lib-table / fp-lib-table parsing and writing, and the
D1 nickname resolver (netlist ``libraries`` block > project table > stock).

Unit tier (no KiCad): the table excerpts are verbatim from the KiCad 9.0 /
10.99 templates and from tables the KiCad 11 nightly ``pcb import`` / ``sch
import`` wrote. Live tier (skipped without KiCad): the full templates parse;
``kicad-cli sch erc`` honours materialized tables beside a minimal .kicad_pro;
KiCad's bundled Python imports the module and resolves the same map.
"""
from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import kicad_env as env
import libtable as lt

TOOLS = Path(__file__).resolve().parents[1] / "server" / "tools"
LIVE_TIMEOUT_S = 120

# --- verbatim excerpts ------------------------------------------------------
# KiCad 9.0 template/global fp-lib-table: the OLD unquoted row syntax, no
# version line, and "Kicad" (lower-case k) on the first row.
K9_FP = """(fp_lib_table
  (lib (name Audio_Module)(type Kicad)(uri ${KICAD9_FOOTPRINT_DIR}/Audio_Module.pretty)(options "")(descr "Audio Module footprints"))
  (lib (name Connector_BarrelJack)(type KiCad)(uri ${KICAD9_FOOTPRINT_DIR}/Connector_BarrelJack.pretty)(options "")(descr "(DC) barrel jack connector footprints"))
  (lib (name Resistor_SMD)(type KiCad)(uri ${KICAD9_FOOTPRINT_DIR}/Resistor_SMD.pretty)(options "")(descr "Resistor footprints, surface mount (SMD)"))
)
"""
# KiCad 9.0 template sym-lib-table: quoted, no spaces between tokens.
K9_SYM = """(sym_lib_table
  (lib (name "Device")(type "KiCad")(uri "${KICAD9_SYMBOL_DIR}/Device.kicad_sym")(options "")(descr "Generic symbols for common devices"))
  (lib (name "power")(type "KiCad")(uri "${KICAD9_SYMBOL_DIR}/power.kicad_sym")(options "")(descr "Power symbols, special power flags"))
)
"""
# KiCad 10.99 template: (version 7), tab indent, spaces between tokens (CRLF
# as installed on Windows).
K11_FP = ("(fp_lib_table\r\n\t(version 7)\r\n"
          '\t(lib (name "MountingHole") (type "KiCad") (uri "${KICAD10_FOOTPRINT_DIR}/'
          'MountingHole.pretty") (options "") (descr "Mechanical fasteners"))\r\n'
          '\t(lib (name "Resistor_SMD") (type "KiCad") (uri "${KICAD10_FOOTPRINT_DIR}/'
          'Resistor_SMD.pretty") (options "") (descr "Resistor footprints, surface mount '
          '(SMD)"))\r\n)\r\n')
K11_DEVICE_ROW = ('(lib (name "Device") (type "KiCad") (uri "${KICAD10_SYMBOL_DIR}/'
                  'Device.kicad_sym") (options "") (descr "Generic symbols for common devices"))')
# Written by the KiCad 11 nightly: `pcb import` (Altium + EasyEDA into one dir)
# and `sch import` of an EasyEDA Pro project.
IMPORT_FP = """(fp_lib_table
\t(version 7)
\t(lib (name "eDP_adapter_dvt1-import-fps") (type "KiCad") (uri "${KIPRJMOD}/eDP_adapter_dvt1-import-fps.pretty") (options "kicad_import_cache=1") (descr ""))
\t(lib (name "pcb_watch-import-fps") (type "KiCad") (uri "${KIPRJMOD}/pcb_watch-import-fps.pretty") (options "kicad_import_cache=1") (descr ""))
)
"""
IMPORT_SYM = """(sym_lib_table
\t(version 7)
\t(lib (name "yuzuki-import-syms") (type "KiCad") (uri "${KIPRJMOD}/yuzuki-import-syms.kicad_sym") (options "kicad_import_cache=1") (descr ""))
)
"""


# --- helpers ----------------------------------------------------------------
def make_lib(path: Path) -> Path:
    """Create a .kicad_sym file or a .pretty directory (with one footprint)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix == ".pretty":
        path.mkdir(exist_ok=True)
        (path / "R.kicad_mod").write_text('(footprint "R" (version 20241229))\n',
                                          encoding="utf-8")
    else:
        path.write_text("(kicad_symbol_lib (version 20241209))\n", encoding="utf-8")
    return path


def same(a: Path | str, b: Path | str) -> bool:
    return os.path.normcase(os.path.normpath(str(a))) == os.path.normcase(os.path.normpath(str(b)))


@pytest.fixture
def stock(tmp_path: Path) -> tuple[Path, Path]:
    syms, fps = tmp_path / "stock" / "symbols", tmp_path / "stock" / "footprints"
    for nick in ("Device", "power", "SPS_parts"):
        make_lib(syms / f"{nick}.kicad_sym")
    for nick in ("Resistor_SMD", "Battery", "SPS_parts"):
        make_lib(fps / f"{nick}.pretty")
    return syms, fps


@pytest.fixture
def proj(tmp_path: Path) -> Path:
    """A netlist dir with authored libs and a project table (KiCad 9 syntax)."""
    d = tmp_path / "proj"
    make_lib(d / "libs" / "SPS_parts.kicad_sym")
    make_lib(d / "libs" / "SPS_parts.pretty")
    make_lib(d / "libs" / "ProjSym.kicad_sym")
    make_lib(d / "libs" / "MyDevice.kicad_sym")
    (d / "sym-lib-table").write_text(
        '(sym_lib_table\n'
        '  (lib (name "ProjSym")(type "KiCad")(uri "${KIPRJMOD}/libs/ProjSym.kicad_sym")(options "")(descr ""))\n'
        '  (lib (name "Device")(type "KiCad")(uri "${KIPRJMOD}/libs/MyDevice.kicad_sym")(options "")(descr "shadows stock"))\n'
        ')\n', encoding="utf-8")
    return d


BLOCK = {"symbols": {"SPS_parts": "libs/SPS_parts.kicad_sym"},
         "footprints": {"SPS_parts": "${KIPRJMOD}/libs/SPS_parts.pretty"}}


def resolver(proj: Path, stock: tuple[Path, Path], block: dict | None = BLOCK,
             **kw) -> lt.LibraryResolver:
    kw.setdefault("env", {})
    return lt.LibraryResolver(proj, block, stock[0], stock[1], **kw)


# --- 1. parsing both syntaxes -----------------------------------------------
def test_parses_both_row_syntaxes_kicad9_to_11_and_import_caches():
    t = lt.parse_table(K9_FP)                                   # KiCad 9, old unquoted
    assert t.tag == "fp_lib_table" and t.version is None
    assert [r.name for r in t.rows] == ["Audio_Module", "Connector_BarrelJack", "Resistor_SMD"]
    first = t.rows[0]
    assert first.type == "Kicad" and first.is_kicad          # legacy spelling still KiCad
    assert first.uri == "${KICAD9_FOOTPRINT_DIR}/Audio_Module.pretty"
    assert first.options == "" and first.descr == "Audio Module footprints"
    assert t.rows[1].descr == "(DC) barrel jack connector footprints"
    assert not any(r.disabled or r.hidden or r.managed for r in t.rows)
    t = lt.parse_table(K9_SYM)                                  # KiCad 9, quoted
    assert t.tag == "sym_lib_table" and t.version is None
    assert {r.name: r.uri for r in t.rows} == {
        "Device": "${KICAD9_SYMBOL_DIR}/Device.kicad_sym",
        "power": "${KICAD9_SYMBOL_DIR}/power.kicad_sym"}
    t = lt.parse_table(K11_FP)                                  # (version 7), tabs, CRLF
    assert t.version == 7 and [r.name for r in t.rows] == ["MountingHole", "Resistor_SMD"]
    assert t.rows[1].descr == "Resistor footprints, surface mount (SMD)"
    fp, sym = lt.parse_table(IMPORT_FP), lt.parse_table(IMPORT_SYM)
    assert all(r.import_cache for r in fp.rows + sym.rows)
    assert fp.rows[0].option_map == {"kicad_import_cache": "1"}
    assert not lt.LibRow("X", "u", options="a=1|b").import_cache
    assert lt.LibRow("X", "u", options="a=1|b").option_map == {"a": "1", "b": ""}


def test_flags_escapes_and_unknown_tokens_round_trip():
    text = ('(fp_lib_table (version 7)\n'
            '  (lib (name "My \\"Lib\\"")(type "KiCad")(uri "C:\\\\libs\\\\x.pretty")'
            '(options "")(descr "d")(disabled)(hidden)(future_token 1))\n'
            '  (future_top (a b))\n)\n')
    t = lt.parse_table(text)
    row = t.rows[0]
    assert row.name == 'My "Lib"' and row.uri == "C:\\libs\\x.pretty"
    assert row.disabled and row.hidden
    out = lt.format_table(t)
    assert t.extra == ((1, "(future_top (a b))"),)          # verbatim, after one row
    assert row.raw in out and out.index(row.raw) < out.index("(future_top (a b))")
    assert lt.parse_table(out) == t
    built = lt.LibRow('My "Lib"', "C:\\libs\\x.pretty", disabled=True, hidden=True)
    assert lt.parse_table(f"(fp_lib_table {built.to_sexp()})").rows[0] == built
    raw = lt.parse_table('(fp_lib_table (lib (name a)(type KiCad)(uri "C:\\libs\\x")'
                         '(descr "1\\n2")))').rows[0]    # unknown pairs stay as written
    assert (raw.uri, raw.descr) == ("C:\\libs\\x", "1\n2")


def test_new_rows_are_written_in_kicad11_style():
    row = lt.LibRow("Device", "${KICAD10_SYMBOL_DIR}/Device.kicad_sym",
                    descr="Generic symbols for common devices")
    assert row.to_sexp() == K11_DEVICE_ROW
    text = lt.format_table(lt.LibTable("sym_lib_table", (row,)))
    assert text == f"(sym_lib_table\n\t(version 7)\n\t{K11_DEVICE_ROW}\n)\n"


@pytest.mark.parametrize("text, needle", [
    ("", "empty"),
    ("(fp_lib_table (lib (name a)(type KiCad)(uri b))", "unclosed"),
    ("(fp_lib_table))", "unbalanced"),
    ("(fp_lib_table) (sym_lib_table)", "after the table"),
    ('(fp_lib_table (lib (name "a)))', "unterminated"),
    ("(kicad_pcb (version 1))", "not a library table"),
    ("(fp_lib_table (lib (name a)(type KiCad)))", "uri"),
    ("(fp_lib_table (lib (name a)(uri b)))", "type"),
    ("(fp_lib_table (lib (name a)(name b)(type KiCad)(uri c)))", "duplicate"),
    ("(fp_lib_table (version seven))", "version"),
    ("(fp_lib_table (lib name))", "malformed"),
    ("(fp_lib_table (lib (name a)(type KiCad)(uri b)(disabled yes)))", "disabled"),
    ("(fp_lib_table stray)", "stray"),
    ('(fp_lib_table (lib (name a b)(type KiCad)(uri c)))', "one value"),
    ("(fp_lib_table (lib (type KiCad)(uri b)))", "no .name"),
    ("lib (fp_lib_table)", "expected '\\('"),
])
def test_malformed_tables_fail_loudly(text, needle):
    with pytest.raises(lt.LibTableError, match=f"(?i)bad.table.*{needle}"):
        lt.parse_table(text, source="bad.table")


# --- 4. variable expansion ----------------------------------------------------
@pytest.mark.parametrize("n", [6, 7, 8, 9, 10, 11])
def test_versioned_stock_vars_alias_the_chosen_install(n: int, tmp_path: Path):
    syms, fps, models = tmp_path / "s", tmp_path / "f", tmp_path / "m"
    kw = dict(symbols_dir=syms, footprints_dir=fps, models_dir=models, env={})
    assert lt.expand_vars(f"${{KICAD{n}_SYMBOL_DIR}}/D.kicad_sym", **kw) == (f"{syms}/D.kicad_sym", ())
    assert lt.expand_vars(f"$(KICAD{n}_FOOTPRINT_DIR)/R.pretty", **kw) == (f"{fps}/R.pretty", ())
    assert lt.expand_vars(f"${{KICAD{n}_3DMODEL_DIR}}/a.step", **kw) == (f"{models}/a.step", ())


def test_kiprjmod_legacy_env_and_unresolved_vars(tmp_path: Path):
    kw = dict(kiprjmod=tmp_path / "p", symbols_dir=tmp_path / "s",
              footprints_dir=tmp_path / "f",
              env={"MYLIBS": "/opt/libs", "KICAD9_SYMBOL_DIR": "/wrong/install"})
    assert lt.expand_vars("${KIPRJMOD}/libs/x.pretty", **kw) == (f"{tmp_path / 'p'}/libs/x.pretty", ())
    assert lt.expand_vars("${KISYSMOD}/R.pretty", **kw)[0] == f"{tmp_path / 'f'}/R.pretty"
    assert lt.expand_vars("${KICAD_SYMBOL_DIR}/D.kicad_sym", **kw)[0] == f"{tmp_path / 's'}/D.kicad_sym"
    assert lt.expand_vars("${MYLIBS}/x.pretty", **kw) == ("/opt/libs/x.pretty", ())
    # the chosen install beats a stale environment variable of the same name
    assert lt.expand_vars("${KICAD9_SYMBOL_DIR}/D", **kw)[0] == f"{tmp_path / 's'}/D"
    # KiCad 5 never had numbered vars; unknown names stay verbatim and are reported
    assert lt.expand_vars("${KICAD5_SYMBOL_DIR}/a ${NOPE}/b", **kw) == (
        "${KICAD5_SYMBOL_DIR}/a ${NOPE}/b", ("KICAD5_SYMBOL_DIR", "NOPE"))
    assert lt.expand_vars("${KICAD9_SYMBOL_DIR}/D", env={}) == ("${KICAD9_SYMBOL_DIR}/D",
                                                                ("KICAD9_SYMBOL_DIR",))


def test_project_rows_with_any_install_var_resolve_to_the_chosen_install(tmp_path, stock):
    d = tmp_path / "p"
    d.mkdir()
    (d / "fp-lib-table").write_text(K9_FP, encoding="utf-8")
    make_lib(stock[1] / "Audio_Module.pretty")
    r = resolver(d, stock, None)
    assert same(r.footprint_lib_path("Resistor_SMD"), stock[1] / "Resistor_SMD.pretty")
    assert r.source("Audio_Module") == "project"           # a project row, not stock
    assert any("Connector_BarrelJack" in e for e in r.errors())   # absent from this stock


# --- 2. resolution order ----------------------------------------------------
def test_resolution_order_netlist_project_stock(proj, stock):
    r = resolver(proj, stock)
    assert r.source("SPS_parts") == "netlist"                       # block beats stock
    assert same(r.symbol_lib_path("SPS_parts"), proj / "libs" / "SPS_parts.kicad_sym")
    assert same(r.footprint_lib_path("SPS_parts"), proj / "libs" / "SPS_parts.pretty")
    assert r.source("ProjSym") == "project"
    assert r.source("Device") == "project"                          # project beats stock
    assert same(r.symbol_lib_path("Device"), proj / "libs" / "MyDevice.kicad_sym")
    assert r.source("power") == "stock"
    assert same(r.symbol_lib_path("power"), stock[0] / "power.kicad_sym")
    assert r.source("Resistor_SMD", kind="footprint") == "stock"
    assert r.source("Battery") == "stock"                           # falls to footprints
    assert r.errors() == []


def test_unresolved_nicknames_raise_with_what_was_searched(proj, stock):
    r = resolver(proj, stock)
    assert r.resolve("symbol", "Nope") is None
    with pytest.raises(lt.LibraryNotFoundError, match="Nope.*libraries block.*sym-lib-table"):
        r.symbol_lib_path("Nope")
    with pytest.raises(lt.LibraryNotFoundError, match="Nope"):
        r.source("Nope")
    with pytest.raises(lt.LibraryNotFoundError):
        r.footprint_lib_path("ProjSym")           # a symbol library, not a footprint one
    with pytest.raises(ValueError, match="kind"):
        r.resolve("model", "x")
    assert r.errors(symbols=["Nope", "power"], footprints=["SPS_parts"]) == [
        "symbol library 'Nope' is not in the netlist libraries block, the project "
        "sym-lib-table or the stock symbol libraries"]


def test_block_vs_project_conflict_is_an_error(proj, stock):
    r = resolver(proj, stock, {"symbols": {"ProjSym": "./libs/../libs/ProjSym.kicad_sym"}})
    assert r.source("ProjSym") == "netlist" and r.errors() == []   # same library: fine
    block = {"symbols": {"ProjSym": "elsewhere/ProjSym.kicad_sym"}}
    make_lib(proj / "elsewhere" / "ProjSym.kicad_sym")
    r = resolver(proj, stock, block)
    assert r.source("ProjSym") == "netlist"              # the block still wins
    errs = r.errors()
    assert len(errs) == 1 and "ProjSym" in errs[0] and "sym-lib-table" in errs[0]
    assert "different" in errs[0]
    with pytest.raises(lt.LibTableError, match="ProjSym"):
        r.materialize(proj)
    # the same library, but disabled in the project table: KiCad would not load it
    (proj / "sym-lib-table").write_text(
        '(sym_lib_table (lib (name "ProjSym")(type "KiCad")(uri "${KIPRJMOD}/libs/'
        'ProjSym.kicad_sym")(options "")(descr "")(disabled)))', encoding="utf-8")
    r = resolver(proj, stock, {"symbols": {"ProjSym": "libs/ProjSym.kicad_sym"}})
    assert [e for e in r.errors() if "'ProjSym'" in e and "disabled" in e] == r.errors() != []
    with pytest.raises(lt.LibTableError, match="ProjSym.*disabled"):
        r.materialize(proj)


def test_import_cache_rows_resolve(tmp_path, stock):
    d = tmp_path / "imp"
    d.mkdir()
    (d / "fp-lib-table").write_text(IMPORT_FP, encoding="utf-8")
    (d / "sym-lib-table").write_text(IMPORT_SYM, encoding="utf-8")
    make_lib(d / "eDP_adapter_dvt1-import-fps.pretty")
    make_lib(d / "yuzuki-import-syms.kicad_sym")
    r = resolver(d, stock, None)
    hit = r.resolve("footprint", "eDP_adapter_dvt1-import-fps")
    assert hit.source == "project" and hit.import_cache and hit.exists
    assert same(hit.path, d / "eDP_adapter_dvt1-import-fps.pretty")
    assert r.source("yuzuki-import-syms") == "project"
    assert r.errors() == [f"footprint library 'pcb_watch-import-fps' ({d / 'fp-lib-table'}): "
                          f"{d / 'pcb_watch-import-fps.pretty'} does not exist"]


def test_disabled_project_row_hides_the_nickname_like_kicad(tmp_path, stock):
    d = tmp_path / "p"
    d.mkdir()
    (d / "sym-lib-table").write_text(
        '(sym_lib_table (lib (name "power")(type "KiCad")(uri "x.kicad_sym")(options "")'
        '(descr "")(disabled)))', encoding="utf-8")
    r = resolver(d, stock, None)
    assert r.resolve("symbol", "power") is None             # not the stock one either
    with pytest.raises(lt.LibraryNotFoundError, match="disabled"):
        r.symbol_lib_path("power")
    assert r.errors() == []                                   # a disabled row is a choice
    out, other = tmp_path / "out", tmp_path / "other"
    out.mkdir(), other.mkdir()
    r.materialize(out)
    assert _managed(out / "sym-lib-table")["power"].disabled  # out_dir hides it too
    (other / "sym-lib-table").write_text(                     # same library, enabled there
        f'(sym_lib_table (lib (name "power")(type "KiCad")(uri "{(d / "x.kicad_sym").as_posix()}")'
        '(options "")(descr "")))', encoding="utf-8")
    with pytest.raises(lt.LibTableError, match="power.*enabled there"):
        r.materialize(other)


def test_managed_rows_in_the_project_table_are_derived_and_ignored(proj, stock):
    resolver(proj, stock).materialize(proj)                  # writes managed SPS_parts rows
    moved = {"symbols": {"SPS_parts": "v2/SPS_parts.kicad_sym"}}
    make_lib(proj / "v2" / "SPS_parts.kicad_sym")
    r = resolver(proj, stock, moved)
    assert r.errors() == []                                   # no conflict with our old row
    assert same(r.symbol_lib_path("SPS_parts"), proj / "v2" / "SPS_parts.kicad_sym")
    assert r.source("SPS_parts", kind="footprint") == "stock"  # old managed fp row ignored


def test_without_a_netlist_managed_rows_resolve_as_kicad_reads_them(tmp_path, stock):
    d, out = tmp_path / "d", tmp_path / "out"
    make_lib(d / "libs" / "MyDevice.kicad_sym"), make_lib(d / "libs" / "Own.pretty"), out.mkdir()
    block = {"symbols": {"Device": "libs/MyDevice.kicad_sym"},
             "footprints": {"Own": "libs/Own.pretty"}}
    resolver(d, stock, block).materialize(d)
    r = resolver(d, stock, None)               # e.g. a consumer holding a schematic dir
    assert r.dir_only and r.source("Device") == "project" and r.errors() == []
    assert same(r.symbol_lib_path("Device"), d / "libs" / "MyDevice.kicad_sym")  # not stock
    assert same(r.footprint_lib_path("Own"), d / "libs" / "Own.pretty")
    assert r.library_map()["footprints"]["Own"]["managed"]
    rep = r.materialize(d)                     # its own view: nothing to change
    assert not rep["sym-lib-table"]["written"] and not rep["fp-lib-table"]["written"]
    r.materialize(out)                         # elsewhere: KiCad there sees the same
    assert _managed(out / "sym-lib-table")["Device"].uri == (
        "${KIPRJMOD}/../d/libs/MyDevice.kicad_sym")
    # the netlist dropped its block ({}): the old rows are stale, and say so
    gone = resolver(d, stock, {})
    assert not gone.dir_only and gone.source("Device") == "stock"
    with pytest.raises(lt.LibraryNotFoundError, match="Own.*only as a 'kicad-eda managed' row"):
        gone.footprint_lib_path("Own")
    assert "only as a 'kicad-eda managed' row" in gone.errors(footprints=["Own"])[0]
    gone.materialize(d)                        # removes them; KiCad now agrees
    assert resolver(d, stock, None).source("Device") == "stock"


def test_errors_cover_missing_files_wrong_kinds_vars_and_duplicates(tmp_path, stock):
    d = tmp_path / "p"
    make_lib(d / "libs" / "Dir.kicad_sym.pretty")
    (d / "libs" / "File.pretty").write_text("not a dir", encoding="utf-8")
    (d / "sym-lib-table").write_text(
        '(sym_lib_table\n (lib (name "A")(type "KiCad")(uri "${MYLIBS}/a.kicad_sym")(options "")(descr ""))\n'
        ' (lib (name "A")(type "KiCad")(uri "a2.kicad_sym")(options "")(descr ""))\n'
        ' (lib (name "D")(type "KiCad")(uri "${KIPRJMOD}/libs/Dir.kicad_sym.pretty")(options "")(descr ""))\n)',
        encoding="utf-8")
    block = {"symbols": {"Gone": "libs/Gone.kicad_sym", "Odd": "libs/odd.lib"},
             "footprints": {"F": "libs/File.pretty"}}
    errs = "\n".join(resolver(d, stock, block).errors())
    assert "'A'" in errs and "duplicate" in errs
    assert "${MYLIBS}" in errs and "undefined" in errs
    assert "Gone.kicad_sym does not exist" in errs
    assert "'Odd'" in errs and ".kicad_sym" in errs
    assert "'F'" in errs and "not a directory" in errs
    assert "'D'" in errs and "not a file" in errs


@pytest.mark.parametrize("block, needle", [
    ([], "object"),
    ({"symbol": {}}, "symbol"),
    ({"symbols": ["x"]}, "symbols"),
    ({"symbols": {"a:b": "x.kicad_sym"}}, "a:b"),
    ({"symbols": {"": "x.kicad_sym"}}, "nickname"),
    ({"footprints": {"F": ""}}, "'F'"),
    ({"footprints": {"F": 3}}, "'F'"),
])
def test_malformed_libraries_block_fails_fast(block, needle, proj, stock):
    with pytest.raises(lt.LibTableError, match=needle):
        resolver(proj, stock, block)


def test_malformed_or_wrong_kind_project_tables_are_refused(tmp_path, stock):
    (tmp_path / "fp-lib-table").write_text("(fp_lib_table (lib (name x)", encoding="utf-8")
    with pytest.raises(lt.LibTableError, match="fp-lib-table"):
        resolver(tmp_path, stock, None)
    (tmp_path / "fp-lib-table").write_text(K9_SYM, encoding="utf-8")
    with pytest.raises(lt.LibTableError, match="expected \\(fp_lib_table"):
        resolver(tmp_path, stock, None)
    nl = tmp_path / "nl"
    nl.mkdir()
    with pytest.raises(lt.LibTableError, match="expected \\(fp_lib_table"):
        resolver(nl, stock, None).materialize(tmp_path)


def test_project_dir_absolute_paths_and_missing_stock_dirs(tmp_path, proj, stock):
    nl = tmp_path / "netlist_here"                 # block beside the netlist, tables elsewhere
    make_lib(nl / "libs" / "SPS_parts.kicad_sym")
    r = resolver(nl, stock, {"symbols": BLOCK["symbols"]}, project_dir=proj)
    assert same(r.symbol_lib_path("SPS_parts"), nl / "libs" / "SPS_parts.kicad_sym")
    assert same(r.symbol_lib_path("ProjSym"), proj / "libs" / "ProjSym.kicad_sym")
    lib = make_lib(tmp_path / "shared" / "Shared.pretty")
    r = lt.LibraryResolver(proj, {"footprints": {"Shared": str(lib)}}, None, None, env={})
    assert same(r.footprint_lib_path("Shared"), lib)
    assert r.resolve("symbol", "Device").source == "project"     # project row still there
    assert r.resolve("symbol", "power") is None                  # no stock dir configured
    with pytest.raises(lt.LibraryNotFoundError, match="no stock symbol directory"):
        r.symbol_lib_path("power")


def test_for_netlist_reads_the_block(tmp_path, proj, stock):
    netlist = proj / "netlist.json"
    netlist.write_text(json.dumps({"name": "x", "libraries": BLOCK}), encoding="utf-8")
    r = lt.LibraryResolver.for_netlist(netlist, *stock, env={})
    assert r.source("SPS_parts") == "netlist" and not r.dir_only
    netlist.write_text(json.dumps({"name": "x"}), encoding="utf-8-sig")  # BOM; no block: {}
    assert not lt.LibraryResolver.for_netlist(netlist, *stock, env={}).dir_only
    assert lt.LibraryResolver.for_design(proj, {"libraries": BLOCK}, *stock,
                                         env={}).source("SPS_parts") == "netlist"
    with pytest.raises(lt.LibTableError, match="netlist"):
        lt.LibraryResolver.for_design(proj, [1, 2], *stock)
    for bad in ("{not json", "[1, 2]"):
        netlist.write_text(bad, encoding="utf-8")
        with pytest.raises(lt.LibTableError, match="netlist"):
            lt.LibraryResolver.for_netlist(netlist, *stock, env={})


def test_design_nicknames_feed_errors_and_materialize(tmp_path, proj, stock):
    design = {"components": [
        {"ref": "U1", "lib_id": "SPS_parts:LG290P", "footprint": "SPS_parts:LGA79"},
        {"ref": "R1", "lib_id": "Device:R", "footprint": "Resistor_SMD:R_0603_1608Metric"},
        {"ref": "H1", "board_only": True, "footprint": "MountingHole:MountingHole_3.2mm_M3"}],
        "mounting_holes": {"footprint": "Hole:M2"}}
    assert lt.design_nicknames(design) == {                  # power: the PWR_FLAGs
        "symbols": ["Device", "SPS_parts", "power"],
        "footprints": ["Hole", "MountingHole", "Resistor_SMD", "SPS_parts"]}
    for holes, fps in [(True, ["MountingHole"]), ({"inset": 3}, ["MountingHole"]),
                       ({}, []), (False, []), (None, [])]:   # swig_build_board's default
        assert lt.design_nicknames({"mounting_holes": holes}) == {"symbols": ["power"],
                                                                  "footprints": fps}
    for module, needles in [("sch_build.py", (f'"{lt.POWER_LIBRARY}:PWR_FLAG"',)),   # what
                            ("swig_build_board.py", (f'"{lt.DEFAULT_MOUNTING_HOLE_FOOTPRINT}"',
                                                     "DEFAULT_MOUNTING_HOLE_FOOTPRINT"))]:
        src = (TOOLS / module).read_text(encoding="utf-8")    # the builders really place
        assert any(n in src for n in needles), (module, needles)
    design = {"components": [{"ref": "U1", "lib_id": "SPS_parts:X", "footprint": "SPS_parts:Y"},
                             {"ref": "R1", "lib_id": "Device:R", "footprint": "Gone:R"}]}
    used = lt.design_nicknames(design)
    r = resolver(proj, stock)
    assert r.errors(**used) == ["footprint library 'Gone' is not in the netlist libraries "
                                "block, the project fp-lib-table or the stock footprint libraries"]
    out = tmp_path / "out"
    out.mkdir()
    rep = r.materialize(out, **used)
    assert rep["sym-lib-table"]["managed"] == ["Device", "SPS_parts"]   # ProjSym unused
    assert rep["fp-lib-table"]["managed"] == ["SPS_parts"]


# --- library_map (the JSON handed to the SWIG builder) ------------------------
def test_library_map_is_json_and_lookup_matches_the_resolver(tmp_path, proj, stock):
    r = resolver(proj, stock)
    path = tmp_path / "map.json"
    path.write_text(json.dumps(r.library_map()), encoding="utf-8")
    m = json.loads(path.read_text(encoding="utf-8"))
    assert m["symbols"]["SPS_parts"]["source"] == "netlist"
    assert m["symbols"]["ProjSym"]["source"] == "project"
    assert "power" not in m["symbols"]                               # stock: by convention
    assert m["stock"] == {"symbols": str(stock[0]), "footprints": str(stock[1])}
    for kind, nick in [("symbol", "SPS_parts"), ("symbol", "Device"), ("symbol", "power"),
                       ("footprint", "SPS_parts"), ("footprint", "Resistor_SMD")]:
        assert same(lt.lookup(m, kind, nick), r.resolve(kind, nick).path), (kind, nick)
    assert lt.lookup(m, "footprint", "Nope") is None
    with pytest.raises(ValueError, match="kind"):
        lt.lookup(m, "symbols", "x")
    bare = lt.LibraryResolver(proj, None, None, None, env={}).library_map()
    assert bare["stock"] == {"symbols": None, "footprints": None}
    assert lt.lookup(bare, "symbol", "power") is None


# --- 3. materialize ----------------------------------------------------------
FOREIGN_FP = """(fp_lib_table
  (version 7)
  (lib (name Battery)(type KiCad)(uri ${KICAD9_FOOTPRINT_DIR}/Battery.pretty)(options "")(descr "Battery and battery holder footprints"))
  (lib (name "Hand")   (type "KiCad")(uri "${KIPRJMOD}/hand/Hand.pretty")(options "")(descr "hand-authored")(disabled))
  (lib (name "Qwiic_Button-import-fps") (type "KiCad") (uri "${KIPRJMOD}/Qwiic_Button-import-fps.pretty") (options "kicad_import_cache=1") (descr ""))
  (future_item (x))
)
"""


def _managed(path: Path) -> dict[str, lt.LibRow]:
    return {r.name: r for r in lt.read_table(path).rows if r.managed}


def test_materialize_writes_nothing_without_project_libraries(tmp_path, stock):
    nl, out = tmp_path / "nl", tmp_path / "out"
    nl.mkdir(), out.mkdir()
    for block in (None, {}):
        rep = resolver(nl, stock, block).materialize(out)
        assert list(out.iterdir()) == []
        assert not rep["sym-lib-table"]["written"] and not rep["fp-lib-table"]["written"]
    with pytest.raises(lt.LibTableError, match="does not exist"):
        resolver(nl, stock).materialize(tmp_path / "missing")


def test_materialize_is_idempotent_and_preserves_unmanaged_rows(tmp_path, proj, stock):
    out = tmp_path / "out"
    out.mkdir()
    fp_table = out / "fp-lib-table"
    fp_table.write_text(FOREIGN_FP, encoding="utf-8")
    foreign = lt.read_table(fp_table)
    r = resolver(proj, stock, BLOCK, project_dir=tmp_path / "no_tables_here")
    rep = r.materialize(out)
    assert rep["fp-lib-table"]["written"] and rep["sym-lib-table"]["written"]
    after = lt.read_table(fp_table)
    text = fp_table.read_text(encoding="utf-8")
    assert after.rows[:3] == foreign.rows and after.version == 7
    for row in foreign.rows:
        assert row.raw in text                                   # byte-for-byte
    # the unknown item stays after the user's rows; our rows go at the end
    assert text.index("Qwiic") < text.index("(future_item (x))") < text.index('"SPS_parts"')
    row = _managed(fp_table)["SPS_parts"]
    assert row.descr == "kicad-eda managed" and row.uri == "${KIPRJMOD}/../proj/libs/SPS_parts.pretty"
    assert same(lt.expand_vars(row.uri, kiprjmod=out, env={})[0], proj / "libs" / "SPS_parts.pretty")
    assert list(_managed(out / "sym-lib-table")) == ["SPS_parts"]

    before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in out.iterdir()}
    rep2 = r.materialize(out)
    assert not rep2["fp-lib-table"]["written"] and not rep2["sym-lib-table"]["written"]
    assert {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in out.iterdir()} == before


def test_materialize_replaces_and_removes_only_its_own_rows(tmp_path, proj, stock):
    out = tmp_path / "out"
    out.mkdir()
    (out / "fp-lib-table").write_text(FOREIGN_FP, encoding="utf-8")
    foreign = lt.read_table(out / "fp-lib-table").rows
    resolver(proj, stock, BLOCK, project_dir=out).materialize(out)
    make_lib(proj / "v2" / "SPS_parts.pretty")
    moved = {"footprints": {"SPS_parts": "v2/SPS_parts.pretty"}}
    rep = resolver(proj, stock, moved, project_dir=out).materialize(out)
    assert rep["fp-lib-table"]["managed"] == ["SPS_parts"]
    assert _managed(out / "fp-lib-table")["SPS_parts"].uri.endswith("/proj/v2/SPS_parts.pretty")
    assert rep["sym-lib-table"]["removed"] == ["SPS_parts"]       # dropped from the block
    assert _managed(out / "sym-lib-table") == {}
    # no netlist (None): the tables are read as KiCad reads them, so nothing is stale
    assert resolver(proj, stock, None, project_dir=out).materialize(out)["fp-lib-table"][
        "removed"] == []
    rep = resolver(proj, stock, {}, project_dir=out).materialize(out)   # block-less netlist
    assert rep["fp-lib-table"]["removed"] == ["SPS_parts"]
    assert lt.read_table(out / "fp-lib-table").rows == foreign     # back to the user's rows
    assert (out / "fp-lib-table").read_text(encoding="utf-8") == lt.format_table(
        lt.parse_table(FOREIGN_FP))                                # items in their places


def test_materialize_into_the_project_dir_skips_rows_already_there(proj, stock):
    table = proj / "sym-lib-table"
    stamp = (table.read_bytes(), table.stat().st_mtime_ns)
    rep = resolver(proj, stock, {"footprints": BLOCK["footprints"]}).materialize(proj)
    assert (table.read_bytes(), table.stat().st_mtime_ns) == stamp   # untouched
    assert not rep["sym-lib-table"]["written"]
    assert set(rep["sym-lib-table"]["skipped"]) == {"ProjSym", "Device"}
    assert list(_managed(proj / "fp-lib-table")) == ["SPS_parts"]


def test_materialize_elsewhere_copies_project_rows_with_rebased_uris(tmp_path, stock):
    d, out = tmp_path / "imp", tmp_path / "build" / "out"
    d.mkdir(), out.mkdir(parents=True)
    (d / "fp-lib-table").write_text(IMPORT_FP, encoding="utf-8")
    make_lib(d / "eDP_adapter_dvt1-import-fps.pretty")
    lib = make_lib(tmp_path / "abs" / "Abs.kicad_sym")
    r = resolver(d, stock, {"symbols": {"Abs": str(lib)}})
    r.materialize(out, footprints=["eDP_adapter_dvt1-import-fps", "Resistor_SMD"])
    rows = _managed(out / "fp-lib-table")
    assert list(rows) == ["eDP_adapter_dvt1-import-fps"]        # filter; stock never copied
    row = rows["eDP_adapter_dvt1-import-fps"]
    assert row.import_cache and row.uri == "${KIPRJMOD}/../../imp/eDP_adapter_dvt1-import-fps.pretty"
    assert _managed(out / "sym-lib-table")["Abs"].uri == lib.as_posix()   # absolute stays absolute


def test_materialize_keeps_unknown_vars_and_falls_back_to_absolute(tmp_path, stock, monkeypatch):
    d, out = tmp_path / "p", tmp_path / "out"
    d.mkdir(), out.mkdir()
    (d / "fp-lib-table").write_text(
        '(fp_lib_table (lib (name "Ext")(type "KiCad")(uri "${MYLIBS}/Ext.pretty")'
        '(options "")(descr "")))', encoding="utf-8")
    lib = make_lib(d / "libs" / "Own.kicad_sym")
    r = resolver(d, stock, {"symbols": {"Own": "libs/Own.kicad_sym"}})
    assert any("${MYLIBS}" in e for e in r.errors())

    def other_drive(path, start=None):
        raise ValueError("path is on mount 'D:', start on mount 'C:'")
    monkeypatch.setattr(lt.os.path, "relpath", other_drive)
    r.materialize(out)
    assert _managed(out / "fp-lib-table")["Ext"].uri == "${MYLIBS}/Ext.pretty"   # verbatim
    assert _managed(out / "sym-lib-table")["Own"].uri == lib.as_posix()


@pytest.mark.parametrize("uri, flag, needle", [
    ("other.pretty", "", "pointing to"),
    ("{lib}", "(disabled)", "disabled there"),        # the same library, but KiCad skips it
])
def test_materialize_refuses_a_conflicting_unmanaged_row(uri, flag, needle, tmp_path, proj, stock):
    out = tmp_path / "out"
    out.mkdir()
    uri = uri.format(lib=(proj / "libs" / "SPS_parts.pretty").as_posix())
    clash = (f'(fp_lib_table (lib (name "SPS_parts")(type "KiCad")(uri "{uri}")(options "")'
             f'(descr ""){flag}))\n')
    (out / "fp-lib-table").write_text(clash, encoding="utf-8")
    with pytest.raises(lt.LibTableError, match=f"SPS_parts.*unmanaged.*{needle}"):
        resolver(proj, stock).materialize(out)
    assert (out / "fp-lib-table").read_text(encoding="utf-8") == clash
    assert not (out / "sym-lib-table").exists()                  # nothing half-written


def test_table_file_errors_name_the_file_and_a_failed_write_leaves_no_debris(tmp_path,
                                                                             monkeypatch):
    table = tmp_path / "fp-lib-table"
    table.write_text("(fp_lib_table (lib", encoding="utf-8")
    with pytest.raises(lt.LibTableError, match="fp-lib-table"):
        lt.read_table(table)
    with pytest.raises(lt.LibTableError, match="cannot read library table"):
        lt.read_table(tmp_path / "missing" / "sym-lib-table")
    table.write_text(K9_FP, encoding="utf-8")

    def locked(src, dst):
        raise PermissionError("the file is open in another program")
    monkeypatch.setattr(lt.os, "replace", locked)
    with pytest.raises(PermissionError):
        lt.write_table(table, lt.LibTable("fp_lib_table", (lt.LibRow("X", "x.pretty"),)))
    assert [p.name for p in tmp_path.iterdir()] == ["fp-lib-table"]
    assert table.read_text(encoding="utf-8") == K9_FP


def test_write_minimal_kicad_pro_never_overwrites(tmp_path):
    pro = tmp_path / "board.kicad_pro"
    assert lt.write_minimal_kicad_pro(pro) is True
    assert json.loads(pro.read_text(encoding="utf-8")) == {
        "meta": {"filename": "board.kicad_pro", "version": 1}}
    pro.write_text('{"meta": {"filename": "board.kicad_pro"}, "x": 1}', encoding="utf-8")
    assert lt.LibraryResolver.write_minimal_kicad_pro(pro) is False
    assert json.loads(pro.read_text(encoding="utf-8"))["x"] == 1
    with pytest.raises(ValueError, match="kicad_pro"):
        lt.write_minimal_kicad_pro(tmp_path / "board.json")


# --- 5. stdlib only ------------------------------------------------------------------
def test_module_imports_only_the_standard_library_and_loads_without_site_packages():
    tree = ast.parse((TOOLS / "libtable.py").read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "no relative imports"
            names.add(node.module.split(".")[0])
    assert names and names <= set(sys.stdlib_module_names), names - set(sys.stdlib_module_names)
    code = ("import sys; sys.path.insert(0, sys.argv[1]); import libtable; "
            "extra = {m.split('.')[0] for m in sys.modules} - set(sys.stdlib_module_names)"
            " - {'libtable', '__main__'}; print(sorted(extra))")
    proc = subprocess.run([sys.executable, "-I", "-S", "-c", code, str(TOOLS)],
                          capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "[]"


# --- live tier ------------------------------------------------------------------------
FAMILIES = {"9": lambda v: v[0] == 9, "10": lambda v: v[0] == 10 and v[1] < 99,
            "11": lambda v: v >= (10, 99, 0)}


def _install(family: str) -> env.KiCadInstall:
    for inst in env.discover():
        if FAMILIES[family](inst.version_tuple):
            return inst
    pytest.skip(f"no KiCad {family} install")


@pytest.mark.parametrize("family", sorted(FAMILIES))
def test_live_full_templates_parse(family):
    inst = _install(family)
    for name in ("sym-lib-table", "fp-lib-table"):
        path = inst.share / "template" / name
        text = path.read_text(encoding="utf-8")
        table = lt.read_table(path)
        assert len(table.rows) == text.count("(lib (name") > 50
        assert len({r.name for r in table.rows}) == len(table.rows)
        assert all(r.uri.startswith("${KICAD") and r.is_kicad for r in table.rows)


T_SYM = """(kicad_symbol_lib (version 20241209) (generator "kicad-eda")
 (symbol "R" (pin_numbers (hide yes)) (exclude_from_sim no) (in_bom yes) (on_board yes)
  %PROPS% (symbol "R_1_1" %PINS%) (embedded_fonts no)))
"""
T_PROPS = ' '.join(f'(property "{k}" "{v}" (at 0 0 0) (effects (font (size 1.27 1.27)) (hide yes)))'
                   for k, v in [("Reference", "R"), ("Value", "R"), ("Footprint", ""),
                                ("Datasheet", "~"), ("Description", "")])
T_PINS = ' '.join(f'(pin passive line (at 0 {y} {a}) (length 1.27) (name "~" (effects (font '
                  f'(size 1.27 1.27)))) (number "{n}" (effects (font (size 1.27 1.27)))))'
                  for n, y, a in [("1", 3.81, 270), ("2", -3.81, 90)])
T_SCH = """(kicad_sch (version 20250114) (generator "kicad-eda") (generator_version "9.0")
 (uuid "6b0c2f1e-1d7a-4c51-9b0e-3f0a1d2c4e01") (paper "A4")
 (lib_symbols (symbol "T:R" (pin_numbers (hide yes)) (exclude_from_sim no) (in_bom yes) (on_board yes)
  %PROPS% (symbol "R_1_1" %PINS%) (embedded_fonts no)))
 (symbol (lib_id "T:R") (at 50.8 38.1 0) (unit 1) (exclude_from_sim no) (in_bom yes) (on_board yes)
  (dnp no) (uuid "6b0c2f1e-1d7a-4c51-9b0e-3f0a1d2c4e02")
  (property "Reference" "R1" (at 50.8 30.48 0) (effects (font (size 1.27 1.27))))
  (property "Value" "10k" (at 50.8 45.72 0) (effects (font (size 1.27 1.27))))
  (property "Footprint" "T:R" (at 50.8 38.1 0) (effects (font (size 1.27 1.27)) (hide yes)))
  (pin "1" (uuid "6b0c2f1e-1d7a-4c51-9b0e-3f0a1d2c4e03"))
  (pin "2" (uuid "6b0c2f1e-1d7a-4c51-9b0e-3f0a1d2c4e04"))
  (instances (project "t" (path "/6b0c2f1e-1d7a-4c51-9b0e-3f0a1d2c4e01" (reference "R1") (unit 1)))))
 (no_connect (at 50.8 34.29) (uuid "6b0c2f1e-1d7a-4c51-9b0e-3f0a1d2c4e05"))
 (no_connect (at 50.8 41.91) (uuid "6b0c2f1e-1d7a-4c51-9b0e-3f0a1d2c4e06"))
 (sheet_instances (path "/" (page "1"))) (embedded_fonts no))
"""


def _erc_library_warnings(cli: Path, sch: Path) -> list[str]:
    report = sch.with_name("erc.json")
    proc = subprocess.run([str(cli), "sch", "erc", "--severity-all", "--format", "json",
                           "-o", str(report), str(sch)], capture_output=True, text=True,
                          stdin=subprocess.DEVNULL, timeout=LIVE_TIMEOUT_S)
    assert report.is_file(), (proc.returncode, proc.stdout, proc.stderr)
    data = json.loads(report.read_text(encoding="utf-8"))
    return [v["description"] for s in data["sheets"] for v in s["violations"]
            if v["type"] in ("lib_symbol_issues", "footprint_link_issues")]


@pytest.mark.parametrize("family", sorted(FAMILIES))
def test_live_kicad_erc_honours_materialized_tables(family, tmp_path):
    inst = _install(family)
    sub = lambda s: s.replace("%PROPS%", T_PROPS).replace("%PINS%", T_PINS)  # noqa: E731
    (tmp_path / "libs").mkdir()
    (tmp_path / "libs" / "T.kicad_sym").write_text(sub(T_SYM), encoding="utf-8")
    pretty = tmp_path / "libs" / "T.pretty"
    pretty.mkdir()
    (pretty / "R.kicad_mod").write_text(
        '(footprint "R" (version 20241229) (generator "kicad-eda") (layer "F.Cu") (attr smd)\n'
        ' (pad "1" smd rect (at -0.8 0) (size 0.8 0.9) (layers "F.Cu" "F.Paste" "F.Mask"))\n'
        ' (pad "2" smd rect (at 0.8 0) (size 0.8 0.9) (layers "F.Cu" "F.Paste" "F.Mask")))\n',
        encoding="utf-8")
    sch = tmp_path / "t.kicad_sch"
    sch.write_text(sub(T_SCH), encoding="utf-8")
    assert len(_erc_library_warnings(inst.kicad_cli, sch)) == 2        # control: unknown 'T'
    # a hand-written KiCad 9 style row already in the project table must survive
    (tmp_path / "fp-lib-table").write_text(
        "(fp_lib_table\n  (lib (name Other)(type KiCad)(uri ${KIPRJMOD}/libs/T.pretty)"
        '(options "")(descr "hand row"))\n)\n', encoding="utf-8")
    block = {"symbols": {"T": "libs/T.kicad_sym"}, "footprints": {"T": "libs/T.pretty"}}
    r = lt.LibraryResolver(tmp_path, block, None, None, env={})
    assert r.errors() == []
    r.materialize(tmp_path)
    assert lt.write_minimal_kicad_pro(tmp_path / "t.kicad_pro")
    assert _erc_library_warnings(inst.kicad_cli, sch) == []
    assert [x.name for x in lt.read_table(tmp_path / "fp-lib-table").rows] == ["Other", "T"]
    seen = lt.LibraryResolver(tmp_path, None, None, None, env={})   # dir-only = KiCad's view
    assert (seen.source("T"), seen.errors()) == ("project", [])


CHILD = """import json, sys
sys.path.insert(0, sys.argv[1])
import kicad_quiet  # noqa: F401  (D0: no modal wx dialogs in a KiCad-python child)
import libtable
stock = json.loads(sys.argv[4])
r = libtable.LibraryResolver(sys.argv[2], json.loads(sys.argv[3]), stock[0], stock[1], env={})
m = r.library_map()
print(json.dumps({"map": m, "fp": libtable.lookup(m, "footprint", "SPS_parts")}))
"""


def test_live_kicad_bundled_python_imports_and_resolves(proj, stock):
    py = next((i.python for i in env.discover() if i.python is not None), None)
    if py is None:
        pytest.skip("no KiCad bundled Python")
    argv = [str(py), "-c", CHILD, str(TOOLS), str(proj), json.dumps(BLOCK),
            json.dumps([str(stock[0]), str(stock[1])])]
    proc = subprocess.run(argv, capture_output=True, text=True, stdin=subprocess.DEVNULL,
                          timeout=LIVE_TIMEOUT_S)
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    got = json.loads(proc.stdout.strip().splitlines()[-1])
    assert got["map"] == json.loads(json.dumps(resolver(proj, stock).library_map()))
    assert same(got["fp"], proj / "libs" / "SPS_parts.pretty")
