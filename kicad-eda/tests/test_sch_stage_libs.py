"""The schematic stage with project libraries (F2-14, ROADMAP D0/D1/D5).

kicad_server.build_schematic_from_netlist (body: sch_stage_tools) loads the
netlist's libraries with project_libs and fails fast, naming the library's
nickname and path, when they do not load; it merges the libraries the design
uses into the schematic directory's sym-lib-table / fp-lib-table before the
API creates the document and before the file build, and the file route also
writes a minimal <stem>.kicad_pro; the file route searches <netlist dir>/pins
then server/pins for sidecars. When KiCad 11 has no global library tables the
API build (backend "auto") is redone on the file backend, reported as
``fallback`` and recorded once as a G2 decision. build_pin_sidecars gains
``netlist`` (output <project>/pins; project parts are refused for the central
pins dir) and design_report reads project parts through the resolver.

Two tiers: unit (child runs, the API module and the GUI faked; no KiCad) and
live (skipped when absent): kicad-cli on the file backend, the KiCad 11 nightly
on the API backend, and the nightly with an empty KICAD_CONFIG_HOME.
"""
from __future__ import annotations

import inspect
import json
import os
import shutil
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import decisions as dec
import kicad_env as env
import kicad_ipc as ipc
import kicad_server as k
import sch_stage_tools as sst
import sym_author
import toolchain as tc
from backends import sch_session as sess
from fixtures.kicad_install import KICAD, requires_kicad_cli
from fixtures.kicad_procs import owned_pids

FIXTURE_SYMBOLS = Path(__file__).resolve().parent / "fixtures" / "symbols"
NO_GUI = {"ipc_connected": False, "reason": "test: no GUI"}
KICAD_SERVER_MAX_LINES = 720            # acceptance; D7-13 adds one line later
LIVE_PREFIX = "test_live_"            # the live tier runs without the unit isolation
MISSING_TABLES = ("placing Device:R as R1: KiCad returned error: symbol 'Device:R' not found; "
                  f"{sess.GLOBAL_TABLES_MISSING} (0 global symbol libraries loaded): "
                  f"{sess.LIB_TABLE_HINT}")
DEVICE_LIB = """(kicad_symbol_lib (version 20241209) (generator "kicad-eda-tests")
  (symbol "R" (pin_names (offset 0)) (in_bom yes) (on_board yes)
    (property "Reference" "R" (at 0 0 0) (effects (font (size 1.27 1.27))))
    (property "Value" "R" (at 0 0 0) (effects (font (size 1.27 1.27))))
    (symbol "R_1_1"
      (pin passive line (at 0 3.81 270) (length 1.27)
        (name "~" (effects (font (size 1.27 1.27))))
        (number "1" (effects (font (size 1.27 1.27)))))
      (pin passive line (at 0 -3.81 90) (length 1.27)
        (name "~" (effects (font (size 1.27 1.27))))
        (number "2" (effects (font (size 1.27 1.27))))))))
"""


class FakeRun:
    """Stands in for tc.run: records every argv, returns one canned result."""

    def __init__(self, returncode: int = 0, stdout: str = '{"ok": true, "components": 2}',
                 stderr: str = "") -> None:
        self.result = SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str], **kw: Any) -> SimpleNamespace:
        self.calls.append(list(args))
        return self.result


class FakeApiError(Exception):
    pass


class FakeApiModule:
    """backends.sch_api stand-in: build_from_netlist records which project
    files exist when KiCad would create the document, then succeeds or raises."""
    SchematicApiError = FakeApiError

    def __init__(self, error: str = "") -> None:
        self.error = error
        self.seen: list[dict] = []

    def build_from_netlist(self, design: dict, out_path: str, overwrite: bool = True) -> dict:
        out = Path(out_path)
        self.seen.append({"tables": sorted(p.name for p in out.parent.glob("*-lib-table")),
                          "kicad_pro": out.with_suffix(".kicad_pro").exists()})
        if self.error:
            raise FakeApiError(self.error)
        return {"ok": True, "sch": out_path, "components": len(design["components"]),
                "nets": len(design.get("nets", {})), "missing_pins": [], "warnings": [],
                "backend": "api"}


@pytest.fixture(autouse=True)
def isolated(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch,
             tmp_path: Path) -> None:
    """Unit tier: no GUI, no ERC, no KiCad discovery, the API unavailable, a
    stock symbols dir holding Device (R) and power. test_live_* opt out."""
    if request.node.name.startswith(LIVE_PREFIX):
        return
    stock = tmp_path / "stock"
    stock.mkdir()
    (stock / "Device.kicad_sym").write_text(DEVICE_LIB, encoding="utf-8")
    shutil.copyfile(FIXTURE_SYMBOLS / "power.kicad_sym", stock / "power.kicad_sym")
    monkeypatch.setattr(ipc, "connect", lambda: NO_GUI)
    monkeypatch.setattr(tc, "symbols_dir", lambda explicit="": explicit or str(stock))
    monkeypatch.setattr(tc, "footprints_dir", lambda explicit="": explicit or None)
    monkeypatch.setattr(tc, "kicad_cli", lambda: None)
    monkeypatch.setattr(k, "_API", {"checked": True, "module": None, "reason": "test: no API"})
    monkeypatch.setattr(k, "_api_available", lambda: False)
    monkeypatch.setattr(k, "_SESSIONS", {})


@pytest.fixture
def run(monkeypatch: pytest.MonkeyPatch) -> FakeRun:
    fake = FakeRun()
    monkeypatch.setattr(tc, "run", fake)
    return fake


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch):
    """api(error='') -> the FakeApiModule now serving as the API backend."""
    def use(error: str = "") -> FakeApiModule:
        mod = FakeApiModule(error)
        monkeypatch.setattr(k, "_API", {"checked": True, "module": mod, "reason": None})
        monkeypatch.setattr(k, "_api_available", lambda: True)
        return mod
    return use


def _project(root: Path, **overrides: Any) -> Path:
    """<root>/libs/Proj.kicad_sym (the TestLib fixture's symbols) and an empty
    Proj.pretty, declared in <root>/board.json's libraries block; U1 =
    Proj:BaseAmp, R1 = Device:R (stock)."""
    libs = root / "libs"
    libs.mkdir(parents=True)
    shutil.copyfile(FIXTURE_SYMBOLS / "TestLib.kicad_sym", libs / "Proj.kicad_sym")
    (libs / "Proj.pretty").mkdir()
    design = {"name": "board",
              "libraries": {"symbols": {"Proj": "libs/Proj.kicad_sym"},
                            "footprints": {"Proj": "libs/Proj.pretty"}},
              "components": [{"ref": "U1", "lib_id": "Proj:BaseAmp", "value": "AMP",
                              "footprint": "Proj:SOIC8"},
                             {"ref": "R1", "lib_id": "Device:R", "value": "10k",
                              "footprint": ""}],
              "nets": {"OUT": [["U1", "1"], ["R1", "1"]]}, **overrides}
    net = root / "board.json"
    net.write_text(json.dumps(design), encoding="utf-8")
    return net


def _sidecars(netlist: Path) -> str:
    return os.pathsep.join([str(netlist.parent / "pins"), str(tc.PINS_DIR)])


# ----------------------------------------------------------------------------
# File route: tables, .kicad_pro, sidecar dirs
# ----------------------------------------------------------------------------
def test_file_build_materializes_the_tables_and_a_kicad_pro_beside_the_schematic(
        tmp_path, run):
    net = _project(tmp_path / "proj")
    out = tmp_path / "out" / "board.kicad_sch"                   # the directory is created
    res = k.build_schematic_from_netlist(str(net), out_sch=str(out), backend="file")
    assert res["ok"] is True and res["backend"] == "file" and res["sch"] == str(out)
    sym = (out.parent / "sym-lib-table").read_text(encoding="utf-8")
    fp = (out.parent / "fp-lib-table").read_text(encoding="utf-8")
    assert '"${KIPRJMOD}/../proj/libs/Proj.kicad_sym"' in sym and "kicad-eda managed" in sym
    assert '"${KIPRJMOD}/../proj/libs/Proj.pretty"' in fp
    pro = out.with_suffix(".kicad_pro")
    assert json.loads(pro.read_text(encoding="utf-8"))["meta"]["filename"] == pro.name
    assert sorted(res["library_tables"]) == sorted(
        str(out.parent / name) for name in ("sym-lib-table", "fp-lib-table", "board.kicad_pro"))
    assert res["libraries"] == {"U1": {"symbol": "project", "footprint": "project"},
                                "R1": {"symbol": "stock"}}
    assert run.calls == [[sys.executable, str(tc.TOOLS_DIR / "sch_build.py"), str(net), str(out),
                          tc.symbols_dir(), _sidecars(net)]]


def test_a_plain_netlist_gets_a_kicad_pro_and_no_tables(tmp_path, run):
    net = tmp_path / "plain.json"
    net.write_text(json.dumps({"components": [{"ref": "R1", "lib_id": "Device:R"}],
                               "nets": {}}), encoding="utf-8")
    res = k.build_schematic_from_netlist(str(net), backend="file")
    assert res["ok"] and res["library_tables"] == [str(tmp_path / "plain.kicad_pro")]
    assert not list(tmp_path.glob("*-lib-table"))


def test_an_existing_kicad_pro_is_kept_and_a_rerun_writes_nothing(tmp_path, run):
    net = _project(tmp_path)
    pro = tmp_path / "board.kicad_pro"
    pro.write_text('{"mine": true}', encoding="utf-8")
    first = k.build_schematic_from_netlist(str(net), backend="file")
    again = k.build_schematic_from_netlist(str(net), backend="file")
    assert pro.read_text(encoding="utf-8") == '{"mine": true}'
    assert sorted(Path(p).name for p in first["library_tables"]) == ["fp-lib-table",
                                                                     "sym-lib-table"]
    assert again["ok"] and again["library_tables"] == []


def test_an_explicit_sidecars_dir_reaches_sch_build_unchanged(tmp_path, run):
    net = _project(tmp_path)
    mine = str(tmp_path / "my_pins")
    k.build_schematic_from_netlist(str(net), sidecars_dir=mine, backend="file")
    assert run.calls[0][-1] == mine


# ----------------------------------------------------------------------------
# Fail fast: unusable libraries, unreadable netlist, open editor
# ----------------------------------------------------------------------------
@pytest.mark.parametrize("backend", ["file", "api"])
def test_a_missing_declared_library_fails_fast_naming_the_nick_and_path(
        tmp_path, run, api, backend):
    mod = api() if backend == "api" else None
    net = _project(tmp_path, libraries={"symbols": {"Gone": "libs/Gone.kicad_sym"}})
    res = k.build_schematic_from_netlist(str(net), backend=backend)
    assert res["ok"] is False and res["backend"] == backend
    assert "'Gone'" in res["reason"] and str(tmp_path / "libs" / "Gone.kicad_sym") in res["reason"]
    assert run.calls == [] and (mod is None or mod.seen == [])
    assert not list(tmp_path.glob("*-lib-table")) and not (tmp_path / "board.kicad_pro").exists()


def test_a_malformed_libraries_block_fails_fast(tmp_path, run):
    net = _project(tmp_path, libraries={"symbols": "libs/Proj.kicad_sym"})
    res = k.build_schematic_from_netlist(str(net), backend="file")
    assert res["ok"] is False and "libraries" in res["reason"] and run.calls == []


def test_an_unreadable_netlist_fails_without_running(tmp_path, run):
    net = tmp_path / "broken.json"
    net.write_text("{not json", encoding="utf-8")
    res = k.build_schematic_from_netlist(str(net), backend="file")
    assert res["ok"] is False and str(net) in res["reason"] and run.calls == []


def test_a_schematic_open_in_an_editor_is_refused_before_any_table_is_written(
        tmp_path, run, monkeypatch):
    net = _project(tmp_path)
    out = str(tmp_path / "board.kicad_sch")
    monkeypatch.setattr(ipc, "connect", lambda: {
        "ipc_connected": True, "kicad": object(), "kicad_version": "9.0.4",
        "open_documents": [{"type": "schematic", "path": out}],
        "capabilities": ipc.capabilities((9, 0, 4))})
    res = k.build_schematic_from_netlist(str(net), backend="file")
    assert res["ok"] is False and "Eeschema" in res["reason"] and run.calls == []
    assert not list(tmp_path.glob("*-lib-table")) and not (tmp_path / "board.kicad_pro").exists()


# ----------------------------------------------------------------------------
# API route and the api -> file fallback (D5)
# ----------------------------------------------------------------------------
def test_the_api_sees_the_tables_when_it_creates_the_document(tmp_path, run, api):
    mod = api()
    net = _project(tmp_path)
    res = k.build_schematic_from_netlist(str(net))
    assert res["ok"] and res["backend"] == "api" and "fallback" not in res
    assert mod.seen == [{"tables": ["fp-lib-table", "sym-lib-table"], "kicad_pro": False}]
    assert sorted(Path(p).name for p in res["library_tables"]) == ["fp-lib-table",
                                                                   "sym-lib-table"]
    assert not (tmp_path / "board.kicad_pro").exists()      # KiCad writes its own on save
    assert run.calls == [] and not dec.decisions_path(net).exists()


def test_missing_global_tables_fall_back_to_the_file_backend_and_record_a_g2_decision(
        tmp_path, run, api):
    api(MISSING_TABLES)
    net = _project(tmp_path)
    res = k.build_schematic_from_netlist(str(net))
    assert res["ok"] is True and res["backend"] == "file"
    (entry,) = dec.read_decisions(net)
    assert res["fallback"] == {"from": "api", "to": "file",
                               "reason": f"FakeApiError: {MISSING_TABLES}",
                               "decision": entry["id"]}
    assert entry["stage"] == "G2" and entry["source"] == "auto" and entry["reversible"] is True
    assert entry["confidence"] == "high" and "file" in entry["decision"]
    assert "board.kicad_sch" in entry["question"] and entry["alternatives"]
    assert (tmp_path / "board.kicad_pro").is_file()          # the file route's project file
    assert run.calls[0][1].endswith("sch_build.py") and run.calls[0][-1] == _sidecars(net)
    assert str(tmp_path / "board.kicad_pro") in res["library_tables"]


def test_the_fallback_decision_is_recorded_once(tmp_path, run, api):
    api(MISSING_TABLES)
    net = _project(tmp_path)
    first = k.build_schematic_from_netlist(str(net))
    again = k.build_schematic_from_netlist(str(net))           # the G2 loop rebuilds
    assert len(dec.read_decisions(net)) == 1
    assert first["fallback"]["decision"] == again["fallback"]["decision"]


@pytest.mark.parametrize("error", ["api-server failed to start: boom",
                                   "KiCad returned error: symbol 'Device:R' not found"])
def test_other_api_failures_do_not_fall_back(tmp_path, run, api, error):
    api(error)
    net = _project(tmp_path)
    res = k.build_schematic_from_netlist(str(net))
    assert res["ok"] is False and res["backend"] == "api" and error in res["reason"]
    assert "fallback" not in res and run.calls == [] and not dec.decisions_path(net).exists()


def test_an_explicit_api_backend_does_not_fall_back_but_says_how(tmp_path, run, api):
    api(MISSING_TABLES)
    net = _project(tmp_path)
    res = k.build_schematic_from_netlist(str(net), backend="api")
    assert res["ok"] is False and res["backend"] == "api" and "fallback" not in res
    assert "backend='auto'" in res["hint"]
    assert run.calls == [] and not dec.decisions_path(net).exists()


def test_the_fallback_keeps_the_session_release_notes(tmp_path, run, api, monkeypatch):
    api()
    net = _project(tmp_path)

    def released(netlist: str, out: str, symbols_dir: str, sidecars_dir: str) -> dict:
        return {"ok": False, "sch": out, "reason": f"SchematicApiError: {MISSING_TABLES}",
                "released_sessions": ["idle.kicad_sch"], "released_note": "reopen it"}

    monkeypatch.setattr(k, "_build_via_api", released)
    res = k.build_schematic_from_netlist(str(net))
    assert res["ok"] and res["backend"] == "file"
    assert res["sch"] == str(tmp_path / "board.kicad_sch")
    assert res["released_sessions"] == ["idle.kicad_sch"] and res["released_note"] == "reopen it"


def test_an_unwritable_decision_log_does_not_fail_the_fallback_build(tmp_path, run, api):
    api(MISSING_TABLES)
    net = _project(tmp_path)
    dec.decisions_path(net).mkdir()                            # a folder where the log goes
    res = k.build_schematic_from_netlist(str(net))
    assert res["ok"] and res["backend"] == "file"
    assert "decision" not in res["fallback"] and res["fallback"]["decision_error"]


def test_a_failed_fallback_build_is_reported_with_its_fallback(tmp_path, monkeypatch, api):
    api(MISSING_TABLES)
    monkeypatch.setattr(tc, "run", FakeRun(returncode=1, stdout="", stderr="Traceback"))
    net = _project(tmp_path)
    res = k.build_schematic_from_netlist(str(net))
    assert res["ok"] is False and res["backend"] == "file" and res["fallback"]["to"] == "file"
    assert res["stderr"] == "Traceback"


def test_erc_runs_on_the_fallback_schematic(tmp_path, run, api, monkeypatch):
    api(MISSING_TABLES)
    seen: list[str] = []

    def erc_json(sch: str, rpt: str, severity_error: bool = True) -> dict:
        seen.append(sch)
        return {"erc_ok": True, "erc_violations": 0, "erc_by_severity": {}, "erc_report": rpt,
                "report": {"sheets": []}}

    monkeypatch.setattr(tc, "kicad_cli", lambda: "kicad-cli")
    monkeypatch.setattr(tc, "erc_json", erc_json)
    net = _project(tmp_path)
    res = k.build_schematic_from_netlist(str(net))
    assert seen == [str(tmp_path / "board.kicad_sch")] and res["erc_ok"] is True
    assert "report" not in res and res["erc_report"] == str(tmp_path / "board_erc.json")


# ----------------------------------------------------------------------------
# build_pin_sidecars(netlist=...)
# ----------------------------------------------------------------------------
def test_sidecars_for_a_netlist_go_to_its_pins_dir(tmp_path):
    net = _project(tmp_path / "proj")
    central = tc.PINS_DIR / "Proj_BaseAmp.pins.json"
    res = k.build_pin_sidecars(netlist=str(net))              # the real child, no KiCad
    target = tmp_path / "proj" / "pins" / "Proj_BaseAmp.pins.json"
    assert res["ok"] is True, res
    assert res["out_dir"] == str(target.parent) and res["written"] == [str(target)]
    assert res["pins"] == {"Proj:BaseAmp": 8} and res["netlist"] == str(net)
    assert set(json.loads(target.read_text(encoding="utf-8"))) >= {"1", "2", "3", "8"}
    assert not central.exists()


@pytest.mark.parametrize("symbols", ["Proj:BaseAmp, Device:R", ""])
def test_project_part_sidecars_are_refused_for_the_central_pins_dir(tmp_path, run, symbols):
    net = _project(tmp_path)
    res = k.build_pin_sidecars(symbols, out_dir=str(tc.PINS_DIR), netlist=str(net))
    assert res["ok"] is False and res["refused"] == ["Proj:BaseAmp"]
    assert "D1" in res["reason"] and str(tmp_path / "pins") in res["reason"]
    assert run.calls == []                                      # nothing spawned or written


def test_stock_part_sidecars_may_still_go_to_the_central_dir(tmp_path, monkeypatch):
    summary = {"ok": True, "written": ["x"], "failed": [], "pins": {"Device:R": 2}}
    fake = FakeRun(stdout=json.dumps(summary))
    monkeypatch.setattr(tc, "run", fake)
    net = _project(tmp_path)
    res = k.build_pin_sidecars("Device:R", out_dir=str(tc.PINS_DIR), netlist=str(net))
    assert fake.calls == [[sys.executable, str(tc.TOOLS_DIR / "build_pin_sidecars.py"),
                           "--netlist", str(net), "--out", str(tc.PINS_DIR),
                           "--symbols", tc.symbols_dir(), "Device:R"]]
    assert res["ok"] is True and res["pins"] == {"Device:R": 2} and res["written"] == ["x"]


def test_netlist_sidecars_without_stock_symbols_leave_the_option_out(tmp_path, run, monkeypatch):
    monkeypatch.setattr(tc, "symbols_dir", lambda explicit="": explicit or None)
    net = _project(tmp_path)
    k.build_pin_sidecars(netlist=str(net))
    assert run.calls[0][2:] == ["--netlist", str(net), "--out", str(tmp_path / "pins")]


def test_netlist_sidecars_refuse_a_netlist_that_does_not_load(tmp_path, run):
    net = tmp_path / "broken.json"
    net.write_text("[]", encoding="utf-8")
    res = k.build_pin_sidecars(netlist=str(net))
    assert res["ok"] is False and str(net) in res["reason"] and run.calls == []


def test_netlist_sidecars_report_the_child_error(tmp_path, monkeypatch):
    monkeypatch.setattr(tc, "run", FakeRun(returncode=1, stdout='{"ok": false, "error": "bad"}'))
    res = k.build_pin_sidecars(netlist=str(_project(tmp_path)))
    assert res["ok"] is False and res["reason"] == "bad"


# ----------------------------------------------------------------------------
# design_report through the resolver
# ----------------------------------------------------------------------------
def test_design_report_reads_project_parts_through_the_resolver(tmp_path):
    res = k.design_report(str(_project(tmp_path)))
    assert res["ok"] is True and res["errors"] == []
    assert res["report"]["symbols_available"] is True
    floating = res["report"]["flags"]["floating_pins"]
    assert "U1.3" in floating and "R1.2" in floating            # pin types of both libraries


def test_design_report_reads_project_parts_without_stock_symbols(tmp_path, monkeypatch):
    monkeypatch.setattr(tc, "symbols_dir", lambda explicit="": explicit or None)
    net = _project(tmp_path, components=[{"ref": "U1", "lib_id": "Proj:BaseAmp", "value": "A",
                                          "footprint": ""}], nets={})
    res = k.design_report(str(net))
    assert res["ok"] is True and res["report"]["symbols_available"] is True


def test_design_report_fails_fast_on_a_missing_declared_library(tmp_path):
    net = _project(tmp_path, libraries={"symbols": {"Gone": "libs/Gone.kicad_sym"}})
    res = k.design_report(str(net))
    assert res["ok"] is False and "'Gone'" in res["reason"]
    assert str(tmp_path / "libs" / "Gone.kicad_sym") in res["reason"]
    assert not (tmp_path / "board_report.md").exists()


def test_design_report_without_any_symbols_skips_the_pin_checks(tmp_path, monkeypatch):
    monkeypatch.setattr(tc, "symbols_dir", lambda explicit="": explicit or None)
    net = tmp_path / "plain.json"
    net.write_text(json.dumps({"components": [{"ref": "R1", "lib_id": "Device:R"}],
                               "nets": {}}), encoding="utf-8")
    res = k.design_report(str(net))
    assert res["ok"] is True and res["report"]["symbols_available"] is False


# ----------------------------------------------------------------------------
# Structure: the tool surface and the server's line budget
# ----------------------------------------------------------------------------
def test_tool_signatures():
    assert list(inspect.signature(k.build_schematic_from_netlist).parameters) == [
        "netlist", "out_sch", "symbols_dir", "sidecars_dir", "run_erc", "backend"]
    params = inspect.signature(k.build_pin_sidecars).parameters
    assert list(params) == ["symbols", "out_dir", "symbols_dir", "netlist"]
    assert all(p.default == "" for p in params.values())
    assert k.build_pin_sidecars is sst.build_pin_sidecars and k.design_report is sst.design_report


def test_kicad_server_stays_under_its_line_budget():
    lines = Path(k.__file__).read_text(encoding="utf-8").splitlines()
    assert len(lines) < KICAD_SERVER_MAX_LINES, len(lines)


# ============================================================================
# Live tier
# ============================================================================
LG290P_NETS = {"VCC_3V3": [["U1", "23"], ["U1", "22"]],
               "GND": [["U1", p] for p in ["10", "12", "13"] + [str(n) for n in range(24, 80)]]}
LG290P_NC = [["U1", n] for n in ["9", "11", "8", "4", "20", "21", "6", "7", "16", "15", "18",
                                 "19", "3", "14"]]


def _lg290p_project(root: Path, power_lib: Path, extra_parts: tuple = ()) -> Path:
    """The authored LG290P in <root>/libs/SPS_parts.kicad_sym plus the given
    install's power library as a project library (no global table needed)."""
    libs = root / "libs"
    libs.mkdir(parents=True)
    sym_author.author_symbol(sym_author.lg290p_spec(), libs / "SPS_parts.kicad_sym")
    shutil.copyfile(power_lib, libs / "power.kicad_sym")
    comps = [{"ref": "U1", "lib_id": "SPS_parts:LG290P", "value": "LG290P", "footprint": ""}]
    nets = {name: list(pins) for name, pins in LG290P_NETS.items()}
    for ref, lib_id, pins in extra_parts:
        comps.append({"ref": ref, "lib_id": lib_id, "value": ref, "footprint": ""})
        for net, pad in pins:
            nets[net].append([ref, pad])
    net = root / "lg290p.json"
    net.write_text(json.dumps({
        "name": "lg290p", "components": comps, "nets": nets, "no_connect": LG290P_NC,
        "libraries": {"symbols": {"SPS_parts": "libs/SPS_parts.kicad_sym",
                                  "power": "libs/power.kicad_sym"}}}), encoding="utf-8")
    return net


def _erc_types(res: dict) -> list[str]:
    report = json.loads(Path(res["erc_report"]).read_text(encoding="utf-8"))
    return [v["type"] for sheet in report["sheets"] for v in sheet["violations"]]


def _no_leaked_servers(before: set[int] | None, wait_s: float = 20.0) -> set[int]:
    """kicad-cli processes this test started that are still alive after wait_s."""
    if before is None:
        return set()
    deadline = time.monotonic() + wait_s
    while True:
        leaked = (owned_pids() or set()) - before
        if not leaked or time.monotonic() > deadline:
            return leaked
        time.sleep(0.5)


def _api_install() -> env.KiCadInstall | None:
    from backends.sch_api import SchematicApi
    return env.pick_api() if SchematicApi.available() else None


@requires_kicad_cli
def test_live_file_backend_lg290p_project_has_no_lib_symbol_issues(tmp_path, monkeypatch):
    """Acceptance (KiCad 9 locally): the LG290P project builds on the file
    backend with sym-lib-table + .kicad_pro beside the schematic, and ERC
    (that install's kicad-cli) finds every library symbol."""
    monkeypatch.setattr(ipc, "connect", lambda: NO_GUI)
    monkeypatch.setattr(k, "_api_available", lambda: False)
    monkeypatch.setattr(tc, "symbols_dir", lambda explicit="": explicit or str(KICAD.symbols))
    monkeypatch.setattr(tc, "kicad_cli", lambda: str(KICAD.cli))
    net = _lg290p_project(tmp_path / "proj", KICAD.symbols / "power.kicad_sym")
    out = tmp_path / "out" / "lg290p.kicad_sch"
    res = k.build_schematic_from_netlist(str(net), out_sch=str(out))
    assert res["ok"] is True and res["backend"] == "file", res
    assert (out.parent / "sym-lib-table").is_file() and out.with_suffix(".kicad_pro").is_file()
    types = _erc_types(res)
    assert "lib_symbol_issues" not in types and "lib_symbol_mismatch" not in types, types
    assert res["erc_ok"] is True, types


def test_live_nightly_api_builds_the_lg290p_project(tmp_path):
    """Acceptance: the same project builds on the KiCad 11 API backend (the
    tables are there when KiCad creates the document) and ERC finds every
    library symbol."""
    install = _api_install()
    if install is None:
        pytest.skip("no KiCad 11 nightly with the schematic API")
    net = _lg290p_project(tmp_path, env.symbols_dir(install) / "power.kicad_sym")
    before = owned_pids()
    res = k.build_schematic_from_netlist(str(net))
    assert res["ok"] is True and res["backend"] == "api" and "fallback" not in res, res
    types = _erc_types(res)
    assert "lib_symbol_issues" not in types and "lib_symbol_mismatch" not in types, types
    assert not _no_leaked_servers(before)


def test_live_nightly_without_global_tables_falls_back_and_records_it(tmp_path, monkeypatch):
    """Acceptance: KICAD_CONFIG_HOME = an empty dir (the user's config is
    never touched): a stock part fails on the API, the build is redone on the
    file backend and the G2 decision is in the log."""
    install = _api_install()
    if install is None:
        pytest.skip("no KiCad 11 nightly with the schematic API")
    home = tmp_path / "config_home"
    home.mkdir()
    monkeypatch.setenv("KICAD_CONFIG_HOME", str(home))
    net = _lg290p_project(tmp_path / "proj", env.symbols_dir(install) / "power.kicad_sym",
                          extra_parts=(("R1", "Device:R", (("VCC_3V3", "1"), ("GND", "2"))),))
    before = owned_pids()
    res = k.build_schematic_from_netlist(str(net), run_erc=False)
    assert res["ok"] is True and res["backend"] == "file", res
    assert res["fallback"]["from"] == "api" and sess.GLOBAL_TABLES_MISSING in \
        res["fallback"]["reason"]
    (entry,) = dec.read_decisions(net)
    assert entry["id"] == res["fallback"]["decision"] and entry["stage"] == "G2"
    assert not (home / "10.99" / "sym-lib-table").exists()      # nothing wrote the config
    assert not _no_leaked_servers(before)
