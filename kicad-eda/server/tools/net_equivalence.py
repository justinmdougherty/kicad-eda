#!/usr/bin/env python3
"""Connectivity equivalence by pad partition (ROADMAP_DECISIONS D4).

Two designs are equivalent when they connect the same pads together. Each
side is reduced to a partition of ``(ref, pad)`` keys into nets and the
partitions are compared; net NAMES never matter (``/CH1/OUT``, ``CH1_OUT`` and
``Net-(U3-Pad4)`` can all be one net). References match strictly, or through
an explicit ``ref_map`` for a re-annotated rebuild.

Readers, each returning ``{net_name: {(ref, pad), ...}}`` with string pads;
``NO_NET`` ("") collects pads that exist but sit on no net:

1. ``netlist_pad_nets(design)``: netlist.json ``nets`` (+ ``no_connect``).
2. ``xml_pad_nets(text)``: ``kicad-cli sch export netlist --format kicadxml``
   (run by ``export_schematic_xml``). ``sexpr_netlist_pad_nets`` reads the
   default S-expression export as well.
3. ``pcb_pad_nets(text)``: a ``.kicad_pcb`` read as text, no KiCad needed:
   KiCad 4/5 (``module``, bare-word nets), 6/7 (``fp_text reference``), 8/9
   (``property "Reference"``, pad ``(net N "name")``) and 10/11 (pad
   ``(net "name")``, no board net table).

``load_pad_nets(path)`` picks the reader from the file content.

``compare(a, b, ref_map=None, ignore_single_pin=True)`` returns an
``EquivalenceResult``: ``split_nets`` (one net of ``a`` spread over several
nets of ``b``), ``merged_nets`` (the reverse), ``missing_pins`` /
``extra_pins`` (connected pads the other side does not have at all) and
``details`` (counts, the 1:1 ``net_name_map`` that carries net_specs across,
pads found on two nets, ...).

Only the S-expression readers need ``sexpdata`` (imported lazily), so
``compare`` and the netlist.json / XML readers are stdlib-only.
"""
from __future__ import annotations

import argparse
import copy
import json
import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

PadKey = tuple[str, str]
PadNets = Mapping[str, Iterable[Any]]       # {net_name: iterable of (ref, pad)}

NO_NET = ""                     # the "net" of pads present on no net (KiCad's net 0)
DEFAULT_TIMEOUT_S = 300.0       # kicad-cli netlist export of a large hierarchy

_LEADING = "\ufeff \t\r\n"      # BOM + whitespace before the first token
_TOKEN = re.compile(r'"(?:[^"\\]|\\.)*"|[()]')   # a quoted string or a parenthesis
_HEAD = re.compile(r'\(\s*([^\s()"]+)')
_FP_NAME = re.compile(r'\(\s*(?:footprint|module)\s+("(?:[^"\\]|\\.)*"|[^\s()]+)')
_VERSION = re.compile(r"\(\s*version\s+(\d+)\s*\)")
_DIGITS = re.compile(r"([0-9]+)")
_ERROR_LINE = re.compile(r"\bError:")
# KiCad's teardrop parser (parseTEARDROP_PARAMETERS) makes a child's '('
# optional, and some 8.99/9.0 builds wrote `(curved_edges no)filter_ratio 0.9)`
# (349 times in the KiCad 9.0.4 RoyalBlue54L-Feather demo, which KiCad loads).
# Such a bare keyword right after ')' gets its '(' back before scanning.
_TEARDROP_WORDS = ("enabled|allow_two_segments|prefer_zone_connections|best_length_ratio|"
                   "max_length|best_width_ratio|max_width|curve_points|curved_edges|filter_ratio")
_BARE_TEARDROP = re.compile(rf"\)\s*(?:{_TEARDROP_WORDS})\b")
_BARE_TEARDROP_FIX = re.compile(rf'"(?:[^"\\]|\\.)*"|(\)\s*)((?:{_TEARDROP_WORDS})\b)')
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
_EXCLUDED, _DNP = "exclude_from_board", "dnp"


# ----------------------------------------------------------------------------
# Small shared helpers
# ----------------------------------------------------------------------------
def _natural(text: str) -> tuple:
    """'R10' sorts after 'R2'."""
    return tuple(int(p) if i % 2 else p for i, p in enumerate(_DIGITS.split(text)))


def _pad_order(key: PadKey) -> tuple:
    return (_natural(key[0]), _natural(key[1]))


def _sorted_pads(keys: Iterable[PadKey]) -> tuple[PadKey, ...]:
    return tuple(sorted(keys, key=_pad_order))


def _pad_key(raw: Any, where: str, *, allow_empty_ref: bool = False) -> PadKey:
    """[ref, pad] / (ref, pad) -> (ref, str(pad)); anything else fails loudly.
    An empty ref is only legal from a board (a footprint without reference)."""
    if isinstance(raw, (list, tuple)) and len(raw) == 2:
        ref, pad = raw
        if (isinstance(ref, str) and (ref or allow_empty_ref) and isinstance(pad, (str, int))
                and not isinstance(pad, bool) and str(pad)):
            return (ref, str(pad))
    raise ValueError(f"{where}: {raw!r} is not a [ref, pad] pair")


def unescape_net_name(name: str) -> str:
    """KiCad stores a '/' inside a label as ``{slash}`` in board and
    S-expression netlist net names (EscapeString CTX_NETNAME); the XML
    netlist is already unescaped."""
    return name.replace("{slash}", "/")


def _flag_filter(include_excluded: bool, drop_dnp: bool) -> frozenset[str]:
    return frozenset(([] if include_excluded else [_EXCLUDED]) + ([_DNP] if drop_dnp else []))


# ----------------------------------------------------------------------------
# Reader 1: netlist.json
# ----------------------------------------------------------------------------
def netlist_pad_nets(design: Mapping[str, Any], *,
                     exclude_board_only: bool = False) -> dict[str, set[PadKey]]:
    """Pad nets of a netlist.json design. ``no_connect`` pads go under NO_NET
    (they exist, unconnected); ``exclude_board_only`` drops the pads of
    ``"board_only": true`` components (absent from any schematic)."""
    if not isinstance(design, Mapping):
        raise ValueError("a netlist.json design must be an object")
    nets = design.get("nets", {})
    if not isinstance(nets, Mapping):
        raise ValueError("netlist 'nets' must be an object {name: [[ref, pad], ...]}")
    components = design.get("components", [])
    if not isinstance(components, list):
        raise ValueError("netlist 'components' must be a list")
    skip = {str(c.get("ref")) for c in components if exclude_board_only
            and isinstance(c, Mapping) and c.get("board_only") is True}
    out: dict[str, set[PadKey]] = {}
    for name, pads in nets.items():
        where = f"nets[{name!r}]"
        if not isinstance(name, str) or not name:
            raise ValueError(f"{where}: empty net name")
        if not isinstance(pads, list):
            raise ValueError(f"{where} must be a list of [ref, pad] pairs")
        keep = {k for k in (_pad_key(p, where) for p in pads) if k[0] not in skip}
        if keep:
            out[name] = keep
    no_connect = design.get("no_connect", [])
    if not isinstance(no_connect, list):
        raise ValueError("netlist 'no_connect' must be a list of [ref, pad] pairs")
    loose = {k for k in (_pad_key(p, "no_connect") for p in no_connect) if k[0] not in skip}
    if loose:
        out[NO_NET] = loose
    return out


# ----------------------------------------------------------------------------
# Reader 2: kicad-cli netlist exports
# ----------------------------------------------------------------------------
def xml_pad_nets(text: str, *, include_excluded: bool = False,
                 drop_dnp: bool = False) -> dict[str, set[PadKey]]:
    """Pad nets of a ``--format kicadxml`` export. KiCad keeps parts marked
    "exclude from board" in this format (comps AND net nodes), so they are
    dropped unless ``include_excluded``; DNP parts are on the board and stay
    unless ``drop_dnp``. Every unconnected pin is its own single-node net. A
    node with an empty pin number (no pad can carry it) is skipped."""
    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        raise ValueError(f"not well-formed XML: {e}") from e
    if root.tag != "export":
        raise ValueError(f"not a KiCad netlist export (root element <{root.tag}>, "
                         "expected <export>)")
    flags = _flag_filter(include_excluded, drop_dnp)
    skip = {comp.get("ref", "") for comp in root.iterfind("components/comp")
            if {p.get("name", "") for p in comp.findall("property")} & flags}
    nets_el = root.find("nets")
    if nets_el is None:
        raise ValueError("the XML netlist has no <nets> section")
    out: dict[str, set[PadKey]] = {}
    for net in nets_el.findall("net"):
        name = net.get("name") or f"<net code {net.get('code', '?')}>"
        for node in net.findall("node"):
            ref, pin = node.get("ref"), node.get("pin")
            if not ref or pin is None:
                raise ValueError(f"net {name!r} has a node without ref/pin")
            if pin and ref not in skip:
                out.setdefault(name, set()).add((ref, pin))
    return out


def sexpr_netlist_pad_nets(text: str, *, include_excluded: bool = False,
                           drop_dnp: bool = False) -> dict[str, set[PadKey]]:
    """Pad nets of the default (S-expression) ``kicad-cli sch export netlist``
    output. KiCad already leaves board-excluded parts out of this format; the
    flags are honoured anyway when present. Net names are unescaped."""
    if _head_of(text) != "export":
        raise ValueError("not a KiCad S-expression netlist (expected '(export ...')")
    tree = _sexp(text)
    flags = _flag_filter(include_excluded, drop_dnp)
    skip = {_value(comp, "ref") for comp in _children(_child(tree, "components"), "comp")
            if {_value(p, "name") for p in _children(comp, "property")} & flags}
    nets = _child(tree, "nets")
    if nets is None:
        raise ValueError("the S-expression netlist has no (nets ...) section")
    out: dict[str, set[PadKey]] = {}
    for net in _children(nets, "net"):
        name = unescape_net_name(_value(net, "name")) or f"<net code {_value(net, 'code')}>"
        for node in _children(net, "node"):
            ref, pin = _value(node, "ref"), _value(node, "pin")
            if not ref:
                raise ValueError(f"net {name!r} has a node without ref")
            if pin and ref not in skip:
                out.setdefault(name, set()).add((ref, pin))
    return out


def export_schematic_xml(sch: str | Path, out_xml: str | Path, kicad_cli: str | Path, *,
                         timeout_s: float = DEFAULT_TIMEOUT_S) -> Path:
    """Run ``kicad-cli sch export netlist --format kicadxml`` (stdin detached,
    D0) and return the XML path. Fails loudly on a non-zero exit, on any
    ``Error:`` line (kicad-cli can exit 0 after one) and on missing output.
    Use the newest kicad-cli the schematic needs (toolchain.kicad_cli()),
    with the project's footprint libraries in place: when a footprint
    resolves, the 10.99 exporter leaves out symbol pins that have no pad in
    it (verified on the cm5_minima demo, J701 M1..M4). It may write a
    ``.kicad_prl`` beside the schematic."""
    sch, out = Path(sch), Path(out_xml)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.unlink(missing_ok=True)                 # a stale file must not pass for fresh output
    argv = [str(kicad_cli), "sch", "export", "netlist", "--format", "kicadxml", "-o", str(out),
            str(sch)]
    try:
        r = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=timeout_s, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"kicad-cli sch export netlist timed out after {timeout_s:g} s "
                           f"on {sch}") from e
    except OSError as e:
        raise RuntimeError(f"cannot start {kicad_cli}: {e}") from e
    output = "\n".join(s for s in (r.stdout, r.stderr) if s).strip()
    if r.returncode != 0:
        raise RuntimeError(f"kicad-cli sch export netlist failed (exit {r.returncode}) on {sch}: "
                           f"{output[-800:] or 'no output'}")
    errors = [ln.strip() for ln in output.splitlines() if _ERROR_LINE.search(ln)]
    if errors:
        raise RuntimeError(f"kicad-cli sch export netlist reported errors on {sch}: "
                           + "; ".join(errors[:10]))
    if not out.is_file():
        raise RuntimeError(f"kicad-cli sch export netlist exited 0 but wrote no {out}")
    return out


# ----------------------------------------------------------------------------
# S-expression helpers (sexpdata, lazily imported)
# ----------------------------------------------------------------------------
def _sexp(src: str) -> list:
    try:
        import sexpdata
    except ImportError as e:
        raise RuntimeError("reading KiCad S-expression files needs sexpdata "
                           "(server/requirements.txt)") from e
    try:
        tree = sexpdata.loads(src.lstrip(_LEADING), nil=None, true=None, false=None)
    except Exception as e:      # sexpdata raises its own exception classes
        raise ValueError(f"cannot parse S-expression {src[:60]!r}: {e}") from e
    if not isinstance(tree, list):
        raise ValueError(f"not an S-expression list: {src[:60]!r}")
    return tree


def _atom(x: Any) -> str:
    """A token as text: quoted string, bare word (sexpdata.Symbol is a str)
    or a number (KiCad 4/5 write pad numbers and refs unquoted)."""
    if isinstance(x, list):
        raise ValueError(f"expected a value, found a list {x!r}")
    return str(x)


def _tag(node: Any) -> str:
    return str(node[0]) if isinstance(node, list) and node and not isinstance(node[0], list) else ""


def _children(node: list | None, name: str) -> list[list]:
    return [c for c in (node or [])[1:] if _tag(c) == name]


def _child(node: list | None, name: str) -> list | None:
    found = _children(node, name)
    return found[0] if found else None


def _value(node: list, name: str) -> str:
    """``(name "value")`` child of node -> "value" ("" when absent/empty)."""
    child = _child(node, name)
    return _atom(child[1]) if child is not None and len(child) > 1 else ""


def _head_of(text: str) -> str:
    m = _HEAD.match(text, len(text) - len(text.lstrip(_LEADING)))
    return m.group(1) if m else ""


# ----------------------------------------------------------------------------
# Reader 3: .kicad_pcb text
# ----------------------------------------------------------------------------
def _restore_teardrop_parens(text: str) -> str:
    """``)filter_ratio 0.9)`` -> ``)(filter_ratio 0.9)`` outside quoted
    strings, read the way KiCad reads it (see _TEARDROP_WORDS). Boards
    without the defect are returned untouched after one cheap search."""
    if not _BARE_TEARDROP.search(text):
        return text
    return _BARE_TEARDROP_FIX.sub(
        lambda m: m.group(0) if m.group(1) is None else f"{m.group(1)}({m.group(2)}", text)


@dataclass(frozen=True)
class _BoardScan:
    net_table: tuple[str, ...]                           # top-level (net N "name") sources
    footprints: tuple[tuple[str, tuple[str, ...]], ...]  # (lib id, pad/reference/attr sources)


def _scan_board(text: str) -> _BoardScan:
    """One pass over the board text, tracking depth outside quoted strings:
    collect the top-level net table and, per footprint, only the children the
    pad nets need. sexpdata then parses those small pieces instead of the
    whole file (zone fills dominate large boards: a 89 MB board scans in
    seconds and never becomes one huge Python tree)."""
    if _head_of(text) != "kicad_pcb":
        raise ValueError("not a KiCad board (expected '(kicad_pcb ...')")
    text = _restore_teardrop_parens(text)
    start = len(text) - len(text.lstrip(_LEADING))
    stack: list[int] = []
    nets: list[str] = []
    footprints: list[tuple[str, tuple[str, ...]]] = []
    parts: list[str] | None = None
    top, lib = "", ""
    for m in _TOKEN.finditer(text, start):
        tok = m.group()
        if tok == "(":
            stack.append(m.start())
            if len(stack) == 2:
                head = _HEAD.match(text, m.start())
                top = head.group(1) if head else ""
                parts = [] if top in _FOOTPRINT_HEADS else None
                if parts is not None:
                    name = _FP_NAME.match(text, m.start())
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
    return _BoardScan(tuple(nets), tuple(footprints))


def _net_table(sources: Iterable[str]) -> dict[int, str]:
    """KiCad <= 9 board net table ``(net N "name")``; KiCad 10+ has none."""
    table: dict[int, str] = {}
    for src in sources:
        node = _sexp(src)
        if len(node) >= 3 and isinstance(node[1], int):
            table[node[1]] = unescape_net_name(_atom(node[2]))
    return table


def _pad_net(pad: list, table: Mapping[int, str], where: str) -> str:
    """The pad's net: ``(net N "name")`` (KiCad <= 9), ``(net "name")``
    (KiCad 10+), ``(net N)`` resolved through the table, or NO_NET."""
    for item in pad[2:]:
        if _tag(item) != "net":
            continue
        args = item[1:]
        if len(args) >= 2:
            return unescape_net_name(_atom(args[1]))
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
        return unescape_net_name(_atom(only))
    return NO_NET


def _pad_number(pad: list) -> str:
    """The pad's number as KiCad sees it after loading: PAD::CanHaveNumber
    clears it for NPTH pads and for aperture pads (no copper layer), so
    those carry no pad key; neither does an empty number."""
    if len(pad) < 2 or isinstance(pad[1], list):
        return ""
    if len(pad) > 2 and not isinstance(pad[2], list) and str(pad[2]) == "np_thru_hole":
        return ""
    layers = _child(pad, "layers")
    if layers is not None and len(layers) > 1 and all(
            _TECH_LAYER.search(_atom(layer)) for layer in layers[1:]):
        return ""
    return _atom(pad[1])


def _footprint_pads(lib: str, parts: Iterable[str], table: Mapping[int, str]
                    ) -> tuple[str, bool, list[tuple[str, str]]]:
    """(reference, board_only, [(pad number, net)]) of one footprint. A
    footprint without a reference keeps ref "" (pcbnew reads it that way)."""
    ref: str | None = None
    board_only = False
    numbered: list[tuple[str, list]] = []
    for src in parts:
        node = _sexp(src)
        head = _tag(node)
        if head in ("property", "fp_text") and len(node) >= 3:
            # KiCad 8+ (property "Reference" ...) wins over KiCad <= 7 (fp_text reference ...)
            if _atom(node[1]) in ("Reference", "reference") and (head == "property" or ref is None):
                ref = _atom(node[2])
        elif head == "attr":
            board_only = "board_only" in {str(t) for t in node[1:] if not isinstance(t, list)}
        elif head == "pad" and (number := _pad_number(node)):
            numbered.append((number, node))
    ref = ref or ""
    return ref, board_only, [
        (number, _pad_net(pad, table, f"{ref or '(no reference)'} pad {number} ({lib})"))
        for number, pad in numbered]


def pcb_pad_nets(text: str, *, exclude_board_only: bool = False) -> dict[str, set[PadKey]]:
    """Pad nets of a ``.kicad_pcb`` (any format from KiCad 4 to 11), read as
    text. Pads on no net go under NO_NET; ``exclude_board_only`` drops
    footprints with the ``board_only`` attribute (KiCad 6+ "not in
    schematic": holes, logos, fiducials). Copper elements sharing a number
    are one pad key; on different nets the key lands in both (compare joins
    them and reports a pad conflict)."""
    scan = _scan_board(text)
    table = _net_table(scan.net_table)
    out: dict[str, set[PadKey]] = {}
    for lib, parts in scan.footprints:
        ref, board_only, pads = _footprint_pads(lib, parts, table)
        if exclude_board_only and board_only:
            continue
        for number, net in pads:
            out.setdefault(net, set()).add((ref, number))
    return out


def pcb_format_version(text: str) -> int | None:
    """The ``(version N)`` of a board header (20241229 = KiCad 9, 20260206 =
    KiCad 10, 20260901 = KiCad 11 nightly; 4 = KiCad 4), None when absent."""
    m = _VERSION.search(text, 0, 4096)
    return int(m.group(1)) if m else None


def load_pad_nets(path: str | Path, *, exclude_board_only: bool = False) -> dict[str, set[PadKey]]:
    """Read pad nets from a file, choosing the reader from its content:
    netlist.json, a KiCad XML or S-expression netlist, or a .kicad_pcb.
    ``exclude_board_only`` applies to netlist.json and boards (a schematic
    export has no board-only parts). A .kicad_sch must be exported first."""
    p = Path(path)
    if p.suffix.lower() == ".kicad_sch":
        raise ValueError(f"{p.name} is a schematic: export its netlist first with "
                         "export_schematic_xml(sch, out_xml, kicad_cli) and load the XML")
    text = p.read_text(encoding="utf-8").lstrip(_LEADING)
    try:
        if text.startswith("{"):
            return netlist_pad_nets(json.loads(text), exclude_board_only=exclude_board_only)
        if text.startswith("<"):
            return xml_pad_nets(text)
        head = _head_of(text)
        if head == "kicad_pcb":
            return pcb_pad_nets(text, exclude_board_only=exclude_board_only)
        if head == "export":
            return sexpr_netlist_pad_nets(text)
        if head == "kicad_sch":
            raise ValueError("this is a schematic: export its netlist first with "
                             "export_schematic_xml(sch, out_xml, kicad_cli)")
    except ValueError as e:     # json.JSONDecodeError is a ValueError too
        raise ValueError(f"{p}: {e}") from e
    raise ValueError(f"cannot tell what {p} is: expected netlist.json, a KiCad XML or "
                     "S-expression netlist, or a .kicad_pcb")


# ----------------------------------------------------------------------------
# compare()
# ----------------------------------------------------------------------------
@dataclass(frozen=True)
class NetPart:
    """One piece of a split/merged net: the other side's net name (None for a
    pad on no net there) and the shared pads it holds."""
    net: str | None
    pads: tuple[PadKey, ...]

    def as_dict(self) -> dict[str, Any]:
        return {"net": self.net, "pads": [list(k) for k in self.pads]}


@dataclass(frozen=True)
class NetDiff:
    """A net of one side (``a`` for a split, ``b`` for a merge), its pads
    shared by both sides, and the other side's nets those pads fall in."""
    net: str
    pads: tuple[PadKey, ...]
    parts: tuple[NetPart, ...]

    def as_dict(self) -> dict[str, Any]:
        return {"net": self.net, "pads": [list(k) for k in self.pads],
                "parts": [p.as_dict() for p in self.parts]}


@dataclass(frozen=True)
class EquivalenceResult:
    equal: bool
    split_nets: tuple[NetDiff, ...]
    merged_nets: tuple[NetDiff, ...]
    missing_pins: tuple[PadKey, ...]     # connected in a, absent from b
    extra_pins: tuple[PadKey, ...]       # connected in b, absent from a
    details: Mapping[str, Any]

    def as_dict(self) -> dict[str, Any]:
        """JSON-ready (pads as [ref, pad] lists)."""
        return {"equal": self.equal,
                "split_nets": [d.as_dict() for d in self.split_nets],
                "merged_nets": [d.as_dict() for d in self.merged_nets],
                "missing_pins": [list(k) for k in self.missing_pins],
                "extra_pins": [list(k) for k in self.extra_pins],
                "details": copy.deepcopy(dict(self.details))}

    def summary(self) -> str:
        d = self.details
        if self.equal:
            return (f"equivalent: {d['nets_a']} nets over {d['common_pads']} connected pads "
                    f"({len(d['net_name_map'])} matched 1:1)")
        return (f"NOT equivalent: {len(self.split_nets)} split, {len(self.merged_nets)} merged, "
                f"{len(self.missing_pins)} missing, {len(self.extra_pins)} extra pin(s)")


@dataclass(frozen=True)
class _Side:
    present: frozenset[PadKey]                         # every pad, connected or not
    owner: Mapping[PadKey, int]                        # pad -> classes index (2+ pad nets)
    classes: tuple[tuple[str, frozenset[PadKey]], ...]
    lone: Mapping[PadKey, str]                         # pad alone on a named net -> name
    conflicts: tuple[dict[str, Any], ...]              # pads listed on 2+ nets
    dropped: frozenset[str]                            # ignore_refs actually present

    def name_of(self, key: PadKey) -> str:
        return self.classes[self.owner[key]][0] if key in self.owner else self.lone.get(key, NO_NET)


def _entries(nets: PadNets, label: str) -> list[tuple[str, PadKey]]:
    if not isinstance(nets, Mapping):
        raise ValueError(f"{label} must be a mapping {{net_name: [(ref, pad), ...]}}")
    out: list[tuple[str, PadKey]] = []
    for name, pads in nets.items():
        where = f"{label} net {str(name)!r}"
        if isinstance(pads, (str, bytes)) or not isinstance(pads, Iterable):
            raise ValueError(f"{where} must be a collection of (ref, pad) pairs")
        out += [(str(name), _pad_key(raw, where, allow_empty_ref=True)) for raw in pads]
    return out


def _renamer(ref_map: Mapping[str, str] | None,
             refs_a: set[str]) -> tuple[dict[str, str], list[str]]:
    """Validate ref_map ({ref in a: ref in b}) as one-to-one over a's refs;
    returns (map, unused keys)."""
    if not ref_map:
        return {}, []
    if not isinstance(ref_map, Mapping):
        raise TypeError("ref_map must be a mapping {ref_in_a: ref_in_b}")
    mapping = {str(k): str(v) for k, v in ref_map.items()}
    if any(not k or not v for k, v in mapping.items()):
        raise ValueError(f"ref_map: empty reference in {ref_map!r}")
    seen: dict[str, str] = {}
    for src, dst in sorted(mapping.items(), key=lambda kv: _natural(kv[0])):
        if dst in seen:
            raise ValueError(f"ref_map sends {seen[dst]} and {src} both to {dst}")
        seen[dst] = src
    for src, dst in mapping.items():
        if src in refs_a and dst in refs_a and dst not in mapping:
            raise ValueError(f"ref_map renames {src} onto {dst}, which a still uses "
                             f"(map {dst} too)")
    unused = sorted((k for k in mapping if k not in refs_a), key=_natural)
    return mapping, unused


def _build_side(entries: list[tuple[str, PadKey]], rename: Mapping[str, str],
                ignore: frozenset[str]) -> _Side:
    present: set[PadKey] = set()
    member: dict[PadKey, set[str]] = {}
    dropped: set[str] = set()
    for name, (ref, pad) in entries:
        ref = rename.get(ref, ref)
        if ref in ignore:
            dropped.add(ref)
            continue
        present.add((ref, pad))
        if name != NO_NET:
            member.setdefault((ref, pad), set()).add(name)
    parent: dict[str, str] = {}

    def find(n: str) -> str:
        while parent.setdefault(n, n) != n:
            parent[n] = parent[parent[n]]
            n = parent[n]
        return n

    for names in member.values():                 # a pad on two nets joins them
        first, *rest = sorted(names)
        for other in rest:
            parent[find(other)] = find(first)
    groups: dict[str, set[PadKey]] = {}
    names_of: dict[str, set[str]] = {}
    for key, names in member.items():
        root = find(next(iter(names)))
        groups.setdefault(root, set()).add(key)
        names_of.setdefault(root, set()).update(names)
    classes: list[tuple[str, frozenset[PadKey]]] = []
    owner: dict[PadKey, int] = {}
    lone: dict[PadKey, str] = {}
    for root in sorted(groups, key=_natural):
        name = " + ".join(sorted(names_of[root], key=_natural))
        if len(groups[root]) == 1:
            lone[next(iter(groups[root]))] = name
            continue
        owner.update(dict.fromkeys(groups[root], len(classes)))
        classes.append((name, frozenset(groups[root])))
    conflicts = tuple({"pad": list(k), "nets": sorted(n, key=_natural)}
                      for k, n in sorted(member.items(), key=lambda kv: _pad_order(kv[0]))
                      if len(n) > 1)
    return _Side(frozenset(present), MappingProxyType(owner), tuple(classes),
                 MappingProxyType(lone), conflicts, frozenset(dropped))


def _diffs(src: _Side, dst: _Side, universe: frozenset[PadKey]) -> tuple[NetDiff, ...]:
    """src nets whose shared pads fall into more than one dst net (an
    isolated dst pad is a net of its own)."""
    out: list[NetDiff] = []
    for name, pads in src.classes:
        common = pads & universe
        groups: dict[Any, list[PadKey]] = {}
        for key in common:
            group = ("net", dst.owner[key]) if key in dst.owner else ("pad", key)
            groups.setdefault(group, []).append(key)
        if len(groups) < 2:
            continue
        parts = sorted((NetPart(dst.name_of(keys[0]) or None, _sorted_pads(keys))
                        for keys in groups.values()), key=lambda p: _pad_order(p.pads[0]))
        out.append(NetDiff(name, _sorted_pads(common), tuple(parts)))
    return tuple(sorted(out, key=lambda d: _natural(d.net)))


def _name_map(sa: _Side, sb: _Side, universe: frozenset[PadKey]) -> dict[str, str]:
    """a net -> b net for nets that correspond one-to-one on the shared pads."""
    out: dict[str, str] = {}
    for i, (name, pads) in enumerate(sa.classes):
        targets = {sb.owner.get(k) for k in pads & universe}
        if len(targets) != 1 or None in targets:
            continue
        j = targets.pop()
        if {sa.owner.get(k) for k in sb.classes[j][1] & universe} == {i}:
            out[name] = sb.classes[j][0]
    return dict(sorted(out.items(), key=lambda kv: _natural(kv[0])))


def _by_net(side: _Side, keys: Iterable[PadKey]) -> dict[str, list[list[str]]]:
    out: dict[str, list[list[str]]] = {}
    for key in _sorted_pads(keys):
        out.setdefault(side.name_of(key), []).append(list(key))
    return dict(sorted(out.items(), key=lambda kv: _natural(kv[0])))


def _gone_refs(pins: Iterable[PadKey], other: _Side) -> list[str]:
    """Refs of these pins that the other side does not have at all."""
    there = {ref for ref, _ in other.present}
    return sorted({ref for ref, _ in pins if ref not in there}, key=_natural)


def compare(a: PadNets, b: PadNets, ref_map: Mapping[str, str] | None = None,
            ignore_single_pin: bool = True, *,
            ignore_refs: Iterable[str] = ()) -> EquivalenceResult:
    """Compare the pad partitions of ``a`` (the reference) and ``b``.

    - ``ref_map`` {ref in a: ref in b} renames a's references first (one to
      one; a ref left out keeps its name).
    - ``ignore_refs`` (names as in b) drops those parts from both sides, e.g.
      board-only holes when comparing a schematic with a board.
    - ``ignore_single_pin`` (default): a pad alone on its net (single-pin net
      or no net) need not exist on the other side; such pads only matter where
      the other side connects them to something (then it is a split/merge).
      With False every pad of each side must exist on the other.

    Pads present on both sides are compared as partitions: a net of ``a``
    whose pads land in several nets of ``b`` (an isolated pad counts as its
    own net) is a split; the reverse is a merge."""
    entries_a, entries_b = _entries(a, "a"), _entries(b, "b")
    rename, unused = _renamer(ref_map, {ref for _, (ref, _p) in entries_a})
    ignore = frozenset(str(r) for r in ignore_refs)
    sa, sb = _build_side(entries_a, rename, ignore), _build_side(entries_b, {}, ignore)
    connected_a, connected_b = frozenset(sa.owner), frozenset(sb.owner)
    need_a = connected_a if ignore_single_pin else sa.present
    need_b = connected_b if ignore_single_pin else sb.present
    missing, extra = need_a - sb.present, need_b - sa.present
    universe = (connected_a | connected_b) & sa.present & sb.present
    split, merged = _diffs(sa, sb, universe), _diffs(sb, sa, universe)
    details = {
        "ignore_single_pin": ignore_single_pin,
        "nets_a": len(sa.classes), "nets_b": len(sb.classes),
        "pads_a": len(sa.present), "pads_b": len(sb.present),
        "connected_pads_a": len(connected_a), "connected_pads_b": len(connected_b),
        "common_pads": len(universe),
        "missing_refs": _gone_refs(missing, sb), "extra_refs": _gone_refs(extra, sa),
        "missing_by_net": _by_net(sa, missing), "extra_by_net": _by_net(sb, extra),
        "net_name_map": _name_map(sa, sb, universe),
        "pad_conflicts_a": list(sa.conflicts), "pad_conflicts_b": list(sb.conflicts),
        "ref_map_unused": unused,
        "ignored_refs": sorted(sa.dropped | sb.dropped, key=_natural),
    }
    equal = not (split or merged or missing or extra)
    return EquivalenceResult(equal, split, merged, _sorted_pads(missing), _sorted_pads(extra),
                             MappingProxyType(details))


# ----------------------------------------------------------------------------
# Command line
# ----------------------------------------------------------------------------
def _describe(res: EquivalenceResult, limit: int = 20) -> list[str]:
    def pads(keys: Iterable[PadKey]) -> str:
        return " ".join(f"{r}.{p}" for r, p in list(keys)[:limit * 5])

    lines = [res.summary()]
    for verb, diffs in (("split", res.split_nets), ("merged", res.merged_nets)):
        lines += [f"  {verb} {d.net}: " + " | ".join(f"{p.net or '(no net)'}: {pads(p.pads)}"
                                                     for p in d.parts) for d in diffs[:limit]]
    lines += [f"  {label} ({len(keys)}): {pads(keys)}" for label, keys in
              (("missing", res.missing_pins), ("extra", res.extra_pins)) if keys]
    return lines


def _load_arg(path: str, args: argparse.Namespace, work: Path) -> dict[str, set[PadKey]]:
    p = Path(path)
    if p.suffix.lower() == ".kicad_sch" and args.kicad_cli:
        p = export_schematic_xml(p, work / f"{p.stem}.xml", args.kicad_cli)
    return load_pad_nets(p, exclude_board_only=args.exclude_board_only)


def main(argv: list[str] | None = None) -> int:
    """``net_equivalence.py A B``: exit 0 equivalent, 1 different, 2 error."""
    ap = argparse.ArgumentParser(description="Pad-partition equivalence of two designs: "
                                 "netlist.json, KiCad XML/S-expression netlist, .kicad_pcb, "
                                 "or .kicad_sch (with --kicad-cli).")
    ap.add_argument("a", help="reference design")
    ap.add_argument("b", help="candidate design")
    ap.add_argument("--ref-map", help="JSON {ref_in_a: ref_in_b}, inline or a file path")
    ap.add_argument("--ignore-ref", action="append", default=[], help="drop a ref (repeatable)")
    ap.add_argument("--keep-single-pin", action="store_true",
                    help="isolated pads must exist on both sides too")
    ap.add_argument("--exclude-board-only", action="store_true",
                    help="drop board_only parts (netlist.json / .kicad_pcb)")
    ap.add_argument("--kicad-cli", help="kicad-cli that exports a .kicad_sch input")
    ap.add_argument("--json", action="store_true", help="print the full result as JSON")
    args = ap.parse_args(argv)
    try:
        ref_map = None
        if args.ref_map:
            raw = args.ref_map.strip()
            ref_map = json.loads(raw if raw.startswith("{") else Path(raw).read_text("utf-8"))
        with tempfile.TemporaryDirectory() as tmp:
            a, b = (_load_arg(p, args, Path(tmp)) for p in (args.a, args.b))
        res = compare(a, b, ref_map, not args.keep_single_pin, ignore_refs=args.ignore_ref)
    except (OSError, ValueError, TypeError, RuntimeError) as e:
        print(f"net_equivalence: {e}", file=sys.stderr)
        return 2
    print(json.dumps(res.as_dict(), indent=1) if args.json else "\n".join(_describe(res)))
    return 0 if res.equal else 1


if __name__ == "__main__":
    sys.exit(main())
