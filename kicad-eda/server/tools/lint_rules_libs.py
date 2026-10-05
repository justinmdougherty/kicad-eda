#!/usr/bin/env python3
"""Library integrity lint rules (LIB-*), ORDER 10: run right after the core rules.

  LIB-E1  the project libraries did not load (``ctx.library_error``)
  LIB-E2  ``resolver.errors(**libtable.design_nicknames(design))``: a used
          nickname resolving nowhere or to a disabled row, a block-vs-project
          conflict, a declared library file that is missing
  LIB-E3  a symbol / footprint name missing from its resolved library, or an
          unreadable library file (one error per lib_id, naming its refs)
  LIB-E4  a netted pad key that is not a pad of the footprint (the board build
          matches pad NUMBERS; a pin name maps to its number through the symbol
          for the hint), or a no_connect key that is neither a pad nor a pin
          (number or name) of the symbol: a no_connect may flag a pad-less pin
  LIB-E5  the listed pins of one stacked group (one spot of one unit) sit on
          different nets, or a net and no_connect: KiCad joins stacked pins
  LIB-W1  symbol pins without a matching pad (once per symbol + footprint)
  LIB-W2  a project footprint (netlist block / project table) without courtyard
  LIB-W3  height_zones, but a project footprint has no height (no 3D model,
          Height property or component ``height``)

With ``ctx.resolver`` every rule runs (a library it cannot place is LIB-E2's
alone). Without one (dir mode) only LIB-E1, LIB-E3 and LIB-E4 run, against
``ctx.symbols`` (directory, library map or resolver) and the footprints of
``options["footprints_dir"]``, that map / resolver, or an existing
``<symbols dir>/../footprints``; a library missing there is an LIB-E3 unless
LIB-E1 explains it (a symbols directory that does not exist is no source).
Footprints are read with fp_reader, symbols with symlib (lazily: without
sexpdata, one warning); each library file is read once per call.
"""
from __future__ import annotations

import os
import re
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType, ModuleType
from typing import Any

import fp_reader
import libtable
from lint_context import LintContext, is_resolver

ORDER = 10

NO_CONNECT = "no_connect"
FOOTPRINT_FILE_SUFFIX = ".kicad_mod"
FOOTPRINTS_DIR_OPTION = "footprints_dir"      # dir mode: the stock .pretty libraries
STOCK_FOOTPRINTS_DIRNAME = "footprints"       # <share>/symbols sits beside <share>/footprints
COURTYARD_LAYERS = ("F.CrtYd", "B.CrtYd")
HEIGHT_PROPERTY = "height"                    # a footprint property "Height" (any case)
PROJECT_SOURCES = (libtable.NETLIST, libtable.PROJECT)
_SUB_UNIT_RE = re.compile(r"_(\d+)_(\d+)$")   # <symbol>_<unit>_<body style>
_DIGITS_RE = re.compile(r"(\d+)")


@dataclass(frozen=True)
class _Found:
    """``item`` (_Pins / _Footprint), or None and an LIB-E3 ``problem`` ('' = not here)."""
    item: Any = None
    problem: str = ""


@dataclass(frozen=True)
class _Pins:
    """A symbol's pin numbers, pin name -> number (first wins) and stacks."""
    numbers: frozenset[str]
    names: Mapping[str, str]
    stacks: tuple[tuple[str, ...], ...]

    def number_of(self, key: str) -> str | None:
        return key if key in self.numbers else self.names.get(key)


@dataclass(frozen=True)
class _Footprint:
    """A footprint's pad numbers, courtyard presence and file text (LIB-W3)."""
    pads: frozenset[str]
    courtyard: bool
    text: str


@dataclass(frozen=True)
class _Part:
    """One component and what its libraries say (None = unknown)."""
    ref: str
    lib_id: str
    fp_id: str
    pins: _Pins | None
    footprint: _Footprint | None
    wants_symbol: bool                     # not board_only: the schematic places it
    height: bool                           # the component declares "height"


def _lib_ref(value: object) -> tuple[str, str] | None:
    nick, _, name = value.partition(":") if isinstance(value, str) else ("", "", "")
    return (nick, name) if nick and name else None


def _natural(text: str) -> list[tuple[int, int, str]]:
    return [(0, int(t), "") if t.isdigit() else (1, 0, t) for t in _DIGITS_RE.split(text) if t]


def _import_symlib() -> ModuleType:
    import symlib                 # a sibling, like fp_reader / libtable (sexpdata-based)
    return symlib


def _stacks(sl: ModuleType, flat: list) -> tuple[tuple[str, ...], ...]:
    """Pin numbers sharing one spot of one unit (KiCad's stacked pins). The
    alternate (De Morgan) body style is never placed; unit 0 counts as unit 1."""
    spots: dict[tuple[int, float, float], list[str]] = {}
    for sub in sl.kids(flat, "symbol"):
        m = _SUB_UNIT_RE.search(str(sub[1]))
        unit, style = (int(m.group(1)), int(m.group(2))) if m else (1, 1)
        if style > 1:
            continue
        for pin in sl.kids(sub, "pin"):
            num, at = sl.first(pin, "number"), sl.first(pin, "at")
            if num is None or at is None or not str(num[1]):
                continue
            spot = (unit or 1, round(float(at[1]), 3), round(float(at[2]), 3))
            numbers = spots.setdefault(spot, [])
            if str(num[1]) not in numbers:
                numbers.append(str(num[1]))
    return tuple(tuple(n) for n in spots.values() if len(n) > 1)


def _pins(sl: ModuleType, flat: list) -> _Pins:
    names = {k: str(v["number"]) for k, v in sl.pin_offsets(flat).items()
             if v.get("number") and k != v["number"]}
    numbers = frozenset(str(n) for n in sl.pin_numbers(flat) if str(n))
    return _Pins(numbers, MappingProxyType(names), _stacks(sl, flat))


def _read_footprint(path: Path) -> _Footprint:
    text = path.read_text(encoding="utf-8")
    parsed = fp_reader.parse_footprint(text)
    return _Footprint(frozenset(p.number for p in parsed.pads if p.number),
                      any(parsed.strokes.get(layer) for layer in COURTYARD_LAYERS), text)


def _footprint_backend(ctx: LintContext) -> tuple[str, Any]:
    """Where footprint libraries are looked up: 'full' (ctx.resolver), else
    (dir mode) 'dir' (a stock .pretty root; None = none), 'resolver' or 'map'."""
    if ctx.resolver is not None:
        return "full", ctx.resolver
    explicit = ctx.options.get(FOOTPRINTS_DIR_OPTION)
    if explicit:
        return "dir", Path(explicit)          # a non-path value is Path()'s TypeError
    if is_resolver(ctx.symbols) or isinstance(ctx.symbols, Mapping):
        return ("resolver" if is_resolver(ctx.symbols) else "map"), ctx.symbols
    sibling = Path(ctx.symbols_dir).parent / STOCK_FOOTPRINTS_DIRNAME if ctx.symbols_dir else None
    return "dir", (sibling if sibling is not None and sibling.is_dir() else None)


class _Libraries:
    """The symbol / footprint lookups of one check() call, cached per call."""

    def __init__(self, ctx: LintContext) -> None:
        self._ctx = ctx
        self._source = ctx.resolver if ctx.resolver is not None else (ctx.symbols or None)
        if isinstance(self._source, (str, os.PathLike)) and not Path(self._source).is_dir():
            self._source = None                       # dir mode without stock symbols
        self._fp_kind, self._fp_backend = _footprint_backend(ctx)
        self.notes: list[str] = []
        self._sl: ModuleType | None = None
        if self._source is not None:
            try:
                self._sl = _import_symlib()
            except ImportError as e:
                self.notes.append(f"LIB symbol checks skipped (cannot import symlib: {e})")
        self._flat_cache: dict = {}
        self._symbols: dict[tuple[str, str], _Found] = {}
        self._footprints: dict[tuple[str, str], _Found] = {}

    def _missing(self, text: str) -> str:
        """A library missing in dir mode, unless LIB-E1 already explains it."""
        return "" if self._ctx.library_error else text

    @staticmethod
    def _cached(cache: dict, lib_ref: object, load: Callable[[str, str], _Found]) -> _Found:
        ref = _lib_ref(lib_ref)
        if ref is None:
            return _Found()
        if ref not in cache:
            cache[ref] = load(*ref)
        return cache[ref]

    def symbol(self, lib_id: object) -> _Found:
        return _Found() if self._sl is None else \
            self._cached(self._symbols, lib_id, self._load_symbol)

    def footprint(self, fp_id: object) -> _Found:
        return self._cached(self._footprints, fp_id, self._load_footprint)

    def _load_symbol(self, nick: str, name: str) -> _Found:
        sl, resolver = self._sl, self._ctx.resolver
        if resolver is not None:
            hit = resolver.resolve(libtable.SYMBOL, nick)
            if hit is None or not hit.exists:
                return _Found()                      # LIB-E2 names it
        try:
            path = sl.lib_path(self._source, nick)
        except sl.SymbolLibError as e:              # unresolved (dir mode) or not type KiCad
            return _Found(problem=str(e) if resolver is not None else self._missing(str(e)))
        if not path.is_file():
            return _Found(problem=self._missing(
                f"symbol library not found: '{nick}': {path} does not exist"))
        try:
            return _Found(_pins(sl, sl.flatten(self._flat_cache, self._source, nick, name)))
        except sl.SymbolLibError as e:
            return _Found(problem=str(e))
        except (ValueError, IndexError, TypeError, AttributeError) as e:   # malformed library
            return _Found(problem=f"{type(e).__name__}: {e}")

    def _load_footprint(self, nick: str, name: str) -> _Found:
        lib, problem = self._footprint_library(nick)
        if lib is None:
            return _Found(problem=problem)
        path = lib / f"{name}{FOOTPRINT_FILE_SUFFIX}"
        if not path.is_file():
            return _Found(problem=f"{path.name} not found in {lib}")
        try:
            return _Found(_read_footprint(path))
        except (OSError, ValueError) as e:            # FootprintParseError, UnicodeDecodeError
            return _Found(problem=str(e))

    def _footprint_library(self, nick: str) -> tuple[Path | None, str]:
        """(the .pretty directory, '') or (None, why not; '' = said elsewhere)."""
        kind, backend = self._fp_kind, self._fp_backend
        if backend is None:
            return None, ""                           # dir mode without footprints
        if kind in ("full", "resolver"):
            hit = backend.resolve(libtable.FOOTPRINT, nick)
            path = hit.path if hit is not None else None
        elif kind == "map":
            found = libtable.lookup(backend, libtable.FOOTPRINT, nick)
            path = Path(found) if found else None
        else:
            path = backend / f"{nick}{libtable.LIB_SUFFIX[libtable.FOOTPRINT]}"
        if path is not None and path.is_dir():
            return path, ""
        if kind == "full":
            return None, ""                           # LIB-E2 names it
        where = (f"{path} does not exist" if path is not None
                 else f"not in the library {kind} (or disabled there)")
        return None, self._missing(f"footprint library not found: '{nick}': {where}")


def _parts(design: Mapping[str, Any], libs: _Libraries) -> tuple[dict[str, _Part], list[str]]:
    """{ref: _Part} (a duplicated ref keeps its first part) and the LIB-E3
    errors: one per missing / unreadable symbol or footprint, with its refs."""
    comps = design.get("components")
    parts: dict[str, _Part] = {}
    problems: dict[tuple[str, str, str], list[str]] = {}
    for comp in (comps if isinstance(comps, list) else ()):
        if not isinstance(comp, Mapping) or not isinstance(comp.get("ref"), str):
            continue
        ref, lib_id, fp_id = comp["ref"], comp.get("lib_id"), comp.get("footprint")
        wants = not comp.get("board_only")
        sym = libs.symbol(lib_id) if wants else _Found()
        fp = libs.footprint(fp_id)
        for kind, lib_ref, found in (("symbol", lib_id, sym), ("footprint", fp_id, fp)):
            if found.problem:
                problems.setdefault((kind, str(lib_ref), found.problem), []).append(ref)
        parts.setdefault(ref, _Part(ref, str(lib_id or ""), str(fp_id or ""), sym.item,
                                    fp.item, wants, comp.get("height") is not None))
    errors = [f"LIB-E3 {kind} {lib_ref} ({', '.join(refs)}): {problem}"
              for (kind, lib_ref, problem), refs in problems.items()]
    return parts, errors


def _pad_uses(design: Mapping[str, Any]) -> Iterator[tuple[str | None, str, str]]:
    """(net, ref, pad key) of each net entry in order, then (None, ...) of no_connect."""
    nets, no_connect = design.get("nets"), design.get("no_connect")
    uses = [(str(net), conn) for net, conns in (nets.items() if isinstance(nets, Mapping) else ())
            for conn in (conns if isinstance(conns, list) else ())]
    uses += [(None, conn) for conn in (no_connect if isinstance(no_connect, list) else ())]
    for net, conn in uses:
        if isinstance(conn, (list, tuple)) and len(conn) == 2 and isinstance(conn[0], str):
            yield net, conn[0], str(conn[1])


def _refs(parts: list[_Part]) -> str:
    return ", ".join(p.ref for p in parts)


def _key_problem(part: _Part, key: str, no_connect: bool) -> str:
    """Why ``key`` is not a usable pad key of ``part`` ('' = it is)."""
    if key in part.footprint.pads:
        return ""
    number = part.pins.number_of(key) if part.pins is not None else None
    if no_connect and part.wants_symbol:
        if number is not None or part.pins is None:    # a pin, or no symbol to judge by
            return ""
        return f"is neither a pad of {part.fp_id} nor a pin of {part.lib_id}"
    hint = (f" (pin name '{key}' is pin {number}; the board build matches pad numbers)"
            if number is not None and number != key and not no_connect else "")
    return f"is not a pad of {part.fp_id}{hint}"


def _e4_pad_keys(design: Mapping[str, Any], parts: Mapping[str, _Part]) -> list[str]:
    """LIB-E4: each (ref, key) once per kind (netted / no_connect)."""
    errors: list[str] = []
    seen: set[tuple[bool, str, str]] = set()
    for net, ref, key in _pad_uses(design):
        part = parts.get(ref)
        if part is None or part.footprint is None or (net is None, ref, key) in seen:
            continue
        seen.add((net is None, ref, key))
        problem = _key_problem(part, key, net is None)
        if problem:
            where = NO_CONNECT if net is None else f"net '{net}'"
            errors.append(f"LIB-E4 {ref}.{key} ({where}) {problem}")
    return errors


def _e5_stacks(design: Mapping[str, Any], parts: Mapping[str, _Part]) -> list[str]:
    """LIB-E5: a stack's listed pins disagree (first net, else NO_CONNECT; open is fine)."""
    states: dict[tuple[str, str], str] = {}
    for net, ref, key in _pad_uses(design):
        part = parts.get(ref)
        number = part.pins.number_of(key) if part is not None and part.pins else None
        states.setdefault((ref, number or key), NO_CONNECT if net is None else net)
    errors: list[str] = []
    for part in parts.values():
        for stack in (part.pins.stacks if part.pins is not None else ()):
            where = {n: states[(part.ref, n)] for n in stack if (part.ref, n) in states}
            if len(set(where.values())) > 1:
                detail = ", ".join(f"{n} -> {state}" for n, state in where.items())
                errors.append(f"LIB-E5 {part.ref} ({part.lib_id}): stacked pins "
                              f"{', '.join(stack)} sit on different nets ({detail}); KiCad "
                              f"joins stacked pins into one net")
    return errors


def _w1_unpadded_pins(parts: Mapping[str, _Part]) -> list[str]:
    groups: dict[tuple[str, str], tuple[list[str], list[_Part]]] = {}
    for part in parts.values():
        if part.pins is None or part.footprint is None:
            continue
        missing = sorted(part.pins.numbers - part.footprint.pads, key=_natural)
        if missing:
            groups.setdefault((part.lib_id, part.fp_id), (missing, []))[1].append(part)
    return [f"LIB-W1 {lib_id} on {fp_id} ({_refs(users)}): symbol pin(s) "
            f"{', '.join(missing)} have no pad"
            for (lib_id, fp_id), (missing, users) in groups.items()]


def _has_height(fp: _Footprint) -> bool:
    """A 3D model or a non-empty Height property in the footprint file."""
    for node in fp_reader.parse_sexpr(fp.text)[2:]:
        tag, args = (node[0], node[1:3]) if isinstance(node, list) and node else ("", [])
        if tag == "model" and args and str(args[0]).strip():
            return True
        if tag == "property" and len(args) == 2 and str(args[0]).lower() == HEIGHT_PROPERTY \
                and str(args[1]).strip():
            return True
    return False


def _w2_w3_project_footprints(design: Mapping[str, Any], parts: Mapping[str, _Part],
                              resolver: Any) -> list[str]:
    """LIB-W2 / LIB-W3 for the footprints that come from a project library."""
    project: dict[str, tuple[_Footprint, list[_Part]]] = {}
    for part in parts.values():
        ref = _lib_ref(part.fp_id) if part.footprint is not None else None
        hit = resolver.resolve(libtable.FOOTPRINT, ref[0]) if ref is not None else None
        if hit is not None and hit.source in PROJECT_SOURCES:
            project.setdefault(part.fp_id, (part.footprint, []))[1].append(part)
    w2 = [f"LIB-W2 {fp_id} ({_refs(users)}): project footprint has no F.CrtYd/B.CrtYd "
          f"courtyard" for fp_id, (fp, users) in project.items() if not fp.courtyard]
    w3: list[str] = []
    for fp_id, (fp, users) in (project.items() if design.get("height_zones") else ()):
        bare = [p for p in users if not p.height]
        if bare and not _has_height(fp):
            w3.append(f"LIB-W3 {fp_id} ({_refs(bare)}): the design has height_zones but this "
                      f"project footprint has no height (no 3D model or Height property); "
                      f"declare \"height\" on the part")
    return w2 + w3


def check(design: Mapping[str, Any], ctx: LintContext) -> tuple[list[str], list[str]]:
    """(errors, warnings) for the design's libraries (see the module doc)."""
    errors = ([f"LIB-E1 the project libraries did not load: {ctx.library_error}"]
              if ctx.library_error else [])
    if ctx.resolver is not None:
        errors += [f"LIB-E2 {m}" for m in
                   ctx.resolver.errors(**libtable.design_nicknames(design))]
    libs = _Libraries(ctx)
    parts, e3 = _parts(design, libs)
    errors += e3 + _e4_pad_keys(design, parts)
    if ctx.resolver is None:                          # dir mode: LIB-E3 / LIB-E4 only
        return errors, list(libs.notes)
    errors += _e5_stacks(design, parts)
    warnings = [*libs.notes, *_w1_unpadded_pins(parts),
                *_w2_w3_project_footprints(design, parts, ctx.resolver)]
    return errors, warnings
