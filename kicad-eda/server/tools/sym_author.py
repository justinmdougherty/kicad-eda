#!/usr/bin/env python3
"""Author KiCad schematic symbols (.kicad_sym) from a pin table (F2, ROADMAP D1).

A SymbolSpec (pins: number, name, electrical type, optional side and layout
group; properties: Reference prefix, Value, Footprint, Datasheet, Description,
ki_keywords, ki_fp_filters) becomes one single-unit symbol in the D0 dialect
(``(version 20241209)``, the KiCad 9.0 format the SWIG/kicad-cli 9 toolchain
reads), written into a project library: added, or replaced in place when a
symbol of that name exists. Other symbols in the library are kept as data.

Layout (all connection points on the 2.54 mm grid, pin length 2.54 mm):
  * power pins on top, ground pins on the bottom, inputs / passives / NC on
    the left, outputs / bidirectional / open-drain on the right — unless a pin
    names its side explicitly;
  * per side, pins of one layout ``group`` sit on adjacent rows in spec
    order, with one empty row between groups;
  * the body is sized so the estimated pin-name text never overlaps
    (CHAR_W is calibrated against kicad-cli 9 SVG output).

Stacking (D1): power pins (power_in / power_out) that share a NAME are one
"power group"; every pin of it after the first (spec order) is placed on the
first pin's connection point, hidden, and typed ``passive`` — KLC S4.3 and
KiCad 9's own stock symbols (e.g. RF_GPS:L70-R GND pins 1/10/12). A hidden
``power_in`` pin would also act as an implicit global net named after the pin,
silently merging e.g. GND into a differently named ground net; ``passive``
avoids that. Stacked pins are connected by KiCad's connectivity because they
share a point (verified with kicad-cli 9 ERC + netlist export in the tests).

``pin_groups`` (every pin name carried by more than one pin -> its numbers)
lets a netlist name the LG290P's 59 GND pads, or its 4 RESERVED pads for the
no_connect list, without typing them by hand.

The first target is the Quectel LG290P(03) (lg290p_spec), from the "LG290P(03)
Hardware Design" V1.0 (2024-08-21), §2 Fig. 3 "Pin Assignment" and Table 6.

Usage:
  <venv-python> sym_author.py <spec.json> <library.kicad_sym>
  <venv-python> sym_author.py --lg290p <library.kicad_sym> [footprint_lib_id]
The LAST stdout line is the JSON result (author_symbol's summary, or
{"ok": false, "error": ...}).
"""
from __future__ import annotations

import json
import math
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Iterable
from typing import Any

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import sexpdata  # noqa: E402

import build_pin_sidecars  # noqa: E402
import symlib  # noqa: E402

S = sexpdata.Symbol

SYMBOL_LIB_VERSION = 20241209       # D0: KiCad 9.0 .kicad_sym dialect
GENERATOR = "kicad-eda"
GENERATOR_VERSION = "9.0"
GRID = 2.54                         # pin connection grid (100 mil)
PIN_LENGTH = 2.54
FONT = 1.27                         # pin name / number / field text size
NAME_OFFSET = 1.016                 # (pin_names (offset ...)): name inset from the body edge
# Estimated advance per character at FONT. kicad-cli 9 `sym export svg` measures
# 1.15 mm/char for "RTK_STAT/ANT_ON" and 1.42 mm/char for "WWWWWWWWWW" at 1.27 mm;
# 1.27 is conservative for the upper-case / digit / underscore names pins carry.
CHAR_W = 1.27
TEXT_GAP = 1.27                     # clear space between neighbouring texts
BODY_STROKE = 0.254
KICAD_CLI_TIMEOUT_S = 300
_EPS = 1e-6
_MAX_GROW_STEPS = 2000              # body growth guard (2000 x 2.54 mm: never reached)

ELECTRICAL_TYPES = ("input", "output", "bidirectional", "tri_state", "passive", "free",
                    "unspecified", "power_in", "power_out", "open_collector",
                    "open_emitter", "no_connect")
SIDES = ("left", "right", "top", "bottom")
POWER_TYPES = frozenset({"power_in", "power_out"})
_GROUND_TYPES = POWER_TYPES | {"passive"}
_RIGHT_TYPES = frozenset({"output", "bidirectional", "tri_state", "open_collector",
                          "open_emitter"})
_GROUND_RE = re.compile(r"^([ADPSCER]?GND|V(SS|EE))", re.IGNORECASE)
_SYMBOL_NAME_RE = re.compile(r"^[A-Za-z0-9_.+\-]+$")
_REFERENCE_RE = re.compile(r"^#?[A-Za-z]+$")
_OVERBAR_RE = re.compile(r"[~_^]\{([^}]*)\}")
_ANON_PIN_NAMES = ("~",)
_CLI_ERROR_RE = re.compile(r"^\s*error\b|\berror:", re.IGNORECASE)   # D0: "Error:" lines


class SymbolAuthorError(Exception):
    """A spec is invalid, or a library cannot be read, extended or written."""


@dataclass(frozen=True)
class PinSpec:
    """One symbol pin = one footprint pad (pad number == pin number).

    ``side`` is "left" | "right" | "top" | "bottom", or "" for the type
    default; ``group`` keeps pins together on their side (spec order)."""
    number: str
    name: str
    etype: str = "passive"
    side: str = ""
    group: str = ""


@dataclass(frozen=True)
class SymbolSpec:
    name: str
    pins: tuple[PinSpec, ...]
    reference: str = "U"
    value: str = ""                 # "" -> the symbol name
    footprint: str = ""
    datasheet: str = ""
    description: str = ""
    keywords: str = ""
    fp_filters: str = ""
    stack_power: bool = True


@dataclass(frozen=True)
class PlacedPin:
    """A pin in symbol space (+Y up); (x, y) is the connection point and
    ``rot`` the direction from it toward the body (KiCad's pin angle)."""
    number: str
    name: str
    etype: str
    side: str
    x: float
    y: float
    rot: int
    hidden: bool


@dataclass(frozen=True)
class SymbolLayout:
    half_w: float                   # body rectangle = (-half_w, half_h) .. (half_w, -half_h)
    half_h: float
    pins: tuple[PlacedPin, ...]     # spec order
    stacks: dict[str, tuple[str, ...]]   # power group name -> numbers, first = visible


# ----------------------------------------------------------------------------
# Spec input and validation
# ----------------------------------------------------------------------------
_SPEC_STR_KEYS = ("name", "reference", "value", "footprint", "datasheet", "description",
                  "keywords", "fp_filters")
_SPEC_KEYS = frozenset((*_SPEC_STR_KEYS, "pins", "stack_power"))
_PIN_KEYS = frozenset(("number", "name", "type", "side", "group"))


def spec_from_dict(data: Any) -> SymbolSpec:
    """A SymbolSpec from JSON-shaped input (the MCP boundary):
    {"name", "pins": [{"number", "name", "type", "side"?, "group"?}, ...],
     "reference"?, "value"?, "footprint"?, "datasheet"?, "description"?,
     "keywords"?, "fp_filters"?, "stack_power"?}. Integer pin numbers are
    accepted; anything else malformed raises SymbolAuthorError naming every
    problem. Semantic checks (types, sides, duplicates) run in layout()."""
    if not isinstance(data, dict):
        raise SymbolAuthorError("symbol spec must be an object")
    problems: list[str] = []
    unknown = sorted(set(data) - _SPEC_KEYS)
    if unknown:
        problems.append(f"unknown symbol key(s): {', '.join(unknown)}")
    if "name" not in data:
        problems.append("'name' is required")
    for key in _SPEC_STR_KEYS:
        if key in data and not isinstance(data[key], str):
            problems.append(f"'{key}' must be a string")
    stack_power = data.get("stack_power", True)
    if not isinstance(stack_power, bool):
        problems.append("'stack_power' must be a boolean")
    raw_pins = data.get("pins", [])
    if not isinstance(raw_pins, list):
        problems.append("'pins' must be a list")
        raw_pins = []
    pins = tuple(p for p in (_pin_from_dict(i, raw, problems)
                             for i, raw in enumerate(raw_pins, 1)) if p is not None)
    if problems:
        raise SymbolAuthorError("invalid symbol spec: " + "; ".join(problems))
    fields = {k: data[k] for k in _SPEC_STR_KEYS if k in data}
    return SymbolSpec(pins=pins, stack_power=stack_power, **fields)


def _pin_from_dict(index: int, raw: Any, problems: list[str]) -> PinSpec | None:
    if not isinstance(raw, dict):
        problems.append(f"pin #{index} must be an object")
        return None
    start = len(problems)
    unknown = sorted(set(raw) - _PIN_KEYS)
    if unknown:
        problems.append(f"pin #{index}: unknown pin key(s): {', '.join(unknown)}")
    for key in ("number", "name"):
        if key not in raw:
            problems.append(f"pin #{index}: '{key}' is required")
    number = raw.get("number", "")
    if isinstance(number, int) and not isinstance(number, bool):
        number = str(number)
    values = {"number": number, "name": raw.get("name", ""),
              "etype": raw.get("type", "passive"), "side": raw.get("side", ""),
              "group": raw.get("group", "")}
    for key, value in values.items():
        if not isinstance(value, str):
            label = "type" if key == "etype" else key
            problems.append(f"pin #{index}: '{label}' must be a string")
    return PinSpec(**values) if len(problems) == start else None


def spec_problems(spec: SymbolSpec) -> list[str]:
    """Every reason ``spec`` cannot be authored (empty when it can)."""
    problems: list[str] = []
    if not _SYMBOL_NAME_RE.match(spec.name or ""):
        problems.append(f"symbol name {spec.name!r} must match [A-Za-z0-9_.+-]+")
    if not _REFERENCE_RE.match(spec.reference or ""):
        problems.append(f"reference prefix {spec.reference!r} must be letters only (e.g. 'U')")
    if not spec.pins:
        problems.append("a symbol needs at least one pin")
    seen: set[str] = set()
    for i, pin in enumerate(spec.pins, 1):
        label = f"pin #{i} ({pin.number!r})"
        if not pin.number or any(ch.isspace() for ch in pin.number):
            problems.append(f"{label}: pin number {pin.number!r} must be non-empty "
                            "without whitespace")
        elif pin.number in seen:
            problems.append(f"duplicate pin number {pin.number!r}")
        seen.add(pin.number)
        if not pin.name:
            problems.append(f"{label}: pin name must be non-empty (use '~' for an "
                            "unnamed pin)")
        if pin.etype not in ELECTRICAL_TYPES:
            problems.append(f"{label}: electrical type {pin.etype!r} is not one of "
                            f"{', '.join(ELECTRICAL_TYPES)}")
        if pin.side and pin.side not in SIDES:
            problems.append(f"{label}: side {pin.side!r} is not one of {', '.join(SIDES)}")
    return problems


def _check(spec: SymbolSpec) -> None:
    problems = spec_problems(spec)
    if problems:
        raise SymbolAuthorError(f"invalid symbol spec {spec.name!r}: " + "; ".join(problems))


# ----------------------------------------------------------------------------
# Groups and stacks
# ----------------------------------------------------------------------------
def pin_groups(spec: SymbolSpec) -> dict[str, list[str]]:
    """{pin name: [numbers]} for every name carried by more than one pin, in
    spec order — one net's pads (GND) or a set to list together (RESERVED ->
    no_connect). Anonymous ("~") pins are left out."""
    by_name: dict[str, list[str]] = {}
    for pin in spec.pins:
        by_name.setdefault(pin.name, []).append(pin.number)
    return {name: nums for name, nums in by_name.items()
            if len(nums) > 1 and name not in _ANON_PIN_NAMES}


def power_stacks(spec: SymbolSpec) -> dict[str, tuple[str, ...]]:
    """{name: (numbers...)} for the power groups that get stacked: power_in /
    power_out pins sharing a name, first (visible) pin first. Empty when the
    spec turns stacking off."""
    if not spec.stack_power:
        return {}
    by_name: dict[str, list[str]] = {}
    for pin in spec.pins:
        if pin.etype in POWER_TYPES and pin.name not in _ANON_PIN_NAMES:
            by_name.setdefault(pin.name, []).append(pin.number)
    return {name: tuple(nums) for name, nums in by_name.items() if len(nums) > 1}


# ----------------------------------------------------------------------------
# Layout
# ----------------------------------------------------------------------------
def text_length(name: str) -> float:
    """Estimated rendered length (mm) of a pin name at FONT: overbar markup
    ``~{RESET}`` counts its visible characters, an anonymous "~" name is 0."""
    if name in _ANON_PIN_NAMES:
        return 0.0
    return len(_OVERBAR_RE.sub(r"\1", name)) * CHAR_W


def _r(v: float) -> float:
    return round(v, 4) + 0.0        # + 0.0 turns -0.0 into 0.0


def _ceil_grid(v: float) -> float:
    return _r(GRID * math.ceil(v / GRID - _EPS))


def _side_of(pin: PinSpec) -> str:
    """Explicit side, else: ground-named power/passive pins (GND, AGND, VSS,
    an exposed-pad GND) bottom, other power top, drivers right, the rest left."""
    if pin.side:
        return pin.side
    if pin.etype in _GROUND_TYPES and _GROUND_RE.match(pin.name):
        return "bottom"
    if pin.etype in POWER_TYPES:
        return "top"
    return "right" if pin.etype in _RIGHT_TYPES else "left"


def _slots(pins: list[PinSpec]) -> tuple[PinSpec | None, ...]:
    """A side's rows (or columns): pins grouped by layout group in order of
    first appearance, one empty slot (None) between neighbouring groups."""
    order: list[str] = []
    members: dict[str, list[PinSpec]] = {}
    for pin in pins:
        if pin.group not in members:
            order.append(pin.group)
            members[pin.group] = []
        members[pin.group].append(pin)
    out: list[PinSpec | None] = []
    for i, group in enumerate(order):
        out.extend(([None] if i else []) + members[group])
    return tuple(out)


def _reach(slots: tuple[PinSpec | None, ...]) -> float:
    """How far a side's pin names reach into the body from its edge (0 when
    the side has no pins)."""
    names = [text_length(p.name) for p in slots if p is not None]
    return NAME_OFFSET + max(names) if names else 0.0


def _row_start(n: int, lo: float, hi: float) -> float | None:
    """Grid x of the first of ``n`` columns placed inside [lo, hi], as close
    to centred as the grid allows; None when they do not fit."""
    span = (n - 1) * GRID
    first, last = math.ceil(lo / GRID - _EPS), math.floor((hi - span) / GRID + _EPS)
    if first > last:
        return None
    ideal = ((lo + hi) / 2 - span / 2) / GRID
    return _r(GRID * min(range(first, last + 1), key=lambda k: (abs(k - ideal), k)))


def _fit_width(sides: dict[str, tuple]) -> tuple[float, dict[str, float]]:
    """(half body width, {"top"/"bottom": x of the first column}). Top and
    bottom pins (and their rotated names, FONT wide) stay clear of the left and
    right pin names by TEXT_GAP; left and right names never meet."""
    left, right = _reach(sides["left"]), _reach(sides["right"])
    n_tb = max(len(sides["top"]), len(sides["bottom"]))
    w2 = _ceil_grid(max(GRID * (n_tb + 1) / 2, GRID))
    for _ in range(_MAX_GROW_STEPS):
        lo = max(-w2 + GRID, -w2 + left + TEXT_GAP + FONT / 2)
        hi = min(w2 - GRID, w2 - right - TEXT_GAP - FONT / 2)
        starts = {s: _row_start(len(sides[s]), lo, hi) for s in ("top", "bottom") if sides[s]}
        names_clear = -w2 + left + TEXT_GAP <= w2 - right + _EPS
        if names_clear and all(x is not None for x in starts.values()):
            return w2, {s: float(x) for s, x in starts.items()}
        w2 = _r(w2 + GRID)
    raise SymbolAuthorError("could not fit the symbol body (pin names too long?)")


def _fit_height(sides: dict[str, tuple]) -> float:
    """Half body height: room for the longest left/right column plus one grid
    row at each end, and for the top and bottom names not to meet."""
    n_lr = max(len(sides["left"]), len(sides["right"]))
    vertical = sum(_reach(sides[s]) for s in ("top", "bottom")) + TEXT_GAP
    return _ceil_grid(max(GRID * (n_lr + 1) / 2, vertical / 2, GRID))


def _position(side: str, k: int, w2: float, h2: float,
              starts: dict[str, float]) -> tuple[float, float, int]:
    if side == "left":
        return _r(-(w2 + PIN_LENGTH)), _r(h2 - GRID * (k + 1)), 0
    if side == "right":
        return _r(w2 + PIN_LENGTH), _r(h2 - GRID * (k + 1)), 180
    if side == "top":
        return _r(starts["top"] + GRID * k), _r(h2 + PIN_LENGTH), 270
    return _r(starts["bottom"] + GRID * k), _r(-(h2 + PIN_LENGTH)), 90


def layout(spec: SymbolSpec) -> SymbolLayout:
    """Place every pin of a validated spec (raises SymbolAuthorError when the
    spec is invalid). Stacked power pins share their group's first pin's
    connection point, hidden and typed passive."""
    _check(spec)
    stacks = power_stacks(spec)
    hidden = {n for nums in stacks.values() for n in nums[1:]}
    visible = [p for p in spec.pins if p.number not in hidden]
    sides = {s: _slots([p for p in visible if _side_of(p) == s]) for s in SIDES}
    w2, starts = _fit_width(sides)
    h2 = _fit_height(sides)
    placed: dict[str, PlacedPin] = {}
    for side in SIDES:
        for k, pin in enumerate(sides[side]):
            if pin is not None:
                x, y, rot = _position(side, k, w2, h2, starts)
                placed[pin.number] = PlacedPin(pin.number, pin.name, pin.etype, side,
                                               x, y, rot, False)
    by_number = {p.number: p for p in spec.pins}
    for nums in stacks.values():
        anchor = placed[nums[0]]
        for n in nums[1:]:
            placed[n] = PlacedPin(n, by_number[n].name, "passive", anchor.side,
                                  anchor.x, anchor.y, anchor.rot, True)
    return SymbolLayout(w2, h2, tuple(placed[p.number] for p in spec.pins), stacks)


# ----------------------------------------------------------------------------
# S-expression construction and KiCad-style printing
# ----------------------------------------------------------------------------
def _effects(justify: str = "", hide: bool = False) -> list:
    node: list = [S("effects"), [S("font"), [S("size"), FONT, FONT]]]
    if justify:
        node.append([S("justify"), S(justify)])
    if hide:
        node.append([S("hide"), S("yes")])
    return node


def _prop(name: str, value: str, x: float, y: float, justify: str = "",
          hide: bool = False) -> list:
    return [S("property"), name, value, [S("at"), _r(x), _r(y), 0], _effects(justify, hide)]


def _pin_node(pin: PlacedPin) -> list:
    node: list = [S("pin"), S(pin.etype), S("line"), [S("at"), pin.x, pin.y, pin.rot],
                  [S("length"), PIN_LENGTH]]
    if pin.hidden:
        node.append([S("hide"), S("yes")])
    return node + [[S("name"), pin.name, _effects()], [S("number"), pin.number, _effects()]]


def symbol_node(spec: SymbolSpec, lay: SymbolLayout | None = None) -> list:
    """The ``(symbol ...)`` node for ``spec`` (laid out here unless given)."""
    lay = lay or layout(spec)
    w2, h2 = lay.half_w, lay.half_h
    below = h2 + PIN_LENGTH
    node: list = [
        S("symbol"), spec.name,
        [S("pin_names"), [S("offset"), NAME_OFFSET]],
        [S("exclude_from_sim"), S("no")], [S("in_bom"), S("yes")], [S("on_board"), S("yes")],
        _prop("Reference", spec.reference, -w2, h2 + FONT, justify="left"),
        _prop("Value", spec.value or spec.name, w2, h2 + FONT, justify="right"),
        _prop("Footprint", spec.footprint, 0, -(below + 2 * GRID), hide=True),
        _prop("Datasheet", spec.datasheet, 0, -(below + 3 * GRID), hide=True),
        _prop("Description", spec.description, 0, 0, hide=True),
    ]
    if spec.keywords:
        node.append(_prop("ki_keywords", spec.keywords, 0, 0, hide=True))
    if spec.fp_filters:
        node.append(_prop("ki_fp_filters", spec.fp_filters, 0, 0, hide=True))
    body = [S("rectangle"), [S("start"), -w2, h2], [S("end"), w2, -h2],
            [S("stroke"), [S("width"), BODY_STROKE], [S("type"), S("default")]],
            [S("fill"), [S("type"), S("background")]]]
    node.append([S("symbol"), f"{spec.name}_0_1", body])
    node.append([S("symbol"), f"{spec.name}_1_1", *[_pin_node(p) for p in lay.pins]])
    node.append([S("embedded_fonts"), S("no")])
    return node


def fmt_number(v: float) -> str:
    """KiCad number text: no exponent, no trailing zeros, no "-0"."""
    if isinstance(v, bool):
        raise SymbolAuthorError(f"cannot write boolean {v!r} as a number")
    if isinstance(v, int):
        return str(v)
    text = f"{v:.6f}".rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text


def _atom(v: Any) -> str:
    if isinstance(v, S):
        return v.value()
    if isinstance(v, str):
        return sexpdata.dumps(v)          # quoted; escapes ", \\ and control chars
    if isinstance(v, (int, float)):
        return fmt_number(v)
    raise SymbolAuthorError(f"cannot write {type(v).__name__} {v!r} into a symbol library")


def _render(node: list, depth: int, out: list[str]) -> None:
    """KiCad 9 layout: a list holding only atoms stays on one line; a list
    with list children puts each child on its own line, one tab deeper."""
    pad = "\t" * depth
    if not node:
        out.append(pad + "()")
        return
    if not any(isinstance(c, list) for c in node):
        out.append(pad + "(" + " ".join(_atom(c) for c in node) + ")")
        return
    head = 0
    while head < len(node) and not isinstance(node[head], list):
        head += 1
    out.append(pad + "(" + " ".join(_atom(c) for c in node[:head]))
    for child in node[head:]:
        if isinstance(child, list):
            _render(child, depth + 1, out)
        else:
            out.append(pad + "\t" + _atom(child))
    out.append(pad + ")")


def _default_header() -> list[list]:
    return [[S("version"), SYMBOL_LIB_VERSION], [S("generator"), GENERATOR],
            [S("generator_version"), GENERATOR_VERSION]]


def library_text(symbols: Iterable[list], header: list[list] | None = None) -> str:
    """A complete .kicad_sym file holding ``symbols`` (sexp nodes)."""
    root = [S("kicad_symbol_lib"), *(header if header is not None else _default_header()),
            *symbols]
    lines: list[str] = []
    _render(root, 0, lines)
    return "\n".join(lines) + "\n"


# ----------------------------------------------------------------------------
# Library files: read, add-or-replace, write
# ----------------------------------------------------------------------------
def _read_library(path: Path) -> tuple[list[list], list[list]]:
    """(header nodes, symbol nodes) of an existing D0-dialect library."""
    if not path.is_file():
        raise SymbolAuthorError(f"{path} exists but is not a file")
    try:
        root = sexpdata.loads(path.read_text(encoding="utf-8"), nil=None, true=None)
    except Exception as e:  # sexpdata raises assorted parse errors
        raise SymbolAuthorError(f"cannot parse {path}: {e}") from e
    if not symlib.is_(root, "kicad_symbol_lib"):
        raise SymbolAuthorError(f"{path} is not a KiCad symbol library")
    version_node = symlib.first(root, "version")
    version = version_node[1] if version_node is not None and len(version_node) > 1 else None
    if version != SYMBOL_LIB_VERSION:
        raise SymbolAuthorError(
            f"{path} is symbol-library format {version}; only the KiCad 9 dialect "
            f"({SYMBOL_LIB_VERSION}) is extended in place -- convert it with KiCad 9's "
            "`kicad-cli sym upgrade --force` or author into a new library")
    symbols = symlib.kids(root, "symbol")
    if any(len(s) < 2 or not isinstance(s[1], str) for s in symbols):
        raise SymbolAuthorError(f"{path} has a (symbol ...) entry without a name")
    header = [c for c in root[1:] if not symlib.is_(c, "symbol")]
    return header, symbols


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(text, encoding="utf-8", newline="\n")
        os.replace(tmp, path)
    except OSError as e:
        tmp.unlink(missing_ok=True)
        raise SymbolAuthorError(f"cannot write {path}: {e}") from e


def author_symbol(spec: SymbolSpec | dict, lib_path: str | Path) -> dict:
    """Write ``spec``'s symbol into ``lib_path`` (created when missing; a
    symbol of the same name is replaced in place, every other symbol is kept)
    and return the summary: library, nickname (= file stem), lib_id, replaced,
    pins, visible_pins, stacked {name: {visible, hidden}}, pin_groups,
    body_mm [w, h], format_version. Raises SymbolAuthorError on an invalid
    spec or a library that cannot be read or written."""
    spec = spec if isinstance(spec, SymbolSpec) else spec_from_dict(spec)
    path = Path(lib_path)
    if path.suffix != ".kicad_sym":
        raise SymbolAuthorError(f"{path}: a symbol library file must end in .kicad_sym")
    lay = layout(spec)
    node = symbol_node(spec, lay)
    header, symbols = _read_library(path) if path.exists() else (_default_header(), [])
    replaced = any(s[1] == spec.name for s in symbols)
    merged = [node if s[1] == spec.name else s for s in symbols] if replaced else [*symbols, node]
    _atomic_write(path, library_text(merged, header))
    return {
        "ok": True,
        "library": str(path),
        "nickname": path.stem,
        "symbol": spec.name,
        "lib_id": f"{path.stem}:{spec.name}",
        "replaced": replaced,
        "pins": len(lay.pins),
        "visible_pins": sum(1 for p in lay.pins if not p.hidden),
        "stacked": {name: {"visible": nums[0], "hidden": list(nums[1:])}
                    for name, nums in lay.stacks.items()},
        "pin_groups": pin_groups(spec),
        "body_mm": [_r(2 * lay.half_w), _r(2 * lay.half_h)],
        "format_version": SYMBOL_LIB_VERSION,
    }


# ----------------------------------------------------------------------------
# Validation by loading, and the pin sidecar
# ----------------------------------------------------------------------------
def _hidden_pins(flat: list) -> int:
    return sum(1 for sub in symlib.kids(flat, "symbol") for pin in symlib.kids(sub, "pin")
               if (symlib.first(pin, "hide") or [None, None])[1] == S("yes"))


def _pin_counts(lib_dir: Path, stem: str) -> tuple[dict[str, int], dict[str, int]]:
    """({symbol: pin count}, {symbol: hidden pin count}) via symlib.flatten,
    the loader the file schematic backend, lint and sidecars share."""
    lib = symlib.load_lib(lib_dir, stem)
    cache = {stem: lib}
    flats = {name: symlib.flatten(cache, lib_dir, stem, name) for name in lib}
    return ({n: len(symlib.pin_numbers(f)) for n, f in flats.items()},
            {n: _hidden_pins(f) for n, f in flats.items()})


def _kicad_resave(path: Path, kicad_cli: str,
                  timeout: float) -> tuple[dict[str, int], dict[str, int], list[str]]:
    """Let KiCad parse ``path``: `kicad-cli sym upgrade --force` into a temp
    dir, then read KiCad's re-saved copy back. rc 0 alone is not proof (D0):
    an "error" console line or a missing output file also fails."""
    with tempfile.TemporaryDirectory(prefix="sym_author_") as tmp:
        out = Path(tmp) / path.name
        try:
            r = subprocess.run([kicad_cli, "sym", "upgrade", "--force", "-o", str(out),
                                str(path)], stdin=subprocess.DEVNULL, capture_output=True,
                               text=True, timeout=timeout, check=False)
        except (OSError, subprocess.TimeoutExpired) as e:
            return {}, {}, [f"kicad-cli sym upgrade did not run: {e}"]
        console = (r.stdout or "") + (r.stderr or "")
        flagged = any(_CLI_ERROR_RE.search(ln) for ln in console.splitlines())
        if r.returncode != 0 or flagged or not out.is_file():
            reason = (f"kicad-cli sym upgrade rejected {path.name} "
                      f"(rc={r.returncode}): {console.strip()[-600:]}")
            return {}, {}, [reason]
        try:
            pins, hidden = _pin_counts(Path(tmp), out.stem)
        except (symlib.SymbolLibError, IndexError, TypeError, ValueError) as e:
            return {}, {}, [f"kicad-cli re-saved copy unreadable: {e}"]
        return pins, hidden, []


def validate_library(lib_path: str | Path, kicad_cli: str | None,
                     timeout: float = KICAD_CLI_TIMEOUT_S) -> dict:
    """Validate a written library by loading it (D1): symlib.flatten of every
    symbol, then KiCad itself (`sym upgrade` into a temp dir; pass the OLDEST
    supported kicad-cli, KiCad 9, so the D0 dialect is what is proven), and
    the re-saved copy must keep every pin and hidden pin. A missing kicad-cli
    is a failure, never a silent pass. Returns {ok, errors, library,
    kicad_cli, symlib_pins, symlib_hidden_pins, resaved_pins,
    resaved_hidden_pins}."""
    path = Path(lib_path)
    errors: list[str] = []
    try:
        pins, hidden = _pin_counts(path.parent, path.stem)
    except (symlib.SymbolLibError, IndexError, TypeError, ValueError) as e:
        pins, hidden = {}, {}
        errors.append(f"symlib: {e}")
    resaved, resaved_hidden = {}, {}
    if not kicad_cli:
        errors.append("kicad-cli not available: KiCad did not parse the library "
                      "(install KiCad 9 or set KICAD_PATH)")
    else:
        resaved, resaved_hidden, cli_errors = _kicad_resave(path, kicad_cli, timeout)
        errors.extend(cli_errors)
        if not cli_errors and (resaved, resaved_hidden) != (pins, hidden):
            errors.append(f"kicad-cli re-saved a different pin set: pins {resaved} vs "
                          f"{pins}, hidden {resaved_hidden} vs {hidden}")
    return {"ok": not errors, "errors": errors, "library": str(path), "kicad_cli": kicad_cli,
            "symlib_pins": pins, "symlib_hidden_pins": hidden,
            "resaved_pins": resaved, "resaved_hidden_pins": resaved_hidden}


def write_pin_sidecar(lib_path: str | Path, symbol: str, pins_dir: str | Path,
                      nickname: str | None = None) -> dict:
    """Write ``<pins_dir>/<nick>_<symbol>.pins.json`` with build_pin_sidecars'
    math (D1: project parts go to ``<project>/pins/``, never the plugin's
    central server/pins/). ``nickname`` defaults to the library file stem and
    may differ from it (a netlist ``libraries`` block can map any nickname)."""
    path = Path(lib_path)
    nick = nickname or path.stem
    lib_id = f"{nick}:{symbol}"
    try:
        cache = {nick: symlib.load_lib(path.parent, path.stem)}
        pins = build_pin_sidecars.sidecar_map(cache, path.parent, lib_id)
    except symlib.SymbolLibError as e:
        raise SymbolAuthorError(f"no pin sidecar for {lib_id}: {e}") from e
    out = Path(pins_dir) / f"{nick}_{symbol}.pins.json"
    _atomic_write(out, json.dumps(pins, indent=2))
    return {"ok": True, "sidecar": str(out), "lib_id": lib_id, "keys": len(pins)}


# ----------------------------------------------------------------------------
# Quectel LG290P(03) — first authoring target (D1)
# ----------------------------------------------------------------------------
LG290P_DATASHEET = ("https://cdn.sparkfun.com/assets/b/1/7/6/3/"
                    "Quectel_LG290P_03__Hardware_Design_V1.0.pdf")
# Table 6: GND = 10, 12, 13, 24-79 (4 edge pads + all 55 inner pads)
LG290P_GND_PADS = ("10", "12", "13", *(str(n) for n in range(24, 80)))


def lg290p_spec(footprint: str = "") -> SymbolSpec:
    """Quectel LG290P(03) quad-band RTK GNSS module, 79-pin LGA: one pin per
    pad, names and I/O types from Table 6 (PI -> power_in, PO -> power_out,
    DI/AI -> input, DO -> output, DIO -> bidirectional; RESERVED "must be
    left N/C" -> no_connect). Datasheet asterisks (EVENT*, I2C_*,
    RTK_STAT/ANT_ON*: firmware-dependent functions) are dropped from names.
    ``footprint`` is the Footprint property (e.g. "<nick>:<footprint>")."""
    P = PinSpec
    signals = (
        P("23", "VCC", "power_in", "top", "POWER"),
        P("22", "V_BCKP", "power_in", "top", "POWER"),
        P("9", "VDD_RF", "power_out", "top", "POWER"),
        P("11", "RF_IN", "input", "left", "ANT"),
        P("8", "RESET_N", "input", "left", "SYSTEM"),
        P("4", "EVENT", "input", "left", "SYSTEM"),
        *(P(n, "RESERVED", "no_connect", "left", "RESERVED") for n in ("1", "2", "5", "17")),
        P("20", "TXD1", "output", "right", "UART1"),
        P("21", "RXD1", "input", "right", "UART1"),
        P("6", "TXD2", "output", "right", "UART2"),
        P("7", "RXD2", "input", "right", "UART2"),
        P("16", "TXD3", "output", "right", "UART3"),
        P("15", "RXD3", "input", "right", "UART3"),
        P("18", "I2C_SDA", "bidirectional", "right", "I2C"),
        P("19", "I2C_SCL", "input", "right", "I2C"),
        P("3", "1PPS", "output", "right", "STATUS"),
        P("14", "RTK_STAT/ANT_ON", "output", "right", "STATUS"),
    )
    ground = tuple(P(n, "GND", "power_in", "bottom", "GND") for n in LG290P_GND_PADS)
    return SymbolSpec(
        name="LG290P",
        pins=signals + ground,
        reference="U",
        value="LG290P",
        footprint=footprint,
        datasheet=LG290P_DATASHEET,
        description=("Quectel LG290P(03) quad-band (L1/L2/L5/E6) multi-constellation "
                     "RTK GNSS module, 79-pin LGA, 12.2 x 16.0 mm"),
        keywords="GNSS GPS RTK quad-band L1 L2 L5 E6 Quectel LG290P module",
        fp_filters="*LG290P*",
    )


def main(argv: list[str]) -> int:
    if len(argv) < 2 or (argv[0] == "--lg290p" and len(argv) > 3):
        print(__doc__)
        return 2
    try:
        if argv[0] == "--lg290p":
            spec = lg290p_spec(argv[2] if len(argv) > 2 else "")
        else:
            spec = spec_from_dict(json.loads(Path(argv[0]).read_text(encoding="utf-8")))
        res = author_symbol(spec, argv[1])
    except (SymbolAuthorError, OSError, ValueError) as e:
        res = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    print(json.dumps(res))
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
