"""project_libs: the single entry point every consumer uses for project
libraries (ROADMAP D1): load (never raises), materialize, write_map,
footprint_digest, sources / source_callback, pins_dirs, the SWIG footprint
limit, default nicknames, register_in_block (atomic, immutable) and the lint
context. Pure unit tier on tmp libraries: no KiCad.
"""
from __future__ import annotations

import ast
import dataclasses
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import kicad_env
import libtable as lt
import lint_netlist
import project_libs as pl
import review_report as rr
import review_sources as rs
import toolchain as tc
from lint_context import LintContext

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "server" / "project_libs.py"
TOOLS = ROOT / "server" / "tools"

KICAD_EDA_SYM = ('(kicad_symbol_lib\n\t(version 20241209)\n\t(generator "kicad-eda")\n'
                 '\t(symbol "LG290P")\n)\n')
EDITOR_SYM = ('(kicad_symbol_lib (version 20241209) (generator "kicad_symbol_editor")\n'
              '  (symbol "R"))\n')
KICAD_EDA_FP = '(footprint "{n}" (version 20241229) (generator "kicad-eda") (layer "F.Cu"))\n'
EDITOR_FP = '(footprint "{n}" (version 20241229) (generator "pcbnew") (layer "F.Cu"))\n'
LG290P_FP = "SPS_parts:LG290P"
STOCK_R_FP = "Resistor_SMD:R_0603_1608Metric"


# --- helpers / fixtures --------------------------------------------------------------
def sym_lib(path: Path, text: str = EDITOR_SYM) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def fp_lib(path: Path, footprints: dict[str, str]) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    for name, template in footprints.items():
        (path / f"{name}.kicad_mod").write_text(template.format(n=name), encoding="utf-8")
    return path


def comp(ref: str, lib_id: str | None, footprint: str | None) -> dict:
    c: dict = {"ref": ref, "value": ref}
    if lib_id is not None:
        c["lib_id"] = lib_id
    if footprint is not None:
        c["footprint"] = footprint
    return c


def project_design(**extra) -> dict:
    return {"name": "sps_node", "title": "SPS node",
            "libraries": {"symbols": {"SPS_parts": "libs/SPS_parts.kicad_sym"},
                          "footprints": {"SPS_parts": "libs/SPS_parts.pretty"}},
            "components": [comp("U1", "SPS_parts:LG290P", LG290P_FP),
                           comp("R1", "Device:R", STOCK_R_FP)],
            "nets": {"GND": ["U1.10", "R1.2"]}, **extra}


def write_netlist(path: Path, design: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(design, indent=2), encoding="utf-8")
    return path


@pytest.fixture
def stock(tmp_path: Path) -> tuple[Path, Path]:
    syms, fps = tmp_path / "stock" / "symbols", tmp_path / "stock" / "footprints"
    sym_lib(syms / "Device.kicad_sym")
    sym_lib(syms / "power.kicad_sym")
    fp_lib(fps / "Resistor_SMD.pretty", {"R_0603_1608Metric": EDITOR_FP})
    fp_lib(fps / "MountingHole.pretty", {"MountingHole_3.2mm_M3": EDITOR_FP})
    return syms, fps


@pytest.fixture
def netlist(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    sym_lib(proj / "libs" / "SPS_parts.kicad_sym", KICAD_EDA_SYM)
    fp_lib(proj / "libs" / "SPS_parts.pretty", {"LG290P": KICAD_EDA_FP})
    return write_netlist(proj / "netlist.json", project_design())


def load(netlist: Path, stock: tuple[Path, Path], design: object = None) -> pl.ProjectLibs:
    return pl.load(netlist, design, symbols_dir=str(stock[0]), footprints_dir=str(stock[1]))


def row(name: str, uri: str) -> str:
    return f'(lib (name "{name}")(type "KiCad")(uri "{uri}")(options "")(descr ""))'


# --- load ----------------------------------------------------------------------------
def test_load_reads_the_netlist_and_its_block(netlist: Path, stock) -> None:
    libs = load(netlist, stock)
    assert libs.error == "" and libs.ok
    assert isinstance(libs.resolver, lt.LibraryResolver)
    assert libs.netlist == str(netlist.resolve()) and libs.netlist_dir == netlist.parent.resolve()
    assert (libs.stock_symbols, libs.stock_footprints) == (str(stock[0]), str(stock[1]))
    assert libs.resolver.resolve(lt.FOOTPRINT, "SPS_parts").source == lt.NETLIST
    assert libs.resolver.resolve(lt.FOOTPRINT, "Resistor_SMD").source == lt.STOCK


def test_load_uses_the_given_design_without_reading_the_file(tmp_path: Path, stock) -> None:
    missing = tmp_path / "nowhere" / "netlist.json"
    libs = load(missing, stock, {"components": []})
    assert libs.ok and libs.netlist_dir == missing.parent.resolve()


def test_load_is_a_frozen_dataclass(netlist: Path, stock) -> None:
    libs = load(netlist, stock)
    with pytest.raises(dataclasses.FrozenInstanceError):
        libs.error = "x"  # type: ignore[misc]
    assert [f.name for f in dataclasses.fields(pl.ProjectLibs)] == [
        "netlist", "netlist_dir", "resolver", "error", "stock_symbols", "stock_footprints"]


def test_load_defaults_the_stock_dirs_to_toolchain_at_call_time(netlist: Path, stock,
                                                               monkeypatch) -> None:
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(tc, "symbols_dir", lambda explicit="": calls.append(("s", explicit))
                        or str(stock[0]))
    monkeypatch.setattr(tc, "footprints_dir", lambda explicit="": calls.append(("f", explicit))
                        or str(stock[1]))
    libs = pl.load(netlist)
    assert calls == [("s", ""), ("f", "")]
    assert (libs.stock_symbols, libs.stock_footprints) == (str(stock[0]), str(stock[1]))


def test_load_without_any_kicad_is_not_an_error(netlist: Path, monkeypatch) -> None:
    monkeypatch.setattr(tc, "symbols_dir", lambda explicit="": None)
    monkeypatch.setattr(tc, "footprints_dir", lambda explicit="": None)
    libs = pl.load(netlist)
    assert libs.ok and (libs.stock_symbols, libs.stock_footprints) == ("", "")
    assert libs.resolver.stock_dirs == {lt.SYMBOL: None, lt.FOOTPRINT: None}


@pytest.mark.parametrize("block, needle", [
    ("libs/SPS_parts.pretty", "must be an object"),
    ({"symbols": {"SPS_parts": "x.kicad_sym"}, "models": {}}, "unknown key"),
    ({"footprints": {"bad:nick": "libs/x.pretty"}}, "invalid library nickname"),
    ({"footprints": {"SPS_parts": ""}}, "non-empty string"),
    ({"symbols": ["libs/x.kicad_sym"]}, "must be an object"),
])
def test_load_never_raises_on_a_malformed_block(netlist: Path, stock, block, needle) -> None:
    libs = load(netlist, stock, project_design(libraries=block))
    assert libs.resolver is None and not libs.ok
    assert needle in libs.error


def test_load_never_raises_on_missing_or_bad_netlist_files(tmp_path: Path, stock) -> None:
    missing = load(tmp_path / "absent" / "netlist.json", stock)
    assert missing.resolver is None and "cannot read netlist" in missing.error
    bad = tmp_path / "bad" / "netlist.json"
    bad.parent.mkdir()
    bad.write_text("{ not json", encoding="utf-8")
    assert "cannot read netlist" in load(bad, stock).error
    listed = write_netlist(tmp_path / "list" / "netlist.json", [1, 2])
    assert "not a JSON object" in load(listed, stock).error
    assert "JSON object" in load(listed, stock, design=[1]).error


def test_load_never_raises_on_missing_declared_library_files(netlist: Path, stock) -> None:
    shutil.rmtree(netlist.parent / "libs" / "SPS_parts.pretty")
    libs = load(netlist, stock)
    assert libs.resolver is not None and not libs.ok
    assert "SPS_parts" in libs.error and "SPS_parts.pretty" in libs.error
    assert "does not exist" in libs.error


def test_load_reports_a_malformed_project_table(netlist: Path, stock) -> None:
    (netlist.parent / "fp-lib-table").write_text("(fp_lib_table (lib (name", encoding="utf-8")
    libs = load(netlist, stock)
    assert libs.resolver is None and "fp-lib-table" in libs.error


def test_load_reports_a_block_vs_project_table_conflict(netlist: Path, stock) -> None:
    other = fp_lib(netlist.parent / "other.pretty", {"LG290P": EDITOR_FP})
    (netlist.parent / "fp-lib-table").write_text(
        f"(fp_lib_table\n\t{row('SPS_parts', other.as_posix())}\n)\n", encoding="utf-8")
    libs = load(netlist, stock)
    assert libs.resolver is not None and "different URI" in libs.error


def test_load_reports_a_missing_explicit_stock_dir(netlist: Path, tmp_path: Path) -> None:
    libs = pl.load(netlist, symbols_dir=str(tmp_path / "no_such_dir"))
    assert libs.resolver is None and "symbols_dir does not exist" in libs.error


def test_load_never_raises_even_on_an_unexpected_failure(netlist: Path, monkeypatch) -> None:
    def boom(explicit: str = "") -> str:
        raise RuntimeError("discovery exploded")
    monkeypatch.setattr(tc, "symbols_dir", boom)
    libs = pl.load(netlist)
    assert libs.resolver is None and libs.error == "RuntimeError: discovery exploded"


@pytest.mark.parametrize("bad", [None, "", 42])
def test_load_never_raises_on_a_bad_netlist_argument(bad, stock) -> None:
    libs = pl.load(bad, symbols_dir=str(stock[0]), footprints_dir=str(stock[1]))
    assert libs.resolver is None and "netlist must be" in libs.error


# --- materialize / write_map -------------------------------------------------------------
def test_materialize_writes_managed_rows_and_a_minimal_kicad_pro(netlist: Path, stock,
                                                                tmp_path: Path) -> None:
    libs = load(netlist, stock)
    out = tmp_path / "out"
    out.mkdir()
    written = pl.materialize(libs, project_design(), out, kicad_pro=out / "board.kicad_pro")
    assert written == [str(out / "sym-lib-table"), str(out / "fp-lib-table"),
                       str(out / "board.kicad_pro")]
    for table in ("sym-lib-table", "fp-lib-table"):
        rows = lt.read_table(out / table).rows
        assert [(r.name, r.descr) for r in rows] == [("SPS_parts", lt.MANAGED_DESCR)]
    assert json.loads((out / "board.kicad_pro").read_text(encoding="utf-8"))["meta"]
    found = lt.LibraryResolver.for_directory(out, None, None)
    assert found.footprint_lib_path("SPS_parts") == netlist.parent / "libs" / "SPS_parts.pretty"
    assert pl.materialize(libs, project_design(), out, kicad_pro=out / "board.kicad_pro") == []


def test_materialize_never_overwrites_a_kicad_pro_and_takes_it_relative_to_out(
        netlist: Path, stock) -> None:
    libs = load(netlist, stock)
    pro = netlist.parent / "netlist.kicad_pro"
    pro.write_text('{"keep": true}\n', encoding="utf-8")
    written = pl.materialize(libs, project_design(), netlist.parent, kicad_pro="netlist.kicad_pro")
    assert str(pro) not in written and pro.read_text(encoding="utf-8") == '{"keep": true}\n'
    fresh = pl.materialize(libs, project_design(), netlist.parent, kicad_pro="other.kicad_pro")
    assert fresh == [str(netlist.parent / "other.kicad_pro")]


def test_materialize_narrows_to_the_libraries_the_design_uses(netlist: Path, stock,
                                                             tmp_path: Path) -> None:
    design = project_design(components=[comp("R1", "Device:R", STOCK_R_FP)])
    libs = load(netlist, stock, design)
    out = tmp_path / "out"
    out.mkdir()
    assert pl.materialize(libs, design, out) == []
    assert not (out / "fp-lib-table").exists()


def test_materialize_and_write_map_refuse_unloaded_libraries(netlist: Path, stock,
                                                            tmp_path: Path) -> None:
    libs = load(netlist, stock, project_design(libraries="nope"))
    with pytest.raises(lt.LibTableError, match="did not load"):
        pl.materialize(libs, project_design(), tmp_path)
    with pytest.raises(lt.LibTableError, match="did not load"):
        pl.write_map(libs, tmp_path / "map.json")


def test_materialize_rejects_a_non_object_design(netlist: Path, stock, tmp_path: Path) -> None:
    with pytest.raises(TypeError, match="JSON object"):
        pl.materialize(load(netlist, stock), ["x"], tmp_path)


def test_write_map_dumps_the_library_map_for_swig(netlist: Path, stock, tmp_path: Path) -> None:
    libs = load(netlist, stock)
    target = tmp_path / "maps" / "libs.json"
    target.parent.mkdir()
    assert pl.write_map(libs, target) == str(target)
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data == json.loads(json.dumps(libs.resolver.library_map()))
    assert data["version"] == lt.MAP_VERSION
    pretty = lt.lookup(data, lt.FOOTPRINT, "SPS_parts")
    assert Path(pretty) == netlist.parent / "libs" / "SPS_parts.pretty"
    assert Path(lt.lookup(data, lt.FOOTPRINT, "Resistor_SMD")) == stock[1] / "Resistor_SMD.pretty"
    assert [p.name for p in target.parent.iterdir()] == ["libs.json"]    # no temp left


def test_write_map_requires_a_json_path(netlist: Path, stock, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match=r"\.json"):
        pl.write_map(load(netlist, stock), tmp_path / "libs.txt")


# --- footprint_digest -------------------------------------------------------------------
def test_footprint_ids_cover_components_and_mounting_holes() -> None:
    design = project_design(mounting_holes=True)
    design["components"].append(comp("TP1", None, "no-colon"))
    assert pl.footprint_ids(design) == sorted({LG290P_FP, STOCK_R_FP,
                                               lt.DEFAULT_MOUNTING_HOLE_FOOTPRINT})
    custom = project_design(mounting_holes={"footprint": "MountingHole:MH_M2"})
    assert "MountingHole:MH_M2" in pl.footprint_ids(custom)
    assert pl.footprint_ids(["not", "a", "design"]) == []


def test_footprint_digest_changes_when_a_kicad_mod_byte_changes(netlist: Path, stock) -> None:
    libs = load(netlist, stock)
    before = pl.footprint_digest(libs, project_design())
    assert len(before) == 64 and before == pl.footprint_digest(libs, project_design())
    mod = netlist.parent / "libs" / "SPS_parts.pretty" / "LG290P.kicad_mod"
    mod.write_bytes(mod.read_bytes().replace(b"F.Cu", b"F.Cv"))
    assert pl.footprint_digest(libs, project_design()) != before


def test_footprint_digest_includes_stock_footprints(netlist: Path, stock) -> None:
    libs = load(netlist, stock)
    before = pl.footprint_digest(libs, project_design())
    mod = stock[1] / "Resistor_SMD.pretty" / "R_0603_1608Metric.kicad_mod"
    mod.write_bytes(mod.read_bytes() + b" ")
    assert pl.footprint_digest(libs, project_design()) != before


def test_footprint_digest_is_stable_across_locations_and_order(netlist: Path, stock,
                                                              tmp_path: Path) -> None:
    digest = pl.footprint_digest(load(netlist, stock), project_design())
    moved = tmp_path / "elsewhere" / "proj"
    shutil.copytree(netlist.parent, moved)
    reordered = project_design()
    reordered["components"].reverse()
    assert pl.footprint_digest(load(moved / "netlist.json", stock), reordered) == digest


def test_footprint_digest_is_stable_across_runs(netlist: Path, stock) -> None:
    digest = pl.footprint_digest(load(netlist, stock), project_design())
    code = ("import sys; sys.path[:0] = sys.argv[1:3]; import project_libs as pl; "
            "libs = pl.load(sys.argv[3], symbols_dir=sys.argv[4], footprints_dir=sys.argv[5]); "
            "import json; print(pl.footprint_digest(libs, json.load(open(sys.argv[3]))))")
    env = {**os.environ, "PYTHONHASHSEED": "12345"}
    out = subprocess.run([sys.executable, "-c", code, str(ROOT / "server"),
                          str(ROOT / "server" / "tools"), str(netlist), str(stock[0]),
                          str(stock[1])], capture_output=True, text=True, timeout=60,
                         stdin=subprocess.DEVNULL, env=env, encoding="utf-8", errors="replace")
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == digest


def test_footprint_digest_tells_missing_from_empty_and_never_raises(netlist: Path, stock) -> None:
    libs = load(netlist, stock)
    mod = netlist.parent / "libs" / "SPS_parts.pretty" / "LG290P.kicad_mod"
    mod.write_bytes(b"")
    empty = pl.footprint_digest(libs, project_design())
    mod.unlink()
    missing = pl.footprint_digest(libs, project_design())
    assert empty != missing
    broken = load(netlist, stock, project_design(libraries="nope"))
    assert len(pl.footprint_digest(broken, project_design())) == 64


# --- sources / source_callback ------------------------------------------------------------
def test_sources_delegates_to_review_sources(netlist: Path, stock) -> None:
    design = project_design()
    design["components"].append(comp("H1", None, "MountingHole:MountingHole_3.2mm_M3"))
    got = pl.sources(load(netlist, stock), design)
    assert got == rs.library_sources(design, netlist.parent, stock[0], stock[1])
    assert got == {"U1": {"symbol": "generated", "footprint": "generated"},
                   "R1": {"symbol": "stock", "footprint": "stock"},
                   "H1": {"footprint": "stock"}}


def test_sources_with_unloaded_libraries_are_unknown(netlist: Path, stock) -> None:
    design = project_design(libraries="nope")
    got = pl.sources(load(netlist, stock, design), design)
    assert got == {"U1": {"symbol": "unknown", "footprint": "unknown"},
                   "R1": {"symbol": "unknown", "footprint": "unknown"}}


def test_sources_falls_back_per_library_on_an_unreadable_library(netlist: Path, stock,
                                                                monkeypatch) -> None:
    libs = load(netlist, stock)
    real = lt.library_generator

    def flaky(path: object) -> str | None:
        if "SPS_parts" in str(path):
            raise lt.LibTableError(f"cannot read library file {path}")
        return real(path)
    monkeypatch.setattr(lt, "library_generator", flaky)
    got = pl.sources(libs, project_design())
    assert got == {"U1": {"symbol": "unknown", "footprint": "unknown"},
                   "R1": {"symbol": "stock", "footprint": "stock"}}


def test_sources_rejects_a_non_object_design(netlist: Path, stock) -> None:
    with pytest.raises(TypeError, match="JSON object"):
        pl.sources(load(netlist, stock), "design")


def test_source_callback_speaks_library_sources_for(netlist: Path, stock) -> None:
    hand = fp_lib(netlist.parent / "hand.pretty", {"X": EDITOR_FP})
    (netlist.parent / "fp-lib-table").write_text(
        f"(fp_lib_table\n\t{row('Hand', hand.as_posix())}\n)\n", encoding="utf-8")
    design = project_design(libraries={"symbols": {"Mine": "libs/mine.kicad_sym"},
                                       "footprints": {"SPS_parts": "libs/SPS_parts.pretty"}})
    sym_lib(netlist.parent / "libs" / "mine.kicad_sym")
    resolve = pl.source_callback(load(netlist, stock, design))
    assert resolve(lt.FOOTPRINT, "SPS_parts") == "generated"
    assert resolve(lt.SYMBOL, "Mine") == "netlist"
    assert resolve(lt.FOOTPRINT, "Hand") == "project"
    assert resolve(lt.SYMBOL, "Device") == "stock"
    assert resolve(lt.SYMBOL, "Nowhere") is None
    assert rr.library_sources_for(design, resolve)["R1"] == {"symbol": "stock",
                                                             "footprint": "stock"}


def test_source_callback_without_a_resolver_resolves_nothing(netlist: Path, stock) -> None:
    resolve = pl.source_callback(load(netlist, stock, project_design(libraries=1)))
    assert resolve(lt.SYMBOL, "Device") is None


def test_source_callback_reads_each_library_header_once(netlist: Path, stock,
                                                        monkeypatch) -> None:
    calls: list[str] = []
    real = lt.library_generator
    monkeypatch.setattr(lt, "library_generator", lambda p: calls.append(str(p)) or real(p))
    resolve = pl.source_callback(load(netlist, stock))
    assert [resolve(lt.FOOTPRINT, "SPS_parts") for _ in range(3)] == ["generated"] * 3
    assert len(calls) == 1


# --- pins_dirs / SWIG limit / nicknames -------------------------------------------------------
def test_pins_dirs_put_the_project_before_the_central_dir(tmp_path: Path) -> None:
    assert pl.pins_dirs(tmp_path) == [str(tmp_path / "pins"), str(tc.PINS_DIR)]
    assert pl.pins_dirs(str(tc.PINS_DIR.parent)) == [str(tc.PINS_DIR)]


@pytest.mark.parametrize("version, limit", [("9.0.4", 20241229), ("10.0.5", 20260206),
                                            ("10.99.0-4025-g1", None)])
def test_swig_footprint_version_limit_probes_the_pcbnew_install(monkeypatch, version,
                                                                limit) -> None:
    monkeypatch.setattr(kicad_env, "pick_swig", lambda: SimpleNamespace(version=version))
    assert pl.swig_footprint_version_limit() == limit


def test_swig_footprint_version_limit_is_none_without_pcbnew(monkeypatch) -> None:
    monkeypatch.setattr(kicad_env, "pick_swig", lambda: None)
    assert pl.swig_footprint_version_limit() is None

    def boom() -> None:
        raise OSError("probe failed")
    monkeypatch.setattr(kicad_env, "pick_swig", boom)
    assert pl.swig_footprint_version_limit() is None


@pytest.mark.parametrize("netlist_path, design, nick", [
    ("p/netlist.json", {"name": "sps_node"}, "sps_node_parts"),
    ("p/netlist.json", {"name": "LM2596 12V->5V"}, "LM2596_12V_5V_parts"),
    ("boards/sps/netlist.json", {}, "sps_parts"),
    ("boards/sps/lg290p_node.json", None, "lg290p_node_parts"),
    ("boards/sps/netlist.json", {"name": "::"}, "sps_parts"),
])
def test_default_library_nickname(tmp_path: Path, netlist_path: str, design, nick: str) -> None:
    assert pl.default_library_nickname(tmp_path / netlist_path, design) == nick


def test_default_library_nickname_is_a_valid_nickname_even_from_nothing() -> None:
    assert pl.default_library_nickname("/", {"name": "!!"}) == "project_parts"


def test_default_library_path() -> None:
    assert pl.default_library_path("symbol", "sps_parts") == "libs/sps_parts.kicad_sym"
    assert pl.default_library_path("footprints", "sps_parts") == "libs/sps_parts.pretty"


# --- register_in_block --------------------------------------------------------------------
def test_register_in_block_adds_the_entry_and_returns_a_new_dict(netlist: Path) -> None:
    before = json.loads(netlist.read_text(encoding="utf-8"))
    got = pl.register_in_block(netlist, "footprint", "Extra", "libs/extra.pretty")
    assert got["libraries"]["footprints"] == {"SPS_parts": "libs/SPS_parts.pretty",
                                              "Extra": "libs/extra.pretty"}
    assert json.loads(netlist.read_text(encoding="utf-8")) == got
    assert before == json.loads(json.dumps(project_design()))      # earlier read untouched
    assert {k for k in got if k != "libraries"} == {k for k in before if k != "libraries"}


def test_register_in_block_creates_the_block_and_never_mutates_earlier_results(
        tmp_path: Path) -> None:
    path = write_netlist(tmp_path / "netlist.json", {"name": "x", "components": []})
    first = pl.register_in_block(path, "symbols", "x_parts", "libs/x_parts.kicad_sym")
    second = pl.register_in_block(path, "footprints", "x_parts", "libs/x_parts.pretty")
    assert first["libraries"] == {"symbols": {"x_parts": "libs/x_parts.kicad_sym"}}
    assert second["libraries"] == {"symbols": {"x_parts": "libs/x_parts.kicad_sym"},
                                   "footprints": {"x_parts": "libs/x_parts.pretty"}}
    assert list(second) == ["name", "components", "libraries"]


def test_register_in_block_same_location_is_a_no_op(netlist: Path) -> None:
    stamp = netlist.stat().st_mtime_ns
    text = netlist.read_text(encoding="utf-8")
    got = pl.register_in_block(netlist, "footprint", "SPS_parts",
                               "${KIPRJMOD}/libs/SPS_parts.pretty")
    assert got["libraries"]["footprints"]["SPS_parts"] == "libs/SPS_parts.pretty"
    assert netlist.read_text(encoding="utf-8") == text and netlist.stat().st_mtime_ns == stamp


def test_register_in_block_refuses_another_path_for_the_nick(netlist: Path) -> None:
    text = netlist.read_text(encoding="utf-8")
    with pytest.raises(lt.LibTableError, match="already declares"):
        pl.register_in_block(netlist, "footprint", "SPS_parts", "../shared/SPS_parts.pretty")
    assert netlist.read_text(encoding="utf-8") == text


def test_register_in_block_refuses_a_conflicting_project_table_row(netlist: Path) -> None:
    other = fp_lib(netlist.parent / "other.pretty", {"X": EDITOR_FP})
    (netlist.parent / "fp-lib-table").write_text(
        f"(fp_lib_table\n\t{row('Shared', other.as_posix())}\n)\n", encoding="utf-8")
    text = netlist.read_text(encoding="utf-8")
    with pytest.raises(lt.LibTableError, match="fp-lib-table"):
        pl.register_in_block(netlist, "footprint", "Shared", "libs/shared.pretty")
    assert netlist.read_text(encoding="utf-8") == text
    same = pl.register_in_block(netlist, "footprint", "Shared", "other.pretty")
    assert same["libraries"]["footprints"]["Shared"] == "other.pretty"


@pytest.mark.parametrize("failing", ["replace", "fsync"])
def test_register_in_block_is_atomic(netlist: Path, monkeypatch, failing: str) -> None:
    original = netlist.read_bytes()

    def boom(*args: object) -> None:
        raise OSError(f"simulated {failing} failure")
    monkeypatch.setattr(pl.os, failing, boom)
    with pytest.raises(OSError, match="simulated"):
        pl.register_in_block(netlist, "symbol", "Extra", "libs/extra.kicad_sym")
    assert netlist.read_bytes() == original
    assert sorted(p.name for p in netlist.parent.iterdir()) == ["libs", "netlist.json"]


@pytest.mark.parametrize("kind, nick, rel, needle", [
    ("model", "X", "libs/x.pretty", "kind"),
    ("footprint", "", "libs/x.pretty", "nickname"),
    ("footprint", "a:b", "libs/x.pretty", "nickname"),
    ("footprint", "X", "", "path"),
    ("footprint", "X", "libs/x.kicad_sym", ".pretty"),
    ("symbol", "X", "libs/x.pretty", ".kicad_sym"),
])
def test_register_in_block_validates_its_arguments(netlist: Path, kind, nick, rel,
                                                   needle) -> None:
    with pytest.raises(ValueError, match=needle):
        pl.register_in_block(netlist, kind, nick, rel)


def test_register_in_block_refuses_a_malformed_netlist(tmp_path: Path) -> None:
    listed = write_netlist(tmp_path / "a" / "netlist.json", [])
    with pytest.raises(lt.LibTableError, match="JSON object"):
        pl.register_in_block(listed, "symbol", "X", "x.kicad_sym")
    bad_block = write_netlist(tmp_path / "b" / "netlist.json", {"libraries": {"symbols": []}})
    with pytest.raises(lt.LibTableError, match="libraries.symbols"):
        pl.register_in_block(bad_block, "symbol", "X", "x.kicad_sym")
    with pytest.raises(lt.LibTableError, match="cannot read netlist"):
        pl.register_in_block(tmp_path / "missing.json", "symbol", "X", "x.kicad_sym")


def test_register_in_block_stores_portable_separators(tmp_path: Path) -> None:
    path = write_netlist(tmp_path / "netlist.json", {})
    rel = os.path.join("libs", "x_parts.pretty")
    got = pl.register_in_block(path, "footprint", "x_parts", rel)
    assert got["libraries"]["footprints"]["x_parts"] == "libs/x_parts.pretty"


# --- lint_context ---------------------------------------------------------------------------
def test_lint_context_hands_the_resolver_to_the_rules(netlist: Path, stock, monkeypatch) -> None:
    monkeypatch.setattr(kicad_env, "pick_swig", lambda: SimpleNamespace(version="9.0.4"))
    libs = load(netlist, stock)
    ctx = pl.lint_context(libs)
    assert isinstance(ctx, LintContext)
    assert ctx.symbols is libs.resolver and ctx.resolver is libs.resolver
    assert ctx.library_error == "" and ctx.netlist_dir == libs.netlist_dir
    assert ctx.swig_fp_version_max == 20241229
    assert pl.lint_context(libs, probe_swig=False).swig_fp_version_max is None
    report = lint_netlist.lint(project_design(), ctx=pl.lint_context(libs, probe_swig=False))
    assert isinstance(report["errors"], list) and report["components"] == 2


def test_lint_context_without_a_resolver_carries_the_load_error(netlist: Path, stock) -> None:
    libs = load(netlist, stock, project_design(libraries="nope"))
    ctx = pl.lint_context(libs, probe_swig=False)
    assert ctx.resolver is None and ctx.symbols == str(stock[0])
    assert ctx.library_error == libs.error != ""


def test_lint_context_without_stock_symbols_keeps_dir_mode(netlist: Path, monkeypatch) -> None:
    monkeypatch.setattr(tc, "symbols_dir", lambda explicit="": None)
    monkeypatch.setattr(tc, "footprints_dir", lambda explicit="": None)
    ctx = pl.lint_context(pl.load(netlist), probe_swig=False)
    assert ctx.symbols == "" and ctx.resolver is not None


# --- module contract ------------------------------------------------------------------------
def test_module_is_small_and_stdlib_only_besides_the_plugin() -> None:
    text = MODULE.read_text(encoding="utf-8")
    assert len(text.splitlines()) < 400
    tools = {"libtable", "lint_context", "review_sources", "review_report", "fp_dialect"}
    imported: set[str] = set()
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            imported.add(node.module.split(".")[0])
        elif (isinstance(node, ast.Call) and getattr(node.func, "id", "") == "_tool"
              and isinstance(node.args[0], ast.Constant)):
            assert node.args[0].value in tools, node.args[0].value
            assert (TOOLS / f"{node.args[0].value}.py").is_file()
    plugin = {"toolchain", "kicad_env"} | tools
    assert imported - plugin <= set(sys.stdlib_module_names), imported - plugin


def test_importing_the_module_keeps_server_tools_off_sys_path(tmp_path: Path) -> None:
    """board_build imports project_libs (F2-13), and importing the board
    modules must leave server/tools off sys.path (test_board_pipeline_split)."""
    code = ("import os, sys\n"
            "sys.path.insert(0, sys.argv[1])\n"
            "import project_libs\n"
            "tools = os.path.normcase(os.path.abspath(sys.argv[2]))\n"
            "print(sum(1 for p in sys.path if p and "
            "os.path.normcase(os.path.abspath(p)) == tools))\n")
    child_env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    r = subprocess.run([sys.executable, "-c", code, str(ROOT / "server"), str(TOOLS)],
                       cwd=tmp_path, env=child_env, stdin=subprocess.DEVNULL,
                       capture_output=True, encoding="utf-8", errors="replace", timeout=120)
    assert r.returncode == 0, r.stderr[-1500:]
    assert r.stdout.strip().splitlines()[-1] == "0"
