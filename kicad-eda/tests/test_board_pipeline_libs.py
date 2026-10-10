"""F2-13: build_board_from_netlist (board_build.build) with project libraries (D1).

The build loads the netlist's libraries (project_libs.load: the netlist
``libraries`` block > the project sym-lib-table / fp-lib-table beside the
netlist > the stock libraries) and refuses, without spawning or writing, when
they do not load. It then merges the libraries the design uses into the
board directory's tables (``kicad-eda managed`` rows) plus a minimal
``<stem>.kicad_pro`` so a later ``kicad-cli pcb drc`` resolves the FPIDs, and
hands swig_build_board the library map as argv[3]: a temp ``*.json``
(project_libs.write_map) deleted when the child is done. A design whose
footprints all come from the stock libraries (no block or project table
declares one) keeps dir mode: argv[3] is the stock footprints directory and
nothing is written, exactly as before. Every spawned build reports
``libraries`` ({ref: {symbol, footprint}} = stock | project | generated |
unknown) and ``library_tables`` (the table / project files it wrote).

Unit tier: tc.run is a fake that records the argv, the map JSON it was handed
and the files beside the board at spawn time. Live tier (KiCad 9/10 pcbnew +
kicad-cli; skipped without them): lm358 + the authored LG290P from a project
library builds through board_build.build into another directory, every saved
FPID carries its nickname, and kicad-cli DRC reports no lib_footprint_issues
for the project library (and does once the materialized fp-lib-table is
removed); stock libraries resolve through kicad-cli's global table: where it
declares them DRC reports no library issue at all, while a bare container
lacks it, so there only stock nicknames may be reported. Relative netlist /
board paths (the harness passes them as given) put the tables and the
``.kicad_pro`` beside the board, and a missing output directory is refused
in plain words before anything is written or spawned.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest

import board_build
import board_pipeline_tools as bpt
import board_route_tools as brt
import kicad_env
import kicad_ipc as ipc
import libtable as lt
import toolchain as tc
from conftest import load_example

NICK = "Proj_parts"
LG290P = f"{NICK}:Quectel_LG290P"
R0603 = "Resistor_SMD:R_0603_1608Metric"
SCRIPT = str(tc.TOOLS_DIR / "swig_build_board.py")
FAKE_RESULT = {"ok": True, "footprints": 2, "nets": 2, "pads_assigned": 4, "zones": [],
               "missing_pads": [], "fpids": {"R1": R0603, "U1": LG290P}}
_FP_HEAD = re.compile(r'^\t\(footprint "([^"]*)"', re.M)       # board-level footprints only


# --- the world: a stock install, a project with its own libraries, a fake builder -------
@dataclass
class World:
    root: Path
    stock_fp: Path
    stock_sym: Path
    project: Path
    runs: list[dict] = field(default_factory=list)
    result: dict = field(default_factory=lambda: dict(FAKE_RESULT))
    rc: int = 0
    raise_: BaseException | None = None

    def netlist(self, design: dict, folder: Path | None = None) -> Path:
        path = (folder or self.project) / "netlist.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(design, indent=1), encoding="utf-8")
        return path

    def build(self, netlist: Path, out: Path, **kw: object) -> dict:
        return board_build.build(str(netlist), 30, 20, out_pcb=str(out),
                                 footprints_dir=str(self.stock_fp), **kw)

    def fake_run(self, args: list[str], **kw: object) -> subprocess.CompletedProcess:
        footprints = str(args[4])
        out_dir = Path(args[3]).parent
        self.runs.append({
            "args": [str(a) for a in args], "kw": kw,
            "map": (json.loads(Path(footprints).read_text(encoding="utf-8"))
                    if footprints.endswith(".json") else None),
            "present": sorted(p.name for p in out_dir.iterdir())})
        if self.raise_ is not None:
            raise self.raise_
        return subprocess.CompletedProcess(args, self.rc, stdout=json.dumps(self.result) + "\n",
                                           stderr="")


def _footprint(lib: Path, name: str, generator: str = "kicad-eda") -> Path:
    lib.mkdir(parents=True, exist_ok=True)
    path = lib / f"{name}.kicad_mod"
    path.write_text(f'(footprint "{name}"\n\t(version 20241229)\n\t(generator "{generator}")\n)\n',
                    encoding="utf-8")
    return path


def _symbols(path: Path, generator: str = "kicad-eda") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'(kicad_symbol_lib\n\t(version 20241209)\n\t(generator "{generator}")\n)\n',
                    encoding="utf-8")
    return path


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    stock_fp, stock_sym = tmp_path / "stock" / "footprints", tmp_path / "stock" / "symbols"
    _footprint(stock_fp / "Resistor_SMD.pretty", "R_0603_1608Metric", "pcbnew")
    _symbols(stock_sym / "Device.kicad_sym", "kicad_symbol_editor")
    project = tmp_path / "proj"
    _footprint(project / "libs" / f"{NICK}.pretty", "Quectel_LG290P")
    _symbols(project / "libs" / f"{NICK}.kicad_sym")
    w = World(tmp_path, stock_fp, stock_sym, project)
    monkeypatch.setattr(tc, "kicad_python", lambda: "kpy")
    monkeypatch.setattr(tc, "symbols_dir", lambda explicit="": str(stock_sym))
    monkeypatch.setattr(tc, "run", w.fake_run)
    monkeypatch.setattr(ipc, "write_guard", lambda path: None)
    monkeypatch.setattr(brt, "_BOARDS", {})
    monkeypatch.setattr(brt, "_PROGRESS", {})
    return w


def _design(block: bool = True, symbols: bool = True) -> dict:
    """R1 (stock) + U1 (the project's LG290P), with or without the D1 block."""
    design: dict = {
        "components": [
            {"ref": "R1", "lib_id": "Device:R", "value": "10k", "footprint": R0603},
            {"ref": "U1", "lib_id": f"{NICK}:LG290P", "value": "LG290P", "footprint": LG290P}],
        "nets": {"GND": [["R1", "2"], ["U1", "10"]], "N1": [["R1", "1"], ["U1", "3"]]}}
    if block:
        libs = {"footprints": {NICK: f"libs/{NICK}.pretty"}}
        if symbols:
            libs["symbols"] = {NICK: f"libs/{NICK}.kicad_sym"}
        design["libraries"] = libs
    return design


def _stock_only() -> dict:
    return {"components": [{"ref": "R1", "lib_id": "Device:R", "value": "10k",
                            "footprint": R0603}],
            "nets": {"N1": [["R1", "1"]], "N2": [["R1", "2"]]}}


def _rows(table: Path) -> dict[str, lt.LibRow]:
    return {r.name: r for r in lt.read_table(table).rows}


def _written(folder: Path) -> list[str]:
    names = ("fp-lib-table", "sym-lib-table")
    return sorted(p.name for p in folder.iterdir() if p.name in names or p.suffix == ".kicad_pro")


# --- map mode ---------------------------------------------------------------------------
def test_the_library_map_is_argv3_and_is_deleted_after_the_build(world: World) -> None:
    netlist = world.netlist(_design())
    out = world.root / "build" / "board.kicad_pcb"
    out.parent.mkdir()
    res = world.build(netlist, out)
    assert res["ok"] is True, res
    [run] = world.runs
    args = run["args"]
    assert args[:4] == ["kpy", SCRIPT, str(netlist), str(out)] and args[5:] == ["30", "20"]
    assert args[4].endswith(".json") and not Path(args[4]).exists()
    lib_map = run["map"]
    assert lib_map["version"] == lt.MAP_VERSION and lib_map["errors"] == []
    assert lib_map["stock"]["footprints"] == str(world.stock_fp)
    assert lt.lookup(lib_map, lt.FOOTPRINT, NICK) == str(world.project / "libs" / f"{NICK}.pretty")
    assert lt.lookup(lib_map, lt.FOOTPRINT, "Resistor_SMD") == str(
        world.stock_fp / "Resistor_SMD.pretty")


def test_the_map_is_deleted_when_the_build_times_out_or_crashes(world: World) -> None:
    netlist = world.netlist(_design())
    out = world.project / "board.kicad_pcb"
    world.raise_ = subprocess.TimeoutExpired(["kpy"], board_build.BUILD_TIMEOUT_S)
    res = world.build(netlist, out)
    assert res["ok"] is False and "timed out" in res["reason"], res
    assert res["pcb"] == str(out) and "libraries" in res
    world.raise_ = RuntimeError("the child exploded")
    with pytest.raises(RuntimeError, match="exploded"):
        world.build(netlist, out)
    assert len(world.runs) == 2
    assert not any(Path(r["args"][4]).exists() for r in world.runs)


def test_a_map_that_cannot_be_written_refuses_without_spawning(
        world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    import project_libs
    temps: list[Path] = []

    def full_disk(libs: object, path: str) -> str:
        temps.append(Path(path))
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(project_libs, "write_map", full_disk)
    res = world.build(world.netlist(_design()), world.project / "board.kicad_pcb")
    assert res["ok"] is False and "No space left on device" in res["reason"], res
    assert world.runs == [] and len(temps) == 1 and not temps[0].exists()


def test_a_map_that_cannot_be_deleted_keeps_the_build_and_warns(
        world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    real_unlink = Path.unlink

    def locked(self: Path, missing_ok: bool = False) -> None:
        if self.name.startswith(board_build.MAP_PREFIX):
            raise PermissionError(13, "in use by another process", str(self))
        real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", locked)
    res = world.build(world.netlist(_design()), world.project / "board.kicad_pcb")
    monkeypatch.setattr(Path, "unlink", real_unlink)
    left = Path(world.runs[0]["args"][4])
    assert left.exists()
    left.unlink()
    assert res["ok"] is True and "netclasses" in res, res
    assert any(str(left) in w and "in use by another process" in w for w in res["warnings"])


def test_a_child_that_cannot_start_is_an_error_envelope(world: World) -> None:
    world.raise_ = FileNotFoundError("kpy")
    res = world.build(world.netlist(_design()), world.project / "board.kicad_pcb")
    assert res["ok"] is False and "kpy" in res["reason"], res
    assert not Path(world.runs[0]["args"][4]).exists()


def test_the_child_gets_a_timeout_utf8_and_pack_scale(world: World) -> None:
    world.build(world.netlist(_design()), world.project / "board.kicad_pcb", pack_scale=1.5,
                placement=str(world.project / "placement.json"))
    [run] = world.runs
    kw = run["kw"]
    assert kw["timeout"] == board_build.BUILD_TIMEOUT_S > 0
    assert (kw["encoding"], kw["errors"]) == ("utf-8", "replace")
    assert kw["env"]["PACK_SCALE"] == "1.5"
    assert run["args"][-1] == str(world.project / "placement.json")


def test_tables_and_a_minimal_project_are_written_beside_the_board_before_the_build(
        world: World) -> None:
    netlist = world.netlist(_design())
    out = world.root / "build" / "board.kicad_pcb"
    out.parent.mkdir()
    res = world.build(netlist, out)
    assert world.runs[0]["present"] == ["board.kicad_pro", "fp-lib-table", "sym-lib-table"]
    fp_row = _rows(out.parent / "fp-lib-table")[NICK]
    assert fp_row.managed and fp_row.uri == f"${{KIPRJMOD}}/../proj/libs/{NICK}.pretty"
    sym_row = _rows(out.parent / "sym-lib-table")[NICK]
    assert sym_row.managed and sym_row.uri == f"${{KIPRJMOD}}/../proj/libs/{NICK}.kicad_sym"
    assert set(_rows(out.parent / "fp-lib-table")) == {NICK}         # stock stays global
    pro = json.loads((out.parent / "board.kicad_pro").read_text(encoding="utf-8"))
    assert pro["meta"]["filename"] == "board.kicad_pro"
    assert sorted(Path(p).name for p in res["library_tables"]) == [
        "board.kicad_pro", "fp-lib-table", "sym-lib-table"]


def test_the_result_reports_each_parts_library_source(world: World) -> None:
    _footprint(world.project / "libs" / f"{NICK}.pretty", "Hand_Made", "pcbnew")
    design = _design()
    design["components"].append({"ref": "U2", "lib_id": f"{NICK}:LG290P", "value": "x",
                                 "footprint": f"{NICK}:Hand_Made"})
    res = world.build(world.netlist(design), world.project / "board.kicad_pcb")
    assert res["libraries"] == {"R1": {"symbol": "stock", "footprint": "stock"},
                                "U1": {"symbol": "generated", "footprint": "generated"},
                                "U2": {"symbol": "generated", "footprint": "project"}}


def test_a_failed_build_still_reports_sources_but_writes_no_netclasses(world: World) -> None:
    world.rc, world.result = 1, {"ok": False, "error": "pcbnew could not load footprint"}
    res = world.build(world.netlist(_design()), world.project / "board.kicad_pcb")
    assert res["ok"] is False and res["error"] == "pcbnew could not load footprint"
    assert res["libraries"]["U1"]["footprint"] == "generated" and "netclasses" not in res


def test_a_rerun_writes_nothing_and_keeps_the_users_rows(world: World) -> None:
    out_dir = world.root / "build"
    out_dir.mkdir()
    user = lt.LibRow("Mine", "${KIPRJMOD}/mine.pretty", descr="hand made")
    lt.write_table(out_dir / "fp-lib-table", lt.LibTable("fp_lib_table", (user,)))
    netlist = world.netlist(_design())
    first = world.build(netlist, out_dir / "board.kicad_pcb")
    assert first["ok"] is True and len(first["library_tables"]) == 3
    table_text = (out_dir / "fp-lib-table").read_text(encoding="utf-8")
    assert '(name "Mine")' in table_text and _rows(out_dir / "fp-lib-table")["Mine"] == user
    again = world.build(netlist, out_dir / "board.kicad_pcb")
    assert again["ok"] is True and again["library_tables"] == []
    assert (out_dir / "fp-lib-table").read_text(encoding="utf-8") == table_text


def test_a_project_fp_lib_table_without_a_block_resolves_too(world: World) -> None:
    """D1 order: block > project table beside the netlist > stock."""
    lt.write_table(world.project / "fp-lib-table", lt.LibTable("fp_lib_table", (
        lt.LibRow(NICK, f"${{KIPRJMOD}}/libs/{NICK}.pretty"),)))
    netlist = world.netlist(_design(block=False))
    out = world.project / "board.kicad_pcb"
    res = world.build(netlist, out)
    assert res["ok"] is True, res
    [run] = world.runs
    assert run["map"]["footprints"][NICK]["source"] == lt.PROJECT
    assert res["libraries"]["U1"]["footprint"] == "generated"
    assert _rows(world.project / "fp-lib-table")[NICK].managed is False   # the user's row
    assert [Path(p).name for p in res["library_tables"]] == ["board.kicad_pro"]


@pytest.mark.parametrize("out_pcb", ["", "proj/out/board.kicad_pcb"])
def test_relative_paths_put_the_tables_and_project_beside_the_board(
        world: World, monkeypatch: pytest.MonkeyPatch, out_pcb: str) -> None:
    """The harness passes ctx.netlist / ctx.pcb as given (run_pipeline.py proj/netlist.json)."""
    world.netlist(_design())
    if out_pcb:
        (world.project / "out").mkdir()
    monkeypatch.chdir(world.root)
    res = board_build.build("proj/netlist.json", 30, 20, out_pcb=out_pcb,
                            footprints_dir=str(world.stock_fp))
    assert res["ok"] is True and len(world.runs) == 1, res
    given = out_pcb or str(Path("proj/netlist.json").with_suffix(".kicad_pcb"))
    assert res["pcb"] == given and world.runs[0]["args"][3] == given
    board = world.root / given
    expected = [board.parent / "fp-lib-table", board.parent / "sym-lib-table",
                board.with_suffix(".kicad_pro")]
    assert {board.with_suffix(".kicad_pro").name, "fp-lib-table"} <= set(world.runs[0]["present"])
    assert sorted(Path(p).resolve() for p in res["library_tables"]) == sorted(
        p.resolve() for p in expected)
    assert not (board.parent / "proj").exists() and "netclasses" in res
    uri = _rows(board.parent / "fp-lib-table")[NICK].uri
    assert Path(uri.replace("${KIPRJMOD}", str(board.parent))).resolve() == (
        world.project / "libs" / f"{NICK}.pretty").resolve()


@pytest.mark.parametrize("make", [_design, _stock_only], ids=["libraries", "stock only"])
def test_a_missing_output_directory_refuses_in_plain_words(
        world: World, make: Callable[[], dict]) -> None:
    out = world.root / "nowhere" / "board.kicad_pcb"
    res = world.build(world.netlist(make()), out)
    assert res["ok"] is False and world.runs == [], res
    assert res["reason"] == f"output directory {out.parent} does not exist (nothing was built)"
    assert not out.parent.exists()


def test_the_mcp_tool_goes_through_the_library_handling(world: World) -> None:
    netlist = world.netlist(_design())
    res = bpt.build_board_from_netlist(str(netlist), 30, 20,
                                       out_pcb=str(world.project / "board.kicad_pcb"),
                                       footprints_dir=str(world.stock_fp))
    assert res["ok"] is True and world.runs[0]["args"][4].endswith(".json")
    assert res["libraries"]["U1"] == {"symbol": "generated", "footprint": "generated"}


# --- dir mode: no project footprint library ------------------------------------------------
def test_a_stock_only_design_keeps_dir_mode_and_writes_nothing(world: World) -> None:
    netlist = world.netlist(_stock_only())
    out = world.project / "board.kicad_pcb"
    res = world.build(netlist, out, placement="p.json")
    assert res["ok"] is True, res
    [run] = world.runs
    assert run["args"] == ["kpy", SCRIPT, str(netlist), str(out), str(world.stock_fp), "30",
                           "20", "p.json"]
    assert run["map"] is None and _written(world.project) == []
    assert res["libraries"] == {"R1": {"symbol": "stock", "footprint": "stock"}}
    assert res["library_tables"] == [] and res["fpids"] == FAKE_RESULT["fpids"]


def test_a_symbols_only_block_builds_in_dir_mode_but_writes_the_symbol_table(
        world: World) -> None:
    design = _stock_only()
    design["components"][0]["lib_id"] = f"{NICK}:LG290P"
    design["libraries"] = {"symbols": {NICK: f"libs/{NICK}.kicad_sym"}}
    out = world.root / "build" / "board.kicad_pcb"
    out.parent.mkdir()
    res = world.build(world.netlist(design), out)
    assert world.runs[0]["args"][4] == str(world.stock_fp) and world.runs[0]["map"] is None
    assert _written(out.parent) == ["board.kicad_pro", "sym-lib-table"]
    assert res["libraries"]["R1"] == {"symbol": "generated", "footprint": "stock"}


def test_stale_managed_rows_are_cleared_when_the_block_drops_the_library(
        world: World) -> None:
    out = world.root / "build" / "board.kicad_pcb"
    out.parent.mkdir()
    world.build(world.netlist(_design()), out)
    assert NICK in _rows(out.parent / "fp-lib-table")
    res = world.build(world.netlist(_stock_only()), out)
    assert res["ok"] is True and world.runs[-1]["map"] is None
    assert _rows(out.parent / "fp-lib-table") == {} and _rows(out.parent / "sym-lib-table") == {}
    assert sorted(Path(p).name for p in res["library_tables"]) == ["fp-lib-table",
                                                                   "sym-lib-table"]


# --- refusals: nothing spawned, nothing written ---------------------------------------------
def _missing_library(world: World) -> Path:
    design = _design()
    design["libraries"]["footprints"]["Nope_parts"] = "libs/Nope_parts.pretty"
    return world.netlist(design)


def _malformed_block(world: World) -> Path:
    return world.netlist({**_design(), "libraries": ["not", "an", "object"]})


def _conflict(world: World) -> Path:
    lt.write_table(world.project / "fp-lib-table", lt.LibTable("fp_lib_table", (
        lt.LibRow(NICK, "${KIPRJMOD}/elsewhere/Proj_parts.pretty"),)))
    return world.netlist(_design())


def _bad_json(world: World) -> Path:
    path = world.project / "netlist.json"
    path.write_text("{not json", encoding="utf-8")
    return path


def _not_an_object(world: World) -> Path:
    path = world.project / "netlist.json"
    path.write_text("[1, 2]", encoding="utf-8")
    return path


REFUSALS: dict[str, tuple[Callable[[World], Path], tuple[str, ...]]] = {
    "missing declared library": (_missing_library, ("Nope_parts", "Nope_parts.pretty",
                                                    "does not exist")),
    "malformed block": (_malformed_block, ("libraries",)),
    "block vs project table": (_conflict, (NICK, "elsewhere", "same nickname")),
    "unreadable netlist": (_bad_json, ("cannot read netlist",)),
    "netlist not an object": (_not_an_object, ("not a JSON object",)),
}


@pytest.mark.parametrize("case", list(REFUSALS))
def test_library_problems_refuse_before_spawning_or_writing(world: World, case: str) -> None:
    make, words = REFUSALS[case]
    netlist = make(world)
    out = world.root / "build" / "board.kicad_pcb"
    out.parent.mkdir()
    res = world.build(netlist, out)
    assert res["ok"] is False and world.runs == [], res
    assert all(w in res["reason"] for w in words), res["reason"]
    assert list(out.parent.iterdir()) == []


def test_a_clashing_row_in_the_board_dir_refuses_before_spawning(world: World) -> None:
    out_dir = world.root / "build"
    out_dir.mkdir()
    clash = lt.LibTable("fp_lib_table", (lt.LibRow(NICK, "${KIPRJMOD}/other.pretty"),))
    lt.write_table(out_dir / "fp-lib-table", clash)
    before = (out_dir / "fp-lib-table").read_bytes()
    res = world.build(world.netlist(_design()), out_dir / "board.kicad_pcb")
    assert res["ok"] is False and world.runs == [], res
    assert NICK in res["reason"] and "rename one of them" in res["reason"]
    assert (out_dir / "fp-lib-table").read_bytes() == before
    assert sorted(p.name for p in out_dir.iterdir()) == ["fp-lib-table"]


def test_an_open_editor_or_a_dirty_session_refuses_before_any_write(
        world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    netlist = world.netlist(_design())
    out = world.root / "build" / "board.kicad_pcb"
    out.parent.mkdir()
    refusal = {"ok": False, "reason": "open in the PCB editor"}
    monkeypatch.setattr(ipc, "write_guard", lambda path: refusal)
    assert world.build(netlist, out) == refusal
    monkeypatch.setattr(ipc, "write_guard", lambda path: None)
    blocked = {"ok": False, "blocking_board": str(out), "reason": "unsaved changes"}
    monkeypatch.setattr(board_build, "release_board_session", lambda pcb: blocked)
    assert world.build(netlist, out) == blocked
    assert world.runs == [] and list(out.parent.iterdir()) == []


def test_released_sessions_are_reported_with_the_build(world: World,
                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    out = world.project / "board.kicad_pcb"
    monkeypatch.setattr(board_build, "release_board_session",
                        lambda pcb: {"released_boards": [pcb]})
    res = world.build(world.netlist(_design()), out)
    assert res["ok"] is True and res["released_boards"] == [str(out)]


# --- live: KiCad 9/10 pcbnew build + kicad-cli DRC --------------------------------------
_LIB_NAMED = re.compile(r"footprint library '([^']*)'")


def _drc_lib_issues(cli: str, pcb: Path) -> list[str]:
    """Descriptions of the lib_footprint_issues kicad-cli DRC reports (all severities)."""
    report = pcb.with_name(pcb.stem + "_libdrc.json")
    report.unlink(missing_ok=True)
    r = tc.run([cli, "pcb", "drc", str(pcb), "--format", "json", "--severity-all", "-o",
                str(report)], timeout=300, encoding="utf-8", errors="replace")
    errors = re.findall(r"(?:^|\s)Error:.*", r.stdout + r.stderr)
    assert r.returncode == 0 and not errors and report.is_file(), (r.stdout + r.stderr)[-1500:]
    data = json.loads(report.read_text(encoding="utf-8"))
    return [v.get("description", "") for v in data.get("violations", [])
            if v.get("type") == "lib_footprint_issues"]


def _global_fp_nicknames(cli: str) -> set[str]:
    """The nicknames in kicad-cli's GLOBAL fp-lib-table (<config>/<major>.<minor>/;
    config = $KICAD_CONFIG_HOME, else %APPDATA%/kicad, ~/Library/Preferences/kicad
    or $XDG_CONFIG_HOME|~/.config/kicad); empty when it is absent or unreadable."""
    version = kicad_env.cli_version(Path(cli))
    if version is None:
        return set()
    major, minor, _ = kicad_env.parse_version(version)
    home = os.environ.get("KICAD_CONFIG_HOME")
    if not home:
        if sys.platform == "win32":
            home = str(Path(os.environ.get("APPDATA", Path.home())) / "kicad")
        elif sys.platform == "darwin":
            home = str(Path.home() / "Library" / "Preferences" / "kicad")
        else:
            home = str(Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "kicad")
    table = Path(home) / f"{major}.{minor}" / "fp-lib-table"
    try:
        return {r.name for r in lt.read_table(table).rows} if table.is_file() else set()
    except (OSError, lt.LibTableError):
        return set()


def test_live_project_footprints_build_with_nicknames_and_drc_finds_their_library(
        tmp_path: Path) -> None:
    from fixtures.swig_python import require_kicad_python
    require_kicad_python()
    import fp_author
    import fp_author_templates as fpt
    project, build_dir = tmp_path / "proj", tmp_path / "build"
    build_dir.mkdir()
    fp_author.write_footprint(fpt.lg290p(), project / "libs" / f"{NICK}.pretty")
    design = load_example("lm358_buffer")
    design["libraries"] = {"footprints": {NICK: f"libs/{NICK}.pretty"}}
    design["components"] += [{"ref": "U2", "lib_id": "Device:R", "value": "LG290P",
                              "footprint": LG290P}]
    design["nets"]["GND"] = design["nets"]["GND"] + [["U2", "10"], ["U2", "12"]]
    netlist = project / "netlist.json"
    netlist.write_text(json.dumps(design, indent=1), encoding="utf-8")
    pcb = build_dir / "board.kicad_pcb"
    res = board_build.build(str(netlist), 0, 0, out_pcb=str(pcb))
    assert res.get("ok") is True, res
    names = _FP_HEAD.findall(pcb.read_text(encoding="utf-8"))
    assert LG290P in names and all(":" in n for n in names), names
    assert sorted(res["fpids"].values()) == sorted(names) and res["fpids"]["U2"] == LG290P
    assert res["libraries"]["U2"]["footprint"] == "generated"
    assert _rows(build_dir / "fp-lib-table")[NICK].managed
    cli = tc.board_cli(str(pcb))
    if not cli:
        pytest.skip("no kicad-cli for the built board")
    # The project library resolves through the materialized fp-lib-table. Stock
    # nicknames resolve through kicad-cli's GLOBAL table: on an install whose
    # global table declares them all (KiCad 9 here) DRC reports no library issue
    # at all; a bare container (Docker K10) has none, so there each stock library
    # is reported, never the project one.
    stock = {n.split(":", 1)[0] for n in names} - {NICK}
    issues = _drc_lib_issues(cli, pcb)
    if stock <= _global_fp_nicknames(cli):
        assert issues == [], issues
    named = [_LIB_NAMED.search(d) for d in issues]
    assert all(m is not None and m.group(1) in stock for m in named), issues
    shutil.move(build_dir / "fp-lib-table", tmp_path / "fp-lib-table.moved")
    missing = _drc_lib_issues(cli, pcb)
    lost = [m.group(1) for m in map(_LIB_NAMED.search, missing) if m is not None]
    assert lost.count(NICK) == 1 and len(missing) == len(issues) + 1, missing   # check is live
