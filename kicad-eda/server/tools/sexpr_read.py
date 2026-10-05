#!/usr/bin/env python3
"""KiCad S-expression text readers, shared by net_equivalence and the KiCad
importer (kicad_import_pcb / _sch / _checks).

Two layers:

1. Regex scanning (stdlib only, any Python >= 3.11, KiCad's bundled one
   included): ``TOKEN_RE`` (a quoted string or a parenthesis), ``HEAD_RE``,
   ``head_of``, ``FP_NAME_RE``, ``VERSION_RE``, ``restore_teardrop_parens``,
   ``quote_bare_keys``, ``scan_board`` (one pass over a ``.kicad_pcb``:
   the net table plus, per footprint, the children the pad nets need) and
   ``natural_key`` / ``pad_order``.
2. Small parsed trees (``parse`` imports sexpdata lazily): ``atom``, ``tag``,
   ``children``, ``child``, ``value``, and the board pad readers
   ``pad_number``, ``pad_net``, ``net_table``, ``footprint_pads``.

KiCad 4/5 wrote pad numbers, ``fp_text`` values, footprint names and net
names as bare tokens, and KiCad keeps their text: ``(pad 01 ...)`` is pad
"01", ``1e1`` and ``1_0`` stay text (pcbnew 9.0.4 ground truth). sexpdata
would read them as numbers, so ``quote_bare_keys`` quotes those tokens before
``parse`` wherever pad keys or net names are read (``net_table``,
``footprint_pads``, kicad_import_pcb's footprints).
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

PadKey = tuple[str, str]

NO_NET = ""                     # the "net" of pads present on no net (KiCad's net 0)
LEADING = "﻿ \t\r\n"       # BOM + whitespace before the first token
TOKEN_RE = re.compile(r'"(?:[^"\\]|\\.)*"|[()]')   # a quoted string or a parenthesis
HEAD_RE = re.compile(r'\(\s*([^\s()"]+)')
FP_NAME_RE = re.compile(r'\(\s*(?:footprint|module)\s+("(?:[^"\\]|\\.)*"|[^\s()]+)')
VERSION_RE = re.compile(r"\(\s*version\s+(\d+)\s*\)")
_DIGITS = re.compile(r"([0-9]+)")
# KiCad's teardrop parser (parseTEARDROP_PARAMETERS) makes a child's '('
# optional, and some 8.99/9.0 builds wrote `(curved_edges no)filter_ratio 0.9)`
# (349 times in the KiCad 9.0.4 RoyalBlue54L-Feather demo, which KiCad loads).
# Such a bare keyword right after ')' gets its '(' back before scanning.
_TEARDROP_WORDS = ("enabled|allow_two_segments|prefer_zone_connections|best_length_ratio|"
                   "max_length|best_width_ratio|max_width|curve_points|curved_edges|filter_ratio")
_BARE_TEARDROP = re.compile(rf"\)\s*(?:{_TEARDROP_WORDS})\b")
_BARE_TEARDROP_FIX = re.compile(rf'"(?:[^"\\]|\\.)*"|(\)\s*)((?:{_TEARDROP_WORDS})\b)')
# a bare token KiCad reads as text: the pad number, a footprint name, an
# fp_text value and a numbered net's name (quoted strings are skipped whole)
_BARE_KEY = re.compile(r'"(?:[^"\\]|\\.)*"'
                       r'|(\(\s*(?:pad|module|footprint|fp_text\s+[^\s()"]+|net\s+[0-9]+)\s+)'
                       r'([^\s()"]+)')
# technical (non-copper) layer names; anything else, including KiCad 4/5
# custom copper names such as "Dessus", counts as copper
_TECH_LAYER = re.compile(r"\.(?:Paste|Mask|SilkS|Adhes|Fab|CrtYd|Cuts|User)$"
                         r"|^(?:Margin|User\.\d+)$")
_FOOTPRINT_HEADS = frozenset({"footprint", "module"})
# the footprint children the pad nets need: pads, attr (board_only) and the
# reference (KiCad 8+ property, KiCad <= 7 fp_text); other properties are
# skipped unparsed (Altium imports carry dozens per footprint)
_FOOTPRINT_PART = re.compile(r'\(\s*(?:pad\b|attr\b|property\s+"?Reference"?\s'
                             r'|fp_text\s+reference\s)')


# ----------------------------------------------------------------------------
# Small shared helpers
# ----------------------------------------------------------------------------
def natural_key(text: str) -> tuple:
    """'R10' sorts after 'R2'."""
    return tuple(int(p) if i % 2 else p for i, p in enumerate(_DIGITS.split(text)))


def pad_order(key: PadKey) -> tuple:
    return (natural_key(key[0]), natural_key(key[1]))


def unescape_net_name(name: str) -> str:
    """KiCad stores a '/' inside a label as ``{slash}`` in board and
    S-expression netlist net names (EscapeString CTX_NETNAME); the XML
    netlist is already unescaped."""
    return name.replace("{slash}", "/")


# ----------------------------------------------------------------------------
# S-expression helpers (sexpdata, lazily imported)
# ----------------------------------------------------------------------------
def parse(src: str) -> list:
    try:
        import sexpdata
    except ImportError as e:
        raise RuntimeError("reading KiCad S-expression files needs sexpdata "
                           "(server/requirements.txt)") from e
    try:
        tree = sexpdata.loads(src.lstrip(LEADING), nil=None, true=None, false=None)
    except Exception as e:      # sexpdata raises its own exception classes
        raise ValueError(f"cannot parse S-expression {src[:60]!r}: {e}") from e
    if not isinstance(tree, list):
        raise ValueError(f"not an S-expression list: {src[:60]!r}")
    return tree


def atom(x: Any) -> str:
    """A token as text: quoted string, bare word (sexpdata.Symbol is a str)
    or a number (KiCad 4/5 write pad numbers and refs unquoted)."""
    if isinstance(x, list):
        raise ValueError(f"expected a value, found a list {x!r}")
    return str(x)


def tag(node: Any) -> str:
    return str(node[0]) if isinstance(node, list) and node and not isinstance(node[0], list) else ""


def children(node: list | None, name: str) -> list[list]:
    return [c for c in (node or [])[1:] if tag(c) == name]


def child(node: list | None, name: str) -> list | None:
    found = children(node, name)
    return found[0] if found else None


def value(node: list, name: str) -> str:
    """``(name "value")`` child of node -> "value" ("" when absent/empty)."""
    found = child(node, name)
    return atom(found[1]) if found is not None and len(found) > 1 else ""


def head_of(text: str) -> str:
    m = HEAD_RE.match(text, len(text) - len(text.lstrip(LEADING)))
    return m.group(1) if m else ""


# ----------------------------------------------------------------------------
# Text repairs, as KiCad reads the file
# ----------------------------------------------------------------------------
def restore_teardrop_parens(text: str) -> str:
    """``)filter_ratio 0.9)`` -> ``)(filter_ratio 0.9)`` outside quoted
    strings, read the way KiCad reads it (see _TEARDROP_WORDS). Boards
    without the defect are returned untouched after one cheap search."""
    if not _BARE_TEARDROP.search(text):
        return text
    return _BARE_TEARDROP_FIX.sub(
        lambda m: m.group(0) if m.group(1) is None else f"{m.group(1)}({m.group(2)}", text)


def _quoted(m: re.Match) -> str:
    if m.group(1) is None:                          # a quoted string: unchanged
        return m.group(0)
    token = m.group(2).replace("\\", "\\\\")
    return f'{m.group(1)}"{token}"'


def quote_bare_keys(text: str) -> str:
    """``(pad 01 ...)`` -> ``(pad "01" ...)``, likewise the bare token after
    ``module`` / ``footprint``, ``fp_text <kind>`` and ``net <number>``,
    outside quoted strings, so ``parse`` keeps the text KiCad keeps (see the
    module doc). Quoted tokens (KiCad 6+) are left as they are."""
    return _BARE_KEY.sub(_quoted, text)


# ----------------------------------------------------------------------------
# A .kicad_pcb, read as text
# ----------------------------------------------------------------------------
@dataclass(frozen=True)
class BoardScan:
    net_table: tuple[str, ...]                           # top-level (net N "name") sources
    footprints: tuple[tuple[str, tuple[str, ...]], ...]  # (lib id, pad/reference/attr sources)


def scan_board(text: str) -> BoardScan:
    """One pass over the board text, tracking depth outside quoted strings:
    collect the top-level net table and, per footprint, only the children the
    pad nets need. sexpdata then parses those small pieces instead of the
    whole file (zone fills dominate large boards: a 89 MB board scans in
    seconds and never becomes one huge Python tree)."""
    if head_of(text) != "kicad_pcb":
        raise ValueError("not a KiCad board (expected '(kicad_pcb ...')")
    text = restore_teardrop_parens(text)
    start = len(text) - len(text.lstrip(LEADING))
    stack: list[int] = []
    nets: list[str] = []
    footprints: list[tuple[str, tuple[str, ...]]] = []
    parts: list[str] | None = None
    top, lib = "", ""
    for m in TOKEN_RE.finditer(text, start):
        tok = m.group()
        if tok == "(":
            stack.append(m.start())
            if len(stack) == 2:
                head = HEAD_RE.match(text, m.start())
                top = head.group(1) if head else ""
                parts = [] if top in _FOOTPRINT_HEADS else None
                if parts is not None:
                    name = FP_NAME_RE.match(text, m.start())
                    lib = name.group(1).strip('"') if name else "?"
        elif tok == ")":
            if not stack:
                raise ValueError(f"unbalanced parentheses: ')' at offset {m.start()} "
                                 "closes nothing")
            opened = stack.pop()
            depth = len(stack) + 1
            if depth == 3 and parts is not None:
                if _FOOTPRINT_PART.match(text, opened):
                    parts.append(text[opened:m.end()])
            elif depth == 2:
                if top == "net":
                    nets.append(text[opened:m.end()])
                elif parts is not None:
                    footprints.append((lib, tuple(parts)))
                parts = None
    if stack:
        raise ValueError(f"unbalanced parentheses: '(' at offset {stack[-1]} is never closed")
    return BoardScan(tuple(nets), tuple(footprints))


def net_table(sources: Iterable[str]) -> dict[int, str]:
    """KiCad <= 9 board net table ``(net N "name")``; KiCad 10+ has none.
    A KiCad 4/5 bare name keeps its text (``(net 3 01)`` is "01")."""
    table: dict[int, str] = {}
    for src in sources:
        node = parse(quote_bare_keys(src))
        if len(node) >= 3 and isinstance(node[1], int):
            table[node[1]] = unescape_net_name(atom(node[2]))
    return table


def pad_net(pad: list, table: Mapping[int, str], where: str) -> str:
    """The pad's net: ``(net N "name")`` (KiCad <= 9), ``(net "name")``
    (KiCad 10+), ``(net N)`` resolved through the table, or NO_NET."""
    for item in pad[2:]:
        if tag(item) != "net":
            continue
        args = item[1:]
        if len(args) >= 2:
            return unescape_net_name(atom(args[1]))
        if not args:
            return NO_NET
        only = args[0]
        if isinstance(only, int):
            if only in table:
                return table[only]
            if only == 0:
                return NO_NET
            raise ValueError(f"{where} references net {only}, which the board's net table "
                             "does not define")
        return unescape_net_name(atom(only))
    return NO_NET


def pad_number(pad: list) -> str:
    """The pad's number as KiCad sees it after loading: PAD::CanHaveNumber
    clears it for NPTH pads and for aperture pads (no copper layer), so
    those carry no pad key; neither does an empty number. Parse the pad
    through quote_bare_keys so a KiCad 4/5 ``01`` stays "01"."""
    if len(pad) < 2 or isinstance(pad[1], list):
        return ""
    if len(pad) > 2 and not isinstance(pad[2], list) and str(pad[2]) == "np_thru_hole":
        return ""
    layers = child(pad, "layers")
    if layers is not None and len(layers) > 1 and all(
            _TECH_LAYER.search(atom(layer)) for layer in layers[1:]):
        return ""
    return atom(pad[1])


def footprint_pads(lib: str, parts: Iterable[str], table: Mapping[int, str]
                   ) -> tuple[str, bool, list[tuple[str, str]]]:
    """(reference, board_only, [(pad number, net)]) of one footprint (the
    scan_board parts). A footprint without a reference keeps ref "" (pcbnew
    reads it that way)."""
    ref: str | None = None
    board_only = False
    numbered: list[tuple[str, list]] = []
    for src in parts:
        node = parse(quote_bare_keys(src))
        head = tag(node)
        if head in ("property", "fp_text") and len(node) >= 3:
            # KiCad 8+ (property "Reference" ...) wins over KiCad <= 7 (fp_text reference ...)
            if atom(node[1]) in ("Reference", "reference") and (head == "property" or ref is None):
                ref = atom(node[2])
        elif head == "attr":
            board_only = "board_only" in {str(t) for t in node[1:] if not isinstance(t, list)}
        elif head == "pad" and (number := pad_number(node)):
            numbered.append((number, node))
    ref = ref or ""
    return ref, board_only, [
        (number, pad_net(pad, table, f"{ref or '(no reference)'} pad {number} ({lib})"))
        for number, pad in numbered]
