#!/usr/bin/env python3
"""KiCad ``(stackup ...)`` blocks as text: emit, splice, extract, read back (ADR
D7.1 'Rules for the block', D7.3, D7.5).

No KiCad API can author a stackup (D7.1). The builder sets the copper count,
layer types and thickness through SWIG, saves the board, splices ``emit(rs)`` in
as the first child of ``(setup``, then loads the file fresh, fills and saves it
(swig_stackup.finalize); KiCad 9.0.4 and 10.0.5 keep the block as written.

* ``emit(rs, dialect=9)``: the block exactly as KiCad 9/10 write it (the KiCad 9
  dialect serves both): every technical layer (F/B SilkS, Paste, Mask: without
  them the .gbrjob drops every Thickness), extra plies after the bare token
  ``addsublayer``, mask thickness = the resolved ``over_substrate`` (0 without a
  mask), dielectrics ``dielectric k`` with ``locked`` from the stackup,
  ``copper_finish`` in KiCad's names, ``dielectric_constraints yes`` iff
  impedance control is required. Numbers use KiCad's formats (``kicad_mm``,
  ``kicad_er``, ``kicad_double``). The legacy shorthand writes no block
  (ValueError, D7.5 step 6); KiCad 11 extras are not emitted in wave 2.
* ``splice(text, block)`` / ``extract(text)``: put the block in (replacing one)
  / take it out of board text, with a tokenizer that honours quoted strings.
* ``parse_block`` (a block or a board) -> ``StackupBlock``; ``board_layers``,
  ``enabled_copper``, ``copper_types``, ``general_thickness`` read the header.
* ``compare(rs, text)``: the D7.5 step 5 text checks of a saved board (copper
  list, layer types, general thickness +/- 0.001 mm, the block equal to
  ``emit(rs)`` after whitespace normalisation; no block check for legacy).
* ``netlist_stackup_from_board(text)``: a KiCad stackup (block or board) as the
  D7.2.2 ``layers`` form, origin ``designer``, rationale ``imported from KiCad
  board`` (F4 import). Roles come from the board's layer types when the text has
  a ``(layers ...)`` section (power -> plane, mixed -> mixed, else signal).

Stdlib only and Python 3.11-compatible: KiCad's bundled Python imports it.
"""
from __future__ import annotations

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:                                  # pragma: no cover - typing only
    from stackup_model import Dielectric, MaskSpec, Ply, ResolvedStackup

DIALECTS = (9, 10)
SILK = {"F": ("F.SilkS", "Top Silk Screen"), "B": ("B.SilkS", "Bottom Silk Screen")}
PASTE = {"F": ("F.Paste", "Top Solder Paste"), "B": ("B.Paste", "Bottom Solder Paste")}
MASK = {"F": ("F.Mask", "Top Solder Mask"), "B": ("B.Mask", "Bottom Solder Mask")}
FINISH_TO_KICAD = {"ENIG": "ENIG", "HASL": "HAL SnPb", "LF-HASL": "HAL lead-free",
                   "OSP": "OSP", "ImAg": "Immersion silver", "ImSn": "Immersion tin"}
KICAD_TO_FINISH = {**{v: k for k, v in FINISH_TO_KICAD.items()}, "HT_OSP": "OSP"}
KICAD_NO_FINISH = "None"           # KiCad's default finish, written back on every save
MASK_LABELS = {"lpi": "LPI"}       # library ids -> the names KiCad and fabs use
KICAD_MASK_DEFAULTS = (3.3, 0.0)   # KiCad's solder mask Er / loss tangent when unspecified
KICAD_DEFAULT_DIELECTRIC = ("FR4", 4.5, 0.02)   # a KiCad dielectric without a material
KICAD_DEFAULT_COPPER_MM = 0.035
UNNAMED_DIELECTRIC = "custom dielectric"
ROLE_OF_TYPE = {"power": "plane", "mixed": "mixed"}   # every other copper type: signal
IMPORT_RATIONALE = "imported from KiCad board"
THICKNESS_TOL_MM = 0.001
DIFF_CONTEXT = 6                   # tokens shown around the first block difference
I2, I3, I4 = "\t" * 2, "\t" * 3, "\t" * 4
_TOKEN = re.compile(r'"(?:[^"\\]|\\.)*"|[()]|[^\s()"]+')
_ESCAPE = re.compile(r"\\(.)", re.S)
_COPPER = re.compile(r"^(?:F|B|In([1-9][0-9]?))\.Cu$")
_SPANS = {("kicad_pcb", "general"): "general", ("kicad_pcb", "layers"): "layers",
          ("kicad_pcb", "setup"): "setup", ("kicad_pcb", "setup", "stackup"): "stackup",
          ("stackup",): "block"}


# ---------------------------------------------------------------------------
# KiCad's number and string formats
# ---------------------------------------------------------------------------
def _nm(value_mm: float) -> int:
    """mm -> nm, rounded half away from zero (KiCad's parseBoardUnits / KiROUND)."""
    scaled = abs(float(value_mm)) * 1e6
    return int(math.copysign(math.floor(scaled + 0.5), value_mm))


def _strip_fixed(value: float, digits: int) -> str:
    return f"{value:.{digits}f}".rstrip("0").rstrip(".")


def kicad_mm(value_mm: float) -> str:
    """A length as KiCad writes it (FormatInternalUnits): nm-rounded, '%.10g', or
    '%.10f' without trailing zeros at or below 0.0001 mm."""
    mm = _nm(value_mm) / 1e6
    if mm != 0.0 and abs(mm) <= 0.0001:
        return _strip_fixed(mm, 10)
    return f"{mm:.10g}"


def kicad_er(value: float) -> str:
    """epsilon_r as KiCad 9/10 write it ('%g', six significant digits)."""
    return f"{float(value):g}"


def kicad_double(value: float) -> str:
    """A double as KiCad's FormatDouble2Str writes it (loss tangent)."""
    v = float(value)
    if v != 0.0 and abs(v) <= 0.0001:
        return _strip_fixed(v, 16)
    return f"{v:.10g}"


def _q(text: str) -> str:
    """A quoted string as KiCad writes it (Quotew)."""
    escaped = str(text).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{escaped}"'


def _unquote(token: str) -> str:
    if not token.startswith('"'):
        return token
    return _ESCAPE.sub(lambda m: "\n" if m.group(1) == "n" else m.group(1), token[1:-1])


# ---------------------------------------------------------------------------
# emit
# ---------------------------------------------------------------------------
def _item(name: str, type_name: str, sublayers: Sequence[Sequence[str]]) -> list[str]:
    lines = [f"{I3}(layer {_q(name)}", f"{I4}(type {_q(type_name)})"]
    for k, tokens in enumerate(sublayers):
        if k:   # KiCad's layout: the bare keyword trails the previous ply's last token
            lines[-1] += " addsublayer"
        lines.extend(f"{I4}{t}" for t in tokens)
    lines.append(f"{I3})")
    return lines


def mask_label(material: str | None) -> str | None:
    """The mask material name written to KiCad (library 'lpi' -> 'LPI')."""
    return None if material is None else MASK_LABELS.get(material.lower(), material)


def _mask_tokens(mask: MaskSpec) -> list[str]:
    tokens = [f"(color {_q(mask.color)})"] if mask.color else []
    tokens.append(f"(thickness {kicad_mm(mask.over_substrate)})")
    label = mask_label(mask.material) if mask.source != "none" else None
    if label:
        er = mask.dk if mask.dk is not None else KICAD_MASK_DEFAULTS[0]
        df = mask.df if mask.df is not None else KICAD_MASK_DEFAULTS[1]
        tokens += [f"(material {_q(label)})", f"(epsilon_r {kicad_er(er)})",
                   f"(loss_tangent {kicad_double(df)})"]
    return tokens


def _outer(side: str, mask: MaskSpec) -> list[list[str]]:
    return [_item(*SILK[side], [[]]), _item(*PASTE[side], [[]]),
            _item(*MASK[side], [_mask_tokens(mask)])]


def ply_label(ply: Ply) -> str:
    """The material name written for a ply: its label (plus its construction
    when the label does not name it), else the construction, else
    UNNAMED_DIELECTRIC; KiCad drops Er and loss tangent of an unnamed ply."""
    construction = ply.construction if ply.construction not in (None, "core") else None
    if ply.label and construction and construction not in ply.label:
        return f"{ply.label} {construction}"
    return ply.label or construction or UNNAMED_DIELECTRIC


def _dielectric(die: Dielectric) -> list[str]:
    lock = " locked" if die.locked else ""
    plies = [[f"(thickness {kicad_mm(p.thickness)}{lock})", f"(material {_q(ply_label(p))})",
              f"(epsilon_r {kicad_er(p.er)})", f"(loss_tangent {kicad_double(p.df)})"]
             for p in die.plies]
    return _item(die.name, die.kind, plies)


def _check_emittable(rs: ResolvedStackup, dialect: int) -> None:
    if dialect not in DIALECTS:
        raise ValueError(f"only the KiCad 9 dialect is emitted (used for 9 and 10 files, ADR "
                         f"D7.5); dialect {dialect!r} is not supported in wave 2")
    if rs.legacy is not None:
        raise ValueError("the legacy {h, er, t} stackup shorthand writes no KiCad stackup block "
                         "(ADR D7.2.2 (a), D7.5 step 6)")
    if len(rs.dielectrics) != len(rs.copper) - 1 or not rs.copper:
        raise ValueError(f"a stackup alternates copper and dielectric: {len(rs.copper)} copper "
                         f"layers need {len(rs.copper) - 1} dielectrics, got "
                         f"{len(rs.dielectrics)}")


def emit(rs: ResolvedStackup, dialect: int = 9) -> str:
    """The ``(stackup ...)`` block of ``rs`` as KiCad 9/10 write it, from
    ``(stackup`` to the closing ``)`` (indented for its place in ``(setup``)."""
    _check_emittable(rs, dialect)
    items = _outer("F", rs.mask)
    for k, copper in enumerate(rs.copper):
        items.append(_item(copper.name, "copper", [[f"(thickness {kicad_mm(copper.thickness)})"]]))
        if k < len(rs.dielectrics):
            items.append(_dielectric(rs.dielectrics[k]))
    items += _outer("B", rs.mask)[::-1]
    finish = FINISH_TO_KICAD.get(rs.finish or "", rs.finish) or KICAD_NO_FINISH
    constraints = "yes" if rs.impedance_controlled else "no"
    lines = ["(stackup", *(line for item in items for line in item),
             f"{I3}(copper_finish {_q(finish)})", f"{I3}(dielectric_constraints {constraints})",
             f"{I2})"]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Scanning board text: spans of the header lists, splice, extract
# ---------------------------------------------------------------------------
def tokens(text: str) -> list[str]:
    """S-expression tokens: quoted strings (kept quoted), parentheses, atoms."""
    return _TOKEN.findall(text)


def normalize(block: str) -> str:
    """Whitespace-normalised block text (token by token)."""
    return " ".join(tokens(block))


def _spans(text: str) -> dict[str, tuple[int, int]]:
    """Spans of (kicad_pcb (general) (layers) (setup (stackup))) or of a bare
    (stackup ...); the scan stops when (setup ...) closes, so a large board's
    footprints and zones are never tokenised."""
    found: dict[str, tuple[int, int]] = {}
    stack: list[list[Any]] = []          # [start offset, head]
    expect_head = False
    for m in _TOKEN.finditer(text):
        tok = m.group()
        if tok == "(":
            stack.append([m.start(), ""])
            expect_head = True
        elif tok == ")":
            if not stack:
                raise ValueError(f"unbalanced parentheses: ')' at offset {m.start()} closes "
                                 "nothing")
            path = tuple(h for _, h in stack)
            start = stack.pop()[0]
            key = _SPANS.get(path)
            if key:
                found[key] = (start, m.end())
            if key in ("setup", "block"):
                return found
            expect_head = False
        elif expect_head:
            stack[-1][1] = _unquote(tok)
            expect_head = False
    if stack:
        raise ValueError(f"unbalanced parentheses: '(' at offset {stack[-1][0]} is never closed")
    return found


def _head(text: str) -> str:
    m = re.match(r'\s*\(\s*([^\s()"]+)', text)
    return m.group(1) if m else ""


def extract(text: str) -> str | None:
    """The ``(stackup ...)`` block inside ``(setup`` of board text, or None."""
    span = _spans(text).get("stackup")
    return text[span[0]:span[1]] if span else None


def splice(text: str, block: str) -> str:
    """Board text with ``block`` as the first child of ``(setup``: an existing
    stackup block is replaced in place, else the block is inserted right after
    ``(setup``. Idempotent. ValueError without a ``(setup`` list."""
    if _head(block) != "stackup" or "block" not in _spans(block):
        raise ValueError("splice takes a (stackup ...) block (stackup_kicad.emit)")
    spans = _spans(text)
    if "stackup" in spans:
        start, end = spans["stackup"]
        return text[:start] + block + text[end:]
    if "setup" not in spans:
        raise ValueError("the board text has no (setup ...) list: save the board with pcbnew "
                         "before splicing the stackup in")
    head = re.compile(r"\(\s*setup\b").match(text, spans["setup"][0])
    at = head.end() if head else spans["setup"][0] + len("(setup")
    return text[:at] + "\n" + I2 + block + text[at:]


# ---------------------------------------------------------------------------
# A tiny tree reader (enough for the header lists)
# ---------------------------------------------------------------------------
def _tree(text: str) -> list:
    """Nested lists of atoms (quoted strings unescaped) of every list in text."""
    stack: list[list] = [[]]
    for tok in tokens(text):
        if tok == "(":
            node: list = []
            stack[-1].append(node)
            stack.append(node)
        elif tok == ")":
            if len(stack) == 1:
                raise ValueError("unbalanced parentheses: ')' closes nothing")
            stack.pop()
        else:
            stack[-1].append(_unquote(tok))
    if len(stack) != 1:
        raise ValueError("unbalanced parentheses: a list is never closed")
    return stack[0]


def _float(value: Any, what: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{what}: not a number {value!r}") from exc


# ---------------------------------------------------------------------------
# parse_block
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class SubLayer:
    """One ply (or the single sublayer of a copper / mask / silk item)."""
    thickness: float | None = None
    locked: bool = False
    material: str | None = None
    epsilon_r: float | None = None
    loss_tangent: float | None = None
    color: str | None = None
    spec_frequency: float | None = None
    dielectric_model: str | None = None


@dataclass(frozen=True)
class BlockItem:
    """One ``(layer ...)`` entry of a stackup block."""
    name: str
    type: str
    sublayers: tuple[SubLayer, ...]

    @property
    def kind(self) -> str:
        """copper | mask | silk | paste | dielectric (any name KiCad has no layer for)."""
        if _COPPER.match(self.name):
            return "copper"
        suffix = self.name.rsplit(".", 1)[-1] if self.name[:2] in ("F.", "B.") else ""
        return {"Mask": "mask", "SilkS": "silk", "Paste": "paste"}.get(suffix, "dielectric")


@dataclass(frozen=True)
class StackupBlock:
    items: tuple[BlockItem, ...]
    copper_finish: str | None = None
    dielectric_constraints: bool = False
    other: tuple[tuple[str, ...], ...] = field(default=())   # edge_connector, castellated, ...

    def _of(self, kind: str) -> tuple[BlockItem, ...]:
        return tuple(i for i in self.items if i.kind == kind)

    def copper(self) -> tuple[BlockItem, ...]:
        return self._of("copper")

    def dielectrics(self) -> tuple[BlockItem, ...]:
        return self._of("dielectric")

    def item(self, name: str) -> BlockItem | None:
        return next((i for i in self.items if i.name == name), None)

    def thickness(self) -> float:
        """Copper + every ply + the masks (what KiCad sums for the board)."""
        nm = sum(_nm(s.thickness or 0.0) for i in self.items
                 if i.kind in ("copper", "dielectric", "mask") for s in i.sublayers)
        return nm / 1e6


_SUB_TEXT = ("material", "color", "dielectric_model")
_SUB_NUMBER = ("epsilon_r", "loss_tangent", "spec_frequency")


def _sub_values(node: list, where: str) -> dict[str, Any]:
    """The SubLayer fields one ``(key value ...)`` child sets ({} for others)."""
    key = node[0] if node else None
    if len(node) < 2:
        return {}
    if key == "thickness":
        return {"thickness": _float(node[1], f"{where} thickness"), "locked": "locked" in node[2:]}
    if key in _SUB_TEXT:
        return {key: str(node[1])}
    if key in _SUB_NUMBER:
        return {key: _float(node[1], f"{where} {key}")}
    return {}


def _block_item(node: list) -> BlockItem:
    if len(node) < 2 or isinstance(node[1], list):
        raise ValueError(f"a stackup layer entry has no name: {node!r}")
    name, type_name = str(node[1]), ""
    subs: list[SubLayer] = []
    values: dict[str, Any] = {}
    for element in node[2:]:
        if element == "addsublayer":
            subs.append(SubLayer(**values))
            values = {}
        elif isinstance(element, list) and element and element[0] == "type":
            type_name = str(element[1]) if len(element) > 1 else ""
        elif isinstance(element, list):
            values = {**values, **_sub_values(element, f"stackup layer {name!r}")}
    subs.append(SubLayer(**values))
    return BlockItem(name, type_name, tuple(subs))


def _block_text(text: str) -> str:
    if _head(text) == "stackup":
        return text
    block = extract(text)
    if block is None:
        raise ValueError("no (stackup ...) block in (setup ...): the board has no physical "
                         "stackup (KiCad Board Setup > Physical Stackup was never saved)")
    return block


def parse_block(text: str) -> StackupBlock:
    """The stackup of a ``(stackup ...)`` block, or of a board's ``(setup``."""
    root = _tree(_block_text(text))
    node = root[0] if root and isinstance(root[0], list) else []
    if not node or node[0] != "stackup":
        raise ValueError("not a (stackup ...) block")
    items: list[BlockItem] = []
    finish, constraints, other = None, False, []
    for child in node[1:]:
        if not isinstance(child, list) or not child:
            continue
        if child[0] == "layer":
            items.append(_block_item(child))
        elif child[0] == "copper_finish" and len(child) > 1:
            finish = str(child[1])
        elif child[0] == "dielectric_constraints":
            constraints = len(child) > 1 and child[1] == "yes"
        else:
            other.append(tuple(str(x) for x in child if not isinstance(x, list)))
    return StackupBlock(tuple(items), finish, constraints, tuple(other))


# ---------------------------------------------------------------------------
# Board header readers
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class BoardLayer:
    """One ``(layers ...)`` entry: (ordinal "canonical name" type ["user name"])."""
    ordinal: int
    name: str
    type: str
    user_name: str | None = None


def _span_tree(text: str, key: str) -> list | None:
    span = _spans(text).get(key)
    if span is None:
        return None
    root = _tree(text[span[0]:span[1]])
    return root[0] if root else None


def board_layers(text: str) -> tuple[BoardLayer, ...]:
    """Every entry of the board's ``(layers ...)`` list, in file order."""
    node = _span_tree(text, "layers")
    if node is None:
        raise ValueError("no (layers ...) list: not KiCad board text")
    out = []
    for entry in node[1:]:
        if isinstance(entry, list) and len(entry) >= 3:
            user = str(entry[3]) if len(entry) > 3 and not isinstance(entry[3], list) else None
            out.append(BoardLayer(int(_float(entry[0], "layer ordinal")), str(entry[1]),
                                  str(entry[2]), user))
    return tuple(out)


def _copper_rank(name: str) -> int:
    m = _COPPER.match(name)
    if m is None:
        raise ValueError(f"not a copper layer name: {name!r}")
    return 0 if name == "F.Cu" else 99 if name == "B.Cu" else int(m.group(1))


def enabled_copper(text: str) -> tuple[str, ...]:
    """The board's copper layers, top to bottom (F.Cu, In1.Cu.., B.Cu)."""
    names = [layer.name for layer in board_layers(text) if _COPPER.match(layer.name)]
    return tuple(sorted(names, key=_copper_rank))


def copper_types(text: str) -> dict[str, str]:
    """{copper layer: KiCad layer type} (signal | power | mixed | jumper)."""
    return {layer.name: layer.type for layer in board_layers(text) if _COPPER.match(layer.name)}


def general_thickness(text: str) -> float | None:
    """``(general (thickness X))`` in mm, or None."""
    node = _span_tree(text, "general")
    for child in (node or [])[1:]:
        if isinstance(child, list) and len(child) > 1 and child[0] == "thickness":
            return _float(child[1], "general thickness")
    return None


def _is_board(text: str) -> bool:
    return _head(text) == "kicad_pcb"


# ---------------------------------------------------------------------------
# compare (ADR D7.5 step 5, the text half; swig_stackup adds the zone layers)
# ---------------------------------------------------------------------------
def _first_difference(saved: list[str], emitted: list[str]) -> str:
    k = next((i for i, (a, b) in enumerate(zip(saved, emitted, strict=False)) if a != b),
             min(len(saved), len(emitted)))
    lo = max(0, k - DIFF_CONTEXT)
    hi = k + DIFF_CONTEXT
    return (f"the saved stackup block differs from the emitted one at token {k}: saved "
            f"'{' '.join(saved[lo:hi])}' vs emitted '{' '.join(emitted[lo:hi])}'")


def _type_problems(rs: ResolvedStackup, text: str) -> list[str]:
    types = copper_types(text)
    return [f"{c.name} is a {types[c.name]} layer on the board, but its stackup role {c.role} "
            f"needs the KiCad type {c.kicad_type}"
            for c in rs.copper if c.name in types and types[c.name] != c.kicad_type]


def compare(rs: ResolvedStackup, text: str) -> list[str]:
    """Why saved board text does not carry ``rs`` ([] when it does): the enabled
    copper, each copper layer type, ``(general (thickness))`` within 0.001 mm of
    the computed sum, and (except for the legacy shorthand) the saved block equal
    to ``emit(rs)`` token for token."""
    problems = []
    enabled, want = enabled_copper(text), rs.copper_names()
    if enabled != tuple(want):
        problems.append(f"the board enables copper {list(enabled)}, the stackup has {list(want)}")
    problems += _type_problems(rs, text)
    thickness = general_thickness(text)
    if thickness is None or abs(thickness - rs.board_thickness_computed) > THICKNESS_TOL_MM:
        problems.append(f"the board thickness is {thickness} mm, the stackup sums to "
                        f"{rs.board_thickness_computed} mm (copper + plies + 2 masks)")
    if rs.legacy is None:
        saved = extract(text)
        if saved is None:
            problems.append("no (stackup ...) block in the saved board's (setup ...)")
        elif normalize(saved) != normalize(emit(rs)):
            problems.append(_first_difference(tokens(saved), tokens(emit(rs))))
    return problems


# ---------------------------------------------------------------------------
# netlist_stackup_from_board (D7.3, F4 import)
# ---------------------------------------------------------------------------
def _ply_spec(sub: SubLayer, where: str) -> dict[str, Any]:
    if sub.thickness is None:
        raise ValueError(f"{where} has no thickness")
    name, er, df = KICAD_DEFAULT_DIELECTRIC
    if sub.material is not None:
        name = sub.material
        er = sub.epsilon_r if sub.epsilon_r is not None else er
        df = sub.loss_tangent if sub.loss_tangent is not None else df
    spec: dict[str, Any] = {"thickness": sub.thickness, "material": name, "dk": er, "df": df}
    if sub.spec_frequency:
        spec["dk_freq_hz"] = sub.spec_frequency
    if sub.dielectric_model and sub.dielectric_model != "constant":
        spec["dielectric_model"] = sub.dielectric_model
    return spec


def _dielectric_entry(item: BlockItem) -> dict[str, Any]:
    kind = "prepreg" if item.type.strip().lower() == "prepreg" else "core"
    plies = [_ply_spec(s, f"{item.name} ply {k + 1}") for k, s in enumerate(item.sublayers)]
    entry: dict[str, Any] = {"type": "dielectric", "kind": kind}
    entry.update(plies[0] if len(plies) == 1 else {"plies": plies})
    if any(s.locked for s in item.sublayers):
        entry["locked"] = True
    return entry


def _copper_entry(item: BlockItem, kicad_type: str | None) -> dict[str, Any]:
    thickness = item.sublayers[0].thickness
    entry: dict[str, Any] = {"type": "copper", "name": item.name,
                             "thickness": KICAD_DEFAULT_COPPER_MM if thickness is None
                             else thickness}
    if kicad_type is not None:
        entry["role"] = ROLE_OF_TYPE.get(kicad_type, "signal")
    return entry


def _mask_entry(block: StackupBlock) -> dict[str, Any] | None:
    item = block.item("F.Mask") or block.item("B.Mask")
    sub = item.sublayers[0] if item else None
    if sub is None or not sub.thickness or sub.thickness <= 0:
        return None
    mask: dict[str, Any] = {"over_substrate": sub.thickness}
    if sub.material is not None:
        mask.update(material=sub.material,
                    dk=sub.epsilon_r if sub.epsilon_r is not None else KICAD_MASK_DEFAULTS[0],
                    df=sub.loss_tangent if sub.loss_tangent is not None
                    else KICAD_MASK_DEFAULTS[1])
    if sub.color is not None:
        mask["color"] = sub.color
    return mask


def _physical(block: StackupBlock) -> list[BlockItem]:
    """Copper and dielectrics, checked to alternate copper first and last with
    positional copper names."""
    seq = [i for i in block.items if i.kind in ("copper", "dielectric")]
    kinds = [i.kind for i in seq]
    alternates = (bool(seq) and kinds[0] == kinds[-1] == "copper"
                  and all(a != b for a, b in zip(kinds, kinds[1:], strict=False)))
    if not alternates:
        raise ValueError(f"the stackup block does not alternate copper and dielectric, copper "
                         f"first and last: {[i.name for i in seq]}")
    names = [i.name for i in seq if i.kind == "copper"]
    want = ["F.Cu", *(f"In{k}.Cu" for k in range(1, len(names) - 1)), "B.Cu"]
    if names != want:
        raise ValueError(f"stackup copper must be {want} top to bottom, got {names}")
    return seq


def _board_info(text: str, copper: Sequence[str]) -> tuple[dict[str, str], float | None]:
    if not _is_board(text):
        return {}, None
    enabled = enabled_copper(text)
    if tuple(copper) != enabled:
        raise ValueError(f"the stackup lists copper {list(copper)} but the board enables "
                         f"{list(enabled)}: KiCad does not keep them in sync; fix Board Setup > "
                         "Physical Stackup")
    return copper_types(text), general_thickness(text)


def netlist_stackup_from_board(text: str) -> dict[str, Any]:
    """A KiCad stackup (bare block or board text) as a D7.2.2 ``stackup`` object
    in the ``layers`` form: copper thickness (and role from the board's layer
    type), dielectrics with material, thickness, Dk/Df (KiCad's FR4 4.5 / 0.02
    for a ply without material), ``locked``, KiCad 11 ``spec_frequency`` /
    ``dielectric_model`` as ``dk_freq_hz`` / ``dielectric_model``; ``finish``
    (a KiCad finish outside the netlist list is left out), ``mask`` (None when
    its thickness is 0), ``impedance_control`` and the board's
    ``board_thickness``; origin ``designer``, rationale ``imported from KiCad
    board``. Always new objects. ValueError when there is no block, it does not
    alternate, or its copper disagrees with the board's enabled copper."""
    block = parse_block(text)
    seq = _physical(block)
    types, thickness = _board_info(text, [i.name for i in seq if i.kind == "copper"])
    layers = [_copper_entry(i, types.get(i.name) if types else None) if i.kind == "copper"
              else _dielectric_entry(i) for i in seq]
    out: dict[str, Any] = {"layers": layers}
    finish = KICAD_TO_FINISH.get(block.copper_finish or "")
    if finish:
        out["finish"] = finish
    out["mask"] = _mask_entry(block)
    if block.dielectric_constraints:
        out["impedance_control"] = {"required": True}
    if thickness:
        out["board_thickness"] = thickness
    out["origin"] = "designer"
    out["rationale"] = IMPORT_RATIONALE
    return out
