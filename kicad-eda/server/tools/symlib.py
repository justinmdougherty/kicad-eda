#!/usr/bin/env python3
"""Shared KiCad symbol-library helpers (sexpdata based).

One implementation of the .kicad_sym parsing that sch_build.py, the pin-sidecar
builder, lint_netlist and design_report all need: load a library, flatten an
``extends`` symbol, embed it into a schematic ``lib_symbols`` block, list pins /
electrical types / units, and derive every pin's schematic-space connection
offset directly from the symbol geometry (so no sidecar file is required).

Coordinate note: symbol files use +Y = up; a placed schematic symbol uses
+Y = down. So the schematic-space offset of a pin at symbol coord (x, y) is
(x, -y). Offsets are valid for a symbol placed at rotation 0 (the only
orientation the generators use).
"""
from __future__ import annotations

from pathlib import Path

import sexpdata

S = sexpdata.Symbol

# tags valid in a .kicad_sym symbol-editor library but NOT accepted inside a
# .kicad_sch <lib_symbols> block — they must be stripped or KiCad refuses to load.
SYM_ONLY_TAGS = {"in_pos_files"}

# pin "names" that carry no information and must never become a lookup key
_ANON_PIN_NAMES = ("~", "")


class SymbolLibError(Exception):
    """A library file or a symbol inside it could not be resolved."""


def is_(n, t: str) -> bool:
    return isinstance(n, list) and bool(n) and isinstance(n[0], S) and n[0].value() == t


def kids(n, t: str) -> list:
    return [c for c in n if is_(c, t)]


def first(n, t: str):
    for c in n:
        if is_(c, t):
            return c
    return None


def load_lib(symbols_dir, lib: str) -> dict:
    """Parse ``<symbols_dir>/<lib>.kicad_sym`` into {symbol_name: sexp}."""
    path = Path(symbols_dir) / f"{lib}.kicad_sym"
    if not path.is_file():
        raise SymbolLibError(f"symbol library not found: {path}")
    try:
        data = sexpdata.loads(path.read_text(encoding="utf-8"))
    except Exception as e:  # sexpdata raises assorted parse errors
        raise SymbolLibError(f"cannot parse {path}: {e}") from e
    return {c[1]: c for c in kids(data, "symbol")}


def flatten(cache: dict, symbols_dir, lib: str, name: str) -> list:
    """Return a fully-resolved symbol sexp, merging an `extends` base.

    ``cache`` is {lib_name: {symbol_name: sexp}} and is filled on demand.
    Raises SymbolLibError when the library or the symbol does not exist."""
    if lib not in cache:
        cache[lib] = load_lib(symbols_dir, lib)
    if name not in cache[lib]:
        raise SymbolLibError(f"symbol {lib}:{name} not found in {lib}.kicad_sym")
    s = cache[lib][name]
    ext = first(s, "extends")
    if ext is None:
        return s
    base = flatten(cache, symbols_dir, lib, ext[1])
    out = [S("symbol"), name]
    for tag in ("pin_numbers", "pin_names", "exclude_from_sim", "in_bom", "on_board"):
        node = first(s, tag) or first(base, tag)
        if node is not None:
            out.append(node)
    props, order = {}, []
    for src in (base, s):
        for p in kids(src, "property"):
            if p[1] not in props:
                order.append(p[1])
            props[p[1]] = p
    out += [props[n] for n in order]
    bn = ext[1]
    for sub in kids(base, "symbol"):
        s2 = list(sub)
        s2[1] = name + sub[1][len(bn):]
        out.append(s2)
    return out


def embed(flat: list, lib_id: str) -> list:
    """Rename the top symbol to the full lib_id, dropping library-only tags the
    schematic parser rejects. Unit sub-symbols KEEP their bare name (e.g.
    ``R_0_1``) — KiCad rejects sub-symbols prefixed with the library nickname."""
    out = [flat[0], lib_id]
    for c in flat[2:]:
        if is_(c, "symbol"):
            out.append(c)  # unit sub-symbol: leave name untouched
        elif isinstance(c, list) and c and isinstance(c[0], S) and c[0].value() in SYM_ONLY_TAGS:
            continue
        else:
            out.append(c)
    return out


def pin_numbers(flat: list) -> list:
    """All pin numbers declared in a flattened symbol's unit sub-symbols."""
    nums = []
    for sub in kids(flat, "symbol"):
        for pin in kids(sub, "pin"):
            num = first(pin, "number")
            if num is not None and num[1] not in nums:
                nums.append(num[1])
    return nums


def pin_etypes(flat: list) -> dict:
    """Map pin number -> electrical type (e.g. 'power_in', 'power_out', 'passive').
    The type is the first atom after the `pin` tag: (pin power_in line ...)."""
    out = {}
    for sub in kids(flat, "symbol"):
        for pin in kids(sub, "pin"):
            num = first(pin, "number")
            if num is None:
                continue
            etype = pin[1].value() if len(pin) > 1 and isinstance(pin[1], S) else "unspecified"
            out[str(num[1])] = etype
    return out


def _unit_of_sub(flat: list, sub: list) -> int:
    """Unit number of a ``<bare>_<unit>_<bodystyle>`` sub-symbol. Unit 0
    (common-to-all-units graphics) is folded into unit 1."""
    bare = flat[1]
    rest = str(sub[1])[len(bare):].strip("_").split("_")
    try:
        u = int(rest[0])
    except (ValueError, IndexError):
        u = 1
    return 1 if u == 0 else u


def unit_pins(flat: list) -> dict:
    """Map unit number -> [pin numbers], parsed from sub-symbol names of the form
    ``<bare>_<unit>_<bodystyle>``. Unit 0 (common-to-all-units graphics) is folded
    into unit 1. A single-unit part returns {1: [...]}; a dual op-amp returns
    {1: [...], 2: [...], 3: [...]} (the 3rd being the power unit)."""
    units: dict[int, list] = {}
    for sub in kids(flat, "symbol"):
        u = _unit_of_sub(flat, sub)
        for pin in kids(sub, "pin"):
            num = first(pin, "number")
            if num is not None:
                units.setdefault(u, [])
                if num[1] not in units[u]:
                    units[u].append(num[1])
    return units or {1: []}


def _harvest_pins(node: list, unit: int, pins: dict) -> None:
    """Add every (pin ...) directly under ``node`` to ``pins``. A pin NUMBER
    always wins (later duplicates overwrite); a pin NAME is only recorded the
    first time it is seen (so "+" on a dual op-amp resolves to unit A)."""
    for pin in kids(node, "pin"):
        at = first(pin, "at")
        name = first(pin, "name")
        number = first(pin, "number")
        if at is None:
            continue
        x, y = float(at[1]), float(at[2])
        num = number[1] if number is not None and isinstance(number[1], str) else None
        off = {"x": round(x, 4), "y": round(-y, 4),  # +Y up -> +Y down
               "unit": unit, "number": num}
        if num is not None:
            pins[num] = off
        if name is not None and isinstance(name[1], str) and name[1] not in _ANON_PIN_NAMES:
            pins.setdefault(name[1], off)


def pin_offsets(flat: list) -> dict:
    """{key: {"x": mm, "y": mm, "unit": n, "number": str|None}} for a
    flattened symbol, keyed by BOTH pin number and pin name, in schematic space
    (y negated). ``number`` is the pin's own number, so a NAME key can be
    resolved back to the number the pin-type / unit maps are keyed by. This is
    the same math the pin sidecars were built with, so a sidecar and this map
    agree key-for-key; offsets assume the symbol is placed at rotation 0."""
    pins: dict[str, dict] = {}
    _harvest_pins(flat, 1, pins)  # tolerate pins directly on the symbol
    for sub in kids(flat, "symbol"):
        _harvest_pins(sub, _unit_of_sub(flat, sub), pins)
    return pins


def dumps(node) -> str:
    return sexpdata.dumps(node)
