#!/usr/bin/env python3
"""Tracks, arcs and vias of a ``.kicad_pcb`` read from its TEXT, any format
from KiCad 5 to 11, no KiCad needed (F3-20).

Why: a board saved by the KiCad 11 API session (an agent-routed board the
harness resumes) cannot be loaded by KiCad 9/10's pcbnew, so the SWIG gate
scripts cannot judge it, and kicad-cli DRC misses copper lying wholly inside
an Edge.Cuts cutout (it only measures the edge clearance band). The pipeline's
outline feature (pipeline/rp_feature_outline.py) feeds these records, moved
into the board frame with ``translated``, to outline_audit.copper_issues.

Records are outline_audit item records in KiCad's SHEET frame (mm, Y down)::

    {"kind": "track", "layer", "net", "start": (x, y), "end": (x, y), "width"}
    {"kind": "arc",   "layer", "net", "start", "mid", "end", "width"}
    {"kind": "via",   "layers": (top, bottom), "net", "center": (x, y),
     "radius", "drill", "via_type": "through" | "blind" | "buried" | "micro"}

``net`` is the net NAME ("" = no net): KiCad <= 9 ``(net N)`` codes resolve
through the board's top-level net table, KiCad 10+ ``(net "name")`` is the
name, ``{slash}`` is unescaped. A via's radius is half its largest copper
size (a KiCad 10+ ``padstack`` may give inner/back layers their own size).
Footprint pads, zones and drawings are not read.

Stdlib + sexpr_read's regex scanners only (never sexpdata), Python 3.11:
only the top-level items read here are tokenised, KiCad 4/5 bare tokens keep
their text (``(net 2 01)`` is net "01") and a malformed item raises
ValueError naming it (and its uuid when it has one).
"""
from __future__ import annotations

import re
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import sexpr_read as sr  # noqa: E402

COPPER_HEADS = frozenset({"segment", "arc", "via"})
VIA_TYPES = ("through", "blind", "buried", "micro")
FRAME = "sheet frame, mm (KiCad page coordinates, Y down)"
_KINDS = {"segment": "track", "arc": "arc", "via": "via"}
_ITEM_TOKEN = re.compile(r'"((?:[^"\\]|\\.)*)"|([()])|([^\s()"]+)')
_ESCAPE = re.compile(r"\\(.)")
_ESCAPES = {"n": "\n", "t": "\t", "r": "\r"}
_POINT_KEYS = ("start", "mid", "end", "center")

Node = list   # a parsed item: [head, child, ...]; a child is a str, a Quoted or a Node


class Quoted(str):
    """A token that was a quoted string in the file (KiCad 10+ net names)."""


@dataclass(frozen=True)
class BoardCopper:
    """The copper items of one board text, in file order (sheet frame)."""
    format_version: int | None
    items: tuple[dict, ...]


# --- tokenising ------------------------------------------------------------------------
def _unescape(text: str) -> str:
    return _ESCAPE.sub(lambda m: _ESCAPES.get(m.group(1), m.group(1)), text)


def _tree(src: str) -> Node:
    """One S-expression item as nested lists of str (Quoted for quoted strings)."""
    stack: list[Node] = [[]]
    for m in _ITEM_TOKEN.finditer(src):
        quoted, paren, bare = m.groups()
        if paren == "(":
            stack.append([])
        elif paren == ")":
            if len(stack) == 1:
                raise ValueError(f"unbalanced parentheses in {src[:60]!r}")
            node = stack.pop()
            stack[-1].append(node)
        else:
            stack[-1].append(Quoted(_unescape(quoted)) if quoted is not None else bare)
    if len(stack) != 1 or len(stack[0]) != 1 or not isinstance(stack[0][0], list):
        raise ValueError(f"not one S-expression item: {src[:60]!r}")
    return stack[0][0]


def top_level_items(text: str, heads: Iterable[str]) -> list[tuple[str, str]]:
    """(head, source) of every direct child of ``(kicad_pcb ...)`` whose head
    is in `heads`, in file order; everything else (footprints, zone fills) is
    skipped unparsed. Raises ValueError for a non-board or unbalanced text."""
    if sr.head_of(text) != "kicad_pcb":
        raise ValueError("not a KiCad board (expected '(kicad_pcb ...')")
    wanted, out = frozenset(heads), []
    text = sr.restore_teardrop_parens(text)
    depth, opened, head, closed = 0, 0, "", False
    for m in sr.TOKEN_RE.finditer(text, len(text) - len(text.lstrip(sr.LEADING))):
        if m.group() == "(":
            if closed:
                raise ValueError(f"text after the board's closing parenthesis at offset "
                                 f"{m.start()}")
            depth += 1
            if depth == 2:
                opened = m.start()
                found = sr.HEAD_RE.match(text, opened)
                head = found.group(1) if found else ""
        elif m.group() == ")":
            if depth == 0:
                raise ValueError(f"unbalanced parentheses: ')' at offset {m.start()} "
                                 "closes nothing")
            if depth == 2 and head in wanted:
                out.append((head, text[opened:m.end()]))
            depth -= 1
            closed = depth == 0
    if depth:
        raise ValueError("unbalanced parentheses: the board text is truncated")
    return out


# --- small readers ---------------------------------------------------------------------
def _child(node: Node, name: str) -> Node | None:
    return next((c for c in node[1:] if isinstance(c, list) and c and c[0] == name), None)


def _number(token: Any, where: str) -> float:
    try:
        if isinstance(token, list):
            raise ValueError
        return float(token)
    except ValueError:
        raise ValueError(f"{where}: {token!r} is not a number") from None


def _point(node: Node, key: str, where: str) -> tuple[float, float]:
    found = _child(node, key)
    if found is None or len(found) < 3:
        raise ValueError(f"{where}: missing ({key} x y)")
    return (_number(found[1], f"{where} ({key} ...)"), _number(found[2], f"{where} ({key} ...)"))


def _value(node: Node, key: str, where: str) -> float:
    found = _child(node, key)
    if found is None or len(found) < 2:
        raise ValueError(f"{where}: missing ({key} ...)")
    return _number(found[1], f"{where} ({key} ...)")


def _atoms(node: Node | None) -> tuple[str, ...]:
    return tuple(str(t) for t in (node or [])[1:] if not isinstance(t, list))


def _where(head: str, index: int, node: Node) -> str:
    uuid = _atoms(_child(node, "uuid") or _child(node, "tstamp"))
    return f"{head} #{index}" + (f" (uuid {uuid[0]})" if uuid else "")


def net_table(sources: Iterable[str]) -> dict[int, str]:
    """The KiCad <= 9 top-level net table ``(net N "name")`` (a KiCad 4/5 bare
    name keeps its text); KiCad 10+ boards have none."""
    table: dict[int, str] = {}
    for src in sources:
        args = _atoms(_tree(src))
        if len(args) >= 2 and args[0].isdigit():
            table[int(args[0])] = sr.unescape_net_name(args[1])
    return table


def _net(node: Node, table: Mapping[int, str], where: str) -> str:
    """``(net "name")`` (KiCad 10+), ``(net N "name")``, ``(net N)`` through
    the net table (0 = no net), or no net child at all."""
    args = [t for t in (_child(node, "net") or [])[1:] if not isinstance(t, list)]
    if not args:
        return sr.NO_NET
    if len(args) >= 2 or isinstance(args[0], Quoted) or not args[0].isdigit():
        return sr.unescape_net_name(str(args[-1]))
    code = int(args[0])
    if code in table:
        return table[code]
    if code == 0:
        return sr.NO_NET
    raise ValueError(f"{where} references net {code}, which the board's net table does "
                     "not define")


def _layer(node: Node) -> str:
    atoms = _atoms(_child(node, "layer"))
    return atoms[0] if atoms else ""


# --- the items -------------------------------------------------------------------------
def _track(kind: str, node: Node, table: Mapping[int, str], where: str) -> dict:
    keys = ("start", "mid", "end") if kind == "arc" else ("start", "end")
    return {"kind": kind, "layer": _layer(node), "net": _net(node, table, where),
            **{key: _point(node, key, where) for key in keys},
            "width": _value(node, "width", where)}


def _via_size(node: Node, where: str) -> float:
    sizes = [_value(node, "size", where)]
    for layer in (c for c in (_child(node, "padstack") or [])[1:]
                  if isinstance(c, list) and c and c[0] == "layer"):
        if _child(layer, "size") is not None:
            sizes.append(_value(layer, "size", f"{where} padstack layer"))
    return max(sizes)


def _via(node: Node, table: Mapping[int, str], where: str) -> dict:
    kind = next((t for t in _atoms(node) if t in VIA_TYPES), "through")
    drill = _child(node, "drill")
    return {"kind": "via", "layers": _atoms(_child(node, "layers")),
            "net": _net(node, table, where), "center": _point(node, "at", where),
            "radius": _via_size(node, where) / 2.0,
            "drill": _value(node, "drill", where) if drill is not None else None,
            "via_type": kind}


def read_copper(text: str) -> BoardCopper:
    """Every top-level segment / arc / via of a board text (module doc).
    Raises ValueError for a non-board, unbalanced text or a malformed item."""
    found = top_level_items(text, COPPER_HEADS | {"net"})
    table = net_table(src for head, src in found if head == "net")
    items, counts = [], dict.fromkeys(COPPER_HEADS, 0)
    for head, src in found:
        if head == "net":
            continue
        counts[head] += 1
        node = _tree(src)
        where = _where(head, counts[head], node)
        items.append(_via(node, table, where) if head == "via"
                     else _track(_KINDS[head], node, table, where))
    version = sr.VERSION_RE.search(text, 0, 4096)
    return BoardCopper(int(version.group(1)) if version else None, tuple(items))


def read_copper_file(path: str | Path) -> BoardCopper:
    """read_copper of a board file (UTF-8; OSError when it cannot be read)."""
    return read_copper(Path(path).read_text(encoding="utf-8", errors="replace"))


def translated(items: Iterable[Mapping[str, Any]], dx: float, dy: float) -> list[dict]:
    """NEW records with every point moved by (dx, dy) (e.g. minus the board
    frame's sheet origin); sizes, nets and layers are copied as they are."""
    def move(rec: Mapping[str, Any]) -> dict:
        return {key: (value[0] + dx, value[1] + dy) if key in _POINT_KEYS else value
                for key, value in rec.items()}
    return [move(rec) for rec in items]
