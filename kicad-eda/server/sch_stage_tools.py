"""Schematic-stage tools of the net-map pipeline (netlist.json -> .kicad_sch),
split out of kicad_server so the server entry point stays small.

  * build_netlist_schematic: the body of
    kicad_server.build_schematic_from_netlist (project libraries, the API
    route kicad_server hands in, the file route, the api -> file fallback, ERC);
  * _build_via_sch_build: the file route (server/tools/sch_build.py under this
    venv), used when KiCad 11's schematic API is unavailable;
  * build_pin_sidecars: pin-coordinate sidecars for the file-backend wiring
    tools (server/tools/build_pin_sidecars.py);
  * design_report: design-intent + DFA review of a netlist
    (server/tools/design_report.py).

Project libraries (ROADMAP D1, F2-14): every tool here loads the netlist's
libraries with project_libs (``libraries`` block > the sym-lib-table /
fp-lib-table beside the netlist > stock). A build refuses, before anything is
written or spawned, when they do not load (each problem names the library's
nickname and path) or when the schematic is open in a KiCad editor. It merges
the libraries the design uses into the schematic directory's tables
(``kicad-eda managed`` rows) before KiCad's API creates the document (KiCad
reads project tables only then) and before the file build; the file route also
writes a minimal ``<stem>.kicad_pro`` (kicad-cli ERC reads the tables only
beside one; KiCad's API saves its own) and searches ``<netlist dir>/pins``
then the central pins/ for sidecars. Results carry ``libraries``
(project_libs.sources) and ``library_tables`` (the files written).

Missing nightly library tables (D5): when the API build (backend "auto")
fails because KiCad 11 has no global symbol library table
(backends.sch_session.is_missing_global_tables), the build is redone on the
file backend; the response carries ``fallback`` = {from, to, reason,
decision | decision_error} and the G2 decision is recorded once in the
netlist's ``<stem>.decisions.jsonl``. An explicit backend "api" does not fall
back (the response says how). The KiCad config is never edited.

kicad_server imports these names back and serves build_pin_sidecars and
design_report from its own TOOLS tuple (same names, same order as before the
split). Toolchain functions are looked up on the module at call time
(`tc.run`, `tc.symbols_dir`), so a patch of toolchain reaches them. This
module never imports kicad_server (no cycle): the API route and the
schematic session registry stay there; server/tools modules are imported on
first use.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from types import ModuleType
from typing import Any

import kicad_ipc as ipc
import project_libs
import toolchain as tc

TOOLS_DIR = tc.TOOLS_DIR
PINS_DIR = tc.PINS_DIR
ApiBuild = Callable[[str, str, str, str], dict]   # kicad_server._build_via_api
Loaded = tuple[project_libs.ProjectLibs, dict]
PROJECT_SUFFIX = ".kicad_pro"
STOCK = "stock"                                   # libtable.STOCK: a stock library resolved
API_NOTE_SKIPPED = ("ok", "sch", "reason", "backend", "note")   # not carried into a fallback
# The D5 fallback decision (stage G2: the schematic build). question + stage +
# decision identify it, so the G2 loop's rebuilds record it once.
FALLBACK_STAGE = "G2"
FALLBACK_SOURCE = "auto"
FALLBACK_QUESTION = ("KiCad 11's schematic API cannot place stock symbols: it has no global "
                     "symbol library table (an empty config dir). Which backend builds {sch}?")
FALLBACK_DECISION = "the file backend (server/tools/sch_build.py embeds the library symbols)"
FALLBACK_RATIONALE = ("ROADMAP D5: missing nightly library tables -> fall back to the file "
                      "backend for G2 and record it; this plugin never edits the KiCad config")
FALLBACK_ALTERNATIVES = ("copy sym-lib-table and fp-lib-table from <install>/share/kicad/"
                         "template into KiCad 11's config dir, then rebuild with backend='api'",)
INSIST_HINT = ("backend='auto' (the default) falls back to the file backend when KiCad 11 has "
               "no global library tables (ROADMAP D5); this plugin never edits the KiCad config")


def _tool_module(name: str) -> ModuleType:
    """A server/tools module, imported on first use (server/tools put on sys.path then)."""
    if str(TOOLS_DIR) not in sys.path:
        sys.path.insert(0, str(TOOLS_DIR))
    return importlib.import_module(name)


def _same_dir(a: str | os.PathLike, b: str | os.PathLike) -> bool:
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


# ----------------------------------------------------------------------------
# Project libraries (D1)
# ----------------------------------------------------------------------------
def load_project(netlist: str, symbols_dir: str = "", *, strict: bool = True) -> Loaded | dict:
    """(libs, design) of a netlist.json, or the refusal {"ok": False, "reason"}
    when it cannot be read or its libraries do not load: with ``strict``, any
    declared-library problem (each names the nickname and path); else only
    when nothing loaded (a malformed block or project table)."""
    try:
        design = json.loads(Path(netlist).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as e:
        return {"ok": False, "reason": f"cannot read netlist {netlist}: {e}"}
    if not isinstance(design, dict):
        return {"ok": False, "reason": f"netlist {netlist} is not a JSON object"}
    libs = project_libs.load(netlist, design, symbols_dir=symbols_dir)
    if libs.resolver is None or (strict and libs.error):
        return {"ok": False, "reason": f"the project libraries of {netlist} are not usable "
                                       f"(ROADMAP D1; nothing was written): {libs.error}"}
    return libs, design


def declares(libs: project_libs.ProjectLibs, key: str) -> bool:
    """Whether the netlist block or a project table declares a library of
    ``key`` ("symbols" / "footprints"); False = every one is stock."""
    return libs.resolver is not None and bool(libs.resolver.library_map().get(key))


def materialize(libs: project_libs.ProjectLibs, design: dict, out: str,
                with_project: bool) -> list[str] | dict:
    """Merge the libraries the design uses into ``out``'s directory tables
    (managed rows; stale ones cleared) and, ``with_project``, a minimal
    <stem>.kicad_pro when none exists (by NAME: project_libs puts it in the
    absolute directory). The paths written, or the refusal envelope."""
    pro = Path(out).with_suffix(PROJECT_SUFFIX).name if with_project else None
    try:
        return project_libs.materialize(libs, design, Path(out).parent, kicad_pro=pro)
    except (ValueError, OSError) as e:              # libtable.LibTableError is a ValueError
        return {"ok": False, "reason": f"cannot write the project library tables beside {out} "
                                       f"(nothing was built): {e}"}


def prepare(netlist: str, out: str, symbols_dir: str, with_project: bool
            ) -> tuple[project_libs.ProjectLibs, dict, list[str]] | dict:
    """A build's writes before any child runs: load the libraries (fail fast),
    the open-editor guard, the schematic directory, then the tables."""
    loaded = load_project(netlist, symbols_dir)
    if isinstance(loaded, dict):
        return loaded
    libs, design = loaded
    guard = ipc.write_guard(out)
    if guard:
        return guard
    try:
        Path(out).parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return {"ok": False, "reason": f"cannot create {Path(out).parent}: {e}"}
    tables = materialize(libs, design, out, with_project)
    return tables if isinstance(tables, dict) else (libs, design, tables)


# ----------------------------------------------------------------------------
# build_schematic_from_netlist's body
# ----------------------------------------------------------------------------
def build_netlist_schematic(netlist: str, out: str, symbols_dir: str, sidecars_dir: str,
                            backend: str, api_build: ApiBuild, *, insist: bool = False,
                            erc: bool = True) -> dict:
    """Build ``out`` from ``netlist`` on ``backend`` ("api" | "file", already
    chosen; ``insist``: the caller asked for "api" by name, so no fallback),
    then ERC (newest kicad-cli, errors gate). See the module doc."""
    prepared = prepare(netlist, out, symbols_dir, with_project=backend == "file")
    if isinstance(prepared, dict):
        return {**prepared, "sch": out, "backend": backend}
    libs, design, tables = prepared
    sides = sidecars_dir or os.pathsep.join(project_libs.pins_dirs(libs.netlist_dir))
    if backend == "file":
        res = {**_build_via_sch_build(netlist, out, symbols_dir, sides), "backend": "file"}
    else:
        res = {**api_build(netlist, out, symbols_dir, sidecars_dir), "backend": "api"}
        if not res.get("ok") and missing_global_tables(res.get("reason")):
            res = ({**res, "hint": INSIST_HINT} if insist else
                   fall_back(res, netlist, out, symbols_dir, sides, libs, design))
    res = {**res, "libraries": project_libs.sources(libs, design),
           "library_tables": [*tables, *res.get("library_tables", [])]}
    return with_erc(res, out) if erc else res


def missing_global_tables(reason: object) -> bool:
    """backends.sch_session.is_missing_global_tables on an API failure reason
    (imported lazily: False when the API backend itself cannot be imported,
    since then no API build ran)."""
    try:
        from backends.sch_session import is_missing_global_tables
    except Exception:  # noqa: BLE001 - no API backend, so no API error to recognise
        return False
    return is_missing_global_tables(str(reason or ""))


def fall_back(api_res: dict, netlist: str, out: str, symbols_dir: str, sides: str,
              libs: project_libs.ProjectLibs, design: dict) -> dict:
    """The D5 fallback: the file route (with its .kicad_pro) after the API
    failed for missing global tables; keeps the API's session-release notes.
    The decision is recorded once the file build is about to run."""
    notes = {k: v for k, v in api_res.items() if k not in API_NOTE_SKIPPED}
    fallback = {"from": "api", "to": "file", "reason": str(api_res.get("reason", ""))}
    written = materialize(libs, design, out, with_project=True)
    if isinstance(written, dict):
        return {**notes, **written, "sch": out, "backend": "file", "fallback": fallback}
    fallback = {**fallback, **record_fallback(netlist, out)}
    res = _build_via_sch_build(netlist, out, symbols_dir, sides)
    return {**notes, **res, "backend": "file", "fallback": fallback, "library_tables": written}


def record_fallback(netlist: str, sch: str) -> dict:
    """{"decision": id} of the G2 decision for this fallback in the netlist's
    decision log, appended unless the log already holds it (the G2 loop
    rebuilds; a rerun repeats nothing); {"decision_error": why} when the log
    cannot be read or written (the build itself stands)."""
    fields = {"stage": FALLBACK_STAGE, "question": FALLBACK_QUESTION.format(sch=Path(sch).name),
              "decision": FALLBACK_DECISION, "rationale": FALLBACK_RATIONALE,
              "alternatives": list(FALLBACK_ALTERNATIVES), "confidence": "high",
              "source": FALLBACK_SOURCE, "reversible": True}
    try:
        dec = _tool_module("decisions")
        same = next((e for e in dec.load_decisions(netlist).entries
                     if all(e[key] == fields[key] for key in ("stage", "question", "decision"))),
                    None)
        entry = same or dec.append_decision(netlist, **fields)
    except (ValueError, OSError) as e:             # DecisionError is a ValueError
        return {"decision_error": f"{type(e).__name__}: {e}"}
    return {"decision": entry["id"]}


def with_erc(res: dict, out: str) -> dict:
    """``res`` plus the ERC fields (not the report) after a good build when a
    kicad-cli exists. Gates on ERRORS only (warnings inform: e.g. an
    address-strap pin tied to a power net); the same policy as the DRC gate."""
    if not (res.get("ok") and tc.kicad_cli()):
        return res
    erc = tc.erc_json(out, str(Path(out).with_suffix("")) + "_erc.json")
    return {**res, **{k: v for k, v in erc.items() if k != "report"}}


def _build_via_sch_build(netlist: str, out: str, symbols_dir: str, sidecars_dir: str) -> dict:
    """server/tools/sch_build.py under this venv (sexpdata). ``sidecars_dir``
    is passed through: one directory or an os.pathsep list (default PINS_DIR)."""
    script = TOOLS_DIR / "sch_build.py"
    if not script.is_file():
        return {"ok": False, "reason": f"missing {script}"}
    syms = tc.symbols_dir(symbols_dir)
    sides = sidecars_dir or str(PINS_DIR)
    if not syms:
        return {"ok": False, "reason": "symbols_dir not given and KiCad share dir not found"}
    guard = ipc.write_guard(out)
    if guard:
        return guard
    r = tc.run([sys.executable, str(script), netlist, out, syms, sides])
    res = tc.last_json(r.stdout) or {}
    res["ok"] = r.returncode == 0 and res.get("ok", False)
    res["sch"] = out
    if r.stderr:
        res["stderr"] = r.stderr[-1200:]
    return res


# ----------------------------------------------------------------------------
# build_pin_sidecars
# ----------------------------------------------------------------------------
def build_pin_sidecars(symbols: str = "", out_dir: str = "", symbols_dir: str = "",
                       netlist: str = "") -> dict:
    """Build pin-coordinate sidecars (pin name/number -> schematic-space offset) for
    a comma-separated list of `Lib:Symbol` ids, used by the file-backend wiring
    tools. Writes <Lib_Symbol>.pins.json into out_dir (default: this server's
    pins/, for stock parts). With `netlist` (a netlist.json) its project
    libraries resolve too (its `libraries` block, then the sym-lib-table beside
    it, then stock), out_dir defaults to <netlist dir>/pins, `symbols` defaults
    to the netlist's project-library parts, and a project-library part is
    refused for the central pins/ (ROADMAP D1: project sidecars live with the
    project)."""
    script = TOOLS_DIR / "build_pin_sidecars.py"
    if not script.is_file():
        return {"ok": False, "reason": f"missing {script}"}
    ids = [s.strip() for s in symbols.split(",") if s.strip()]
    if netlist:
        return _project_sidecars(script, netlist, ids, out_dir, symbols_dir)
    syms = tc.symbols_dir(symbols_dir)
    out = out_dir or str(PINS_DIR)
    if not syms:
        return {"ok": False, "reason": "symbols_dir not given and KiCad share dir not found"}
    if not ids:
        raise ValueError("symbols must list at least one Library:Symbol id")
    r = tc.run([sys.executable, str(script), syms, out] + ids)
    return _sidecar_result(r, out, tc.last_json(r.stdout) or {})


def _sidecar_result(r: Any, out: str, summary: Mapping) -> dict:
    """The tool response of one build_pin_sidecars.py run (``r``: tc.run's result)."""
    return {"ok": r.returncode == 0 and summary.get("ok", r.returncode == 0),
            "out_dir": out, "written": summary.get("written", []),
            "failed": summary.get("failed", []),
            "stdout": r.stdout[-800:], "stderr": r.stderr[-800:]}


def _project_sidecars(script: Path, netlist: str, ids: list[str], out_dir: str,
                      symbols_dir: str) -> dict:
    """build_pin_sidecars(netlist=...): the --netlist mode of the script."""
    loaded = load_project(netlist, symbols_dir, strict=False)
    if isinstance(loaded, dict):
        return loaded
    libs, design = loaded
    out = out_dir or str(libs.netlist_dir / project_libs.PINS_SUBDIR)
    refused = central_refusals(libs, design, ids, out)
    if refused:
        return {"ok": False, "out_dir": out, "netlist": libs.netlist, "refused": refused,
                "reason": f"{', '.join(refused)}: project-library sidecars belong in "
                          f"{libs.netlist_dir / project_libs.PINS_SUBDIR} (ROADMAP D1), never "
                          f"in the plugin's central {PINS_DIR}; nothing was written"}
    stock = ["--symbols", libs.stock_symbols] if libs.stock_symbols else []
    r = tc.run([sys.executable, str(script), "--netlist", libs.netlist, "--out", out,
                *stock, *ids])
    summary = tc.last_json(r.stdout) or {}
    res = {**_sidecar_result(r, out, summary), "netlist": libs.netlist,
           "pins": summary.get("pins", {})}
    extra = {"note": summary["note"]} if summary.get("note") else {}
    if summary.get("error"):
        extra = {**extra, "reason": summary["error"]}
    if libs.error:
        extra = {**extra, "library_errors": libs.error}
    return {**res, **extra}


def central_refusals(libs: project_libs.ProjectLibs, design: dict, ids: list[str],
                     out: str) -> list[str]:
    """The project-library lib_ids (``ids``, else every component's) a
    sidecar run would write into the central PINS_DIR; [] for another dir."""
    if not _same_dir(out, PINS_DIR):
        return []
    comps = design.get("components")
    wanted = ids or [c.get("lib_id") for c in (comps if isinstance(comps, list) else [])
                     if isinstance(c, Mapping)]
    kind = _tool_module("libtable").SYMBOL
    refused: list[str] = []
    for lib_id in dict.fromkeys(i for i in wanted if isinstance(i, str) and ":" in i):
        hit = libs.resolver.resolve(kind, lib_id.partition(":")[0])
        if hit is not None and hit.source != STOCK:
            refused.append(lib_id)
    return refused


# ----------------------------------------------------------------------------
# design_report
# ----------------------------------------------------------------------------
def design_report(netlist: str, symbols_dir: str = "", out_md: str = "") -> dict:
    """Audit a netlist for design intent + assembly (DFA): part-count reduction
    (repeated passives -> arrays), decoupling coverage (every IC power rail has a
    bypass cap), the power tree (each rail's driver + load count), floating pins,
    and nets that likely need a pull resistor. Reads pin electrical types from the
    KiCad symbols: project-library parts through the netlist's libraries (its
    `libraries` block, then the sym-lib-table beside it), the rest from stock.
    Returns the structured report and writes a Markdown copy next to the netlist.
    This reviews; it does not modify the design. ok is False (with `errors`) when
    a symbol could not be read: the pin-type checks did not cover that part, so
    the review is incomplete rather than clean; and (with `reason`, nothing
    written) when the project libraries do not load."""
    try:
        dr = _tool_module("design_report")
    except Exception as e:
        return {"ok": False, "reason": f"design_report import failed: {e}"}
    loaded = load_project(netlist, symbols_dir)
    if isinstance(loaded, dict):
        return loaded
    libs, design = loaded
    source = libs.resolver if libs.stock_symbols or declares(libs, "symbols") else ""
    rep = dr.analyze(design, source)
    md = dr.to_markdown(rep)
    md_path = out_md or (str(Path(netlist).with_suffix("")) + "_report.md")
    Path(md_path).write_text(md, encoding="utf-8")
    errors = list(rep.get("errors", []))
    res = {"ok": not errors, "errors": errors, "report": rep, "markdown": md,
           "markdown_path": md_path}
    if errors:
        res["reason"] = (f"design review could not run for {len(errors)} symbol(s): "
                         + "; ".join(errors))
    return res
