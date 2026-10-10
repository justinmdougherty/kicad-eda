#!/usr/bin/env python3
"""Determinism check: run the gated pipeline N times on fresh copies of one netlist and
compare what every run produced (ROADMAP D5: N repeated runs give identical routed
results, or at minimum identical verdicts).

  <venv-python> determinism_check.py <netlist.json> --runs N [--jobs J] [--keep DIR]
                [--verdict-only] [--timeout S] [-- <extra run_pipeline args>]

A run folder holds the netlist's INPUTS only: netlist.json, its libs/, mech/ and pins/,
every existing path a netlist string names (relative to it, ${KIPRJMOD} expanded; ../x
keeps its relative place) and a project sym-lib-table / fp-lib-table with rows of its own
plus the files those rows name; GENERATED outputs are never copied. Each run is
'<python> pipeline/run_pipeline.py <copy> [extra args]' in its folder, PIPELINE_NO_CACHE=1,
stdin closed, J at a time; an extra argument naming a file is made absolute; output goes
to LOGS. Compared: FIELDS (--verdict-only: VERDICT_FIELDS), the run folder's path replaced
by '<run>'. Output: a table, the differing fields with each run's value and the
board_fingerprint diff, then ONE JSON line {"determinism_check": {..., "durations_s"}}.
Exit 0 iff all runs agree; 1 when a field differs or a run left no readable report or
board; 2 on a usage error. Run folders are temporary, or DIR/run1..runN with --keep DIR."""
from __future__ import annotations

import argparse
import fnmatch
import glob
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, NoReturn

import rp_paths  # noqa: F401  first: puts server/ and server/tools/ on sys.path
import board_fingerprint
import libtable

RUN_PIPELINE = Path(__file__).resolve().parent / "run_pipeline.py"
REPORT_SUFFIX = "_pipeline_report.json"         # = rp_context.REPORT_SUFFIX (tested)
INPUT_DIRS = ("libs", "mech", "pins")
TRANSIENT = ("__pycache__", "*.pyc", "~*.lck", "*.lck")
_IGNORE = shutil.ignore_patterns(*TRANSIENT)
GENERATED = ("{s}.kicad_sch", "{s}.kicad_pcb", "{s}.kicad_pro", "{s}.kicad_prl",
             "{s}.kicad_dru", "{s}.dsn", "{s}.ses", "{s}.kicad_pcb.*", "{s}.k9.*", "{s}.agent.*",
             "{s}.decisions.jsonl*", "{s}.cache.json", "{s}.outline.json", "{s}.stackup.json",
             "{s}_pipeline_report.json", "{s}_review.md", "{s}_report.md", "{s}_drc.json",
             "{s}_erc.json", "{s}_route_handoff.json", "{s}_fab_notes.md", "{s}_stackup.csv",
             "{s}_ipc2581.xml", "{s}_*.png", "{s}_*.zip", "{s}*.rpt", "{s}_gerbers", "{s}_mech")
VERDICT_FIELDS = ("exit_code", "gate_summary", "remediation")
AUTOROUTE_KEYS = ("pack_scale", "dsn_sha256", "dsn_seed", "alternates")    # F5-11
FIELDS = ("exit_code", "PASS", "gate_summary", "remediation", *AUTOROUTE_KEYS,
          "dsn_file_sha256", "ses_file_sha256", "board_fingerprint")
DEFAULT_TIMEOUT_S = 3600.0
TIMEOUT_RC = 124                                  # a run killed at --timeout
LOGS = ("determinism_run.stdout.txt", "determinism_run.stderr.txt")
PLACEHOLDER = "<run>"
_KIPRJMOD = ("${KIPRJMOD}", "$(KIPRJMOD)")
_NOT_A_PATH = frozenset('<>|?*":\n\r\t')
RunResult = subprocess.CompletedProcess          # what a runner returns (args, rc, out, err)
Runner = Callable[[list[str], Path, dict[str, str], float], RunResult]


class UsageError(ValueError):
    """A command line or netlist the check cannot start from (exit 2)."""


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise UsageError(message)


def _norm(path: Path) -> Path:
    return Path(os.path.normpath(os.path.abspath(path)))


# --- the copy step ---------------------------------------------------------------------------
def _copyable(path: Path, src: Path, stem: str) -> bool:
    """Not the netlist folder or one above it, and nothing generated or transient."""
    if path == src or path in src.parents:
        return False
    names = path.relative_to(src).parts if src in path.parents else (path.name,)
    patterns = [p.format(s=glob.escape(stem)) for p in GENERATED] + list(TRANSIENT)
    return not any(fnmatch.fnmatchcase(n, p) for n in names for p in patterns)


def _reference(text: str, src: Path) -> Path | None:
    """The existing path a netlist string names relative to src (${KIPRJMOD} = src),
    or None (absolute paths resolve from anywhere, so they are never copied)."""
    var = next((v for v in _KIPRJMOD if text.startswith(v)), "")
    text = (text[len(var):].lstrip("/\\") or ".") if var else text
    if not text or len(text) > 260 or _NOT_A_PATH & set(text) or text[0] in "/\\":
        return None
    path = _norm(src / text)
    try:
        return path if path.exists() else None
    except OSError:
        return None


def _strings(value: Any) -> Iterator[str]:
    """Every string of a JSON value, object keys included."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, (dict, list)):
        for item in (value if isinstance(value, list) else [*value, *value.values()]):
            yield from _strings(item)


def _table_inputs(src: Path) -> list[Path]:
    """Project library tables with rows of their own, and the files those rows name;
    a table that cannot be parsed is copied as it is (every run then sees it)."""
    out: list[Path] = []
    for path in (src / name for name in libtable.TABLE_FILES.values()):
        try:
            own = [r.uri for r in libtable.read_table(path).rows if not r.managed] \
                if path.is_file() else []
        except libtable.LibTableError:
            own = [""]                          # unparsable: copied, names nothing
        out += [path, *filter(None, (_reference(uri, src) for uri in own))] if own else []
    return out


def input_items(netlist: Path) -> list[Path]:
    """What a run copies besides the netlist (see the module doc), outermost only."""
    src, stem = netlist.parent, netlist.stem
    try:
        design = json.loads(netlist.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise UsageError(f"cannot read the netlist {netlist}: {e}") from e
    found = [src / d for d in INPUT_DIRS if (src / d).is_dir()]
    found += [p for p in (_reference(s, src) for s in _strings(design)) if p is not None]
    unique = sorted({p for p in found + _table_inputs(src)
                     if p != netlist and _copyable(p, src, stem)})
    return [p for p in unique if not any(q in p.parents for q in unique)]


def copy_inputs(netlist: Path, run_root: Path) -> Path:
    """Copy the netlist and its inputs into run_root, keeping their relative places;
    returns the copied netlist."""
    netlist = _norm(netlist)
    src = netlist.parent
    items = input_items(netlist)
    base = Path(os.path.commonpath([src, *items])) if items else src
    for item in items:
        dst = run_root / item.relative_to(base)
        dst.parent.mkdir(parents=True, exist_ok=True)
        if item.is_dir():
            shutil.copytree(item, dst, ignore=_IGNORE, dirs_exist_ok=True)
        else:
            shutil.copy2(item, dst)
    target = run_root / src.relative_to(base) / netlist.name
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(netlist, target)
    return target


# --- running ---------------------------------------------------------------------------------
def _kill_tree(proc: subprocess.Popen) -> None:
    """Kill the run and its children (Freerouting's Java, kicad-cli, KiCad python)."""
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True,
                           stdin=subprocess.DEVNULL, timeout=60, check=False)   # output unused
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError) as e:
        print(f"determinism_check: could not kill process tree {proc.pid}: {e}", file=sys.stderr)
    proc.kill()


def run_command(cmd: list[str], cwd: Path, env: dict[str, str], timeout: float) -> RunResult:
    """The real runner: stdin closed, UTF-8 output; at the timeout the whole process tree
    is killed and the return code is TIMEOUT_RC."""
    extra: dict[str, Any] = {} if os.name == "nt" else {"start_new_session": True}
    with subprocess.Popen(cmd, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding="utf-8",
                          errors="replace", **extra) as proc:
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_tree(proc)
            try:
                out, err = proc.communicate(timeout=60)
            except subprocess.TimeoutExpired:
                out, err = "", ""
            note = f"determinism_check: timed out after {timeout:g} s; process tree killed"
            return RunResult(cmd, TIMEOUT_RC, out, f"{err}\n{note}")
    return RunResult(cmd, proc.returncode, out, err)


def _sha_file(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def _read_report(path: Path) -> tuple[dict, str | None]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}, f"no pipeline report ({path.name})"
    except (OSError, ValueError) as e:
        return {}, f"unreadable pipeline report {path.name}: {e}"
    return (data, None) if isinstance(data, dict) else ({}, f"{path.name} is not a JSON object")


def _board(path: Path) -> tuple[str | None, str | None]:
    if not path.is_file():
        return None, None                       # the run stopped before a board existed
    try:
        return board_fingerprint.fingerprint(path)["sha256"], None
    except (OSError, ValueError, RuntimeError) as e:
        return None, f"board_fingerprint failed on {path.name}: {e}"


def _scrub(fields: dict[str, Any], root: Path) -> dict[str, Any]:
    """fields with the run folder's path (as given, resolved, / or \\, repr) -> PLACEHOLDER."""
    forms = {str(root), root.as_posix(), str(root.resolve()), root.resolve().as_posix()}
    text = json.dumps(fields)
    for form in sorted(forms | {f.replace("\\", "\\\\") for f in forms}, key=len, reverse=True):
        text = text.replace(json.dumps(form)[1:-1], PLACEHOLDER)
    return json.loads(text)


def collect(netlist: Path, run_root: Path, returncode: int) -> dict[str, Any]:
    """One run's compared fields (run paths scrubbed) plus its board and any error."""
    folder, stem = netlist.parent, netlist.stem
    report, report_error = _read_report(folder / f"{stem}{REPORT_SUFFIX}")
    auto = report.get("autoroute") if isinstance(report.get("autoroute"), dict) else {}
    board = folder / f"{stem}.kicad_pcb"
    fingerprint, board_error = _board(board)
    fields = {"exit_code": returncode, "PASS": report.get("PASS"),
              "gate_summary": report.get("gate_summary"),
              "remediation": report.get("remediation", []),
              **{key: auto.get(key) for key in AUTOROUTE_KEYS},
              "dsn_file_sha256": _sha_file(folder / f"{stem}.dsn"),
              "ses_file_sha256": _sha_file(folder / f"{stem}.ses"),
              "board_fingerprint": fingerprint}
    errors = [e for e in (report_error, board_error) if e]
    return {**_scrub(fields, run_root), "board": str(board) if fingerprint else None,
            **({"error": "; ".join(errors)} if errors else {})}


def run_one(i: int, run_root: Path, netlist: Path, opts: argparse.Namespace, runner: Runner,
            env: dict[str, str]) -> dict[str, Any]:
    cmd = [sys.executable, str(RUN_PIPELINE), str(netlist), *opts.extra]
    started = time.monotonic()
    try:
        result = runner(cmd, netlist.parent, env, opts.timeout)
    except OSError as e:
        result = RunResult(cmd, -1, "", f"determinism_check: cannot start the run: {e}")
    seconds = round(time.monotonic() - started, 1)
    for name, text in zip(LOGS, (result.stdout, result.stderr), strict=True):
        (netlist.parent / name).write_text(text or "", encoding="utf-8")
    return {"run": i, "seconds": seconds, **collect(netlist, run_root, result.returncode)}


def run_all(copies: list[tuple[Path, Path]], opts: argparse.Namespace,
            runner: Runner) -> list[dict]:
    env = {**os.environ, "PIPELINE_NO_CACHE": "1", "PYTHONIOENCODING": "utf-8"}
    with ThreadPoolExecutor(max_workers=opts.jobs) as pool:
        futures = [pool.submit(run_one, i, root, netlist, opts, runner, env)
                   for i, (root, netlist) in enumerate(copies, start=1)]
        return [f.result() for f in futures]


# --- comparing and reporting -----------------------------------------------------------------
def _canon(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str)


def differing_fields(records: list[dict], fields: tuple[str, ...]) -> list[str]:
    return [f for f in fields if len({_canon(r.get(f)) for r in records}) > 1]


def _cell(value: Any) -> str:
    text = "-" if value is None else value if isinstance(value, str) else _canon(value)
    if len(text) == 64 and all(c in "0123456789abcdef" for c in text):
        return text[:12]                        # a sha256
    short = len(text) <= 14 and "\n" not in text
    return text if short else "#" + hashlib.sha256(_canon(value).encode()).hexdigest()[:8]


def render_table(records: list[dict], fields: tuple[str, ...], differing: list[str]) -> list[str]:
    rows = [["field", "same", *(f"run{r['run']}" for r in records)]]
    rows += [[f, "NO" if f in differing else "yes", *(_cell(r.get(f)) for r in records)]
             for f in fields]
    rows.append(["seconds", "", *(str(r["seconds"]) for r in records)])
    widths = [max(len(row[c]) for row in rows) for c in range(len(rows[0]))]
    return ["  ".join(c.ljust(w) for c, w in zip(row, widths, strict=True)).rstrip()
            for row in rows]


def explain(records: list[dict], differing: list[str], problems: list[str]) -> list[str]:
    """The differing fields with each distinct value and its runs (run 1's first), under a
    differing fingerprint the board diff of run 1 (A) against that run (B); the problems."""
    lines = [f"DIFFER: {', '.join(differing)}"] if differing else []
    a = records[0].get("board")
    for field in differing:
        groups: dict[str, list[int]] = {}
        for r in records:
            groups.setdefault(_canon(r.get(field)), []).append(r["run"])
        for runs in groups.values():
            value, b = records[runs[0] - 1].get(field), records[runs[0] - 1].get("board")
            text = value if isinstance(value, str) else _canon(value)
            lines.append(f"  {field} runs {', '.join(map(str, runs))}: {text[:400]}")
            if field == "board_fingerprint" and runs[0] != 1 and a and b:
                lines += [f"    board diff run1 (A) -> run{runs[0]} (B):",
                          *(f"      {d}" for d in board_fingerprint.diff(a, b))]
    return lines + (["PROBLEMS:", *(f"  {p}" for p in problems)] if problems else [])


# --- command line ----------------------------------------------------------------------------
def parse_args(argv: list[str]) -> argparse.Namespace:
    """The options: netlist and keep absolute, extra = the args after '--' (one naming an
    existing file made absolute: the runs work in other folders)."""
    cut = argv.index("--") if "--" in argv else len(argv)
    ap = _Parser(prog="determinism_check.py", description=__doc__.split("\n\n")[0])
    ap.add_argument("netlist")
    ap.add_argument("--runs", type=int, required=True)
    ap.add_argument("--jobs", type=int, default=1)
    ap.add_argument("--keep")
    ap.add_argument("--verdict-only", action="store_true")
    ap.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S)
    ns = ap.parse_args(argv[:cut])
    if not Path(ns.netlist).is_file():
        raise UsageError(f"no netlist at {ns.netlist}")
    if ns.runs < 2:
        raise UsageError("--runs must be at least 2 (one run has nothing to agree with)")
    if ns.jobs < 1 or not ns.timeout > 0:
        raise UsageError("--jobs must be at least 1 and --timeout positive")
    extra = tuple(str(_norm(Path(a))) if not a.startswith("-") and Path(a).is_file() else a
                  for a in argv[cut + 1:])
    return argparse.Namespace(**{**vars(ns), "netlist": _norm(Path(ns.netlist)), "extra": extra,
                                 "keep": _norm(Path(ns.keep)) if ns.keep else None})


def runs_root(opts: argparse.Namespace) -> tuple[Path, bool]:
    """(the folder holding run1..runN, whether it is temporary)."""
    if opts.keep is None:
        return Path(tempfile.mkdtemp(prefix="kicad_det_")), True
    for used in (opts.keep / f"run{i}" for i in range(1, opts.runs + 1)):
        if used.exists() and (not used.is_dir() or any(used.iterdir())):
            raise UsageError(f"--keep: {used} exists and is not empty (give a new folder)")
    opts.keep.mkdir(parents=True, exist_ok=True)
    return opts.keep, False


def check(opts: argparse.Namespace, runner: Runner) -> int:
    root, temporary = runs_root(opts)
    fields = VERDICT_FIELDS if opts.verdict_only else FIELDS
    try:
        copies = [(root / f"run{i}", copy_inputs(opts.netlist, root / f"run{i}"))
                  for i in range(1, opts.runs + 1)]
        started = time.monotonic()
        records = run_all(copies, opts, runner)
        total = round(time.monotonic() - started, 1)
        differing = differing_fields(records, fields)
        problems = [f"run {r['run']}: {r['error']}" for r in records if r.get("error")]
        lines = render_table(records, fields, differing) + explain(records, differing, problems)
    finally:
        try:
            if temporary:
                shutil.rmtree(root)
        except OSError as e:                    # reported, never fatal: the verdict stands
            print(f"determinism_check: could not remove {root}: {e}", file=sys.stderr)
    summary = {"netlist": str(opts.netlist), "runs": opts.runs, "jobs": opts.jobs,
               "verdict_only": opts.verdict_only, "fields": list(fields),
               "agree": not differing and not problems, "differing": differing,
               "problems": problems, "exit_codes": [r["exit_code"] for r in records],
               "durations_s": [r["seconds"] for r in records], "total_s": total,
               "kept": None if temporary else str(root)}
    print("\n".join(lines))
    print(json.dumps({"determinism_check": summary}))
    return 0 if summary["agree"] else 1


def main(argv: list[str] | None = None, runner: Runner | None = None) -> int:
    try:
        return check(parse_args(sys.argv[1:] if argv is None else argv), runner or run_command)
    except UsageError as e:
        print(f"determinism_check: error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
