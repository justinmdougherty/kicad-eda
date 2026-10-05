#!/usr/bin/env python3
"""The human review report of a hands-off run: ``<stem>_review.md`` (ROADMAP D5).

``render_review(pipeline_report, decisions, design, analysis, library_sources)``
renders the D5 sections (SECTIONS): assumptions and decisions low confidence
first, decisions grouped by stage, parts with their library source (stock |
project | generated | unknown), gate verdicts via ``gates.summarize``,
remediations, accepted risks, routing method and why, board format and
pre-KiCad 11 snapshot, renders, fab zip, open items. ``pipeline_report`` is
run_pipeline's report; optional keys it may grow: ``routing`` {"method",
"requested", "why", "handoff"}, ``board_file`` (toolchain.board_file_info()
plus "path"), ``pre_k11_snapshot``. ``analysis`` is design_report.analyze()
output (DFA line; its findings stand in for a missing G2 verdict).
``library_sources`` maps ref -> source or ref -> {"symbol", "footprint"}
(``library_sources_for`` builds it; libtable's "netlist" means "project");
without it the column reads "not supplied" (nothing was looked up, which
is not "unknown": a library that resolved nowhere).

Safety-relevant designs (mains / > 50 V, Li-ion charging) open with **HUMAN
REVIEW REQUIRED**; the screen lives in safety_screen (re-exported here:
``detect_safety`` documents every heuristic, ``safety_warnings`` gives the
G5 warning lines). Every rendered line is one Markdown line: report text
(gate warnings carry stderr tails) is flattened, so it cannot add headings
or close a code fence. Pure: stdlib + gates + decisions + safety_screen, no
KiCad. CLI (regenerates a review from a finished run's files; it resolves
no libraries, so the parts table says "not supplied"):
  <python> review_report.py <netlist.json>   -> writes <stem>_review.md
"""
from __future__ import annotations

import json
import os
import posixpath
import re
import sys
import urllib.parse
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, NamedTuple, Sequence

import decisions as dec
import gates
from safety_screen import (HUMAN_REVIEW, SAFETY_LABELS, SAFETY_LIION, SAFETY_MAINS,  # noqa: F401
                           SafetyFlag, detect_safety, safety_lines, safety_warnings)

SECTIONS: tuple[str, ...] = (
    "Summary", "Assumptions", "Decisions", "Parts", "Gate verdicts", "Auto-remediations",
    "Accepted risks and warnings", "Routing", "Board file", "Renders", "Fab package",
    "Open items")
GATE_ORDER: tuple[str, ...] = ("G1", "G2", "G3", "G4", "G5")
GATE_KEYS: tuple[str, ...] = ("gate", "name", "feeds", "passed", "blockers", "warnings")
LIBRARY_SOURCES: tuple[str, ...] = ("stock", "project", "generated", "unknown")
NOT_SUPPLIED = "not supplied"      # the column without library_sources (nothing looked up)
SOURCE_ALIASES: Mapping[str, str] = {"netlist": "project"}       # libtable.NETLIST
# a part's summary source is its least-trusted library (count + open items)
SOURCE_PRIORITY: tuple[str, ...] = ("unknown", "generated", "project", "stock")
REPORT_SUFFIX = "_pipeline_report.json"                         # run_pipeline.REPORT_SUFFIX
REVIEW_SUFFIX = "_review.md"
K11_WRITER = (10, 99)              # boards written by KiCad >= 10.99 are KiCad 11 format
OPEN_PREFIX = "OPEN:"              # run_pipeline's prefix for an unfixed board-fix finding

# --- formatting helpers ------------------------------------------------------------
def _flat(value: Any) -> str:
    """``value`` as one line: every run of whitespace (newlines, U+2028, ...)
    becomes one space; non-strings are shown as JSON."""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return " ".join(text.split())


def _cell(value: Any) -> str:
    return _flat(value).replace("|", "\\|") or "-"


def _longest_ticks(text: str) -> int:
    return max(map(len, re.findall(r"`+", text)), default=0)


def _code(value: Any, cell: bool = False) -> str:
    """Inline code that survives backticks in the text (and pipes, in a
    table ``cell``)."""
    text = _flat(value).replace("|", "\\|") if cell else _flat(value)
    ticks = "`" * (_longest_ticks(text) + 1)
    pad = " " if text[:1] == "`" or text[-1:] == "`" else ""
    return f"{ticks}{pad}{text}{pad}{ticks}"


def _one_line(line: str) -> str:
    """Markdown ends a line at \\n or \\r: no rendered line may add one."""
    return line.replace("\r", " ").replace("\n", " ")


def _dicts(items: Any) -> list[Mapping]:
    return [i for i in items if isinstance(i, Mapping)] if isinstance(items, list) else []


def _table(header: Sequence[str], rows: Iterable[Sequence[str]]) -> list[str]:
    out = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    return out + ["| " + " | ".join(row) + " |" for row in rows]


def _natural(ref: str) -> tuple:
    m = re.match(r"([A-Za-z_]*)(\d*)(.*)", ref)
    prefix, number, rest = m.groups() if m else (ref, "", "")
    return prefix, int(number) if number else -1, rest


_ANCHOR = re.compile(r"(?:([A-Za-z]):)?(/*)")


def _path_parts(path: str) -> tuple[str, list[str]]:
    """(anchor, segments) of a POSIX or Windows path with either separator:
    anchor "/" (rooted), "C:/" (drive, upper-cased) or "" (relative)."""
    text = path.replace("\\", "/")
    m = _ANCHOR.match(text)
    anchor = (f"{m.group(1).upper()}:" if m.group(1) else "") + ("/" if m.group(2) else "")
    rest = posixpath.normpath(text[m.end():] or ".")
    segments = [] if rest == "." else rest.split("/")
    while anchor.endswith("/") and segments[:1] == [".."]:      # nothing is above a root
        segments = segments[1:]
    return anchor, segments


def _render_link(png: str, netlist: str) -> str | None:
    """The link to a render from the review, which sits beside the netlist,
    percent-encoded: relative when both paths share an anchor (whatever their
    separators), a file:// URI for a rooted path. None when a relative path
    cannot be placed: it is relative to the run's working directory, which
    an absolute netlist path, a '..' left in the review folder or a missing
    netlist path does not reveal (any link would name the wrong file)."""
    anchor, target = _path_parts(png)
    netlist_anchor, netlist_parts = _path_parts(netlist)
    folder, k = netlist_parts[:-1], 0
    while (k < min(len(folder), len(target) - 1)
           and os.path.normcase(folder[k]) == os.path.normcase(target[k])):
        k += 1
    if netlist and anchor == netlist_anchor and ".." not in folder[k:]:
        link = "/".join([".."] * (len(folder) - k) + target[k:])
    elif anchor.endswith("/"):
        link = "file://" + ("" if anchor.startswith("/") else "/") + anchor + "/".join(target)
    else:
        return None
    return urllib.parse.quote(link, safe="/:")


def _stem(netlist: str) -> str:
    for suffix in dec.STEM_SUFFIXES:
        if netlist.endswith(suffix):
            return netlist[:-len(suffix)]
    return netlist


def _yes(flag: Any) -> str:
    return "yes" if flag else "no"


# --- input checks --------------------------------------------------------------------
def _check_report(report: Any) -> list[Mapping]:
    if not isinstance(report, Mapping):
        raise ValueError(f"the pipeline report must be a JSON object, got {type(report).__name__}")
    gate_list = report.get("gates", [])
    if not isinstance(gate_list, list):
        raise ValueError("the pipeline report's 'gates' must be a list of gate verdicts")
    for i, g in enumerate(gate_list):
        missing = [k for k in GATE_KEYS if not isinstance(g, Mapping) or k not in g]
        if missing:
            raise ValueError(f"pipeline report gates[{i}] is not a gate verdict "
                             f"(missing {', '.join(missing)})")
        for key in ("blockers", "warnings"):
            if not isinstance(g[key], list):
                raise ValueError(f"pipeline report gates[{i}].{key} must be a list of strings")
        for j, task in enumerate(g.get("tasks") or []):
            if not isinstance(task, Mapping) or "desc" not in task or "auto" not in task:
                raise ValueError(f"pipeline report gates[{i}].tasks[{j}] needs 'desc' and 'auto'")
    return [_flat_gate(g) for g in gate_list]


def _flat_gate(g: Mapping) -> dict:
    """A copy of a gate verdict with its text on one line (a G4 warning can
    carry a multi-line stderr tail)."""
    return {**g, "name": _flat(g["name"]), "feeds": _flat(g["feeds"]),
            "blockers": [_flat(b) for b in g["blockers"]],
            "warnings": [_flat(w) for w in g["warnings"]],
            "tasks": [{**t, "desc": _flat(t["desc"])} for t in g.get("tasks") or []]}


def _normal_source(ref: str, value: Any) -> str:
    if value is None:
        return "unknown"
    source = SOURCE_ALIASES.get(value, value) if isinstance(value, str) else value
    if source not in LIBRARY_SOURCES:
        raise ValueError(f"library source for {ref} must be one of {', '.join(LIBRARY_SOURCES)} "
                         f"(or 'netlist'), got {value!r}")
    return source


def _part_sources(ref: str, value: Any) -> dict[str, str]:
    if isinstance(value, Mapping):
        unknown = sorted(set(value) - {"symbol", "footprint"})
        if unknown:
            raise ValueError(f"library source for {ref} has unknown keys {unknown} "
                             f"(allowed: symbol, footprint)")
        return {k: _normal_source(ref, v) for k, v in value.items()}
    source = _normal_source(ref, value)
    return {"symbol": source, "footprint": source}


def library_sources_for(design: Mapping,
                        resolve: Callable[[str, str], str | None]) -> dict[str, dict[str, str]]:
    """{ref: {"symbol": source, "footprint": source}} for render_review.
    ``resolve(kind, nickname)`` (kind "symbol" | "footprint", libtable's
    vocabulary) returns stock | project | netlist | generated, or None when
    the library resolves nowhere (shown as "unknown"). A board-only part
    without a lib_id gets only a footprint source."""
    out: dict[str, dict[str, str]] = {}
    for comp in _dicts(design.get("components")):
        ref, entry = str(comp.get("ref", "?")), {}
        for kind, key in (("symbol", "lib_id"), ("footprint", "footprint")):
            lib_ref = comp.get(key)
            if isinstance(lib_ref, str) and ":" in lib_ref:
                entry[kind] = _normal_source(ref, resolve(kind, lib_ref.split(":", 1)[0]))
        out[ref] = entry
    return out


# --- the report model -------------------------------------------------------------
class _Review(NamedTuple):
    report: Mapping
    gates: list[Mapping]
    design: Mapping
    analysis: Mapping | None
    decisions: list[dict]
    bad_decisions: list[str]
    sources: dict[str, dict[str, str]] | None
    flags: list[SafetyFlag]
    netlist: str


def _decision_split(entries: Any, extra_errors: Sequence[str]) -> tuple[list[dict], list[str]]:
    if not isinstance(entries, (list, tuple)):
        raise ValueError(f"decisions must be a list of entries, got {type(entries).__name__}")
    checked = [(entry, dec.validate_decision(entry)) for entry in entries]
    bad = [str(e) for e in extra_errors] + [
        f"decision entry {i}: {'; '.join(problems)}"
        for i, (_, problems) in enumerate(checked, 1) if problems]
    return [entry for entry, problems in checked if not problems], bad


def _not_reached(gate_list: Sequence[Mapping]) -> list[str]:
    ran = {g["gate"] for g in gate_list}
    return [g for g in GATE_ORDER if g not in ran]


def _passed(r: _Review) -> bool:
    if "PASS" in r.report:
        return bool(r.report["PASS"])
    return (not r.report.get("error") and bool(r.gates) and all(g["passed"] for g in r.gates))


def _part_label(sources: Mapping[str, str]) -> str:
    values = list(dict.fromkeys(sources.values()))
    return values[0] if len(values) == 1 else (
        ", ".join(f"{k} {v}" for k, v in sources.items()) or "unknown")


def _part_summary_source(sources: Mapping[str, str]) -> str:
    return next((s for s in SOURCE_PRIORITY if s in sources.values()), "unknown")


# --- sections ------------------------------------------------------------------------
def _banner(r: _Review) -> list[str]:
    if not r.flags:
        return []
    return ([f"> **{HUMAN_REVIEW}**: this design is safety-relevant and was built hands-off. "
             f"A qualified person must review it before it is powered, connected or ordered.",
             ">"] + [f"> - {SAFETY_LABELS[f.kind]}: {f.reason}" for f in r.flags] + [""])


def _verdict(r: _Review) -> str:
    error = r.report.get("error")
    if error:
        return f"- **Verdict: FAIL**: the run crashed: {_flat(error)}"
    missing = _not_reached(r.gates)
    tail = f" Not reached: {', '.join(missing)}." if missing else ""
    failed = next((g for g in r.gates if not g["passed"]), None)
    if failed is not None:
        return (f"- **Verdict: FAIL**: stopped at {failed['gate']} {failed['name']} with "
                f"{len(failed['blockers'])} blocker(s).{tail}")
    if not r.gates:
        return "- **Verdict: FAIL**: no gate ran."
    if _passed(r):
        return (f"- **Verdict: PASS**: all {len(r.gates)} gates passed "
                f"({r.gates[0]['gate']}-{r.gates[-1]['gate']}).{tail}")
    return f"- **Verdict: FAIL**: the report says PASS is false.{tail}"


def _summary(r: _Review) -> list[str]:
    lines = [_verdict(r), f"- Netlist: {_code(r.netlist) if r.netlist else '(not recorded)'}"]
    if r.report.get("pipeline_report"):
        lines.append(f"- Pipeline report: {_code(str(r.report['pipeline_report']))}")
    mode = r.design.get("mode")
    lines.append("- Mode: napkin (hands-off)" if mode == "napkin" else f"- Mode: {_flat(mode)}"
                 if mode else "- Mode: not set (the pipeline harness defaults to napkin "
                              "behaviour, D5)")
    comps, nets = _dicts(r.design.get("components")), r.design.get("nets") or {}
    cls = r.design.get("class")
    lines.append(f"- Design: {len(comps)} components, {len(nets)} nets"
                 + (f", IPC class {cls}" if cls is not None else ""))
    sch = r.report.get("schematic")
    if isinstance(sch, Mapping):
        lines.append(f"- Schematic: {_code(_stem(r.netlist) + '.kicad_sch')} (backend "
                     f"{sch.get('backend')}, ERC {sch.get('erc_violations')} violation(s))")
    return lines


def _assumptions(r: _Review) -> list[str]:
    block = r.design.get("assumptions")
    if not block:
        return ['No requirements-level assumptions recorded (the netlist has no "assumptions" '
                'block).']
    entries = [a for a in _dicts(block) if not dec.validate_assumption(a)]
    entries.sort(key=lambda a: dec.CONFIDENCE_RANK[a["confidence"]])
    rows = [(a["confidence"], _cell(a["id"]), _cell(a["topic"]), _cell(a["value"]),
             _cell(a["rationale"]), _code(a["override"], cell=True) if a.get("override") else "-")
            for a in entries]
    lines = _table(("Confidence", "ID", "Topic", "Value", "Rationale", "Override"), rows)
    problems = dec.validate_assumptions(block)
    if problems:
        lines += ["", f"{len(problems)} problem(s) in the assumptions block (see Open items)."]
    return lines


def _decision_item(e: Mapping) -> list[str]:
    alts = "; ".join(_flat(a) for a in e["alternatives"]) or "none recorded"
    override = _code(e["override"]) if e["override"] else "none"
    return [f"- **[{e['confidence']}]** {_flat(e['decision'])}",
            f"  - Question: {_flat(e['question'])}",
            f"  - Rationale: {_flat(e['rationale'])}",
            f"  - Alternatives: {alts}",
            f"  - Source: {_flat(e['source'])}. Reversible: {_yes(e['reversible'])}. "
            f"Override: {override}",
            f"  - {e['id']} at {e['ts']}"]


def _decisions(r: _Review) -> list[str]:
    lines: list[str] = []
    if r.decisions:
        counts = {c: sum(e["confidence"] == c for e in r.decisions) for c in ("low", "med", "high")}
        lines.append(f"{len(r.decisions)} decision(s): "
                     + ", ".join(f"{n} {c}" for c, n in counts.items()) + " confidence.")
        for stage in dec.STAGES:
            group = [e for e in r.decisions if e["stage"] == stage]
            if not group:
                continue
            group.sort(key=lambda e: (dec.CONFIDENCE_RANK[e["confidence"]], e["ts"]))
            lines += ["", f"### {stage}", ""]
            for entry in group:
                lines += _decision_item(entry)
    elif r.bad_decisions:
        lines.append("No valid runtime decisions.")
    else:
        lines.append("No runtime decisions recorded (the decision log is empty or absent).")
    if r.bad_decisions:
        lines += ["", f"{len(r.bad_decisions)} malformed log entry(ies) not shown "
                      f"(see Open items)."]
    return lines


def _parts(r: _Review) -> list[str]:
    comps = sorted(_dicts(r.design.get("components")), key=lambda c: _natural(str(c.get("ref"))))
    if not comps:
        return ["No components (the netlist was not loaded or is empty)."]
    rows, tally = [], {}
    for c in comps:
        ref = str(c.get("ref", "?"))
        sources = (r.sources or {}).get(ref) or {"symbol": "unknown", "footprint": "unknown"}
        summary = _part_summary_source(sources)
        tally[summary] = tally.get(summary, 0) + 1
        sourcing = " / ".join(str(c[k]) for k in ("lcsc", "mpn") if c.get(k)) or "-"
        symbol = _code(str(c["lib_id"]), cell=True) if c.get("lib_id") else "(board only)"
        rows.append((_cell(ref), _cell(c.get("value", "")), symbol,
                     _code(str(c.get("footprint", "")), cell=True), _cell(sourcing),
                     NOT_SUPPLIED if r.sources is None else _part_label(sources)))
    lines = _table(("Ref", "Value", "Symbol", "Footprint", "LCSC / MPN", "Library source"), rows)
    lines.append("")
    if r.sources is None:
        lines.append("Library sources were not supplied to the report.")
    else:
        lines.append("Library sources: " + ", ".join(
            f"{s} {tally[s]}" for s in reversed(SOURCE_PRIORITY) if tally.get(s)) + ".")
    dfa = (r.analysis or {}).get("dfa")
    if isinstance(dfa, Mapping):
        lines.append(f"DFA: {dfa.get('placements')} placements, "
                     f"{dfa.get('unique_line_items')} unique line items.")
    return lines


def _gate_verdicts(r: _Review) -> list[str]:
    if not r.gates:
        return ["No gate ran."]
    summary = gates.summarize(r.gates)          # r.gates are flattened: one line per item
    fence = "`" * max(3, _longest_ticks(summary) + 1)   # no backtick run inside can close it
    lines = [fence + "text", *summary.split("\n"), fence]
    missing = _not_reached(r.gates)
    return lines + (["", f"Not reached: {', '.join(missing)}."] if missing else [])


def _remediations(r: _Review) -> list[str]:
    lines = []
    for rnd in _dicts(r.report.get("remediation")):
        for item in rnd.get("applied") or []:
            text = _flat(item)
            if text.startswith(OPEN_PREFIX):
                text = "OPEN (not fixed): " + text[len(OPEN_PREFIX):].strip()
            lines.append(f"- {rnd.get('gate')} round {rnd.get('round')}: {text}")
    return lines or ["None: no gate needed a corrective action."]


def _analysis_stands_in(r: _Review) -> bool:
    """design_report findings are reported only when no G2 verdict carries
    them already (the conversational path)."""
    return r.analysis is not None and not any(g["gate"] == "G2" for g in r.gates)


def _analysis_warnings(r: _Review) -> list[str]:
    if not _analysis_stands_in(r):
        return []
    flags = r.analysis.get("flags") or {}
    return ([f"net '{n}' may need a pull resistor" for n in flags.get("nets_missing_pull", [])]
            + [f"power rail '{n}' has no test point for bring-up"
               for n in flags.get("rails_missing_testpoint", [])])


def _risks(r: _Review) -> list[str]:
    lines = [f"- {g['gate']}: {w}" for g in r.gates for w in g["warnings"]]
    lines += [f"- design review: {w}" for w in _analysis_warnings(r)]
    if r.flags:
        lines.append(f"- Safety-relevant design built anyway (D5): see {HUMAN_REVIEW} at the top.")
    return lines or ["None: no gate raised a warning."]


def _routing(r: _Review) -> list[str]:
    routing = r.report.get("routing") if isinstance(r.report.get("routing"), Mapping) else None
    auto = r.report.get("autoroute") if isinstance(r.report.get("autoroute"), Mapping) else None
    if routing is None and auto is None:
        return ["- Not reached: the run stopped before routing."]
    lines = []
    if routing is not None:
        requested = routing.get("requested")
        lines.append(f"- Method: {_flat(routing.get('method', '(not recorded)'))}"
                     + (f" (requested: {_flat(requested)})" if requested else ""))
        if routing.get("why"):
            lines.append(f"- Why: {_flat(routing['why'])}")
        if routing.get("handoff"):
            lines.append(f"- Handoff report: {_code(str(routing['handoff']))}")
    elif auto.get("cached"):
        lines.append("- Method: route cache hit (the routed board of an earlier run was reused: "
                     "nothing that moves copper changed)")
    else:
        lines.append("- Method: Freerouting (the harness router; the report records no routing "
                     "method, so the reason is the harness default: deterministic, no tokens)")
    if auto is not None:
        what = "cached board" if auto.get("cached") else f"attempt {auto.get('attempt')}"
        lines.append(f"- Result: {what} at pack_scale {auto.get('pack_scale')}: "
                     f"{auto.get('tracks')} track(s), {auto.get('unconnected')} unconnected"
                     + (f"; error: {_flat(auto['error'])}" if auto.get("error") else ""))
    if r.report.get("route_cache"):
        reason = r.report.get("route_cache_reason")
        lines.append(f"- Route cache: {r.report['route_cache']}"
                     + (f" ({_flat(reason)})" if reason else ""))
    lines += [f"- Decision ({e['confidence']}): {_flat(e['decision'])}"
              for e in r.decisions if e["stage"] == "routing"]
    drc = r.report.get("drc")
    if isinstance(drc, Mapping):
        types = ", ".join(f"{k} x{v}" for k, v in (drc.get("types") or {}).items())
        lines.append(f"- DRC: {drc.get('errors')} error(s), {drc.get('unconnected')} unconnected "
                     f"item(s)" + (f" [{types}]" if types else "")
                     + (f" ({_code(str(drc['kicad_cli']))})" if drc.get("kicad_cli") else ""))
    emc = r.report.get("emc")
    if isinstance(emc, Mapping):
        lines.append(f"- EMC stitching: {emc.get('vias_added')} GND via(s)" if emc.get("ok")
                     else f"- EMC stitching failed: {_flat(emc.get('reason'))}")
    return lines


def _board_info(r: _Review) -> tuple[Mapping, bool | None]:
    """(board_file info, written by KiCad >= 10.99?), None when unrecorded."""
    info = r.report.get("board_file") if isinstance(r.report.get("board_file"), Mapping) else {}
    m = re.match(r"(\d+)\.(\d+)", str(info.get("generator_version") or ""))
    return info, (None if m is None else (int(m.group(1)), int(m.group(2))) >= K11_WRITER)


def _board_file(r: _Review) -> list[str]:
    info, k11 = _board_info(r)
    path = str(info.get("path") or (_stem(r.netlist) + ".kicad_pcb"))
    built = "board" in r.report or bool(info)
    lines = [f"- Board: {_code(path)}"
             + ("" if built else " (not built: the run stopped before placement)")]
    if k11 is None:
        lines.append("- Format: not recorded in the pipeline report")
    else:
        dialect = ("KiCad 11 dialect: KiCad 9/10 and the SWIG scripts cannot open it" if k11
                   else "pre-KiCad 11 dialect: the SWIG scripts and KiCad 9/10 can open it")
        lines.append(f"- Format: {info.get('format_version')}, written by "
                     f"{info.get('generator')} {info.get('generator_version')} ({dialect})")
    snapshot = r.report.get("pre_k11_snapshot")
    lines.append(f"- Pre-KiCad 11 snapshot: {_code(str(snapshot))}" if snapshot else
                 "- Pre-KiCad 11 snapshot: none needed (the board is in the pre-KiCad 11 "
                 "dialect)" if k11 is False else "- Pre-KiCad 11 snapshot: none recorded")
    if r.report.get("agent_board_preserved"):
        lines.append(f"- Agent-routed KiCad 11 board preserved as "
                     f"{_code(str(r.report['agent_board_preserved']))}")
    return lines


def _basename(path: str) -> str:
    return (_path_parts(path)[1] or [""])[-1]


def _renders(r: _Review) -> list[str]:
    lines = []
    for key, tag in (("renders_placement", "placement"), ("renders_final", "final")):
        for png in r.report.get(key) or []:
            side = _basename(str(png)).rsplit(".", 1)[0].rsplit("_", 1)[-1]
            link = _render_link(str(png), r.netlist)
            lines.append(f"![{tag}, {side}]({link})" if link else
                         f"- {tag}, {side}: {_code(str(png))} (recorded relative to the run's "
                         f"working directory, which the review cannot locate)")
    return lines or ["- None: the run stopped before placement, or rendering failed."]


def _fab(r: _Review) -> list[str]:
    fab = r.report.get("fab")
    if not isinstance(fab, Mapping):
        return ["- Not produced: the run stopped before G5."]
    lines = [f"- Zip: {_code(str(fab.get('zip') or '(none)'))} ({fab.get('files')} file(s), "
             f"BOM {_yes(fab.get('bom'))}, CPL {_yes(fab.get('cpl'))})"]
    if not fab.get("ok"):
        lines.append("- Export FAILED (see G5)")
    src = r.report.get("sourcing")
    if isinstance(src, Mapping):
        missing = sorted((str(m) for m in src.get("missing") or []), key=_natural)
        lines.append(f"- Sourcing: {len(missing)} part(s) without an LCSC number or MPN: "
                     f"{', '.join(missing)}" if missing else
                     "- Sourcing: every part has an LCSC number or MPN")
        lines += [f"- Sourcing problem: {_flat(b)}" for b in src.get("bad") or []]
    return lines


def _gate_open_items(r: _Review) -> list[str]:
    items = []
    for g in r.gates:
        if g["passed"]:
            continue
        items += [f"{g['gate']} blocker: {b}" for b in g["blockers"]]
        items += [f"{g['gate']} task [{'auto' if t['auto'] else 'designer'}]: {t['desc']}"
                  for t in g.get("tasks") or []]
    missing = _not_reached(r.gates)
    if missing and r.gates:
        items.append(f"Gates not reached: {', '.join(missing)}")
    for rnd in _dicts(r.report.get("remediation")):
        items += [f"Unfixed {rnd.get('gate')} issue: {_flat(i)[len(OPEN_PREFIX):].strip()}"
                  for i in rnd.get("applied") or [] if _flat(i).startswith(OPEN_PREFIX)]
    return items


def _analysis_open_items(r: _Review) -> list[str]:
    if not _analysis_stands_in(r):
        return []
    items = [f"Design review could not run: {e}" for e in r.analysis.get("errors") or []]
    for u in (r.analysis.get("decoupling") or {}).get("uncovered", []):
        items += [f"{u['ref']}: rail '{rail}' has no bypass cap" for rail in u["rails_without_cap"]]
    items += [f"Floating pin (not netted, not no_connect): {p}"
              for p in (r.analysis.get("flags") or {}).get("floating_pins", [])]
    return items


def _confirm_items(r: _Review) -> list[str]:
    block = r.design.get("assumptions")
    items = [f"Confirm low-confidence assumption {a['id']} ({a['topic']}): {_flat(a['value'])}"
             for a in _dicts(block) if not dec.validate_assumption(a) and a["confidence"] == "low"]
    items += [f"Confirm low-confidence decision ({e['stage']}): {_flat(e['decision'])}"
              for e in r.decisions if e["confidence"] == "low"]
    items += [f"Fix the assumptions block: {p}" for p in dec.validate_assumptions(block)]
    items += [f"Fix the decision log: {p}" for p in r.bad_decisions]
    if r.design.get("mode") == "napkin" and not block:
        items.append("Record the requirements assumptions: the design is in napkin mode but the "
                     "netlist records no assumptions block")
    return items


def _supply_items(r: _Review) -> list[str]:
    items = []
    src = r.report.get("sourcing")
    if isinstance(src, Mapping) and src.get("missing"):
        missing = sorted((str(m) for m in src["missing"]), key=_natural)
        items.append(f"Add an LCSC number or MPN for {len(missing)} part(s) before ordering "
                     f"assembly: {', '.join(missing)}")
    comps = _dicts(r.design.get("components"))
    if r.sources is None and comps:
        items.append("Library sources were not supplied: the parts table cannot tell stock, "
                     "project and generated libraries apart")
    unknown = sorted((str(c.get("ref")) for c in comps if r.sources is not None and
                      _part_summary_source(r.sources.get(str(c.get("ref"))) or {}) == "unknown"),
                     key=_natural)
    if unknown:
        items.append(f"Confirm the library of {len(unknown)} part(s) with an unknown source: "
                     f"{', '.join(unknown)}")
    if _board_info(r)[1] and not r.report.get("pre_k11_snapshot"):
        items.append("The board is in KiCad 11 format and no pre-KiCad 11 snapshot was recorded: "
                     "KiCad 9/10 and the SWIG gate scripts cannot reopen it")
    return items


def _open_items(r: _Review) -> list[str]:
    items = [f"{HUMAN_REVIEW}: {line}" for line in safety_lines(r.flags)]
    if r.report.get("error"):
        items.append(f"Run error: {_flat(r.report['error'])}")
    items += _gate_open_items(r)
    for key, what in (("pipeline_report_error", "Pipeline report not written"),
                      ("route_cache_error", "Route cache manifest not written")):
        if r.report.get(key):
            items.append(f"{what}: {_flat(r.report[key])}")
    items += _analysis_open_items(r) + _confirm_items(r) + _supply_items(r)
    return [f"{i}. {_flat(item)}" for i, item in enumerate(items, 1)] or ["- None."]


_RENDERERS: tuple[Callable[[_Review], list[str]], ...] = (
    _summary, _assumptions, _decisions, _parts, _gate_verdicts, _remediations, _risks,
    _routing, _board_file, _renders, _fab, _open_items)


def render_review(pipeline_report: Mapping, decisions: Sequence[Mapping], design: Mapping,
                  analysis: Mapping | None = None,
                  library_sources: Mapping[str, Any] | None = None, *,
                  decision_errors: Sequence[str] = ()) -> str:
    """The ``<stem>_review.md`` Markdown for one run (see the module doc for
    the inputs). Never mutates its inputs. Malformed decision entries and
    ``decision_errors`` (decisions.load_decisions().errors) become open items
    rather than errors; a report that is not a pipeline report, a design that
    is not an object, or an invalid library source raises ValueError."""
    gate_list = _check_report(pipeline_report)
    if not isinstance(design, Mapping):
        raise ValueError(f"the design must be the netlist JSON object, got {type(design).__name__}")
    good, bad = _decision_split(decisions, decision_errors)
    sources = (None if library_sources is None else
               {str(ref): _part_sources(str(ref), v) for ref, v in library_sources.items()})
    review = _Review(pipeline_report, gate_list, design, analysis, good, bad, sources,
                     detect_safety(design), str(pipeline_report.get("netlist") or ""))
    title = _flat(design.get("title") or design.get("name") or _basename(_stem(review.netlist)))
    lines = _banner(review) + [f"# Design review: {title or '(untitled)'}"]
    for heading, render in zip(SECTIONS, _RENDERERS):
        lines += ["", f"## {heading}", ""] + render(review)
    return "\n".join(map(_one_line, lines)) + "\n"


# --- files + command line -----------------------------------------------------------
def review_path(path: str | os.PathLike) -> Path:
    """``<stem>_review.md`` for a netlist, board, schematic or bare stem."""
    return Path(_stem(os.fspath(path)) + REVIEW_SUFFIX)


def write_review(path: str | os.PathLike, markdown: str) -> Path:
    """Write ``markdown`` to review_path(path); returns where it went."""
    out = review_path(path)
    out.write_text(markdown, encoding="utf-8", newline="\n")
    return out


def _read_json(path: Path, what: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise ValueError(f"cannot read the {what} {path}: {e}") from e


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: review_report.py <netlist.json>", file=sys.stderr)
        return 2
    netlist = Path(args[0])
    try:
        report = _read_json(Path(_stem(str(netlist)) + REPORT_SUFFIX), "pipeline report")
        log = dec.load_decisions(netlist)
        out = write_review(netlist, render_review(report, list(log.entries),
                                                  _read_json(netlist, "netlist"),
                                                  decision_errors=log.errors))
    except (OSError, ValueError) as e:
        print(f"review_report: {e}", file=sys.stderr)
        return 1
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
