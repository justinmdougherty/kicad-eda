"""sch_stage_tools: the schematic-stage tools split out of kicad_server (X-04).

_build_via_sch_build (the file route of build_schematic_from_netlist),
build_pin_sidecars and design_report live in server/sch_stage_tools.py and are
imported back into kicad_server, whose TOOLS keeps serving them under the same
names in the same order. They look toolchain functions up at call time
(`tc.run`, `tc.symbols_dir`), so the patches below reach them through either
module. No KiCad needed: the child runs and the GUI connection are faked.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import kicad_ipc as ipc
import kicad_server as k
import sch_stage_tools as sst
import toolchain as tc

MOVED = ("_build_via_sch_build", "build_pin_sidecars", "design_report")
KICAD_SERVER_MAX_LINES = 720               # X-04 target; F2-14 keeps it there
NO_GUI = {"ipc_connected": False, "reason": "test: no GUI"}


class FakeRun:
    """Stands in for tc.run: records every argv, returns one canned result."""

    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.result = SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str], **kw: Any) -> SimpleNamespace:
        self.calls.append(list(args))
        return self.result


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch) -> None:
    """No GUI and no symbol libraries unless a test provides them."""
    monkeypatch.setattr(ipc, "connect", lambda: NO_GUI)
    monkeypatch.setattr(tc, "symbols_dir", lambda explicit="": None)


@pytest.fixture
def syms(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    """tc.symbols_dir resolves to tmp_path/symbols; records the explicit argument."""
    lib = tmp_path / "symbols"
    lib.mkdir()
    seen: dict = {"dir": str(lib), "explicit": []}

    def symbols_dir(explicit: str = "") -> str:
        seen["explicit"].append(explicit)
        return explicit or str(lib)

    monkeypatch.setattr(tc, "symbols_dir", symbols_dir)
    return seen


def _use_run(monkeypatch: pytest.MonkeyPatch, **result: Any) -> FakeRun:
    fake = FakeRun(**result)
    monkeypatch.setattr(tc, "run", fake)
    return fake


def _netlist(tmp_path: Path) -> Path:
    path = tmp_path / "board.json"
    path.write_text(json.dumps({
        "components": [{"ref": "R1", "lib_id": "Device:R", "value": "10k", "footprint": ""}],
        "nets": {"N1": [["R1", "1"]]}}), encoding="utf-8")
    return path


# ----------------------------------------------------------------------------
# The split itself
# ----------------------------------------------------------------------------
@pytest.mark.parametrize("name", MOVED)
def test_moved_function_lives_in_sch_stage_tools_and_is_imported_back(name: str):
    fn = getattr(sst, name)
    assert fn.__module__ == "sch_stage_tools"
    assert getattr(k, name) is fn


def test_kicad_server_tools_serve_the_moved_tools_in_their_old_positions():
    names = [fn.__name__ for fn in k.TOOLS]
    at = names.index("build_schematic_from_netlist")
    assert names[at + 1:at + 3] == ["build_pin_sidecars", "design_report"]
    assert k.TOOLS[at + 1] is sst.build_pin_sidecars and k.TOOLS[at + 2] is sst.design_report
    assert not hasattr(sst, "TOOLS")      # served from kicad_server.TOOLS, not a second module


def test_kicad_server_stays_under_its_line_budget():
    lines = Path(k.__file__).read_text(encoding="utf-8").splitlines()
    assert len(lines) < KICAD_SERVER_MAX_LINES, len(lines)


def test_paths_are_the_toolchain_ones():
    assert sst.TOOLS_DIR == tc.TOOLS_DIR == k.TOOLS_DIR
    assert sst.PINS_DIR == tc.PINS_DIR == k.PINS_DIR


_STANDALONE_IMPORT = r'''
import json, sys
sys.path[:0] = [sys.argv[1]]
import sch_stage_tools, kicad_env
moved = ("_build_via_sch_build", "build_pin_sidecars", "design_report")
print(json.dumps({"kicad_server": "kicad_server" in sys.modules,
                  "discovered": kicad_env._INSTALLS is not None,
                  "tools": [n for n in moved if callable(getattr(sch_stage_tools, n))]}))
'''


def test_sch_stage_tools_imports_on_its_own_without_kicad_server():
    # no import cycle back into kicad_server, and importing probes no KiCad
    r = subprocess.run([sys.executable, "-c", _STANDALONE_IMPORT, str(Path(sst.__file__).parent)],
                       capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL,
                       encoding="utf-8", errors="replace")
    assert r.returncode == 0, r.stderr[-2000:]
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert out == {"kicad_server": False, "discovered": False, "tools": list(MOVED)}


# ----------------------------------------------------------------------------
# _build_via_sch_build (file route of build_schematic_from_netlist)
# ----------------------------------------------------------------------------
def test_build_runs_sch_build_with_the_default_sidecars(tmp_path, syms, monkeypatch):
    run = _use_run(monkeypatch, stdout='noise\n{"ok": true, "components": 1}\n')
    net, out = str(_netlist(tmp_path)), str(tmp_path / "board.kicad_sch")
    res = sst._build_via_sch_build(net, out, "", "")
    assert run.calls == [[sys.executable, str(tc.TOOLS_DIR / "sch_build.py"), net, out,
                          syms["dir"], str(tc.PINS_DIR)]]
    assert res == {"ok": True, "components": 1, "sch": out}


def test_build_passes_explicit_symbol_and_sidecar_dirs(tmp_path, syms, monkeypatch):
    run = _use_run(monkeypatch, stdout='{"ok": true}')
    mine, sides = str(tmp_path / "mylib"), str(tmp_path / "mypins")
    sst._build_via_sch_build("n.json", "o.kicad_sch", mine, sides)
    assert syms["explicit"] == [mine]
    assert run.calls[0][-2:] == [mine, sides]


def test_build_without_a_symbols_dir_fails_before_running(tmp_path, monkeypatch):
    run = _use_run(monkeypatch, stdout='{"ok": true}')
    res = sst._build_via_sch_build("n.json", str(tmp_path / "o.kicad_sch"), "", "")
    assert res == {"ok": False,
                   "reason": "symbols_dir not given and KiCad share dir not found"}
    assert run.calls == []


def test_build_refuses_a_schematic_open_in_a_kicad_editor(tmp_path, syms, monkeypatch):
    out = str(tmp_path / "open.kicad_sch")
    monkeypatch.setattr(ipc, "connect", lambda: {
        "ipc_connected": True, "kicad": object(), "kicad_version": "9.0.4",
        "open_documents": [{"type": "schematic", "path": out}],
        "capabilities": ipc.capabilities((9, 0, 4))})
    run = _use_run(monkeypatch, stdout='{"ok": true}')
    res = sst._build_via_sch_build("n.json", out, "", "")
    assert res["ok"] is False and "Eeschema" in res["reason"]
    assert run.calls == []


@pytest.mark.parametrize("rc, stdout, ok", [(0, '{"ok": true}', True),
                                            (1, '{"ok": true}', False),
                                            (0, '{"ok": false}', False),
                                            (0, "no json at all", False)])
def test_build_needs_exit_code_zero_and_the_script_saying_ok(rc, stdout, ok, syms, monkeypatch):
    _use_run(monkeypatch, returncode=rc, stdout=stdout)
    res = sst._build_via_sch_build("n.json", "o.kicad_sch", "", "")
    assert res["ok"] is ok and res["sch"] == "o.kicad_sch" and "stderr" not in res


def test_build_keeps_the_stderr_tail(syms, monkeypatch):
    _use_run(monkeypatch, returncode=1, stdout="", stderr="x" * 2000 + "END")
    res = sst._build_via_sch_build("n.json", "o.kicad_sch", "", "")
    assert len(res["stderr"]) == 1200 and res["stderr"].endswith("END")


def test_build_schematic_from_netlist_file_backend_reaches_the_moved_route(
        tmp_path, syms, monkeypatch):
    monkeypatch.setattr(k, "_api_available", lambda: False)
    run = _use_run(monkeypatch, stdout='{"ok": true, "components": 1}')
    monkeypatch.setattr(tc, "kicad_cli", lambda: None)   # no ERC pass
    net = _netlist(tmp_path)
    res = k.build_schematic_from_netlist(str(net), backend="file")
    assert res["ok"] and res["backend"] == "file"
    assert res["sch"] == str(net.with_suffix(".kicad_sch"))
    assert run.calls[0][1].endswith("sch_build.py")


# ----------------------------------------------------------------------------
# build_pin_sidecars
# ----------------------------------------------------------------------------
def test_sidecars_default_to_the_server_pins_dir(syms, monkeypatch):
    summary = {"ok": True, "written": ["Device_R.pins.json"], "failed": []}
    run = _use_run(monkeypatch, stdout=json.dumps(summary), stderr="warn")
    res = k.build_pin_sidecars(" Device:R, Device:C ,,")
    assert run.calls == [[sys.executable, str(tc.TOOLS_DIR / "build_pin_sidecars.py"),
                          syms["dir"], str(tc.PINS_DIR), "Device:R", "Device:C"]]
    assert res == {"ok": True, "out_dir": str(tc.PINS_DIR), "written": ["Device_R.pins.json"],
                   "failed": [], "stdout": json.dumps(summary), "stderr": "warn"}


def test_sidecars_honour_out_dir_and_symbols_dir(tmp_path, syms, monkeypatch):
    run = _use_run(monkeypatch, stdout='{"ok": true, "written": []}')
    out, lib = str(tmp_path / "pins"), str(tmp_path / "lib")
    res = sst.build_pin_sidecars("Device:R", out_dir=out, symbols_dir=lib)
    assert syms["explicit"] == [lib] and run.calls[0][2:4] == [lib, out]
    assert res["out_dir"] == out


def test_sidecars_refuse_an_empty_symbol_list(syms, monkeypatch):
    run = _use_run(monkeypatch)
    with pytest.raises(ValueError, match="at least one Library:Symbol"):
        sst.build_pin_sidecars(" , ")
    assert run.calls == []


def test_sidecars_without_a_symbols_dir_fail_before_running(monkeypatch):
    run = _use_run(monkeypatch)
    res = sst.build_pin_sidecars("Device:R")
    assert res == {"ok": False,
                   "reason": "symbols_dir not given and KiCad share dir not found"}
    assert run.calls == []


@pytest.mark.parametrize("rc, stdout, ok", [(0, "", True),
                                            (0, '{"ok": false, "failed": ["X:Y"]}', False),
                                            (2, '{"ok": true}', False)])
def test_sidecars_ok_follows_exit_code_and_summary(rc, stdout, ok, syms, monkeypatch):
    _use_run(monkeypatch, returncode=rc, stdout=stdout, stderr="e" * 900)
    res = sst.build_pin_sidecars("Device:R")
    assert res["ok"] is ok and len(res["stderr"]) == 800


# ----------------------------------------------------------------------------
# design_report
# ----------------------------------------------------------------------------
def test_design_report_writes_the_markdown_beside_the_netlist(tmp_path):
    net = _netlist(tmp_path)
    res = k.design_report(str(net))
    md_path = tmp_path / "board_report.md"
    assert res["ok"] is True and res["errors"] == [] and "reason" not in res
    assert res["markdown_path"] == str(md_path)
    assert md_path.read_text(encoding="utf-8") == res["markdown"]
    assert isinstance(res["report"], dict)


def test_design_report_honours_out_md_and_the_patched_symbols_dir(tmp_path, monkeypatch):
    empty = tmp_path / "empty_lib"
    empty.mkdir()
    monkeypatch.setattr(tc, "symbols_dir", lambda explicit="": str(empty))
    out_md = tmp_path / "review" / "custom.md"
    out_md.parent.mkdir()
    res = sst.design_report(str(_netlist(tmp_path)), out_md=str(out_md))
    assert res["markdown_path"] == str(out_md) and out_md.is_file()
    assert res["ok"] is False                     # Device:R unreadable from an empty lib
    assert res["reason"].startswith("design review could not run for 1 symbol(s)")
