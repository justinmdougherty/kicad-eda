"""toolchain's per-job kicad-cli picks.

Schematic jobs (kicad_cli / erc_json) take the NEWEST install; board jobs
(board_cli(pcb) / drc_counts) take the install of the KiCad that WROTE the
board file (its generator_version header), else the oldest install that can
read it, else the newest with a note; without a readable header they take the
pcbnew install and fall back to the newest only when no install has such a
Python. SWIG tools refuse a board newer than the pcbnew install can read
(swig_readable). The installs are fakes handed to kicad_env.discover and the
boards are header-only files, so no KiCad is needed.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable

import pytest

import kicad_env as env
import toolchain as tc


def _install(base: Path, version: str, vt: tuple[int, int, int], *,
             python: bool) -> env.KiCadInstall:
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


def test_policy_text_states_the_file_aware_rule() -> None:
    text = tc.KICAD_CLI_POLICY.lower()
    assert "schematic" in text and "newest" in text
    assert "board" in text and "pcbnew" in text and "nightly" in text
    assert "wrote the board file" in text and "generator_version" in text
    assert "kicad-cli 11" in text


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
    pcb = _board(tmp_path, "9.0", 20241229)
    res = tc.drc_counts(pcb)
    assert calls[0][0] == str(stable.kicad_cli) and calls[0][1:3] == ["pcb", "drc"]
    assert calls[0][-1] == pcb and "--severity-error" in calls[0]
    assert res["kicad_cli"] == str(stable.kicad_cli)
    assert res["errors"] == 1 and res["types"] == {"clearance": 1} and res["unconnected"] == 0
    assert res["report"] == str(tmp_path / "b_drc.json")


def test_drc_counts_unreadable_report_still_names_the_cli(machine, stable, tmp_path,
                                                          monkeypatch) -> None:
    machine(stable)
    monkeypatch.setattr(tc, "run", lambda args, **kw: _Done())      # writes no report
    res = tc.drc_counts(_board(tmp_path, "9.0", 20241229))
    assert res["errors"] == -1 and "wrote no DRC report b_drc.json" in res["error"]
    assert res["kicad_cli"] == str(stable.kicad_cli)
    assert (res["unconnected"], res["types"]) == (-1, {})
    assert res["report"] == str(tmp_path / "b_drc.json")


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
    assert seen["encoding"] == "utf-8" and seen["errors"] == "replace"   # wx.LogStderr is UTF-8


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


# ----------------------------------------------------------------------------
# board file headers: which KiCad wrote the file
# ----------------------------------------------------------------------------
NO_INFO = {"format_version": None, "generator": None, "generator_version": None}


def _board(tmp_path: Path, generator_version: str | None, format_version: int = 20260901,
           name: str = "b.kicad_pcb", quoted: bool = True) -> str:
    """Write a .kicad_pcb whose header claims `generator_version` (None: a
    KiCad 7-style header without one; quoted=False: KiCad 7's bare tokens)."""
    q = '"' if quoted else ""
    gen_line = (f"\t(generator_version {q}{generator_version}{q})\n"
                if generator_version is not None else "")
    text = (f"(kicad_pcb\n\t(version {format_version})\n\t(generator {q}pcbnew{q})\n"
            f"{gen_line}\t(general\n\t\t(thickness 1.6)\n\t)\n\t(paper \"A4\")\n)\n")
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return str(path)


def test_board_file_info_reads_a_kicad9_header(tmp_path) -> None:
    pcb = _board(tmp_path, "9.0", 20241229)
    assert tc.board_file_info(pcb) == {"format_version": 20241229, "generator": "pcbnew",
                                       "generator_version": "9.0"}


def test_board_file_info_reads_a_kicad11_header(tmp_path) -> None:
    pcb = _board(tmp_path, "10.99", 20260901)
    assert tc.board_file_info(pcb) == {"format_version": 20260901, "generator": "pcbnew",
                                       "generator_version": "10.99"}


def test_board_file_info_reads_an_unquoted_kicad7_header(tmp_path) -> None:
    pcb = _board(tmp_path, None, 20221018, quoted=False)
    assert tc.board_file_info(pcb) == {"format_version": 20221018, "generator": "pcbnew",
                                       "generator_version": None}


@pytest.mark.parametrize("content", [b"", b"hello world", b"(kicad_pcb (version x) (generator )",
                                     bytes(range(256))])
def test_board_file_info_garbage_is_all_none(tmp_path, content: bytes) -> None:
    junk = tmp_path / "junk.kicad_pcb"
    junk.write_bytes(content)
    assert tc.board_file_info(str(junk)) == NO_INFO


def test_board_file_info_missing_file_never_raises(tmp_path) -> None:
    assert tc.board_file_info(str(tmp_path / "missing.kicad_pcb")) == NO_INFO
    assert tc.board_file_info("") == NO_INFO
    assert tc.board_file_info(str(tmp_path)) == NO_INFO          # a directory


# ----------------------------------------------------------------------------
# board jobs use the kicad-cli of the KiCad that WROTE the file
# ----------------------------------------------------------------------------
def test_board_cli_follows_the_writer_of_a_kicad9_file(machine, nightly, stable, tmp_path) -> None:
    machine(stable, nightly)
    pcb = _board(tmp_path, "9.0", 20241229)
    assert tc.pick_board_cli(pcb) is stable
    assert tc.board_cli(pcb) == str(stable.kicad_cli)
    assert tc.board_cli_note(pcb) is None


def test_board_cli_follows_the_writer_of_a_kicad11_file(machine, nightly, stable, tmp_path) -> None:
    machine(stable, nightly)
    pcb = _board(tmp_path, "10.99", 20260901)
    assert tc.pick_board_cli(pcb) is nightly                 # not the pcbnew install
    assert tc.board_cli(pcb) == str(nightly.kicad_cli)
    assert tc.board_cli_note(pcb) is None


def test_board_cli_takes_the_oldest_install_that_can_read_an_older_file(machine, nightly, stable,
                                                                         tmp_path) -> None:
    machine(stable, nightly)
    pcb = _board(tmp_path, "8.0", 20240108)
    assert tc.pick_board_cli(pcb) is stable            # 9.0.4 reads an 8.0 file; not the nightly
    assert tc.board_cli_note(pcb) is None


def test_board_cli_falls_back_to_the_newest_with_a_note_for_a_future_file(machine, nightly, stable,
                                                                           tmp_path) -> None:
    machine(stable, nightly)
    pcb = _board(tmp_path, "12.0", 20300101, name="future.kicad_pcb")
    assert tc.pick_board_cli(pcb) is nightly
    note = tc.board_cli_note(pcb)
    assert note and "future.kicad_pcb" in note and "12.0" in note and "20300101" in note
    assert nightly.version in note and "newest" in note


def test_board_cli_without_a_readable_header_keeps_the_pcbnew_rule(machine, nightly, stable,
                                                                    tmp_path) -> None:
    machine(stable, nightly)
    missing = str(tmp_path / "missing.kicad_pcb")
    assert tc.pick_board_cli() is stable
    assert tc.pick_board_cli(missing) is stable
    assert tc.pick_board_cli(_board(tmp_path, None, 20221018, quoted=False)) is stable
    assert tc.board_cli_note(missing) is None and tc.board_cli_note() is None


def test_board_cli_without_a_pcbnew_install_reads_any_file_with_the_newest(machine, tmp_path,
                                                                            nightly) -> None:
    machine(nightly)
    pcb = _board(tmp_path, "9.0", 20241229)
    assert tc.pick_board_cli(pcb) is nightly and tc.board_cli_note(pcb) is None


def test_board_cli_with_nothing_installed_is_none_for_any_file(machine, tmp_path) -> None:
    machine()
    pcb = _board(tmp_path, "10.99")
    assert tc.pick_board_cli(pcb) is None and tc.board_cli(pcb) is None
    assert tc.board_cli_note(pcb) is None


def test_release_match_is_token_safe() -> None:
    assert tc._release_matches("9.0.4", "9.0")
    assert tc._release_matches("10.99.0-4025-gf7ca62", "10.99")
    assert tc._release_matches("9.0.4", "9.0.4")
    assert not tc._release_matches("10.99.0", "10.9")


# ----------------------------------------------------------------------------
# SWIG tools refuse a board the pcbnew install cannot read
# ----------------------------------------------------------------------------
def test_swig_readable_refuses_a_kicad11_board_on_a_kicad9_pcbnew(machine, nightly, stable,
                                                                  tmp_path) -> None:
    machine(stable, nightly)
    pcb = _board(tmp_path, "10.99", 20260901, name="lm358.kicad_pcb")
    reason = tc.swig_readable(pcb)
    assert reason is not None
    assert reason.startswith("lm358.kicad_pcb was saved by KiCad 10.99 (format 20260901)")
    assert f"KiCad {stable.version}'s pcbnew Python cannot read it" in reason
    assert "route_check / route_status for a KiCad 11 board" in reason
    assert "KiCad 9 GUI session" in reason


@pytest.mark.parametrize("generator_version", ["9.0", "8.0"])
def test_swig_readable_accepts_a_board_the_pcbnew_install_can_load(
        machine, nightly, stable, tmp_path, generator_version: str) -> None:
    machine(stable, nightly)
    assert tc.swig_readable(_board(tmp_path, generator_version)) is None


@pytest.mark.parametrize("generator_version, label", [("10.0", "KiCad 10 board"),
                                                      ("9.1", "KiCad 9 board")])
def test_swig_readable_is_minor_aware(machine, stable, tmp_path, generator_version: str,
                                      label: str) -> None:
    machine(stable)                        # a 9.0.4 pcbnew cannot load a 9.1 or 10.0 board either
    reason = tc.swig_readable(_board(tmp_path, generator_version))
    assert reason and label in reason


def test_swig_readable_stays_quiet_when_it_cannot_tell(machine, nightly, stable, tmp_path) -> None:
    machine(stable, nightly)
    assert tc.swig_readable(str(tmp_path / "missing.kicad_pcb")) is None
    assert tc.swig_readable(_board(tmp_path, None, 20221018, quoted=False)) is None
    machine(nightly)                       # no pcbnew install at all: kicad_python() speaks for it
    assert tc.swig_readable(_board(tmp_path, "10.99")) is None


# ----------------------------------------------------------------------------
# drc_counts and the board tools pass the file through
# ----------------------------------------------------------------------------
def _done(stdout: str = ""):
    return type("R", (), {"returncode": 0, "stdout": stdout, "stderr": ""})()


def _record(monkeypatch: pytest.MonkeyPatch, stdout: str = "") -> list[list[str]]:
    """Replace tc.run with a fake that only records argv (no report written)."""
    calls: list[list[str]] = []
    monkeypatch.setattr(tc, "run", lambda args, **kw: calls.append(list(args)) or _done(stdout))
    return calls


def test_drc_counts_picks_the_cli_by_the_board_file(machine, nightly, stable, tmp_path,
                                                    monkeypatch) -> None:
    machine(stable, nightly)
    calls = _capture(monkeypatch, {"violations": [], "unconnected_items": []})
    new = _board(tmp_path, "10.99", name="new.kicad_pcb")
    old = _board(tmp_path, "9.0", 20241229, name="old.kicad_pcb")
    assert tc.drc_counts(new)["kicad_cli"] == str(nightly.kicad_cli)
    assert tc.drc_counts(old)["kicad_cli"] == str(stable.kicad_cli)
    assert [c[0] for c in calls] == [str(nightly.kicad_cli), str(stable.kicad_cli)]
    assert [c[-1] for c in calls] == [new, old]
    assert "kicad_cli_note" not in tc.drc_counts(old)


def test_drc_counts_carries_the_fallback_note(machine, nightly, stable, tmp_path,
                                              monkeypatch) -> None:
    machine(stable, nightly)
    _capture(monkeypatch, {"violations": [], "unconnected_items": []})
    res = tc.drc_counts(_board(tmp_path, "12.0"))
    assert res["kicad_cli"] == str(nightly.kicad_cli) and "12.0" in res["kicad_cli_note"]


def test_board_jobs_use_the_cli_of_the_kicad_that_wrote_the_file(machine, nightly, stable, tmp_path,
                                                                  monkeypatch) -> None:
    import board_pipeline_tools as bpt
    machine(stable, nightly)
    monkeypatch.setattr(bpt.ipc, "open_in_editor", lambda path: False)
    pcb = _board(tmp_path, "10.99")
    _capture(monkeypatch, {"violations": [], "unconnected_items": []})
    drc = bpt.drc_pcb(pcb)
    assert drc["ok"] and drc["kicad_cli"] == str(nightly.kicad_cli) and "kicad_cli_note" not in drc
    calls = _record(monkeypatch)
    out = str(tmp_path / "fab")
    ger = bpt.export_gerbers(pcb, out)
    ren = bpt.render_pcb(pcb, out_png=str(tmp_path / "b.png"))
    fab = bpt.export_fab_package(pcb, out)
    assert ger["kicad_cli"] == ren["kicad_cli"] == fab["kicad_cli"] == str(nightly.kicad_cli)
    assert {c[0] for c in calls} == {str(nightly.kicad_cli)} and len(calls) == 6
    assert all(pcb in c for c in calls)


def test_drc_pcb_surfaces_the_fallback_note(machine, nightly, stable, tmp_path,
                                            monkeypatch) -> None:
    import board_pipeline_tools as bpt
    machine(stable, nightly)
    monkeypatch.setattr(bpt.ipc, "open_in_editor", lambda path: False)
    _capture(monkeypatch, {"violations": [], "unconnected_items": []})
    res = bpt.drc_pcb(_board(tmp_path, "12.0"))
    assert res["ok"] and "12.0" in res["kicad_cli_note"]


def test_swig_tools_refuse_a_board_pcbnew_cannot_read(machine, nightly, stable, tmp_path,
                                                      monkeypatch) -> None:
    import board_pipeline_tools as bpt
    machine(stable, nightly)
    monkeypatch.setattr(tc, "run",
                        lambda args, **kw: pytest.fail(f"ran {args[:2]} on an unreadable board"))
    pcb = _board(tmp_path, "10.99", name="lm358.kicad_pcb")
    # stage "routed" on such a board goes to the board API gate instead
    # (tests/test_board_route_tools.py); the placement stage still refuses
    for res in (bpt.check_design_gates(pcb, str(tmp_path / "netlist.json"), "placement"),
                bpt.autoroute_board(pcb),
                bpt.add_ground_stitching(pcb)):
        assert res["ok"] is False
        assert res["reason"].startswith("lm358.kicad_pcb was saved by KiCad 10.99")
        assert "route_check / route_status" in res["reason"]


def test_swig_tools_still_run_on_a_board_pcbnew_can_read(machine, nightly, stable, tmp_path,
                                                          monkeypatch) -> None:
    import board_pipeline_tools as bpt
    machine(stable, nightly)
    calls = _record(monkeypatch, stdout='{"issues": [], "warnings": []}')
    pcb = _board(tmp_path, "9.0", 20241229)
    netlist = str(tmp_path / "netlist.json")
    res = bpt.check_design_gates(pcb, netlist, "routed")
    assert res == {"ok": True, "stage": "routed", "passed": True, "issues": []}
    assert calls[0][0] == str(stable.python) and calls[0][2:] == ["routed", pcb, netlist]


def test_pipeline_board_cli_follows_the_file(machine, nightly, stable, tmp_path) -> None:
    import run_pipeline as rp
    machine(stable, nightly)
    new = _board(tmp_path, "10.99", name="new.kicad_pcb")
    old = _board(tmp_path, "9.0", 20241229, name="old.kicad_pcb")
    assert rp._board_cli(new) == str(nightly.kicad_cli)
    assert rp._board_cli(old) == str(stable.kicad_cli)


# ----------------------------------------------------------------------------
# run: child output is UTF-8 (wx.LogStderr writes UTF-8), never a decode crash
# ----------------------------------------------------------------------------
def test_run_decodes_child_output_as_utf8_with_replacement() -> None:
    code = ("import sys; sys.stdout.buffer.write('µm Ω ✓'.encode('utf-8') + b'\\xff'); "
            "sys.stderr.buffer.write('Fehler: Größe'.encode('utf-8'))")
    done = tc.run([sys.executable, "-c", code], timeout=60)
    assert done.returncode == 0
    assert done.stdout == "µm Ω ✓�" and done.stderr == "Fehler: Größe"


def test_importing_toolchain_leaves_kicad_ipc_and_sys_path_alone() -> None:
    """The pipeline imports toolchain: that must not load kicad_ipc (which puts
    the vendored kipy at the front of sys.path) or drc_guard (server/tools)."""
    code = ("import sys; sys.path.insert(0, sys.argv[1]); before = list(sys.path); "
            "import toolchain; "
            "print([m for m in ('kicad_ipc', 'drc_guard', 'kipy') if m in sys.modules]); "
            "print(sys.path == before)")
    done = tc.run([sys.executable, "-c", code, str(tc.SERVER_DIR)], timeout=60)
    assert done.returncode == 0, done.stderr
    assert done.stdout.split() == ["[]", "True"]


# ----------------------------------------------------------------------------
# drc_counts delegates to drc_guard.run_drc: never a stale or half-run report
# ----------------------------------------------------------------------------
def _fake_run(monkeypatch: pytest.MonkeyPatch, *, report: dict | None = None, rc: int = 0,
              stdout: str = "") -> list[dict]:
    """tc.run standing in for kicad-cli DRC: records argv, timeout and whether
    the report existed when it started; writes `report` (None: nothing)."""
    seen: list[dict] = []

    def fake(args: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
        out = Path(args[args.index("-o") + 1])
        seen.append({"args": list(args), "timeout": kw.get("timeout"),
                     "report_existed": out.exists()})
        if report is not None:
            out.write_text(json.dumps(report), encoding="utf-8")
        return subprocess.CompletedProcess(args, rc, stdout, "")

    monkeypatch.setattr(tc, "run", fake)
    return seen


def test_drc_counts_deletes_the_last_report_before_kicad_cli_runs(machine, stable, tmp_path,
                                                                 monkeypatch) -> None:
    machine(stable)
    pcb = _board(tmp_path, "9.0", 20241229)
    stale = tmp_path / "b_drc.json"
    stale.write_text(json.dumps({"violations": [{"type": "clearance"}] * 5}), encoding="utf-8")
    seen = _fake_run(monkeypatch)                                   # writes nothing this time
    res = tc.drc_counts(pcb)
    assert seen[0]["report_existed"] is False and not stale.exists()
    assert seen[0]["timeout"] == tc.KICAD_CLI_TIMEOUT_S
    assert res["errors"] == -1 and "wrote no DRC report" in res["error"]
    seen = _fake_run(monkeypatch, report={"violations": [], "unconnected_items": [{}]})
    fresh = tc.drc_counts(pcb)
    assert (fresh["errors"], fresh["unconnected"], fresh["types"]) == (0, 1, {})
    assert "error" not in fresh and fresh["kicad_cli"] == str(stable.kicad_cli)


@pytest.mark.parametrize("rc, crash, words", [
    (3, False, "kicad-cli pcb drc exited 3"),
    (3221226356, True, "kicad-cli crashed"),
    (-1073740940, True, "heap corruption")])
def test_drc_counts_reports_a_failed_or_crashed_kicad_cli(machine, stable, tmp_path, monkeypatch,
                                                          rc: int, crash: bool,
                                                          words: str) -> None:
    machine(stable)
    _fake_run(monkeypatch, report={"violations": [], "unconnected_items": []}, rc=rc)
    res = tc.drc_counts(_board(tmp_path, "9.0", 20241229))
    assert res["errors"] == -1 and res["unconnected"] == -1 and res["types"] == {}
    assert words in res["error"] and res["crash"] is crash and res["rc"] == rc


def test_drc_counts_reports_an_error_line_even_with_exit_0(machine, stable, tmp_path,
                                                           monkeypatch) -> None:
    machine(stable)
    _fake_run(monkeypatch, report={"violations": [], "unconnected_items": []},
              stdout="12:00:01: Error: Failed to load board\n")
    res = tc.drc_counts(_board(tmp_path, "9.0", 20241229))
    assert res["errors"] == -1 and "Error: Failed to load board" in res["error"]


def test_drc_counts_refuses_a_missing_board_or_an_undeletable_report(machine, stable, tmp_path,
                                                                     monkeypatch) -> None:
    machine(stable)
    monkeypatch.setattr(tc, "run", lambda args, **kw: pytest.fail("kicad-cli ran"))
    missing = tc.drc_counts(str(tmp_path / "gone.kicad_pcb"))
    assert missing["errors"] == -1 and "board file not found" in missing["error"]
    pcb = _board(tmp_path, "9.0", 20241229)
    (tmp_path / "b_drc.json").mkdir()                     # a report path that cannot be deleted
    stuck = tc.drc_counts(pcb)
    assert stuck["errors"] == -1 and "cannot be deleted" in stuck["error"]


def test_drc_check_carries_the_run_failure_as_its_reason(machine, stable, tmp_path,
                                                         monkeypatch) -> None:
    machine(stable)
    _fake_run(monkeypatch, rc=3)
    res = tc.drc_check(_board(tmp_path, "9.0", 20241229), 5)
    assert res["ok"] is False and "exited 3" in res["reason"] and "errors" not in res
    assert res["kicad_cli"] == str(stable.kicad_cli) and res["report"].endswith("b_drc.json")


# ============================================================================
# keep_agent_copy (an agent's KiCad 11 copy is rotated, never lost)
# ============================================================================
SET_EXTS = (".kicad_pcb", ".kicad_pro", ".kicad_prl", ".kicad_dru")
K11_SET = {".kicad_pro": '{"meta": {"version": 3}}\n', ".kicad_prl": '{"meta": {"version": 7}}\n',
           ".kicad_dru": "(version 1)\n"}


def _sib(pcb: str, tag: str, ext: str) -> Path:
    return Path(str(Path(pcb).with_suffix("")) + tag + ext)


def _k11_set(tmp_path: Path, *, dru: bool = True) -> str:
    """netlist.kicad_pcb saved by KiCad 11 with its project, prl, rules and lock."""
    pcb = _board(tmp_path, "10.99", 20260901, name="netlist.kicad_pcb")
    for ext, text in K11_SET.items():
        if dru or ext != ".kicad_dru":
            _sib(pcb, "", ext).write_text(text, encoding="utf-8")
    (tmp_path / "~netlist.kicad_pro.lck").write_text("{}", encoding="utf-8")
    return pcb


def _set(pcb: str, tag: str = ".agent") -> dict[str, bytes]:
    """{ext: bytes} of the X<tag>.* files that exist (tag "" = the canonical set)."""
    return {ext: _sib(pcb, tag, ext).read_bytes() for ext in SET_EXTS
            if _sib(pcb, tag, ext).is_file()}


def _listing(folder: Path) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in folder.iterdir() if p.is_file()}


def _append(path: str | Path, text: str) -> None:
    with open(path, "a", encoding="utf-8", newline="") as f:
        f.write(text)


def test_keep_agent_copy_copies_the_set_and_rotates_older_copies(tmp_path) -> None:
    pcb = _k11_set(tmp_path)
    first_set = _set(pcb, "")
    first = tc.keep_agent_copy(pcb, move=False)                # the harness preservation
    assert first["agent_copy"] == tc.agent_copy_path(pcb) and first["previous_agent_copy"] is None
    assert sorted(Path(f).name for f in first["files"]) == [
        "netlist.agent.kicad_dru", "netlist.agent.kicad_pcb", "netlist.agent.kicad_prl",
        "netlist.agent.kicad_pro"]
    assert first["rotated"] == [] and _set(pcb) == first_set and _set(pcb, "") == first_set
    _append(pcb, "; second\n")
    second = tc.keep_agent_copy(pcb, move=False)
    assert second["previous_agent_copy"] == str(_sib(pcb, ".agent.1", ".kicad_pcb"))
    assert sorted(Path(f).name for f in second["rotated"]) == [
        "netlist.agent.1.kicad_dru", "netlist.agent.1.kicad_pcb", "netlist.agent.1.kicad_prl",
        "netlist.agent.1.kicad_pro"]
    assert _set(pcb, ".agent.1") == first_set                  # the first copy survives
    assert _set(pcb)[".kicad_pcb"].endswith(b"; second\n")
    third = tc.keep_agent_copy(pcb, move=False)
    assert third["previous_agent_copy"] == str(_sib(pcb, ".agent.2", ".kicad_pcb"))
    assert _set(pcb, ".agent.1") == first_set
    assert _set(pcb, ".agent.2")[".kicad_pcb"].endswith(b"; second\n")


def test_keep_agent_copy_move_takes_the_kicad11_files_and_copies_the_rules(tmp_path) -> None:
    pcb = _k11_set(tmp_path)
    canonical = _set(pcb, "")
    res = tc.keep_agent_copy(pcb, move=True)                   # the snapshot restore's step
    assert _set(pcb) == canonical and res["previous_agent_copy"] is None
    assert _set(pcb, "") == {".kicad_dru": canonical[".kicad_dru"]}   # KiCad 11 never writes it
    assert (tmp_path / "~netlist.kicad_pro.lck").is_file()     # the lock is not part of the set


def test_keep_agent_copy_takes_the_first_free_number(tmp_path) -> None:
    pcb = _k11_set(tmp_path, dru=False)
    _sib(pcb, ".agent", ".kicad_pcb").write_text("agent 0", encoding="utf-8")
    _sib(pcb, ".agent.1", ".kicad_pcb").write_text("agent 1", encoding="utf-8")
    _sib(pcb, ".agent.2", ".kicad_pro").write_text("{}", encoding="utf-8")   # holds 2 too
    res = tc.keep_agent_copy(pcb, move=False)
    assert res["previous_agent_copy"] == str(_sib(pcb, ".agent.3", ".kicad_pcb"))
    assert _sib(pcb, ".agent.3", ".kicad_pcb").read_text(encoding="utf-8") == "agent 0"
    assert _sib(pcb, ".agent.1", ".kicad_pcb").read_text(encoding="utf-8") == "agent 1"
    assert not _sib(pcb, ".agent.2", ".kicad_pcb").exists()


def test_keep_agent_copy_refuses_a_missing_board(tmp_path) -> None:
    before = _listing(tmp_path)
    with pytest.raises(ValueError, match="not found; there is no board to keep"):
        tc.keep_agent_copy(str(tmp_path / "netlist.kicad_pcb"), move=False)
    assert _listing(tmp_path) == before


@pytest.mark.parametrize("move, failing_copy", [(True, 1), (False, 3)])
def test_keep_agent_copy_undoes_every_step_when_one_fails(tmp_path, monkeypatch, move: bool,
                                                          failing_copy: int) -> None:
    pcb = _k11_set(tmp_path)
    tc.keep_agent_copy(pcb, move=False)                        # an older copy to rotate
    _append(pcb, "; newer\n")
    before = _listing(tmp_path)
    real = shutil.copy2
    calls: list[str] = []

    def flaky(src: str | Path, dst: str | Path, **kw: bool) -> object:
        calls.append(str(dst))
        if len(calls) == failing_copy:
            raise OSError("disk full")
        return real(src, dst, **kw)

    monkeypatch.setattr(shutil, "copy2", flaky)
    with pytest.raises(OSError, match="disk full"):
        tc.keep_agent_copy(pcb, move=move)
    assert _listing(tmp_path) == before and not _sib(pcb, ".agent.1", ".kicad_pcb").exists()


@pytest.mark.parametrize("move", [True, False])
def test_keep_agent_copy_never_overwrites_what_squats_on_an_agent_name(tmp_path,
                                                                     move: bool) -> None:
    pcb = _k11_set(tmp_path)
    tc.keep_agent_copy(pcb, move=False)                        # an older copy to rotate
    _sib(pcb, ".agent", ".kicad_prl").unlink()
    _sib(pcb, ".agent", ".kicad_prl").mkdir()                  # not a file: never rotated
    before = _listing(tmp_path)
    with pytest.raises(FileExistsError, match="agent.kicad_prl exists; refusing to overwrite"):
        tc.keep_agent_copy(pcb, move=move)
    assert _listing(tmp_path) == before and not _sib(pcb, ".agent.1", ".kicad_pcb").exists()
    assert _sib(pcb, ".agent", ".kicad_prl").is_dir()
