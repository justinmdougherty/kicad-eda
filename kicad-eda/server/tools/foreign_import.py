#!/usr/bin/env python3
"""Foreign designs through KiCad's own importers (ROADMAP_DECISIONS D4, F6).

Eagle, Altium and EasyEDA files enter KiCad only through KiCad's importers:

  * Boards: kicad-cli ``pcb import --report-format json`` on the newest
    install that has it (KiCad 10.0 and the KiCad 11 nightly); the SWIG
    importer (swig_load_foreign.py: ``pcbnew.LoadBoard(path, <PLUGIN>)`` in a
    child, kicad_quiet first, stdin=DEVNULL) only when no kicad-cli can import
    the board, when the CLI import failed (never with a layer map), or on
    request (``board_backend="swig"``: e.g. a board in the KiCad 9 / D0
    dialect). Eagle goes through SWIG only on pcbnew 10+ (``LoadBoard(EAGLE)``
    returns None on KiCad 9). An Altium board whose internal planes pcbnew 9
    lost (``inner_plane_loss``) blocks an automatic fallback and degrades an
    explicit one. The board step lives in foreign_board.py.
  * Schematics: KiCad 11 ``sch import --report-format json``, one call per
    sheet (multi-sheet Altium = one SchDoc per call, and ``hierarchy``
    records that a root sheet must still be assembled), then a minimal
    ``<stem>.kicad_pro`` so kicad-cli ERC reads the project tables the
    importers wrote. KiCad 9 and 10.0 import schematics only in the GUI.
  * Libraries: ``fp upgrade`` / ``sym upgrade`` on the OLDEST install that
    converts the file, so the output is the D0 dialect (footprint 20241229,
    symbol 20241209) whenever KiCad 9 reads the format.
  * GUI-only cases come back as ``human_steps`` entries instead of obscure
    failures: EasyEDA Pro boards, Altium ``.PrjPcb``, anything no installed
    KiCad can import (installing software is a human step, D5), and the
    inner-plane-loss blocker. A capability probe that timed out is a warning
    and is named in those steps (the importer may exist).

rc=0 and an importer's JSON report are not proof of success (D0): every
console line is scanned for ``Error:`` (the Altium SchDoc importer printed 51
of them with rc=0 and ``"errors": []``), outputs are checked on disk and the
report's counts are compared with the files.

Classification, the per-install capability probe (which install does what)
and console scanning live in foreign_formats.py. Venv-side only.
``import_project`` returns what the KiCad converter (F4a / import_pipeline)
needs -- ``project``, ``board``, ``schematics``, ``source`` (D4 provenance:
kind, path, sha256, tool, files), ``importer_footprint_library`` and the
import report -- and writes the report to ``<out_dir>/<stem>.foreign_import.json``;
console logs and the importers' own reports stay in ``<out_dir>/import_logs/``.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from pathlib import Path
from typing import Iterable, Sequence

import foreign_formats as ff
import kicad_env as env
import libtable
from foreign_board import (DEGRADED, FAILED, IMPORT_TIMEOUT_S, INCOMPLETE, LOG_DIR, OK,
                           cli_import, gui_board_step, human, import_board,
                           importer_footprint_library, output_problem, probe_note, read_text,
                           tail, write_log)
from foreign_formats import (BOARD, FOOTPRINT, LIBRARY, PROJECT, SCHEMATIC, SYMBOL, ForeignFile,
                             ForeignFormatError, ImportCapabilities, classify,
                             discover_capabilities, first_with, scan_console)
from sexpr_read import TOKEN_RE

D0_BOARD_VERSION = 20241229        # D0: the .kicad_pcb / .kicad_mod dialect KiCad 9 pcbnew reads
D0_FOOTPRINT_VERSION = 20241229
D0_SYMBOL_VERSION = 20241209       # D0: KiCad 9 .kicad_sym
HEADER_BYTES = 1024
REPORT_SUFFIX = ".foreign_import.json"
BOARD_BACKENDS = ("auto", "cli", "swig")

_VERSION_RE = re.compile(r"\(version\s+(\d+)\s*\)")
_GENERATOR_VERSION_RE = re.compile(r'\(generator_version\s+"?([^\s")]+)')
_BOARD_NICK_RE = re.compile(r'^\s*\(footprint\s+"([^":]+):', re.M)
_SCH_FOOTPRINT_RE = re.compile(r'\(property\s+"Footprint"\s+"((?:[^"\\]|\\.)*)"')
_LIB_SYMBOLS_RE = re.compile(r"\(\s*lib_symbols\b")
_LIB_SYMBOL_RE = re.compile(r'^\t\(symbol\s+"', re.M)


# ----------------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------------
def _format_version(path: Path) -> int | None:
    m = _VERSION_RE.search(read_text(path, HEADER_BYTES))
    return int(m.group(1)) if m else None


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _provenance(src: ForeignFile, tool: str | None, sha256: str | None = None) -> dict:
    """The D4 ``source`` keys (netlist_schema "source"): kind, path, sha256, tool."""
    return {"kind": src.kind, "path": str(src.path), "sha256": sha256 or _sha256(src.path),
            **({"tool": tool} if tool else {})}


def _safe_stem(text: str) -> str:
    return re.sub(r"[^\w.-]+", "_", text, flags=re.ASCII).strip("._") or "imported"


# ----------------------------------------------------------------------------
# Request checks (all before any probe or import)
# ----------------------------------------------------------------------------
def _refuse_duplicates(files: Sequence[ForeignFile]) -> None:
    seen: dict[str, ForeignFile] = {}
    for f in files:
        key = os.path.normcase(str(f.path.resolve()))
        if key in seen:
            raise ValueError(f"{seen[key].path.name} is given more than once ({seen[key].path} "
                             f"and {f.path}): pass each input once")
        seen[key] = f


def _classify_inputs(inputs: Sequence[str | os.PathLike] | str | os.PathLike
                     ) -> list[ForeignFile]:
    if isinstance(inputs, (str, os.PathLike)):
        inputs = [inputs]
    files = [classify(p) for p in inputs]
    if not files:
        raise ValueError("import_project: no input files")
    _refuse_duplicates(files)
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
        raise ValueError(f"{board.path.name}: the SWIG importer loads only Altium .PcbDoc, "
                         f"EasyEDA Std and (pcbnew 10+) Eagle boards, not a {board.label}")


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


# ----------------------------------------------------------------------------
# import_project
# ----------------------------------------------------------------------------
def _import_schematic(src: ForeignFile, caps: Sequence[ImportCapabilities], dst: Path,
                      logs: Path) -> tuple[dict | None, list[dict]]:
    cli = first_with(caps, "sch_import")
    if cli is not None:
        return cli_import(cli, src, SCHEMATIC, dst, logs), []
    return None, [human(
        "no_schematic_importer", src, SCHEMATIC,
        "no installed kicad-cli has `sch import` (the KiCad 11 nightly does; KiCad 9 and "
        "10.0 import schematics only in the GUI)" + probe_note(caps),
        f"Install a KiCad 11 nightly (kicad-cli sch import), or open {src.path.name} in a "
        f"KiCad Schematic Editor (File > Import > Non-KiCad Schematic...) and save it as {dst}",
        dst)]


def _gui_only_steps(files: Sequence[ForeignFile], out: Path, stem: str) -> list[dict]:
    """Human steps for inputs no headless importer handles at all."""
    steps = []
    for f in files:
        if f.role == PROJECT:
            docs = ", ".join(f.documents) or "(none listed)"
            steps.append(human(
                "gui_only_project", f, PROJECT, f.gui_only,
                f"In a KiCad 11 Project Manager use File > Import Non-KiCad Project > Altium "
                f"Project... on {f.path.name} and save the project in {out}; or pass its "
                f"documents to import_project to import them one SchDoc at a time: {docs}",
                out, documents=list(f.documents)))
        elif f.role == SCHEMATIC and f.gui_only:
            steps.append(gui_board_step(f, f.gui_only, out / f"{stem}.kicad_pcb",
                                        "gui_only_board"))
    return steps


def _run_imports(board: ForeignFile | None, schematics: Sequence[ForeignFile],
                 caps: Sequence[ImportCapabilities], targets: dict[Path, Path], logs: Path,
                 backend: str, layer_map: str) -> tuple[list[dict], list[dict], dict[Path, str]]:
    """(steps, human_steps, {source path: importer tool}) for the board, then
    every schematic."""
    steps: list[dict] = []
    human_steps: list[dict] = []
    tools: dict[Path, str] = {}
    for f in ([board] if board else []) + list(schematics):
        if f.role == BOARD:
            step, asks = import_board(f, caps, targets[f.path], logs, backend, layer_map)
        else:
            step, asks = _import_schematic(f, caps, targets[f.path], logs)
        human_steps += asks
        if step:
            steps.append(step)
            tools[f.path] = step["tool"]
    return steps, human_steps, tools


def import_project(inputs: Sequence[str | os.PathLike] | str | os.PathLike,
                   out_dir: str | os.PathLike, *, name: str = "", board_backend: str = "auto",
                   layer_map: str = "", installs: Iterable[env.KiCadInstall] | None = None
                   ) -> dict:
    """Import one foreign design (at most one board plus its schematics, or an
    Altium project / EasyEDA Pro file) into a KiCad project in `out_dir`.

    `name` is the project stem (default: the board's, else the first input's).
    `board_backend`: "auto" (kicad-cli `pcb import`; the SWIG importer when no
    CLI can or the CLI import failed), "cli", or "swig" (the board comes out in
    the SWIG install's dialect, D0 on KiCad 9; Altium planes are lost there).
    `layer_map`: a JSON layer map for `pcb import --layer-map` (KiCad 11).
    `installs` defaults to every discovered KiCad.

    Returns the import report: ``status`` ok | degraded (outputs written but
    errors were reported) | incomplete (human steps pending) | failed;
    ``project`` / ``board`` / ``schematics`` / ``root_schematic`` paths;
    ``source`` (D4: kind, path, sha256, tool, files) and ``sources``;
    ``importer_footprint_library`` (+ ``_note``); ``statistics``,
    ``layer_mapping``, ``libraries`` (rows of the tables the importers wrote),
    ``footprint_links``, ``hierarchy``, ``errors``, ``warnings``,
    ``human_steps`` and every ``steps`` record (argv, rc, console log).
    Raises on a bad request (duplicate inputs included) before any import
    runs, and FileExistsError rather than overwrite anything."""
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
    steps, asks, tools = _run_imports(board, schematics, caps, targets, out / LOG_DIR,
                                      board_backend, layer_map)
    return _assemble(files, out, stem, steps, _gui_only_steps(files, out, stem) + asks, tools,
                     caps)


# ----------------------------------------------------------------------------
# The report
# ----------------------------------------------------------------------------
def _schematic_outputs(step: dict) -> list[str]:
    """The step's root schematic, then the sub-sheets it wrote beside it."""
    return [step["output"]] + [c for c in step["created"]
                               if c.endswith(".kicad_sch") and c != step["output"]]


def _library_rows(out: Path) -> tuple[dict[str, list[dict]], list[str]]:
    rows: dict[str, list[dict]] = {"footprints": [], "symbols": []}
    warnings: list[str] = []
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
    return rows, warnings


def _placed_text(text: str) -> str:
    """A schematic without its (lib_symbols ...) block: the library copies'
    Footprint fields say nothing about the placed symbols."""
    m = _LIB_SYMBOLS_RE.search(text)
    if m is None:
        return text
    depth = 0
    for tok in TOKEN_RE.finditer(text, m.start()):
        depth += {"(": 1, ")": -1}.get(tok.group(), 0)
        if depth == 0:
            return text[:m.start()] + text[tok.end():]
    return text[:m.start()]


def _footprint_links(board_text: str, sch_paths: Sequence[str]) -> tuple[dict, list[str]]:
    """The footprint libraries the board and the schematic name. consistent:
    False when a schematic nickname is not the board's, None when empty or
    unqualified Footprint fields leave it unknown, else True."""
    nicks = sorted(set(_BOARD_NICK_RE.findall(board_text)))
    values = [m.group(1) for p in sch_paths
              for m in _SCH_FOOTPRINT_RE.finditer(_placed_text(read_text(Path(p))))]
    empty = sum(1 for v in values if not v.strip())
    bare = sum(1 for v in values if v.strip() and ":" not in v)
    sch = sorted({v.split(":", 1)[0] for v in values if ":" in v})
    missing = sorted(set(sch) - set(nicks))
    consistent = False if missing else None if empty or bare else True
    warnings = []
    if missing:
        warnings.append(f"the schematic's Footprint fields name library {', '.join(missing)} but "
                        f"the board's footprints come from {', '.join(nicks) or 'no library'} "
                        f"(a KiCad importer quirk): take footprints from the board "
                        f"(D4: the board wins)")
    elif consistent is None:
        warnings.append(f"{empty} empty and {bare} unqualified schematic Footprint field(s) "
                        f"cannot be checked against the board's footprints: take footprints "
                        f"from the board (D4: the board wins)")
    return ({"board": nicks, "schematic": sch, "consistent": consistent,
             "counts": {"empty": empty, "unqualified": bare}}, warnings)


def _board_facts(board: str | None, sch_paths: Sequence[str],
                 caps: Sequence[ImportCapabilities]) -> tuple[dict, list[str]]:
    """Format version / dialect of the imported board and the footprint
    library nicknames the board and the schematic use."""
    facts: dict = {"board_format_version": None, "d0_dialect": None, "swig_readable": None,
                   "footprint_links": None}
    if not board:
        return facts, []
    path = Path(board)
    version = _format_version(path)
    m = _GENERATOR_VERSION_RE.search(read_text(path, HEADER_BYTES))
    writer = env.parse_version(m.group(1)) if m else (0, 0, 0)
    readers = [c for c in caps if c.python]
    warnings = []
    if version is not None and version > D0_BOARD_VERSION:
        warnings.append(f"{path.name} is in format {version}, newer than the D0 dialect "
                        f"{D0_BOARD_VERSION}: KiCad 9 pcbnew (the SWIG/Freerouting path) cannot "
                        f"load it -- rebuild through the KiCad 11 path or from stock footprints "
                        f"(D4); board_backend='swig' imports Altium/EasyEDA Std boards in D0")
    links, link_warnings = _footprint_links(read_text(path), sch_paths) if sch_paths else \
        (None, [])
    return ({**facts, "board_format_version": version,
             "d0_dialect": version is not None and version <= D0_BOARD_VERSION,
             "swig_readable": any(writer[:2] <= c.version_tuple[:2] for c in readers)
             if readers and writer != (0, 0, 0) else None,
             "footprint_links": links}, warnings + link_warnings)


def _hierarchy(sch_steps: Sequence[dict], sch_paths: list[str]) -> tuple[dict | None, list[str]]:
    if len(sch_steps) < 2:
        return None, []
    return ({"assembly_required": True, "sheets": sch_paths,
             "note": "each source sheet was imported by its own `sch import`; no root sheet "
                     "links them yet (assemble one, or convert every sheet and merge their "
                     "nets by name)"},
            [f"hierarchy: {len(sch_steps)} schematics imported one sheet at a time; a root "
             f"sheet must still be assembled"])


def _source_block(files: Sequence[ForeignFile], tools: dict[Path, str],
                  hashes: dict[Path, str]) -> dict:
    """D4 provenance of the import: the original board (else the first
    schematic), the importer actually used, and every original input."""
    primary = next((f for f in files if f.role == BOARD), None) or \
        next((f for f in files if f.role == SCHEMATIC), files[0])
    names = [f.path.name for f in files]
    return {**_provenance(primary, tools.get(primary.path), hashes[primary.path]),
            "files": {(f.path.name if names.count(f.path.name) == 1 else str(f.path)):
                      hashes[f.path] for f in files}}


def _status(steps: Sequence[dict], human_steps: Sequence[dict], project: Path | None,
            errors: Sequence[str]) -> str:
    if any(s["status"] == FAILED for s in steps):
        return FAILED
    if human_steps:
        return INCOMPLETE
    return FAILED if project is None else DEGRADED if errors else OK


def _assemble(files: list[ForeignFile], out: Path, stem: str, steps: list[dict],
              human_steps: list[dict], tools: dict[Path, str],
              caps: Sequence[ImportCapabilities]) -> dict:
    errors = [f"{Path(s['input']).name}: {e}" for s in steps for e in s["errors"]]
    warnings = [f"{Path(s['input']).name}: {w}" for s in steps for w in s["warnings"]]
    warnings += [f"{c.tool}: capability probe unsettled ({e}): an importer this install has "
                 f"may have been skipped" for c in caps for e in c.probe_errors]
    usable = [s for s in steps if s["status"] != FAILED and s["output"]]
    board = next((s["output"] for s in usable if s["role"] == BOARD), None)
    sch_steps = [s for s in usable if s["role"] == SCHEMATIC]
    sch_paths = [p for s in sch_steps for p in _schematic_outputs(s)]
    project = out / f"{stem}.kicad_pro" if board or sch_paths else None
    if project is not None:
        libtable.write_minimal_kicad_pro(project)   # False: the SWIG SaveBoard wrote one
    hierarchy, hierarchy_warnings = _hierarchy(sch_steps, sch_paths)
    facts, fact_warnings = _board_facts(board, sch_paths, caps)
    libraries, library_warnings = _library_rows(out)
    board_step = next((s for s in steps if s["role"] == BOARD), None)
    fp_library, fp_note = importer_footprint_library(board_step)
    hashes = {f.path: _sha256(f.path) for f in files}
    status = _status(steps, human_steps, project, errors)
    report = {
        "ok": status == OK, "status": status, "project": str(project) if project else None,
        "board": board, "schematics": sch_paths,
        "root_schematic": sch_steps[0]["output"] if len(sch_steps) == 1 else None,
        **facts, "source": _source_block(files, tools, hashes),
        "sources": [{**_provenance(f, tools.get(f.path), hashes[f.path]), "role": f.role}
                    for f in files],
        "importer_footprint_library": fp_library, "importer_footprint_library_note": fp_note,
        "statistics": {"board": board_step["statistics"] if board_step else {},
                       "schematics": {s["output"]: s["statistics"] for s in sch_steps}},
        "layer_mapping": (board_step.get("report") or {}).get("layer_mapping", {})
        if board_step else {},
        "libraries": libraries, "hierarchy": hierarchy, "errors": errors,
        "warnings": warnings + hierarchy_warnings + fact_warnings + library_warnings,
        "human_steps": human_steps, "steps": steps, "installs": [c.as_dict() for c in caps],
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
    problem = output_problem(target, "kicad_symbol_lib")
    if problem:
        return 0, None, problem
    count = len(_LIB_SYMBOL_RE.findall(read_text(target)))
    return count, _format_version(target), None if count else f"{target.name} has no symbols"


def _remove(target: Path) -> None:
    """Drop a failed attempt's partial output (the path did not exist before)."""
    if target.is_dir():
        shutil.rmtree(target)
    elif target.exists():
        target.unlink()


def _attempt(c: ImportCapabilities, words: tuple[str, str], src: ForeignFile, kind: str,
             target: Path, logs: Path) -> tuple[dict, ff.ConsoleScan, int, int | None]:
    """One `kicad-cli fp|sym upgrade` run: (attempt record, console, items, version)."""
    argv = [str(c.kicad_cli), *words, "--force", "-o", str(target), str(src.path)]
    r = ff.run_child(argv, IMPORT_TIMEOUT_S, cwd=target.parent)
    log = write_log(logs / f"{words[0]}-{c.version}-{src.path.name}.log", argv, r)
    scan = scan_console(r.stdout, r.stderr)
    count, version, problem = _inspect_library(kind, target)
    if r.returncode:
        problem = f"exit code {r.returncode}{tail(r.stderr or r.stdout)}"
    return ({"tool": c.tool, "argv": argv, "rc": r.returncode, "errors": list(scan.errors),
             "problem": problem, "log": str(log)}, scan, count, version)


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
        attempt, scan, count, version = _attempt(c, words, src, kind, target, logs)
        result["attempts"].append(attempt)
        if attempt["problem"] is None:
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
