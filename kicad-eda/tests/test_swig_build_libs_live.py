"""swig_build_board and project libraries (F2-05, ROADMAP_DECISIONS D0 / D1).

argv[3] of the SWIG builder is a stock footprints directory (back-compat) or a
library-map JSON (libtable.LibraryResolver.library_map()); every footprint it
loads, stock parts and mounting holes included, is stamped with its library
nickname (SetFPID(LIB_ID(nick, name)), read back with GetUniStringLibId); a
footprint pcbnew does not load fails with fp_dialect's message (path,
(version N), pcbnew build, loadable limit) instead of a None crash.

Unit tier (no KiCad): the builder module imported against a stub pcbnew, its
library helpers driven with a fake pcbnew; static checks on the script.

Live tier (skipped without a KiCad Python that imports pcbnew; run on KiCad
9.0.4 locally and KiCad 10.0.5 in Docker, RULES.md "K10"): lm358 plus a
scratch project library holding the authored Quectel_LG290P, built through a
map JSON -> the saved board names "Proj_parts:Quectel_LG290P" and the stock
"Resistor_SMD:..." etc.; dir mode builds the same board as map mode and through
board_build.build (the existing caller); a KiCad 11 (version 20260901)
footprint fails with path, version and limit on both releases.
"""
from __future__ import annotations

import ast
import importlib.util
import json
import re
import shutil
import subprocess
import sys
import types
from pathlib import Path

import pytest

import fp_dialect as fd
import libtable as lt
import toolchain as tc
from conftest import load_example
from fixtures.swig_python import require_kicad_python

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "server" / "tools"
BUILDER = TOOLS / "swig_build_board.py"
K11_FIXTURE = ROOT / "tests" / "fixtures" / "dialect" / "Dialect_Test.kicad_mod"
BUILD_TIMEOUT_S = 600
PROJ = "Proj_parts"
LG290P = f"{PROJ}:Quectel_LG290P"
_FP_HEAD = re.compile(r'^\t\(footprint "([^"]*)"', re.M)     # board-level footprints only


# --- unit tier: the builder against a fake pcbnew ----------------------------------------
def _load_builder() -> types.ModuleType:
    """swig_build_board as a fresh module, imported against a stub pcbnew;
    every module the import added (the stub included) is dropped afterwards so
    nothing bound to the stub leaks into other tests."""
    before = dict(sys.modules)
    sys.modules["pcbnew"] = types.ModuleType("pcbnew")
    try:
        spec = importlib.util.spec_from_file_location("_swig_build_board_ut", BUILDER)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        for name in set(sys.modules) - set(before):
            del sys.modules[name]
        sys.modules.update(before)
    return module


class FakeLibId:
    def __init__(self, nick: str, name: str) -> None:
        self.nick, self.name = nick, name

    def GetUniStringLibId(self) -> str:  # noqa: N802 (KiCad API)
        return f"{self.nick}:{self.name}" if self.nick else self.name


class FakeFootprint:
    def __init__(self, name: str) -> None:
        self.fpid = FakeLibId("", name)

    def SetFPID(self, lib_id: FakeLibId) -> None:  # noqa: N802 (KiCad API)
        self.fpid = lib_id

    def GetFPID(self) -> FakeLibId:  # noqa: N802 (KiCad API)
        return self.fpid


def fake_pcbnew(load=None, build: str = "9.0.4", lib_id=FakeLibId) -> types.SimpleNamespace:
    """FootprintLoad(lib, name) -> `load(lib, name)` (default: a footprint)."""
    calls: list[tuple[str, str]] = []

    def footprint_load(lib: str, name: str):
        calls.append((lib, name))
        return FakeFootprint(name) if load is None else load(lib, name)
    return types.SimpleNamespace(FootprintLoad=footprint_load, LIB_ID=lib_id, calls=calls,
                                 GetBuildVersion=lambda: build)


@pytest.fixture(scope="module")
def sbb() -> types.ModuleType:
    return _load_builder()


@pytest.fixture
def libs(tmp_path: Path) -> dict:
    """A project .pretty (one K9 and one K11 footprint), a stock dir with
    Resistor_SMD.pretty, and their library map."""
    proj = tmp_path / "libs" / f"{PROJ}.pretty"
    proj.mkdir(parents=True)
    (proj / "Quectel_LG290P.kicad_mod").write_text(
        '(footprint "Quectel_LG290P"\n\t(version 20241229)\n)\n', encoding="utf-8")
    shutil.copyfile(K11_FIXTURE, proj / "Dialect_Test.kicad_mod")
    stock = tmp_path / "stock"
    (stock / "Resistor_SMD.pretty").mkdir(parents=True)
    design = {"libraries": {"footprints": {PROJ: f"libs/{PROJ}.pretty"}}}
    lib_map = lt.LibraryResolver.for_design(tmp_path, design, None, stock).library_map()
    path = tmp_path / "libmap.json"
    path.write_text(json.dumps(lib_map), encoding="utf-8")
    return {"proj": proj, "stock": stock, "map": lib_map, "map_path": path}


def test_footprint_libraries_dir_or_map(sbb, libs, tmp_path: Path) -> None:
    assert sbb.footprint_libraries(str(libs["stock"])) == str(libs["stock"])   # back-compat
    assert sbb.footprint_libraries(str(libs["map_path"])) == libs["map"]
    upper = tmp_path / "MAP.JSON"
    upper.write_text("\ufeff" + json.dumps(libs["map"]), encoding="utf-8")       # BOM, suffix case
    assert sbb.footprint_libraries(str(upper)) == libs["map"]


@pytest.mark.parametrize("text, match", [
    ("{not json", "cannot read the library map"),
    ("[1, 2]", "not a library map"),
    (json.dumps({"version": 99, "footprints": {}}), "version 99"),
    (json.dumps({"version": lt.MAP_VERSION, "footprints": [1]}), "not a library map"),
])
def test_footprint_libraries_rejects_a_bad_map(sbb, tmp_path: Path, text: str, match: str) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text(text, encoding="utf-8")
    with pytest.raises(RuntimeError, match=match) as err:
        sbb.footprint_libraries(str(bad))
    assert str(bad) in str(err.value)
    with pytest.raises(RuntimeError, match="cannot read"):
        sbb.footprint_libraries(str(tmp_path / "missing.json"))


def test_library_dir_map_dir_and_unknown(sbb, libs) -> None:
    assert Path(sbb.library_dir(libs["map"], PROJ)) == libs["proj"]                  # block
    assert Path(sbb.library_dir(libs["map"], "Resistor_SMD")) == \
        libs["stock"] / "Resistor_SMD.pretty"                                          # stock
    assert sbb.library_dir("/kicad/footprints", "Device") == "/kicad/footprints/Device.pretty"
    with pytest.raises(RuntimeError, match="'Nope'") as err:
        sbb.library_dir(libs["map"], "Nope")
    assert "library map" in str(err.value) and str(libs["stock"]) in str(err.value)


def test_load_footprint_stamps_the_nickname(sbb, libs, monkeypatch) -> None:
    pcb = fake_pcbnew()
    monkeypatch.setattr(sbb, "pcbnew", pcb)
    fp = sbb.load_footprint(libs["map"], LG290P)
    assert fp.GetFPID().GetUniStringLibId() == LG290P
    assert [(Path(lib), name) for lib, name in pcb.calls] == [(libs["proj"], "Quectel_LG290P")]
    stock = sbb.load_footprint(str(libs["stock"]), "Resistor_SMD:R_0603_1608Metric")
    assert stock.GetFPID().GetUniStringLibId() == "Resistor_SMD:R_0603_1608Metric"
    assert pcb.calls[-1] == (f"{libs['stock']}/Resistor_SMD.pretty", "R_0603_1608Metric")


def test_load_footprint_too_new_names_path_version_build_and_limit(sbb, libs,
                                                                    monkeypatch) -> None:
    monkeypatch.setattr(sbb, "pcbnew", fake_pcbnew(load=lambda lib, name: None))
    with pytest.raises(RuntimeError) as err:
        sbb.load_footprint(libs["map"], f"{PROJ}:Dialect_Test")
    msg = str(err.value)
    for needle in (str(libs["proj"] / "Dialect_Test.kicad_mod"), "(version 20260901)",
                   "pcbnew 9.0.4", "(version 20241229)", f"{PROJ}:Dialect_Test"):
        assert needle in msg, (needle, msg)


def test_load_footprint_wraps_a_raising_load(sbb, libs, monkeypatch) -> None:
    def boom(lib: str, name: str):
        raise OSError("IO_ERROR: unexpected token")
    monkeypatch.setattr(sbb, "pcbnew", fake_pcbnew(load=boom, build="10.0.5"))
    with pytest.raises(RuntimeError, match="IO_ERROR: unexpected token") as err:
        sbb.load_footprint(libs["map"], f"{PROJ}:Dialect_Test")
    assert "(version 20260206)" in str(err.value) and "pcbnew 10.0.5" in str(err.value)


def test_load_footprint_missing_library_never_calls_pcbnew(sbb, libs, tmp_path,
                                                         monkeypatch) -> None:
    pcb = fake_pcbnew()
    monkeypatch.setattr(sbb, "pcbnew", pcb)          # pcbnew raises AttributeError for it
    with pytest.raises(RuntimeError, match="does not exist") as err:
        sbb.load_footprint(str(tmp_path / "nowhere"), "Gone:X")
    assert "Gone:X" in str(err.value) and pcb.calls == []


@pytest.mark.parametrize("lib_id", ["R_0603", ":R_0603", "Device:", ""])
def test_load_footprint_needs_nick_and_name(sbb, libs, monkeypatch, lib_id: str) -> None:
    monkeypatch.setattr(sbb, "pcbnew", fake_pcbnew())
    with pytest.raises(RuntimeError, match="'<library>:<footprint>'"):
        sbb.load_footprint(libs["map"], lib_id)


def test_load_footprint_checks_the_read_back(sbb, libs, monkeypatch) -> None:
    class Ignored(FakeLibId):                        # a LIB_ID that drops the nickname
        def __init__(self, nick: str, name: str) -> None:
            super().__init__("", name)
    monkeypatch.setattr(sbb, "pcbnew", fake_pcbnew(lib_id=Ignored))
    with pytest.raises(RuntimeError, match="reads back"):
        sbb.load_footprint(libs["map"], LG290P)


def test_builder_source_contract() -> None:
    src = BUILDER.read_text(encoding="utf-8")
    tree = ast.parse(src, feature_version=(3, 11))   # KiCad 9.0.4's bundled Python
    calls = [n.func.attr for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)]
    assert "Format" not in calls and "SetFPID" in calls            # D1: never LIB_ID.Format()
    assert "libtable.DEFAULT_MOUNTING_HOLE_FOOTPRINT" in src
    assert re.search(r"fp = load_footprint\(footprints, mh_fp\)", src)   # holes stamped too
    assert len(src.splitlines()) < 800
    mine = {"footprint_libraries", "library_dir", "load_footprint"}
    funcs = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    assert mine <= set(funcs)
    for name in mine:
        node = funcs[name]
        assert node.end_lineno - node.lineno < 50, name
        assert node.returns is not None and all(a.annotation for a in node.args.args), name


# --- live tier: KiCad's pcbnew ----------------------------------------------------------
@pytest.fixture(scope="module")
def kpy() -> str:
    return require_kicad_python()


@pytest.fixture(scope="module")
def stock_fps(kpy: str) -> Path:
    """The footprints of the install whose Python builds (a newer install's
    footprints may be a dialect this pcbnew cannot read)."""
    found = tc.footprints_dir()
    if not found:
        pytest.skip("no stock footprints beside the KiCad Python")
    return Path(found)


def build(kpy: str, netlist: Path, out: Path, footprints: Path) -> tuple[int, dict, str]:
    """(rc, result JSON, stderr) of swig_build_board.py, stdin closed."""
    proc = subprocess.run([kpy, str(BUILDER), str(netlist), str(out), str(footprints)],
                          capture_output=True, text=True, encoding="utf-8", errors="replace",
                          stdin=subprocess.DEVNULL, timeout=BUILD_TIMEOUT_S)
    return proc.returncode, tc.last_json(proc.stdout) or {"stdout": proc.stdout}, proc.stderr


def write_design(folder: Path, design: dict, stock: Path | None) -> tuple[Path, Path]:
    """netlist.json and its library map (libtable.library_map()) in folder."""
    folder.mkdir(parents=True, exist_ok=True)
    netlist = folder / "netlist.json"
    netlist.write_text(json.dumps(design, indent=1), encoding="utf-8")
    lib_map = lt.LibraryResolver.for_design(folder, design, None, stock).library_map()
    map_path = folder / "libmap.json"
    map_path.write_text(json.dumps(lib_map, indent=1), encoding="utf-8")
    return netlist, map_path


def board_footprints(pcb: Path) -> list[str]:
    return _FP_HEAD.findall(pcb.read_text(encoding="utf-8"))


def _lm358_with_project_part() -> dict:
    """lm358_buffer + a stock 0603 resistor + the authored LG290P from the
    scratch project library + corner mounting holes."""
    design = load_example("lm358_buffer")
    design["libraries"] = {"footprints": {PROJ: f"libs/{PROJ}.pretty"}}
    design["components"] += [
        {"ref": "R1", "lib_id": "Device:R", "value": "10k",
         "footprint": "Resistor_SMD:R_0603_1608Metric"},
        {"ref": "U2", "lib_id": f"{PROJ}:LG290P", "value": "LG290P", "footprint": LG290P}]
    design["nets"]["IN_A"] = design["nets"]["IN_A"] + [["R1", "1"]]
    design["nets"]["GND"] = design["nets"]["GND"] + [["R1", "2"], ["U2", "10"], ["U2", "12"],
                                                     ["U2", "13"]]
    design["mounting_holes"] = True
    return design


def test_live_map_build_stamps_project_and_stock_nicknames(kpy, stock_fps, tmp_path) -> None:
    import fp_author
    import fp_author_templates as fpt
    fp_author.write_footprint(fpt.lg290p(), tmp_path / "libs" / f"{PROJ}.pretty")
    netlist, map_path = write_design(tmp_path, _lm358_with_project_part(), stock_fps)
    out = tmp_path / "board.kicad_pcb"
    rc, res, err = build(kpy, netlist, out, map_path)
    assert rc == 0 and res.get("ok") is True, (res, err[-2000:])
    text = out.read_text(encoding="utf-8")
    assert f'(footprint "{LG290P}"' in text
    assert '(footprint "Resistor_SMD:R_0603_1608Metric"' in text
    assert '(footprint "Package_SO:SOIC-8_3.9x4.9mm_P1.27mm"' in text
    assert text.count('(footprint "MountingHole:MountingHole_3.2mm_M3"') == 4
    names = board_footprints(out)
    assert len(names) == 10 and all(":" in n for n in names), names   # 6 parts + 4 holes
    assert sorted(res["fpids"].values()) == sorted(names)
    assert res["fpids"]["U2"] == LG290P and res["fpids"]["H1"] == lt.DEFAULT_MOUNTING_HOLE_FOOTPRINT
    assert res["mounting_holes"] == ["H1", "H2", "H3", "H4"] and res["missing_pads"] == []
    assert tc.board_file_info(str(out))["format_version"] in (20241229, 20260206)


def test_live_dir_mode_builds_the_same_board_as_map_mode(kpy, stock_fps, tmp_path) -> None:
    design = load_example("lm358_buffer")
    netlist, map_path = write_design(tmp_path, design, stock_fps)
    results = {}
    for mode, footprints in (("dir", stock_fps), ("map", map_path)):
        out = tmp_path / f"{mode}.kicad_pcb"
        rc, res, err = build(kpy, netlist, out, footprints)
        assert rc == 0 and res.get("ok") is True, (mode, res, err[-2000:])
        assert sorted(board_footprints(out)) == sorted(res["fpids"].values())
        results[mode] = {k: v for k, v in res.items() if k != "pcb"}
    assert results["dir"] == results["map"]
    assert results["dir"]["fpids"] == {c["ref"]: c["footprint"] for c in design["components"]}


def test_live_board_build_dir_mode_caller(kpy, stock_fps, tmp_path) -> None:
    import board_build
    netlist = tmp_path / "netlist.json"
    shutil.copyfile(ROOT / "examples" / "lm358_buffer" / "netlist.json", netlist)
    out = tmp_path / "lm358_buffer.kicad_pcb"
    res = board_build.build(str(netlist), 30, 25, out_pcb=str(out))
    assert res.get("ok") is True and res["footprints"] == 4, res
    assert sorted(board_footprints(out)) == sorted(
        c["footprint"] for c in load_example("lm358_buffer")["components"])


@pytest.mark.parametrize("mode", ["map", "dir"])
def test_live_kicad11_footprint_names_path_version_and_limit(kpy, stock_fps, tmp_path,
                                                             mode: str) -> None:
    lib = tmp_path / "libs" / f"{PROJ}.pretty"
    lib.mkdir(parents=True)
    mod = lib / "Dialect_Test.kicad_mod"
    shutil.copyfile(K11_FIXTURE, mod)
    design = {"components": [{"ref": "U1", "lib_id": "Device:R", "value": "x",
                              "footprint": f"{PROJ}:Dialect_Test"}], "nets": {}}
    if mode == "map":
        design["libraries"] = {"footprints": {PROJ: f"libs/{PROJ}.pretty"}}
    netlist, map_path = write_design(tmp_path, design, stock_fps)
    rc, res, err = build(kpy, netlist, tmp_path / "b.kicad_pcb",
                         map_path if mode == "map" else tmp_path / "libs")
    assert rc == 1 and res.get("ok") is False, (res, err[-2000:])
    msg = res["error"]
    found = re.search(r"pcbnew (\S+) could not load footprint", msg)
    assert found, msg
    limit = fd.pcbnew_footprint_limit(found.group(1))
    assert limit in (20241229, 20260206), msg                    # KiCad 9.0.x or 10.0.x
    for needle in (str(mod), "(version 20260901)", f"(version {limit})", f"{PROJ}:Dialect_Test"):
        assert needle in msg, (needle, msg)
    assert "NoneType" not in msg and "AttributeError" not in res.get("trace", "")
