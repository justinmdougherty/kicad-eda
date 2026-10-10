"""pipeline/run_all.py (F5-16): run_all.json markers (validation, discovery, order),
expect-FAIL rows, post_checks, FAIL-row gate summaries, the decisions column, the G3/G4
fixtures and the ALL PASS verdict; every pipeline run is a scripted stand-in (no KiCad)."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

import pytest

import decisions
import gates as gates_mod
import kicad_server
import rp_kicad
import run_all
import run_pipeline as rp
import toolchain

STATIC_NAMES = ["cp2102n_uart", "stm32f103_dev", "lm358_buffer", "usb_afe", "lm324_quad",
                "buck_5v3a", "rf_coupon_50r", "rf_lna_gainblock", "esp32c3_env_node",
                "negative_gate_test"]
CP2102N = Path(run_all.__file__).resolve().parents[1] / "examples" / "cp2102n_uart" / "netlist.json"


# --- scripted reports -------------------------------------------------------------------
def gate(name: str, passed: bool = True, blockers: tuple = (), warnings: tuple = ()) -> dict:
    return {"gate": name, "name": f"{name} review", "feeds": "next", "passed": passed,
            "blockers": list(blockers), "warnings": list(warnings), "tasks": []}


def passing() -> list[dict]:
    return [gate(g) for g in ("G1", "G2", "G3", "G4", "G5")]


def report(netlist: str, gate_list: list[dict], **extra: Any) -> dict:
    stem = str(Path(netlist).with_suffix(""))
    rep = {"PASS": bool(gate_list) and all(g["passed"] for g in gate_list),
           "gates": gate_list, "gate_summary": gates_mod.summarize(gate_list),
           "pipeline_report": stem + "_pipeline_report.json",
           "schematic": {"erc_violations": 0}, "drc": {"errors": 0, "unconnected": 0},
           "autoroute": {"pack_scale": 1.0}, "remediation": []}
    return {**rep, **extra}


def failing_at(netlist: str, at: str, blocker: str) -> dict:
    order = ("G1", "G2", "G3", "G4", "G5")
    gate_list = [gate(g) for g in order[:order.index(at)]] + [gate(at, False, (blocker,))]
    return report(netlist, gate_list)


class FakeRun:
    """run_pipeline.run stand-in: `script[name](netlist)` gives the report of
    the board in folder `name` (default: a full PASS); every call recorded."""

    def __init__(self, script: dict[str, Callable[[str], dict]] | None = None) -> None:
        self.script = dict(script or {})
        self.calls: list[tuple[str, float, float, tuple, dict]] = []

    def __call__(self, netlist: str, w: float, h: float, *args: Any, **kw: Any) -> dict:
        name = Path(netlist).parent.name
        self.calls.append((name, w, h, args, kw))
        make = self.script.get(name, lambda n: report(n, passing()))
        return make(netlist)

    def names(self) -> list[str]:
        return [c[0] for c in self.calls]


@pytest.fixture
def fake_run(monkeypatch: pytest.MonkeyPatch) -> FakeRun:
    fake = FakeRun()
    monkeypatch.setattr(rp, "run", fake)
    return fake


@pytest.fixture
def no_kicad(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(kicad_server, "_kicad_python", lambda: None)


def board_dir(root: Path, name: str, design: dict | None = None,
              marker: Any = None) -> Path:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "netlist.json").write_text(json.dumps(design or {"components": [], "nets": {}}),
                                    encoding="utf-8")
    if marker is not None:
        text = marker if isinstance(marker, str) else json.dumps(marker)
        (d / run_all.MARKER).write_text(text, encoding="utf-8")
    return d


def run_main(argv: list[str], capsys: pytest.CaptureFixture) -> tuple[int, str, dict]:
    code = run_all.main(argv)
    out = capsys.readouterr().out
    last = out.strip().splitlines()[-1]
    return code, out, (json.loads(last) if last.startswith("{") else {})


def result(summary: dict, board: str) -> dict:
    return next(r for r in summary["results"] if r["board"] == board)


# --- the module ---------------------------------------------------------------------
def test_module_defaults():
    """Importing never reads the boards dir from argv; BOARDS unchanged; CI routes
    with Freerouting."""
    assert run_all.HERE == run_all.DEFAULT_BOARDS == run_all.PLUGIN / "examples"
    assert [b[0] for b in run_all.BOARDS] == STATIC_NAMES
    assert run_all.ROUTE == "freerouting"


# --- marker parsing -------------------------------------------------------------------
def test_valid_markers_parse():
    assert run_all.marker_problems({"w": 40, "h": 25.5}) == run_all.marker_problems({}) == []
    assert run_all.to_marker({"w": 40, "h": 25.5}) == run_all.Marker(
        w=40.0, h=25.5, expect="PASS", gate=None, blocker_contains=None, args=(),
        post_checks=())
    assert run_all.to_marker({}).w is None
    data = {"expect": "FAIL", "gate": "G3", "blocker_contains": "courtyard_outside"}
    assert run_all.marker_problems(data) == []
    m = run_all.to_marker(data)
    assert (m.expect, m.gate, m.blocker_contains) == ("FAIL", "G3", "courtyard_outside")


def test_post_check_defaults(tmp_path, monkeypatch):
    _plugin_with_script(tmp_path, monkeypatch, "e2e_x_check.py")
    data = {"post_checks": [{"script": "pipeline/e2e_x_check.py"}]}
    assert run_all.marker_problems(data) == []
    assert run_all.to_marker(data).post_checks == (
        run_all.PostCheck("pipeline/e2e_x_check.py", "venv", ()),)


def _plugin_with_script(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str) -> Path:
    plugin = tmp_path / "plugin"
    (plugin / "pipeline").mkdir(parents=True)
    (plugin / "pipeline" / name).write_text("print('ok')\n", encoding="utf-8")
    monkeypatch.setattr(run_all, "PLUGIN", plugin)
    return plugin


@pytest.mark.parametrize("data, problem", [
    ([], "must be a JSON object"),
    ({"w": 40, "h": 25, "colour": "red"}, "unknown field 'colour'"),
    ({"w": 40}, "give both w and h"),
    ({"w": 0, "h": 25}, "w must be a positive number"),
    ({"w": True, "h": 25}, "w must be a positive number"),
    ({"w": "40", "h": 25}, "w must be a positive number"),
    ({"w": 40, "h": float("inf")}, "h must be a positive number"),
    ({"expect": "MAYBE"}, "expect must be PASS or FAIL"),
    ({"expect": "FAIL", "blocker_contains": "x"}, "expect FAIL needs gate"),
    ({"expect": "FAIL", "gate": "G3"}, "expect FAIL needs blocker_contains"),
    ({"expect": "FAIL", "gate": "G9", "blocker_contains": "x"}, "gate must be one of G1"),
    ({"expect": "FAIL", "gate": "G3", "blocker_contains": " "}, "blocker_contains must be"),
    ({"gate": "G3"}, "gate only with expect FAIL"),
    ({"blocker_contains": "x"}, "blocker_contains only with expect FAIL"),
    ({"args": "--strict"}, "args must be a list of strings"),
    ({"args": [1]}, "args must be a list of strings"),
    ({"args": ["--fast"]}, "unknown option --fast"),
    ({"args": ["--route", "teleport"]}, "--route must be one of"),
    ({"args": ["30", "40"]}, "the board size goes in w/h"),
    ({"args": ["--resume", "routed"]}, "--resume continues a handed-off run"),
    ({"post_checks": {}}, "post_checks must be a list"),
    ({"post_checks": ["pipeline/x.py"]}, "post_checks[0] must be an object"),
    ({"post_checks": [{"python": "venv"}]}, "post_checks[0]: script must be"),
    ({"post_checks": [{"script": "server/tools/gates.py"}]}, "directly in pipeline/"),
    ({"post_checks": [{"script": "pipeline/../server/x.py"}]}, "directly in pipeline/"),
    ({"post_checks": [{"script": "pipeline/absent_check.py"}]}, "not found"),
    ({"post_checks": [{"script": "pipeline/run_all.py", "python": "py2"}]},
     "python must be venv or kicad"),
    ({"post_checks": [{"script": "pipeline/run_all.py", "args": "{dir}"}]},
     "args must be a list of strings"),
    ({"post_checks": [{"script": "pipeline/run_all.py", "args": ["{board}"]}]},
     "unknown placeholder {board}"),
    ({"post_checks": [{"script": "pipeline/run_all.py", "args": ["{pcb"]}]},
     "post_checks[0]: arg '{pcb'"),
    ({"post_checks": [{"script": "pipeline/run_all.py", "timeout": 5}]},
     "post_checks[0]: unknown field 'timeout'"),
])
def test_bad_markers(data, problem):
    problems = run_all.marker_problems(data)
    assert any(problem in p for p in problems), problems


def test_every_problem_is_reported_at_once():
    problems = run_all.marker_problems({"w": -1, "h": 2, "expect": "FAIL", "extra": 1})
    assert len(problems) >= 4, problems


def test_known_placeholders_are_accepted():
    data = {"post_checks": [{"script": "pipeline/run_all.py", "python": "kicad",
                             "args": ["{pcb}", "{netlist}", "{dir}", "{report}", "--x={dir}/y"]}]}
    assert run_all.marker_problems(data) == []


# --- discovery -------------------------------------------------------------------------
def test_discovery_sorted_by_name_markers_only(tmp_path):
    board_dir(tmp_path, "zeta", marker={"w": 10, "h": 10})
    board_dir(tmp_path, "alpha", marker={"w": 20, "h": 20})
    board_dir(tmp_path, "no_marker")
    (tmp_path / "loose.json").write_text("{}", encoding="utf-8")
    found = run_all.discover(tmp_path)
    assert [e.name for e in found] == ["alpha", "zeta"]
    assert all(not e.problems for e in found)
    assert found[0].marker.w == 20.0


def test_discovery_bad_markers_are_entries_with_problems(tmp_path):
    board_dir(tmp_path, "broken_json", marker="{not json")
    (tmp_path / "no_netlist").mkdir()
    (tmp_path / "no_netlist" / run_all.MARKER).write_text("{}", encoding="utf-8")
    board_dir(tmp_path, "lm358_buffer", marker={})
    board_dir(tmp_path, "napkin_g3", marker={})
    found = {e.name: e for e in run_all.discover(tmp_path)}
    assert "not JSON" in " ".join(found["broken_json"].problems)
    assert "no netlist.json" in " ".join(found["no_netlist"].problems)
    assert "static BOARDS row" in " ".join(found["lm358_buffer"].problems)
    assert "fixture" in " ".join(found["napkin_g3"].problems)
    assert all(found[n].marker is None for n in found)


# --- the matrix --------------------------------------------------------------------------
def test_matrix_order_static_then_markers_then_fixtures(tmp_path, fake_run, no_kicad, capsys):
    board_dir(tmp_path, "lm358_buffer")
    board_dir(tmp_path, "zz_marker", marker={"w": 10, "h": 10})
    board_dir(tmp_path, "aa_marker", marker={"w": 10, "h": 10})
    code, out, summary = run_main([str(tmp_path)], capsys)
    names = [r["board"] for r in summary["results"]]
    assert names == STATIC_NAMES + ["aa_marker", "zz_marker", "negative_g3", "napkin_g3",
                                    "negative_g4_heal"]
    assert fake_run.names() == ["lm358_buffer", "aa_marker", "zz_marker"]
    assert code == 0 and summary["all_pass"] is True
    assert "ALL PASS: True" in out


def test_absent_static_boards_skip_in_a_custom_dir(tmp_path, fake_run, no_kicad, capsys):
    code, out, summary = run_main([str(tmp_path)], capsys)
    assert fake_run.calls == []
    assert {r["status"] for r in summary["results"]} == {"SKIP"}
    assert code == 1 and summary["all_pass"] is False and "nothing ran" in out
    assert "not in" in result(summary, "usb_afe")["detail"]
    assert "cp2102n_uart" in result(summary, "napkin_g3")["detail"]
    assert set(summary["skipped"]) == set(STATIC_NAMES) | set(run_all.FIXTURES)


def test_absent_static_board_is_missing_in_the_default_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(run_all, "DEFAULT_BOARDS", tmp_path)
    assert run_all.static_row(tmp_path, "usb_afe", 40, 40).status == "MISSING"


def test_static_boards_route_with_freerouting(tmp_path, fake_run, no_kicad, capsys):
    board_dir(tmp_path, "usb_afe")
    run_main([str(tmp_path)], capsys)
    assert fake_run.calls == [("usb_afe", 40, 40, (), {"route": "freerouting"})]


def test_a_bad_directory_is_a_usage_error(tmp_path, capsys):
    assert run_all.main([str(tmp_path / "absent")]) == 2
    assert run_all.main(["--fast"]) == 2
    assert run_all.main([str(tmp_path), str(tmp_path)]) == 2
    assert "usage" in capsys.readouterr().err


# --- the dry listing -----------------------------------------------------------------------
def test_list_prints_the_plan_without_running(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(rp, "run", lambda *a, **k: pytest.fail("--list must not run boards"))
    board_dir(tmp_path, "lm358_buffer")
    board_dir(tmp_path, "my_board", marker={"w": 12, "h": 34, "post_checks": [
        {"script": "pipeline/run_all.py", "args": ["{dir}"]}]})
    board_dir(tmp_path, "my_fail", marker={"expect": "FAIL", "gate": "G1",
                                           "blocker_contains": "lint"})
    assert run_all.main(["--list", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "my_board" in out and "12 x 34" in out and "1 post_check" in out
    assert "expect FAIL at G1" in out and "'lint'" in out
    lines = out.splitlines()
    assert [ln for ln in lines if "lm358_buffer" in ln][0].split()[0] == "board"
    assert any("usb_afe" in ln and "absent" in ln for ln in lines)
    for fixture in run_all.FIXTURES:
        assert any(fixture in ln for ln in lines)


def test_list_exits_1_and_names_a_bad_marker(tmp_path, capsys):
    board_dir(tmp_path, "bad_one", marker={"w": -3, "h": 1})
    assert run_all.main([str(tmp_path), "--list"]) == 1
    out = capsys.readouterr().out
    assert "bad_one" in out and "BAD run_all.json" in out and "w must be a positive" in out


# --- marker rows ---------------------------------------------------------------------------
def test_bad_marker_is_a_fail_row_naming_it(tmp_path, fake_run, no_kicad, capsys):
    board_dir(tmp_path, "bad_one", marker={"w": 10, "h": 10, "expect": "SOMETIMES"})
    code, out, summary = run_main([str(tmp_path)], capsys)
    row = result(summary, "bad_one")
    assert code == 1 and row["status"] == "FAIL"
    assert "bad run_all.json" in row["detail"] and "expect must be PASS or FAIL" in row["detail"]
    assert fake_run.calls == []


def test_marker_runs_with_its_dims_and_freerouting(tmp_path, fake_run):
    board_dir(tmp_path, "m", marker={"w": 12.5, "h": 7})
    row = run_all.marker_row(tmp_path, run_all.discover(tmp_path)[0])
    assert row.status == "PASS"
    assert fake_run.calls == [("m", 12.5, 7.0, (), {"napkin": True, "route": "freerouting"})]


@pytest.mark.parametrize("args, kw", [
    (["--strict"], {"napkin": False, "route": "freerouting"}),
    (["--route", "agent"], {"napkin": True, "route": "agent"}),
    (["--route=auto", "--strict"], {"napkin": False, "route": "auto"}),
])
def test_marker_args_reach_run(tmp_path, fake_run, args, kw):
    board_dir(tmp_path, "m", marker={"w": 10, "h": 10, "args": args})
    run_all.marker_row(tmp_path, run_all.discover(tmp_path)[0])
    assert fake_run.calls[0][4] == kw


def test_marker_placement_resolves_against_its_folder(tmp_path, fake_run):
    d = board_dir(tmp_path, "m", marker={"w": 10, "h": 10, "args": ["place.json"]})
    run_all.marker_row(tmp_path, run_all.discover(tmp_path)[0])
    assert fake_run.calls[0][4]["placement"] == str(d / "place.json")


def test_marker_without_dims_takes_the_netlist_board_block(tmp_path, fake_run):
    board_dir(tmp_path, "m", design={"components": [], "nets": {},
                                     "board": {"w": 61, "h": 42}}, marker={})
    run_all.marker_row(tmp_path, run_all.discover(tmp_path)[0])
    assert fake_run.calls[0][1:3] == (61.0, 42.0)


def test_marker_whose_dims_cannot_be_decided_is_a_fail_row(tmp_path, fake_run):
    board_dir(tmp_path, "m", design={"components": [], "nets": {}, "board": {"w": -1}},
              marker={})
    row = run_all.marker_row(tmp_path, run_all.discover(tmp_path)[0])
    assert row.status == "FAIL" and "board block" in row.info["detail"]
    assert fake_run.calls == []


def test_a_netlist_the_dims_rule_chokes_on_is_a_fail_row(tmp_path, fake_run, no_kicad, capsys):
    """No w/h, a malformed netlist: the dims rule's non-usage error is one FAIL row."""
    board_dir(tmp_path, "m", design={"components": ["U1"], "nets": {}}, marker={})
    board_dir(tmp_path, "n", marker={"w": 10, "h": 10})
    code, out, summary = run_main([str(tmp_path)], capsys)
    row = result(summary, "m")
    assert row["status"] == "FAIL" and "AttributeError" in row["detail"]
    assert "cannot size the board" in row["detail"]
    assert result(summary, "n")["status"] == "PASS" and fake_run.names() == ["n"]
    assert code == 1 and "ALL PASS: False" in out


EXPECT_G3 = {"w": 10, "h": 10, "expect": "FAIL", "gate": "G3",
             "blocker_contains": "courtyard_outside"}


@pytest.mark.parametrize("make, status, detail", [
    (lambda n: failing_at(n, "G3", "U1 courtyard_outside the outline"), "PASS",
     "failed at G3 as expected"),
    (lambda n: failing_at(n, "G4", "U1 courtyard_outside the outline"), "FAIL",
     "expected FAIL at G3, failed at G4"),
    (lambda n: report(n, passing()), "FAIL", "expected FAIL at G3, but the run passed"),
    (lambda n: failing_at(n, "G3", "J1 mating face points inward"), "FAIL",
     "without a blocker containing 'courtyard_outside'"),
], ids=["caught", "wrong-gate", "passed", "wrong-blocker"])
def test_expect_fail_rows(tmp_path, fake_run, make, status, detail):
    board_dir(tmp_path, "neg", marker=EXPECT_G3)
    fake_run.script["neg"] = make
    row = run_all.marker_row(tmp_path, run_all.discover(tmp_path)[0])
    assert row.status == status and detail in row.info["detail"]
    assert row.info["gate"] == ("G3*" if status == "PASS" else row.info["gate"])
    assert bool(row.summary) is (status == "FAIL")


# --- post_checks -----------------------------------------------------------------------------
class FakeChild:
    """toolchain.run stand-in: one scripted (stdout, returncode) per call from
    `outputs`, else always (stdout, returncode); `exc` is raised instead."""

    def __init__(self, returncode: int = 0, stdout: str = "", exc: Exception | None = None,
                 outputs: list[tuple[str, int]] | None = None) -> None:
        self.returncode, self.stdout, self.exc = returncode, stdout, exc
        self.outputs = iter(outputs) if outputs is not None else None
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, argv: list[str], **kw: Any) -> subprocess.CompletedProcess:
        self.calls.append((list(argv), kw))
        if self.exc:
            raise self.exc
        out, rc = next(self.outputs) if self.outputs else (self.stdout, self.returncode)
        return subprocess.CompletedProcess(argv, rc, out, "")


def _post_row(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, child: FakeChild,
              python: str = "venv", **extra: Any) -> run_all.Row:
    """A marker board 'm' with one post_check, its children answered by `child`."""
    _plugin_with_script(tmp_path, monkeypatch, "e2e_x_check.py")
    boards = tmp_path / "boards"
    board_dir(boards, "m", marker={"w": 10, "h": 10, **extra, "post_checks": [
        {"script": "pipeline/e2e_x_check.py", "python": python,
         "args": ["{pcb}", "{netlist}", "{dir}", "--report={report}"]}]})
    monkeypatch.setattr(toolchain, "run", child)
    return run_all.marker_row(boards, run_all.discover(boards)[0])


def test_post_check_argv_and_placeholders(tmp_path, monkeypatch, fake_run):
    d = tmp_path / "boards" / "m"
    pcb = str(d / "x.kicad_pcb")
    fake_run.script["m"] = lambda n: report(n, passing(), board_file={"path": pcb})
    child = FakeChild(0, '{"ok": true}')
    row = _post_row(tmp_path, monkeypatch, child)
    assert row.status == "PASS" and "post_check" in row.info["detail"], row
    (argv, kw), = child.calls
    plugin = tmp_path / "plugin"
    assert argv == [sys.executable, str(plugin / "pipeline" / "e2e_x_check.py"), pcb,
                    str(d / "netlist.json"), str(d),
                    f"--report={d / 'netlist'}_pipeline_report.json"]
    assert kw["stdin"] is subprocess.DEVNULL and kw["timeout"] == run_all.POST_CHECK_TIMEOUT_S
    assert kw["cwd"] == str(plugin)


def test_post_check_pcb_defaults_to_the_netlist_stem(tmp_path, monkeypatch, fake_run):
    child = FakeChild(0)
    _post_row(tmp_path, monkeypatch, child)
    assert child.calls[0][0][2] == str(tmp_path / "boards" / "m" / "netlist.kicad_pcb")


def test_a_failing_post_check_fails_the_row(tmp_path, monkeypatch, fake_run):
    row = _post_row(tmp_path, monkeypatch, FakeChild(1, "impedance off by 7 %"))
    assert row.status == "FAIL"
    assert "pipeline/e2e_x_check.py" in row.info["detail"] and "exit 1" in row.info["detail"]
    assert "impedance off by 7 %" in row.summary


def test_a_failing_post_checks_output_keeps_whole_last_lines(tmp_path, monkeypatch, fake_run):
    noise = "\n".join(f"line {i} " + "x" * 50 for i in range(20))
    row = _post_row(tmp_path, monkeypatch, FakeChild(3, noise + "\n\nFINAL: 2 checks failed\n"))
    assert row.summary.splitlines() == ["pipeline/e2e_x_check.py: exit 3"] + [
        f"line {i} " + "x" * 50 for i in range(15, 20)] + ["FINAL: 2 checks failed"]


def test_console_never_raises_on_unencodable_text(monkeypatch):
    seen = []

    class Stream:
        def reconfigure(self, **kw: Any) -> None:
            seen.append(kw)
    monkeypatch.setattr(sys, "stdout", Stream())
    monkeypatch.setattr(sys, "stderr", object())          # no reconfigure: left alone
    run_all._console_never_raises()
    assert seen == [{"errors": "replace"}]


def test_a_post_check_that_does_not_finish_fails_the_row(tmp_path, monkeypatch, fake_run):
    row = _post_row(tmp_path, monkeypatch, FakeChild(exc=subprocess.TimeoutExpired("py", 5)))
    assert row.status == "FAIL" and "TimeoutExpired" in row.info["detail"]


@pytest.mark.parametrize("kpy, status", [("C:/KiCad/bin/python.exe", "PASS"), (None, "FAIL")])
def test_kicad_post_check_runs_under_kicad_python(tmp_path, monkeypatch, fake_run, kpy, status):
    monkeypatch.setattr(kicad_server, "_kicad_python", lambda: kpy)
    child = FakeChild(0)
    row = _post_row(tmp_path, monkeypatch, child, python="kicad")
    assert row.status == status
    if kpy:
        assert child.calls[0][0][0] == kpy
    else:
        assert child.calls == [] and "KiCad's Python" in row.info["detail"]


def test_post_checks_do_not_run_when_the_run_missed(tmp_path, monkeypatch, fake_run):
    fake_run.script["m"] = lambda n: failing_at(n, "G2", "ERC: 3 errors")
    child = FakeChild(0)
    assert _post_row(tmp_path, monkeypatch, child).status == "FAIL" and child.calls == []


def test_post_checks_run_on_a_caught_expect_fail(tmp_path, monkeypatch, fake_run):
    fake_run.script["m"] = lambda n: failing_at(n, "G1", "lint: U9 unknown")
    child = FakeChild(0)
    row = _post_row(tmp_path, monkeypatch, child, expect="FAIL", gate="G1",
                    blocker_contains="lint")
    assert row.status == "PASS" and len(child.calls) == 1


# --- FAIL rows: the gate summary, the report path, crashes ---------------------------------
def test_fail_row_prints_the_gate_summary_under_it(tmp_path, fake_run, no_kicad, capsys):
    board_dir(tmp_path, "lm358_buffer")
    fake_run.script["lm358_buffer"] = lambda n: failing_at(
        n, "G4", "DRC: 2 unconnected items (net /OUT)")
    code, out, summary = run_main([str(tmp_path)], capsys)
    lines = out.splitlines()
    at = next(i for i, ln in enumerate(lines) if "lm358_buffer" in ln and "FAIL" in ln)
    assert "AT G4" in lines[at]
    tail = lines[at + 1:at + 6]
    assert any(ln.startswith("        ") and "BLOCKER: DRC: 2 unconnected items (net /OUT)" in ln
               for ln in tail), tail
    row = result(summary, "lm358_buffer")
    assert code == 1 and row["status"] == "FAIL" and row["gate"] == "G4"
    assert row["report"] == str(tmp_path / "lm358_buffer" / "netlist_pipeline_report.json")
    assert row["blockers"] == ["DRC: 2 unconnected items (net /OUT)"]
    assert summary["all_pass"] is False


def test_passing_rows_print_no_summary(tmp_path, fake_run, no_kicad, capsys):
    board_dir(tmp_path, "lm358_buffer")
    _, out, summary = run_main([str(tmp_path)], capsys)
    assert "BLOCKER" not in out and "[PASS] G1" not in out
    assert result(summary, "lm358_buffer")["report"].endswith("netlist_pipeline_report.json")


def test_a_crash_is_a_fail_row_and_the_matrix_goes_on(tmp_path, fake_run, no_kicad, capsys):
    board_dir(tmp_path, "lm358_buffer")
    board_dir(tmp_path, "usb_afe")
    stem = tmp_path / "lm358_buffer" / "netlist"
    written = failing_at(str(stem) + ".json", "G2", "schematic build crashed")
    Path(str(stem) + "_pipeline_report.json").write_text(json.dumps(written), encoding="utf-8")

    def boom(netlist: str) -> dict:
        raise RuntimeError("pcbnew exploded")
    fake_run.script["lm358_buffer"] = boom
    code, out, summary = run_main([str(tmp_path)], capsys)
    row = result(summary, "lm358_buffer")
    assert row["status"] == "FAIL" and "RuntimeError: pcbnew exploded" in row["detail"]
    assert "BLOCKER: schematic build crashed" in out
    assert result(summary, "usb_afe")["status"] == "PASS"
    assert code == 1


def test_a_handoff_is_a_fail_naming_it(tmp_path, fake_run):
    board_dir(tmp_path, "m", marker={"w": 10, "h": 10, "args": ["--route", "auto"]})
    fake_run.script["m"] = lambda n: report(n, passing(), PASS=False, status="handoff",
                                            handoff="netlist_route_handoff.json")
    row = run_all.marker_row(tmp_path, run_all.discover(tmp_path)[0])
    assert row.status == "FAIL" and "handed off" in row.info["detail"]


# --- the decisions column -------------------------------------------------------------------
def _decide(netlist: str, stage: str = "G2", source: str = "test") -> None:
    decisions.append_decision(netlist, stage=stage, question="q?", decision="d",
                              rationale="r", confidence="high", source=source)


def test_decisions_column_counts_this_runs_entries(tmp_path, fake_run, no_kicad, capsys):
    d = board_dir(tmp_path, "lm358_buffer")
    _decide(str(d / "netlist.json"))                      # an earlier run's entry

    def two(netlist: str) -> dict:
        _decide(netlist)
        _decide(netlist, "routing")
        return report(netlist, passing())
    fake_run.script["lm358_buffer"] = two
    _, out, summary = run_main([str(tmp_path)], capsys)
    assert result(summary, "lm358_buffer")["decisions"] == 2
    line = next(ln for ln in out.splitlines() if "lm358_buffer" in ln and "PASS" in ln)
    assert "decisions=2" in line
    assert "decisions=-" in next(ln for ln in out.splitlines() if "usb_afe" in ln)


def test_decisions_column_is_zero_without_a_log(tmp_path, fake_run):
    board_dir(tmp_path, "m", marker={"w": 10, "h": 10})
    assert run_all.marker_row(tmp_path, run_all.discover(tmp_path)[0]).info["decisions"] == 0


def test_malformed_decision_log_lines_are_named(tmp_path, fake_run):
    board_dir(tmp_path, "m", marker={"w": 10, "h": 10})

    def torn(netlist: str) -> dict:
        _decide(netlist)
        with decisions.decisions_path(netlist).open("a", encoding="utf-8") as f:
            f.write("{torn\n")
        return report(netlist, passing())
    fake_run.script["m"] = torn
    row = run_all.marker_row(tmp_path, run_all.discover(tmp_path)[0])
    assert row.status == "PASS" and row.info["decisions"] == 1
    assert "not JSON" in row.info["detail"] and "not JSON" in row.info["decision_log"][0]


def test_an_unreadable_decision_log_is_named_not_counted(tmp_path, fake_run, monkeypatch):
    _with_cp2102n(tmp_path)
    board_dir(tmp_path, "m", marker={"w": 10, "h": 10})

    def locked(path: Any) -> decisions.DecisionLog:
        raise PermissionError("log locked")
    monkeypatch.setattr(decisions, "load_decisions", locked)
    row = run_all.marker_row(tmp_path, run_all.discover(tmp_path)[0])
    assert row.status == "PASS" and row.info["decisions"] is None
    assert "unreadable" in row.info["detail"] and "log locked" in row.info["detail"]
    napkin = run_all._fixture_napkin_g3(tmp_path)
    assert napkin.status == "FAIL" and "log locked" in napkin.info["detail"]


# --- the G3 fixtures ------------------------------------------------------------------------
def _with_cp2102n(root: Path) -> None:
    (root / "cp2102n_uart").mkdir(parents=True, exist_ok=True)
    (root / "cp2102n_uart" / "netlist.json").write_bytes(CP2102N.read_bytes())


def _napkin_ok(netlist: str) -> dict:
    _decide(netlist, "G3", "run_pipeline G3 napkin policy D5.face_connector_out")
    return report(netlist, passing())


def test_napkin_g3_netlist_is_the_negative_g3_netlist(tmp_path):
    _with_cp2102n(tmp_path)
    strict = json.loads(Path(run_all._build_g3_fixture("negative_g3", tmp_path)).read_text())
    napkin = json.loads(Path(run_all._build_g3_fixture("napkin_g3", tmp_path)).read_text())
    assert napkin["name"] == "napkin_g3" and strict["name"] == "negative_g3"
    assert {**napkin, "name": "x"} == {**strict, "name": "x"}
    src = json.loads(CP2102N.read_text(encoding="utf-8"))   # J1 / C2 moved, the J9 wall added
    moved = {"J1": {"x": 45.0, "y": 14.0, "rot": 270}, "C2": {"x": 10.0, "y": 46.0}}
    assert strict["components"] == [*({**c, **moved.get(c["ref"], {})} for c in src["components"]),
                                     run_all.G3_WALL]
    wall_pins = [["J9", str(i)] for i in range(1, 41)]
    assert strict["no_connect"] == [*src.get("no_connect", []), *wall_pins]


def test_napkin_g3_passes_when_the_policy_turned_the_connector_out(tmp_path, fake_run):
    _with_cp2102n(tmp_path)
    fake_run.script["napkin_g3"] = _napkin_ok
    row = run_all._fixture_napkin_g3(tmp_path)
    assert fake_run.calls == [("napkin_g3", 56, 40, (), {"napkin": True,
                                                         "route": "freerouting"})]
    assert row.status == "PASS" and row.info["decisions"] == 1
    assert row.info["gate"] == row.info["overall_gate"] == "-"
    assert "face_connector_out" in row.info["detail"] and "overall PASS" in row.info["detail"]


def test_napkin_g3_fails_while_the_mating_face_still_blocks(tmp_path, fake_run):
    _with_cp2102n(tmp_path)

    def still(netlist: str) -> dict:
        _decide(netlist, "G3", "run_pipeline G3 napkin policy D5.face_connector_out")
        return failing_at(netlist, "G3", "J1: mating face points INTO the board interior")
    fake_run.script["napkin_g3"] = still
    row = run_all._fixture_napkin_g3(tmp_path)
    assert row.status == "FAIL" and "mating face" in row.info["detail"]
    assert "BLOCKER: J1: mating face" in row.summary


def test_napkin_g3_fails_without_a_face_connector_out_decision(tmp_path, fake_run):
    _with_cp2102n(tmp_path)
    nl = tmp_path / "napkin_g3" / "netlist.json"
    nl.parent.mkdir()
    _decide(str(nl), "G3", "run_pipeline G3 napkin policy D5.face_connector_out")  # stale
    row = run_all._fixture_napkin_g3(tmp_path)
    assert row.status == "FAIL" and "no G3 face_connector_out decision" in row.info["detail"]


def test_napkin_g3_fails_when_the_run_stops_before_g3(tmp_path, fake_run):
    _with_cp2102n(tmp_path)
    fake_run.script["napkin_g3"] = lambda n: failing_at(n, "G2", "ERC: 1 error")
    row = run_all._fixture_napkin_g3(tmp_path)
    assert row.status == "FAIL" and "before G3" in row.info["detail"]


def test_napkin_g3_reports_a_later_failure_in_detail(tmp_path, fake_run):
    _with_cp2102n(tmp_path)

    def later(netlist: str) -> dict:
        _napkin_ok(netlist)
        return failing_at(netlist, "G4", "DRC: 3 unconnected")
    fake_run.script["napkin_g3"] = later
    row = run_all._fixture_napkin_g3(tmp_path)
    assert row.status == "PASS"
    assert "overall FAIL AT G4" in row.info["detail"] and "DRC: 3 unconnected" in row.info["detail"]
    assert "BLOCKER: DRC: 3 unconnected" in row.summary
    # a PASS row shows no failing gate; the overall one is its own JSON field
    assert row.info["gate"] == "-" and row.info["overall_gate"] == "G4"
    assert run_all.format_row(row).count("AT G4") == 1          # only the detail's


def test_negative_g3_stays_strict_and_counts_decisions(tmp_path, fake_run):
    _with_cp2102n(tmp_path)
    fake_run.script["negative_g3"] = lambda n: report(n, [
        gate("G1"), gate("G2"),
        gate("G3", False, ("J1: mating face points INTO the board interior",),
             ("plane on In2.Cu is pinched",))])
    row = run_all._fixture_g3(tmp_path)
    assert fake_run.calls == [("negative_g3", 56, 40, (), {"napkin": False})]
    assert row.status == "PASS" and row.info["gate"] == "G3*" and row.info["decisions"] == 0


def test_negative_g3_that_stops_early_names_the_real_gate(tmp_path, fake_run):
    _with_cp2102n(tmp_path)
    fake_run.script["negative_g3"] = lambda n: failing_at(n, "G1", "LIB-E4 J1.SH is not a pad")
    row = run_all._fixture_g3(tmp_path)
    assert row.status == "FAIL" and row.info["gate"] == "G1"
    assert row.info["blockers"] == ["LIB-E4 J1.SH is not a pad"]
    assert "stopped before G3 (AT G1)" in row.info["detail"]
    assert "BLOCKER: LIB-E4 J1.SH is not a pad" in row.summary


def test_g3_fixtures_skip_without_cp2102n(tmp_path, fake_run):
    for fixture in (run_all._fixture_g3, run_all._fixture_napkin_g3):
        row = fixture(tmp_path)
        assert row.status == "SKIP" and "cp2102n_uart" in row.info["detail"]
    assert fake_run.calls == []


# --- negative_g4_heal -----------------------------------------------------------------------
def _routed_lm358(root: Path) -> Path:
    d = board_dir(root, "lm358_buffer")
    (d / "netlist.kicad_pcb").write_text("(kicad_pcb)", encoding="utf-8")
    (d / "netlist.kicad_pro").write_text("{}", encoding="utf-8")
    return d / "netlist.kicad_pcb"


def test_g4_heal_skips_without_kicad_python_or_this_runs_lm358_board(tmp_path, no_kicad):
    row = run_all._fixture_g4_heal(_routed_lm358(tmp_path), tmp_path)
    assert row.status == "SKIP" and "KiCad's Python" in row.info["detail"]
    row = run_all._fixture_g4_heal(None, tmp_path)
    assert row.status == "SKIP" and "lm358_buffer" in row.info["detail"]


def _cut(net: str, mm: float, rc: int = 0) -> tuple[str, int]:
    return json.dumps({"cut_net": net, "length_mm": mm}), rc


def _heal_world(monkeypatch, drc_results: list, fixes: list,
                cuts: list | None = None) -> FakeChild:
    monkeypatch.setattr(kicad_server, "_kicad_python", lambda: "kpy")
    child = FakeChild(outputs=cuts or [_cut("/OUT", 4.2)] * run_all.CUT_ATTEMPTS)
    monkeypatch.setattr(toolchain, "run", child)
    results = iter(drc_results)

    def drc(pcb: str) -> dict:
        r = next(results)
        if isinstance(r, Exception):
            raise r
        return {"errors": r[0], "unconnected": r[1], "types": {}}
    monkeypatch.setattr(rp_kicad, "drc", drc)
    monkeypatch.setattr(rp_kicad, "board_fix", lambda pcb, nl, mode: list(fixes))
    return child


def test_g4_heal_cuts_with_stdin_closed_and_heals_through_rp_kicad(tmp_path, monkeypatch):
    child = _heal_world(monkeypatch, [(0, 1), (0, 0)], ["healed /OUT gap 0.1 mm"])
    row = run_all._fixture_g4_heal(_routed_lm358(tmp_path), tmp_path)
    assert row.status == "PASS", row
    (argv, kw), = child.calls
    assert argv[0] == "kpy" and argv[1].endswith("swig_cut_track.py")
    assert argv[2] == str(tmp_path / "negative_g4_heal" / "netlist.kicad_pcb")
    assert kw["stdin"] is subprocess.DEVNULL and kw["timeout"] > 0
    assert (tmp_path / "negative_g4_heal" / "netlist.kicad_pro").is_file()
    assert (tmp_path / "negative_g4_heal" / "netlist.json").is_file()
    assert "healed (1 fix(es))" in row.info["detail"]


def test_g4_heal_open_findings_are_not_a_heal(tmp_path, monkeypatch):
    _heal_world(monkeypatch, [(0, 1), (0, 0)], ["OPEN: gap on /OUT not fixable"])
    assert run_all._fixture_g4_heal(_routed_lm358(tmp_path), tmp_path).status == "FAIL"


def test_g4_heal_cuts_past_a_plane_net_until_the_board_breaks(tmp_path, monkeypatch):
    """A net with a copper pour (the D7 4-layer default: lm358's P5V on In2.Cu)
    stays connected when its track is cut: the next-longest track is cut."""
    child = _heal_world(monkeypatch, [(0, 0), (0, 1), (0, 0)], ["healed IN_A gap"],
                        cuts=[_cut("P5V", 16.86, -11), _cut("IN_A", 9.1)])
    row = run_all._fixture_g4_heal(_routed_lm358(tmp_path), tmp_path)
    assert row.status == "PASS", row
    assert len(child.calls) == 2
    assert all(kw["stdin"] is subprocess.DEVNULL for _, kw in child.calls)
    assert "P5V 16.86 mm (still connected)" in row.info["detail"]
    assert "IN_A 9.1 mm -> 1 unconnected -> healed (1 fix(es)) -> 0" in row.info["detail"]


def test_g4_heal_cut_that_breaks_nothing_fails(tmp_path, monkeypatch):
    child = _heal_world(monkeypatch, [(0, 0)] * run_all.CUT_ATTEMPTS, [])
    row = run_all._fixture_g4_heal(_routed_lm358(tmp_path), tmp_path)
    assert row.status == "FAIL" and "no cut broke the board" in row.info["detail"]
    assert len(child.calls) == run_all.CUT_ATTEMPTS


def test_g4_heal_cut_script_without_a_result_fails(tmp_path, monkeypatch):
    _heal_world(monkeypatch, [], [], cuts=[("Traceback: pcbnew.LoadBoard returned None", 1)])
    row = run_all._fixture_g4_heal(_routed_lm358(tmp_path), tmp_path)
    assert row.status == "FAIL"
    assert "swig_cut_track.py cut nothing (exit 1" in row.info["detail"]
    assert "LoadBoard returned None" in row.info["detail"]


def test_g4_heal_drc_failure_is_a_fail_row(tmp_path, monkeypatch):
    _heal_world(monkeypatch, [RuntimeError("DRC of netlist.kicad_pcb failed: Error: x")], [])
    row = run_all._fixture_g4_heal(_routed_lm358(tmp_path), tmp_path)
    assert row.status == "FAIL" and "DRC of netlist.kicad_pcb failed" in row.info["detail"]


def test_g4_heal_gets_lm358s_routed_board_from_the_matrix(tmp_path, fake_run, monkeypatch,
                                                          capsys):
    pcb = _routed_lm358(tmp_path)
    fake_run.script["lm358_buffer"] = lambda n: report(n, passing(),
                                                       board_file={"path": str(pcb)})
    _heal_world(monkeypatch, [(0, 2), (0, 0)], ["healed"])
    code, _, summary = run_main([str(tmp_path)], capsys)
    assert result(summary, "lm358_buffer")["pcb"] == str(pcb)
    assert result(summary, "negative_g4_heal")["status"] == "PASS"
    assert code == 0
