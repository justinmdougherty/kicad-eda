#!/usr/bin/env python3
"""The 'Import / rebuild' section of the hands-off review (ROADMAP D4, D5).

D5 asks the review report to explain every hands-off decision; for a rebuild
(a design with a ``source`` block, D4) that includes the import itself. The
section is rendered from three inputs, each read tolerantly (a missing or
odd-shaped key renders 'not recorded', never an exception):

* the pipeline report (run_pipeline's ``<stem>_pipeline_report.json``) and
  its ``faithful`` record (pipeline/stages_faithful.py): {active, kind,
  rebuild, reference, import_report, equivalence: {G2, G3, G4: {equal,
  summary, differences[, differences_total]}}, erc: {total, new,
  baseline_total}, drc: {ours, placement, intrinsic[, unconnected]},
  relaxed: [...]}. A stage entry may also hold named sub-checks
  ({"design": {...}, "schematic": {...}}), one row each;
* the design (netlist.json): source {kind, path, sha256, tool, via, files,
  reference, erc_baseline, drc_baseline, board, copper}, rebuild, variant
  {mode, from, dropped_refs, holes} and blocks;
* the import report (``import_report.json``, written by kicad_import.convert
  beside netlist.json): counts, mismatches [{kind, ref, detail}], sch_vs_pcb,
  checks.netlist_vs_pcb, libraries {symbols, footprints: {route, format,
  d0_dialect, k9_verified, refused, extracted, reason,
  kicad11_path_required}}, renamed_refs, dropped, unsupported; import_pipeline
  adds foreign {status, human_steps, warnings}, human_steps, value_repairs,
  eagle_library_check {footprints, symbols: {identical, differing, missing}}
  and vision {score, verdict, disagreements | open}.

API:
  load_import_report(design, netlist_path) -> dict | None
      source.import_report when set (D1 path rule: relative to the netlist
      directory, ``${KIPRJMOD}`` = that directory, or absolute), else
      <netlist dir>/import_report.json; None when the design has no source or
      no report exists; {"error": ...} when the report cannot be read.
  render_import_section(pipeline_report, design, import_report) -> list[str]
      Markdown lines, HEADING first; [] when the design has no source. An
      unreadable import report is one line naming the problem.
  open_items(pipeline_report, design, import_report) -> list[str]
      lines for the review's open-items section (human steps, non-equal
      equivalence, refused / non-D0 libraries, unsupported entries, ...).

Every rendered line is one Markdown line and every table cell escapes '|'
and line breaks. Pure: stdlib only (Python 3.11), never mutates its inputs.
"""
from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

SECTION_TITLE = "Import / rebuild"
HEADING = f"## {SECTION_TITLE}"
REPORT_FILE = "import_report.json"            # kicad_import.REPORT_FILE
TOP = 20                                      # table rows / refs before '+N more'
LISTED = 10                                   # items in one line or one list
DIFFS_SHOWN = 3                               # equivalence differences in a table cell
SHA_SHOWN = 12
STAGES = ("G2", "G3", "G4")
LATER_GATES = ("G2", "G3", "G4", "G5")        # gates after which faithful should be recorded
D0_FORMATS: Mapping[str, int] = {"symbols": 20241209, "footprints": 20241229}   # ROADMAP D0
LIBRARY_KINDS = (("symbols", "Symbols"), ("footprints", "Footprints"))
SEVERITY_ORDER = ("high", "med", "low")       # vision_crosscheck.SEVERITIES
FOREIGN_BLOCKED = ("incomplete", "failed")    # foreign_import status without board or schematic
KIPRJMOD = re.compile(r"^\$\{KIPRJMOD\}[\\/]*")

REBUILD_TEXT: Mapping[str, str] = {
    "keep": "keep (the source placement through `at` anchors, D3)",
    "reflow": "reflow (re-placed by the pipeline, e.g. a new form factor)"}
HOLES_TEXT: Mapping[str, str] = {
    "source": "the source's hole parts become mounting_holes with their footprint",
    "corners": "corner mounting holes (the pipeline default)",
    "none": "the source's hole parts are dropped without replacement"}
NO_REPORT = ("- Import report: none found (source.import_report is unset and no "
             "import_report.json is beside the netlist)")
NO_FAITHFUL = ("- Not recorded: the pipeline report has no faithful-rebuild record (the run "
               "stopped before G2, or the rebuild stages did not run)")
INACTIVE = ("The pipeline ran without the faithful-rebuild rules (faithful.active is false) "
            "although the design has a source")
NOT_CHECKED = ("Connectivity against the source was not checked: the pipeline report has no "
               "faithful-rebuild record")
CONNECTIVITY_INTRO = "Connectivity against the source ((ref, pad) partitions, D4):"
EQUIV_HEADER = ("Check", "Equal", "Summary", "Differences")
LIBRARY_HEADER = ("Library", "Route", "Format", "D0 dialect", "KiCad 9 verified", "Notes")
MISMATCH_HEADER = ("Kind", "Count", "Refs", "First detail")
VISION_HEADER = ("Severity", "Kind", "Subject", "Detail")
IMPORT_CHECKS = (("import: source schematic vs board", ("sch_vs_pcb",)),
                 ("import: netlist.json vs source board", ("checks", "netlist_vs_pcb")))
BASELINES = (("reference", "reference pad nets"), ("erc_baseline", "ERC"))
DRC_GROUPS = (("ours", "ours (blockers)"),
              ("placement", "from the source placement (warnings)"),
              ("intrinsic", "intrinsic to one footprint (warnings)"))
_NAME_KEYS = ("name", "ref", "id", "item", "subject", "gate", "stage")
_TEXT_KEYS = ("message", "detail", "reason", "why", "text", "desc", "rule", "construct")
_REPAIR_BEFORE = ("from", "old", "before", "original")
_REPAIR_AFTER = ("to", "new", "after", "repaired")
_HEX = re.compile(r"[0-9a-fA-F]{12,64}")
_PIPE = re.compile(r"(\\*)\|")


# --- small helpers (own copies: review_report's are private) ---------------------------
def _flat(value: Any) -> str:
    """``value`` as one line: every whitespace run (newlines, U+2028, ...)
    becomes one space; None is empty; non-strings are shown as JSON."""
    if value is None:
        return ""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False,
                                                           default=str)
    return " ".join(text.split())


def _cell(value: Any) -> str:
    """A table cell: one line, '|' escaped (a backslash right before it is
    doubled, so it cannot cancel the escape), '-' when empty."""
    text = _PIPE.sub(lambda m: m.group(1) * 2 + "\\|", _flat(value))
    return text or "-"


def _code(value: Any) -> str:
    """Inline code that survives backticks in the text."""
    text = _flat(value)
    ticks = "`" * (max(map(len, re.findall(r"`+", text)), default=0) + 1)
    pad = " " if text[:1] == "`" or text[-1:] == "`" else ""
    return f"{ticks}{pad}{text}{pad}{ticks}"


def _line(text: str) -> str:
    """Last guard: no rendered line may carry a line break."""
    return " ".join(text.splitlines())


def _table(header: Sequence[str], rows: Sequence[Sequence[str]]) -> list[str]:
    out = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    return out + ["| " + " | ".join(row) + " |" for row in rows]


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _items(value: Any) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple)) else []


def _dicts(value: Any) -> list[Mapping[str, Any]]:
    return [v for v in _items(value) if isinstance(v, Mapping)]


def _count(value: Any) -> int | None:
    if isinstance(value, (list, tuple, Mapping)):
        return len(value)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _num(value: Any) -> str:
    return "?" if value is None else _flat(value)


def _yes(flag: Any) -> str:
    return "yes" if flag is True else "no" if flag is False else "-"


def _listed(texts: Sequence[str], limit: int = LISTED) -> str:
    more = len(texts) - limit
    return ", ".join(texts[:limit]) + (f" (+{more} more)" if more > 0 else "")


def _more(total: int, shown: int, prefix: str) -> list[str]:
    return [f"{prefix}(+{total - shown} more)"] if total > shown else []


def _describe(item: Any) -> str:
    """One line for a list entry: a string as is, an object as
    '<name>: <text>' from its usual keys, else its JSON."""
    if not isinstance(item, Mapping):
        return _flat(item)
    name = next((_flat(item[k]) for k in _NAME_KEYS if item.get(k) not in (None, "")), "")
    text = next((_flat(item[k]) for k in _TEXT_KEYS if item.get(k) not in (None, "")), "")
    return f"{name}: {text}" if name and text else name or text or _flat(item)


def _names(value: Any) -> list[str]:
    """Entries of a list (described) or the keys of an object."""
    if isinstance(value, Mapping):
        return [_flat(k) for k in value]
    return [_describe(v) for v in _items(value)]


def _source(design: Any) -> Mapping[str, Any] | None:
    source = design.get("source") if isinstance(design, Mapping) else None
    return source if isinstance(source, Mapping) else None


def _faithful(report: Any) -> Mapping[str, Any] | None:
    record = _mapping(report).get("faithful")
    return record if isinstance(record, Mapping) else None


def _usable(import_report: Any) -> Mapping[str, Any] | None:
    """The import report when it was read (not None, not an error)."""
    if not isinstance(import_report, Mapping) or import_report.get("error"):
        return None
    return import_report


# --- load_import_report ----------------------------------------------------------------
def _report_path(raw: Any, base: Path | None) -> tuple[Path | None, str]:
    """(path, '') of source.import_report by the D1 rule, or (None, problem)."""
    if not isinstance(raw, str):
        return None, f"source.import_report must be a path string, not {raw!r}"
    m = KIPRJMOD.match(raw)
    path = Path(raw[m.end():] if m else raw)
    if path.is_absolute():
        return path, ""
    if base is None:
        return None, (f"source.import_report {raw!r} is relative and the netlist path is "
                      f"unknown")
    return base / path, ""


def _read_report(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"error": f"import report {path} not found"}
    except OSError as e:
        return {"error": f"cannot read the import report {path}: {e.strerror or e}"}
    except ValueError as e:
        return {"error": f"the import report {path} is not valid JSON: {e}"}
    if not isinstance(data, dict):
        return {"error": f"the import report {path} is not a JSON object"}
    return data


def load_import_report(design: Mapping[str, Any], netlist_path: str | os.PathLike | None
                       ) -> dict[str, Any] | None:
    """The import report of a rebuild: source.import_report when set, else
    <netlist dir>/import_report.json. None when the design has no source or
    no report exists; {"error": <problem>} when the named report is missing
    or the report is unreadable, not JSON or not a JSON object."""
    source = _source(design)
    if source is None:
        return None
    base = Path(netlist_path).parent if netlist_path else None
    raw = source.get("import_report")
    if raw is None or raw == "":
        default = None if base is None else base / REPORT_FILE
        return _read_report(default) if default is not None and default.exists() else None
    path, problem = _report_path(raw, base)
    return {"error": problem} if path is None else _read_report(path)


# --- section: source, rebuild, variant -------------------------------------------------
def _sha(value: Any) -> str:
    if value is None or value == "":
        return "not recorded"
    if isinstance(value, str) and _HEX.fullmatch(value):
        return value[:SHA_SHOWN].lower()
    return "malformed"


def _source_lines(source: Mapping[str, Any], imp: Mapping[str, Any] | None) -> list[str]:
    fallback = _mapping((imp or {}).get("source"))

    def get(key: str) -> Any:
        value = source.get(key)
        return fallback.get(key) if value is None or value == "" else value

    path = get("path")
    lines = [f"- Source: {_flat(get('kind')) or 'unknown kind'} "
             f"{_code(path) if path else '(path not recorded)'} (sha256 {_sha(get('sha256'))}, "
             f"tool {_flat(get('tool')) or 'not recorded'})"]
    if source.get("via"):
        lines.append(f"- Imported through {_code(source['via'])} (the intermediate KiCad "
                     f"project)")
    files = _mapping(source.get("files"))
    if files:
        lines.append(f"- Source files hashed: {_listed([_flat(k) for k in files])}")
    parts = [f"{label} {_code(source[key]) if source.get(key) else 'not recorded'}"
             for key, label in BASELINES]
    if source.get("drc_baseline"):
        parts.append(f"DRC {_code(source['drc_baseline'])}")
    lines.append("- Baselines: " + "; ".join(parts))
    if source.get("copper") or source.get("board"):
        lines.append(f"- Copper: {_flat(source.get('copper')) or 'reroute'}"
                     + (f" from {_code(source['board'])}" if source.get("board") else ""))
    return lines


def _variant_lines(variant: Any) -> list[str]:
    if not isinstance(variant, Mapping):
        return ["- Variant: none (the design is the import as converted)"]
    origin = f" from {_code(variant['from'])}" if variant.get("from") else ""
    dropped = [_flat(r) for r in _items(variant.get("dropped_refs"))]
    holes = variant.get("holes")
    if isinstance(holes, str) and holes in HOLES_TEXT:
        policy = f"{holes} ({HOLES_TEXT[holes]})"
    else:
        policy = _flat(holes) or "not recorded"
    return [f"- Variant: {_flat(variant.get('mode')) or 'mode not recorded'}{origin}",
            f"- Dropped references ({len(dropped)}): {_listed(dropped, TOP)}" if dropped
            else "- Dropped references: none",
            f"- Holes policy: {policy}"]


def _block_lines(blocks: Any) -> list[str]:
    rows = _dicts(blocks)
    if not rows:
        return []
    return [f"- Composed blocks ({len(rows)}): " + "; ".join(
        f"{_flat(b.get('name')) or '?'} ({_count(b.get('ref_map')) or 0} ref(s), rebuild "
        f"{_flat(b.get('rebuild')) or '-'}, source "
        f"{_flat(_mapping(b.get('source')).get('kind')) or '-'})" for b in rows)]


def _rebuild_lines(design: Mapping[str, Any]) -> list[str]:
    mode = design.get("rebuild")
    if mode is None:
        text = "not set (the faithful rules judge it as reflow: the pipeline places the parts)"
    else:
        text = (REBUILD_TEXT.get(mode) if isinstance(mode, str) else None) or _flat(mode)
    return ([f"- Rebuild: {text}"] + _variant_lines(design.get("variant"))
            + _block_lines(design.get("blocks")))


# --- section: the import report --------------------------------------------------------
def _foreign_lines(foreign: Mapping[str, Any]) -> list[str]:
    if not foreign:
        return []
    warnings = _names(foreign.get("warnings"))
    return [f"- Foreign import: status {_flat(foreign.get('status')) or 'not recorded'}, "
            f"{len(warnings)} warning(s)" + (f": {_listed(warnings)}" if warnings else "")]


def _quote(value: Any) -> str:
    return "?" if value is None else f"'{_flat(value)}'"


def _repair_text(item: Any) -> str:
    if not isinstance(item, Mapping):
        return _flat(item)
    before = next((item[k] for k in _REPAIR_BEFORE if k in item), None)
    after = next((item[k] for k in _REPAIR_AFTER if k in item), None)
    note = item.get("assumption")
    return (f"{_flat(item.get('ref')) or '?'}: {_quote(before)} -> {_quote(after)}"
            + (f" (assumption {_flat(note)})" if note else ""))


def _repair_lines(repairs: list[Any]) -> list[str]:
    if not repairs:
        return []
    return ([f"- Value repairs ({len(repairs)}):"]
            + [f"  - {_repair_text(r)}" for r in repairs[:TOP]] + _more(len(repairs), TOP, "  - "))


def _tally(group: Mapping[str, Any]) -> str:
    parts = []
    for key in ("identical", "differing", "missing"):
        value = group.get(key)
        count = _count(value)
        names = [] if key == "identical" else _names(value)
        parts.append(f"{'-' if count is None else count} {key}"
                     + (f" ({_listed(names)})" if names else ""))
    return ", ".join(parts)


def _eagle_lines(check: Any) -> list[str]:
    if check is None:
        return []
    if not isinstance(check, Mapping):
        return [f"- Eagle library cross-check: {_flat(check)}"]
    note = check.get("error") or check.get("skipped")
    if note:
        return [f"- Eagle library cross-check: not done: {_flat(note)}"]
    parts = [f"{kind} {_tally(check[kind])}" for kind in ("footprints", "symbols")
             if isinstance(check.get(kind), Mapping)]
    return [f"- Eagle library cross-check: {'; '.join(parts) or 'no results recorded'}"]


def _summary_lines(imp: Mapping[str, Any]) -> list[str]:
    counts = _mapping(imp.get("counts"))
    lines = ["- Counts: " + ", ".join(f"{_flat(k).replace('_', ' ')} {_flat(v)}"
                                      for k, v in counts.items())
             if counts else "- Counts: not recorded"]
    fmt, tool = imp.get("board_format"), imp.get("schematic_tool")
    if fmt is not None or tool:
        lines.append(f"- Source files as read: board format {_flat(fmt) or '-'} (generator "
                     f"{_flat(imp.get('board_generator')) or '-'}), schematic tool "
                     f"{_flat(tool) or '-'}")
    lines += _foreign_lines(_mapping(imp.get("foreign")))
    renamed = _dicts(imp.get("renamed_refs"))
    if renamed:
        lines.append(f"- Renamed references ({len(renamed)}): " + _listed(
            [f"{_flat(r.get('original'))} -> {_flat(r.get('ref'))}" for r in renamed]))
    dropped = _names(imp.get("dropped"))
    if dropped:
        lines.append(f"- Left off the board ({len(dropped)}): {_listed(dropped)}")
    unsupported = _items(imp.get("unsupported"))
    lines.append(f"- Unsupported constructs: {len(unsupported)} (listed under Open items)"
                 if unsupported else "- Unsupported constructs: none")
    return (lines + _repair_lines(_items(imp.get("value_repairs")))
            + _eagle_lines(imp.get("eagle_library_check")))


def _d0(kind: str, info: Mapping[str, Any]) -> str:
    flag, fmt = info.get("d0_dialect"), info.get("format")
    if isinstance(flag, bool):
        return _yes(flag)
    if isinstance(fmt, int) and not isinstance(fmt, bool):
        return f"{_yes(fmt <= D0_FORMATS[kind])} (by format)"
    return "-"


def _library_notes(info: Mapping[str, Any]) -> list[str]:
    notes = []
    if info.get("extracted") is False:
        notes.append(f"not extracted: {_flat(info.get('reason')) or 'no reason recorded'}")
    k11 = info.get("kicad11_path_required")
    if k11:
        notes.append("KiCad 11 path required" + (f": {_flat(k11)}" if isinstance(k11, str)
                                                 else ""))
    for key in ("refused", "missing", "problems"):
        names = _names(info.get(key))
        if names:
            notes.append(f"{key} {len(names)}: {_listed(names)}")
    if info.get("models_dropped"):
        notes.append(f"3D models dropped: {_flat(info['models_dropped'])}")
    return notes


def _library_lines(imp: Mapping[str, Any]) -> list[str]:
    libs = _mapping(imp.get("libraries"))
    rows = []
    for kind, label in LIBRARY_KINDS:
        info = libs.get(kind)
        if isinstance(info, Mapping):
            k9 = info.get("k9_verified")
            rows.append((label, _cell(info.get("route")), _cell(info.get("format")),
                         _d0(kind, info), _yes(k9) if isinstance(k9, bool) else "not checked",
                         _cell("; ".join(_library_notes(info)))))
    if not rows:
        return ["- Libraries: not recorded"]
    return ["", "### Libraries", ""] + _table(LIBRARY_HEADER, rows)


def _mismatch_lines(imp: Mapping[str, Any]) -> list[str]:
    found = _dicts(imp.get("mismatches"))
    head = ["", "### Mismatches", ""]
    if not found:
        return head + ["- None: the source schematic and board agree on every part"]
    groups: dict[str, list[Mapping[str, Any]]] = {}
    for m in found:
        groups.setdefault(_flat(m.get("kind")) or "unknown", []).append(m)
    order = sorted(groups, key=lambda k: (-len(groups[k]), k))
    rows = [(_cell(kind), str(len(groups[kind])),
             _cell(_listed([_flat(m.get("ref")) or "?" for m in groups[kind]], TOP)),
             _cell(groups[kind][0].get("detail"))) for kind in order[:TOP]]
    lines = head + [f"{len(found)} mismatch(es) between the source schematic and board; on a "
                    f"disagreement the board wins for pad nets, footprints and placement, the "
                    f"schematic for symbols, values and fields (D4).", ""]
    lines += _table(MISMATCH_HEADER, rows)
    if len(order) > TOP:
        lines += ["", f"(+{len(order) - TOP} more kind(s); see import_report.json)"]
    return lines


def _human_steps(imp: Mapping[str, Any]) -> list[Any]:
    """foreign.human_steps then the top-level human_steps, each once."""
    seen, steps = set(), []
    for step in (_items(_mapping(imp.get("foreign")).get("human_steps"))
                 + _items(imp.get("human_steps"))):
        key = json.dumps(step, sort_keys=True, ensure_ascii=False, default=str)
        if key not in seen:
            seen.add(key)
            steps.append(step)
    return steps


def _human_text(step: Any) -> str:
    if not isinstance(step, Mapping):
        return _flat(step)
    head = ("[blocking] " if step.get("blocking") else "") + (_flat(step.get("id")) or "step")
    text = f"{head}: {_flat(step.get('action')) or '(no action recorded)'}"
    if step.get("why"):
        text += f" (why: {_flat(step['why'])})"
    if step.get("expected_output"):
        text += f"; expected output {_code(step['expected_output'])}"
    return text


def _human_lines(imp: Mapping[str, Any]) -> list[str]:
    steps = _human_steps(imp)
    if not steps:
        return []
    return ["", "### Human steps", ""] + [f"- {_human_text(s)}" for s in steps]


def _open_disagreements(vision: Mapping[str, Any]) -> list[Any]:
    for key in ("open", "open_disagreements"):
        if isinstance(vision.get(key), list):
            return list(vision[key])
    return [d for d in _items(vision.get("disagreements"))
            if not (isinstance(d, Mapping) and d.get("resolved"))]


def _severity_rank(item: Any) -> int:
    severity = item.get("severity") if isinstance(item, Mapping) else None
    return SEVERITY_ORDER.index(severity) if severity in SEVERITY_ORDER else len(SEVERITY_ORDER)


def _vision_row(item: Any) -> tuple[str, ...]:
    if not isinstance(item, Mapping):
        return ("-", "-", "-", _cell(item))
    return (_cell(item.get("severity")), _cell(item.get("kind")), _cell(item.get("subject")),
            _cell(item.get("message") or item.get("detail")))


def _vision_lines(imp: Mapping[str, Any]) -> list[str]:
    vision = imp.get("vision")
    if not isinstance(vision, Mapping):
        return []
    found = sorted(_open_disagreements(vision), key=_severity_rank)
    lines = ["", "### Vision cross-check", "",
             f"- Score {_flat(vision.get('score')) or 'not recorded'}, verdict "
             f"{_flat(vision.get('verdict')) or '-'}; {len(found)} open disagreement(s)"]
    if not found:
        return lines
    return (lines + [""] + _table(VISION_HEADER, [_vision_row(d) for d in found[:TOP]])
            + _more(len(found), TOP, "- "))


def _import_lines(import_report: Any) -> list[str]:
    if not isinstance(import_report, Mapping):
        return [NO_REPORT]
    if import_report.get("error"):
        return [f"- Import report: unreadable: {_flat(import_report['error'])}"]
    imp = import_report
    return (["", "### Import report", ""] + _summary_lines(imp) + _library_lines(imp)
            + _mismatch_lines(imp) + _human_lines(imp) + _vision_lines(imp))


# --- section: rebuild checks (pipeline report) -----------------------------------------
def _dig(data: Mapping[str, Any], path: Sequence[str]) -> Any:
    node: Any = data
    for key in path:
        node = _mapping(node).get(key)
    return node


def _stage_entries(stage: str, entry: Any) -> list[tuple[str, Any]]:
    """One (label, check) per stage, or per named sub-check of a stage."""
    if isinstance(entry, Mapping) and "equal" not in entry:
        subs = [(f"{stage} {_flat(k)}", v) for k, v in entry.items() if isinstance(v, Mapping)]
        if subs:
            return subs
    return [(stage, entry)]


def _equivalence_entries(faithful: Mapping[str, Any] | None,
                         import_report: Any) -> list[tuple[str, Any]]:
    """(label, check) of the import's own checks, then of G2/G3/G4 (+ others)."""
    imp = _usable(import_report)
    entries = [(label, _dig(imp, path)) for label, path in IMPORT_CHECKS
               if imp is not None and isinstance(_dig(imp, path), Mapping)]
    equivalence = _mapping(_mapping(faithful).get("equivalence"))
    order = [s for s in STAGES if s in equivalence] + sorted(
        str(k) for k in equivalence if k not in STAGES)
    for stage in order:
        entries += _stage_entries(stage, equivalence[stage])
    return entries


def _differences(entry: Mapping[str, Any]) -> str:
    diffs = [_flat(d) for d in _items(entry.get("differences"))]
    total = entry.get("differences_total")
    if not isinstance(total, int) or isinstance(total, bool) or total < len(diffs):
        total = len(diffs)
    shown = diffs[:DIFFS_SHOWN]
    return "; ".join(shown) + (f" (+{total - len(shown)} more)" if total > len(shown) else "")


def _equiv_row(label: str, entry: Any) -> tuple[str, ...]:
    if not isinstance(entry, Mapping):
        return (_cell(label), "-", _cell(entry), "-")
    note = next((entry[k] for k in ("summary", "warning", "note", "skipped") if entry.get(k)),
                None)
    return (_cell(label), _yes(entry.get("equal")), _cell(note), _cell(_differences(entry)))


def _erc_lines(erc: Any) -> list[str]:
    if not isinstance(erc, Mapping):
        return ["- ERC against the source baseline: not recorded"]
    new, base = erc.get("new"), erc.get("baseline_total")
    if new is None and base is None:
        return [f"- ERC: {_num(erc.get('total'))} violation(s); no source baseline "
                f"(source.erc_baseline), so ERC is judged as usual"]
    names = _names(new)
    return [f"- ERC against the source baseline: {_num(erc.get('total'))} violation(s) in the "
            f"rebuild, {_num(base)} in the source; {_num(_count(new))} new"
            + (f": {_listed(names)}" if names else "")]


def _drc_lines(drc: Any) -> list[str]:
    if not isinstance(drc, Mapping):
        return ["- DRC split: not recorded"]
    counts, detail = [], []
    for key, label in DRC_GROUPS:
        value = drc.get(key)
        count = _count(value)
        if count is None and key == "ours":
            count = _count(drc.get("ours_errors"))       # faithful.DrcSplit.as_dict()
        counts.append(f"{_num(count)} {label}")
        texts = _names(value)
        detail += [f"  - {key}: {t}" for t in texts[:LISTED]] + _more(len(texts), LISTED, "  - ")
    if drc.get("unconnected") is not None:
        counts.append(f"{_flat(drc['unconnected'])} unconnected (blockers)")
    return [f"- DRC split: {', '.join(counts)}"] + detail


def _relaxed_lines(relaxed: Any) -> list[str]:
    rules = _names(relaxed)
    if not rules:
        return ["- Relaxed gate rules: none recorded"]
    return ([f"- Relaxed gate rules (D4 faithful rebuild, {len(rules)}):"]
            + [f"  - {r}" for r in rules[:TOP]] + _more(len(rules), TOP, "  - "))


def _check_lines(faithful: Mapping[str, Any] | None, import_report: Any) -> list[str]:
    head = ["", "### Rebuild checks", ""]
    rows = [_equiv_row(label, e) for label, e in _equivalence_entries(faithful, import_report)]
    table = [CONNECTIVITY_INTRO, ""] + _table(EQUIV_HEADER, rows) + [""] if rows else []
    if faithful is None:
        return head + table + [NO_FAITHFUL]
    status = [f"- {INACTIVE}", ""] if faithful.get("active") is False else []
    return (head + status + table + _erc_lines(faithful.get("erc"))
            + _drc_lines(faithful.get("drc")) + _relaxed_lines(faithful.get("relaxed")))


def render_import_section(pipeline_report: Mapping[str, Any] | None, design: Mapping[str, Any],
                          import_report: Mapping[str, Any] | None) -> list[str]:
    """The 'Import / rebuild' Markdown lines (HEADING first) of a rebuild's
    review; [] when the design has no source. ``import_report`` is
    load_import_report's result: None renders 'none found', {"error": ...}
    one line naming the problem. Never mutates its inputs."""
    source = _source(design)
    if source is None:
        return []
    imp = import_report if isinstance(import_report, Mapping) else None
    lines = ([HEADING, ""] + _source_lines(source, _usable(imp)) + _rebuild_lines(design)
             + _import_lines(imp) + _check_lines(_faithful(pipeline_report), imp))
    return [_line(text) for text in lines]


# --- open items ------------------------------------------------------------------------
def _library_open_items(libs: Mapping[str, Any]) -> list[str]:
    items = []
    for kind, label in LIBRARY_KINDS:
        info = libs.get(kind)
        if not isinstance(info, Mapping):
            continue
        refused = _names(info.get("refused"))
        if refused:
            items.append(f"{label}: {len(refused)} item(s) refused by the KiCad 9 dialect "
                         f"downgrade: {_listed(refused)}")
        k11 = info.get("kicad11_path_required")
        if k11:
            why = k11 if isinstance(k11, str) else info.get("reason")
            items.append(f"{label} are not in the KiCad 9 dialect: the rebuild must take the "
                         f"KiCad 11 path" + (f" ({_flat(why)})" if why else ""))
        if info.get("k9_verified") is False:
            items.append(f"{label}: the KiCad 9 load check failed (k9_verified false)")
    return items


def _unsupported_items(imp: Mapping[str, Any]) -> list[str]:
    texts = _names(imp.get("unsupported"))
    more = len(texts) - LISTED
    return ([f"Unsupported in the import: {t}" for t in texts[:LISTED]]
            + ([f"{more} more unsupported construct(s): see import_report.json"]
               if more > 0 else []))


def _eagle_open_items(check: Any) -> list[str]:
    items = []
    for kind in ("footprints", "symbols"):
        group = _mapping(_mapping(check).get(kind))
        for key in ("differing", "missing"):
            count, names = _count(group.get(key)), _names(group.get(key))
            if count:
                items.append(f"Eagle library cross-check: {count} {kind} {key}"
                             + (f": {_listed(names)}" if names else ""))
    return items


def _vision_open_items(vision: Any) -> list[str]:
    if not isinstance(vision, Mapping):
        return []
    count = len(_open_disagreements(vision))
    return [f"Settle {count} open vision disagreement(s) against the drawing (score "
            f"{_flat(vision.get('score')) or '-'})"] if count else []


def _import_open_items(imp: Mapping[str, Any]) -> list[str]:
    items = [f"Human step: {_human_text(s)}" for s in _human_steps(imp)]
    status = _mapping(imp.get("foreign")).get("status")
    if status in FOREIGN_BLOCKED:
        items.append(f"Foreign import {status}: a board or schematic import is missing (see "
                     f"the human steps of the import)")
    return (items + _library_open_items(_mapping(imp.get("libraries")))
            + _unsupported_items(imp) + _eagle_open_items(imp.get("eagle_library_check"))
            + _vision_open_items(imp.get("vision")))


def _equivalence_item(label: str, entry: Mapping[str, Any]) -> str:
    summary = _flat(entry.get("summary")) or "(no summary)"
    what = "not equivalent" if label.startswith("import:") else \
        "connectivity differs from the source"
    return f"{label}: {what}: {summary}"


def _reached_g2(report: Any) -> bool:
    return any(isinstance(g, Mapping) and g.get("gate") in LATER_GATES
               for g in _items(_mapping(report).get("gates")))


def open_items(pipeline_report: Mapping[str, Any] | None, design: Mapping[str, Any],
               import_report: Mapping[str, Any] | None) -> list[str]:
    """Open items of a rebuild for the review's open-items section (one line
    each, unnumbered): an unreadable import report, human steps, a blocked
    foreign import, refused or non-D0 libraries, unsupported entries, Eagle
    library differences, open vision disagreements, faithful rules not
    applied, and every non-equal equivalence check. [] without a source."""
    if _source(design) is None:
        return []
    imp = import_report if isinstance(import_report, Mapping) else None
    faithful = _faithful(pipeline_report)
    items: list[str] = []
    if imp is not None and imp.get("error"):
        items.append(f"Import report unreadable: {_flat(imp['error'])}")
    elif imp is not None:
        items += _import_open_items(imp)
    if faithful is not None and faithful.get("active") is False:
        items.append(INACTIVE)
    items += [_equivalence_item(label, entry)
              for label, entry in _equivalence_entries(faithful, imp)
              if isinstance(entry, Mapping) and entry.get("equal") is False]
    if faithful is None and _reached_g2(pipeline_report):
        items.append(NOT_CHECKED)
    return [_line(_flat(item)) for item in items]
