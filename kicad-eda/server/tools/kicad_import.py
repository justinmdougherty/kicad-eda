#!/usr/bin/env python3
"""KiCad project -> netlist.json converter, the rebuild source of ROADMAP D4.

``convert(project, out_dir)`` reads a KiCad project (a directory, .kicad_pro,
.kicad_sch or .kicad_pcb) and writes::

    <out_dir>/netlist.json                     the design (docs/PIPELINE.md s2)
    <out_dir>/libs/<name>_imported.kicad_sym   the schematic's embedded symbols
    <out_dir>/libs/<name>_imported.pretty/     the board's footprints (KiCad 9
                                               dialect boards; else reported)
    <out_dir>/import_report.json               mismatches, assumptions, checks

Inputs: the schematic netlist exported by kicad-cli (``--format kicadxml``,
the oldest install that reads the file: KiCad 9 for a KiCad 9 schematic,
the newest, i.e. the 10.99 nightly, otherwise) for components, values,
fields, flags and pin lists; the ``.kicad_pcb`` read as text
(kicad_import_pcb, on net_equivalence's readers) for pad nets, footprints,
placement and the outline; the ``.kicad_sch`` files for embedded symbols.
The footprints are extracted by the oldest pcbnew >= 9.0
(kicad_import_fplib).

D4 rules applied by ``build_design`` (pure, no I/O):
  * the board is the source of truth for pad nets, footprints and placement,
    the schematic for symbols, values and fields; every disagreement is a
    report ``mismatches`` entry, and the pad-net partitions of both sides are
    compared with net_equivalence (``sch_vs_pcb``);
  * net names: the leading ``/`` is stripped and hierarchical ``/CH1/OUT``
    becomes ``CH1_OUT``; KiCad auto names (``Net-(...)``) are kept;
  * a pad alone on its net is no net; an unconnected symbol pin becomes
    ``no_connect``, with an ``assumptions`` entry unless it carried a
    no-connect flag;
  * every footprint gets ``at`` = [x, y, rot] (origin in the board frame +
    rotation as KiCad displays it) and ``side`` (``rebuild: "keep"``; a
    ``"reflow"`` import leaves placement to the pipeline); footprints that are
    not in the schematic are ``board_only``;
  * ``board`` carries the Edge.Cuts outline (board and footprint-internal
    shapes, arcs as bulges) in the board frame, ``datum`` = the source's aux
    origin, else its coordinate origin.

``convert`` then checks its own output (kicad_import_checks):
netlist_schema.validate (a validation that could not run fails too),
lint_netlist, a strict net_equivalence.compare(netlist.json, source board)
that covers renamed parts at their place on the board, and the extracted
libraries' paths (KiCad on Windows cannot open a file past MAX_PATH) must
all pass for ``ok``; disagreements inside the source design are reported,
not failed.

CLI: ``kicad_import.py <project> <out_dir> [--rebuild keep|reflow] [--name N]
[--netlist-xml X] [--kicad-cli C] [--no-footprints]`` prints a summary as
JSON; exit 0 when every check passed, 1 when one failed, 2 on an error.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
for _p in (str(_HERE), str(_HERE.parent)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import kicad_import_checks as kchk  # noqa: E402
import kicad_import_fplib as kfp  # noqa: E402
import kicad_import_pcb as kpcb  # noqa: E402
import kicad_import_sch as ksch  # noqa: E402
import net_equivalence as ne  # noqa: E402

REBUILD_MODES = ("keep", "reflow")
SUPPORTED_COPPER = (2, 4, 6)          # netlist_schema.COPPER_LAYER_COUNTS when importable
NETLIST_FILE, REPORT_FILE, LIBS_DIR = "netlist.json", "import_report.json", "libs"
_NAME_SAFE = re.compile(r"[^A-Za-z0-9_-]+")
_LCSC_KEYS = ("lcsc", "lcscpart", "lcscpartnumber", "lcsc#", "lcscpn", "jlcpcbpart",
              "jlcpcbpartnumber", "jlcpcb#", "jlcpartnumber")
_MPN_KEYS = ("mpn", "manufacturerpartnumber", "mfrpn", "mfrpartnumber", "mfgpn", "mfpn",
             "mpn1", "partnumber")
_LCSC_RE = re.compile(r"^C\d{3,}$")
_STANDARD_FIELDS = frozenset({"Reference", "Value", "Footprint", "Datasheet", "Description"})


def _copper_counts() -> tuple[int, ...]:
    try:
        from netlist_schema import COPPER_LAYER_COUNTS
        return tuple(COPPER_LAYER_COUNTS)
    except ImportError:
        return SUPPORTED_COPPER


# ----------------------------------------------------------------------------
# Options and the report being built
# ----------------------------------------------------------------------------
@dataclass(frozen=True)
class ImportOptions:
    """build_design's inputs besides the two parsed sides. ``symbol_lib`` +
    ``symbol_names`` {ref: name} point lib_ids into the extracted symbol
    library; ``footprint_names`` {BoardFootprint.index: "nick:name"} into the
    extracted footprint library (else the board's own footprint ids)."""
    name: str
    rebuild: str = "keep"
    title: str = ""
    symbol_lib: str = ""
    symbol_names: Mapping[str, str] = field(default_factory=dict)
    footprint_names: Mapping[int, str] = field(default_factory=dict)
    libraries: Mapping[str, Mapping[str, str]] | None = None
    source: Mapping[str, Any] | None = None


@dataclass
class _Report:
    mismatches: list[dict] = field(default_factory=list)
    assumptions: list[dict] = field(default_factory=list)
    no_connect: list[dict] = field(default_factory=list)
    unsupported: list[str] = field(default_factory=list)
    renamed_refs: list[dict] = field(default_factory=list)
    dropped: list[dict] = field(default_factory=list)
    extra: dict = field(default_factory=dict)

    def mismatch(self, kind: str, ref: str, detail: str, **more: Any) -> None:
        self.mismatches.append({"kind": kind, "ref": ref, "detail": detail, **more})

    def assume(self, aid: str, topic: str, value: Any, rationale: str, confidence: str) -> None:
        self.assumptions.append({"id": aid, "topic": topic, "value": value,
                                 "rationale": rationale, "confidence": confidence})


@dataclass(frozen=True)
class _Part:
    fp: kpcb.BoardFootprint
    ref: str                                  # unique ref in the design
    comp: ksch.SchComponent | None            # None = board-only


def _natural(text: str) -> tuple:
    return ne._natural(text)


# ----------------------------------------------------------------------------
# References and parts
# ----------------------------------------------------------------------------
def _unique_refs(board: kpcb.BoardDesign, rep: _Report) -> dict[int, str]:
    """index -> a unique reference: the board's own, else (empty or already
    used) its letter prefix numbered past every reference in use."""
    taken = {fp.ref for fp in board.footprints}
    seen: set[str] = set()
    out: dict[int, str] = {}
    for fp in board.footprints:
        ref = fp.ref
        if not ref or ref in seen:
            prefix = re.match(r"[A-Za-z_#]*", ref).group() or "X"
            n = 1
            while f"{prefix}{n}" in taken:
                n += 1
            ref = f"{prefix}{n}"
            taken.add(ref)
            rep.renamed_refs.append({"original": fp.ref, "ref": ref, "index": fp.index,
                                     "footprint": fp.lib_id})
            rep.mismatch("duplicate_ref", ref, f"board footprint {fp.lib_id} has reference "
                         f"{fp.ref!r}, {'empty' if not fp.ref else 'used twice'}; renamed {ref}")
        seen.add(ref)
        out[fp.index] = ref
    return out


def _linked(sch: ksch.SchDesign, board: kpcb.BoardDesign, refs: Mapping[int, str]
            ) -> dict[int, ksch.SchComponent]:
    by_ref = {c.ref: c for c in sch.components}
    return {fp.index: by_ref[fp.ref] for fp in board.footprints
            if fp.ref in by_ref and refs[fp.index] == fp.ref and not fp.board_only}


def schematic_parts(sch: ksch.SchDesign, board: kpcb.BoardDesign
                    ) -> dict[int, ksch.SchComponent]:
    """BoardFootprint.index -> its schematic component: the footprint that
    keeps the component's reference (not a renamed duplicate); footprints
    with the board_only attribute never link (KiCad keeps them out of the
    schematic)."""
    return _linked(sch, board, _unique_refs(board, _Report()))


def _parts(sch: ksch.SchDesign, board: kpcb.BoardDesign, rep: _Report) -> list[_Part]:
    refs = _unique_refs(board, rep)
    linked = _linked(sch, board, refs)
    parts = [_Part(fp, refs[fp.index], linked.get(fp.index)) for fp in board.footprints]
    on_board = {p.comp.ref for p in parts if p.comp is not None}
    for comp in sch.components:
        if comp.ref in on_board:
            if ksch.FLAG_NO_BOARD in comp.flags:
                rep.mismatch("excluded_but_on_board", comp.ref, "the symbol is excluded from the "
                             "board but the board has its footprint; the board wins (D4)")
        elif ksch.FLAG_NO_BOARD in comp.flags:
            rep.dropped.append({"ref": comp.ref, "reason": "exclude_from_board"})
        else:
            rep.mismatch("missing_from_board", comp.ref, f"schematic symbol ({comp.symbol_key}) "
                         "has no footprint on the board; dropped, the board wins (D4)")
    for p in parts:
        if p.comp is None and not p.fp.board_only:
            rep.mismatch("not_in_schematic", p.ref, f"board footprint {p.fp.lib_id} has no symbol "
                         "and is not marked board-only; imported as board_only")
    return parts


# ----------------------------------------------------------------------------
# Nets
# ----------------------------------------------------------------------------
def sanitize_net_name(name: str) -> str:
    """D4: strip the leading '/', hierarchical '/CH1/OUT' -> 'CH1_OUT'. A
    '/' inside parentheses (KiCad auto names such as 'Net-(U1-TEST#/SDA)')
    is kept; names without a leading '/' are kept as they are."""
    if not name.startswith("/"):
        return name
    out, depth = [], 0
    for ch in name[1:]:
        if ch == "(":
            depth += 1
        elif ch == ")" and depth:
            depth -= 1
        out.append("_" if ch == "/" and depth == 0 else ch)
    return "".join(out) or "unnamed"


def _net_names(names: Iterable[str]) -> dict[str, str]:
    """original -> unique sanitized name; names that need no change claim
    theirs first, then the mapped ones in natural order (``_2``, ... on a
    collision)."""
    out: dict[str, str] = {}
    used: set[str] = set()
    for name in sorted(set(names), key=lambda n: (sanitize_net_name(n) != n, _natural(n))):
        base = sanitize_net_name(name)
        cand, n = base, 2
        while cand in used:
            cand, n = f"{base}_{n}", n + 1
        used.add(cand)
        out[name] = cand
    return out


def _board_nets(parts: list[_Part], rep: _Report) -> dict[str, list[tuple[str, str]]]:
    """{original board net: [(ref, pad)]}, each pad key once (a pad number
    on two nets keeps its first and is reported)."""
    nets: dict[str, list[tuple[str, str]]] = {}
    owner: dict[tuple[str, str], str] = {}
    for p in parts:
        for pad in p.fp.pads:
            key = (p.ref, pad.number)
            if pad.net == ne.NO_NET:
                continue
            if key in owner:
                if owner[key] != pad.net:
                    rep.mismatch("pad_conflict", p.ref, f"pad {pad.number} sits on {owner[key]!r} "
                                 f"and {pad.net!r}; kept on {owner[key]!r}", pad=pad.number)
                continue
            owner[key] = pad.net
            nets.setdefault(pad.net, []).append(key)
    return nets


def _design_nets(board_nets: Mapping[str, list[tuple[str, str]]], rep: _Report
                 ) -> dict[str, list[list[str]]]:
    multi = {name: keys for name, keys in board_nets.items() if len(keys) >= 2}
    names = _net_names(multi)
    rep.extra["net_name_map"] = {names[o]: o for o in sorted(multi, key=_natural)
                                 if names[o] != o}
    out = {names[o]: [list(k) for k in sorted(keys, key=ne._pad_order)]
           for o, keys in multi.items()}
    return dict(sorted(out.items(), key=lambda kv: _natural(kv[0])))


_NC_TEXT = {
    "unconnected": ("high", "the pin is unconnected in the source design and carries no "
                            "no-connect flag; marked no_connect so the rebuild states it (D4)"),
    "isolated_on_board": ("med", "the schematic connects the pin but its board pad is alone on "
                                 "its net; the board is the source of truth for pad nets (D4)"),
    "no_pad": ("med", "the board footprint has no pad with this number, so the pin cannot "
                      "connect; marked no_connect"),
    "not_in_netlist_export": ("med", "the symbol has the pin but the schematic netlist export "
                                     "lists it on no net (a unit that is not placed, or a pin "
                                     "without a footprint pad); marked no_connect"),
}


def _nc_reason(key: tuple[str, str], node: tuple[ksch.SchNode, int] | None,
               pads: set[tuple[str, str]]) -> str:
    if node is not None and node[0].flagged_nc:
        return "flagged"
    if key not in pads:
        return "no_pad"
    if node is None:
        return "not_in_netlist_export"
    return "isolated_on_board" if node[1] > 1 else "unconnected"


def _no_connects(sch: ksch.SchDesign, parts: list[_Part], netted: set[tuple[str, str]],
                 rep: _Report) -> list[list[str]]:
    """Every symbol pin of a schematic part that is on no design net."""
    nodes: dict[tuple[str, str], tuple[ksch.SchNode, int]] = {}
    node_pins: dict[str, set[str]] = {}
    for net in sch.nets:
        for node in net.nodes:
            nodes[(node.ref, node.pin)] = (node, len(net.nodes))
            node_pins.setdefault(node.ref, set()).add(node.pin)
    out: list[list[str]] = []
    for p in sorted((p for p in parts if p.comp is not None), key=lambda p: _natural(p.ref)):
        comp = p.comp
        pins = {pin.number for pin in sch.pins_of(comp)} | node_pins.get(comp.ref, set())
        pads = {(comp.ref, pad.number) for pad in p.fp.pads}
        for pin in sorted((x for x in pins if x), key=_natural):
            if (p.ref, pin) in netted:
                continue
            reason = _nc_reason((comp.ref, pin), nodes.get((comp.ref, pin)), pads)
            out.append([p.ref, pin])
            rep.no_connect.append({"pad": [p.ref, pin], "reason": reason})
            if reason == "no_pad":
                rep.mismatch("pin_without_pad", p.ref, f"symbol pin {pin} has no pad on "
                             f"footprint {p.fp.lib_id}", pad=pin)
            if reason != "flagged":
                confidence, why = _NC_TEXT[reason]
                rep.assume(f"import-nc-{p.ref}-{pin}", "no_connect", [p.ref, pin], why, confidence)
    return out


# ----------------------------------------------------------------------------
# Board frame and outline
# ----------------------------------------------------------------------------
def _frame(board: kpcb.BoardDesign, rep: _Report) -> tuple[dict, float, float]:
    """kicad_import_pcb.board_frame, its fallbacks recorded in the report."""
    frame = kpcb.board_frame(board)
    rep.extra["outline"] = {**frame.info,
                            "footprint_edges": sorted(frame.info["footprint_edges"],
                                                      key=_natural)}
    if frame.problem:
        rep.unsupported.append(f"Edge.Cuts outline not usable ({frame.problem}); the board "
                               "block is its bounding rectangle")
        rep.assume("import-outline", "board outline", "bounding rectangle",
                   f"the Edge.Cuts shapes do not form valid closed loops: {frame.problem}", "low")
    elif frame.info["kind"] == "fallback_rectangle":
        rep.assume("import-outline", "board outline", "footprint extent + margin",
                   "the board has no Edge.Cuts outline; the frame is the footprint origins' "
                   f"extent plus {kpcb.FALLBACK_MARGIN_MM:g} mm", "low")
    return frame.block, frame.x0, frame.y0


def _copper(board: kpcb.BoardDesign, rep: _Report) -> int | None:
    n = len(board.copper_layers)
    allowed = _copper_counts()
    rep.extra["copper_layers_source"] = list(board.copper_layers)
    if n in allowed:
        return n
    if n == 1 and 2 in allowed:
        rep.assume("import-copper-layers", "copper_layers", 2, "the source board has one copper "
                   "layer; the rebuild uses 2 with the back layer left empty", "high")
        return 2
    rep.unsupported.append(f"the source board has {n} copper layers; netlist copper_layers "
                           f"accepts {list(allowed)} (D4), so it is left out")
    rep.assume("import-copper-layers", "copper_layers", n, f"the source has {n} copper layers, "
               "which the netlist schema cannot express yet; copper_layers is omitted and the "
               "rebuild must set the stackup by hand", "low")
    return None


# ----------------------------------------------------------------------------
# Components
# ----------------------------------------------------------------------------
def _norm_field(name: str) -> str:
    return re.sub(r"[\s_.\-]", "", name.lower())


def _sourcing(comp: ksch.SchComponent, rep: _Report) -> dict[str, str]:
    fields = {_norm_field(k): v.strip() for k, v in comp.fields if v.strip()}
    out: dict[str, str] = {}
    lcsc = next((fields[k] for k in _LCSC_KEYS if k in fields), "")
    if lcsc:
        if _LCSC_RE.match(lcsc):
            out["lcsc"] = lcsc
        else:
            rep.unsupported.append(f"{comp.ref}: LCSC field {lcsc!r} is not a C<digits> part "
                                   "number; kept in fields only")
    mpn = next((fields[k] for k in _MPN_KEYS if k in fields), "")
    if mpn:
        out["mpn"] = mpn
    return out


def _fields(comp: ksch.SchComponent) -> dict[str, str]:
    return {k: v for k, v in comp.fields
            if v.strip() and k not in _STANDARD_FIELDS and not k.startswith(("Sim.", "ki_"))}


def _flag_mismatches(p: _Part, rep: _Report) -> None:
    comp, fp = p.comp, p.fp
    if comp.value != fp.value:
        rep.mismatch("value", p.ref, f"schematic value {comp.value!r}, board value {fp.value!r}; "
                     "the schematic wins (D4)")
    if comp.footprint and comp.footprint != fp.lib_id:
        rep.mismatch("footprint", p.ref, f"schematic footprint field {comp.footprint!r}, board "
                     f"footprint {fp.lib_id!r}; the board wins (D4)")
    if comp.dnp != ("dnp" in fp.attrs):
        rep.mismatch("dnp", p.ref, f"DNP is {comp.dnp} in the schematic and "
                     f"{'dnp' in fp.attrs} on the board; the schematic wins")
    no_bom = ksch.FLAG_NO_BOM in comp.flags
    if no_bom != ("exclude_from_bom" in fp.attrs):
        rep.mismatch("in_bom", p.ref, f"exclude-from-BOM is {no_bom} in the schematic and "
                     f"{'exclude_from_bom' in fp.attrs} on the board; the schematic wins")


def _symbol_id(p: _Part, opts: ImportOptions, rep: _Report) -> str:
    name = opts.symbol_names.get(p.comp.ref)
    if name and opts.symbol_lib:
        return f"{opts.symbol_lib}:{name}"
    rep.unsupported.append(f"{p.ref}: no extracted symbol; lib_id is the schematic's "
                           f"{p.comp.symbol_key!r}")
    return p.comp.symbol_key if p.comp.lib else f"{opts.symbol_lib or opts.name}:{p.comp.part}"


def _component(p: _Part, x0: float, y0: float, opts: ImportOptions, rep: _Report) -> dict:
    fp, comp = p.fp, p.comp
    out: dict[str, Any] = {"ref": p.ref}
    if comp is None:
        out["board_only"] = True
    else:
        out["lib_id"] = _symbol_id(p, opts, rep)
        _flag_mismatches(p, rep)
    out["value"] = comp.value if comp is not None else fp.value
    out["footprint"] = opts.footprint_names.get(fp.index, fp.lib_id)
    if ":" not in out["footprint"]:
        rep.unsupported.append(f"{p.ref}: board footprint {fp.lib_id!r} names no library and "
                               "was not extracted; no library can resolve it")
    if opts.rebuild == "keep":
        out["at"] = [kpcb.round_mm(fp.x - x0), kpcb.round_mm(fp.y - y0), fp.rot]
    out["side"] = fp.side
    dnp = comp.dnp if comp is not None else "dnp" in fp.attrs
    no_bom = (ksch.FLAG_NO_BOM in comp.flags if comp is not None
              else "exclude_from_bom" in fp.attrs)
    if dnp:
        out["dnp"] = True
    if no_bom:
        out["in_bom"] = False
    if comp is not None:
        out.update(_sourcing(comp, rep))
        if comp.datasheet not in ("", "~"):
            out["datasheet"] = comp.datasheet
        if comp.description:
            out["description"] = comp.description
        if fields := _fields(comp):
            out["fields"] = fields
        if comp.sheet_names.strip("/"):
            out["sheet"] = comp.sheet_names.strip("/")
    return out


# ----------------------------------------------------------------------------
# build_design
# ----------------------------------------------------------------------------
def sch_pad_nets(sch: ksch.SchDesign) -> dict[str, set[tuple[str, str]]]:
    """The schematic's pad nets as net_equivalence.xml_pad_nets reads them
    (parts excluded from the board dropped, empty pin numbers skipped)."""
    skip = {c.ref for c in sch.components if ksch.FLAG_NO_BOARD in c.flags}
    out: dict[str, set[tuple[str, str]]] = {}
    for net in sch.nets:
        for node in net.nodes:
            if node.pin and node.ref not in skip:
                out.setdefault(net.name, set()).add((node.ref, node.pin))
    return out


def board_pad_nets(board: kpcb.BoardDesign) -> dict[str, set[tuple[str, str]]]:
    """The board's pad nets (pads on no net under NO_NET), original refs."""
    out: dict[str, set[tuple[str, str]]] = {}
    for fp in board.footprints:
        for pad in fp.pads:
            out.setdefault(pad.net, set()).add((fp.ref, pad.number))
    return out


def _net_mismatches(res: ne.EquivalenceResult, rep: _Report) -> None:
    for d in res.split_nets:
        rep.mismatch("net_split", "", f"schematic net {d.net!r} is split on the board into "
                     + ", ".join(repr(part.net or "(no net)") for part in d.parts),
                     net=d.net, parts=[part.as_dict() for part in d.parts])
    for d in res.merged_nets:
        rep.mismatch("net_merged", "", f"board net {d.net!r} joins the schematic nets "
                     + ", ".join(repr(part.net or "(no net)") for part in d.parts),
                     net=d.net, parts=[part.as_dict() for part in d.parts])
    for ref, pad in res.missing_pins:
        rep.mismatch("pin_missing_on_board", ref, f"{ref}.{pad} is connected in the schematic "
                     "but has no pad on the board", pad=pad)
    for ref, pad in res.extra_pins:
        rep.mismatch("pad_missing_in_schematic", ref, f"{ref}.{pad} is connected on the board "
                     "but is no pin in the schematic", pad=pad)


def _sch_vs_pcb(sch: ksch.SchDesign, board: kpcb.BoardDesign, parts: list[_Part],
                rep: _Report) -> dict:
    """net_equivalence.compare(schematic, board) without the board-only parts."""
    ignore = sorted({p.fp.ref for p in parts if p.comp is None}, key=_natural)
    res = ne.compare(sch_pad_nets(sch), board_pad_nets(board), ignore_refs=ignore)
    _net_mismatches(res, rep)
    return {"summary": res.summary(), **res.as_dict()}


def build_design(sch: ksch.SchDesign, board: kpcb.BoardDesign,
                 opts: ImportOptions) -> tuple[dict, dict]:
    """(netlist.json design, import report) from the parsed schematic export
    and board (see the module doc for the rules). Pure: no files, no KiCad."""
    if opts.rebuild not in REBUILD_MODES:
        raise ValueError(f"rebuild must be one of {REBUILD_MODES}, got {opts.rebuild!r}")
    rep = _Report()
    parts = _parts(sch, board, rep)
    sch_vs_pcb = _sch_vs_pcb(sch, board, parts, rep)
    nets = _design_nets(_board_nets(parts, rep), rep)
    netted = {(ref, pad) for keys in nets.values() for ref, pad in keys}
    no_connect = _no_connects(sch, parts, netted, rep)
    block, x0, y0 = _frame(board, rep)
    components = [_component(p, x0, y0, opts, rep)
                  for p in sorted(parts, key=lambda p: _natural(p.ref))]
    design: dict[str, Any] = {"name": opts.name, "title": opts.title or sch.title or opts.name}
    if opts.source:
        design["source"] = dict(opts.source)
    design["rebuild"] = opts.rebuild
    if (copper := _copper(board, rep)) is not None:
        design["copper_layers"] = copper
    design["board"] = block
    if opts.libraries:
        design["libraries"] = {k: dict(v) for k, v in opts.libraries.items() if v}
    design.update(components=components, nets=nets, no_connect=no_connect,
                  assumptions=[dict(a) for a in rep.assumptions])
    return design, _report_dict(sch, board, parts, rep, sch_vs_pcb, opts.rebuild)


def _report_dict(sch: ksch.SchDesign, board: kpcb.BoardDesign, parts: list[_Part],
                 rep: _Report, sch_vs_pcb: dict, rebuild: str) -> dict:
    out = {
        "counts": {"schematic_components": len(sch.components), "board_footprints": len(parts),
                   "board_only": sum(p.comp is None for p in parts),
                   "no_connect": len(rep.no_connect), "assumptions": len(rep.assumptions),
                   "mismatches": len(rep.mismatches)},
        "board_format": board.format_version, "board_generator": board.generator_version,
        "schematic_tool": sch.tool,
        "mismatches": rep.mismatches, "sch_vs_pcb": sch_vs_pcb,
        "assumptions": rep.assumptions, "no_connect": rep.no_connect,
        "board_only": sorted((p.ref for p in parts if p.comp is None), key=_natural),
        "dropped": rep.dropped, "renamed_refs": rep.renamed_refs,
        "unsupported": rep.unsupported + list(board.notes),
        **rep.extra,
    }
    if rebuild == "reflow":      # the placement a reflow import does not carry
        out["source_placement"] = [
            {"ref": p.ref, "footprint": p.fp.lib_id, "x": kpcb.round_mm(p.fp.x),
             "y": kpcb.round_mm(p.fp.y), "rot": p.fp.rot, "side": p.fp.side}
            for p in parts]
    return out


# ----------------------------------------------------------------------------
# Project files, kicad-cli, libraries
# ----------------------------------------------------------------------------
@dataclass(frozen=True)
class ProjectFiles:
    root: Path
    stem: str
    sch: Path
    pcb: Path
    pro: Path | None


def locate_project(path: str | Path) -> ProjectFiles:
    """The project's root schematic and board: ``path`` is a project
    directory (one .kicad_pro, or one board), or any of its .kicad_pro /
    .kicad_sch / .kicad_pcb files. Both the schematic and the board must
    exist (D4 reads both)."""
    p = Path(path)
    if p.is_dir():
        pros = sorted(p.glob("*.kicad_pro"))
        if len(pros) > 1:
            raise ValueError(f"{p} holds {len(pros)} projects ("
                             + ", ".join(x.name for x in pros) + "); pass one .kicad_pro")
        cands = pros or sorted(p.glob("*.kicad_pcb"))
        if len(cands) != 1:
            raise ValueError(f"{p} holds no single KiCad project (.kicad_pro or .kicad_pcb)")
        root, stem = p, cands[0].stem
    elif p.suffix in (".kicad_pro", ".kicad_sch", ".kicad_pcb"):
        if not p.is_file():
            raise FileNotFoundError(f"{p} not found")
        root, stem = p.parent, p.stem
    else:
        raise ValueError(f"{p} is not a KiCad project directory or .kicad_pro/.kicad_sch/"
                         ".kicad_pcb file")
    sch, pcb, pro = (root / f"{stem}{ext}" for ext in (".kicad_sch", ".kicad_pcb", ".kicad_pro"))
    missing = [x.name for x in (sch, pcb) if not x.is_file()]
    if missing:
        raise FileNotFoundError(f"{root}: {', '.join(missing)} not found; the converter reads "
                                "both the schematic and the board")
    return ProjectFiles(root.resolve(), stem, sch.resolve(), pcb.resolve(),
                        pro.resolve() if pro.is_file() else None)


def project_name(stem: str) -> str:
    """A netlist name / library nickname stem from a project stem."""
    return _NAME_SAFE.sub("_", stem).strip("_") or "project"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_block(files: ProjectFiles, sheets: Iterable[tuple[str, str]], tool: str) -> dict:
    """The design ``source`` block (D4): ``path`` = the .kicad_pro (else the
    root schematic), ``sha256`` = the sha256 of that file, ``files`` =
    {project-relative name: sha256} of every file read (the sheets, the
    board and the project file), so each input can be verified."""
    names = [rel for rel, _t in sheets] + [files.pcb.name]
    if files.pro is not None:
        names.append(files.pro.name)
    path = files.pro or files.sch
    return {"kind": "kicad", "path": str(path), "sha256": _sha256(path), "tool": tool,
            "files": {rel: _sha256(files.root / rel) for rel in sorted(set(names))}}


# ----------------------------------------------------------------------------
# convert()
# ----------------------------------------------------------------------------
def _netlist_text(files: ProjectFiles, sheets: list[tuple[str, str]], netlist_xml: str | None,
                  kicad_cli: str | None) -> tuple[str, str]:
    """(XML export text, tool) from a given export or a fresh kicad-cli run."""
    if netlist_xml:
        text = Path(netlist_xml).read_text(encoding="utf-8")
        return text, ksch.parse_netlist_xml(text).tool or "kicad xml netlist"
    if kicad_cli:
        import kicad_env as env
        version = env.cli_version(Path(kicad_cli)) or "unknown version"
    else:
        kicad_cli, version = ksch.choose_kicad_cli(ksch.schematic_version(sheets[0][1]))
    extra = [files.pro] if files.pro is not None else []
    with tempfile.TemporaryDirectory(prefix="kicad_import_") as tmp:
        text = ksch.export_netlist_xml(files.root, sheets, extra, kicad_cli, Path(tmp))
    return text, f"kicad-cli {version}"


def convert(project: str | Path, out_dir: str | Path, *, rebuild: str = "keep",
            name: str | None = None, netlist_xml: str | None = None,
            kicad_cli: str | None = None, kicad_python: str | None = None,
            extract_footprints: bool = True) -> dict:
    """Convert a KiCad project into ``<out_dir>/netlist.json`` + libraries +
    ``import_report.json`` (module doc). ``netlist_xml``: an existing
    kicadxml export to use instead of running kicad-cli; ``kicad_cli`` /
    ``kicad_python``: override the installs picked; ``extract_footprints``:
    False keeps the board's own footprint ids. Returns {"ok", "netlist",
    "report_file", "design", "report"}; ``ok`` = every check of
    kicad_import_checks.check_design ran and passed (source mismatches are
    reported, not failed). Raises on unreadable input."""
    if rebuild not in REBUILD_MODES:
        raise ValueError(f"rebuild must be one of {REBUILD_MODES}, got {rebuild!r}")
    files = locate_project(project)
    name = project_name(name or files.stem)
    nick, out = f"{name}_imported", Path(out_dir)
    libs = out / LIBS_DIR
    sheets = ksch.read_sheets(files.sch)
    xml_text, tool = _netlist_text(files, sheets, netlist_xml, kicad_cli)
    sch = ksch.parse_netlist_xml(xml_text)
    board_text = files.pcb.read_text(encoding="utf-8")
    board = kpcb.read_board(board_text)
    linked = schematic_parts(sch, board)
    sym_path = libs / f"{nick}.kicad_sym"
    plan, sym_info = ksch.write_symbol_library(
        sorted(linked.values(), key=lambda c: _natural(c.ref)), sheets, sym_path)
    fplib, fp_info = kfp.write_footprint_library(
        files.pcb, board, [fp.index for fp in board.footprints if fp.index not in linked],
        nick, libs / f"{nick}.pretty", kicad_python, extract_footprints)
    footprint_libs = ({nick: f"{LIBS_DIR}/{nick}.pretty"} if fplib
                      else kfp.project_footprint_libs(files.root, board))
    source = source_block(files, sheets, tool)
    opts = ImportOptions(
        name=name, rebuild=rebuild, symbol_lib=nick if plan.entries else "",
        symbol_names=plan.names, footprint_names=fplib.names if fplib else {},
        libraries={"symbols": {nick: f"{LIBS_DIR}/{nick}.kicad_sym"} if plan.entries else {},
                   "footprints": footprint_libs},
        source=source)
    design, built = build_design(sch, board, opts)
    library_files = ([str(sym_path)] if plan.entries else []) + list(fplib.files if fplib else ())
    checks = kchk.check_design(design, board_text, built["renamed_refs"], library_files)
    report = {"ok": checks["ok"], "source": source,
              "outputs": {"netlist": str(out / NETLIST_FILE), "report": str(out / REPORT_FILE)},
              "libraries": {"symbols": sym_info, "footprints": fp_info},
              **built, "checks": checks}
    ksch.io_path(out).mkdir(parents=True, exist_ok=True)
    for file_name, data in ((NETLIST_FILE, design), (REPORT_FILE, report)):
        ksch.io_path(out / file_name).write_text(
            json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    return {"ok": report["ok"], "netlist": str(out / NETLIST_FILE),
            "report_file": str(out / REPORT_FILE), "design": design, "report": report}


def summary(result: Mapping[str, Any]) -> dict:
    """The CLI's one-screen view of a convert() result."""
    rep = result["report"]
    checks = rep["checks"]
    return {"ok": result["ok"], "netlist": result["netlist"], "report": result["report_file"],
            "counts": rep["counts"], "schema_validated": checks["schema_validated"],
            "schema_errors": checks["schema_errors"] if checks["schema_errors"] is None
            else len(checks["schema_errors"]),
            "lint_errors": checks["lint"]["errors"],
            "netlist_vs_pcb": checks["netlist_vs_pcb"]["summary"],
            "sch_vs_pcb": rep["sch_vs_pcb"]["summary"],
            "mismatch_kinds": sorted({m["kind"] for m in rep["mismatches"]}),
            "footprints_extracted": rep["libraries"]["footprints"]["extracted"],
            "footprint_format": rep["libraries"]["footprints"].get("format"),
            "library_paths_too_long": len(checks["library_paths"]["too_long"])}


def main(argv: list[str] | None = None) -> int:
    """Exit 0: converted and every check passed; 1: converted, a check
    failed (see the report); 2: the conversion itself failed."""
    ap = argparse.ArgumentParser(description="Convert a KiCad project to netlist.json (D4).")
    ap.add_argument("project", help="project directory, .kicad_pro, .kicad_sch or .kicad_pcb")
    ap.add_argument("out_dir", help="where netlist.json, libs/ and import_report.json go")
    ap.add_argument("--rebuild", choices=REBUILD_MODES, default="keep")
    ap.add_argument("--name", help="netlist name / library nickname stem (default: project)")
    ap.add_argument("--netlist-xml", help="use this kicadxml export instead of running kicad-cli")
    ap.add_argument("--kicad-cli", help="kicad-cli to export the schematic with")
    ap.add_argument("--no-footprints", action="store_true",
                    help="keep the board's footprint ids, extract no footprint library")
    args = ap.parse_args(argv)
    try:
        res = convert(args.project, args.out_dir, rebuild=args.rebuild, name=args.name,
                      netlist_xml=args.netlist_xml, kicad_cli=args.kicad_cli,
                      extract_footprints=not args.no_footprints)
    except (OSError, ValueError, RuntimeError) as e:
        print(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}))
        return 2
    print(json.dumps(summary(res), indent=1))
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
