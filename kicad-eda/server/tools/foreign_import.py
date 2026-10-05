#!/usr/bin/env python3
"""Foreign designs through KiCad's own importers (ROADMAP_DECISIONS D4, F6).

Eagle, Altium and EasyEDA files enter KiCad only through KiCad's importers:

  * KiCad 11 kicad-cli (the 10.99 nightly): the board through ``pcb import
    --report-format json``, each schematic through its own ``sch import
    --report-format json`` (multi-sheet Altium = one SchDoc per call, and
    ``hierarchy`` records that a root sheet must still be assembled), then a
    minimal ``<stem>.kicad_pro`` so kicad-cli ERC reads the project tables the
    importers wrote. KiCad 10.0 has ``pcb import`` only; KiCad 9 neither.
  * KiCad 9 SWIG fallback, boards only: swig_load_foreign.py runs
    ``pcbnew.LoadBoard(path, ALTIUM_DESIGNER | EASYEDA)`` in a child
    (kicad_quiet first, stdin=DEVNULL) when no kicad-cli has ``pcb import``,
    when the CLI import fails, or on request (``board_backend="swig"``: a board
    in the D0 dialect). Never Eagle: that importer aborts the process.
  * Libraries: ``fp upgrade`` / ``sym upgrade`` on the OLDEST install that
    converts the file, so the output is the D0 dialect (footprint 20241229,
    symbol 20241209) whenever KiCad 9 reads the format.
  * GUI-only cases come back as ``human_steps`` entries instead of obscure
    failures: EasyEDA Pro boards, Altium ``.PrjPcb``, and anything no
    installed KiCad can import (installing software is a human step, D5).

rc=0 and an importer's JSON report are not proof of success (D0): every
console line is scanned for ``Error:`` (the Altium SchDoc importer printed 51
of them with rc=0 and ``"errors": []``), outputs are checked on disk and the
report's counts are compared with the files.

Classification, the per-install capability probe (which install does what)
and console scanning live in foreign_formats.py. Venv-side only.
``import_project`` returns what the KiCad converter
(F4a) needs -- ``project``, ``board``, ``schematics``, ``source`` (D4
provenance) and the import report -- and writes the report to
``<out_dir>/<stem>.foreign_import.json``; console logs and the importers' own
reports stay in ``<out_dir>/import_logs/``.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Iterable, Sequence

import foreign_formats as ff
import kicad_env as env
import libtable
from foreign_formats import (BOARD, EAGLE, FOOTPRINT, LIBRARY, PROJECT, SCHEMATIC, SYMBOL,
                             ForeignFile, ForeignFormatError, ImportCapabilities, classify,
                             discover_capabilities, first_with, scan_console)
from toolchain import last_json

TOOLS_DIR = Path(__file__).resolve().parent
SWIG_SCRIPT = TOOLS_DIR / "swig_load_foreign.py"
D0_BOARD_VERSION = 20241229        # D0: the .kicad_pcb / .kicad_mod dialect KiCad 9 pcbnew reads
D0_FOOTPRINT_VERSION = 20241229
D0_SYMBOL_VERSION = 20241209       # D0: KiCad 9 .kicad_sym
IMPORT_TIMEOUT_S = 600             # one import or conversion child
HEADER_BYTES = 1024
LOG_DIR = "import_logs"
REPORT_SUFFIX = ".foreign_import.json"
BOARD_BACKENDS = ("auto", "cli", "swig")
OK, DEGRADED, INCOMPLETE, FAILED = "ok", "degraded", "incomplete", "failed"


# ----------------------------------------------------------------------------
# Files and logs
# ----------------------------------------------------------------------------
def _write_log(path: Path, argv: Sequence[str], r: env.ProbeResult) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"$ {subprocess.list2cmdline(list(argv))}\nexit code: {r.returncode}\n"
                    f"--- stdout ---\n{r.stdout}\n--- stderr ---\n{r.stderr}\n", encoding="utf-8")
    return path


def _text(path: Path, limit: int | None = None) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read(limit) if limit else f.read()
    except OSError:
        return ""


_VERSION_RE = re.compile(r"\(version\s+(\d+)\s*\)")
_GENERATOR_VERSION_RE = re.compile(r'\(generator_version\s+"?([^\s")]+)')
_FOOTPRINT_RE = re.compile(r'^\s*\(footprint\s+"', re.M)
_BOARD_NICK_RE = re.compile(r'^\s*\(footprint\s+"([^":]+):', re.M)
_SCH_NICK_RE = re.compile(r'\(property\s+"Footprint"\s+"([^":]+):')
_LIB_ID_RE = re.compile(r'\(lib_id\s+"')
_LIB_SYMBOL_RE = re.compile(r'^\t\(symbol\s+"', re.M)


def _format_version(path: Path) -> int | None:
    m = _VERSION_RE.search(_text(path, HEADER_BYTES))
    return int(m.group(1)) if m else None


def _output_problem(path: Path, tag: str) -> str | None:
    if not path.is_file() or path.stat().st_size == 0:
        return f"{path.name} was not written"
    if not _text(path, HEADER_BYTES).lstrip().startswith(f"({tag}"):
        return f"{path.name} is not a ({tag} ...) file"
    return None


def _listing(directory: Path) -> set[str]:
    return {p.name for p in directory.iterdir() if p.name != LOG_DIR} \
        if directory.is_dir() else set()


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _provenance(src: ForeignFile, tool: str | None) -> dict:
    """The D4 ``source`` block (netlist_schema "source"): kind, path, sha256, tool."""
    return {"kind": src.kind, "path": str(src.path), "sha256": _sha256(src.path),
            **({"tool": tool} if tool else {})}


def _safe_stem(text: str) -> str:
    return re.sub(r"[^\w.-]+", "_", text, flags=re.ASCII).strip("._") or "imported"


def _tail(text: str, n: int = 300) -> str:
    text = text.strip()
    return f": {text[-n:]}" if text else ""


# ----------------------------------------------------------------------------
# One import step (a kicad-cli import, or the SWIG LoadBoard child)
# ----------------------------------------------------------------------------
def _new_step(name: str, src: ForeignFile, role: str, tool: str, argv: Sequence[str],
              r: env.ProbeResult, seconds: float, log: Path,
              scan: ff.ConsoleScan | None = None) -> dict:
    """A step record; `scan` defaults to scanning the child's whole console."""
    scan = scan or scan_console(r.stdout, r.stderr)
    return {"step": name, "input": str(src.path), "role": role, "output": None, "tool": tool,
            "argv": list(argv), "rc": r.returncode, "seconds": round(seconds, 2),
            "status": OK, "errors": [], "warnings": list(scan.warnings),
            "console_errors": list(scan.errors), "console_warnings": list(scan.warnings),
            "report": None, "statistics": {}, "created": [], "log": str(_write_log(log, argv, r))}


def _finish(step: dict, fail: list[str], errors: Sequence[str] = ()) -> dict:
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
        found = len(_FOOTPRINT_RE.findall(_text(dst)))
    else:
        key = "symbols"
        files = [Path(p) for p in step["created"] if p.endswith(".kicad_sch")] or [dst]
        found = sum(len(_LIB_ID_RE.findall(_text(p))) for p in files)
    claimed = step["statistics"].get(key)
    if claimed is None:
        step["statistics"][key] = found
    elif int(claimed) != found:
        step["warnings"].append(f"report says {claimed} {key} but "
                                f"{', '.join(p.name for p in files)} has {found}")


def _cli_import(caps: ImportCapabilities, src: ForeignFile, role: str, dst: Path,
                logs: Path, layer_map: str = "") -> dict:
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
    argv.append(str(src.path))
    logs.mkdir(parents=True, exist_ok=True)
    before, started = _listing(dst.parent), time.monotonic()
    r = ff.run_child(argv, IMPORT_TIMEOUT_S, cwd=dst.parent)
    step = _new_step(" ".join(words), src, role, caps.tool, argv, r,
                     time.monotonic() - started, logs / f"{tag}.log")
    step["created"] = sorted(str(dst.parent / n) for n in _listing(dst.parent) - before)
    fail = [f"exit code {r.returncode}{_tail(r.stderr or r.stdout)}"] if r.returncode else []
    problem = _output_problem(dst, "kicad_pcb" if board else "kicad_sch")
    fail += [problem] if problem else []
    report_errors: list[str] = []
    if not has_report:
        step["warnings"].append(f"{caps.tool} has no --report-format: counts come from the files")
    elif report_file.is_file():
        try:
            step["report"] = json.loads(report_file.read_text(encoding="utf-8"))
        except ValueError as e:
            step["warnings"].append(f"import report {report_file.name} is not JSON ({e})")
    else:
        step["warnings"].append(f"the importer wrote no report ({report_file.name})")
    if isinstance(step["report"], dict):
        report_errors = [str(e) for e in step["report"].get("errors") or []]
        step["warnings"] += [str(w) for w in step["report"].get("warnings") or []]
        step["statistics"] = dict(step["report"].get("statistics") or {})
    if problem is None:
        step["output"] = str(dst)
        _cross_check(step, dst)
    return _finish(step, fail, report_errors)


_SWIG_COUNTS = ("footprints", "tracks", "vias", "nets", "zones", "copper_layers")


def _swig_import(caps: ImportCapabilities, src: ForeignFile, dst: Path, logs: Path) -> dict:
    argv = [str(caps.python), str(SWIG_SCRIPT), str(src.path), str(dst), src.swig_plugin]
    before, started = _listing(dst.parent), time.monotonic()
    r = ff.run_child(argv, IMPORT_TIMEOUT_S, cwd=dst.parent)
    result = last_json(r.stdout) or {}
    tool = f"pcbnew {result.get('pcbnew') or caps.version} (SWIG LoadBoard {src.swig_plugin})"
    console = "\n".join(line for line in r.stdout.splitlines()       # minus the result line
                        if not line.lstrip().startswith("{"))
    step = _new_step("swig LoadBoard", src, BOARD, tool, argv, r, time.monotonic() - started,
                     logs / f"swig-{src.path.name}.log", scan_console(console, r.stderr))
    step["created"] = sorted(str(dst.parent / n) for n in _listing(dst.parent) - before)
    step["result"] = result or None
    step["statistics"] = {k: result[k] for k in _SWIG_COUNTS if k in result}
    fail = [] if r.returncode == 0 and result.get("ok") else \
        [str(result.get("error") or f"exit code {r.returncode}{_tail(r.stderr)}")]
    problem = _output_problem(dst, "kicad_pcb")
    if problem is None:
        step["output"] = str(dst)
        _cross_check(step, dst)
    elif not fail:
        fail.append(problem)
    return _finish(step, fail)


# ----------------------------------------------------------------------------
# Human steps (GUI-only imports, missing importers)
# ----------------------------------------------------------------------------
def _human(code: str, src: ForeignFile, role: str, why: str, action: str,
           expected: Path | str = "", **extra) -> dict:
    return {"id": f"{code}:{src.path.name}", "input": str(src.path), "role": role,
            "format": src.label, "why": why, "action": action,
            "expected_output": str(expected) if expected else "", "blocking": True, **extra}


def _gui_board_step(src: ForeignFile, why: str, dst: Path, code: str) -> dict:
    return _human(code, src, BOARD, why,
                  f"Open {src.path.name} in a KiCad PCB Editor (File > Import > Non-KiCad "
                  f"Board File...), save it as {dst}, then convert it with the KiCad "
                  f"converter", dst)


def _no_board_importer(src: ForeignFile, dst: Path, backend: str) -> dict:
    if src.gui_only:
        return _gui_board_step(src, src.gui_only, dst, "gui_only_board")
    if backend == "swig":
        why = "board_backend='swig' but no KiCad install has a pcbnew Python"
    elif backend == "cli":
        why = "board_backend='cli' but no installed kicad-cli has `pcb import` (KiCad 10+)"
    elif not src.swig_plugin:
        why = (f"no installed kicad-cli has `pcb import` (KiCad 10+), and {src.label}s never "
               f"go through the KiCad 9 SWIG importer" +
               (" (Eagle aborts the process)" if src.kind == EAGLE else ""))
    else:
        why = ("no installed kicad-cli has `pcb import` (KiCad 10+) and no install has a "
               "pcbnew Python for the SWIG importer")
    step = _gui_board_step(src, why, dst, "no_board_importer")
    step["action"] = f"Install KiCad 10 or newer (kicad-cli pcb import), or: {step['action']}"
    return step


def _import_board(src: ForeignFile, caps: Sequence[ImportCapabilities], dst: Path, logs: Path,
                  backend: str, layer_map: str) -> tuple[dict | None, dict | None]:
    """(step, human_step) for the board: CLI first, the SWIG importer when no
    CLI can (or the CLI import failed, or backend="swig"); never Eagle via SWIG."""
    cli, swig = first_with(caps, "pcb_import"), first_with(caps, "python")
    if src.gui_only:
        return None, _no_board_importer(src, dst, backend)
    use_swig = backend == "swig" or (backend == "auto" and cli is None)
    if use_swig or cli is None:
        if use_swig and src.swig_plugin and swig is not None:
            return _swig_import(swig, src, dst, logs), None
        return None, _no_board_importer(src, dst, backend)
    step = _cli_import(cli, src, BOARD, dst, logs, layer_map)
    if step["status"] != FAILED or backend == "cli" or not src.swig_plugin or swig is None:
        return step, None
    fallback = _swig_import(swig, src, dst, logs)
    fallback["fallback_from"] = step
    fallback["warnings"].append(f"{cli.tool} pcb import failed ({'; '.join(step['errors'])}); "
                                f"fell back to {fallback['tool']}")
    return fallback, None


def _import_schematic(src: ForeignFile, caps: Sequence[ImportCapabilities], dst: Path,
                      logs: Path) -> tuple[dict | None, dict | None]:
    cli = first_with(caps, "sch_import")
    if cli is not None:
        return _cli_import(cli, src, SCHEMATIC, dst, logs), None
    return None, _human(
        "no_schematic_importer", src, SCHEMATIC,
        "no installed kicad-cli has `sch import` (the KiCad 11 nightly does; KiCad 9 and "
        "10.0 import schematics only in the GUI)",
        f"Install a KiCad 11 nightly (kicad-cli sch import), or open {src.path.name} in a "
        f"KiCad Schematic Editor (File > Import > Non-KiCad Schematic...) and save it as {dst}",
        dst)


# ----------------------------------------------------------------------------
# import_project
# ----------------------------------------------------------------------------
def _classify_inputs(inputs: Sequence[str | os.PathLike] | str | os.PathLike
                     ) -> list[ForeignFile]:
    if isinstance(inputs, (str, os.PathLike)):
        inputs = [inputs]
    files = [classify(p) for p in inputs]
    if not files:
        raise ValueError("import_project: no input files")
    for f in files:
        if f.role == LIBRARY:
            raise ValueError(f"{f.path.name} is a {f.label}: use convert_library for libraries")
    boards = [f.path.name for f in files if f.role == BOARD]
    if len(boards) > 1:
        raise ValueError(f"import_project takes at most one board, got {', '.join(boards)}")
    return files


def _check_backend(board: ForeignFile | None, backend: str) -> None:
    if backend not in BOARD_BACKENDS:
        raise ValueError(f"board_backend must be one of {', '.join(BOARD_BACKENDS)}, "
                         f"not {backend!r}")
    if backend == "swig" and board is not None and not board.swig_plugin:
        never = "never Eagle via SWIG: the importer aborts the process; " \
            if board.kind == EAGLE else ""
        raise ValueError(f"{board.path.name}: {never}the SWIG importer loads only Altium "
                         f".PcbDoc and EasyEDA Std boards, not a {board.label}")


def _check_layer_map(board: ForeignFile | None, backend: str, layer_map: str,
                     caps: Sequence[ImportCapabilities]) -> None:
    if not layer_map:
        return
    if not Path(layer_map).is_file():
        raise FileNotFoundError(f"layer_map not found: {layer_map}")
    if board is None:
        raise ValueError("layer_map applies to a board import, and no board was given")
    if backend == "swig":
        raise ValueError("layer_map needs kicad-cli `pcb import --layer-map`; the SWIG "
                         "importer takes no layer map")
    cli = first_with(caps, "pcb_import")
    if cli is None or not cli.layer_map:
        found = f"{cli.tool} has no --layer-map" if cli else \
            "no installed kicad-cli has `pcb import`"
        raise ValueError(f"layer_map needs a kicad-cli with `pcb import --layer-map` "
                         f"(KiCad 11); {found}")


def _targets(out: Path, stem: str, board: ForeignFile | None,
             schematics: Sequence[ForeignFile]) -> dict[Path, Path]:
    """source path -> output path. One schematic is the root <stem>.kicad_sch;
    several are written side by side under their own names."""
    targets = {board.path: out / f"{stem}.kicad_pcb"} if board else {}
    for s in schematics:
        name = stem if len(schematics) == 1 else _safe_stem(s.path.stem)
        targets[s.path] = out / f"{name}.kicad_sch"
    names = [t.name.lower() for t in targets.values()]
    if len(set(names)) != len(names):
        raise ValueError(f"two inputs would be written to the same file: {sorted(names)}")
    return targets


def _refuse_existing(out: Path, stem: str, targets: dict[Path, Path]) -> None:
    planned = [*targets.values(), out / f"{stem}.kicad_pro", out / f"{stem}{REPORT_SUFFIX}",
               *(out / libtable.TABLE_FILES[k] for k in (FOOTPRINT, SYMBOL))]
    existing = [str(p) for p in planned if p.exists()]
    if existing:
        raise FileExistsError(f"refusing to overwrite {', '.join(existing)}: the importers append "
                              f"to library tables and replace outputs; import into a new or "
                              f"empty directory")


def import_project(inputs: Sequence[str | os.PathLike] | str | os.PathLike,
                   out_dir: str | os.PathLike, *, name: str = "", board_backend: str = "auto",
                   layer_map: str = "", installs: Iterable[env.KiCadInstall] | None = None
                   ) -> dict:
    """Import one foreign design (at most one board plus its schematics, or an
    Altium project / EasyEDA Pro file) into a KiCad project in `out_dir`.

    `name` is the project stem (default: the board's, else the first input's).
    `board_backend`: "auto" (kicad-cli `pcb import`, the SWIG importer when no
    CLI can or the CLI import failed), "cli", or "swig" (Altium / EasyEDA Std
    only; the board comes out in the SWIG install's dialect, D0 on KiCad 9).
    `layer_map`: a JSON layer map for `pcb import --layer-map` (KiCad 11).
    `installs` defaults to every discovered KiCad.

    Returns the import report: ``status`` ok | degraded (outputs written but
    errors were reported) | incomplete (human steps pending) | failed;
    ``project`` / ``board`` / ``schematics`` / ``root_schematic`` paths;
    ``source`` / ``sources`` (D4 provenance); ``statistics``,
    ``layer_mapping``, ``libraries`` (rows of the tables the importers wrote),
    ``footprint_links``, ``hierarchy``, ``errors``, ``warnings``,
    ``human_steps`` and every ``steps`` record (argv, rc, console log).
    Raises on a bad request before any import runs, and FileExistsError rather
    than overwrite anything."""
    files = _classify_inputs(inputs)
    board = next((f for f in files if f.role == BOARD), None)
    schematics = [f for f in files if f.role == SCHEMATIC]
    _check_backend(board, board_backend)
    out = Path(os.path.abspath(out_dir))
    stem = _safe_stem(name or (board or files[0]).path.stem)
    targets = _targets(out, stem, board, schematics)
    _refuse_existing(out, stem, targets)
    caps = discover_capabilities(installs)
    _check_layer_map(board, board_backend, layer_map, caps)
    out.mkdir(parents=True, exist_ok=True)
    logs = out / LOG_DIR
    steps: list[dict] = []
    human: list[dict] = []
    tools: dict[Path, str] = {}
    for f in files:
        if f.role == PROJECT:
            docs = ", ".join(f.documents) or "(none listed)"
            human.append(_human(
                "gui_only_project", f, PROJECT, f.gui_only,
                f"In a KiCad 11 Project Manager use File > Import Non-KiCad Project > Altium "
                f"Project... on {f.path.name} and save the project in {out}; or pass its "
                f"documents to import_project to import them one SchDoc at a time: {docs}",
                out, documents=list(f.documents)))
        elif f.role == SCHEMATIC and f.gui_only:
            human.append(_gui_board_step(f, f.gui_only, out / f"{stem}.kicad_pcb",
                                         "gui_only_board"))
    for f in ([board] if board else []) + schematics:
        if f.role == BOARD:
            step, h = _import_board(f, caps, targets[f.path], logs, board_backend, layer_map)
        else:
            step, h = _import_schematic(f, caps, targets[f.path], logs)
        steps += [step] if step else []
        human += [h] if h else []
        if step:
            tools[f.path] = step["tool"]
    return _assemble(files, out, stem, steps, human, tools, caps)


def _schematic_outputs(step: dict) -> list[str]:
    """The step's root schematic, then the sub-sheets it wrote beside it."""
    return [step["output"]] + [c for c in step["created"]
                               if c.endswith(".kicad_sch") and c != step["output"]]


def _library_rows(out: Path, warnings: list[str]) -> dict[str, list[dict]]:
    rows: dict[str, list[dict]] = {"footprints": [], "symbols": []}
    for kind in (FOOTPRINT, SYMBOL):
        table = out / libtable.TABLE_FILES[kind]
        if not table.is_file():
            continue
        try:
            parsed = libtable.read_table(table)
        except libtable.LibTableError as e:
            warnings.append(f"the importer wrote an unreadable {table.name}: {e}")
            continue
        rows[libtable.MAP_KEYS[kind]] = [
            {"name": r.name, "uri": r.uri, "type": r.type, "import_cache": r.import_cache}
            for r in parsed.rows]
    return rows


def _board_facts(board: str | None, sch_paths: Sequence[str],
                 caps: Sequence[ImportCapabilities], warnings: list[str]) -> dict:
    """Format version / dialect of the imported board and the footprint
    library nicknames the board and the schematic use."""
    facts: dict = {"board_format_version": None, "d0_dialect": None, "swig_readable": None,
                   "footprint_links": None}
    if not board:
        return facts
    path = Path(board)
    version = _format_version(path)
    m = _GENERATOR_VERSION_RE.search(_text(path, HEADER_BYTES))
    writer = env.parse_version(m.group(1)) if m else (0, 0, 0)
    readers = [c for c in caps if c.python]
    facts.update(board_format_version=version,
                 d0_dialect=version is not None and version <= D0_BOARD_VERSION,
                 swig_readable=any(writer[:2] <= c.version_tuple[:2] for c in readers)
                 if readers and writer != (0, 0, 0) else None)
    if version is not None and version > D0_BOARD_VERSION:
        warnings.append(f"{path.name} is in format {version}, newer than the D0 dialect "
                        f"{D0_BOARD_VERSION}: KiCad 9 pcbnew (the SWIG/Freerouting path) cannot "
                        f"load it -- rebuild through the KiCad 11 path or from stock footprints "
                        f"(D4); board_backend='swig' imports Altium/EasyEDA Std boards in D0")
    if sch_paths:
        nicks = sorted(set(_BOARD_NICK_RE.findall(_text(path))))
        sch = sorted({n for p in sch_paths for n in _SCH_NICK_RE.findall(_text(Path(p)))})
        consistent = set(sch) <= set(nicks)
        facts["footprint_links"] = {"board": nicks, "schematic": sch, "consistent": consistent}
        if not consistent:
            missing = ", ".join(sorted(set(sch) - set(nicks)))
            warnings.append(f"the schematic's Footprint fields name library {missing} but the "
                            f"board's footprints come from {', '.join(nicks) or 'no library'} "
                            f"(a KiCad importer quirk): take footprints from the board "
                            f"(D4: the board wins)")
    return facts


def _assemble(files: list[ForeignFile], out: Path, stem: str, steps: list[dict],
              human: list[dict], tools: dict[Path, str],
              caps: Sequence[ImportCapabilities]) -> dict:
    errors = [f"{Path(s['input']).name}: {e}" for s in steps for e in s["errors"]]
    warnings = [f"{Path(s['input']).name}: {w}" for s in steps for w in s["warnings"]]
    usable = [s for s in steps if s["status"] != FAILED and s["output"]]
    board = next((s["output"] for s in usable if s["role"] == BOARD), None)
    sch_steps = [s for s in usable if s["role"] == SCHEMATIC]
    sch_paths = [p for s in sch_steps for p in _schematic_outputs(s)]
    project = None
    if board or sch_paths:
        project = out / f"{stem}.kicad_pro"
        libtable.write_minimal_kicad_pro(project)   # False: the SWIG SaveBoard wrote one
    hierarchy = None
    if len(sch_steps) > 1:
        hierarchy = {"assembly_required": True, "sheets": sch_paths,
                     "note": "each source sheet was imported by its own `sch import`; no root "
                             "sheet links them yet (assemble one, or convert every sheet and "
                             "merge their nets by name)"}
        warnings.append(f"hierarchy: {len(sch_steps)} schematics imported one sheet at a time; "
                        f"a root sheet must still be assembled")
    facts = _board_facts(board, sch_paths, caps, warnings)
    primary = next((f for f in files if f.role == BOARD), files[0])
    status = (FAILED if any(s["status"] == FAILED for s in steps) else INCOMPLETE if human
              else FAILED if project is None else DEGRADED if errors else OK)
    board_step = next((s for s in steps if s["role"] == BOARD), None)
    report = {
        "ok": status == OK, "status": status, "project": str(project) if project else None,
        "board": board, "schematics": sch_paths,
        "root_schematic": sch_steps[0]["output"] if len(sch_steps) == 1 else None,
        **facts, "source": _provenance(primary, tools.get(primary.path)),
        "sources": [{**_provenance(f, tools.get(f.path)), "role": f.role} for f in files],
        "statistics": {"board": board_step["statistics"] if board_step else {},
                       "schematics": {s["output"]: s["statistics"] for s in sch_steps}},
        "layer_mapping": (board_step.get("report") or {}).get("layer_mapping", {})
        if board_step else {},
        "libraries": _library_rows(out, warnings), "hierarchy": hierarchy,
        "errors": errors, "warnings": warnings, "human_steps": human, "steps": steps,
        "installs": [c.as_dict() for c in caps],
        "report_path": str(out / f"{stem}{REPORT_SUFFIX}")}
    Path(report["report_path"]).write_text(json.dumps(report, indent=2, default=str),
                                           encoding="utf-8")
    return report


# ----------------------------------------------------------------------------
# Library conversion: fp upgrade / sym upgrade, oldest capable install first
# ----------------------------------------------------------------------------
def _inspect_library(kind: str, target: Path) -> tuple[int, int | None, str | None]:
    """(items, format version, problem) of a converted library."""
    if kind == FOOTPRINT:
        mods = sorted(target.glob("*.kicad_mod")) if target.is_dir() else []
        if not mods:
            return 0, None, f"{target.name} holds no .kicad_mod"
        versions = [v for v in map(_format_version, mods) if v is not None]
        return len(mods), max(versions) if versions else None, None
    problem = _output_problem(target, "kicad_symbol_lib")
    if problem:
        return 0, None, problem
    count = len(_LIB_SYMBOL_RE.findall(_text(target)))
    return count, _format_version(target), None if count else f"{target.name} has no symbols"


def _remove(target: Path) -> None:
    """Drop a failed attempt's partial output (the path did not exist before)."""
    if target.is_dir():
        shutil.rmtree(target)
    elif target.exists():
        target.unlink()


def _convert(src: ForeignFile, kind: str, target: Path, caps: Sequence[ImportCapabilities],
             logs: Path) -> dict:
    attr, words = ("fp_upgrade", ("fp", "upgrade")) if kind == FOOTPRINT else \
        ("sym_upgrade", ("sym", "upgrade"))
    d0 = D0_FOOTPRINT_VERSION if kind == FOOTPRINT else D0_SYMBOL_VERSION
    result: dict = {"kind": kind, "path": str(target), "ok": False, "count": 0,
                    "format_version": None, "d0_dialect": None, "tool": None, "attempts": [],
                    "errors": [], "warnings": []}
    converters = sorted((c for c in caps if getattr(c, attr)), key=lambda c: c.version_tuple)
    if not converters:
        result["errors"].append(f"no installed kicad-cli can run `{' '.join(words)}` "
                                f"(install KiCad 9 or newer)")
        return result
    for c in converters:
        argv = [str(c.kicad_cli), *words, "--force", "-o", str(target), str(src.path)]
        r = ff.run_child(argv, IMPORT_TIMEOUT_S, cwd=target.parent)
        log = _write_log(logs / f"{words[0]}-{c.version}-{src.path.name}.log", argv, r)
        scan = scan_console(r.stdout, r.stderr)
        count, version, problem = _inspect_library(kind, target)
        if r.returncode:
            problem = f"exit code {r.returncode}{_tail(r.stderr or r.stdout)}"
        result["attempts"].append({"tool": c.tool, "argv": argv, "rc": r.returncode,
                                   "errors": list(scan.errors), "problem": problem,
                                   "log": str(log)})
        if problem is None:
            ok_d0 = version is not None and version <= d0
            result.update(ok=True, count=count, format_version=version, d0_dialect=ok_d0,
                          tool=c.tool)
            result["warnings"] += [f"{a['tool']} could not convert it: {a['problem']}"
                                   for a in result["attempts"][:-1]]
            result["warnings"] += [f"{c.tool}: {m}" for m in scan.errors + scan.warnings]
            if not ok_d0:
                result["warnings"].append(
                    f"{target.name} was written by {c.tool} in format {version}, newer than the "
                    f"D0 dialect {d0}: KiCad 9 tools (the SWIG path) cannot load it")
            return result
        _remove(target)
    result["errors"] = [f"{a['tool']}: {a['problem']}" for a in result["attempts"]]
    return result


def convert_library(path: str | os.PathLike, out_dir: str | os.PathLike, *, name: str = "",
                    installs: Iterable[env.KiCadInstall] | None = None) -> dict:
    """Convert a foreign library (Eagle .lbr, Altium .PcbLib/.IntLib, EasyEDA
    Pro .elibz/.elibz2, legacy KiCad .lib) into ``<out_dir>/<name>.pretty``
    and/or ``<name>.kicad_sym`` with `kicad-cli fp/sym upgrade`, trying the
    OLDEST capable install first so the result is in the D0 dialect when
    KiCad 9 reads the format; a newer dialect is reported as a warning.

    Returns {ok, status (ok | degraded | failed), input, kind, source,
    libraries: [{kind, path, ok, count, format_version, d0_dialect, tool,
    attempts}], errors, warnings}. Raises FileExistsError rather than
    overwrite, ForeignFormatError for a non-library input."""
    src = classify(path)
    if src.role != LIBRARY:
        raise ForeignFormatError(f"{src.path.name} is a {src.label}, not a library: "
                                 f"use import_project")
    out = Path(os.path.abspath(out_dir))
    stem = _safe_stem(name or src.path.stem)
    targets = {k: out / f"{stem}{libtable.LIB_SUFFIX[k]}" for k in src.library_kinds}
    existing = [str(t) for t in targets.values() if t.exists()]
    if existing:
        raise FileExistsError(f"refusing to overwrite {', '.join(existing)}")
    caps = discover_capabilities(installs)
    out.mkdir(parents=True, exist_ok=True)
    libs = [_convert(src, k, targets[k], caps, out / LOG_DIR) for k in src.library_kinds]
    errors = [e for lib in libs for e in lib["errors"]]
    warnings = [w for lib in libs for w in lib["warnings"]]
    status = FAILED if not all(lib["ok"] for lib in libs) else \
        DEGRADED if warnings else OK
    tool = next((lib["tool"] for lib in libs if lib["tool"]), None)
    return {"ok": status == OK, "status": status, "input": str(src.path), "kind": src.kind,
            "source": _provenance(src, tool), "libraries": libs, "errors": errors,
            "warnings": warnings}
