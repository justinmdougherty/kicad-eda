#!/usr/bin/env python3
"""The napkin-mode decision log and the netlist "assumptions" block (ROADMAP
D5).

Runtime decisions go to an append-only ``<stem>.decisions.jsonl`` beside the
netlist, one JSON object per line, written by the pipeline harness and by
agents (the ``record_decision`` MCP tool):

  {"id", "ts", "stage", "question", "decision", "rationale", "alternatives",
   "confidence", "source", "reversible", "override"}

  * ``id`` ("dec-" + 12 hex) and ``ts`` (ISO 8601, UTC) are generated here;
  * ``stage``: requirements | parts | G1..G5 | routing;
  * ``question``: what a human would have been asked; ``decision``: the
    answer taken; ``rationale``: why; ``source``: a policy id, datasheet URL,
    "user", ... (free text, non-blank);
  * ``confidence``: high | med | low (netlist_schema.CONFIDENCE_LEVELS);
  * ``alternatives``: the options not taken (list of strings, [] = none
    recorded). append_decision also takes None for [] (an optional tool
    argument passed straight through); a stored entry always holds a list,
    so a null on disk is a malformed line, never a special case for readers;
  * ``reversible``: whether a re-run can undo it (default True — every
    napkin decision is a netlist/board edit; the irreversible ones must say
    so); ``override``: the netlist path a human edits to change it, or None.

Requirements-level assumptions live in the netlist itself (top-level
``assumptions``: ``{"id", "topic", "value", "rationale", "confidence",
"override"?}``). ``validate_assumptions`` checks that block without
jsonschema and agrees with netlist_schema's verdict, except that it is
stricter in two places the schema cannot express: whitespace-only strings
and duplicate ids are errors.

Stdlib only (plus the shared netlist_schema constants), no KiCad. Appends are
one write of one line of pure ASCII (non-ASCII text is JSON-escaped, so no
U+2028 / U+0085 pasted from a datasheet can split a line and no torn write
can end inside a multibyte character). A torn last line (a crash mid-write)
is closed with a newline before the next entry so it costs only itself. The
reader splits on "\\n" alone and decodes each line separately (a log written
by another tool may hold raw UTF-8), naming every bad line by its number.
"""
from __future__ import annotations

import codecs
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, NamedTuple

from netlist_schema import CONFIDENCE_LEVELS

STAGES: tuple[str, ...] = ("requirements", "parts", "G1", "G2", "G3", "G4", "G5", "routing")
ENTRY_FIELDS: tuple[str, ...] = ("id", "ts", "stage", "question", "decision", "rationale",
                                 "alternatives", "confidence", "source", "reversible",
                                 "override")
GENERATED_FIELDS: tuple[str, ...] = ("id", "ts")
REQUIRED_FIELDS: tuple[str, ...] = ("stage", "question", "decision", "rationale",
                                    "confidence", "source")
OPTIONAL_DEFAULTS: Mapping[str, Any] = {"alternatives": (), "reversible": True,
                                        "override": None}
ASSUMPTION_REQUIRED: tuple[str, ...] = ("id", "topic", "value", "rationale", "confidence")
ASSUMPTION_FIELDS: tuple[str, ...] = ASSUMPTION_REQUIRED + ("override",)
CONFIDENCE_RANK: Mapping[str, int] = {"low": 0, "med": 1, "high": 2}

DECISIONS_SUFFIX = ".decisions.jsonl"
STEM_SUFFIXES: tuple[str, ...] = (".json", ".kicad_pcb", ".kicad_sch", ".kicad_pro")
ID_PREFIX = "dec-"
ID_HEX_DIGITS = 12


class DecisionError(ValueError):
    """A decision entry that does not satisfy the D5 schema; nothing was written."""


class DecisionLogError(ValueError):
    """A decision log with malformed lines (each named as <path>:<line>)."""


class DecisionLog(NamedTuple):
    """A leniently read log: the valid entries in file order, and one
    ``<path>:<line>: <problem>`` message per line that was skipped."""
    entries: tuple[dict, ...]
    errors: tuple[str, ...]


# --- paths ----------------------------------------------------------------------
def decisions_path(path: str | os.PathLike) -> Path:
    """``<stem>.decisions.jsonl`` for a netlist (``x.json``), a board or
    schematic (``x.kicad_pcb``), a bare stem (``x``) or the log itself."""
    p = Path(path)
    if p.name.endswith(DECISIONS_SUFFIX):
        return p
    if p.suffix in STEM_SUFFIXES:
        p = p.with_suffix("")
    return p.with_name(p.name + DECISIONS_SUFFIX)


# --- field checks ---------------------------------------------------------------
def _is_text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _text_problem(name: str, value: Any) -> str | None:
    if _is_text(value):
        return None
    if isinstance(value, str):
        return f"{name} must not be blank"
    return f"{name} must be a non-blank string, got {type(value).__name__}"


def _enum_problem(name: str, value: Any, allowed: tuple[str, ...]) -> str | None:
    if isinstance(value, str) and value in allowed:
        return None
    return f"{name} must be one of {', '.join(allowed)} (got {value!r})"


def _override_problem(value: Any) -> str | None:
    if value is None or _is_text(value):
        return None
    return ("override must be the netlist path to change (a non-blank string) or null, "
            f"got {value!r}")


def _alternatives_problems(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        got = "null" if value is None else type(value).__name__
        return [f"alternatives must be a list of strings, got {got}"]
    return [f"alternatives[{i}] must be a non-blank string, got {item!r}"
            for i, item in enumerate(value) if not _is_text(item)]


def _ts_problem(value: Any) -> str | None:
    if not isinstance(value, str):
        return f"ts must be an ISO 8601 timestamp string, got {type(value).__name__}"
    try:
        stamp = datetime.fromisoformat(value)
    except ValueError:
        return f"ts must be an ISO 8601 timestamp, got {value!r}"
    if stamp.tzinfo is None:
        return f"ts must carry a timezone (UTC), got {value!r}"
    return None


def _field_problems(entry: Mapping[str, Any]) -> list[str]:
    """Problems of the fields present in ``entry`` (absence is checked by the
    callers, which know which fields they require)."""
    checks = {
        "id": lambda v: _text_problem("id", v),
        "ts": _ts_problem,
        "stage": lambda v: _enum_problem("stage", v, STAGES),
        "question": lambda v: _text_problem("question", v),
        "decision": lambda v: _text_problem("decision", v),
        "rationale": lambda v: _text_problem("rationale", v),
        "confidence": lambda v: _enum_problem("confidence", v, CONFIDENCE_LEVELS),
        "source": lambda v: _text_problem("source", v),
        "reversible": lambda v: (None if isinstance(v, bool) else
                                 f"reversible must be true or false, got {v!r}"),
        "override": _override_problem,
    }
    problems: list[str] = []
    for name in ENTRY_FIELDS:
        if name not in entry:
            continue
        if name == "alternatives":
            problems += _alternatives_problems(entry[name])
            continue
        problem = checks[name](entry[name])
        if problem:
            problems.append(problem)
    return problems


def _unknown_problem(keys: Any, allowed: tuple[str, ...]) -> str | None:
    unknown = sorted(str(k) for k in keys if k not in allowed)
    if not unknown:
        return None
    noun = "unknown field" if len(unknown) == 1 else "unknown fields"
    return f"{noun} {', '.join(repr(k) for k in unknown)} (allowed: {', '.join(allowed)})"


def validate_decision(entry: Any) -> list[str]:
    """Problems of one complete log entry (generated fields included); []
    when it is valid. Unknown fields are errors: the entry shape is fixed."""
    if not isinstance(entry, Mapping):
        return [f"a decision entry must be a JSON object, got {type(entry).__name__}"]
    problems = [f"missing field '{name}'" for name in ENTRY_FIELDS if name not in entry]
    unknown = _unknown_problem(entry, ENTRY_FIELDS)
    if unknown:
        problems.insert(0, unknown)
    return problems + _field_problems(entry)


def _entry_from_fields(fields: Mapping[str, Any]) -> dict:
    """The complete entry for caller ``fields``; DecisionError lists every
    problem at once."""
    problems: list[str] = []
    generated = [name for name in GENERATED_FIELDS if name in fields]
    if generated:
        problems.append(f"{', '.join(repr(n) for n in generated)} are generated by "
                        f"append_decision; do not pass them")
    unknown = _unknown_problem((k for k in fields if k not in GENERATED_FIELDS),
                               ENTRY_FIELDS)
    if unknown:
        problems.append(unknown)
    problems += [f"missing required field '{name}'" for name in REQUIRED_FIELDS
                 if name not in fields]
    none_recorded = {"alternatives": ()} if fields.get("alternatives", ()) is None else {}
    caller = {**{k: v for k, v in fields.items() if k not in GENERATED_FIELDS}, **none_recorded}
    problems += _field_problems(caller)
    if problems:
        raise DecisionError("invalid decision: " + "; ".join(problems))
    values = {**OPTIONAL_DEFAULTS, **caller}
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    entry: dict = {"id": ID_PREFIX + uuid.uuid4().hex[:ID_HEX_DIGITS], "ts": stamp}
    for name in ENTRY_FIELDS[len(GENERATED_FIELDS):]:
        value = values[name]
        entry[name] = list(value) if name == "alternatives" else value
    return entry


# --- append / read --------------------------------------------------------------
def _ends_without_newline(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            if f.tell() == 0:
                return False
            f.seek(-1, os.SEEK_END)
            return f.read(1) != b"\n"
    except FileNotFoundError:
        return False


def append_decision(stem_path: str | os.PathLike, **fields: Any) -> dict:
    """Validate ``fields`` (D5 entry fields minus ``id``/``ts``; alternatives
    None is written as []), stamp them with a fresh id and a UTC timestamp,
    and append the entry as one line to ``decisions_path(stem_path)``.
    Returns the entry written (a new dict).
    Raises DecisionError, before touching the file, when a field is missing,
    unknown or malformed; OSError when the log cannot be written."""
    entry = _entry_from_fields(fields)
    path = decisions_path(stem_path)
    line = json.dumps(entry, ensure_ascii=True) + "\n"     # pure ASCII: see the module doc
    if _ends_without_newline(path):
        line = "\n" + line
    with open(path, "a", encoding="ascii", newline="\n") as f:
        f.write(line)
    return entry


def _parse_line(raw: bytes) -> tuple[Any, str | None]:
    """(the JSON value of one log line, None) or (None, the problem)."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as e:
        return None, f"not UTF-8 text ({e.reason} at byte {e.start}; a torn write?)"
    try:
        return json.loads(text), None
    except ValueError as e:
        return None, f"not JSON ({e})"


def load_decisions(stem_path: str | os.PathLike) -> DecisionLog:
    """Read the log leniently: every valid entry in file order, plus a
    ``<path>:<line>: <problem>`` message per malformed, undecodable or
    duplicate-id line. A missing log is empty. Blank lines, CRLF line ends
    and a UTF-8 BOM are tolerated. Lines are split on "\\n" only, so a raw
    U+2028 / U+0085 inside a string does not break an entry."""
    path = decisions_path(stem_path)
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return DecisionLog((), ())
    if data.startswith(codecs.BOM_UTF8):
        data = data[len(codecs.BOM_UTF8):]
    entries: list[dict] = []
    errors: list[str] = []
    seen: set[str] = set()
    for number, raw in enumerate(data.split(b"\n"), 1):
        if not raw.strip():
            continue
        entry, problem = _parse_line(raw.rstrip(b"\r"))
        if problem:
            errors.append(f"{path}:{number}: {problem}")
            continue
        problems = validate_decision(entry)
        if not problems and entry["id"] in seen:
            problems = [f"duplicate id {entry['id']!r}"]
        if problems:
            errors.append(f"{path}:{number}: " + "; ".join(problems))
            continue
        seen.add(entry["id"])
        entries.append(entry)
    return DecisionLog(tuple(entries), tuple(errors))


def read_decisions(stem_path: str | os.PathLike) -> list[dict]:
    """Every entry of the log in file order ([] when there is no log).
    Raises DecisionLogError naming each malformed line; use load_decisions
    to keep the valid entries of a damaged log."""
    log = load_decisions(stem_path)
    if log.errors:
        raise DecisionLogError(f"{len(log.errors)} malformed line(s) in "
                               f"{decisions_path(stem_path)}:\n" + "\n".join(log.errors))
    return list(log.entries)


# --- the netlist "assumptions" block --------------------------------------------
_JSON_VALUE_TYPES = (str, int, float, bool, list, dict)


def validate_assumption(entry: Any) -> list[str]:
    """Problems of one assumptions-block entry ([] = valid): the required
    fields present, id/topic/rationale non-blank strings, value any JSON
    value but null, confidence high|med|low, override a non-blank string or
    null. Unknown keys pass (the netlist schema allows them)."""
    if not isinstance(entry, Mapping):
        return [f"must be a JSON object, got {type(entry).__name__}"]
    problems = [f"missing field '{name}'" for name in ASSUMPTION_REQUIRED if name not in entry]
    for name in ("id", "topic", "rationale"):
        if name in entry:
            problem = _text_problem(name, entry[name])
            if problem:
                problems.append(problem)
    if "value" in entry and not isinstance(entry["value"], _JSON_VALUE_TYPES):
        problems.append("value must be a string, number, boolean, array or object, got "
                        + ("null" if entry["value"] is None else type(entry["value"]).__name__))
    if "confidence" in entry:
        problem = _enum_problem("confidence", entry["confidence"], CONFIDENCE_LEVELS)
        if problem:
            problems.append(problem)
    if "override" in entry:
        problem = _override_problem(entry["override"])
        if problem:
            problems.append(problem)
    return problems


def _assumption_label(index: int, entry: Any) -> str:
    ident = entry.get("id") if isinstance(entry, Mapping) else None
    if isinstance(ident, str) and ident.strip():
        return f"assumptions[{index}] (id {ident!r})"
    return f"assumptions[{index}]"


def validate_assumptions(block: Any) -> list[str]:
    """Problems of a netlist ``assumptions`` block as ``assumptions[i] (id
    'X'): <problem>`` strings; [] when it is valid or absent (None)."""
    if block is None:
        return []
    if not isinstance(block, list):
        return [f"assumptions must be a list of entries, got {type(block).__name__}"]
    problems: list[str] = []
    first_index: dict[str, int] = {}
    for index, entry in enumerate(block):
        label = _assumption_label(index, entry)
        problems += [f"{label}: {p}" for p in validate_assumption(entry)]
        ident = entry.get("id") if isinstance(entry, Mapping) else None
        if not _is_text(ident):
            continue
        if ident in first_index:
            problems.append(f"{label}: duplicate id {ident!r} (first used by "
                            f"assumptions[{first_index[ident]}])")
        else:
            first_index[ident] = index
    return problems
