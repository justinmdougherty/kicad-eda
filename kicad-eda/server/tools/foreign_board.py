#!/usr/bin/env python3
"""The import steps of foreign_import.py (ROADMAP_DECISIONS D0/D4, F6): one
kicad-cli import (``pcb import`` / ``sch import``), the SWIG LoadBoard child
(swig_load_foreign.py), and the board step built on them.

Board backend (``import_board``):
  * "auto": ``pcb import`` on the newest kicad-cli that has it (10.0+ / 11).
    The SWIG importer runs only when no kicad-cli can import the board or the
    CLI step failed -- and never when a layer map was requested: the SWIG
    importer takes none, so the failed CLI step stays, with the error
    ``LAYER_MAP_NOT_APPLIED``.
  * "cli": ``pcb import`` only. "swig": swig_load_foreign.py only.
  * Both board importers read a COPY of the board in a temp dir (KiCad
    10.0.5 `pcb import` and pcbnew LoadBoard of an Eagle board write
    ``<stem>.kicad_dru`` beside their input -- an ``Error:`` line on a
    read-only source); what they wrote there is ``importer_side_files``.
  * The SWIG importer runs on the newest install whose pcbnew Python runs
    the plugin: ``ForeignFile.swig_min_major`` (Eagle needs pcbnew 10+;
    ``LoadBoard(EAGLE)`` returns None on KiCad 9).
  * The script's diagnostics (F46-03a): ``inner_plane_loss`` (pcbnew 9 moved
    the Altium internal planes to Eco1.User, or a loss could not be ruled
    out) is a BLOCKER on an automatic fallback -- the step fails, its lossy
    board is set aside as ``<stem>.inner_plane_loss.kicad_pcb`` and a human
    step asks for an import with KiCad 10+ -- and an ERROR (status degraded)
    when ``board_backend="swig"`` asked for that importer.
    ``zones_on_disabled_layers`` and ``diagnostics_errors`` are warnings.
  * ``importer_footprint_library``: the ``<stem>-import-fps.pretty`` the
    board importer wrote (the KiCad 11 ``pcb import`` does; KiCad 10.0 and
    the SWIG importer write none).

rc=0 and an importer's JSON report are not proof of success (D0): every
console line is scanned for ``Error:``, outputs are checked on disk and the
report's counts are compared with the files. Every child runs through
``foreign_formats.run_child`` (stdin detached). Venv-side only.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Sequence

import foreign_formats as ff
import kicad_env as env
from foreign_formats import BOARD, ForeignFile, ImportCapabilities, first_with, scan_console
from toolchain import last_json

TOOLS_DIR = Path(__file__).resolve().parent
SWIG_SCRIPT = TOOLS_DIR / "swig_load_foreign.py"
IMPORT_TIMEOUT_S = 600             # one import or conversion child
HEADER_BYTES = 1024
OK, DEGRADED, INCOMPLETE, FAILED = "ok", "degraded", "incomplete", "failed"
SWIG_STEP = "swig LoadBoard"
LAYER_MAP_NOT_APPLIED = "layer map not applied: the SWIG importer takes none"
IMPORT_FPS_SUFFIX = "-import-fps.pretty"       # the KiCad 11 importer's footprint library
LOSSY_SUFFIX = ".inner_plane_loss.kicad_pcb"   # a board the plane-loss blocker set aside
LOG_DIR = "import_logs"

_FOOTPRINT_RE = re.compile(r'^\s*\(footprint\s+"', re.M)
_LIB_ID_RE = re.compile(r'\(lib_id\s+"')
_SWIG_COUNTS = ("footprints", "tracks", "vias", "nets", "zones", "copper_layers")


# ----------------------------------------------------------------------------
# Files and logs
# ----------------------------------------------------------------------------
def write_log(path: Path, argv: Sequence[str], r: env.ProbeResult) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"$ {subprocess.list2cmdline(list(argv))}\nexit code: {r.returncode}\n"
                    f"--- stdout ---\n{r.stdout}\n--- stderr ---\n{r.stderr}\n", encoding="utf-8")
    return path


def read_text(path: Path, limit: int | None = None) -> str:
    """The file's text ("" when it cannot be read)."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read(limit) if limit else f.read()
    except OSError:
        return ""


def output_problem(path: Path, tag: str) -> str | None:
    if not path.is_file() or path.stat().st_size == 0:
        return f"{path.name} was not written"
    if not read_text(path, HEADER_BYTES).lstrip().startswith(f"({tag}"):
        return f"{path.name} is not a ({tag} ...) file"
    return None


def _listing(directory: Path) -> set[str]:
    return {p.name for p in directory.iterdir() if p.name != LOG_DIR} \
        if directory.is_dir() else set()


def tail(text: str, n: int = 300) -> str:
    text = text.strip()
    return f": {text[-n:]}" if text else ""


# ----------------------------------------------------------------------------
# One import step (a kicad-cli import, or the SWIG LoadBoard child)
# ----------------------------------------------------------------------------
def new_step(name: str, src: ForeignFile, role: str, tool: str, argv: Sequence[str],
             r: env.ProbeResult, seconds: float, log: Path,
             scan: ff.ConsoleScan | None = None) -> dict:
    """A step record; `scan` defaults to scanning the child's whole console."""
    scan = scan or scan_console(r.stdout, r.stderr)
    return {"step": name, "input": str(src.path), "role": role, "output": None, "tool": tool,
            "argv": list(argv), "rc": r.returncode, "seconds": round(seconds, 2),
            "status": OK, "errors": [], "warnings": list(scan.warnings),
            "console_errors": list(scan.errors), "console_warnings": list(scan.warnings),
            "report": None, "statistics": {}, "created": [], "log": str(write_log(log, argv, r))}


def finish(step: dict, fail: Sequence[str], errors: Sequence[str] = ()) -> dict:
    """Failures (no usable output) fail the step; importer-reported and console
    errors degrade it."""
    step["errors"] = [*fail, *errors, *step["console_errors"]]
    step["status"] = FAILED if fail else DEGRADED if step["errors"] else OK
    return step


def _cross_check(step: dict, dst: Path) -> None:
    """Compare the report's footprint / symbol count with the written files;
    without a report the files' count is the statistic."""
    if step["role"] == BOARD:
        key, files = "footprints", [dst]
        found = len(_FOOTPRINT_RE.findall(read_text(dst)))
    else:
        key = "symbols"
        files = [Path(p) for p in step["created"] if p.endswith(".kicad_sch")] or [dst]
        found = sum(len(_LIB_ID_RE.findall(read_text(p))) for p in files)
    claimed = step["statistics"].get(key)
    if claimed is None:
        step["statistics"][key] = found
    elif int(claimed) != found:
        step["warnings"].append(f"report says {claimed} {key} but "
                                f"{', '.join(p.name for p in files)} has {found}")


def _read_report(step: dict, caps: ImportCapabilities, has_report: bool,
                 report_file: Path) -> list[str]:
    """Load the importer's JSON report into the step; its errors."""
    if not has_report:
        step["warnings"].append(f"{caps.tool} has no --report-format: counts come from the files")
    elif report_file.is_file():
        try:
            step["report"] = json.loads(report_file.read_text(encoding="utf-8"))
        except ValueError as e:
            step["warnings"].append(f"import report {report_file.name} is not JSON ({e})")
    else:
        step["warnings"].append(f"the importer wrote no report ({report_file.name})")
    if not isinstance(step["report"], dict):
        return []
    step["warnings"] += [str(w) for w in step["report"].get("warnings") or []]
    step["statistics"] = dict(step["report"].get("statistics") or {})
    return [str(e) for e in step["report"].get("errors") or []]


def _stage(src: Path, tmp: Path) -> tuple[Path, str | None]:
    """(path the importer reads, warning): a copy of `src` in `tmp`, since the
    board importers write side files beside their input (KiCad 10.0.5 `pcb
    import` of an Eagle board writes <stem>.kicad_dru there; on a read-only
    source dir it logs "Error: can't open file ... (error 30)")."""
    copy = tmp / src.name
    try:
        shutil.copyfile(src, copy)
    except OSError as e:
        return src, (f"could not stage a copy of {src.name} ({e}): the importer read the "
                     f"original and may have written side files beside it")
    return copy, None


def cli_import(caps: ImportCapabilities, src: ForeignFile, role: str, dst: Path,
               logs: Path, layer_map: str = "") -> dict:
    """`kicad-cli pcb import` (of a staged copy of the board) / `sch import`
    of `src` into `dst`."""
    board = role == BOARD
    words = ("pcb", "import") if board else ("sch", "import")
    tag = f"{words[0]}-{src.path.name}"
    report_file = logs / f"{tag}.json"
    has_report = caps.pcb_report_json if board else caps.sch_report_json
    argv = [str(caps.kicad_cli), *words, "--format", src.pcb_format if board else src.sch_format,
            "-o", str(dst)]
    if has_report:
        argv += ["--report-format", "json", "--report-file", str(report_file)]
    if layer_map:
        argv += ["--layer-map", str(layer_map)]
    logs.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="kicad-eda-import-", ignore_cleanup_errors=True) as tmp:
        source, stage_warning = _stage(src.path, Path(tmp)) if board else (src.path, None)
        before, started = _listing(dst.parent), time.monotonic()
        r = ff.run_child([*argv, str(source)], IMPORT_TIMEOUT_S, cwd=dst.parent)
        side_files = sorted(set(os.listdir(tmp)) - {source.name})
    step = new_step(" ".join(words), src, role, f"{caps.tool} {' '.join(words)}",
                    [*argv, str(source)], r, time.monotonic() - started, logs / f"{tag}.log")
    step["created"] = sorted(str(dst.parent / n) for n in _listing(dst.parent) - before)
    step["importer_side_files"] = side_files
    step["warnings"] += [stage_warning] if stage_warning else []
    fail = [f"exit code {r.returncode}{tail(r.stderr or r.stdout)}"] if r.returncode else []
    problem = output_problem(dst, "kicad_pcb" if board else "kicad_sch")
    fail += [problem] if problem else []
    report_errors = _read_report(step, caps, has_report, report_file)
    if problem is None:
        step["output"] = str(dst)
        _cross_check(step, dst)
    return finish(step, fail, report_errors)


# ----------------------------------------------------------------------------
# The SWIG importer and its diagnostics
# ----------------------------------------------------------------------------
def swig_install(caps: Sequence[ImportCapabilities],
                 src: ForeignFile) -> ImportCapabilities | None:
    """The newest install whose pcbnew Python runs `src`'s SWIG plugin."""
    if not src.swig_plugin:
        return None
    return next((c for c in caps if c.python and c.version_tuple[0] >= src.swig_min_major), None)


def plane_loss_message(loss: object, tool: str) -> str:
    """One error line for a swig_load_foreign inner_plane_loss object."""
    found = loss if isinstance(loss, dict) else {}
    zones, nets, why = found.get("zones"), found.get("nets"), found.get("error")
    parts = ([f"{zones} Altium internal-plane zone(s) moved to Eco1.User (no copper)"]
             if zones else []) + \
        ([f"plane net(s) {', '.join(map(str, nets))} have no zone on any In*.Cu"]
         if nets else []) + \
        ([f"a loss could not be ruled out ({why})"] if why else [])
    return f"inner plane loss ({tool}): " + "; ".join(parts or ["reported by the importer"])


def _diagnostic_warnings(result: dict) -> list[str]:
    zones = result.get("zones_on_disabled_layers")
    warnings = []
    if isinstance(zones, dict) and zones:
        layers = ", ".join(f"{layer}: {n}" for layer, n in zones.items())
        warnings.append(f"{sum(zones.values())} copper zone(s) on layers this "
                        f"{result.get('copper_layers')}-layer board does not have ({layers}): "
                        f"KiCad will not pour them; check the layer count (copper_layers) "
                        f"before rebuilding")
    return warnings + [f"SWIG diagnostics: {e}" for e in result.get("diagnostics_errors") or []]


def _swig_tool(caps: ImportCapabilities, result: dict, plugin: str) -> str:
    version = str(result.get("pcbnew") or caps.version).strip("() ")
    return f"pcbnew {version} LoadBoard({plugin})"


def _run_swig(caps: ImportCapabilities, src: ForeignFile, dst: Path,
              logs: Path) -> tuple[dict, list[str]]:
    """(step, failures) of one swig_load_foreign.py child."""
    argv = [str(caps.python), str(SWIG_SCRIPT), str(src.path), str(dst), src.swig_plugin]
    before, started = _listing(dst.parent), time.monotonic()
    r = ff.run_child(argv, IMPORT_TIMEOUT_S, cwd=dst.parent)
    result = last_json(r.stdout) or {}
    console = "\n".join(line for line in r.stdout.splitlines()       # minus the result line
                        if not line.lstrip().startswith("{"))
    step = new_step(SWIG_STEP, src, BOARD, _swig_tool(caps, result, src.swig_plugin), argv, r,
                    time.monotonic() - started, logs / f"swig-{src.path.name}.log",
                    scan_console(console, r.stderr))
    step["created"] = sorted(str(dst.parent / n) for n in _listing(dst.parent) - before)
    step["result"] = result or None
    step["statistics"] = {k: result[k] for k in _SWIG_COUNTS if k in result}
    step["warnings"] += _diagnostic_warnings(result)
    fail = [] if r.returncode == 0 and result.get("ok") else \
        [str(result.get("error") or f"exit code {r.returncode}{tail(r.stderr)}")]
    problem = output_problem(dst, "kicad_pcb")
    if problem is None:
        step["output"] = str(dst)
        _cross_check(step, dst)
    elif not fail:
        fail.append(problem)
    return step, fail


def _set_aside(dst: Path) -> tuple[Path, str]:
    """(where the lossy board is now, how the human step says so)."""
    lossy = dst.with_name(dst.stem + LOSSY_SUFFIX)
    try:
        os.replace(dst, lossy)
    except OSError as e:
        return dst, f"it could not be set aside ({e}) and is still {dst.name}: replace it"
    return lossy, f"set aside as {lossy.name}"


def _plane_loss_blocker(src: ForeignFile, step: dict, dst: Path,
                        message: str) -> tuple[dict, dict]:
    """Fail the step, set its lossy board aside, and ask for a KiCad 10+ import."""
    lossy, where = _set_aside(dst)
    created = sorted(str(lossy) if c == str(dst) else c for c in step["created"])
    failed = finish({**step, "output": None, "lossy_output": str(lossy), "created": created},
                    [message])
    action = (f"Import with KiCad 10+: install KiCad 10 or newer and import again (kicad-cli "
              f"pcb import keeps the Altium planes on In*.Cu), or open {src.path.name} in a "
              f"KiCad 10+ PCB Editor (File > Import > Non-KiCad Board File...) and save it as "
              f"{dst}; board_backend='swig' accepts the lossy import as a degraded result")
    why = (f"{message}: the automatic SWIG fallback would give a board without its "
           f"inner-plane copper ({where})")
    return failed, human("inner_plane_loss", src, BOARD, why, action, dst,
                         lossy_output=str(lossy))


def swig_import(caps: ImportCapabilities, src: ForeignFile, dst: Path, logs: Path,
                automatic: bool) -> tuple[dict, list[dict]]:
    """(step, human_steps) of the SWIG importer; `automatic` = chosen by
    board_backend="auto" (a plane loss is then a blocker, not an error)."""
    step, fail = _run_swig(caps, src, dst, logs)
    loss = (step["result"] or {}).get("inner_plane_loss")
    if fail or not loss:
        return finish(step, fail), []
    message = plane_loss_message(loss, step["tool"])
    if not automatic:
        return finish(step, [], [message]), []
    failed, blocker = _plane_loss_blocker(src, step, dst, message)
    return failed, [blocker]


# ----------------------------------------------------------------------------
# Human steps (GUI-only imports, missing importers)
# ----------------------------------------------------------------------------
def human(code: str, src: ForeignFile, role: str, why: str, action: str,
          expected: Path | str = "", **extra: object) -> dict:
    return {"id": f"{code}:{src.path.name}", "input": str(src.path), "role": role,
            "format": src.label, "why": why, "action": action,
            "expected_output": str(expected) if expected else "", "blocking": True, **extra}


def gui_board_step(src: ForeignFile, why: str, dst: Path, code: str) -> dict:
    return human(code, src, BOARD, why,
                 f"Open {src.path.name} in a KiCad PCB Editor (File > Import > Non-KiCad "
                 f"Board File...), save it as {dst}, then convert it with the KiCad "
                 f"converter", dst)


def probe_note(caps: Sequence[ImportCapabilities]) -> str:
    """Why a missing importer may be a false negative: the capability probes
    that timed out or could not start ("" when every probe settled)."""
    unsettled = [f"{c.tool}: {e}" for c in caps for e in c.probe_errors]
    if not unsettled:
        return ""
    return (f" (capability probe unsettled -- {'; '.join(unsettled)}: the importer may "
            f"exist; re-run the import once the probe settles)")


def no_board_importer(src: ForeignFile, dst: Path, backend: str,
                      caps: Sequence[ImportCapabilities]) -> dict:
    if src.gui_only:
        return gui_board_step(src, src.gui_only, dst, "gui_only_board")
    need = (f"a pcbnew {src.swig_min_major}+ Python (LoadBoard({src.swig_plugin}) returns None "
            f"on KiCad {src.swig_min_major - 1})" if src.swig_min_major else "a pcbnew Python")
    if backend == "swig":
        why = f"board_backend='swig' but no KiCad install has {need}"
    elif backend == "cli":
        why = "board_backend='cli' but no installed kicad-cli has `pcb import` (KiCad 10+)"
    else:
        why = (f"no installed kicad-cli has `pcb import` (KiCad 10+) and no install has {need} "
               f"for the SWIG importer")
    step = gui_board_step(src, why + probe_note(caps), dst, "no_board_importer")
    return {**step, "action": f"Install KiCad 10 or newer (kicad-cli pcb import), or: "
                              f"{step['action']}"}


# ----------------------------------------------------------------------------
# The board step
# ----------------------------------------------------------------------------
def import_board(src: ForeignFile, caps: Sequence[ImportCapabilities], dst: Path, logs: Path,
                 backend: str, layer_map: str) -> tuple[dict | None, list[dict]]:
    """(step, human_steps) for the board; see the module docstring."""
    if src.gui_only:
        return None, [no_board_importer(src, dst, backend, caps)]
    cli = first_with(caps, "pcb_import") if backend != "swig" else None
    swig = swig_install(caps, src) if backend != "cli" else None
    if cli is None:
        if swig is None:
            return None, [no_board_importer(src, dst, backend, caps)]
        return swig_import(swig, src, dst, logs, automatic=backend == "auto")
    step = cli_import(cli, src, BOARD, dst, logs, layer_map)
    if step["status"] != FAILED or swig is None:
        return step, []
    if layer_map:
        return {**step, "errors": [*step["errors"], LAYER_MAP_NOT_APPLIED]}, []
    fallback, blockers = swig_import(swig, src, dst, logs, automatic=True)
    note = f"{step['tool']} failed ({'; '.join(step['errors'])}); fell back to {fallback['tool']}"
    return {**fallback, "fallback_from": step, "warnings": [*fallback["warnings"], note]}, blockers


def importer_footprint_library(step: dict | None) -> tuple[str | None, str | None]:
    """(path, note): the <stem>-import-fps.pretty the board importer wrote
    (KiCad 11), else None and why (F46-11 builds D0 footprints from it)."""
    if step is None or step["status"] == FAILED or not step["output"]:
        return None, "no board was imported, so there is no importer footprint library"
    libs = [Path(c) for c in step["created"] if c.endswith(IMPORT_FPS_SUFFIX) and Path(c).is_dir()]
    own = Path(step["input"]).stem + IMPORT_FPS_SUFFIX
    if libs:
        return str(next((p for p in libs if p.name == own), libs[0])), None
    return None, (f"{step['tool']} wrote no footprint library (KiCad 10.0 `pcb import` and "
                  f"the SWIG importer write none; the KiCad 11 importer writes "
                  f"<stem>{IMPORT_FPS_SUFFIX}): extract the footprints from the board (D4)")
