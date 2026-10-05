"""Napkin-mode review tools (F5; docs/ROADMAP_DECISIONS.md D5).

The MCP surface that lets a hands-off run explain itself afterwards:

  * ``record_decision``: append one runtime decision to the append-only
    ``<stem>.decisions.jsonl`` beside the netlist (entry ``{id, ts, stage,
    question, decision, rationale, alternatives, confidence, source,
    reversible, override}``, stage one of requirements|parts|G1..G5|routing)
    — the same file the pipeline harness writes, serialised by its
    ``<log>.lock`` (server/tools/decisions.py);
  * ``review_report``: the review report for the conversation path,
    ``<stem>_review.md`` (assumptions, decisions by stage with low confidence
    first, parts with their library source stock|project|generated|unknown,
    gate verdicts, remediations, accepted risks, routing method, board format
    + snapshot, renders, fab zip, open items), with **HUMAN REVIEW REQUIRED**
    at the top for safety-relevant designs (mains or > 50 V, Li-ion
    charging). It reads the harness's ``<stem>_pipeline_report.json`` when
    one exists and never writes it (that file belongs to
    pipeline/run_pipeline.py); without one the review is rendered against an
    empty gate list.

server/tools is not on sys.path when kicad_server imports this module, so
the tools modules (decisions, review_report, review_sources, design_report)
are imported inside the functions, after toolchain.TOOLS_DIR is put on the
path. Plain functions; kicad_server registers every function in TOOLS with
FastMCP (this module is listed in kicad_server.TOOL_MODULES). A new tool is
added by appending it to TOOLS — kicad_server.py is not edited.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, Callable

import toolchain as tc

VERDICT_PREFIX = "- **Verdict:"              # review_report._verdict's line
OPEN_ITEMS_HEADING = "## Open items"
_OPEN_ITEM = re.compile(r"\d+\. ")           # review_report numbers each open item


def _tools_on_path() -> None:
    tools = str(tc.TOOLS_DIR)
    if tools not in sys.path:
        sys.path.insert(0, tools)


def _netlist_problem(netlist: Any) -> str | None:
    if not isinstance(netlist, str) or not netlist.strip():
        return f"netlist must be the path of the design's netlist.json, got {netlist!r}"
    if Path(netlist).is_dir():
        return f"netlist must be the netlist.json path, not a directory: {netlist}"
    return None


# --- record_decision ----------------------------------------------------------------
def record_decision(netlist: str, stage: str, question: str, decision: str, rationale: str,
                    confidence: str, source: str, alternatives: list[str] | None = None,
                    reversible: bool = True, override: str | None = None) -> dict:
    """Record one runtime decision a hands-off (napkin) run took instead of
    asking the user, in the append-only log <netlist stem>.decisions.jsonl
    beside the netlist (the review report lists it). netlist: the
    netlist.json path (its directory must exist). stage: requirements |
    parts | G1 | G2 | G3 | G4 | G5 | routing. question: what a human would
    have been asked; decision: the answer taken; rationale: why; confidence:
    high | med | low; source: a policy id, datasheet URL or "user".
    alternatives: the options not taken (None = none recorded). reversible:
    whether a re-run can undo it. override: the netlist path a human edits to
    change it (e.g. "components[U3].footprint"), or None. Returns {ok, entry,
    log}, or {ok: false, reason} for an invalid field (nothing is written),
    a missing directory, a log held by another writer for 10 s, or a write
    error."""
    problem = _netlist_problem(netlist)
    if problem:
        return {"ok": False, "reason": problem}
    folder = Path(netlist).parent
    if not folder.is_dir():
        return {"ok": False, "reason": f"the netlist's directory {folder} does not exist (the "
                                       f"decision log is written beside the netlist)"}
    _tools_on_path()
    import decisions as dec
    try:
        entry = dec.append_decision(netlist, stage=stage, question=question, decision=decision,
                                    rationale=rationale, confidence=confidence, source=source,
                                    alternatives=alternatives, reversible=reversible,
                                    override=override)
    except (dec.DecisionError, OSError) as e:
        return {"ok": False, "reason": str(e)}
    return {"ok": True, "entry": entry, "log": str(dec.decisions_path(netlist))}


# --- review_report: inputs ------------------------------------------------------------
def _read_json(path: Path, what: str) -> tuple[Any, str | None]:
    """(the JSON value, None) or (None, why it could not be read)."""
    try:
        return json.loads(path.read_text(encoding="utf-8-sig")), None
    except (OSError, ValueError) as e:
        return None, f"cannot read the {what} {path}: {e}"


def _pipeline_report(netlist: str, suffix: str) -> tuple[dict | None, Path | None, str | None]:
    """(report, its path or None, error): the harness's report read-only, or
    {netlist, gates: []} when there is none (the harness rule for the path:
    the netlist path without its extension + suffix)."""
    path = Path(str(Path(netlist).with_suffix("")) + suffix)
    if not path.is_file():
        return {"netlist": netlist, "gates": []}, None, None
    report, error = _read_json(path, "pipeline report")
    if error:
        return None, path, error
    if not isinstance(report, dict):
        return None, path, f"the pipeline report {path} is not a JSON object"
    return (report if report.get("netlist") else {**report, "netlist": netlist}), path, None


def _stock_dirs(notes: list[str]) -> tuple[str | None, str | None]:
    """The stock symbol / footprint dirs of the installs the pipeline uses."""
    try:
        dirs = tc.symbols_dir() or None, tc.footprints_dir() or None
    except Exception as e:                       # install discovery must not stop a review
        notes.append(f"stock libraries unavailable ({type(e).__name__}: {e}): stock parts "
                     f"read as unknown and the design analysis has no pin types")
        return None, None
    for kind, found in zip(("symbol", "footprint"), dirs, strict=True):
        if found is None:
            notes.append(f"no stock {kind} library directory found (no KiCad install): stock "
                         f"{kind} libraries read as unknown")
    return dirs


def _analysis(design: dict, symbols_dir: str | None, notes: list[str]) -> dict | None:
    import design_report as dr
    try:
        return dr.analyze(design, symbols_dir or "")
    except Exception as e:                       # guarded: the review renders without it
        notes.append(f"design analysis failed ({type(e).__name__}: {e}): the review has no "
                     f"DFA line and no design-review findings")
        return None


def _library_sources(design: dict, folder: Path, dirs: tuple[str | None, str | None],
                     notes: list[str]) -> tuple[dict | None, str | None]:
    import libtable
    import review_sources
    try:
        return review_sources.library_sources(design, folder, *dirs), None
    except (libtable.LibTableError, OSError) as e:
        notes.append(f"library sources could not be resolved: {e}")
        return None, str(e)


def _decision_log(netlist: str, notes: list[str]) -> tuple[list[dict], list[str]]:
    import decisions as dec
    try:
        log = dec.load_decisions(netlist)
    except OSError as e:
        notes.append(f"the decision log could not be read: {e}")
        return [], [f"{dec.decisions_path(netlist)}: cannot read the decision log: {e}"]
    if log.errors:
        notes.append(f"{len(log.errors)} malformed decision log line(s) (listed as open items)")
    return list(log.entries), list(log.errors)


# --- review_report: outputs -----------------------------------------------------------
def _verdict(markdown: str) -> str:
    line = next((ln for ln in markdown.splitlines() if ln.startswith(VERDICT_PREFIX)), "")
    return line[2:] if line else "(no verdict line)"


def _open_item_count(markdown: str) -> int:
    lines = markdown.splitlines()
    start = lines.index(OPEN_ITEMS_HEADING) + 1 if OPEN_ITEMS_HEADING in lines else len(lines)
    section = []
    for line in lines[start:]:
        if line.startswith("## "):
            break
        section.append(line)
    return sum(bool(_OPEN_ITEM.match(line)) for line in section)


def _render(netlist: str, design: dict, report: dict, report_path: Path | None,
            review_md: ModuleType) -> dict:
    """Render and write the review; the tool result (ok False on a render or
    write error, nothing written)."""
    notes: list[str] = []
    if report_path is None:
        notes.append("no pipeline report beside the netlist: the review is rendered against "
                     "an empty gate list (run the pipeline harness for gate verdicts)")
    dirs = _stock_dirs(notes)
    analysis = _analysis(design, dirs[0], notes)
    sources, sources_error = _library_sources(design, Path(netlist).parent, dirs, notes)
    entries, errors = _decision_log(netlist, notes)
    try:
        markdown = review_md.render_review(report, entries, design, analysis, sources,
                                           decision_errors=errors)
    except (ValueError, TypeError, KeyError, AttributeError) as e:    # malformed input data
        what = report_path if report_path is not None else netlist
        return {"ok": False, "reason": f"cannot render the review from {what}: "
                                       f"{type(e).__name__}: {e}"}
    try:
        out = review_md.write_review(netlist, markdown)
    except OSError as e:
        return {"ok": False, "reason": f"cannot write the review "
                                       f"{review_md.review_path(netlist)}: {e}"}
    flags = review_md.detect_safety(design)
    return {"ok": True, "review": str(out),
            "pipeline_report": None if report_path is None else str(report_path),
            "verdict": _verdict(markdown),
            "safety_flags": [{"kind": f.kind, "reason": f.reason} for f in flags],
            "open_items": _open_item_count(markdown), "decisions": len(entries),
            "decision_errors": errors, "library_sources_error": sources_error, "notes": notes}


def review_report(netlist: str) -> dict:
    """Write the human review of a design, <netlist stem>_review.md beside the
    netlist: verdict, requirements assumptions, the runtime decisions grouped
    by stage (low confidence first), every part with its library source
    (stock | project | generated | unknown), gate verdicts, remediations,
    accepted risks, routing, board file, renders, fab package and the open
    items a human must close; HUMAN REVIEW REQUIRED on top for mains / > 50 V
    or Li-ion charging designs. It reads <stem>_pipeline_report.json when the
    pipeline harness left one (never modifies it); otherwise the review has
    no gate verdicts. Returns {ok, review (path), pipeline_report (path or
    None), verdict, safety_flags, open_items (count), decisions,
    decision_errors, library_sources_error, notes}, or {ok: false, reason}
    when the netlist or the pipeline report cannot be read."""
    problem = _netlist_problem(netlist)
    if problem:
        return {"ok": False, "reason": problem}
    _tools_on_path()
    import review_report as review_md        # aliased: this tool shares the module's name
    design, error = _read_json(Path(netlist), "netlist")
    if error:
        return {"ok": False, "reason": error}
    if not isinstance(design, dict):
        return {"ok": False, "reason": f"the netlist {netlist} is not a JSON object"}
    report, report_path, error = _pipeline_report(netlist, review_md.REPORT_SUFFIX)
    if error:
        return {"ok": False, "reason": f"{error} (the file is left untouched)"}
    return _render(netlist, design, report, report_path, review_md)


TOOLS: tuple[Callable[..., Any], ...] = (record_decision, review_report)
