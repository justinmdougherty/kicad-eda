"""toolchain's per-job kicad-cli picks.

Schematic jobs (kicad_cli / erc_json) take the NEWEST install; board jobs
(board_cli / drc_counts) take the install whose bundled Python (pcbnew) built
the board, and fall back to the newest only when no install has such a
Python. The installs are fakes handed to kicad_env.discover, so no KiCad is
needed.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

import pytest

import kicad_env as env
import toolchain as tc


def _install(base: Path, version: str, vt: tuple[int, int, int], *, python: bool) -> env.KiCadInstall:
    root = base / version
    return env.KiCadInstall(
        version=version, version_tuple=vt, root=root, kicad_cli=root / "bin" / "kicad-cli.exe",
        python=(root / "bin" / "python.exe") if python else None,
        share=root / "share" / "kicad", has_api_server=vt >= env.API_SERVER_MIN_VERSION)


@pytest.fixture
def machine(monkeypatch: pytest.MonkeyPatch) -> Callable[..., None]:
    """machine(*installs) makes kicad_env.discover() report exactly those
    installs, newest first, without probing anything."""
    def use(*installs: env.KiCadInstall) -> None:
        ordered = sorted(installs, key=lambda i: i.version_tuple, reverse=True)
        monkeypatch.setattr(env, "discover", lambda refresh=False: list(ordered))
    return use


@pytest.fixture
def nightly(tmp_path: Path) -> env.KiCadInstall:
    """10.99 nightly: api-server, no bundled Python."""
    return _install(tmp_path, "10.99", (10, 99, 0), python=False)


@pytest.fixture
def stable(tmp_path: Path) -> env.KiCadInstall:
    """9.0.4 stable: bundled Python imports pcbnew."""
    return _install(tmp_path, "9.0", (9, 0, 4), python=True)


# ----------------------------------------------------------------------------
# the picks
# ----------------------------------------------------------------------------
def test_mixed_machine_splits_schematic_and_board_clis(machine, nightly, stable) -> None:
    machine(stable, nightly)
    assert tc.kicad_cli() == str(nightly.kicad_cli)          # schematics: newest
    assert tc.board_cli() == str(stable.kicad_cli)           # boards: the pcbnew install
    assert tc.pick_board_cli() is stable
    assert tc.kicad_python() == str(stable.python)


def test_single_install_with_python_serves_both(machine, stable) -> None:
    machine(stable)
    assert tc.kicad_cli() == tc.board_cli() == str(stable.kicad_cli)
    assert tc.pick_board_cli() is stable


def test_no_pcbnew_python_anywhere_falls_back_to_newest(machine, tmp_path, nightly) -> None:
    old_no_py = _install(tmp_path, "8.0", (8, 0, 7), python=False)
    machine(old_no_py, nightly)
    assert tc.kicad_python() is None
    assert tc.pick_board_cli() is nightly
    assert tc.board_cli() == tc.kicad_cli() == str(nightly.kicad_cli)


def test_nothing_installed_gives_none_for_both(machine) -> None:
    machine()
    assert tc.kicad_cli() is None and tc.board_cli() is None and tc.pick_board_cli() is None
    with pytest.raises(RuntimeError, match="kicad-cli not found"):
        tc.drc_counts("x.kicad_pcb")


def test_policy_text_states_the_split_rule() -> None:
    text = tc.KICAD_CLI_POLICY.lower()
    assert "schematic" in text and "newest" in text
    assert "board" in text and "pcbnew" in text and "nightly" in text


# ----------------------------------------------------------------------------
# the runners use the right binary and say which one ran
# ----------------------------------------------------------------------------
class _Done:
    returncode, stdout, stderr = 0, "", ""


def _capture(monkeypatch: pytest.MonkeyPatch, report: dict) -> list[list[str]]:
    """Replace tc.run with a fake that records argv and writes `report` to the
    path after `-o` (what kicad-cli would do)."""
    calls: list[list[str]] = []

    def fake_run(args: list[str], **kw):
        calls.append(list(args))
        Path(args[args.index("-o") + 1]).write_text(json.dumps(report), encoding="utf-8")
        return _Done()

    monkeypatch.setattr(tc, "run", fake_run)
    return calls


def test_drc_counts_runs_the_board_cli_and_records_it(machine, nightly, stable, tmp_path,
                                                      monkeypatch) -> None:
    machine(stable, nightly)
    calls = _capture(monkeypatch, {"violations": [{"type": "clearance"}], "unconnected_items": []})
    pcb = tmp_path / "b.kicad_pcb"
    res = tc.drc_counts(str(pcb))
    assert calls[0][0] == str(stable.kicad_cli) and calls[0][1:3] == ["pcb", "drc"]
    assert res["kicad_cli"] == str(stable.kicad_cli)
    assert res["errors"] == 1 and res["types"] == {"clearance": 1} and res["unconnected"] == 0
    assert res["report"] == str(tmp_path / "b_drc.json")


def test_drc_counts_unreadable_report_still_names_the_cli(machine, stable, tmp_path,
                                                          monkeypatch) -> None:
    machine(stable)
    monkeypatch.setattr(tc, "run", lambda args, **kw: _Done())      # writes no report
    res = tc.drc_counts(str(tmp_path / "b.kicad_pcb"))
    assert res["errors"] == -1 and "DRC report unreadable" in res["error"]
    assert res["kicad_cli"] == str(stable.kicad_cli)


def test_erc_json_runs_the_newest_cli(machine, nightly, stable, tmp_path, monkeypatch) -> None:
    machine(stable, nightly)
    calls = _capture(monkeypatch, {"sheets": [{"violations": []}]})
    res = tc.erc_json(str(tmp_path / "s.kicad_sch"), str(tmp_path / "s_erc.json"))
    assert calls[0][0] == str(nightly.kicad_cli) and calls[0][1:3] == ["sch", "erc"]
    assert res["erc_ok"] and res["erc_violations"] == 0


# ----------------------------------------------------------------------------
# subprocess helper: stdin must never be the MCP server's pipe
# ----------------------------------------------------------------------------
def test_run_detaches_stdin_by_default(monkeypatch) -> None:
    seen: dict = {}
    monkeypatch.setattr(tc.subprocess, "run", lambda args, **kw: seen.update(kw) or args)
    tc.run(["kicad-cli", "version"], timeout=5)
    assert seen["stdin"] is tc.subprocess.DEVNULL
    assert seen["capture_output"] is True and seen["text"] is True and seen["timeout"] == 5


def test_run_keeps_an_explicit_stdin(monkeypatch) -> None:
    seen: dict = {}
    monkeypatch.setattr(tc.subprocess, "run", lambda args, **kw: seen.update(kw) or args)
    tc.run(["kicad-cli", "version"], stdin=None)
    assert seen["stdin"] is None


def test_rescan_tool_drops_the_cache_and_reports_fresh_picks(machine, stable, monkeypatch) -> None:
    import board_pipeline_tools as bpt
    cleared: list[bool] = []
    monkeypatch.setattr(env, "clear_cache", lambda: cleared.append(True))
    machine(stable)
    res = bpt.rescan_kicad_installs()
    assert cleared == [True]
    assert res["kicad_cli_board"] == str(stable.kicad_cli)
    assert res["kicad_python"] == str(stable.python)
    assert res["kicad_python_error"] is None and res["install_probe_errors"] == []
    assert [i["version"] for i in res["installs"]] == [stable.version]
    assert bpt.rescan_kicad_installs in bpt.TOOLS

