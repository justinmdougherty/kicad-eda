"""determinism_check (F5-15): N fresh copies of one netlist through the pipeline,
compared field by field.

A fake runner stands in for '<python> run_pipeline.py <copy>/netlist.json': it
writes a scripted report, board, DSN and SES into the copy (per run number) and
records what it was called with. The real runner is exercised on tiny python
children only (output, exit code, timeout kill)."""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

import determinism_check as dc
import rp_context

GATES = "[PASS] G1 Requirements\n[PASS] G2 Schematic\n[PASS] G4 Layout"
AUTOROUTE = {"attempt": 1, "pack_scale": 1.0, "tracks": 3, "unconnected": 0,
             "dsn_sha256": "a" * 64, "dsn_seed": 0}


def board(via_x: float = 10.0) -> str:
    return ('(kicad_pcb (version 20241229) (generator "pcbnew") (net 0 "") (net 1 "SIG") '
            '(footprint "R:R_0603" (layer "F.Cu") (at 5 5) (property "Reference" "R1")) '
            '(segment (start 1 2) (end 8 2) (width 0.25) (layer "F.Cu") (net 1)) '
            f'(via (at {via_x} 5) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1)))')


def run_number(path: str) -> int:
    """The N of the run<N> folder a copy lives in."""
    return int(re.search(r"[\\/]run(\d+)[\\/]", path).group(1))


def outcome(**over: Any) -> dict[str, Any]:
    """One run's scripted result: rc, report keys, board text, dsn / ses text."""
    report = {"PASS": True, "gate_summary": GATES, "remediation": [], "autoroute": AUTOROUTE}
    base = {"rc": 0, "report": report, "board": board(), "dsn": "(pcb netlist.dsn)",
            "ses": "(session out)"}
    return {**base, **over}


class FakeRunner:
    """Writes outcome(run) into the copy the command names; thread-safe."""

    def __init__(self, script: dict[int, dict[str, Any]] | None = None,
                 delay: float = 0.0) -> None:
        self.script = script or {}
        self.delay = delay
        self.calls: list[tuple[list[str], Path, dict[str, str], float]] = []
        self.lock = threading.Lock()
        self.active = 0
        self.max_active = 0

    def __call__(self, cmd: list[str], cwd: Path, env: dict[str, str],
                 timeout: float) -> dc.RunResult:
        with self.lock:
            self.calls.append((cmd, cwd, env, timeout))
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        time.sleep(self.delay)
        netlist = Path(cmd[2])
        run = run_number(str(netlist))
        out = self.script.get(run, outcome())
        folder, stem = netlist.parent, netlist.stem
        if out["report"] is not None:
            report = {**out["report"], "netlist": str(netlist)}
            (folder / f"{stem}_pipeline_report.json").write_text(json.dumps(report), "utf-8")
        for suffix, key in ((".kicad_pcb", "board"), (".dsn", "dsn"), (".ses", "ses")):
            if out[key] is not None:
                (folder / f"{stem}{suffix}").write_text(out[key], encoding="utf-8")
        with self.lock:
            self.active -= 1
        return dc.RunResult(cmd, out["rc"], "{}", "stage log")


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A netlist folder with inputs AND an earlier run's generated outputs."""
    src = tmp_path / "proj"
    (src / "libs" / "P_parts.pretty").mkdir(parents=True)
    (src / "libs" / "P_parts.kicad_sym").write_text("(kicad_symbol_lib)", "utf-8")
    (src / "libs" / "P_parts.pretty" / "X.kicad_mod").write_text("(footprint X)", "utf-8")
    (src / "libs" / "__pycache__").mkdir()
    (src / "libs" / "__pycache__" / "m.cpython-312.pyc").write_bytes(b"\0")
    (src / "mech").mkdir()
    (src / "mech" / "outline.dxf").write_text("0\nEOF\n", "utf-8")
    (src / "pins").mkdir()
    (src / "pins" / "X.json").write_text("{}", "utf-8")
    (src / "notes").mkdir()
    (src / "notes" / "spec.txt").write_text("spec", "utf-8")
    (tmp_path / "shared").mkdir()
    (tmp_path / "shared" / "common.kicad_sym").write_text("(kicad_symbol_lib)", "utf-8")
    design = {"name": "p", "components": [{"ref": "R1", "value": "libs", "lib_id": "Device:R"}],
              "nets": {"GND": [["R1", "1"]]},
              "libraries": {"symbols": {"P_parts": "${KIPRJMOD}/libs/P_parts.kicad_sym",
                                        "common": "../shared/common.kicad_sym"},
                            "footprints": {"P_parts": "libs/P_parts.pretty"}},
              "board": {"w": 30, "h": 20, "outline": {"dxf": "mech/outline.dxf"}},
              "doc": "notes/spec.txt", "stale": "netlist.kicad_pcb", "up": ".."}
    (src / "netlist.json").write_text(json.dumps(design, indent=1), "utf-8")
    for name in ("netlist.kicad_pcb", "netlist.kicad_pcb.placed", "netlist.kicad_sch",
                 "netlist.kicad_pro", "netlist.dsn", "netlist.ses", "netlist.cache.json",
                 "netlist_pipeline_report.json", "netlist_review.md", "netlist.decisions.jsonl",
                 "netlist_drc.json", "netlist_place_top.png", "netlist_JLCPCB.zip"):
        (src / name).write_text("generated", "utf-8")
    (src / "netlist_gerbers").mkdir()
    (src / "netlist_gerbers" / "netlist-F_Cu.gtl").write_text("G04*", "utf-8")
    managed = ('(sym_lib_table (version 7) (lib (name "P_parts")(type "KiCad")'
               '(uri "${KIPRJMOD}/libs/P_parts.kicad_sym")(options "")'
               '(descr "kicad-eda managed")))')
    (src / "sym-lib-table").write_text(managed, "utf-8")
    return src / "netlist.json"


def files_under(root: Path) -> set[str]:
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}


def run_main(argv: list[str], runner: FakeRunner,
             capsys: pytest.CaptureFixture[str]) -> tuple[int, list[str], dict]:
    rc = dc.main(argv, runner=runner)
    lines = capsys.readouterr().out.strip().splitlines()
    return rc, lines, json.loads(lines[-1])["determinism_check"]


# --- the copy step ----------------------------------------------------------------------

def test_copy_takes_inputs_only_never_generated_outputs(project: Path, tmp_path: Path) -> None:
    copied = dc.copy_inputs(project, tmp_path / "run1")
    assert copied == tmp_path / "run1" / "proj" / "netlist.json"
    assert copied.read_bytes() == project.read_bytes()
    assert files_under(tmp_path / "run1") == {
        "proj/netlist.json", "proj/libs/P_parts.kicad_sym", "proj/libs/P_parts.pretty/X.kicad_mod",
        "proj/mech/outline.dxf", "proj/pins/X.json", "proj/notes/spec.txt",
        "shared/common.kicad_sym"}


def test_copy_keeps_a_project_table_with_rows_of_its_own(project: Path, tmp_path: Path) -> None:
    table = project.parent / "fp-lib-table"
    table.write_text('(fp_lib_table (version 7) (lib (name "Mine")(type "KiCad")'
                     '(uri "${KIPRJMOD}/vendor/Mine.pretty")(options "")(descr "")))', "utf-8")
    (project.parent / "vendor" / "Mine.pretty").mkdir(parents=True)
    (project.parent / "vendor" / "Mine.pretty" / "Y.kicad_mod").write_text("(footprint Y)")
    dc.copy_inputs(project, tmp_path / "run1")
    got = files_under(tmp_path / "run1")
    assert {"proj/fp-lib-table", "proj/vendor/Mine.pretty/Y.kicad_mod"} <= got
    assert "proj/sym-lib-table" not in got          # only managed rows: generated


def test_copy_of_a_self_contained_netlist_sits_at_the_run_root(tmp_path: Path) -> None:
    (tmp_path / "solo").mkdir()
    netlist = tmp_path / "solo" / "board.json"
    netlist.write_text('{"components": [], "nets": {}}', "utf-8")
    assert dc.copy_inputs(netlist, tmp_path / "r") == tmp_path / "r" / "board.json"


# --- agreement and disagreement ---------------------------------------------------------

def test_agreeing_runs_exit_0_with_a_table_and_a_json_line(
        project: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    runner = FakeRunner()
    rc, lines, summary = run_main([str(project), "--runs", "3"], runner, capsys)
    assert rc == 0
    assert summary["agree"] is True and summary["differing"] == [] and summary["runs"] == 3
    assert len(summary["durations_s"]) == 3 and summary["exit_codes"] == [0, 0, 0]
    table = "\n".join(lines[:-1])
    for field in dc.FIELDS:
        assert re.search(rf"^{field}\s+yes\b", table, re.M), field
    copies = {Path(cmd[2]).parent for cmd, *_ in runner.calls}
    assert len(copies) == 3 and project.parent not in copies
    for cmd, cwd, env, timeout in runner.calls:
        assert cmd[:2] == [sys.executable, str(dc.RUN_PIPELINE)] and len(cmd) == 3
        assert cwd == Path(cmd[2]).parent and timeout == dc.DEFAULT_TIMEOUT_S
        assert env["PIPELINE_NO_CACHE"] == "1"
    assert not any(c.exists() for c in copies)          # temporary run folders removed


def test_a_different_fingerprint_exits_1_naming_the_field_and_the_diff(
        project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    runner = FakeRunner({2: outcome(board=board(via_x=10.5))})
    rc, lines, summary = run_main([str(project), "--runs", "2"], runner, capsys)
    assert rc == 1
    assert summary["agree"] is False and summary["differing"] == ["board_fingerprint"]
    text = "\n".join(lines)
    assert re.search(r"^board_fingerprint\s+NO\b", text, re.M)
    assert "DIFFER: board_fingerprint" in text
    assert "via SIG at (10, 5): at (10, 5) -> (10.5, 5)" in text


def test_verdict_only_compares_exit_code_gates_and_remediation(
        project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    moved = outcome(board=board(via_x=10.5), dsn="(pcb other)", ses="(session other)",
                    report={**outcome()["report"], "autoroute": {**AUTOROUTE, "dsn_seed": 1}})
    rc, _, summary = run_main([str(project), "--runs", "2", "--verdict-only"],
                              FakeRunner({2: moved}), capsys)
    assert rc == 0 and summary["fields"] == list(dc.VERDICT_FIELDS)
    fixed = outcome(report={**outcome()["report"],
                            "remediation": [{"gate": "G4", "round": 1, "applied": ["heal"]}]})
    rc, lines, summary = run_main([str(project), "--runs", "2", "--verdict-only"],
                                  FakeRunner({2: fixed}), capsys)
    assert rc == 1 and summary["differing"] == ["remediation"]
    assert "DIFFER: remediation" in "\n".join(lines)


@pytest.mark.parametrize("change, field", [
    ({"rc": 1}, "exit_code"),
    ({"dsn": "(pcb seeded)"}, "dsn_file_sha256"),
    ({"ses": "(session other)"}, "ses_file_sha256"),
    ({"report": {"PASS": False, "gate_summary": "[FAIL] G4", "autoroute": AUTOROUTE}},
     "gate_summary"),
    ({"report": {"PASS": True, "gate_summary": GATES,
                 "autoroute": {**AUTOROUTE, "pack_scale": 1.3}}}, "pack_scale"),
    ({"report": {"PASS": True, "gate_summary": GATES,
                 "autoroute": {**AUTOROUTE, "alternates": [{"seed": 0}]}}}, "alternates"),
])
def test_each_compared_field_is_named_when_it_differs(
        project: Path, capsys: pytest.CaptureFixture[str], change: dict, field: str) -> None:
    rc, _, summary = run_main([str(project), "--runs", "3"],
                              FakeRunner({3: outcome(**change)}), capsys)
    assert rc == 1 and field in summary["differing"]


def test_the_copy_path_in_a_report_is_not_a_difference(
        project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    class PathRunner(FakeRunner):
        def __call__(self, cmd: list[str], cwd: Path, env: dict[str, str],
                     timeout: float) -> dc.RunResult:
            here = Path(cmd[2]).parent
            self.script = {**self.script, run_number(cmd[2]): outcome(
                report={**outcome()["report"], "gate_summary": f"{GATES}\n  wrote {here}",
                        "remediation": [{"gate": "G2", "round": 1,
                                         "applied": [f"sibling {here.as_posix()}/x"]}]})}
            return super().__call__(cmd, cwd, env, timeout)

    rc, _, summary = run_main([str(project), "--runs", "2"], PathRunner(), capsys)
    assert rc == 0, summary


def test_a_run_without_a_report_fails_the_check(
        project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc, lines, summary = run_main([str(project), "--runs", "2"],
                                  FakeRunner({2: outcome(rc=1, report=None, board=None)}), capsys)
    assert rc == 1
    assert any("run 2: no pipeline report" in p for p in summary["problems"])


# --- runs: parallelism, kept folders, extra arguments ------------------------------------

def test_jobs_run_in_parallel(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    runner = FakeRunner(delay=0.3)
    rc, _, summary = run_main([str(project), "--runs", "4", "--jobs", "2"], runner, capsys)
    assert rc == 0 and runner.max_active == 2 and summary["jobs"] == 2


def test_keep_leaves_the_run_folders_and_refuses_used_ones(
        project: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    keep = tmp_path / "kept"
    rc, _, summary = run_main([str(project), "--runs", "2", "--keep", str(keep)],
                              FakeRunner(), capsys)
    assert rc == 0 and summary["kept"] == str(keep)
    for i in (1, 2):
        assert (keep / f"run{i}" / "proj" / "netlist_pipeline_report.json").is_file()
        assert (keep / f"run{i}" / "proj" / "determinism_run.stderr.txt").is_file()
    assert dc.main([str(project), "--runs", "2", "--keep", str(keep)], runner=FakeRunner()) == 2
    assert "not empty" in capsys.readouterr().err


def test_extra_arguments_follow_the_netlist(project: Path, tmp_path: Path,
                                            capsys: pytest.CaptureFixture[str]) -> None:
    placement = tmp_path / "placement.json"
    placement.write_text("{}", "utf-8")
    runner = FakeRunner()
    rc, _, _ = run_main([str(project), "--runs", "2", "--", "60", "40", str(placement)],
                        runner, capsys)
    assert rc == 0
    assert all(cmd[3:] == ["60", "40", str(placement)] for cmd, *_ in runner.calls)


@pytest.mark.parametrize("argv, message", [
    (["--runs", "2"], "netlist"),
    (["missing.json", "--runs", "2"], "no netlist"),
    (["{netlist}", "--runs", "1"], "at least 2"),
    (["{netlist}", "--runs", "2", "--jobs", "0"], "--jobs"),
    (["{netlist}"], "--runs"),
])
def test_usage_errors_exit_2(project: Path, capsys: pytest.CaptureFixture[str],
                             argv: list[str], message: str) -> None:
    argv = [a.replace("{netlist}", str(project)) for a in argv]
    runner = FakeRunner()
    assert dc.main(argv, runner=runner) == 2
    assert message in capsys.readouterr().err and runner.calls == []


# --- the real runner and the harness contract ---------------------------------------------

def test_the_real_runner_runs_a_child_and_kills_it_on_timeout(tmp_path: Path) -> None:
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    ok = dc.run_command([sys.executable, "-c", "print('h\\u00e9'); raise SystemExit(3)"],
                        tmp_path, env, 60)
    assert ok.returncode == 3 and ok.stdout.strip() == "hé"
    started = time.monotonic()
    slow = dc.run_command([sys.executable, "-c", "import time; time.sleep(60)"],
                          tmp_path, env, 1.0)
    assert slow.returncode == dc.TIMEOUT_RC and "timed out" in slow.stderr
    assert time.monotonic() - started < 30


def test_the_report_name_is_the_harness_one() -> None:
    assert dc.REPORT_SUFFIX == rp_context.REPORT_SUFFIX
    assert dc.RUN_PIPELINE.name == "run_pipeline.py" and dc.RUN_PIPELINE.is_file()
