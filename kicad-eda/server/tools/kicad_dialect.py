#!/usr/bin/env python3
"""KiCad 10/11 footprint and symbol libraries in KiCad 9's dialect (ROADMAP D0).

KiCad 9 loads footprints up to ``(version 20241229)`` and symbols up to
``20241209``; KiCad 10 libraries and imports made through KiCad 11 are newer.
The text is edited in place (formatting kept): the version becomes KiCad 9's
and every list and keyword meets POLICY (DROP: KiCad 10+ only, pad/pin-neutral;
REWRITE: to the KiCad 9 form; REFUSE: copper or pin meaning KiCad 9 cannot
express), else GRAMMAR (the pairs KiCad 9 writes, plus K9_PROVEN); anything
else is refused, token and item named. A derived symbol moves after the symbol it extends. A
refused footprint is not written; a refused symbol is left out with every symbol extending it.
k9_check proves a result with KiCad 9's own ``kicad-cli fp|sym upgrade``: the pad or pin table
of the re-save must equal the input's. Stdlib only (KiCad 9's Python 3.11).

CLI: ``fp <src.pretty> <dst.pretty> [--check]``, ``sym <src.kicad_sym>
<dst.kicad_sym> [--check]``, ``survey <out.json> [--root <share/kicad>] [--jobs N]``.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

K9_FP_VERSION = 20241229            # KiCad 9's footprint / board dialect (D0)
K9_SYM_VERSION = 20241209           # KiCad 9's symbol library dialect (D0)
K9_GENERATOR_VERSION = "9.0"
K9_CHECK_TIMEOUT_S = 900.0
DECIMALS = 4                        # the pad / pin tables compare numbers to 0.1 um
ROOT = "/"                          # the parent of a file's root list
ROOT_HEAD = {"fp": "footprint", "sym": "kicad_symbol_lib"}
TO_VERSION = {"fp": K9_FP_VERSION, "sym": K9_SYM_VERSION}
DATA_HEADS = frozenset({"data"})    # embedded files: bare base64 words, never grammar
DROP, REWRITE, REFUSE = "drop", "rewrite", "refuse"
_BOM = chr(0xFEFF)
_TOKEN = re.compile(r'"(?:[^"\\]|\\.)*"|[()]|[^\s()"]+|"')
_NUMBER = re.compile(r"[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?\Z")
_ERROR = re.compile(r"(^|\s)Error:")
_MODEL_VAR = re.compile(r"\$\{KICAD(\d+)_3DMODEL_DIR\}")
_BOOLS = frozenset({"yes", "no"})


class DialectError(ValueError):
    """Text that is not one well-formed S-expression list."""


# --- Scanner: a quote-aware parenthesis tree with source offsets -------------
@dataclass(frozen=True)
class Node:
    """A list: ``head`` is its first token when that is a bare word (else ""),
    ``atoms`` its other tokens as written; text[start:end] is the list."""
    head: str
    start: int
    end: int
    atoms: tuple[str, ...]
    kids: tuple[Node, ...]

    def kid(self, head: str) -> Node | None:
        return next((k for k in self.kids if k.head == head), None)

    @property
    def first(self) -> str:
        return self.atoms[0] if self.atoms else ""

    @property
    def keywords(self) -> list[str]:
        """Bare words (not strings, not numbers): grammar, not data."""
        return [] if self.head in DATA_HEADS else [
            a for a in self.atoms if a[0] != '"' and not _NUMBER.match(a)]

    @property
    def version(self) -> int | None:
        return int(v.first) if (v := self.kid("version")) and v.first.isdigit() else None


def parse(text: str, start: int = 0, end: int | None = None, depth: int = 1 << 30) -> Node:
    """The one list in text[start:end]; lists nested more than ``depth`` below
    it are skipped. Parentheses inside strings are text. Raises DialectError."""
    stack: list[list[Any]] = []
    hidden = 0
    root: Node | None = None
    start += text.startswith(_BOM, start)
    for m in _TOKEN.finditer(text, start, len(text) if end is None else end):
        tok = m.group()
        if tok == '"':
            raise DialectError(f"unterminated string at offset {m.start()}")
        if hidden:
            hidden += (tok == "(") - (tok == ")")
        elif tok == "(":
            if root is not None:
                raise DialectError(f"a second list at offset {m.start()}")
            if len(stack) > depth:
                hidden = 1
            else:
                stack.append([m.start(), None, [], []])
        elif tok == ")":
            if not stack:
                raise DialectError(f"')' at offset {m.start()} closes nothing")
            at, head, atoms, kids = stack.pop()
            node = Node(head or "", at, m.end(), tuple(atoms), tuple(kids))
            if stack:
                stack[-1][3].append(node)
            else:
                root = node
        elif not stack:
            raise DialectError(f"{tok[:40]!r} at offset {m.start()} is outside any list")
        elif stack[-1][1] is None and not stack[-1][2] and not stack[-1][3] and tok[0] != '"':
            stack[-1][1] = tok
        else:
            stack[-1][2].append(tok)
    if stack:
        raise DialectError(f"'(' at offset {stack[-1][0]} is never closed")
    if root is None:
        raise DialectError("no S-expression list")
    return root


def unquote(atom: str) -> str:
    if len(atom) < 2 or atom[0] != '"' or atom[-1] != '"':
        return atom
    return re.sub(r"\\(.)", lambda m: {"n": "\n", "t": "\t"}.get(m.group(1), m.group(1)),
                  atom[1:-1])


def _read(path: Path) -> str:
    with open(path, encoding="utf-8", newline="") as f:     # line endings as written
        return f.read()


def _write(path: Path, text: str) -> None:
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)


# --- KiCad 9 grammar ----------------------------------------------------------
def load_grammar(text: str) -> dict[str, dict[str, frozenset[str]]]:
    """``kind parent: tokens; ...`` as {kind: {parent: tokens}}, booleans paired."""
    acc: dict[str, dict[str, set[str]]] = {}
    for entry in filter(str.strip, text.split(";")):
        label, _, toks = entry.partition(":")
        kind, parent = label.split()
        acc.setdefault(kind, {}).setdefault(parent, set()).update(toks.split())
    return {kind: {p: frozenset(t | _BOOLS if t & _BOOLS else t) for p, t in table.items()}
            for kind, table in acc.items()}


# The (parent, token) pairs of KiCad 9.0.4's stock and demo files, as the tests' build_grammar
# finds them; their live tier proves this text equals format_grammar's output.
_K9_GRAMMAR = """
fp /: (footprint; fp allow_two_segments: yes; fp anchor: circle rect; fp arc: (end (mid (start;
fp arrow_direction: outward; fp attr: (teardrop allow_missing_courtyard allow_soldermask_bridges
board_only dnp exclude_from_bom exclude_from_pos_files smd through_hole; fp bold: yes;
fp border: yes; fp chamfer: bottom_left bottom_right top_left top_right; fp clearance: convexhull
outline; fp connect_pads: (clearance yes; fp copperpour: allowed not_allowed; fp curved_edges: no
yes; fp dimension: (format (gr_text (height (layer (pts (style (type (uuid; fp drill: (offset oval;
fp effects: (font (justify; fp embedded_files: (file; fp embedded_fonts: no; fp enabled: no yes;
fp file: (checksum (data (name (type; fp fill: (hatch_border_algorithm (hatch_gap
(hatch_min_hole_area (hatch_orientation (hatch_thickness (island_area_min (island_removal_mode
(mode (radius (smoothing (thermal_bridge_width (thermal_gap no yes; fp filled_areas_thickness: no;
fp filled_polygon: (layer (pts; fp font: (bold (face (size (thickness; fp footprint: (at (attr
(clearance (descr (dimension (embedded_files (embedded_fonts (fp_arc (fp_circle (fp_line (fp_poly
(fp_rect (fp_text (fp_text_box (generator (generator_version (group (layer (locked (model
(net_tie_pad_groups (pad (path (private_layers (property (sheetfile (sheetname (solder_mask_margin
(solder_paste_margin (solder_paste_margin_ratio (solder_paste_ratio (tags (uuid (version (zone
(zone_connect; fp footprints: allowed not_allowed; fp format: (override_value (precision (prefix
(suffix (units (units_format; fp fp_arc: (end (layer (mid (start (stroke (uuid;
fp fp_circle: (center (end (fill (layer (stroke (uuid; fp fp_line: (end (layer (start (stroke
(uuid; fp fp_poly: (fill (layer (pts (stroke (uuid; fp fp_rect: (end (fill (layer (start (stroke
(uuid; fp fp_text: (at (effects (hide (layer (render_cache (unlocked (uuid user;
fp fp_text_box: (border (effects (end (layer (margins (start (stroke (uuid; fp gr_arc: (end (mid
(start (width; fp gr_circle: (center (end (fill (width; fp gr_line: (end (start (width;
fp gr_poly: (fill (pts (width; fp gr_rect: (end (fill (start (width; fp gr_text: (at (effects
(layer (uuid; fp group: (members (uuid; fp hatch: edge full none;
fp hatch_border_algorithm: hatch_thickness; fp hide: yes; fp justify: bottom left mirror right top;
fp keep_end_layers: yes; fp keep_text_aligned: yes; fp keepout: (copperpour (footprints (pads
(tracks (vias; fp locked: yes; fp mode: hatch; fp model: (hide (offset (rotate (scale;
fp offset: (xyz; fp options: (anchor (clearance; fp pad: (at (chamfer (chamfer_ratio (clearance
(die_length (drill (keep_end_layers (layers (net (options (pinfunction (pintype (primitives
(property (rect_delta (remove_unused_layers (roundrect_rratio (size (solder_mask_margin
(solder_paste_margin (solder_paste_margin_ratio (teardrops (thermal_bridge_angle
(thermal_bridge_width (thermal_gap (uuid (zone_connect circle connect custom np_thru_hole oval rect
roundrect smd thru_hole trapezoid; fp pads: allowed not_allowed; fp placement: (enabled (sheetname;
fp polygon: (pts; fp prefer_zone_connections: yes; fp primitives: (gr_arc (gr_circle (gr_line
(gr_poly (gr_rect; fp property: (at (effects (hide (layer (unlocked (uuid ki_fp_filters
pad_prop_bga pad_prop_fiducial_loc pad_prop_heatsink; fp pts: (arc (xy; fp remove_unused_layers: no
yes; fp render_cache: (polygon; fp rotate: (xyz; fp scale: (xyz; fp smoothing: fillet;
fp stroke: (type (width; fp style: (arrow_direction (arrow_length (extension_height
(extension_offset (keep_text_aligned (text_frame (text_position_mode (thickness;
fp teardrop: (type; fp teardrops: (allow_two_segments (best_length_ratio (best_width_ratio
(curved_edges (enabled (filter_ratio (max_length (max_width (prefer_zone_connections;
fp tracks: allowed not_allowed; fp type: aligned dash datasheet default leader model padvia solid;
fp unlocked: yes; fp vias: allowed not_allowed; fp zone: (attr (connect_pads (fill
(filled_areas_thickness (filled_polygon (hatch (keepout (layer (layers (min_thickness (name (net
(net_name (placement (polygon (priority (uuid; sym /: (kicad_symbol_lib;
sym alternate: bidirectional clock input line no_connect open_collector output passive power_in
power_out tri_state; sym arc: (end (fill (mid (start (stroke; sym bezier: (fill (pts (stroke;
sym bold: yes; sym circle: (center (fill (radius (stroke; sym effects: (font (hide (justify;
sym embedded_fonts: no yes; sym exclude_from_sim: no yes; sym fill: (type; sym font: (bold (italic
(size (thickness; sym hide: yes; sym in_bom: no yes; sym italic: yes; sym justify: bottom left
right top; sym kicad_symbol_lib: (generator (generator_version (symbol (version;
sym name: (effects; sym number: (effects; sym on_board: no yes; sym pin: (alternate (at (hide
(length (name (number bidirectional clock clock_low free hide input input_low inverted
inverted_clock line no_connect non_logic open_collector open_emitter output output_low passive
power_in power_out tri_state unspecified; sym pin_names: (hide (offset; sym pin_numbers: (hide;
sym polyline: (fill (pts (stroke; sym property: (at (do_not_autoplace (effects (show_name;
sym pts: (xy; sym rectangle: (end (fill (start (stroke; sym stroke: (color (type (width;
sym symbol: (arc (bezier (circle (embedded_fonts (exclude_from_sim (extends (in_bom (on_board (pin
(pin_names (pin_numbers (polyline (power (property (rectangle (symbol (text (text_box;
sym text: (at (effects; sym text_box: (at (effects (fill (margins (size (stroke;
sym type: background dash default none outline solid
"""

# KiCad 9 grammar no KiCad 9 file uses: KiCad 9.0.4 loads each and keeps its meaning
# on a re-save (tests/test_kicad_dialect.py::test_k9_proven_survives_a_kicad9_resave)
K9_PROVEN = ("fp property: pad_prop_mechanical; sym property: (hide private; "
             "sym symbol: (unit_name; sym show_name: no yes; sym do_not_autoplace: no yes")
GRAMMAR = load_grammar(_K9_GRAMMAR + ";" + K9_PROVEN)


# --- Policy -------------------------------------------------------------------
@dataclass(frozen=True)
class Rule:
    action: str                     # DROP | REWRITE | REFUSE
    note: str
    replace: str = ""               # REWRITE: the KiCad 9 text


_DEFAULT = "KiCad 10's default"
_JUMPERS = "jumpered inside the part (KiCad 10); KiCad 9 has no form for it"
# (kind, parent or "*", head, first atom or "" for any) -> Rule
POLICY: Mapping[tuple[str, str, str, str], Rule] = {
    ("fp", "footprint", "duplicate_pad_numbers_are_jumpers", "no"): Rule(DROP, _DEFAULT),
    ("fp", "footprint", "duplicate_pad_numbers_are_jumpers", ""): Rule(
        REFUSE, f"pads sharing a number are {_JUMPERS}"),
    ("fp", "footprint", "jumper_pad_groups", ""): Rule(REFUSE, f"pad groups {_JUMPERS}"),
    ("fp", "footprint", "point", ""): Rule(DROP, "a KiCad 10 snap marker (no copper) removed"),
    ("fp", "*", "knockout", "no"): Rule(DROP, _DEFAULT),
    ("sym", "symbol", "duplicate_pin_numbers_are_jumpers", "no"): Rule(DROP, _DEFAULT),
    ("sym", "symbol", "duplicate_pin_numbers_are_jumpers", ""): Rule(
        REFUSE, f"pins sharing a number are {_JUMPERS}"),
    ("sym", "symbol", "jumper_pin_groups", ""): Rule(REFUSE, f"pin groups {_JUMPERS}"),
    ("sym", "symbol", "in_pos_files", "yes"): Rule(DROP, _DEFAULT),
    ("sym", "symbol", "in_pos_files", "no"): Rule(
        DROP, "position-file exclusion lost (KiCad 9 takes it from the footprint's attr)"),
    ("sym", "symbol", "body_styles", "demorgan"): Rule(DROP, "KiCad 9 reads De Morgan from _N_2"),
    ("sym", "symbol", "body_styles", ""): Rule(REFUSE, "named body styles have no KiCad 9 form"),
    ("sym", "symbol", "power", "global"): Rule(REWRITE, "KiCad 9 power is global", "(power)"),
    ("sym", "symbol", "power", "local"): Rule(
        REWRITE, "local power scope lost; pad-level netlists carry nets explicitly", "(power)"),
    ("sym", "rectangle", "radius", ""): Rule(DROP, "rounded corners drawn square (graphic only)"),
}
_MOVED = (REWRITE, "(extends ...) after its parent",
          "KiCad 9 resolves extends while reading: a derived symbol follows its parent")


# --- Downgrade ----------------------------------------------------------------
@dataclass(frozen=True)
class Change:
    rule: str
    count: int
    note: str


@dataclass(frozen=True)
class DowngradeResult:
    """``text`` is "" when refused; a symbol library refuses symbol by symbol
    (``refused_items``: (name, construct, reason)) and keeps the rest."""
    text: str
    from_version: int | None
    to_version: int | None
    dropped: tuple[Change, ...]
    rewritten: tuple[Change, ...]
    refused: str | None
    construct: str | None = None
    refused_items: tuple[tuple[str, str, str], ...] = ()
    names: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.refused is None and not self.refused_items

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "from_version": self.from_version, "to_version": self.to_version,
                "dropped": {c.rule: c.count for c in self.dropped},
                "rewritten": {c.rule: c.count for c in self.rewritten},
                "notes": {c.rule: c.note for c in self.dropped + self.rewritten if c.note},
                "refused": self.refused, "construct": self.construct}


class _Refused(Exception):
    def __init__(self, construct: str, reason: str) -> None:
        super().__init__(f"{construct}: {reason}")
        self.construct, self.reason = construct, reason


Edit = tuple[int, int, str]
Used = tuple[str, str, str]          # (action, label, note)


def _rule(kind: str, parent: str, node: Node) -> tuple[str, Rule] | None:
    """(label, rule) of the POLICY row for node, if any."""
    for p in (parent, "*"):
        for v in (node.first, ""):
            if (rule := POLICY.get((kind, p, node.head, v))) is not None:
                shown = node.first if node.first in node.keywords else "..."
                where = "" if parent in ("footprint", "symbol") else f" in ({parent})"
                label = f"({node.head} {shown}){where}"
                return (f"{label} -> {rule.replace}" if rule.action == REWRITE else label), rule
    return None


def _check(kind: str, node: Node, parent: str) -> None:
    """Refuse a list or keyword KiCad 9's grammar does not have there."""
    table = GRAMMAR[kind]
    if "(" + node.head not in table.get(parent, ()):
        where = "the file" if parent == ROOT else f"({parent})"
        raise _Refused(f"({node.head} ...) in {where}", "not KiCad 9 grammar")
    for word in node.keywords:
        if word not in table.get(node.head, ()):
            raise _Refused(f"keyword {word} in ({node.head})", "not KiCad 9 grammar")


def _drop(text: str, node: Node) -> Edit:
    """Remove node with the whitespace before it."""
    at = node.start
    while at > 0 and text[at - 1] in " \t\r\n":
        at -= 1
    return (at, node.end, "")


def _walk(kind: str, text: str, node: Node, parent: str, edits: list[Edit],
          used: list[Used]) -> None:
    """POLICY, else GRAMMAR, for node and below (appends edits, rules used)."""
    found = _rule(kind, parent, node)
    if found is not None:
        label, rule = found
        if rule.action == REFUSE:
            raise _Refused(label, rule.note)
        used.append((rule.action, label, rule.note))
        edits.append(_drop(text, node) if rule.action == DROP
                     else (node.start, node.end, rule.replace))
        return
    _check(kind, node, parent)
    m = _MODEL_VAR.search(node.first) if node.head == "model" else None
    if m and int(m.group(1)) > 9:
        at = text.index(node.first, node.start) + m.start()
        edits.append((at, at + len(m.group()), "${KICAD9_3DMODEL_DIR}"))
        used.append((REWRITE, f"KICAD{m.group(1)}_3DMODEL_DIR -> KICAD9_3DMODEL_DIR",
                     "the 3D model path variable of KiCad 9"))
    for kid in node.kids:
        _walk(kind, text, kid, node.head, edits, used)


def _apply(text: str, edits: Iterable[Edit], start: int = 0, end: int | None = None) -> str:
    """text[start:end] with the edits (absolute offsets inside it) applied."""
    out, pos = [], start
    for a, b, new in sorted(edits):
        out += [text[pos:a], new]
        pos = b
    return "".join(out + [text[pos:end]])


def _changes(used: Iterable[Used], action: str) -> tuple[Change, ...]:
    mine = [(label, note) for act, label, note in used if act == action]
    notes = dict(mine)
    return tuple(Change(label, n, notes[label])
                 for label, n in sorted(Counter(label for label, _ in mine).items()))


def _start(kind: str, text: str, depth: int
           ) -> tuple[Node | None, list[Edit], DowngradeResult | None]:
    """(root, version edits, None) for text newer than KiCad 9's dialect, else
    (None, [], the result): the text unchanged (KiCad 9 or older) or a refusal."""
    try:
        root = parse(text, depth=depth)
    except DialectError as e:
        return None, [], DowngradeResult("", None, None, (), (), f"unreadable: {e}", "unreadable")
    if root.head != ROOT_HEAD[kind]:
        return None, [], DowngradeResult("", None, None, (), (), f"not a {ROOT_HEAD[kind]} "
                                         f"file (found ({root.head} ...))", "not a library")
    if root.version is None or root.version <= TO_VERSION[kind]:
        names = (unquote(root.first),) if kind == "fp" else tuple(
            unquote(k.first) for k in root.kids if k.head == "symbol")
        return None, [], DowngradeResult(text, root.version, root.version, (), (), None,
                                         names=names)
    return root, [(k.start, k.end, f"(version {TO_VERSION[kind]})" if k.head == "version" else
                   f'(generator_version "{K9_GENERATOR_VERSION}")')
                  for k in root.kids if k.head in ("version", "generator_version")], None


def downgrade_footprint(text: str) -> DowngradeResult:
    """A .kicad_mod in KiCad 9's dialect (KiCad 9 or older text unchanged)."""
    root, edits, done = _start("fp", text, 1 << 30)
    if root is None:
        return done  # type: ignore[return-value]
    name, used = unquote(root.first), []
    try:
        _walk("fp", text, root, ROOT, edits, used)
    except _Refused as r:
        return DowngradeResult("", root.version, K9_FP_VERSION, (), (),
                               f"footprint '{name}': {r}", r.construct, names=(name,))
    return DowngradeResult(_apply(text, edits), root.version, K9_FP_VERSION,
                           _changes(used, DROP), _changes(used, REWRITE), None, names=(name,))


def _moves(names: list[str], parents: Mapping[str, str | None]) -> dict[str, list[str]]:
    """{symbol: derived symbols listed before it, to move right after it}."""
    known, done, waiting, moves = set(names), set(), {}, {}

    def emit(name: str, anchor: str) -> None:
        done.add(name)
        for child in waiting.pop(name, []):
            moves.setdefault(anchor, []).append(child)
            emit(child, anchor)
    for name in names:
        if parents.get(name) in known and parents[name] not in done:
            waiting.setdefault(parents[name], []).append(name)
        else:
            emit(name, name)
    return moves


def _symbols(text: str, lib: Node, edits: list[Edit], used: list[Used]
             ) -> tuple[dict[str, tuple[Node, list[Edit], list[Used]]],
                        dict[str, str | None], dict[str, _Refused]]:
    """(per symbol (node, edits, rules used), what each extends, the refused
    and those extending them). Raises _Refused for an item that is no symbol."""
    per: dict[str, tuple[Node, list[Edit], list[Used]]] = {}
    parents: dict[str, str | None] = {}
    refused: dict[str, _Refused] = {}
    for kid in lib.kids:
        node = parse(text, kid.start, kid.end)
        if kid.head != "symbol":
            _walk("sym", text, node, lib.head, edits, used)
            continue
        name, ext, mine, rules = unquote(kid.first), node.kid("extends"), [], []
        parents[name] = unquote(ext.first) if ext else None
        per[name] = (kid, mine, rules)
        try:
            _walk("sym", text, node, lib.head, mine, rules)
        except _Refused as r:
            refused[name] = r
    while bad := [(n, p) for n, p in parents.items() if n not in refused and p in refused]:
        refused.update({n: _Refused(f'(extends "{p}")', f"symbol '{p}' is refused")
                        for n, p in bad})
    return per, parents, refused


def downgrade_symbol_library(text: str) -> DowngradeResult:
    """A .kicad_sym in KiCad 9's dialect without the refused symbols (and the
    symbols extending them); KiCad 9 or older text comes back unchanged."""
    lib, edits, done = _start("sym", text, 1)
    if lib is None:
        return done  # type: ignore[return-value]
    used: list[Used] = []
    try:
        _check("sym", lib, ROOT)
        per, parents, refused = _symbols(text, lib, edits, used)
    except _Refused as r:
        return DowngradeResult("", lib.version, K9_SYM_VERSION, (), (),
                               f"symbol library: {r}", r.construct)
    moves = _moves(list(per), parents)
    moved = {name for names in moves.values() for name in names if name not in refused}
    for name, (kid, mine, rules) in per.items():
        edits += [_drop(text, kid)] if name in refused or name in moved else mine
        used += [] if name in refused else rules
    for anchor, names in moves.items():          # each moved symbol keeps its indent
        end, blocks = per[anchor][0].end, [per[n][:2] for n in names if n in moved]
        edits.append((end, end, "".join(text[_drop(text, k)[0]:k.start] + _apply(
            text, e, k.start, k.end) for k, e in blocks)))
    return DowngradeResult(
        _apply(text, edits), lib.version, K9_SYM_VERSION, _changes(used, DROP),
        _changes(used + [_MOVED] * len(moved), REWRITE), None, None,
        tuple((n, r.construct, r.reason) for n, r in refused.items()), tuple(per))


# --- Files --------------------------------------------------------------------
def _totals(results: Iterable[Mapping[str, Any]], key: str) -> dict[str, int]:
    return dict(sum((Counter(r.get(key, {})) for r in results), Counter()))


def _report(src: Path, dst: Path, items: Mapping[str, dict[str, Any]],
            changes: list[Mapping[str, Any]]) -> dict[str, Any]:
    ok = sum(i["ok"] for i in items.values())
    return {"ok": ok == len(items), "src": str(src), "dst": str(dst),
            "counts": {"items": len(items), "ok": ok, "refused": len(items) - ok},
            "dropped": _totals(changes, "dropped"), "rewritten": _totals(changes, "rewritten"),
            "items": dict(items)}


def downgrade_pretty(src_dir: str | Path, dst_dir: str | Path,
                     names: Iterable[str] | None = None) -> dict[str, Any]:
    """Footprints of src_dir (all, or ``names``) into dst_dir: {ok, counts, dropped,
    rewritten, items}; a refused one leaves no file there (a stale one is removed)."""
    src, dst = Path(src_dir), Path(dst_dir)
    if not src.is_dir():
        raise FileNotFoundError(f"no footprint library {src}")
    if dst.resolve() == src.resolve():
        raise ValueError(f"{dst} is the source library; choose another destination")
    found = {p.stem: p for p in sorted(src.glob("*.kicad_mod"))}
    dst.mkdir(parents=True, exist_ok=True)
    items: dict[str, dict[str, Any]] = {}
    for name in (list(found) if names is None else list(dict.fromkeys(names))):
        res = downgrade_footprint(_read(found[name])) if name in found else DowngradeResult(
            "", None, None, (), (), f"no {name}.kicad_mod in {src}", "missing")
        if res.ok:
            _write(dst / f"{name}.kicad_mod", res.text)
        else:
            (dst / f"{name}.kicad_mod").unlink(missing_ok=True)
        items[name] = res.as_dict() if res.ok else {
            "ok": False, "construct": res.construct, "reason": res.refused}
    return _report(src, dst, items, list(items.values()))


def downgrade_symbol_file(src: str | Path, dst: str | Path) -> dict[str, Any]:
    """A .kicad_sym into dst without its refused symbols: {ok, counts, dropped,
    rewritten, items, written}; a library refused whole is not written."""
    src, dst = Path(src), Path(dst)
    if dst.resolve() == src.resolve():
        raise ValueError(f"{dst} is the source library; choose another destination")
    res = downgrade_symbol_library(_read(src))
    if res.refused is not None:
        dst.unlink(missing_ok=True)
        whole = {"ok": False, "construct": res.construct, "reason": res.refused}
        return {**_report(src, dst, {"(library)": whole}, []), "written": False}
    items: dict[str, dict[str, Any]] = {n: {"ok": True} for n in res.names}
    items.update({n: {"ok": False, "construct": c, "reason": f"symbol '{n}': {c}: {r}"}
                  for n, c, r in res.refused_items})
    dst.parent.mkdir(parents=True, exist_ok=True)
    _write(dst, res.text)
    return {**_report(src, dst, items, [res.as_dict()]), "written": True}


# --- Pad and pin tables -------------------------------------------------------
PAD_FIELDS = frozenset({"size", "drill", "layers", "roundrect_rratio", "chamfer_ratio", "chamfer",
                        "rect_delta", "property", "primitives", "options"})
# (list, absent kid) -> the kid KiCad 9 and the 10.99 nightly both read it as; each writes
# it out on a re-save (tests/test_kicad_dialect.py::test_live_pad_defaults_are_read_alike)
PAD_DEFAULTS: Mapping[tuple[str, str], str] = {("gr_circle", "fill"): "(fill no)"}


def _num(atom: str) -> str:
    return repr(round(float(atom), DECIMALS) + 0.0) if _NUMBER.match(atom) else unquote(atom)


def _canon(node: Node) -> str:
    atoms = [_num(a) for a in node.atoms]
    kids = [_canon(k) for k in node.kids if k.head != "uuid"] + [
        d for (h, k), d in PAD_DEFAULTS.items() if h == node.head and node.kid(k) is None]
    return "(" + " ".join([node.head, *(sorted(atoms) if node.head in ("layers", "chamfer")
                                        else atoms), *kids]) + ")"


def _at(node: Node | None) -> tuple[float, ...]:
    v = [float(a) for a in (node.atoms if node else ()) if _NUMBER.match(a)] + [0.0] * 3
    return tuple(round(x, DECIMALS) + 0.0 for x in (v[0], v[1], v[2] % 360.0))


def _pin_row(pin: Node) -> tuple[Any, ...]:
    hide, name, number, length = (pin.kid(h) for h in ("hide", "name", "number", "length"))
    return (pin.atoms[:2], _at(pin.kid("at")), _num(length.first) if length else "",
            "hide" in pin.atoms or (hide is not None and hide.first != "no"),
            unquote(name.first) if name else "", unquote(number.first) if number else "",
            sorted(" ".join(map(unquote, a.atoms)) for a in pin.kids if a.head == "alternate"))


def rows(kind: str, text: str) -> tuple[str, ...]:
    """A footprint's pad table or a symbol's pin table (+ extends, power), sorted."""
    node = parse(text)
    if kind == "fp":
        return tuple(sorted(repr((unquote(p.first), p.atoms[1:3], _at(p.kid("at")), sorted(
            _canon(k) for k in p.kids if k.head in PAD_FIELDS))) for p in node.kids
            if p.head == "pad"))
    ext = node.kid("extends")
    out = [repr(("extends", unquote(ext.first) if ext else "", node.kid("power") is not None))]
    for unit in (k for k in node.kids if k.head == "symbol"):
        tag = "_".join(unquote(unit.first).rsplit("_", 2)[-2:])     # unit _ body style
        out += [repr((tag, *_pin_row(p))) for p in unit.kids if p.head == "pin"]
    return tuple(sorted(out))


# --- KiCad 9 proof --------------------------------------------------------------
def _run(argv: list[str], timeout_s: float) -> tuple[int, str]:
    """(exit code, output) of a KiCad child, stdin detached (D0); -1: no start / timeout."""
    try:
        r = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout_s, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return -1, f"{argv[0]}: timed out after {timeout_s:g} s"
    except OSError as e:
        return -1, f"{argv[0]}: cannot start ({e})"
    return r.returncode, (r.stdout or "") + (r.stderr or "")


@dataclass(frozen=True)
class _Lib:
    """A library as k9_check reads it: items (name -> text) in library order."""
    kind: str
    header: str                     # a symbol library's text before its first symbol
    items: Mapping[str, str]
    parents: Mapping[str, str]      # symbol -> the symbol it extends

    @classmethod
    def open(cls, kind: str, path: Path) -> _Lib:
        if kind == "fp":
            files = sorted(path.glob("*.kicad_mod")) if path.is_dir() else [path]
            return cls(kind, "", {p.stem: _read(p) for p in files}, {})
        text = _read(path)
        lib = parse(text, depth=1)
        syms = [k for k in lib.kids if k.head == "symbol"]
        ext = {unquote(k.first): parse(text, k.start, k.end, depth=1).kid("extends") for k in syms}
        return cls(kind, text[:syms[0].start] if syms else text[:lib.end - 1],
                   {unquote(k.first): text[k.start:k.end] for k in syms},
                   {n: unquote(e.first) for n, e in ext.items() if e is not None})

    def write(self, names: Iterable[str], folder: Path) -> Path:
        """A library of ``names`` and the symbols they extend (library order)."""
        chosen, todo = set(names), list(names)
        while todo:
            parent = self.parents.get(todo.pop())
            if parent in self.items and parent not in chosen:
                chosen.add(parent)
                todo.append(parent)
        wanted = [n for n in self.items if n in chosen]
        out = folder / ("in.pretty" if self.kind == "fp" else "in.kicad_sym")
        if self.kind == "sym":
            _write(out, self.header + "\n\t".join(self.items[n] for n in wanted) + "\n)\n")
            return out
        out.mkdir()
        for name in wanted:
            _write(out / f"{name}.kicad_mod", self.items[name])
        return out

    def resave(self, cli: Path, names: Iterable[str], tmp: Path, timeout_s: float
               ) -> tuple[list[str], dict[str, str]]:
        """(errors, {name: KiCad 9 text}) of a KiCad 9 upgrade of those items."""
        folder = Path(tempfile.mkdtemp(dir=tmp))
        src = self.write(names, folder)
        out = folder / ("k9.pretty" if self.kind == "fp" else "k9.kicad_sym")
        code, log = _run([str(cli), self.kind, "upgrade", "--force", "-o", str(out), str(src)],
                         timeout_s)
        errors = [line.strip() for line in log.splitlines() if _ERROR.search(line)]
        if code != 0:
            errors.append(f"kicad-cli {self.kind} upgrade exit {code}: {log.strip()[-300:]}")
        elif not out.exists():
            errors.append(f"kicad-cli {self.kind} upgrade wrote nothing")
        return errors, ({} if errors else dict(_Lib.open(self.kind, out).items))

    def compare(self, saved: Mapping[str, str], failed: list[str]) -> dict[str, Any]:
        differing = []
        for name in (n for n in self.items if n in saved):
            a, b = rows(self.kind, self.items[name]), rows(self.kind, saved[name])
            if a != b:
                differing.append({"item": name, "input": sorted(set(a) - set(b))[:4],
                                  "k9": sorted(set(b) - set(a))[:4]})
        return {"items": len(self.items), "loaded": len(saved),
                "compared": sum(n in saved for n in self.items), "differing": differing,
                "missing": [n for n in self.items if n not in saved and n not in failed],
                "failed_items": failed}


def _failing(names: list[str], loads: Callable[[list[str]], bool]) -> list[str]:
    """Bisect a group that fails to load down to the items that fail alone."""
    if len(names) <= 1:
        return list(names)
    mid = len(names) // 2
    return [n for half in (names[:mid], names[mid:]) if not loads(half)
            for n in _failing(half, loads)]


def _k9_install(newest: bool = False) -> Any | None:
    """The oldest KiCad 9.x install of kicad_env.discover() (or the newest install)."""
    if (server := str(Path(__file__).resolve().parent.parent)) not in sys.path:
        sys.path.insert(0, server)
    import kicad_env
    found = kicad_env.discover()
    pool = found if newest else sorted((i for i in found if i.version_tuple[0] == 9),
                                       key=lambda i: i.version_tuple)
    return pool[0] if pool else None


def k9_check(path: str | Path, kind: str = "fp", kicad_cli: str | Path | None = None, *,
             timeout_s: float = K9_CHECK_TIMEOUT_S, locate_failures: bool = True
             ) -> dict[str, Any]:
    """KiCad 9 (default: the oldest 9.x) re-saves a .pretty, .kicad_mod or .kicad_sym;
    its pad / pin tables must equal the input's: {ok, items, loaded, compared,
    differing, missing, failed_items (bisected), errors}; {ok: None} without KiCad 9."""
    if kind not in ROOT_HEAD:
        raise ValueError(f"kind must be 'fp' or 'sym', not {kind!r}")
    if kicad_cli is None and (install := _k9_install()) is not None:
        kicad_cli = install.kicad_cli
    base = {"kind": kind, "path": str(path), "kicad_cli": str(kicad_cli) if kicad_cli else None}
    if kicad_cli is None:
        return {**base, "ok": None, "reason": "no KiCad 9.x install found (kicad_env.discover)"}
    lib = _Lib.open(kind, Path(path))
    with tempfile.TemporaryDirectory(prefix="k9check_") as tmp:
        def resave(names: list[str]) -> tuple[list[str], dict[str, str]]:
            return lib.resave(Path(kicad_cli), names, Path(tmp), timeout_s)
        errors, saved = resave(list(lib.items))
        failed: list[str] = []
        if errors and locate_failures and lib.items:
            failed = _failing(list(lib.items), lambda names: not resave(names)[0])
            if failed and (rest := [n for n in lib.items if n not in failed]):
                more, saved = resave(rest)
                errors += more
    found = lib.compare(saved, failed)
    return {**base, "ok": not errors and not found["differing"] and not found["missing"],
            **found, "errors": errors}


# --- Survey of an install's stock libraries -------------------------------------
def _survey_lib(kind: str, src: Path, tmp: Path, cli: Path) -> dict[str, Any]:
    dst = tmp / kind / src.name
    dst.parent.mkdir(parents=True, exist_ok=True)
    down = downgrade_pretty(src, dst) if kind == "fp" else downgrade_symbol_file(src, dst)
    check = k9_check(dst, kind, cli) if down["counts"]["ok"] else dict(
        loaded=0, compared=0, differing=[], failed_items=[], missing=[], errors=[])
    return {"kind": kind, "library": src.stem, "items": down["counts"]["items"],
            "refused": [[n, i["construct"], i["reason"]] for n, i in down["items"].items()
                        if not i["ok"]], "dropped": down["dropped"],
            "rewritten": down["rewritten"], "errors": check["errors"][:5],
            **{k: check[k] for k in ("loaded", "compared", "differing", "failed_items",
                                     "missing")}}


def _summary(libs: list[dict[str, Any]], kind: str) -> dict[str, Any]:
    mine = [lib for lib in libs if lib["kind"] == kind]
    groups: dict[str, dict[str, Any]] = {}
    for lib in mine:
        for name, construct, reason in lib["refused"]:
            g = groups.setdefault(construct, {"count": 0, "reason": reason, "examples": []})
            g["count"] += 1
            g["examples"] = (g["examples"] + [f"{lib['library']}:{name}"])[:5]
    items = sum(lib["items"] for lib in mine)
    identical = sum(lib["compared"] - len(lib["differing"]) for lib in mine)
    return {"libraries": len(mine), "items": items, "loaded": sum(m["loaded"] for m in mine),
            "identical": identical, **{k: sum(len(m[k]) for m in mine) for k in (
                "refused", "differing", "failed_items", "missing")},
            "percent_identical": round(100.0 * identical / items, 3) if items else None,
            "refusals": groups, "dropped": _totals(mine, "dropped"),
            "rewritten": _totals(mine, "rewritten")}


def survey(out: str | Path, root: str | Path | None = None, *, jobs: int = 2,
           kicad_cli: str | Path | None = None) -> dict[str, Any]:
    """Downgrade and k9_check every library of a share/kicad (default: the newest
    install's); JSON report to out (per kind, refusals grouped by construct)."""
    share = Path(root) if root else _k9_install(newest=True).share
    if kicad_cli is None and (install := _k9_install()) is not None:
        kicad_cli = install.kicad_cli
    if kicad_cli is None:
        raise RuntimeError("the survey needs a KiCad 9.x install (kicad_env.discover)")
    tasks = [("fp", p) for p in sorted(share.glob("footprints/*.pretty"))] + \
        [("sym", p) for p in sorted(share.glob("symbols/*.kicad_sym"))]
    with tempfile.TemporaryDirectory(prefix="k9survey_") as tmp, \
            ThreadPoolExecutor(max(1, jobs)) as pool:
        libs = list(pool.map(lambda t: _survey_lib(*t, Path(tmp), Path(kicad_cli)), tasks))
    report = {"root": str(share), "kicad9_cli": str(kicad_cli), "footprints": _summary(
        libs, "fp"), "symbols": _summary(libs, "sym"), "libraries": libs}
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(json.dumps(report, indent=1), encoding="utf-8")
    return report


# --- CLI ------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="KiCad 10/11 libraries in KiCad 9's dialect (D0)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for kind in ("fp", "sym"):
        p = sub.add_parser(kind, help=f"downgrade a {'.pretty' if kind == 'fp' else '.kicad_sym'}")
        p.add_argument("src")
        p.add_argument("dst")
        p.add_argument("--check", action="store_true", help="prove the result with KiCad 9")
    p = sub.add_parser("survey", help="downgrade and k9_check an install's stock libraries")
    p.add_argument("out")
    p.add_argument("--root", help="the share/kicad to survey (default: the newest install's)")
    p.add_argument("--jobs", type=int, default=2)
    args = ap.parse_args(argv)
    if args.cmd == "survey":
        report = survey(args.out, args.root, jobs=args.jobs)
        print(json.dumps({k: v for k, v in report.items() if k != "libraries"}, indent=1))
        return 0
    report = (downgrade_pretty if args.cmd == "fp" else downgrade_symbol_file)(args.src, args.dst)
    if args.check:
        report = {**report, "k9_check": k9_check(args.dst, args.cmd)}
    print(json.dumps(report, indent=1))
    return 0 if report["ok"] and (not args.check or report["k9_check"]["ok"]) else 1


if __name__ == "__main__":
    sys.exit(main())
