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
   KiCad 4/5 (``module``, bare-word nets and pad numbers), 6/7 (``fp_text
   reference``), 8/9 (``property "Reference"``, pad ``(net N "name")``) and
   10/11 (pad ``(net "name")``, no board net table). The text readers live in
   sexpr_read.py. ``board_duplicate_refs`` lists references several
   footprints share (their pads would collapse into one part).
4. ``read_reference(path)``: a frozen ``kicad-eda.padnets/1`` reference
   written by ``write_reference`` (faithful rebuilds compare against it
   without the source tool).

``load_pad_nets(path)`` picks the reader from the file content;
``load_pad_nets_meta`` also says what it read (kind, format version,
duplicate and board-only references).

``compare(a, b, ref_map=None, ignore_single_pin=True)`` returns an
``EquivalenceResult``: ``split_nets`` (one net of ``a`` spread over several
nets of ``b``), ``merged_nets`` (the reverse), ``missing_pins`` /
``extra_pins`` (connected pads the other side does not have at all) and
``details`` (counts, the 1:1 ``net_name_map`` that carries net_specs across,
pads found on two nets, ...). ``real_conflicts(details)`` drops the harmless
pad conflicts.

Only the S-expression readers need ``sexpdata`` (imported lazily), so
``compare`` and the netlist.json / XML / reference readers are stdlib-only.
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
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import sexpr_read as sr  # noqa: E402
from sexpr_read import NO_NET, PadKey, unescape_net_name  # noqa: E402

PadNets = Mapping[str, Iterable[Any]]       # {net_name: iterable of (ref, pad)}

DEFAULT_TIMEOUT_S = 300.0       # kicad-cli netlist export of a large hierarchy
REFERENCE_FORMAT = "kicad-eda.padnets/1"
UNCONNECTED_PREFIX = "unconnected-("        # KiCad's name for a pin left on its own
_ERROR_LINE = re.compile(r"\bError:")
_EXCLUDED, _DNP = "exclude_from_board", "dnp"
_NETLIST_VERSION = re.compile(r'<export\b[^>]*\bversion\s*=\s*["\']([^"\']*)'
                              r'|\(\s*version\s+"?([^\s")]*)')

# The old private names, kept for wave 2 only: server/tools/kicad_import.py
# still uses _natural / _pad_order until F46-11 switches it to sexpr_read.
_LEADING, _TOKEN, _HEAD = sr.LEADING, sr.TOKEN_RE, sr.HEAD_RE
_FP_NAME, _VERSION = sr.FP_NAME_RE, sr.VERSION_RE
_head_of, _sexp, _atom, _tag = sr.head_of, sr.parse, sr.atom, sr.tag
_children, _child, _value = sr.children, sr.child, sr.value
_restore_teardrop_parens, _natural, _pad_order = (sr.restore_teardrop_parens, sr.natural_key,
                                                  sr.pad_order)
_pad_number, _pad_net, _net_table = sr.pad_number, sr.pad_net, sr.net_table


# ----------------------------------------------------------------------------
# Small shared helpers
# ----------------------------------------------------------------------------
def _sorted_pads(keys: Iterable[PadKey]) -> tuple[PadKey, ...]:
    return tuple(sorted(keys, key=sr.pad_order))


def _pad_key(raw: Any, where: str, *, allow_empty_ref: bool = False) -> PadKey:
    """[ref, pad] / (ref, pad) -> (ref, str(pad)); anything else fails loudly.
    An empty ref is only legal from a board (a footprint without reference)."""
    if isinstance(raw, (list, tuple)) and len(raw) == 2:
        ref, pad = raw
        if (isinstance(ref, str) and (ref or allow_empty_ref) and isinstance(pad, (str, int))
                and not isinstance(pad, bool) and str(pad)):
            return (ref, str(pad))
    raise ValueError(f"{where}: {raw!r} is not a [ref, pad] pair")


def _flag_filter(include_excluded: bool, drop_dnp: bool) -> frozenset[str]:
    return frozenset(([] if include_excluded else [_EXCLUDED]) + ([_DNP] if drop_dnp else []))


def _repeated(refs: Iterable[str]) -> list[str]:
    return sorted((r for r, n in Counter(refs).items() if n > 1), key=sr.natural_key)


def _refs(raw: Any, label: str) -> list[str]:
    """A list of references, de-duplicated and natural-sorted."""
    if isinstance(raw, (str, bytes)) or not isinstance(raw, Iterable):
        raise ValueError(f"{label} must be a list of references, not {raw!r}")
    refs = list(raw)
    if not all(isinstance(r, str) for r in refs):
        raise ValueError(f"{label} must be a list of references (strings): {refs!r}")
    return sorted(set(refs), key=sr.natural_key)


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
    if sr.head_of(text) != "export":
        raise ValueError("not a KiCad S-expression netlist (expected '(export ...')")
    tree = sr.parse(text)
    flags = _flag_filter(include_excluded, drop_dnp)
    skip = {sr.value(comp, "ref") for comp in sr.children(sr.child(tree, "components"), "comp")
            if {sr.value(p, "name") for p in sr.children(comp, "property")} & flags}
    nets = sr.child(tree, "nets")
    if nets is None:
        raise ValueError("the S-expression netlist has no (nets ...) section")
    out: dict[str, set[PadKey]] = {}
    for net in sr.children(nets, "net"):
        name = unescape_net_name(sr.value(net, "name")) or f"<net code {sr.value(net, 'code')}>"
        for node in sr.children(net, "node"):
            ref, pin = sr.value(node, "ref"), sr.value(node, "pin")
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
# Reader 3: .kicad_pcb text (sexpr_read does the reading)
# ----------------------------------------------------------------------------
_Footprint = tuple[str, bool, list[tuple[str, str]]]    # ref, board_only, [(pad, net)]


def _board_footprints(text: str) -> list[_Footprint]:
    scan = sr.scan_board(text)
    table = sr.net_table(scan.net_table)
    return [sr.footprint_pads(lib, parts, table) for lib, parts in scan.footprints]


def _footprint_nets(footprints: Iterable[_Footprint],
                    exclude_board_only: bool) -> dict[str, set[PadKey]]:
    out: dict[str, set[PadKey]] = {}
    for ref, board_only, pads in footprints:
        if exclude_board_only and board_only:
            continue
        for number, net in pads:
            out.setdefault(net, set()).add((ref, number))
    return out


def pcb_pad_nets(text: str, *, exclude_board_only: bool = False) -> dict[str, set[PadKey]]:
    """Pad nets of a ``.kicad_pcb`` (any format from KiCad 4 to 11), read as
    text. Pads on no net go under NO_NET; ``exclude_board_only`` drops
    footprints with the ``board_only`` attribute (KiCad 6+ "not in
    schematic": holes, logos, fiducials). Copper elements sharing a number
    are one pad key: on different nets the key lands in both (compare joins
    them and reports a pad conflict); one netted and one on no net (U1 'EP'
    on GND plus a no-net 'EP') puts the key under that net AND NO_NET, which
    compare treats as connected and lists in ``partly_unconnected_pads``.
    Footprints sharing a reference collapse into one part
    (board_duplicate_refs)."""
    return _footprint_nets(_board_footprints(text), exclude_board_only)


def board_duplicate_refs(text: str) -> list[str]:
    """References that two or more footprints of the board share (the
    microwave demo's three POLY, tiny_tapeout's STITCH1 vias), natural-sorted;
    "" when several footprints have none. Their pads collapse into one part in
    pcb_pad_nets: rename them first (kicad_import_checks.board_as_imported)."""
    return _repeated(ref for ref, _board_only, _pads in _board_footprints(text))


def pcb_format_version(text: str) -> int | None:
    """The ``(version N)`` of a board header (20241229 = KiCad 9, 20260206 =
    KiCad 10, 20260901 = KiCad 11 nightly; 4 = KiCad 4), None when absent."""
    m = sr.VERSION_RE.search(text, 0, 4096)
    return int(m.group(1)) if m else None


# ----------------------------------------------------------------------------
# Reader 4: the frozen reference (kicad-eda.padnets/1)
# ----------------------------------------------------------------------------
def write_reference(path: str | Path, pad_nets: PadNets, *, board_only: Iterable[str] = (),
                    duplicate_refs: Iterable[str] = (),
                    source: Mapping[str, Any] | None = None) -> Path:
    """Freeze pad nets for a faithful rebuild (D4): UTF-8 JSON, '\\n' line
    ends, ``{"format": "kicad-eda.padnets/1", "nets": {net: [[ref, pad],
    ...]}, "board_only": [...], "duplicate_refs": [...], "source": {...}}``
    with nets and pads natural-sorted, NO_NET pads under "" and empty nets
    left out. Validates everything before writing; returns the path."""
    nets: dict[str, set[PadKey]] = {}
    for name, key in _entries(pad_nets, "pad_nets"):
        nets.setdefault(name, set()).add(key)
    if source is not None and not isinstance(source, Mapping):
        raise ValueError(f"source must be an object (a netlist 'source' block), not {source!r}")
    doc = {"format": REFERENCE_FORMAT,
           "nets": {name: [list(k) for k in _sorted_pads(nets[name])]
                    for name in sorted(nets, key=sr.natural_key)},
           "board_only": _refs(board_only, "board_only"),
           "duplicate_refs": _refs(duplicate_refs, "duplicate_refs"),
           "source": dict(source or {})}
    try:
        text = json.dumps(doc, indent=1, ensure_ascii=False) + "\n"
    except (TypeError, ValueError) as e:
        raise ValueError(f"source is not JSON-serialisable: {e}") from e
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8", newline="\n")
    return out


def _reference(data: Any) -> tuple[dict[str, set[PadKey]], dict[str, Any]]:
    fmt = data.get("format") if isinstance(data, Mapping) else None
    if fmt != REFERENCE_FORMAT:
        raise ValueError(f"not a pad-net reference: format {fmt!r}, expected "
                         f"{REFERENCE_FORMAT!r}")
    nets = data.get("nets")
    if not isinstance(nets, Mapping):
        raise ValueError("reference 'nets' must be an object {net: [[ref, pad], ...]}")
    out: dict[str, set[PadKey]] = {}
    for name, pads in nets.items():
        where = f"reference net {name!r}"
        if not isinstance(pads, list):
            raise ValueError(f"{where} must be a list of [ref, pad] pairs")
        keys = {_pad_key(raw, where, allow_empty_ref=True) for raw in pads}
        if keys:
            out[name] = keys
    source = data.get("source")
    if source is not None and not isinstance(source, Mapping):
        raise ValueError(f"reference 'source' must be an object, not {source!r}")
    meta = _meta("reference", REFERENCE_FORMAT, _refs(data.get("duplicate_refs", []),
                                                      "duplicate_refs"),
                 _refs(data.get("board_only", []), "board_only"))
    return out, {**meta, "source": copy.deepcopy(dict(source or {}))}


def read_reference(path: str | Path) -> tuple[dict[str, set[PadKey]], dict[str, Any]]:
    """(pad nets, meta) of a write_reference file; meta as load_pad_nets_meta
    plus ``source``. Fails loudly (ValueError) on anything else."""
    p = Path(path)
    try:
        return _reference(json.loads(p.read_text(encoding="utf-8")))
    except ValueError as e:     # json.JSONDecodeError is a ValueError too
        raise ValueError(f"{p}: {e}") from e


# ----------------------------------------------------------------------------
# Files: load_pad_nets / load_pad_nets_meta
# ----------------------------------------------------------------------------
def _meta(kind: str, version: Any, duplicates: Iterable[str] = (),
          board_only: Iterable[str] = ()) -> dict[str, Any]:
    return {"kind": kind, "format_version": version, "duplicate_refs": list(duplicates),
            "board_only_refs": list(board_only)}


def _without_refs(nets: Mapping[str, set[PadKey]], refs: Iterable[str]) -> dict[str, set[PadKey]]:
    drop = set(refs)
    kept = {name: {k for k in keys if k[0] not in drop} for name, keys in nets.items()}
    return {name: keys for name, keys in kept.items() if keys}


def _json_nets(data: Any, exclude_board_only: bool) -> tuple[dict[str, set[PadKey]], dict]:
    if isinstance(data, Mapping) and "format" in data:
        nets, meta = _reference(data)
        return (_without_refs(nets, meta["board_only_refs"]) if exclude_board_only
                else nets), meta
    nets = netlist_pad_nets(data, exclude_board_only=exclude_board_only)
    parts = [c for c in data.get("components", []) if isinstance(c, Mapping)
             and isinstance(c.get("ref"), str)]
    board_only = _refs((c["ref"] for c in parts if c.get("board_only") is True), "board_only")
    return nets, _meta("netlist", None, _repeated(c["ref"] for c in parts), board_only)


def _board_nets(text: str, exclude_board_only: bool) -> tuple[dict[str, set[PadKey]], dict]:
    footprints = _board_footprints(text)
    board_only = _refs((ref for ref, flag, _pads in footprints if flag), "board_only")
    return (_footprint_nets(footprints, exclude_board_only),
            _meta("board", pcb_format_version(text),
                  _repeated(ref for ref, _flag, _pads in footprints), board_only))


def _netlist_version(text: str) -> str | None:
    m = _NETLIST_VERSION.search(text, 0, 4096)
    return (m.group(1) if m.group(1) is not None else m.group(2)) if m else None


def load_pad_nets_meta(path: str | Path, *, exclude_board_only: bool = False
                       ) -> tuple[dict[str, set[PadKey]], dict[str, Any]]:
    """load_pad_nets plus what was read: ``{"kind": "board" | "xml" | "net" |
    "netlist" | "reference", "format_version": board header int, netlist
    export version ("E"), the reference format or None, "duplicate_refs":
    references several parts share, "board_only_refs": [...]}`` (a reference
    adds its ``source``). The lists describe the whole file, whatever
    ``exclude_board_only`` dropped."""
    p = Path(path)
    if p.suffix.lower() == ".kicad_sch":
        raise ValueError(f"{p.name} is a schematic: export its netlist first with "
                         "export_schematic_xml(sch, out_xml, kicad_cli) and load the XML")
    text = p.read_text(encoding="utf-8").lstrip(sr.LEADING)
    try:
        if text.startswith("{"):
            return _json_nets(json.loads(text), exclude_board_only)
        if text.startswith("<"):
            return xml_pad_nets(text), _meta("xml", _netlist_version(text))
        head = sr.head_of(text)
        if head == "kicad_pcb":
            return _board_nets(text, exclude_board_only)
        if head == "export":
            return sexpr_netlist_pad_nets(text), _meta("net", _netlist_version(text))
        if head == "kicad_sch":
            raise ValueError("this is a schematic: export its netlist first with "
                             "export_schematic_xml(sch, out_xml, kicad_cli)")
    except ValueError as e:     # json.JSONDecodeError is a ValueError too
        raise ValueError(f"{p}: {e}") from e
    raise ValueError(f"cannot tell what {p} is: expected netlist.json, a pad-net reference, "
                     "a KiCad XML or S-expression netlist, or a .kicad_pcb")


def load_pad_nets(path: str | Path, *, exclude_board_only: bool = False) -> dict[str, set[PadKey]]:
    """Read pad nets from a file, choosing the reader from its content:
    netlist.json, a ``kicad-eda.padnets/1`` reference (a JSON object with a
    ``format`` key), a KiCad XML or S-expression netlist, or a .kicad_pcb.
    ``exclude_board_only`` applies to netlist.json, boards and references
    (through their stored board_only list); a schematic export has no
    board-only parts. A .kicad_sch must be exported first."""
    return load_pad_nets_meta(path, exclude_board_only=exclude_board_only)[0]


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
    partly: tuple[dict[str, Any], ...]                 # pads on a net AND on NO_NET

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
    for src, dst in sorted(mapping.items(), key=lambda kv: sr.natural_key(kv[0])):
        if dst in seen:
            raise ValueError(f"ref_map sends {seen[dst]} and {src} both to {dst}")
        seen[dst] = src
    for src, dst in mapping.items():
        if src in refs_a and dst in refs_a and dst not in mapping:
            raise ValueError(f"ref_map renames {src} onto {dst}, which a still uses "
                             f"(map {dst} too)")
    unused = sorted((k for k in mapping if k not in refs_a), key=sr.natural_key)
    return mapping, unused


def _net_roots(member: Mapping[PadKey, set[str]]) -> dict[str, str]:
    """net name -> the root of its class (a pad on two nets joins them)."""
    parent: dict[str, str] = {}

    def find(n: str) -> str:
        while parent.setdefault(n, n) != n:
            parent[n] = parent[parent[n]]
            n = parent[n]
        return n

    for names in member.values():
        first, *rest = sorted(names)
        for other in rest:
            parent[find(other)] = find(first)
        find(first)
    return {n: find(n) for n in list(parent)}


def _build_side(entries: list[tuple[str, PadKey]], rename: Mapping[str, str],
                ignore: frozenset[str]) -> _Side:
    present: set[PadKey] = set()
    loose: set[PadKey] = set()
    member: dict[PadKey, set[str]] = {}
    dropped: set[str] = set()
    for name, (ref, pad) in entries:
        ref = rename.get(ref, ref)
        if ref in ignore:
            dropped.add(ref)
            continue
        present.add((ref, pad))
        if name == NO_NET:
            loose.add((ref, pad))
        else:
            member.setdefault((ref, pad), set()).add(name)
    root_of = _net_roots(member)
    groups: dict[str, set[PadKey]] = {}
    names_of: dict[str, set[str]] = {}
    for key, names in member.items():
        root = root_of[next(iter(names))]
        groups.setdefault(root, set()).add(key)
        names_of.setdefault(root, set()).update(names)
    classes: list[tuple[str, frozenset[PadKey]]] = []
    owner: dict[PadKey, int] = {}
    lone: dict[PadKey, str] = {}
    for root in sorted(groups, key=sr.natural_key):
        name = " + ".join(sorted(names_of[root], key=sr.natural_key))
        if len(groups[root]) == 1:
            lone[next(iter(groups[root]))] = name
            continue
        owner.update(dict.fromkeys(groups[root], len(classes)))
        classes.append((name, frozenset(groups[root])))
    by_pad = sorted(member.items(), key=lambda kv: sr.pad_order(kv[0]))
    conflicts = tuple({"pad": list(k), "nets": sorted(n, key=sr.natural_key),
                       "single_pin": len(groups[root_of[next(iter(n))]]) == 1}
                      for k, n in by_pad if len(n) > 1)
    partly = tuple({"pad": list(k), "nets": sorted(n, key=sr.natural_key)}
                   for k, n in by_pad if k in loose)
    return _Side(frozenset(present), MappingProxyType(owner), tuple(classes),
                 MappingProxyType(lone), conflicts, frozenset(dropped), partly)


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
                        for keys in groups.values()), key=lambda p: sr.pad_order(p.pads[0]))
        out.append(NetDiff(name, _sorted_pads(common), tuple(parts)))
    return tuple(sorted(out, key=lambda d: sr.natural_key(d.net)))


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
    return dict(sorted(out.items(), key=lambda kv: sr.natural_key(kv[0])))


def _by_net(side: _Side, keys: Iterable[PadKey]) -> dict[str, list[list[str]]]:
    out: dict[str, list[list[str]]] = {}
    for key in _sorted_pads(keys):
        out.setdefault(side.name_of(key), []).append(list(key))
    return dict(sorted(out.items(), key=lambda kv: sr.natural_key(kv[0])))


def _gone_refs(pins: Iterable[PadKey], other: _Side) -> list[str]:
    """Refs of these pins that the other side does not have at all."""
    there = {ref for ref, _ in other.present}
    return sorted({ref for ref, _ in pins if ref not in there}, key=sr.natural_key)


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
    own net) is a split; the reverse is a merge. ``details``: a pad on a net
    AND under NO_NET (one of two same-number pads left unconnected) counts as
    connected and is listed in ``partly_unconnected_pads_a/_b`` {pad, nets};
    a pad on 2+ nets in ``pad_conflicts_a/_b`` {pad, nets, single_pin}."""
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
        "partly_unconnected_pads_a": list(sa.partly),
        "partly_unconnected_pads_b": list(sb.partly),
        "ref_map_unused": unused,
        "ignored_refs": sorted(sa.dropped | sb.dropped, key=sr.natural_key),
    }
    equal = not (split or merged or missing or extra)
    return EquivalenceResult(equal, split, merged, _sorted_pads(missing), _sorted_pads(extra),
                             MappingProxyType(details))


def real_conflicts(details: Mapping[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """``pad_conflicts_a`` / ``_b`` of compare's details (or of their JSON)
    without the harmless ones: a pad alone on every net it is listed on, all
    of them KiCad ``unconnected-(...)`` nets (two copper pads of one number,
    each left unconnected: the cm5_minima demo's J502/J503 'MP'). New dicts;
    ``{**details, **real_conflicts(details)}`` is the filtered details."""
    def real(c: Mapping[str, Any]) -> bool:
        return not (c.get("single_pin") is True
                    and all(str(n).startswith(UNCONNECTED_PREFIX) for n in c["nets"]))

    return {key: [copy.deepcopy(dict(c)) for c in details.get(key, ()) if real(c)]
            for key in ("pad_conflicts_a", "pad_conflicts_b")}


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
    if p.suffix.lower() == ".kicad_sch":
        if not args.kicad_cli:
            raise ValueError(f"{p.name} is a schematic: pass --kicad-cli <path to kicad-cli> "
                             "to export it")
        p = export_schematic_xml(p, work / f"{p.stem}.xml", args.kicad_cli)
    return load_pad_nets(p, exclude_board_only=args.exclude_board_only)


def main(argv: list[str] | None = None) -> int:
    """``net_equivalence.py A B``: exit 0 equivalent, 1 different, 2 error."""
    ap = argparse.ArgumentParser(description="Pad-partition equivalence of two designs: "
                                 "netlist.json, a kicad-eda.padnets/1 reference, KiCad XML/"
                                 "S-expression netlist, .kicad_pcb, or .kicad_sch (with "
                                 "--kicad-cli).")
    ap.add_argument("a", help="reference design")
    ap.add_argument("b", help="candidate design")
    ap.add_argument("--ref-map", help="JSON {ref_in_a: ref_in_b}, inline or a file path")
    ap.add_argument("--ignore-ref", action="append", default=[], help="drop a ref (repeatable)")
    ap.add_argument("--keep-single-pin", action="store_true",
                    help="isolated pads must exist on both sides too")
    ap.add_argument("--exclude-board-only", action="store_true",
                    help="drop board_only parts (netlist.json / .kicad_pcb / reference)")
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
