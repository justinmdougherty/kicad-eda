"""kicad-cli DRC that never reports a stale or half-run check, and the rules
canary that proves a custom rules file compiled (ADR D7.8, R23).

Why (ADR D7.1 'Broken rules files'): kicad-cli writes `<stem>_drc.json` only
when the check ran, so a run that wrote nothing used to be judged on the
previous round's report; KiCad 9.0.4 and 10.0.5 drop a `.kicad_dru` that does
not compile WITHOUT a word (rc 0, the board checked as if the file were
absent), 10.99 exits 3 with no report, and a bare `intersectsArea(...)`
crashes 9.0.4 with 0xC0000374 (heap corruption) and no report.

  run_drc(pcb, cli, *, severity, timeout)   DRC on a saved board. Deletes
      <stem>_drc.json first, so the report read afterwards is this run's.
      Success: {errors, unconnected, types, by_severity, report, kicad_cli,
      rc: 0, crash: False} -- errors = every violation in the report (the
      JSON holds only the requested severity on 9.0.4 and 10.99), types =
      {type: count}, by_severity = {severity: count}. Failure (the board is
      missing, no kicad-cli, the stale report cannot be deleted, rc != 0, a
      crash, a timeout, no or an unparseable report, or an `Error:` line in
      the output): {error, rc, crash, report, kicad_cli, output} with
      rc None when kicad-cli did not finish, crash per is_crash(rc), output =
      the stdout + stderr tail, plus timeout: True for a timeout. A failure
      never carries `errors`: callers (toolchain.drc_counts, rp_kicad.drc)
      map it to their own error shape.
  rules_compile(dru_text, cli)              the canary proof. The shipped
      canary board (server/data/drc_canary.*: one 0.2 mm F.Cu track on net
      __KICAD_EDA_CANARY__) is copied to a temp dir with `dru_text`
      (+ CANARY_RULE when absent) and checked with --severity-all; ok iff
      rc 0, a fresh report and >= 1 track_width violation on the canary net.
      Rule names are never matched: 9.0.4 reports a custom track_width hit
      as 'board setup constraints'. Returns {ok, rc, crash, fired,
      canary_added, types, kicad_cli} plus `reason` when not ok, and the
      run's `error` / `output` when DRC itself failed.

Expected signatures: valid text -> ok on 9.0.4, 10.0.5 and 10.99; broken text
-> ok False with rc 0 and fired 0 on 9.0.4 / 10.0.5 (silent drop) and an
error with rc 3 on 10.99; a bare intersectsArea -> crash True on 9.0.4.

Stdlib only and Python 3.11 compatible (KiCad 9's bundled Python may import
it). Every kicad-cli child runs with stdin closed, UTF-8 decoding with
replacement and a timeout.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any, Callable

DATA_DIR = Path(__file__).resolve().parents[1] / "data"
CANARY_BOARD = DATA_DIR / "drc_canary.kicad_pcb"
CANARY_PROJECT = DATA_DIR / "drc_canary.kicad_pro"
CANARY_NET = "__KICAD_EDA_CANARY__"
CANARY_RULE = (f'(rule "__kicad_eda_canary" (condition "A.NetName == \'{CANARY_NET}\'") '
               "(constraint track_width (min 10mm)))")
CANARY_TYPE = "track_width"
DRU_HEADER = "(version 1)"
DRU_SUFFIX = ".kicad_dru"
PROJECT_SUFFIX = ".kicad_pro"
REPORT_SUFFIX = "_drc.json"
DRC_TIMEOUT_S = 600                      # toolchain.KICAD_CLI_TIMEOUT_S
CANARY_TIMEOUT_S = 120                   # a 10 x 10 mm board: ~1 s on 9.0.4, ~5-15 s on 10.99
OUTPUT_TAIL = 600                        # chars of kicad-cli output kept in a failure
SEVERITY_FLAGS = {"error": "--severity-error", "warning": "--severity-warning",
                  "all": "--severity-all"}
HEAP_CORRUPTION = 0xC0000374             # 9.0.4 on a bare intersectsArea (STATUS_HEAP_CORRUPTION)
CRASH_CODES = frozenset({HEAP_CORRUPTION, HEAP_CORRUPTION - (1 << 32)})  # unsigned / signed
MAX_EXIT_CODE = 255                      # above: a Windows NTSTATUS, i.e. the process died
RULES_NOT_COMPILED = "could not compile custom design rules"
_ERROR_LINE = re.compile(r"(^|\s)Error:", re.M)   # wx.LogStderr may prefix a timestamp

Runner = Callable[[list[str], float], subprocess.CompletedProcess]


def report_path(pcb: str | Path) -> Path:
    """<stem>_drc.json beside the board: where run_drc writes the report."""
    board = Path(pcb)
    return board.with_name(board.stem + REPORT_SUFFIX)


def is_crash(rc: int | None) -> bool:
    """True when an exit code means kicad-cli died rather than exited:
    0xC0000374 (as unsigned or signed), a POSIX signal (< 0) or a Windows
    NTSTATUS (> 255). None (it never finished) and ordinary codes are not."""
    if rc is None:
        return False
    return rc in CRASH_CODES or rc < 0 or rc > MAX_EXIT_CODE


def default_runner(argv: list[str], timeout: float) -> subprocess.CompletedProcess:
    """subprocess.run for a kicad-cli child: stdin closed (an inherited MCP
    stdio pipe would block it), output captured as UTF-8 with replacement,
    and a timeout."""
    return subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)


def drc_argv(pcb: str, cli: str, severity: str, report: str) -> list[str]:
    """The kicad-cli pcb drc command line (JSON report at `report`)."""
    if severity not in SEVERITY_FLAGS:
        raise ValueError(f"severity must be one of {sorted(SEVERITY_FLAGS)}, not {severity!r}")
    return [cli, "pcb", "drc", "--format", "json", SEVERITY_FLAGS[severity], "-o", report, pcb]


# ----------------------------------------------------------------------------
# run_drc
# ----------------------------------------------------------------------------
def run_drc(pcb: str, cli: str | None, *, severity: str = "error",
            timeout: float = DRC_TIMEOUT_S, runner: Runner | None = None) -> dict:
    """Run kicad-cli DRC on `pcb` and summarise this run's report (shape in
    the module docstring). Raises ValueError only for an unknown severity;
    every runtime problem is returned as {error, rc, crash, ...}."""
    return _drc(pcb, cli, severity, timeout, runner or default_runner)[0]


def _drc(pcb: str, cli: str | None, severity: str, timeout: float,
         runner: Runner) -> tuple[dict, dict | None]:
    """(run_drc's result, the parsed report or None on failure)."""
    rpt = report_path(pcb)
    argv = drc_argv(str(pcb), str(cli or ""), severity, str(rpt))
    base = {"report": str(rpt), "kicad_cli": cli}
    refused = _precheck(pcb, cli, rpt)
    if refused:
        return _failure(base, refused, None, ""), None
    try:
        proc = runner(argv, timeout)
    except subprocess.TimeoutExpired as exc:
        out = _text(exc.output) + _text(exc.stderr)
        msg = f"kicad-cli pcb drc did not finish within {timeout:g} s"
        return {**_failure(base, msg, None, out), "timeout": True}, None
    except OSError as exc:
        return _failure(base, f"kicad-cli could not be started ({exc})", None, ""), None
    return _judge(base, rpt, proc.returncode, (proc.stdout or "") + (proc.stderr or ""))


def _precheck(pcb: str, cli: str | None, rpt: Path) -> str | None:
    """Why DRC must not run at all, or None. Deletes the stale report."""
    if not cli:
        return "kicad-cli not found (install KiCad or set KICAD_PATH)"
    if not Path(pcb).is_file():
        return f"board file not found: {pcb}"
    try:
        rpt.unlink(missing_ok=True)
    except OSError as exc:
        return (f"the stale DRC report {rpt} cannot be deleted ({exc}); a fresh report "
                "could not be told apart from it")
    return None


def _judge(base: dict, rpt: Path, rc: int, out: str) -> tuple[dict, dict | None]:
    """Turn a finished kicad-cli run into run_drc's result."""
    if is_crash(rc):
        msg = (f"kicad-cli crashed (exit {rc} = {_hex(rc)}"
               f"{', heap corruption' if rc in CRASH_CODES else ''}); DRC did not run")
        return _failure(base, msg, rc, out), None
    if rc != 0:
        return _failure(base, f"kicad-cli pcb drc exited {rc}", rc, out), None
    report, why = _read_report(rpt)
    if report is None:
        return _failure(base, why, rc, out), None
    lines = [ln.strip() for ln in out.splitlines() if _ERROR_LINE.search(ln)]
    if lines:
        return _failure(base, "kicad-cli reported " + "; ".join(lines[:3]), rc, out), None
    return {**_counts(report), **base, "rc": 0, "crash": False}, report


def _read_report(rpt: Path) -> tuple[dict | None, str]:
    """(the parsed report, '') or (None, why it is unusable)."""
    try:
        report = json.loads(rpt.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, f"kicad-cli exited 0 but wrote no DRC report {rpt.name}"
    except (OSError, ValueError) as exc:
        return None, f"DRC report {rpt.name} unreadable ({exc})"
    if not isinstance(report, dict):
        return None, f"DRC report {rpt.name} is not a JSON object"
    for key in ("violations", "unconnected_items"):
        entries = report.get(key, [])
        if not isinstance(entries, list) or not all(isinstance(e, dict) for e in entries):
            return None, f"DRC report {rpt.name}: '{key}' is not a list of objects"
    return report, ""


def _counts(report: dict) -> dict:
    violations = report.get("violations", [])
    return {"errors": len(violations),
            "unconnected": len(report.get("unconnected_items", [])),
            "types": dict(Counter(str(v.get("type", "unknown")) for v in violations)),
            "by_severity": dict(Counter(str(v.get("severity", "unknown")) for v in violations))}


def _failure(base: dict, msg: str, rc: int | None, out: str) -> dict:
    tail = out[-OUTPUT_TAIL:]
    hint = (f" (the {DRU_SUFFIX} beside the board does not compile; check it with "
            "drc_guard.rules_compile)") if RULES_NOT_COMPILED in out else ""
    detail = f": {tail.strip()}" if tail.strip() else ""
    return {"error": f"{msg}{hint}{detail}", "rc": rc, "crash": is_crash(rc), **base,
            "output": tail}


def _hex(rc: int) -> str:
    return f"0x{rc & 0xFFFFFFFF:08X}"


def _text(data: Any) -> str:
    if isinstance(data, bytes):
        return data.decode("utf-8", errors="replace")
    return data or ""


# ----------------------------------------------------------------------------
# rules_compile: the canary
# ----------------------------------------------------------------------------
def with_canary(dru_text: str) -> tuple[str, bool]:
    """(the text to compile, whether CANARY_RULE was added). A text with no
    rule at all (blank or comments only) becomes the version header plus the
    canary; any other text gets the canary appended when it lacks it."""
    if CANARY_RULE in dru_text:
        return dru_text, False
    content = [ln for ln in dru_text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    if not content:
        return f"{DRU_HEADER}\n{CANARY_RULE}\n", True
    return f"{dru_text.rstrip()}\n{CANARY_RULE}\n", True


def canary_hits(report: dict) -> int:
    """track_width violations with an item on the canary net (the item text
    names the net, e.g. 'Track [__KICAD_EDA_CANARY__] on F.Cu')."""
    tag = f"[{CANARY_NET}]"
    hits = 0
    for violation in report.get("violations", []):
        items = violation.get("items")
        if violation.get("type") != CANARY_TYPE or not isinstance(items, list):
            continue
        if any(isinstance(i, dict) and tag in str(i.get("description", "")) for i in items):
            hits += 1
    return hits


def _stage_canary(tmp: Path, text: str) -> tuple[Path | None, str]:
    """Copy the canary board + project into `tmp` with `text` as their rules
    file: (the board path, '') or (None, why the shipped canary cannot be
    copied)."""
    pcb = tmp / CANARY_BOARD.name
    try:
        shutil.copyfile(CANARY_BOARD, pcb)
        shutil.copyfile(CANARY_PROJECT, pcb.with_suffix(PROJECT_SUFFIX))
        pcb.with_suffix(DRU_SUFFIX).write_text(text, encoding="utf-8")
    except OSError as exc:
        return None, (f"the canary board {CANARY_BOARD.name} / {CANARY_PROJECT.name} cannot be "
                      f"staged ({exc}); reinstall the plugin")
    return pcb, ""


def rules_compile(dru_text: str, cli: str | None, *, timeout: float = CANARY_TIMEOUT_S,
                  runner: Runner | None = None) -> dict:
    """Prove `dru_text` compiles under `cli` (shape in the module docstring).
    The shipped canary files are never written; the temp dir is removed."""
    text, added = with_canary(dru_text)
    with tempfile.TemporaryDirectory(prefix="kicad_eda_canary_",
                                     ignore_cleanup_errors=True) as tmp:
        pcb, why = _stage_canary(Path(tmp), text)
        if pcb is None:
            res, report = _failure({"report": None, "kicad_cli": cli}, why, None, ""), None
        else:
            res, report = _drc(str(pcb), cli, "all", timeout, runner or default_runner)
    out = {"rc": res["rc"], "crash": res["crash"], "canary_added": added, "kicad_cli": cli}
    if report is None:
        return {"ok": False, **out, "fired": 0, "types": {}, "reason": res["error"],
                "error": res["error"], "output": res["output"]}
    fired = canary_hits(report)
    verdict = {"ok": fired > 0, **out, "fired": fired, "types": res["types"]}
    if fired:
        return verdict
    return {**verdict, "reason": (
        f"kicad-cli exited 0 but the canary rule did not fire: the rules file was dropped "
        f"silently (KiCad 9.0/10.0 ignore a whole {DRU_SUFFIX} that does not compile; "
        "check its syntax, layer names and that every area function is A./B.-qualified)")}
