"""libtable — the D1 nickname resolver (netlist ``libraries`` block > project
table > stock), materialize, library_generator and the consumer helpers. The
table syntax itself (libtable_sexp) is tested in test_libtable_sexp.

Unit tier (no KiCad). Live tier (skipped without KiCad): the full templates
parse; ``kicad-cli sch erc`` honours materialized tables beside a minimal
.kicad_pro; KiCad's bundled Python (3.11 on KiCad 9) imports both modules and
resolves the same map.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

import kicad_env as env
import libtable as lt
from test_libtable_sexp import (FAMILIES, IMPORT_FP, IMPORT_SYM, K9_FP,  # verbatim tables
                                K9_SYM, kicad_install)

TOOLS = Path(__file__).resolve().parents[1] / "server" / "tools"
LIVE_TIMEOUT_S = 120


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


def row(name: str, uri: str, tail: str = "") -> str:
    """One quoted-syntax table row."""
    return f'(lib (name "{name}")(type "KiCad")(uri "{uri}")(options "")(descr ""){tail})'


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
        '  (lib (name "ProjSym")(type "KiCad")(uri "${KIPRJMOD}/libs/ProjSym.kicad_sym")'
        '(options "")(descr ""))\n'
        '  (lib (name "Device")(type "KiCad")(uri "${KIPRJMOD}/libs/MyDevice.kicad_sym")'
        '(options "")(descr "shadows stock"))\n'
        ')\n', encoding="utf-8")
    return d


BLOCK = {"symbols": {"SPS_parts": "libs/SPS_parts.kicad_sym"},
         "footprints": {"SPS_parts": "${KIPRJMOD}/libs/SPS_parts.pretty"}}


def resolver(proj: Path, stock: tuple[Path, Path], block: dict = BLOCK,
             **kw) -> lt.LibraryResolver:
    kw.setdefault("env", {})
    return lt.LibraryResolver(proj, block, stock[0], stock[1], **kw)


def dir_resolver(d: Path, stock: tuple[Path, Path], **kw) -> lt.LibraryResolver:
    """No netlist: the directory's tables as KiCad reads them."""
    kw.setdefault("env", {})
    return lt.LibraryResolver.for_directory(d, stock[0], stock[1], **kw)


# --- variable expansion --------------------------------------------------------
@pytest.mark.parametrize("n", [6, 7, 8, 9, 10, 11])
def test_versioned_stock_vars_alias_the_chosen_install(n: int, tmp_path: Path):
    syms, fps, models = tmp_path / "s", tmp_path / "f", tmp_path / "m"
    kw = dict(symbols_dir=syms, footprints_dir=fps, models_dir=models, env={})
    assert lt.expand_vars(f"${{KICAD{n}_SYMBOL_DIR}}/D.kicad_sym", **kw) == (
        f"{syms}/D.kicad_sym", ())
    assert lt.expand_vars(f"$(KICAD{n}_FOOTPRINT_DIR)/R.pretty", **kw) == (f"{fps}/R.pretty", ())
    assert lt.expand_vars(f"${{KICAD{n}_3DMODEL_DIR}}/a.step", **kw) == (f"{models}/a.step", ())


def test_kiprjmod_legacy_env_and_unresolved_vars(tmp_path: Path):
    kw = dict(kiprjmod=tmp_path / "p", symbols_dir=tmp_path / "s",
              footprints_dir=tmp_path / "f",
              env={"MYLIBS": "/opt/libs", "KICAD9_SYMBOL_DIR": "/wrong/install"})
    assert lt.expand_vars("${KIPRJMOD}/libs/x.pretty", **kw) == (
        f"{tmp_path / 'p'}/libs/x.pretty", ())
    assert lt.expand_vars("${KISYSMOD}/R.pretty", **kw)[0] == f"{tmp_path / 'f'}/R.pretty"
    assert lt.expand_vars("${KICAD_SYMBOL_DIR}/D.kicad_sym", **kw)[0] == (
        f"{tmp_path / 's'}/D.kicad_sym")
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
    r = dir_resolver(d, stock)
    assert same(r.footprint_lib_path("Resistor_SMD"), stock[1] / "Resistor_SMD.pretty")
    assert r.source("Audio_Module") == "project"           # a project row, not stock
    assert any("Connector_BarrelJack" in e for e in r.errors())   # absent from this stock


# --- resolution order ----------------------------------------------------
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
        f'(sym_lib_table {row("ProjSym", "${KIPRJMOD}/libs/ProjSym.kicad_sym", "(disabled)")})',
        encoding="utf-8")
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
    r = dir_resolver(d, stock)
    hit = r.resolve("footprint", "eDP_adapter_dvt1-import-fps")
    assert hit.source == "project" and hit.import_cache and hit.exists
    assert same(hit.path, d / "eDP_adapter_dvt1-import-fps.pretty")
    assert r.source("yuzuki-import-syms") == "project"
    assert r.errors() == [f"footprint library 'pcb_watch-import-fps' ({d / 'fp-lib-table'}): "
                          f"{d / 'pcb_watch-import-fps.pretty'} does not exist"]


def test_disabled_project_row_hides_the_nickname_like_kicad(tmp_path, stock):
    d = tmp_path / "p"
    d.mkdir()
    (d / "sym-lib-table").write_text(f'(sym_lib_table {row("power", "x.kicad_sym", "(disabled)")})',
                                     encoding="utf-8")
    r = dir_resolver(d, stock)
    assert r.resolve("symbol", "power") is None             # not the stock one either
    with pytest.raises(lt.LibraryNotFoundError, match="disabled"):
        r.symbol_lib_path("power")
    assert r.errors() == []                                   # a disabled row is a choice
    out, other = tmp_path / "out", tmp_path / "other"
    out.mkdir(), other.mkdir()
    r.materialize(out)
    assert _managed(out / "sym-lib-table")["power"].disabled  # out_dir hides it too
    (other / "sym-lib-table").write_text(                     # same library, enabled there
        f'(sym_lib_table {row("power", (d / "x.kicad_sym").as_posix())})', encoding="utf-8")
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
    r = dir_resolver(d, stock)                 # e.g. a consumer holding a schematic dir
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
    assert dir_resolver(d, stock).source("Device") == "stock"


# --- dir-only mode is explicit: for_directory; None is refused -----------------------
def test_for_directory_reads_the_tables_as_kicad_does(tmp_path, stock, monkeypatch):
    d, ext = tmp_path / "sch", tmp_path / "ext"
    make_lib(d / "libs" / "Own.pretty"), make_lib(ext / "Ext.kicad_sym")
    lt.write_table(d / "fp-lib-table", lt.LibTable("fp_lib_table", (
        lt.LibRow("Own", "${KIPRJMOD}/libs/Own.pretty", descr=lt.MANAGED_DESCR),)))
    lt.write_table(d / "sym-lib-table", lt.LibTable("sym_lib_table", (
        lt.LibRow("Ext", "${MYLIBS}/Ext.kicad_sym"),)))
    r = lt.LibraryResolver.for_directory(d, *stock, env={"MYLIBS": str(ext)})
    assert r.dir_only and r.netlist_dir == r.project_dir == d
    assert r.source("Own") == "project"            # a managed row counts: no netlist owns it
    assert same(r.symbol_lib_path("Ext"), ext / "Ext.kicad_sym")     # env= honoured
    assert r.source("Device") == "stock" and r.errors() == []
    m = r.library_map()
    assert m["netlist_dir"] == m["project_dir"] == str(d) and m["footprints"]["Own"]["managed"]
    bare = lt.LibraryResolver.for_directory(str(d), None, None, env={})
    errs = bare.errors()
    assert [e for e in errs if "${MYLIBS}" in e and "undefined" in e] == errs != []
    assert bare.resolve("symbol", "Device") is None                   # no stock configured
    monkeypatch.setenv("MYLIBS", str(ext))                            # env=None: os.environ
    assert lt.LibraryResolver.for_directory(d, None, None).errors() == []


def test_constructor_refuses_none_and_points_to_for_directory(proj, stock):
    for kw in ({}, {"project_dir": proj, "env": {}}):
        with pytest.raises(TypeError, match="for_directory"):
            lt.LibraryResolver(proj, None, *stock, **kw)
    assert not lt.LibraryResolver(proj, {}, *stock, env={}).dir_only    # block-less netlist
    assert not lt.LibraryResolver.for_design(proj, {"name": "x"}, *stock, env={}).dir_only
    with pytest.raises(lt.LibTableError, match="object"):               # still a block error
        lt.LibraryResolver(proj, [], *stock, env={})


@pytest.mark.parametrize("kw", [{"symbols": "Device"}, {"footprints": "SPS_parts"},
                                {"symbols": b"power"}])
def test_a_bare_str_of_nicknames_is_a_type_error(kw, tmp_path, proj, stock):
    r = resolver(proj, stock)
    with pytest.raises(TypeError, match=r"(symbols|footprints)=.*bare (str|bytes).*\["):
        r.errors(**kw)
    out = tmp_path / "out"
    out.mkdir()
    with pytest.raises(TypeError, match="bare"):
        r.materialize(out, **kw)
    assert list(out.iterdir()) == []                                  # nothing written
    assert r.errors(symbols=["Device"], footprints=("SPS_parts",)) == []
    assert r.errors(symbols=(n for n in ["Nope"])) != []               # any other iterable
    rep = r.materialize(out, symbols={"SPS_parts"}, footprints=[])
    assert rep["sym-lib-table"]["managed"] == ["SPS_parts"] and not rep["fp-lib-table"]["written"]


def test_errors_cover_missing_files_wrong_kinds_vars_and_duplicates(tmp_path, stock):
    d = tmp_path / "p"
    make_lib(d / "libs" / "Dir.kicad_sym.pretty")
    (d / "libs" / "File.pretty").write_text("not a dir", encoding="utf-8")
    (d / "sym-lib-table").write_text(
        f'(sym_lib_table\n {row("A", "${MYLIBS}/a.kicad_sym")}\n {row("A", "a2.kicad_sym")}\n'
        f' {row("D", "${KIPRJMOD}/libs/Dir.kicad_sym.pretty")}\n)', encoding="utf-8")
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
        dir_resolver(tmp_path, stock)
    (tmp_path / "fp-lib-table").write_text(K9_SYM, encoding="utf-8")
    with pytest.raises(lt.LibTableError, match="expected \\(fp_lib_table"):
        dir_resolver(tmp_path, stock)
    nl = tmp_path / "nl"
    nl.mkdir()
    with pytest.raises(lt.LibTableError, match="expected \\(fp_lib_table"):
        dir_resolver(nl, stock).materialize(tmp_path)


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


# --- library_generator: 'generated' parts for the review (F2-08 / F5-06) ------------
GEN_SYM = ('(kicad_symbol_lib\r\n\t(version 20241209)\r\n\t(generator "{g}")\r\n'
           '\t(generator_version "9.0")\r\n\t(symbol "X")\r\n)\r\n')
GEN_FP = '(footprint "{n}" (version 20241229) (generator "{g}") (layer "F.Cu"))\n'


def test_library_generator_true_false_and_mixed_pretty(tmp_path: Path):
    def write(path: Path, text: str) -> Path:
        path.write_text(text, encoding="utf-8")
        return path
    gen = lt.KICAD_EDA_GENERATOR
    assert gen == "kicad-eda"
    assert lt.library_generator(write(tmp_path / "a.kicad_sym", GEN_SYM.format(g=gen))) == gen
    assert lt.library_generator(str(tmp_path / "a.kicad_sym")) == gen
    bom = tmp_path / "bom.kicad_sym"
    bom.write_text(GEN_SYM.format(g=gen), encoding="utf-8-sig")
    assert lt.library_generator(bom) == gen
    other = write(tmp_path / "b.kicad_sym", GEN_SYM.format(g="kicad_symbol_editor"))
    assert lt.library_generator(other) is None
    assert lt.library_generator(make_lib(tmp_path / "c.kicad_sym")) is None   # no generator
    pretty = tmp_path / "Gen.pretty"
    pretty.mkdir()
    for n in ("A", "B"):
        write(pretty / f"{n}.kicad_mod", GEN_FP.format(n=n, g=gen))
    write(pretty / "README.txt", "not a footprint")
    assert lt.library_generator(pretty) == gen                     # every .kicad_mod
    assert lt.library_generator(pretty / "A.kicad_mod") == gen     # one part (F5-06)
    write(pretty / "C.kicad_mod", GEN_FP.format(n="C", g="pcbnew"))
    assert lt.library_generator(pretty) is None                    # mixed
    assert lt.library_generator(pretty / "A.kicad_mod") == gen
    (tmp_path / "Empty.pretty").mkdir()
    for p in (tmp_path / "Empty.pretty", tmp_path / "missing.pretty", tmp_path / "gone.kicad_sym",
              tmp_path, write(tmp_path / "x.txt", GEN_FP.format(n="x", g=gen))):
        assert lt.library_generator(p) is None, p
    authored = "".join(p.read_text(encoding="utf-8") for p in TOOLS.glob("*_author*.py"))
    assert f'"{gen}"' in authored                  # what fp_author / sym_author stamp


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
    bare = lt.LibraryResolver.for_directory(proj, None, None, env={}).library_map()
    assert bare["stock"] == {"symbols": None, "footprints": None}
    assert lt.lookup(bare, "symbol", "power") is None


# --- materialize ----------------------------------------------------------
FOREIGN_FP = """(fp_lib_table
  (version 7)
  (lib (name Battery)(type KiCad)(uri ${KICAD9_FOOTPRINT_DIR}/Battery.pretty)(options "")(descr "Battery and battery holder footprints"))
  (lib (name "Hand")   (type "KiCad")(uri "${KIPRJMOD}/hand/Hand.pretty")(options "")(descr "hand-authored")(disabled))
  (lib (name "Qwiic_Button-import-fps") (type "KiCad") (uri "${KIPRJMOD}/Qwiic_Button-import-fps.pretty") (options "kicad_import_cache=1") (descr ""))
  (future_item (x))
)
"""  # noqa: E501


def _managed(path: Path) -> dict[str, lt.LibRow]:
    return {r.name: r for r in lt.read_table(path).rows if r.managed}


def test_materialize_writes_nothing_without_project_libraries(tmp_path, stock):
    nl, out = tmp_path / "nl", tmp_path / "out"
    nl.mkdir(), out.mkdir()
    for r in (dir_resolver(nl, stock), resolver(nl, stock, {})):
        rep = r.materialize(out)
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
    for foreign_row in foreign.rows:
        assert foreign_row.raw in text                           # byte-for-byte
    # the unknown item stays after the user's rows; our rows go at the end
    assert text.index("Qwiic") < text.index("(future_item (x))") < text.index('"SPS_parts"')
    managed = _managed(fp_table)["SPS_parts"]
    assert managed.descr == "kicad-eda managed"
    assert managed.uri == "${KIPRJMOD}/../proj/libs/SPS_parts.pretty"
    assert same(lt.expand_vars(managed.uri, kiprjmod=out, env={})[0],
                proj / "libs" / "SPS_parts.pretty")
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
    # no netlist: the tables are read as KiCad reads them, so nothing is stale
    assert dir_resolver(out, stock).materialize(out)["fp-lib-table"]["removed"] == []
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
    imported = rows["eDP_adapter_dvt1-import-fps"]
    assert imported.import_cache
    assert imported.uri == "${KIPRJMOD}/../../imp/eDP_adapter_dvt1-import-fps.pretty"
    assert _managed(out / "sym-lib-table")["Abs"].uri == lib.as_posix()   # absolute stays


def test_materialize_keeps_unknown_vars_and_falls_back_to_absolute(tmp_path, stock, monkeypatch):
    d, out = tmp_path / "p", tmp_path / "out"
    d.mkdir(), out.mkdir()
    (d / "fp-lib-table").write_text(f'(fp_lib_table {row("Ext", "${MYLIBS}/Ext.pretty")})',
                                    encoding="utf-8")
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
    clash = f'(fp_lib_table {row("SPS_parts", uri, flag)})\n'
    (out / "fp-lib-table").write_text(clash, encoding="utf-8")
    with pytest.raises(lt.LibTableError, match=f"SPS_parts.*unmanaged.*{needle}"):
        resolver(proj, stock).materialize(out)
    assert (out / "fp-lib-table").read_text(encoding="utf-8") == clash
    assert not (out / "sym-lib-table").exists()                  # nothing half-written


@pytest.mark.parametrize("key, lib", [("footprints", "SPS_parts.pretty"),
                                      ("symbols", "SPS_parts.kicad_sym")])
def test_same_relative_uri_text_in_another_dir_is_a_clash(key, lib, tmp_path, proj, stock):
    """Repro: the out table's row has the block's exact URI text, but a relative /
    ${KIPRJMOD} URI resolves against the out dir: another library, not ours."""
    out = tmp_path / "out"
    make_lib(out / "libs" / lib)
    table = "fp-lib-table" if key == "footprints" else "sym-lib-table"
    text = f'({table.replace("-", "_")} {row("SPS_parts", BLOCK[key]["SPS_parts"])})\n'
    (out / table).write_text(text, encoding="utf-8")
    r = resolver(proj, stock, {key: BLOCK[key]})
    with pytest.raises(lt.LibTableError,
                       match=r"SPS_parts.*unmanaged.*pointing to .*out.*same URI text"):
        r.materialize(out)
    assert (out / table).read_text(encoding="utf-8") == text


def test_same_absolute_path_or_install_var_is_not_a_clash(tmp_path, proj, stock):
    out = tmp_path / "out"
    out.mkdir()
    lib = proj / "libs" / "SPS_parts.pretty"
    text = (f'(fp_lib_table {row("SPS_parts", lib.as_posix())}\n'
            f' {row("Ext", "${MYLIBS}/Ext.pretty")})\n')       # unresolved: text decides
    (out / "fp-lib-table").write_text(text, encoding="utf-8")
    (proj / "fp-lib-table").write_text(f'(fp_lib_table {row("Ext", "${MYLIBS}/Ext.pretty")})',
                                       encoding="utf-8")
    rep = resolver(proj, stock, {"footprints": {"SPS_parts": str(lib)}}).materialize(out)
    assert set(rep["fp-lib-table"]["skipped"]) == {"SPS_parts", "Ext"}
    assert not rep["fp-lib-table"]["written"]
    assert (out / "fp-lib-table").read_text(encoding="utf-8") == text
    link = tmp_path / "link"                       # another spelling of the same real path
    try:
        link.symlink_to(proj / "libs", target_is_directory=True)
    except (OSError, NotImplementedError) as e:
        pytest.skip(f"cannot create a symlink here: {e}")
    (out / "fp-lib-table").write_text(
        f'(fp_lib_table {row("SPS_parts", (link / "SPS_parts.pretty").as_posix())})',
        encoding="utf-8")
    rep = resolver(proj, stock, {"footprints": BLOCK["footprints"]}).materialize(out)
    assert rep["fp-lib-table"]["skipped"] == {"SPS_parts": f"already in {out / 'fp-lib-table'} "
                                                           f"as an unmanaged row"}


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


# --- live tier (FAMILIES / kicad_install: test_libtable_sexp) ----------------------
T_SYM = """(kicad_symbol_lib (version 20241209) (generator "kicad-eda")
 (symbol "R" (pin_numbers (hide yes)) (exclude_from_sim no) (in_bom yes) (on_board yes)
  %PROPS% (symbol "R_1_1" %PINS%) (embedded_fonts no)))
"""
T_PROPS = ' '.join(f'(property "{k}" "{v}" (at 0 0 0) (effects (font (size 1.27 1.27)) '
                   f'(hide yes)))'
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
"""  # noqa: E501


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
    inst = kicad_install(family)
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
    seen = lt.LibraryResolver.for_directory(tmp_path, None, None, env={})   # KiCad's view
    assert (seen.source("T"), seen.errors()) == ("project", [])
    assert lt.library_generator(pretty) == lt.library_generator(tmp_path / "libs" /
                                                                "T.kicad_sym") == "kicad-eda"


CHILD = """import json, sys
sys.path.insert(0, sys.argv[1])
import kicad_quiet  # noqa: F401  (D0: no modal wx dialogs in a KiCad-python child)
import libtable
stock = json.loads(sys.argv[4])
r = libtable.LibraryResolver(sys.argv[2], json.loads(sys.argv[3]), stock[0], stock[1], env={})
d = libtable.LibraryResolver.for_directory(sys.argv[2], stock[0], stock[1], env={})
m = r.library_map()
print(json.dumps({"map": m, "fp": libtable.lookup(m, "footprint", "SPS_parts"),
                  "dir": d.library_map(), "gen": libtable.library_generator(sys.argv[5]),
                  "version": list(sys.version_info[:2])}))
"""


def test_live_kicad_bundled_python_imports_and_resolves(tmp_path, proj, stock):
    py = next((i.python for i in env.discover() if i.python is not None), None)
    if py is None:
        pytest.skip("no KiCad bundled Python")
    gen = tmp_path / "gen.kicad_mod"
    gen.write_text(GEN_FP.format(n="gen", g="kicad-eda"), encoding="utf-8")
    argv = [str(py), "-c", CHILD, str(TOOLS), str(proj), json.dumps(BLOCK),
            json.dumps([str(stock[0]), str(stock[1])]), str(gen)]
    proc = subprocess.run(argv, capture_output=True, text=True, stdin=subprocess.DEVNULL,
                          timeout=LIVE_TIMEOUT_S)
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    got = json.loads(proc.stdout.strip().splitlines()[-1])
    assert got["map"] == json.loads(json.dumps(resolver(proj, stock).library_map()))
    assert got["dir"] == json.loads(json.dumps(dir_resolver(proj, stock).library_map()))
    assert same(got["fp"], proj / "libs" / "SPS_parts.pretty")
    assert got["gen"] == "kicad-eda" and got["version"] >= [3, 11]
