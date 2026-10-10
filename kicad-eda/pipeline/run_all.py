#!/usr/bin/env python3
"""Regression suite: every test board's netlist through the gated pipeline,
printed as a PASS/FAIL matrix (QFN, LQFP, multi-unit and multi-rail boards).

Usage: <venv-python> run_all.py [--list] [boards_dir]   (default: examples/)
       --list prints the matrix it would run and runs nothing.
       Exit 0 ALL PASS, 1 a row failed (--list: a bad marker), 2 usage.

The matrix, in order: (1) the static BOARDS (absent from examples/: MISSING,
a failure; from another boards_dir: SKIP); (2) each <boards_dir>/<name>/
run_all.json marker, sorted by name (how a new example joins without editing
this file); (3) the fixtures derived each run: negative_g3 (strict G3 must
DETECT an inward connector and a plane pinch), napkin_g3 (the same netlist in
napkin mode: the D5 policy must turn the connector out, a G3
face_connector_out decision; the overall verdict is in the detail and, on a
PASS row, its failing gate in the JSON's overall_gate),
negative_g4_heal (a track cut on this run's routed lm358 board; the G4 healer
must restore it). A fixture without its source or KiCad's Python: SKIP.

Every board routes with route='freerouting' (CI never hands routing to the
agent; a marker may override it in "args"). ALL PASS = no FAIL or MISSING row
and at least one row ran. A FAIL row prints the run's gate summary under it;
the JSON summary (last line) carries each run's report path; decisions=<n>
counts the entries the run appended to <stem>.decisions.jsonl ('-' on a row
that ran no pipeline, or when the log cannot be read: the detail names that
and any malformed line).

Marker <name>/run_all.json (beside <name>/netlist.json); every key optional:
  {"w": mm, "h": mm,              both or neither (neither: the harness's own
                                  rule: netlist board block, a feature, auto)
   "expect": "PASS" | "FAIL",     default PASS
   "gate": "G1".."G5",            expect FAIL only, and required there: the
   "blocker_contains": "text",    gate that must fail, with a blocker holding text
   "args": ["--strict", "--route", "agent", "place.json"],
                                  extra run_pipeline arguments (a placement
                                  file is relative to the marker's folder;
                                  no --resume: every board starts fresh)
   "post_checks": [{"script": "pipeline/<x>.py", "python": "venv" | "kicad",
                    "args": ["{pcb}", "{netlist}", "{dir}", "{report}"]}]}
A post_check runs once the board met its expectation (stdin closed, cwd = the
plugin root, POST_CHECK_TIMEOUT_S); a non-zero exit fails the row. A "kicad"
script imports kicad_quiet before pcbnew (ROADMAP D0). A marker that does
not validate is a FAIL row naming every problem.
"""
from __future__ import annotations

import json
import math
import shutil
import string
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, NamedTuple, Sequence

import run_pipeline as rp  # first: its rp_paths puts server/ and server/tools/ on sys.path
import decisions
import rp_cli
import rp_context
import rp_kicad
import rp_paths
import toolchain as tc

PLUGIN = Path(__file__).resolve().parents[1]
DEFAULT_BOARDS = PLUGIN / "examples"
HERE = DEFAULT_BOARDS            # the boards dir; main() takes another from the command line
ROUTE = rp_context.ROUTE_FREEROUTING   # CI never hands routing to the agent
MARKER = "run_all.json"
LIST_FLAG = "--list"
USAGE = f"usage: run_all.py [{LIST_FLAG}] [boards_dir]"
EXIT_PASS, EXIT_FAIL, EXIT_USAGE = 0, 1, 2
POST_CHECK_TIMEOUT_S = 1800
CUT_TIMEOUT_S = 120
CUT_ATTEMPTS = 4                 # negative_g4_heal: cuts until one disconnects the board
OUTPUT_TAIL = 400                # chars kept per line of a child's output in a FAIL row
TAIL_LINES = 6                   # lines of a failed post_check's output kept
INDENT = " " * 8                 # the gate summary under a FAIL row

# (folder, board_w, board_h) — dims are hints; auto-placement sizes the board.
BOARDS = [
    ("cp2102n_uart", 50, 50),
    ("stm32f103_dev", 50, 50),
    ("lm358_buffer", 30, 30),
    ("usb_afe", 40, 40),
    ("lm324_quad", 35, 35),
    ("buck_5v3a", 30, 60),
    ("rf_coupon_50r", 40, 25),
    ("rf_lna_gainblock", 35, 30),
    ("esp32c3_env_node", 50, 50),     # practical board: module+overhang, holes, BOM/CPL
    ("negative_gate_test", 60, 40),   # self-healing fixture, reset each run
]

NEG_GATE, NEG_GATE_SOURCE = "negative_gate_test", "buck_5v3a"
G3_FIXTURE, NAPKIN_G3, G4_FIXTURE = "negative_g3", "napkin_g3", "negative_g4_heal"
FIXTURES: tuple[str, ...] = (G3_FIXTURE, NAPKIN_G3, G4_FIXTURE)
G3_SOURCE, G4_SOURCE = "cp2102n_uart", "lm358_buffer"
G3_DIMS: tuple[float, float] = (56, 40)
G3_MOVES = {"J1": {"x": 45.0, "y": 14.0, "rot": 270},     # USB-C: mid-board, mouth inward
            "C2": {"x": 10.0, "y": 46.0}}                 # +3V3 cap stranded below the wall
# the wall: pad antipads span ~51 mm, leaving ~1.2 mm fill corridors at each
# board edge, under the 1.8 mm pinch radius, so G3 must warn
G3_WALL = {"ref": "J9", "lib_id": "Connector_Generic:Conn_02x20_Odd_Even", "value": "WALL",
           "footprint": "Connector_PinSocket_2.54mm:PinSocket_2x20_P2.54mm_Vertical",
           "x": 27.0, "y": 34.0, "rot": 90, "interior": True}
FACE_OUT = "face_connector_out"  # the napkin policy napkin_g3 proves (rp_napkin.POLICY_FIXES)
OK_STATUSES = ("PASS", "SKIP")

MARKER_KEYS = ("w", "h", "expect", "gate", "blocker_contains", "args", "post_checks")
POST_CHECK_KEYS = ("script", "python", "args")
EXPECTS = ("PASS", "FAIL")
GATES = ("G1", "G2", "G3", "G4", "G5")
PYTHONS = ("venv", "kicad")
PLACEHOLDERS = ("pcb", "netlist", "dir", "report")


class PostCheck(NamedTuple):
    script: str                   # pipeline/<x>.py, relative to the plugin root
    python: str = "venv"          # venv (this interpreter) | kicad (KiCad's bundled Python)
    args: tuple[str, ...] = ()    # with {pcb} {netlist} {dir} {report}


class Marker(NamedTuple):
    w: float | None = None
    h: float | None = None
    expect: str = "PASS"
    gate: str | None = None
    blocker_contains: str | None = None
    args: tuple[str, ...] = ()
    post_checks: tuple[PostCheck, ...] = ()


class Entry(NamedTuple):
    """One discovered marker: the parsed marker, or None and its problems."""
    name: str
    marker: Marker | None
    problems: tuple[str, ...] = ()


class Run(NamedTuple):
    report: dict
    error: str | None             # a crash (rp.run raised) or the report's own error
    decided: tuple[dict, ...] | None    # the log entries this run appended (None: unreadable)
    log_problems: tuple[str, ...] = ()  # why decided is None; malformed lines skipped


class Row(NamedTuple):
    board: str
    status: str                   # PASS | FAIL | SKIP | MISSING
    info: dict                    # the matrix columns, detail, report path (also the JSON)
    summary: str = ""             # printed indented under the row ("" = nothing)


# --- markers: validation, parsing, discovery ----------------------------------------------
def _unknown(data: dict, allowed: Sequence[str]) -> list[str]:
    return [f"unknown field {k!r} (allowed: {', '.join(allowed)})" for k in data
            if k not in allowed]


def _strings(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(v, str) for v in value)


def _dims_problems(data: dict) -> list[str]:
    given = [k for k in ("w", "h") if k in data]
    if len(given) == 1:
        return ["give both w and h, or neither"]
    return [f"{k} must be a positive number of mm, got {data[k]!r}" for k in given
            if not (isinstance(data[k], (int, float)) and not isinstance(data[k], bool)
                    and math.isfinite(data[k]) and data[k] > 0)]


def _expect_problems(data: dict) -> list[str]:
    expect = data.get("expect", "PASS")
    if expect not in EXPECTS:
        return [f"expect must be PASS or FAIL, got {expect!r}"]
    if expect == "PASS":
        return [f"{k} only with expect FAIL" for k in ("gate", "blocker_contains") if k in data]
    problems = []
    if "gate" not in data:
        problems.append("expect FAIL needs gate (G1..G5)")
    elif data["gate"] not in GATES:
        problems.append(f"gate must be one of {', '.join(GATES)}, got {data['gate']!r}")
    text = data.get("blocker_contains")
    if "blocker_contains" not in data:
        problems.append("expect FAIL needs blocker_contains (text the gate's blocker holds)")
    elif not (isinstance(text, str) and text.strip()):
        problems.append(f"blocker_contains must be non-blank text, got {text!r}")
    return problems


def _args_problems(args: Any) -> list[str]:
    if not _strings(args):
        return [f"args must be a list of strings, got {args!r}"]
    try:
        cli = rp_cli.parse_cli(["netlist.json", *args])
    except rp_cli.UsageError as e:
        return [f"args: {e}"]
    problems = ["args: the board size goes in w/h, not in args"] if cli.w is not None else []
    if cli.resume:
        problems.append(f"args: {rp_cli.RESUME_FLAG} continues a handed-off run; run_all "
                        f"starts every board fresh")
    return problems


def _script_problem(script: Any) -> str | None:
    if not isinstance(script, str) or not script.strip():
        return f"script must be a pipeline/<name>.py path, got {script!r}"
    pipeline = (PLUGIN / "pipeline").resolve()
    path = (PLUGIN / script).resolve()
    if Path(script).is_absolute() or path.parent != pipeline or path.suffix != ".py":
        return f"script {script!r} must be a .py file directly in pipeline/"
    if not path.is_file():
        return f"script {script!r} not found in {pipeline}"
    return None


def _placeholder_problems(arg: str) -> list[str]:
    try:
        fields = [f for _, f, _, _ in string.Formatter().parse(arg) if f is not None]
    except ValueError as e:
        return [f"arg {arg!r}: {e}"]
    allowed = ", ".join("{" + p + "}" for p in PLACEHOLDERS)
    return [f"arg {arg!r}: unknown placeholder {{{f}}} (allowed: {allowed})" for f in fields
            if f not in PLACEHOLDERS]


def _post_check_problems(index: int, check: Any) -> list[str]:
    where = f"post_checks[{index}]"
    if not isinstance(check, dict):
        return [f"{where} must be an object {{script, python, args}}, got {check!r}"]
    problems = _unknown(check, POST_CHECK_KEYS)
    script = _script_problem(check.get("script"))
    problems += [script] if script else []
    if check.get("python", "venv") not in PYTHONS:
        problems.append(f"python must be venv or kicad, got {check['python']!r}")
    args = check.get("args", [])
    problems += ([f"args must be a list of strings, got {args!r}"] if not _strings(args)
                 else [p for a in args for p in _placeholder_problems(a)])
    return [f"{where}: {p}" for p in problems]


def marker_problems(data: Any) -> list[str]:
    """Every problem of a parsed run_all.json ([] = valid), all at once."""
    if not isinstance(data, dict):
        return [f"must be a JSON object, got {type(data).__name__}"]
    checks = data.get("post_checks", [])
    check_problems = ([f"post_checks must be a list of objects, got {checks!r}"]
                      if not isinstance(checks, list)
                      else [p for i, c in enumerate(checks) for p in _post_check_problems(i, c)])
    return (_unknown(data, MARKER_KEYS) + _dims_problems(data) + _expect_problems(data)
            + _args_problems(data.get("args", [])) + check_problems)


def to_marker(data: dict) -> Marker:
    """The Marker of a run_all.json that marker_problems passed."""
    checks = tuple(PostCheck(c["script"], c.get("python", "venv"), tuple(c.get("args", ())))
                   for c in data.get("post_checks", ()))
    return Marker(w=float(data["w"]) if "w" in data else None,
                  h=float(data["h"]) if "h" in data else None,
                  expect=data.get("expect", "PASS"), gate=data.get("gate"),
                  blocker_contains=data.get("blocker_contains"),
                  args=tuple(data.get("args", ())), post_checks=checks)


def _name_problems(name: str) -> list[str]:
    if name in {b[0] for b in BOARDS}:
        return [f"{name} is a static BOARDS row already (a marker would run it twice)"]
    if name in FIXTURES:
        return [f"{name} is a fixture run_all derives on every run (use another folder name)"]
    return []


def read_entry(path: Path) -> Entry:
    """One <name>/run_all.json: parsed and validated (never raises)."""
    name = path.parent.name
    problems = _name_problems(name)
    if not (path.parent / "netlist.json").is_file():
        problems = [*problems, f"no netlist.json beside {MARKER}"]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as e:
        return Entry(name, None, (*problems, f"cannot read it: {e}"))
    except ValueError as e:
        return Entry(name, None, (*problems, f"not JSON: {e}"))
    problems = [*problems, *marker_problems(data)]
    return Entry(name, None if problems else to_marker(data), tuple(problems))


def discover(boards: Path) -> list[Entry]:
    """Every <boards>/<name>/run_all.json, sorted by folder name."""
    return [read_entry(p) for p in sorted(boards.glob(f"*/{MARKER}"),
                                           key=lambda p: p.parent.name)]


# --- one pipeline run and its row --------------------------------------------------------
def _report_path(netlist: Path) -> Path:
    return Path(str(netlist.with_suffix("")) + rp_context.REPORT_SUFFIX)


def _report_on_disk(netlist: Path, since: float) -> dict:
    """The report a crashed run wrote (rp.run writes it, then re-raises); {}
    when there is none newer than the run's start."""
    path = _report_path(netlist)
    try:
        if path.stat().st_mtime < since:
            return {}
        rep = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return rep if isinstance(rep, dict) else {}


def _decision_log(netlist: Path) -> tuple[tuple[dict, ...] | None, tuple[str, ...]]:
    """(the log's valid entries, its problems): (None, why) when it cannot be
    read; each malformed line is named (decisions.load_decisions skips it)."""
    try:
        log = decisions.load_decisions(netlist)
    except OSError as e:
        return None, (f"decision log {decisions.decisions_path(netlist).name} unreadable "
                      f"({type(e).__name__}: {e})",)
    return log.entries, tuple(f"decision log line skipped: {p}" for p in log.errors)


def run_netlist(netlist: Path, w: float, h: float, **options: Any) -> Run:
    """rp.run(netlist, w, h, **options); a crash becomes the report on disk
    plus the error, so one board never stops the matrix. decided = the log
    entries whose id the log lacked before (None if a read failed)."""
    print(f"run_all: {netlist.parent.name} ...", file=sys.stderr, flush=True)
    before, before_problems = _decision_log(netlist)
    start = time.time() - 1.0
    try:
        rep, error = rp.run(str(netlist), w, h, **options), None
    except Exception as e:                  # a FAIL row; the matrix goes on
        rep, error = _report_on_disk(netlist, start), f"{type(e).__name__}: {e}"
    rep = rep if isinstance(rep, dict) else {}
    after, after_problems = _decision_log(netlist)
    seen = {e["id"] for e in before or ()}
    decided = (None if before is None or after is None
               else tuple(e for e in after if e["id"] not in seen))
    problems = tuple(dict.fromkeys(before_problems + after_problems))
    return Run(rep, error or rep.get("error"), decided, problems)


def _detail(run: Run, detail: str) -> str:
    """`detail` plus the first decision-log problem (and how many more)."""
    more = len(run.log_problems) - 1
    note = (run.log_problems[0] + (f" (+{more} more)" if more > 0 else "")
            if run.log_problems else "")
    return "; ".join(d for d in (detail, note) if d)


def _failed_gate(rep: dict) -> str:
    return next((str(g.get("gate")) for g in rep.get("gates", []) if not g.get("passed")), "-")


def _gate(rep: dict, name: str) -> dict:
    return next((g for g in rep.get("gates", []) if g.get("gate") == name), {})


def _blockers(rep: dict, name: str) -> list[str]:
    failed = next((g for g in rep.get("gates", []) if g.get("gate") == name
                   and not g.get("passed")), {})
    return [str(b) for b in failed.get("blockers") or []]


def _columns(run: Run) -> dict:
    rep = run.report
    sch, drc, auto = (rep.get(k) or {} for k in ("schematic", "drc", "autoroute"))
    return {"erc": sch.get("erc_violations"), "drc_err": drc.get("errors"),
            "unconn": drc.get("unconnected"), "scale": auto.get("pack_scale"),
            "gate": _failed_gate(rep),
            "remediated": sum(len(r.get("applied", [])) for r in rep.get("remediation", [])),
            "decisions": None if run.decided is None else len(run.decided),
            "report": rep.get("pipeline_report"),
            "pcb": (rep.get("board_file") or {}).get("path"),
            **({"decision_log": list(run.log_problems)} if run.log_problems else {})}


def _summary(run: Run) -> str:
    lines = [run.report.get("gate_summary") or "", f"ERROR: {run.error}" if run.error else ""]
    return "\n".join(line for line in lines if line)


def _overall(rep: dict) -> str:
    if rep.get("PASS"):
        return "PASS"
    at = _failed_gate(rep)
    first = next(iter(_blockers(rep, at)), rep.get("status") or "no blocker recorded")
    return f"FAIL AT {at}: {first}"


def judge(run: Run, marker: Marker) -> tuple[bool, str]:
    """(met, detail): did the run meet the marker's expectation?"""
    rep = run.report
    if run.error:
        return False, f"crashed: {run.error}"
    if marker.expect == "PASS":
        if rep.get("PASS"):
            return True, ""
        if rep.get("status") == rp_context.STATUS_HANDOFF:
            return False, f"routing handed off to the agent ({rep.get('handoff')})"
        return False, ""
    if rep.get("PASS"):
        return False, f"expected FAIL at {marker.gate}, but the run passed"
    at = _failed_gate(rep)
    if at != marker.gate:
        return False, f"expected FAIL at {marker.gate}, failed at {at}"
    if not any(str(marker.blocker_contains) in b for b in _blockers(rep, at)):
        return False, f"failed at {at} without a blocker containing {marker.blocker_contains!r}"
    return True, f"failed at {at} as expected ({marker.blocker_contains!r})"


def run_row(name: str, run: Run, met: bool, detail: str, expected_gate: str | None = None) -> Row:
    """PASS when the run met its expectation (an expected failure's gate shows
    as 'Gn*'); a FAIL row names its gate's blockers and prints the summary."""
    shown = _detail(run, detail)
    info = {**_columns(run), **({"detail": shown} if shown else {})}
    if met:
        return Row(name, "PASS", {**info, "gate": f"{expected_gate}*"} if expected_gate else info)
    return Row(name, "FAIL", {**info, "blockers": _blockers(run.report, info["gate"])},
               _summary(run))


def _skip(name: str, reason: str) -> Row:
    return Row(name, "SKIP", {"detail": reason})


def static_row(boards: Path, folder: str, w: float, h: float) -> Row:
    netlist = boards / folder / "netlist.json"
    if not netlist.is_file():
        if boards.resolve() == DEFAULT_BOARDS.resolve():
            return Row(folder, "MISSING", {"detail": f"no {folder}/netlist.json in {boards}"})
        return _skip(folder, f"not in {boards}")
    run = run_netlist(netlist, w, h, route=ROUTE)
    return run_row(folder, run, *judge(run, Marker()))


# --- marker rows and their post_checks ----------------------------------------------------
def marker_call(marker: Marker, netlist: Path) -> tuple[float, float, dict[str, Any]]:
    """(w, h, the rp.run keywords) of a marker board: its w/h, else the harness's
    dims rule (rp_cli.cli_dims; UsageError when undecidable); napkin and route
    always given (route freerouting unless its args say --route)."""
    cli = rp_cli.parse_cli([str(netlist), *marker.args])
    if cli.placement and not Path(cli.placement).is_absolute():
        cli = cli._replace(placement=str(netlist.parent / cli.placement))
    if marker.w is not None and marker.h is not None:
        w, h = marker.w, marker.h
    else:
        w, h, _ = rp_cli.cli_dims(cli)
    asked = any(a == rp_cli.ROUTE_FLAG or a.startswith(rp_cli.ROUTE_FLAG + "=")
                for a in marker.args)
    return w, h, {"napkin": cli.napkin, "route": cli.route if asked else ROUTE,
                  **({"placement": cli.placement} if cli.placement else {})}


def marker_row(boards: Path, entry: Entry) -> Row:
    if entry.marker is None:
        return Row(entry.name, "FAIL", {"gate": "-", "detail": f"bad {MARKER}: "
                                        + "; ".join(entry.problems)})
    marker, netlist = entry.marker, boards / entry.name / "netlist.json"
    try:
        w, h, options = marker_call(marker, netlist)
    except Exception as e:                  # e.g. a malformed netlist: a FAIL row, never a stop
        why = (f"usage: {e}" if isinstance(e, rp_cli.UsageError)
               else f"cannot size the board: {type(e).__name__}: {e}")
        return Row(entry.name, "FAIL", {"gate": "-", "detail": why})
    run = run_netlist(netlist, w, h, **options)
    met, detail = judge(run, marker)
    row = run_row(entry.name, run, met, detail, marker.gate if marker.expect == "FAIL" else None)
    return post_checks_row(row, marker.post_checks, netlist, run) if met else row


def _placeholder_values(netlist: Path, rep: dict) -> dict[str, str]:
    pcb = (rep.get("board_file") or {}).get("path") or str(netlist.with_suffix(".kicad_pcb"))
    return {"pcb": str(pcb), "netlist": str(netlist), "dir": str(netlist.parent),
            "report": str(rep.get("pipeline_report") or _report_path(netlist))}


def run_post_check(check: PostCheck, values: dict[str, str]) -> str | None:
    """None when the check exits 0, else why it failed (first line: the
    script and the reason; then its output tail)."""
    python = sys.executable if check.python == "venv" else rp.k._kicad_python()
    if not python:
        return f"{check.script}: KiCad's Python (pcbnew) not found: cannot run a kicad post_check"
    argv = [python, str(PLUGIN / check.script), *(a.format_map(values) for a in check.args)]
    try:
        r = tc.run(argv, stdin=subprocess.DEVNULL, timeout=POST_CHECK_TIMEOUT_S, cwd=str(PLUGIN))
    except (OSError, subprocess.SubprocessError) as e:
        return f"{check.script}: did not finish ({type(e).__name__}: {e})"
    if r.returncode == 0:
        return None
    tail = _tail((r.stdout or "") + "\n" + (r.stderr or ""))
    return f"{check.script}: exit {r.returncode}" + (f"\n{tail}" if tail else "")


def _tail(text: str) -> str:
    """A child output's last TAIL_LINES non-blank lines (each <= OUTPUT_TAIL chars)."""
    lines = [ln.rstrip()[:OUTPUT_TAIL] for ln in text.splitlines() if ln.strip()]
    return "\n".join(lines[-TAIL_LINES:])


def post_checks_row(row: Row, checks: Sequence[PostCheck], netlist: Path, run: Run) -> Row:
    if not checks:
        return row
    values = _placeholder_values(netlist, run.report)
    failures = [f for f in (run_post_check(c, values) for c in checks) if f]
    if failures:
        detail = "post_check failed: " + "; ".join(f.splitlines()[0] for f in failures)
        return Row(row.board, "FAIL", {**row.info, "detail": detail}, "\n".join(failures))
    note = f"{len(checks)} post_check(s) passed"
    detail = "; ".join(d for d in (row.info.get("detail"), note) if d)
    return row._replace(info={**row.info, "detail": detail})


# --- fixtures derived from the examples ----------------------------------------------------
def _reset_negative_fixture(boards: Path | None = None) -> None:
    """Re-break the negative gate-test board (buck without its input bypass):
    G2 must detect it and add the bypass back, proving the review loop each run."""
    boards = boards or HERE
    src, dst = boards / NEG_GATE_SOURCE / "netlist.json", boards / NEG_GATE
    if not src.is_file():
        return
    dst.mkdir(exist_ok=True)
    d, drop = json.loads(src.read_text(encoding="utf-8")), {"C1", "C2"}
    d = {**d, "title": "NEGATIVE TEST - buck with no input decoupling", "name": NEG_GATE,
         "components": [c for c in d["components"] if c["ref"] not in drop],
         "nets": {n: [p for p in conns if p[0] not in drop] for n, conns in d["nets"].items()}}
    (dst / "netlist.json").write_text(json.dumps(d, indent=2), encoding="utf-8")


def _build_g3_fixture(folder: str = G3_FIXTURE, boards: Path | None = None) -> str:
    """negative_g3 / napkin_g3 (one netlist), from cp2102n: a USB-C FIXED
    mid-board, mouth facing the interior (G3 blocker), plus a 2x20 THT socket
    wall pinching the +3V3 plane between a fixed cap and the auto field (G3
    plane warning). Strict, the run stops AT G3: detection, cheaply, every run."""
    boards = boards or HERE
    dst = boards / folder
    dst.mkdir(exist_ok=True)
    src = json.loads((boards / G3_SOURCE / "netlist.json").read_text(encoding="utf-8"))
    d = {**src, "title": "NEGATIVE TEST - inward connector + plane pinch wall", "name": folder,
         "board": {"w": 54.0, "h": 52.0, "auto_x": 2.0, "auto_y": 2.0,
                   "auto_w": 34.0, "auto_h": 28.0},
         "components": [*({**c, **G3_MOVES.get(c["ref"], {})} for c in src["components"]),
                        G3_WALL],
         "no_connect": [*src.get("no_connect", []), *(["J9", str(i)] for i in range(1, 41))]}
    (dst / "netlist.json").write_text(json.dumps(d, indent=2), encoding="utf-8")
    return str(dst / "netlist.json")


def _no_g3_source(boards: Path) -> str | None:
    if (boards / G3_SOURCE / "netlist.json").is_file():
        return None
    return f"no {G3_SOURCE}/netlist.json in {boards} to derive it from"


def _g3_reached(run: Run) -> tuple[dict, str]:
    """(the run's G3 gate, '') or ({}, why it never got there)."""
    if run.error:
        return {}, f"crashed: {run.error}"
    g3 = _gate(run.report, "G3")
    return g3, "" if g3 else f"the run stopped before G3 (AT {_failed_gate(run.report)})"


def _g3_verdict(run: Run) -> tuple[bool, str]:
    """(met, detail): stopped AT G3 with the mouth blocker and the pinch warning?"""
    g3, why = _g3_reached(run)
    if why:
        return False, why
    blockers = " | ".join(str(b) for b in g3.get("blockers", []))
    warnings = " | ".join(str(w) for w in g3.get("warnings", []))
    if not run.report.get("PASS") and "mating face" in blockers and "plane on In2.Cu" in warnings:
        return True, "inward-mouth blocker + plane-pinch warning detected"
    return False, f"blockers=[{blockers[:120]}] warnings=[{warnings[:120]}]"


def _fixture_g3(boards: Path | None = None) -> Row:
    """Run the G3 fixture: it must fail AT G3 with both findings (row 'G3*';
    a miss shows the gate it really stopped at). Strict (napkin=False): napkin
    mode would turn the inward connector out, and this fixture proves
    DETECTION. It stops at G3, before routing: no route mode (F5-12 pins it)."""
    boards = boards or HERE
    missing = _no_g3_source(boards)
    if missing:
        return _skip(G3_FIXTURE, missing)
    run = run_netlist(Path(_build_g3_fixture(G3_FIXTURE, boards)), *G3_DIMS, napkin=False)
    return run_row(G3_FIXTURE, run, *_g3_verdict(run), "G3")


def _napkin_verdict(run: Run, netlist: Path) -> tuple[bool, str]:
    g3, why = _g3_reached(run)
    if why:
        return False, why
    mating = [b for b in g3.get("blockers", []) if "mating face" in b]
    if mating:
        return False, f"G3 still blocks: {mating[0]}"
    if run.decided is None:
        return False, "cannot count this run's G3 decisions"   # _detail names the log problem
    faced = [e for e in run.decided if e.get("stage") == "G3"
             and FACE_OUT in f"{e.get('source', '')} {e.get('question', '')}"]
    if not faced:
        return False, (f"no G3 {FACE_OUT} decision in "
                       f"{decisions.decisions_path(netlist).name} from this run")
    return True, (f"connector turned out: {len(faced)} G3 {FACE_OUT} decision(s); "
                  f"overall {_overall(run.report)}")


def _fixture_napkin_g3(boards: Path | None = None) -> Row:
    """The negative_g3 netlist in napkin mode (F5-12): G3 must end without a
    'mating face' blocker, with a G3 face_connector_out decision from this run.
    PASS = the policy worked; a later failure is in the detail and summary."""
    boards = boards or HERE
    missing = _no_g3_source(boards)
    if missing:
        return _skip(NAPKIN_G3, missing)
    netlist = Path(_build_g3_fixture(NAPKIN_G3, boards))
    run = run_netlist(netlist, *G3_DIMS, napkin=True, route=ROUTE)
    met, detail = _napkin_verdict(run, netlist)
    if not met:
        return run_row(NAPKIN_G3, run, False, detail)
    shown = "" if run.report.get("PASS") else _summary(run)
    info = _columns(run)                    # PASS: no failing gate; the run's own is overall_gate
    return Row(NAPKIN_G3, "PASS", {**info, "gate": "-", "overall_gate": info["gate"],
                                   "detail": _detail(run, detail)}, shown)


def _copy_heal_board(routed: Path, boards: Path) -> Path:
    dst = boards / G4_FIXTURE
    dst.mkdir(exist_ok=True)
    shutil.copy(routed, dst / "netlist.kicad_pcb")
    for src in (routed.with_suffix(".kicad_pro"), routed.with_suffix(".kicad_dru"),
                routed.parent / "netlist.json"):
        if src.is_file():
            shutil.copy(src, dst / f"netlist{src.suffix}")
    return dst / "netlist.kicad_pcb"


def _cut_until_broken(board: Path, kpy: str) -> tuple[dict | None, list[str], str]:
    """swig_cut_track.py (stdin closed) removes the longest non-GND track per
    call; a net with a pour (D7 4-layer default: lm358's P5V plane) stays
    connected, so cut again, at most CUT_ATTEMPTS times: (the DRC showing the
    break or None, the cuts made, why it stopped)."""
    script = rp_paths.SERVER / "tools" / "swig_cut_track.py"
    cuts: list[str] = []
    for _ in range(CUT_ATTEMPTS):
        r = tc.run([kpy, str(script), str(board)], stdin=subprocess.DEVNULL,
                   timeout=CUT_TIMEOUT_S)
        done = tc.last_json(r.stdout or "") or {}       # a crash at pcbnew teardown is fine
        if not done.get("cut_net"):
            said = _tail((r.stdout or "") + "\n" + (r.stderr or ""))[-200:]
            return None, cuts, f"{script.name} cut nothing (exit {r.returncode}: {said})"
        label = f"{done['cut_net']} {done.get('length_mm')} mm"
        drc = rp_kicad.drc(str(board))
        if drc["unconnected"] >= 1:
            return drc, [*cuts, label], ""
        cuts = [*cuts, f"{label} (still connected)"]
    return None, cuts, f"no cut broke the board after {CUT_ATTEMPTS} cuts"


def _heal(board: Path, kpy: str) -> Row:
    """Cut until broken; the healer must bring DRC to 0 / 0 with an applied fix."""
    broken, cuts, why = _cut_until_broken(board, kpy)
    if broken is None:
        return Row(G4_FIXTURE, "FAIL", {"gate": "G4*", "detail": f"{why}; cuts: "
                                        f"{', '.join(cuts) or 'none'}"})
    fixes = rp_kicad.board_fix(str(board), str(board.parent / "netlist.json"), "heal")
    applied = [f for f in fixes if not str(f).startswith(rp_kicad.OPEN)]
    healed = rp_kicad.drc(str(board))
    ok = healed["unconnected"] == 0 and healed["errors"] == 0 and bool(applied)
    detail = (f"cut {', '.join(cuts)} -> {broken['unconnected']} unconnected -> healed "
              f"({len(applied)} fix(es)) -> {healed['unconnected']}" if ok else
              f"cut {', '.join(cuts)}: unconn {broken['unconnected']} -> "
              f"{healed['unconnected']}, errors {healed['errors']}, fixes={fixes[:2]}")
    return Row(G4_FIXTURE, "PASS" if ok else "FAIL", {"gate": "G4*", "detail": detail})


def _fixture_g4_heal(routed: Path | None, boards: Path | None = None) -> Row:
    """Cut a COPY of lm358's board routed THIS run; the G4 healer must restore it
    without the router. SKIP without that board or KiCad's Python."""
    boards = boards or HERE
    if routed is None or not Path(routed).is_file():
        return _skip(G4_FIXTURE, f"{G4_SOURCE} produced no routed board this run")
    kpy = rp.k._kicad_python()
    if not kpy:
        return _skip(G4_FIXTURE, "KiCad's Python (pcbnew) not found: cannot cut a track")
    try:
        return _heal(_copy_heal_board(Path(routed), boards), kpy)
    except rp_kicad.CHILD_ERRORS as e:
        return Row(G4_FIXTURE, "FAIL", {"gate": "G4*", "detail": f"{type(e).__name__}: {e}"})


# --- the matrix ---------------------------------------------------------------------------
def _routed(rows: Sequence[Row], name: str) -> Path | None:
    row = next((r for r in rows if r.board == name), None)
    pcb = row.info.get("pcb") if row is not None and row.status == "PASS" else None
    return Path(pcb) if pcb else None


def run_matrix(boards: Path, entries: Sequence[Entry]) -> list[Row]:
    _reset_negative_fixture(boards)
    rows = [static_row(boards, *b) for b in BOARDS] + [marker_row(boards, e) for e in entries]
    return [*rows, _fixture_g3(boards), _fixture_napkin_g3(boards),
            _fixture_g4_heal(_routed(rows, G4_SOURCE), boards)]


def _cell(value: Any) -> str:
    return "-" if value is None else str(value)


def format_row(row: Row) -> str:
    m = row.info
    blocked = "" if m.get("gate") in ("-", None) else f"  AT {m['gate']}"
    fixed = f" fixed={m['remediated']}" if m.get("remediated") else ""
    detail = f"  {m['detail']}" if m.get("detail") else ""
    return (f"  {row.status:7} {row.board:24} erc={_cell(m.get('erc'))} "
            f"drc={_cell(m.get('drc_err'))} unconn={_cell(m.get('unconn'))} "
            f"scale={_cell(m.get('scale'))} decisions={_cell(m.get('decisions'))}"
            f"{fixed}{blocked}{detail}")


def print_matrix(rows: Sequence[Row]) -> int:
    print("\n=== REGRESSION MATRIX (gated) ===")
    for row in rows:
        print(format_row(row))
        for line in row.summary.splitlines():
            print(INDENT + line)
    skipped = [r.board for r in rows if r.status == "SKIP"]
    ran = len(skipped) < len(rows)
    allpass = ran and all(r.status in OK_STATUSES for r in rows)
    notes = [f"{len(skipped)} skipped: {', '.join(skipped)}"] if skipped else []
    note = "; ".join([*notes, *([] if ran else ["nothing ran"])])
    print(f"\nALL PASS: {allpass}" + (f" ({note})" if note else ""))
    print(json.dumps({"all_pass": allpass, "skipped": skipped,
                      "results": [{"board": r.board, "status": r.status, **r.info}
                                  for r in rows]}, default=str))
    return EXIT_PASS if allpass else EXIT_FAIL


def _describe(m: Marker) -> str:
    parts = [f"{m.w:g} x {m.h:g} mm" if m.w is not None and m.h is not None
             else "dims from the netlist",
             "expect PASS" if m.expect == "PASS"
             else f"expect FAIL at {m.gate} ({m.blocker_contains!r})",
             "args " + " ".join(m.args) if m.args else "",
             f"{len(m.post_checks)} post_check(s): " + ", ".join(c.script for c in m.post_checks)
             if m.post_checks else ""]
    return "; ".join(p for p in parts if p)


def print_plan(boards: Path, entries: Sequence[Entry]) -> int:
    """--list: the matrix run_all would run on `boards`; 1 when a marker is bad."""
    print(f"=== REGRESSION MATRIX PLAN ({boards}) ===")
    for folder, w, h in BOARDS:
        source = NEG_GATE_SOURCE if folder == NEG_GATE else folder
        here = (boards / source / "netlist.json").is_file()
        print(f"  board    {folder:24} {w} x {h} mm" + ("" if here else "  (absent)"))
    for e in entries:
        what = (_describe(e.marker) if e.marker is not None
                else f"BAD {MARKER}: " + "; ".join(e.problems))
        print(f"  marker   {e.name:24} {what}")
    for name, what in ((G3_FIXTURE, f"strict G3 detection, from {G3_SOURCE}"),
                       (NAPKIN_G3, f"napkin G3 {FACE_OUT} policy, from {G3_SOURCE}"),
                       (G4_FIXTURE, f"G4 heal of a cut track, from {G4_SOURCE}'s routed board")):
        print(f"  fixture  {name:24} {what}")
    return EXIT_FAIL if any(e.marker is None for e in entries) else EXIT_PASS


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else list(argv)
    if any(a in ("-h", "--help") for a in args):
        print(f"{USAGE}\n{__doc__}")
        return EXIT_PASS
    rest = [a for a in args if a != LIST_FLAG]
    if len(rest) > 1 or any(a.startswith("-") for a in rest):
        print(f"run_all: error: unexpected argument(s) {' '.join(rest)}\n{USAGE}",
              file=sys.stderr)
        return EXIT_USAGE
    boards = Path(rest[0]).resolve() if rest else HERE
    if not boards.is_dir():
        print(f"run_all: error: no boards directory {boards}\n{USAGE}", file=sys.stderr)
        return EXIT_USAGE
    entries = discover(boards)
    if LIST_FLAG in args:
        return print_plan(boards, entries)
    return print_matrix(run_matrix(boards, entries))


def _console_never_raises() -> None:
    """On a cp1252 console an unencodable character prints as '?', never crashes."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")


if __name__ == "__main__":
    _console_never_raises()
    sys.exit(main())
