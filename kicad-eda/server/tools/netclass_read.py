#!/usr/bin/env python3
"""Net-class track widths read from a KiCad project file (stdlib only).

The healer, the G4 width gate (swig_gate_checks.check_routed_widths) and the
route handoff need the track width a net's class demands. pcbnew cannot be
asked: on KiCad 10 SWIG NETINFO_ITEM.GetNetClass() is an unwrapped
SwigPyObject. So read ``<pcb>.kicad_pro`` (netclass_writer writes it)::

    "net_settings": {
      "classes": [{"name": "NC_VIN", "track_width": 0.528, "priority": 2}, ...],
      "netclass_patterns": [{"pattern": "VIN", "netclass": "NC_VIN"}, ...],
      "netclass_assignments": {"/USB/D+": ["USB_DIFF"]} | null
    }

A net resolves the way KiCad's NET_SETTINGS::GetEffectiveNetClass does
(measured on 9.0.4 and 10.0.5; tests/test_netclass_read.py re-measures it):

* every netclass_patterns entry whose pattern matches the net, and every class
  netclass_assignments lists for it, contributes its class; names with no class
  definition contribute nothing. File order plays no part, so a Default or
  undefined binding never shadows another match;
* a pattern matches like KiCad's combined matcher (EDA_COMBINED_MATCHER::
  StartsWith, CTX_NETCLASS): as a regex anchored with ``^``/``$`` whose match
  starts at the net's FIRST character (so ``X|Y``, i.e. ``^X`` or ``Y$``,
  matches XA but not AY), OR as a wildcard where only ``*`` and ``?`` are
  special and the whole name must match. Matching is case-sensitive. A
  pattern KiCad cannot compile as a regex is a wildcard only;
* the contributing classes are ordered by (priority, name) and the first one
  with a track width supplies it. A class without ``priority`` sorts at -1, as
  KiCad 10 loads it (9.0.4 drops such a class's widths in a v4 file; the
  gate stays strict instead); Default without one sorts last;
* when that first class is Default, or no class has a width, the net has no
  class width: it routes at the board default. The empty (netless) name has
  none either.

Wildcards and regexes can only be resolved against real net names, so
callers pass them (``nets``); the result is keyed by those nets and a net's
answer never depends on which other nets were asked.

KiCad compiles the regex with wxRegEx's advanced syntax (ARE); Python's ``re``
stands in for it, with bracket expressions rewritten the way ARE reads them:
POSIX classes such as ``[[:digit:]]`` become ASCII ranges (KiCad's are ASCII:
'é' is no ``[:alpha:]``), and an unknown class, a collating element ``[.a.]``
or an equivalence class ``[=a=]`` makes the regex invalid, as in KiCad. In
both matchers ``.`` also matches a newline and ``$`` matches at the end of the
name or just before a final newline (measured; Python's own ``$``).
Known differences, none of which a netclass_writer
pattern (an exact net name) runs into:

* bus patterns: 10.0.5 expands a pattern that parses as a bus (vector
  ``D[0..3]``, group ``X{A B}``, also ``A{2}``) into its member names first,
  and gives a net named like a bus its members' common class; 9.0.4 and this
  reader treat the pattern as a regex / wildcard (``A{2}`` matches AA);
* ARE-only syntax (escapes such as ``\\m``, ``\\y``, ``\\B``; ``***=``) and
  Python-only syntax (lookbehind, named groups, possessive quantifiers) are
  judged by ``re``, not by ARE.

Imported by KiCad's bundled Python (9.0.4 ships 3.11): stdlib only.
"""
from __future__ import annotations

import json
import re
import warnings
from functools import lru_cache
from pathlib import Path
from typing import Iterable, Mapping, NamedTuple

DEFAULT_CLASS = "Default"
DEFAULT_PRIORITY = 2**31 - 1        # KiCad writes INT_MAX for Default
UNSET_PRIORITY = -1                 # KiCad 10: a class without "priority" sorts here

ClassEntry = tuple[str, float]                   # (class name, track width mm)

# ARE [:name:] classes as Python set ranges (KiCad's are ASCII; measured).
POSIX_CLASSES = {
    "alnum": "0-9A-Za-z", "alpha": "A-Za-z", "blank": " \\t", "cntrl": "\\x00-\\x1f\\x7f",
    "digit": "0-9", "graph": "!-~", "lower": "a-z", "print": " -~",
    "punct": "!-/:-@\\[-`{-~", "space": " \\t\\n\\r\\f\\v", "upper": "A-Z",
    "xdigit": "0-9A-Fa-f",
}
_SET_LITERALS = frozenset("[&|~")     # literal in an ARE set; Python warns on [[ && || ~~


class NetClass(NamedTuple):
    name: str
    priority: float
    track_width: float | None                    # None: the class sets no width


class _Rules(NamedTuple):
    classes: Mapping[str, NetClass]
    patterns: tuple[tuple[str, str], ...]         # (pattern, class name)
    assignments: Mapping[str, tuple[str, ...]]    # net -> class names


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------
def _bracket(pattern: str, start: int) -> tuple[str, int] | None:
    """The ARE bracket expression opening at ``pattern[start]`` as a Python
    set, and the index after it; None when ARE rejects it (an unknown class,
    a collating or equivalence element, no closing ``]``)."""
    out, i = ["["], start + 1
    if pattern.startswith("^", i):
        out.append("^")
        i += 1
    if pattern.startswith("]", i):                  # ']' first is a literal
        out.append("\\]")
        i += 1
    while i < len(pattern):
        c = pattern[i]
        if c == "]":
            return "".join(out) + "]", i + 1
        if pattern.startswith(("[.", "[="), i):
            return None
        if pattern.startswith("[:", i):
            close = pattern.find(":]", i + 2)
            ranges = POSIX_CLASSES.get(pattern[i + 2:close]) if close >= 0 else None
            if ranges is None:
                return None
            out.append(ranges)
            i = close + 2
            continue
        step = 2 if c == "\\" else 1
        out.append("\\" + c if c in _SET_LITERALS else pattern[i:i + step])
        i += step
    return None


def are_to_python(pattern: str) -> str | None:
    """An ARE pattern as Python ``re`` text (only bracket expressions are
    rewritten), or None when KiCad's regex rejects it."""
    out, i = [], 0
    while i < len(pattern):
        c = pattern[i]
        if c == "[":
            found = _bracket(pattern, i)
            if found is None:
                return None
            text, i = found
            out.append(text)
            continue
        step = 2 if c == "\\" else 1
        out.append(pattern[i:i + step])
        i += step
    return "".join(out)


@lru_cache(maxsize=4096)
def _regex(pattern: str) -> re.Pattern | None:
    """KiCad's anchored-regex matcher; None when the pattern is no regex."""
    anchored = ("" if pattern.startswith("^") else "^") + pattern \
        + ("" if pattern.endswith("$") else "$")
    text = are_to_python(anchored)
    if text is None:
        return None
    try:
        with warnings.catch_warnings():             # '--' in a set: FutureWarning
            warnings.simplefilter("ignore", FutureWarning)
            return re.compile(text, re.DOTALL)
    except (re.error, OverflowError, RecursionError):
        return None


@lru_cache(maxsize=4096)
def _wildcard(pattern: str) -> re.Pattern:
    """KiCad's anchored-wildcard matcher: * any run, ? one char, rest literal."""
    parts = (".*" if c == "*" else "." if c == "?" else re.escape(c) for c in pattern)
    return re.compile("^" + "".join(parts) + "$", re.DOTALL)


def pattern_matches(pattern: str, net: str) -> bool:
    """True when a netclass pattern matches a net name the way KiCad does:
    the regex or the wildcard matches starting at the first character."""
    rx = _regex(pattern)
    return (rx is not None and rx.match(net) is not None) \
        or _wildcard(pattern).match(net) is not None


# ---------------------------------------------------------------------------
# Reading and validating the project
# ---------------------------------------------------------------------------
def _malformed(path: Path, why: str) -> ValueError:
    return ValueError(f"{path}: malformed KiCad project file ({why})")


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _net_settings(path: Path) -> dict:
    """The project's ``net_settings`` object ({} when it has none)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise _malformed(path, str(e)) from e
    if not isinstance(data, dict):
        raise _malformed(path, f"top level is {type(data).__name__}, not an object")
    ns = data.get("net_settings")
    if ns is None:
        return {}
    if not isinstance(ns, dict):
        raise _malformed(path, f"net_settings is {type(ns).__name__}, not an object")
    return ns


def _field(path: Path, ns: dict, key: str, kind: type) -> list | dict:
    value = ns.get(key)
    if value is None:
        return kind()
    if not isinstance(value, kind):
        raise _malformed(path, f"net_settings.{key} is {type(value).__name__}, "
                               f"not {'a list' if kind is list else 'an object'}")
    return value


def _parse_class(path: Path, index: int, entry: object) -> NetClass:
    if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
        raise _malformed(path, f"net_settings.classes[{index}] is not an object "
                               f"with a string name")
    name, width, prio = entry["name"], entry.get("track_width"), entry.get("priority")
    if width is not None and not (_is_number(width) and width > 0):
        raise _malformed(path, f"class {name!r} track_width {width!r} is not a positive number")
    if prio is None:
        prio = DEFAULT_PRIORITY if name == DEFAULT_CLASS else UNSET_PRIORITY
    elif not _is_number(prio):
        raise _malformed(path, f"class {name!r} priority {prio!r} is not a number")
    return NetClass(name, prio, None if width is None else float(width))


def _parse_pattern(path: Path, index: int, entry: object) -> tuple[str, str]:
    if not (isinstance(entry, dict) and isinstance(entry.get("pattern"), str)
            and isinstance(entry.get("netclass"), str)):
        raise _malformed(path, f"net_settings.netclass_patterns[{index}] is not an object "
                               f"with string pattern and netclass")
    return entry["pattern"], entry["netclass"]


def _parse_assignment(path: Path, net: str, names: object) -> tuple[str, ...]:
    names = [names] if isinstance(names, str) else names
    if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
        raise _malformed(path, f"net_settings.netclass_assignments[{net!r}] is not "
                               f"a list of class names")
    return tuple(names)


def _load(path: Path) -> _Rules:
    ns = _net_settings(path)
    classes = [_parse_class(path, i, c)
               for i, c in enumerate(_field(path, ns, "classes", list))]
    patterns = tuple(_parse_pattern(path, i, p)
                     for i, p in enumerate(_field(path, ns, "netclass_patterns", list)))
    assignments = {net: _parse_assignment(path, net, names) for net, names
                   in _field(path, ns, "netclass_assignments", dict).items()}
    return _Rules({c.name: c for c in classes}, patterns, assignments)


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------
def _resolve(rules: _Rules, net: str) -> ClassEntry | None:
    """(class, width) the net must be routed at, else None (board default)."""
    if not net:
        return None
    names = {cls for pat, cls in rules.patterns if pattern_matches(pat, net)}
    names.update(rules.assignments.get(net, ()))
    matched = sorted((rules.classes[n] for n in names if n in rules.classes),
                     key=lambda c: (c.priority, c.name))
    first = next((c for c in matched if c.track_width is not None), None)
    if first is None or first.name == DEFAULT_CLASS:
        return None
    return first.name, first.track_width


def net_class_widths(pro_path: str | Path, nets: Iterable[str]) -> dict[str, ClassEntry]:
    """{net: (class_name, track_width_mm)} for each of ``nets`` (e.g. the
    board's net names) that resolves to a non-Default class with a track
    width; keys sorted. {} for a missing project file; ValueError naming the
    file when it is malformed (bad JSON or a malformed class, pattern or
    assignment entry)."""
    if isinstance(nets, (str, bytes)):
        raise TypeError("nets must be an iterable of net names, not one string")
    path = Path(pro_path)
    if not path.is_file():
        return {}
    rules = _load(path)
    resolved = ((net, _resolve(rules, net)) for net in sorted(set(nets)))
    return {net: entry for net, entry in resolved if entry is not None}


def class_of(widths: Mapping, net: str) -> ClassEntry | None:
    """(class_name, track_width_mm) of a net in a net_class_widths() result
    (also after a JSON round trip), else None."""
    hit = widths.get(net)
    return None if hit is None else (str(hit[0]), float(hit[1]))


def class_width(widths: Mapping, net: str) -> float | None:
    """The class track width (mm) a net must be routed at, else None."""
    entry = class_of(widths, net)
    return None if entry is None else entry[1]
