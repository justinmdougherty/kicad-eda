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

Symbol sources (ROADMAP_DECISIONS D1): every loader takes a ``source`` that
names where a library nickname lives (``lib_path``):

* a directory (str / os.PathLike): ``<dir>/<nick>.kicad_sym``, the stock
  naming convention (the behaviour before project libraries);
* a ``libtable.LibraryResolver``: netlist ``libraries`` block, then the
  project ``sym-lib-table``, then its stock directory;
* a library map dict (``LibraryResolver.library_map()``, e.g. read back from
  JSON): its ``symbols`` entries, else its stock directory by convention.

Library files are parsed once per (path, mtime_ns, size) and the parse is
shared process-wide (an LRU bounded by PARSE_CACHE_BYTES of library files):
the symbol nodes returned are read-only by contract.
"""
from __future__ import annotations

import os
import sys
import threading
import time
from collections import OrderedDict
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Protocol, Union

import sexpdata

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import libtable  # noqa: E402  (stdlib-only sibling: library tables + resolver)

S = sexpdata.Symbol

# tags valid in a .kicad_sym symbol-editor library but NOT accepted inside a
# .kicad_sch <lib_symbols> block — they must be stripped or KiCad refuses to load.
SYM_ONLY_TAGS = {"in_pos_files"}

# pin "names" that carry no information and must never become a lookup key
_ANON_PIN_NAMES = ("~", "")

SYMBOL_SUFFIX = libtable.LIB_SUFFIX[libtable.SYMBOL]     # ".kicad_sym"
KICAD_LIB_TYPE = "kicad"              # the only sym-lib-table type symlib can parse
# Parsed libraries kept per process (LRU), bounded by their FILE size: a parse
# retains ~10x its file (KiCad 9 MCU_ST_STM32H7: 16 MB file, ~156 MB parsed,
# ~10-25 s to parse), so 32 MB of libraries is a few hundred MB at most.
PARSE_CACHE_BYTES = 32_000_000
# A file modified this recently is parsed afresh: a same-size rewrite inside one
# filesystem timestamp tick (FAT: 2 s) would otherwise look unchanged.
JUST_WRITTEN_NS = 2_000_000_000


class SymbolLibError(Exception):
    """A library file or a symbol inside it could not be resolved."""


class SymbolResolver(Protocol):
    """What symlib needs of a libtable.LibraryResolver."""

    def symbol_lib_path(self, nick: str) -> Path: ...

    def resolve(self, kind: str, nick: str) -> object: ...


SymbolSource = Union[str, "os.PathLike[str]", SymbolResolver, Mapping]


class SymbolLib(dict):
    """{symbol_name: sexp} of one library file; ``path`` is that file. A
    fresh dict per load_lib call (the sexp nodes inside are shared)."""

    def __init__(self, symbols: Mapping, path: Path) -> None:
        super().__init__(symbols)
        self.path = path


def is_(n, t: str) -> bool:
    return isinstance(n, list) and bool(n) and isinstance(n[0], S) and n[0].value() == t


def kids(n, t: str) -> list:
    return [c for c in n if is_(c, t)]


def first(n, t: str):
    for c in n:
        if is_(c, t):
            return c
    return None


# --- where a nickname lives -----------------------------------------------------
def _not_found(lib: str, detail: str) -> SymbolLibError:
    return SymbolLibError(f"symbol library not found: '{lib}': {detail}")


def _require_kicad(lib: str, lib_type: str, path: object, origin: str) -> None:
    """Refuse a library row of a type symlib cannot parse (Legacy, Eagle, ...)."""
    if lib_type.strip().lower() != KICAD_LIB_TYPE:
        raise SymbolLibError(
            f"symbol library '{lib}' ({path}, from {origin}) has type '{lib_type}': only "
            f"KiCad {SYMBOL_SUFFIX} libraries can be read; convert it (kicad-cli sym upgrade) "
            f"and declare it with type KiCad")


def _resolver_lib_path(resolver: SymbolResolver, lib: str) -> Path:
    try:
        path = Path(resolver.symbol_lib_path(lib))
    except (libtable.LibTableError, LookupError) as e:   # LibraryNotFoundError: says where
        raise _not_found(lib, str(e).removeprefix(f"symbol library '{lib}' not found: ")) from e
    hit = resolver.resolve(libtable.SYMBOL, lib)
    if hit is not None:
        _require_kicad(lib, str(getattr(hit, "type", "KiCad")), path,
                       str(getattr(hit, "origin", "") or "the library resolver"))
    return path


def _map_section(lib_map: Mapping, key: str) -> Mapping:
    section = lib_map.get(key)
    if section is None:
        return {}
    if not isinstance(section, Mapping):
        raise SymbolLibError(f"library map: '{key}' must be an object, got "
                             f"{type(section).__name__}")
    return section


def _map_lib_path(lib_map: Mapping, lib: str) -> Path:
    key = libtable.MAP_KEYS[libtable.SYMBOL]
    entry = _map_section(lib_map, key).get(lib)
    stock = _map_section(lib_map, "stock").get(key)
    if stock is not None and not isinstance(stock, str):
        raise SymbolLibError(f"library map: stock['{key}'] must be a directory path string or "
                             f"null, got {stock!r}")
    if entry is not None:
        if not isinstance(entry, Mapping) or not isinstance(entry.get("path"), str):
            raise SymbolLibError(f"library map entry symbols['{lib}'] must be an object with a "
                                 f"'path' string, got {entry!r}")
        origin = str(entry.get("origin") or "the library map")
        if entry.get("disabled"):
            raise _not_found(lib, f"disabled in {origin}")
        _require_kicad(lib, str(entry.get("type") or "KiCad"), entry["path"], origin)
    path = libtable.lookup(lib_map, libtable.SYMBOL, lib)
    if path is None:
        where = (f"{Path(stock) / (lib + SYMBOL_SUFFIX)} does not exist" if stock
                 else "the map has no stock symbol directory")
        raise _not_found(lib, f"not in the library map's symbols, and {where}")
    return Path(path)


def lib_path(source: SymbolSource, lib: str) -> Path:
    """The .kicad_sym file nickname ``lib`` names in ``source``: a directory
    (``<dir>/<lib>.kicad_sym``, need not exist), a LibraryResolver
    (``symbol_lib_path``) or a library map (``libtable.lookup``). An
    unresolvable nickname, or a library of a type other than KiCad, is a
    SymbolLibError naming it; another kind of source is a TypeError."""
    if isinstance(source, (str, os.PathLike)):
        return Path(source) / f"{lib}{SYMBOL_SUFFIX}"
    if isinstance(source, Mapping):
        return _map_lib_path(source, lib)
    if callable(getattr(source, "symbol_lib_path", None)):
        return _resolver_lib_path(source, lib)
    raise TypeError(f"symbol source must be a directory, a libtable.LibraryResolver or a "
                    f"library map dict, got {type(source).__name__}")


# --- parsing (an LRU cache keyed by file version) ------------------------------
_CacheKey = tuple[str, int, int]                       # (abs path, mtime_ns, size)
_PARSED: OrderedDict[_CacheKey, Mapping[str, list]] = OrderedDict()   # oldest first
_PARSED_LOCK = threading.Lock()


def _parse_file(path: str) -> Mapping[str, list]:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as e:
        raise SymbolLibError(f"cannot read {path}: {e}") from e
    try:
        data = sexpdata.loads(text)
    except Exception as e:  # sexpdata raises assorted parse errors
        raise SymbolLibError(f"cannot parse {path}: {e}") from e
    return MappingProxyType({c[1]: c for c in kids(data, "symbol")})


def _cached(key: _CacheKey) -> Mapping[str, list] | None:
    with _PARSED_LOCK:
        hit = _PARSED.get(key)
        if hit is not None:
            _PARSED.move_to_end(key)
        return hit


def _remember(key: _CacheKey, parsed: Mapping[str, list]) -> None:
    """Keep ``parsed`` as the newest entry, dropping older versions of the same
    file, then least-recently-used entries until the libraries total at most
    PARSE_CACHE_BYTES. A library larger than the whole budget is not kept."""
    if key[2] > PARSE_CACHE_BYTES:
        return
    with _PARSED_LOCK:
        for old in [k for k in _PARSED if k[0] == key[0]]:
            del _PARSED[old]
        _PARSED[key] = parsed
        while sum(k[2] for k in _PARSED) > PARSE_CACHE_BYTES:
            _PARSED.popitem(last=False)


def _parse(path: Path) -> Mapping[str, list]:
    """One parse per file version; a failed parse raises and is not cached."""
    abspath = os.path.abspath(path)
    try:
        st = os.stat(abspath)
    except OSError as e:
        raise SymbolLibError(f"cannot read {path}: {e}") from e
    if time.time_ns() - st.st_mtime_ns < JUST_WRITTEN_NS:
        return _parse_file(abspath)
    key = (abspath, st.st_mtime_ns, st.st_size)
    parsed = _cached(key)
    if parsed is None:
        parsed = _parse_file(abspath)
        _remember(key, parsed)
    return parsed


def cache_info() -> dict:
    """{"entries", "bytes", "limit"} of the parse cache (bytes = library file sizes)."""
    with _PARSED_LOCK:
        return {"entries": len(_PARSED), "bytes": sum(k[2] for k in _PARSED),
                "limit": PARSE_CACHE_BYTES}


def clear_cache() -> None:
    """Forget every cached library parse (tests; a long-lived process that
    wants to drop the memory)."""
    with _PARSED_LOCK:
        _PARSED.clear()


def load_lib(source: SymbolSource, lib: str) -> SymbolLib:
    """Parse the library nickname ``lib`` names in ``source`` (see lib_path)
    into {symbol_name: sexp}; ``.path`` is the file read."""
    path = lib_path(source, lib)
    if not path.is_file():
        note = "" if isinstance(source, (str, os.PathLike)) else f" (library '{lib}')"
        raise SymbolLibError(f"symbol library not found: {path}{note}")
    return SymbolLib(_parse(path), path)


def flatten(cache: dict, source: SymbolSource, lib: str, name: str) -> list:
    """Return a fully-resolved symbol sexp, merging an `extends` base.

    ``cache`` is {lib_name: {symbol_name: sexp}} and is filled on demand from
    ``source`` (a directory, a LibraryResolver or a library map; see
    lib_path), which is not consulted for a library already in ``cache``.
    Raises SymbolLibError when the library or the symbol does not exist."""
    if lib not in cache:
        cache[lib] = load_lib(source, lib)
    if name not in cache[lib]:
        path = getattr(cache[lib], "path", None)
        file = path.name if isinstance(path, Path) else f"{lib}{SYMBOL_SUFFIX}"
        raise SymbolLibError(f"symbol {lib}:{name} not found in {file}")
    s = cache[lib][name]
    ext = first(s, "extends")
    if ext is None:
        return s
    base = flatten(cache, source, lib, ext[1])
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
