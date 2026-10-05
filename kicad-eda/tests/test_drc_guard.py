"""drc_guard: kicad-cli DRC that never reads a stale report, and the rules
canary that proves a .kicad_dru compiled (ADR D7.8, R23).

Unit tier (no KiCad): run_drc with a fake kicad-cli runner -- rc 0 with a
fresh report gives counts; an older <stem>_drc.json is deleted before the run
and never read; rc != 0 (10.99's rc 3 for a broken rules file), a crash
(0xC0000374 heap corruption, a signal, any rc > 255), a missing or unparseable
report, an `Error:` line, a timeout or a launch failure give
{error, rc, crash}. rules_compile copies the shipped canary board to a temp
dir with the text (+ CANARY_RULE when absent), runs DRC --severity-all and is
ok only for rc 0 + a fresh report + >= 1 track_width violation on the canary
net (never by rule name: 9.0.4 omits it).

Live tier, per installed release (skipped when that release is absent): the
valid text compiles on 9.0.4, 10.0.5 (the K10 Docker recipe) and 10.99;
broken text ('(min 135deg)', 'Bogus.Cu') is dropped silently with rc 0 by
9.0.4 and 10.0.5 (ok false, fired 0) and gives rc 3 on 10.99; a bare
intersectsArea crashes 9.0.4 (crash true).

The canary (server/data/drc_canary.kicad_pcb + .kicad_pro) is a 10 x 10 mm
2-layer KiCad 9 board with one 0.2 mm F.Cu track on net __KICAD_EDA_CANARY__;
its project keeps min_track_width 0, so only a compiled rules file can flag
the track. It was made by this script under KiCad 9.0.4's bundled Python
(stdin closed; SaveBoard also writes canary.kicad_pro), then both files were
renamed to drc_canary.* and the project's meta.filename set to match:

    import wx
    wx.DisableAsserts(); wx.Log.SetActiveTarget(wx.LogStderr())
    import pcbnew
    b = pcbnew.CreateEmptyBoard()
    b.SetCopperLayerCount(2)
    mm = pcbnew.FromMM
    pts = [(0, 0), (10, 0), (10, 10), (0, 10)]
    for a, c in zip(pts, pts[1:] + pts[:1]):
        s = pcbnew.PCB_SHAPE(b); s.SetShape(pcbnew.SHAPE_T_SEGMENT); s.SetLayer(pcbnew.Edge_Cuts)
        s.SetStart(pcbnew.VECTOR2I(mm(a[0]), mm(a[1]))); s.SetEnd(pcbnew.VECTOR2I(mm(c[0]), mm(c[1]))); s.SetWidth(mm(0.1)); b.Add(s)
    ni = pcbnew.NETINFO_ITEM(b, "__KICAD_EDA_CANARY__"); b.Add(ni)
    t = pcbnew.PCB_TRACK(b); t.SetStart(pcbnew.VECTOR2I(mm(2), mm(5))); t.SetEnd(pcbnew.VECTOR2I(mm(8), mm(5)))
    t.SetWidth(mm(0.2)); t.SetLayer(pcbnew.F_Cu); t.SetNet(ni); b.Add(t)
    pcbnew.SaveBoard("canary.kicad_pcb", b)
"""  # noqa: E501  (the generator script is quoted verbatim)
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

import pytest

import drc_guard as dg
import kicad_env as env

ROOT = Path(__file__).resolve().parents[1]
STALE_VIOLATIONS = 99
K10_RECIPE = "RULES.md K10 (kicad-eda:wave1, KiCad 10.0.5)"


# ---------------------------------------------------------------------------
# A scripted kicad-cli
# ---------------------------------------------------------------------------
def _violation(vtype: str, net: str = dg.CANARY_NET, severity: str = "error",
               description: str = "") -> dict:
    return {"type": vtype, "severity": severity,
            "description": description or f"{vtype} violation",
            "items": [{"description": f"Track [{net}] on F.Cu, length 6.0000 mm",
                       "pos": {"x": 2.0, "y": 5.0}}]}


def _report(*violations: dict, unconnected: int = 0) -> dict:
    return {"violations": list(violations),
            "unconnected_items": [_violation("unconnected_items")] * unconnected}


class FakeCli:
    """A runner(argv, timeout) that plays one kicad-cli run: it records the
    call and whether the report path existed when kicad-cli would start, then
    writes `report` (dict -> JSON, str -> raw text, None -> nothing) and
    returns rc / stdout / stderr, or raises `raises`."""

    def __init__(self, rc: int = 0, report: dict | str | None = None, stdout: str = "",
                 stderr: str = "", raises: BaseException | None = None,
                 inspect: Callable[[list[str]], None] | None = None) -> None:
        self.rc, self.report, self.stdout, self.stderr = rc, report, stdout, stderr
        self.raises, self.inspect = raises, inspect
        self.calls: list[tuple[list[str], float]] = []
        self.report_existed: list[bool] = []

    def __call__(self, argv: list[str], timeout: float) -> subprocess.CompletedProcess:
        self.calls.append((list(argv), timeout))
        rpt = Path(argv[argv.index("-o") + 1])
        self.report_existed.append(rpt.exists())
        if self.inspect:
            self.inspect(argv)
        if self.raises:
            raise self.raises
        if isinstance(self.report, dict):
            rpt.write_text(json.dumps(self.report), encoding="utf-8")
        elif isinstance(self.report, str):
            rpt.write_text(self.report, encoding="utf-8")
        return subprocess.CompletedProcess(argv, self.rc, self.stdout, self.stderr)


@pytest.fixture
def board(tmp_path: Path) -> Path:
    pcb = tmp_path / "b.kicad_pcb"
    pcb.write_text("(kicad_pcb (version 20241229))\n", encoding="utf-8")
    return pcb


def _stale(pcb: Path) -> Path:
    rpt = dg.report_path(pcb)
    rpt.write_text(json.dumps(_report(*[_violation("clearance")] * STALE_VIOLATIONS)),
                   encoding="utf-8")
    return rpt


def _assert_failure(res: dict, rc: int | None, crash: bool) -> None:
    assert set(res) >= {"error", "rc", "crash"}, res
    assert res["rc"] == rc and res["crash"] is crash, res
    assert isinstance(res["error"], str) and res["error"], res
    assert "errors" not in res, res


# ---------------------------------------------------------------------------
# run_drc: success
# ---------------------------------------------------------------------------
def test_rc0_with_a_fresh_report_gives_counts(board: Path) -> None:
    fake = FakeCli(report=_report(_violation("track_width"), _violation("clearance"),
                                  _violation("track_width"), unconnected=2))
    res = dg.run_drc(str(board), "kicad-cli", runner=fake)
    assert res == {"errors": 3, "unconnected": 2,
                   "types": {"track_width": 2, "clearance": 1},
                   "by_severity": {"error": 3}, "report": str(dg.report_path(board)),
                   "kicad_cli": "kicad-cli", "rc": 0, "crash": False}
    (argv, timeout), = fake.calls
    assert argv == ["kicad-cli", "pcb", "drc", "--format", "json", "--severity-error",
                    "-o", str(dg.report_path(board)), str(board)]
    assert timeout == dg.DRC_TIMEOUT_S


def test_report_path_is_the_stem_drc_json_beside_the_board(tmp_path: Path) -> None:
    assert dg.report_path(tmp_path / "x.y.kicad_pcb") == tmp_path / "x.y_drc.json"
    assert dg.report_path(str(tmp_path / "b.kicad_pcb")) == tmp_path / "b_drc.json"


@pytest.mark.parametrize(("severity", "flag"), [("error", "--severity-error"),
                                                ("warning", "--severity-warning"),
                                                ("all", "--severity-all")])
def test_severity_selects_the_kicad_cli_flag(board: Path, severity: str, flag: str) -> None:
    fake = FakeCli(report=_report())
    dg.run_drc(str(board), "cli", severity=severity, timeout=7, runner=fake)
    (argv, timeout), = fake.calls
    assert flag in argv and timeout == 7


def test_an_unknown_severity_is_refused_before_anything_runs(board: Path) -> None:
    fake = FakeCli(report=_report())
    with pytest.raises(ValueError, match="severity"):
        dg.run_drc(str(board), "cli", severity="fatal", runner=fake)
    assert fake.calls == []


# ---------------------------------------------------------------------------
# run_drc: a stale report is never read
# ---------------------------------------------------------------------------
def test_a_stale_report_is_deleted_first_and_never_read(board: Path) -> None:
    rpt = _stale(board)
    fake = FakeCli(rc=0, report=None, stdout="Found 0 violations")
    res = dg.run_drc(str(board), "cli", runner=fake)
    _assert_failure(res, 0, False)
    assert fake.report_existed == [False]          # gone before kicad-cli started
    assert not rpt.exists()
    assert "no DRC report" in res["error"]


def test_a_stale_report_is_replaced_by_the_fresh_one(board: Path) -> None:
    _stale(board)
    fake = FakeCli(report=_report(_violation("track_width")))
    res = dg.run_drc(str(board), "cli", runner=fake)
    assert fake.report_existed == [False]
    assert res["errors"] == 1 and res["types"] == {"track_width": 1}


def test_a_report_that_cannot_be_deleted_stops_the_run(board: Path) -> None:
    dg.report_path(board).mkdir()                  # unlink() of a directory raises
    fake = FakeCli(report=_report())
    res = dg.run_drc(str(board), "cli", runner=fake)
    _assert_failure(res, None, False)
    assert fake.calls == [] and "stale" in res["error"]


# ---------------------------------------------------------------------------
# run_drc: failures
# ---------------------------------------------------------------------------
K1099_BROKEN = ("DRC incomplete: could not compile custom design rules.\n"
                "ERROR: Unrecognized layer 'Bogus.Cu'. in 'b.kicad_dru', line 5, offset 19.\n")


def test_rc3_without_a_report_is_an_error_naming_the_cli_output(board: Path) -> None:
    res = dg.run_drc(str(board), "cli", runner=FakeCli(rc=3, stdout=K1099_BROKEN))
    _assert_failure(res, 3, False)
    assert "exited 3" in res["error"] and "Bogus.Cu" in res["error"]
    assert "could not compile custom design rules" in res["output"]
    assert ".kicad_dru" in res["error"]            # the hint names the rules file


def test_nonzero_rc_is_an_error_even_with_a_report(board: Path) -> None:
    fake = FakeCli(rc=2, report=_report(), stderr="something failed")
    res = dg.run_drc(str(board), "cli", runner=fake)
    _assert_failure(res, 2, False)
    assert res["report"] == str(dg.report_path(board))


@pytest.mark.parametrize("rc", [3221226356, -1073740940, -11, -6, 256, 0xC0000005])
def test_a_crash_is_flagged(board: Path, rc: int) -> None:
    res = dg.run_drc(str(board), "cli", runner=FakeCli(rc=rc))
    _assert_failure(res, rc, True)
    assert "crashed" in res["error"]


def test_the_9_0_4_heap_corruption_is_named(board: Path) -> None:
    res = dg.run_drc(str(board), "cli", runner=FakeCli(rc=3221226356))
    assert "0xC0000374" in res["error"]


@pytest.mark.parametrize("rc", [1, 2, 3, 5, 255])
def test_an_ordinary_exit_code_is_not_a_crash(rc: int) -> None:
    assert dg.is_crash(rc) is False


@pytest.mark.parametrize("rc", [3221226356, -1073740940, -1, 256, 0xC0000409])
def test_is_crash(rc: int) -> None:
    assert dg.is_crash(rc) is True


def test_is_crash_of_zero_and_none() -> None:
    assert dg.is_crash(0) is False and dg.is_crash(None) is False


def test_a_missing_report_after_rc0_is_an_error(board: Path) -> None:
    res = dg.run_drc(str(board), "cli", runner=FakeCli(rc=0, report=None))
    _assert_failure(res, 0, False)


@pytest.mark.parametrize("text", ["{not json", "", "[1, 2]", '{"violations": 3}',
                                  '{"violations": [], "unconnected_items": {}}',
                                  '{"violations": ["x"]}'])
def test_an_unusable_report_is_an_error(board: Path, text: str) -> None:
    res = dg.run_drc(str(board), "cli", runner=FakeCli(rc=0, report=text))
    _assert_failure(res, 0, False)
    assert "report" in res["error"]


@pytest.mark.parametrize("line", ["Error: Cannot load board", "10:30:04: Error: bad file",
                                  "Found 2 violations\n12:00:01 Error: wx says no"])
def test_an_error_line_fails_a_rc0_run(board: Path, line: str) -> None:
    res = dg.run_drc(str(board), "cli", runner=FakeCli(rc=0, report=_report(), stderr=line))
    _assert_failure(res, 0, False)
    assert "Error:" in res["error"]


@pytest.mark.parametrize("text", ["Found 2 violations", "RuntimeError:x", "no Error here"])
def test_text_that_is_not_an_error_line_passes(board: Path, text: str) -> None:
    res = dg.run_drc(str(board), "cli", runner=FakeCli(rc=0, report=_report(), stdout=text))
    assert res["errors"] == 0 and "error" not in res


def test_a_timeout_is_an_error(board: Path) -> None:
    boom = subprocess.TimeoutExpired(["cli"], 5, output="partial", stderr=None)
    res = dg.run_drc(str(board), "cli", timeout=5, runner=FakeCli(raises=boom))
    _assert_failure(res, None, False)
    assert res["timeout"] is True and "5" in res["error"]


def test_a_launch_failure_is_an_error(board: Path) -> None:
    res = dg.run_drc(str(board), "C:/nope/kicad-cli.exe",
                     runner=FakeCli(raises=FileNotFoundError("no such file")))
    _assert_failure(res, None, False)
    assert "no such file" in res["error"]


@pytest.mark.parametrize("cli", ["", None])
def test_no_cli_is_an_error_without_running(board: Path, cli: str | None) -> None:
    fake = FakeCli(report=_report())
    res = dg.run_drc(str(board), cli, runner=fake)
    _assert_failure(res, None, False)
    assert fake.calls == [] and "kicad-cli" in res["error"]


def test_a_missing_board_is_an_error_without_running(tmp_path: Path) -> None:
    fake = FakeCli(report=_report())
    res = dg.run_drc(str(tmp_path / "gone.kicad_pcb"), "cli", runner=fake)
    _assert_failure(res, None, False)
    assert fake.calls == [] and "gone.kicad_pcb" in res["error"]


def test_the_output_tail_is_bounded(board: Path) -> None:
    res = dg.run_drc(str(board), "cli", runner=FakeCli(rc=4, stdout="x" * 5000 + "TAIL"))
    assert len(res["output"]) <= dg.OUTPUT_TAIL and res["output"].endswith("TAIL")


# ---------------------------------------------------------------------------
# The default runner: stdin closed, UTF-8 with replacement, a timeout
# ---------------------------------------------------------------------------
def test_default_runner_passes_the_child_rules(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def fake_run(argv: list[str], **kw: Any) -> subprocess.CompletedProcess:
        seen.update(kw, argv=argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(dg.subprocess, "run", fake_run)
    dg.default_runner(["cli", "x"], 12.5)
    assert seen["stdin"] is subprocess.DEVNULL and seen["timeout"] == 12.5
    assert seen["encoding"] == "utf-8" and seen["errors"] == "replace"
    assert seen["capture_output"] is True and seen["text"] is True


def test_default_runner_really_closes_stdin_and_decodes_utf8() -> None:
    code = ("import sys; sys.stdout.buffer.write(repr(sys.stdin.read()).encode() + "
            "b' \\xc3\\xa9 \\xff')")
    proc = dg.default_runner([sys.executable, "-c", code], 30)
    assert proc.returncode == 0 and proc.stdout == "'' \u00e9 \ufffd"


# ---------------------------------------------------------------------------
# The canary
# ---------------------------------------------------------------------------
def test_the_canary_rule_is_the_adr_text() -> None:
    assert dg.CANARY_RULE == ('(rule "__kicad_eda_canary" (condition "A.NetName == '
                              "'__KICAD_EDA_CANARY__'\") (constraint track_width (min 10mm)))")
    assert dg.CANARY_NET == "__KICAD_EDA_CANARY__"


def test_the_shipped_canary_board_and_project() -> None:
    assert dg.CANARY_BOARD.parent == ROOT / "server" / "data"
    text = dg.CANARY_BOARD.read_text(encoding="utf-8")
    assert "(version 20241229)" in text and '(generator_version "9.0")' in text
    assert re.findall(r"\(net (\d+) \"([^\"]*)\"\)", text) == [("0", ""), ("1", dg.CANARY_NET)]
    segments = re.findall(r"\(segment\b.*?\(width ([\d.]+)\)\s*\(layer \"([^\"]+)\"\)\s*"
                          r"\(net (\d+)\)", text, re.S)
    assert segments == [("0.2", "F.Cu", "1")]
    assert text.count('(layer "Edge.Cuts")') == 4      # the 10 x 10 mm outline
    assert "(zone" not in text and "(footprint" not in text and "(via" not in text
    project = json.loads(dg.CANARY_PROJECT.read_text(encoding="utf-8"))
    assert project["board"]["design_settings"]["rules"]["min_track_width"] == 0.0
    assert project["meta"]["filename"] == dg.CANARY_PROJECT.name


def test_the_canary_files_are_not_git_ignored() -> None:
    git = shutil.which("git")
    if not git:
        pytest.skip("git not installed")
    paths = [str(p.relative_to(ROOT)) for p in (dg.CANARY_BOARD, dg.CANARY_PROJECT)]
    try:
        proc = subprocess.run([git, "-C", str(ROOT), "check-ignore", *paths],
                              capture_output=True, text=True, stdin=subprocess.DEVNULL,
                              timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        pytest.skip(f"git check-ignore did not run: {exc}")
    if proc.returncode not in (0, 1):
        pytest.skip(f"not a git work tree here: {proc.stderr.strip()}")
    assert proc.stdout == "", proc.stdout


@pytest.mark.parametrize(("text", "expected", "added"), [
    ("(version 1)\n", f"(version 1)\n{dg.CANARY_RULE}\n", True),
    ("(version 1)\n(rule a)", f"(version 1)\n(rule a)\n{dg.CANARY_RULE}\n", True),
    (f"(version 1)\n{dg.CANARY_RULE}\n(rule a)\n", f"(version 1)\n{dg.CANARY_RULE}\n(rule a)\n",
     False),
    ("", f"(version 1)\n{dg.CANARY_RULE}\n", True),
    ("  \n# comment only\n", f"(version 1)\n{dg.CANARY_RULE}\n", True),
])
def test_with_canary(text: str, expected: str, added: bool) -> None:
    assert dg.with_canary(text) == (expected, added)


def _canary_ok_report() -> dict:
    """A 9.0.4-style report: the rule name is missing from the description."""
    return _report(_violation("track_dangling", severity="warning"), _violation(
        "track_width", description="Track width (board setup constraints min width 10.0000 mm; "
                                   "actual 0.2000 mm)"))


def test_rules_compile_runs_on_a_temp_copy_of_the_canary(tmp_path: Path) -> None:
    before = (dg.CANARY_BOARD.read_bytes(), dg.CANARY_PROJECT.read_bytes())
    seen: dict[str, Any] = {}

    def inspect(argv: list[str]) -> None:
        pcb = Path(argv[-1])
        seen.update(dir=pcb.parent, argv=argv, board=pcb.read_bytes(),
                    project=pcb.with_suffix(".kicad_pro").read_bytes(),
                    dru=pcb.with_suffix(".kicad_dru").read_text(encoding="utf-8"))

    text = ('(version 1)\n'
            '(rule "w" (condition "A.NetName == \'X\'") (constraint track_width (min 1mm)))')
    fake = FakeCli(report=_canary_ok_report(), inspect=inspect)
    res = dg.rules_compile(text, "cli", runner=fake)
    assert res["ok"] is True and res["fired"] == 1 and res["rc"] == 0, res
    assert res["crash"] is False and res["canary_added"] is True and "error" not in res
    assert seen["dir"] != dg.DATA_DIR and Path(seen["argv"][-1]).name == dg.CANARY_BOARD.name
    assert "--severity-all" in seen["argv"]
    assert (seen["board"], seen["project"]) == before
    assert seen["dru"] == dg.with_canary(text)[0]
    assert not seen["dir"].exists()                # the temp dir is cleaned up
    assert (dg.CANARY_BOARD.read_bytes(), dg.CANARY_PROJECT.read_bytes()) == before
    assert fake.calls[0][1] == dg.CANARY_TIMEOUT_S


def test_rules_compile_with_the_canary_already_present_adds_nothing() -> None:
    text = f"(version 1)\n{dg.CANARY_RULE}\n"
    dru: list[str] = []
    fake = FakeCli(report=_canary_ok_report(), inspect=lambda argv: dru.append(
        Path(argv[-1]).with_suffix(".kicad_dru").read_text(encoding="utf-8")))
    res = dg.rules_compile(text, "cli", runner=fake)
    assert res["ok"] is True and res["canary_added"] is False and dru == [text]


def test_a_silently_dropped_file_is_not_ok() -> None:
    fake = FakeCli(report=_report(_violation("track_dangling", severity="warning")))
    res = dg.rules_compile("(version 1)\n(rule broken", "cli", runner=fake)
    assert res["ok"] is False and res["rc"] == 0 and res["fired"] == 0, res
    assert res["crash"] is False and "silently" in res["reason"]


def test_a_track_width_hit_off_the_canary_net_does_not_count() -> None:
    fake = FakeCli(report=_report(_violation("track_width", net="GND")))
    res = dg.rules_compile("(version 1)\n", "cli", runner=fake)
    assert res["ok"] is False and res["fired"] == 0


def test_the_rule_name_alone_never_counts() -> None:
    named = _violation("clearance", description="Clearance (rule '__kicad_eda_canary' ...)")
    res = dg.rules_compile("(version 1)\n", "cli", runner=FakeCli(report=_report(named)))
    assert res["ok"] is False and res["fired"] == 0


@pytest.mark.parametrize("items", [None, {}, "Track [__KICAD_EDA_CANARY__]", ["x", None]])
def test_canary_hits_ignores_malformed_items(items: Any) -> None:
    good = _violation("track_width")
    assert dg.canary_hits(_report({"type": "track_width", "items": items}, good)) == 1


def test_rules_compile_reports_10_99_rc3_as_an_error() -> None:
    fake = FakeCli(rc=3, stdout=K1099_BROKEN)
    res = dg.rules_compile("(version 1)\n(rule x)", "cli", runner=fake)
    assert res["ok"] is False and res["rc"] == 3 and res["crash"] is False
    assert res["fired"] == 0 and "exited 3" in res["error"] and res["reason"] == res["error"]


def test_rules_compile_reports_a_crash() -> None:
    res = dg.rules_compile("(version 1)\n(rule x)", "cli", runner=FakeCli(rc=3221226356))
    assert res["ok"] is False and res["crash"] is True and res["rc"] == 3221226356


def test_rules_compile_without_a_cli() -> None:
    res = dg.rules_compile("(version 1)\n", "", runner=FakeCli(report=_canary_ok_report()))
    assert res["ok"] is False and res["rc"] is None and "kicad-cli" in res["error"]


def test_rules_compile_without_the_shipped_canary(monkeypatch: pytest.MonkeyPatch,
                                                  tmp_path: Path) -> None:
    monkeypatch.setattr(dg, "CANARY_BOARD", tmp_path / "drc_canary.kicad_pcb")
    fake = FakeCli(report=_canary_ok_report())
    res = dg.rules_compile("(version 1)\n", "cli", runner=fake)
    assert res["ok"] is False and res["rc"] is None and res["crash"] is False
    assert fake.calls == [] and "canary board" in res["reason"]


def test_rules_compile_reports_the_violation_types() -> None:
    res = dg.rules_compile("(version 1)\n", "cli", runner=FakeCli(report=_canary_ok_report()))
    assert res["types"] == {"track_dangling": 1, "track_width": 1}


# ---------------------------------------------------------------------------
# Live tier: every installed release
# ---------------------------------------------------------------------------
RELEASES = {"9.0": (9, 0), "10.0": (10, 0), "10.99": (10, 99)}
VALID = "\n".join([
    "(version 1)", dg.CANARY_RULE,
    "(rule \"w_in3\" (layer \"In3.Cu\") (condition \"A.NetName == 'X'\") "
    "(constraint track_width (min 0.3mm) (opt 0.31mm) (max 0.32mm)))",
    "(rule \"ang\" (condition \"A.NetName == 'X'\") (constraint track_angle (min 135)))",
    "(rule \"ar\" (condition \"A.NetName != 'GND' && A.intersectsArea('RFCOR_X')\") "
    "(constraint disallow track via))", ""])
BROKEN = {
    "deg": f"(version 1)\n{dg.CANARY_RULE}\n(rule \"bad\" (condition \"A.NetName == 'X'\") "
           "(constraint track_angle (min 135deg)))\n",
    "bogus_layer": f"(version 1)\n{dg.CANARY_RULE}\n(rule \"w\" (layer \"Bogus.Cu\") "
                   "(condition \"A.NetName == 'X'\") (constraint track_width (min 0.3mm)))\n",
}
BARE_AREA = (f"(version 1)\n{dg.CANARY_RULE}\n(rule \"a2\" (condition "
             "\"intersectsArea('RFCOR_X')\") (constraint disallow track via))\n")


@pytest.fixture(scope="module")
def installs() -> list[env.KiCadInstall]:
    return env.discover()


def _cli(installs: list[env.KiCadInstall], release: str) -> str:
    for inst in installs:
        if inst.version_tuple[:2] == RELEASES[release]:
            return str(inst.kicad_cli)
    where = K10_RECIPE if release == "10.0" else "this machine"
    pytest.skip(f"no KiCad {release} kicad-cli installed (run on {where})")


@pytest.mark.parametrize("release", list(RELEASES))
def test_live_valid_rules_compile(installs: list[env.KiCadInstall], release: str) -> None:
    res = dg.rules_compile(VALID, _cli(installs, release))
    assert res["ok"] is True and res["rc"] == 0 and res["fired"] >= 1, res
    assert res["crash"] is False and res["canary_added"] is False


@pytest.mark.parametrize("release", list(RELEASES))
def test_live_text_without_the_canary_gets_it_added(installs: list[env.KiCadInstall],
                                                    release: str) -> None:
    res = dg.rules_compile("(version 1)\n", _cli(installs, release))
    assert res["ok"] is True and res["canary_added"] is True and res["fired"] >= 1, res


@pytest.mark.parametrize("case", list(BROKEN))
@pytest.mark.parametrize("release", list(RELEASES))
def test_live_broken_rules_are_caught(installs: list[env.KiCadInstall], release: str,
                                      case: str) -> None:
    res = dg.rules_compile(BROKEN[case], _cli(installs, release))
    assert res["ok"] is False and res["fired"] == 0 and res["crash"] is False, res
    if release == "10.99":
        assert res["rc"] == 3 and "error" in res, res
    else:                                          # dropped silently, rc 0 (9.0.4, 10.0.5)
        assert res["rc"] == 0 and "error" not in res and "silently" in res["reason"], res


@pytest.mark.parametrize("release", list(RELEASES))
def test_live_bare_intersects_area(installs: list[env.KiCadInstall], release: str) -> None:
    res = dg.rules_compile(BARE_AREA, _cli(installs, release))
    assert res["ok"] is False and res["fired"] == 0, res
    expected = {"9.0": (True, None), "10.0": (False, 0), "10.99": (False, 3)}[release]
    assert res["crash"] is expected[0], res
    if expected[1] is not None:
        assert res["rc"] == expected[1], res


@pytest.mark.parametrize("release", list(RELEASES))
def test_live_run_drc_never_reads_the_stale_report(installs: list[env.KiCadInstall],
                                                   release: str, tmp_path: Path) -> None:
    cli = _cli(installs, release)
    pcb = tmp_path / dg.CANARY_BOARD.name
    shutil.copy(dg.CANARY_BOARD, pcb)
    shutil.copy(dg.CANARY_PROJECT, pcb.with_suffix(".kicad_pro"))
    pcb.with_suffix(".kicad_dru").write_text(VALID, encoding="utf-8")
    _stale(pcb)
    res = dg.run_drc(str(pcb), cli)
    assert res["rc"] == 0 and res["crash"] is False and "error" not in res, res
    assert res["errors"] == 1 and res["types"] == {"track_width": 1}, res
    assert res["unconnected"] == 0
