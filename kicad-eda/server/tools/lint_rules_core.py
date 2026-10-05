#!/usr/bin/env python3
"""Core structural lint rules (ORDER 0), pure data, no KiCad needed:

  E1  a pad (ref,num) assigned to more than one net
  E2  a pad in both a net and the no_connect list
  E3  a net with fewer than 2 pads (floating / single-ended)
  E4  a net/no_connect referencing a ref not in components
  E5  duplicate component ref
  W1  a net name reused with different casing (likely a typo split)
  W2  a power_in pin (per symbol) left unconnected — would trip ERC
       (only when the context has a symbol source; otherwise skipped).
       Symbols resolve per component: an unresolvable lib_id skips W2 for
       its own parts only and is named in one "W2 skipped" warning.

Messages and their order are lint_netlist's historical output: E5, then E3/E4
per net in net order, E1, E2, E4 for no_connect (in declared order); W1, W2.
"""
from __future__ import annotations

import os
import sys
from collections import defaultdict
from collections.abc import Mapping
from pathlib import Path
from types import ModuleType
from typing import Any

from lint_context import LintContext, symbols_dir_of

ORDER = 0

Pad = tuple[Any, str]


def check(design: Mapping[str, Any], ctx: LintContext) -> tuple[list[str], list[str]]:
    """E1-E5 errors and W1/W2 warnings for one design."""
    comps = design.get("components", [])
    refs = [c["ref"] for c in comps]
    ref_set = set(refs)
    nets = design.get("nets", {})
    declared_nc = list(dict.fromkeys((r, str(p)) for r, p in design.get("no_connect", [])))
    no_connect = set(declared_nc)
    pad_to_nets, net_errors = _net_errors(nets, ref_set)
    errors = [*_e5_duplicate_refs(refs), *net_errors, *_e1_multi_net(pad_to_nets),
              *_e2_netted_and_no_connect(pad_to_nets, no_connect),
              *(f"E4 no_connect references unknown ref {r}"
                for r, _ in declared_nc if r not in ref_set)]
    warnings = _w1_case_collisions(nets)
    if ctx.symbols:      # W2 undriven/unconnected power_in pins (best-effort; needs symbols)
        warnings = warnings + _w2_power_in(comps, set(pad_to_nets) | no_connect, ctx.symbols)
    return errors, warnings


def _e5_duplicate_refs(refs: list) -> list[str]:
    seen: set = set()
    errors: list[str] = []
    for r in refs:
        if r in seen:
            errors.append(f"E5 duplicate component ref: {r}")
        seen.add(r)
    return errors


def _net_errors(nets: Mapping[str, list], ref_set: set) -> tuple[dict[Pad, list], list[str]]:
    """E3 / E4 per net, in net order, and the pad -> [nets] map."""
    errors: list[str] = []
    pad_to_nets: dict[Pad, list] = defaultdict(list)
    for net, conns in nets.items():
        if len(conns) < 2:
            errors.append(f"E3 net '{net}' has {len(conns)} pad(s) (needs >=2)")
        for r, p in conns:
            if r not in ref_set:
                errors.append(f"E4 net '{net}' references unknown ref {r}")
            pad_to_nets[(r, str(p))].append(net)
    return dict(pad_to_nets), errors


def _e1_multi_net(pad_to_nets: Mapping[Pad, list]) -> list[str]:
    return [f"E1 pad {pad[0]}.{pad[1]} assigned to multiple nets: {nlist}"
            for pad, nlist in pad_to_nets.items() if len(nlist) > 1]


def _e2_netted_and_no_connect(pad_to_nets: Mapping[Pad, list], no_connect: set) -> list[str]:
    return [f"E2 pad {pad[0]}.{pad[1]} is both netted and no_connect"
            for pad in pad_to_nets if pad in no_connect]


def _w1_case_collisions(nets: Mapping[str, list]) -> list[str]:
    lower_map: dict[str, set] = defaultdict(set)
    for net in nets:
        lower_map[net.lower()].add(net)
    return [f"W1 net names differ only by case: {sorted(variants)}"
            for variants in lower_map.values() if len(variants) > 1]


# --- W2 -----------------------------------------------------------------------------
def _import_symlib() -> ModuleType:
    here = str(Path(__file__).resolve().parent)
    if here not in sys.path:
        sys.path.insert(0, here)
    import symlib
    return symlib


def symbol_source(symlib_mod: ModuleType, symbols: object) -> object:
    """What W2 hands ``symlib.flatten``: the source itself when it is a
    directory or symlib resolves libraries itself (its ``lib_path``, F2-09);
    for an older symlib that reads only ``<dir>/<lib>.kicad_sym``, the stock
    symbols directory of a resolver / library map (the source if it has none)."""
    if isinstance(symbols, (str, os.PathLike)) or hasattr(symlib_mod, "lib_path"):
        return symbols
    return symbols_dir_of(symbols) or symbols


def _pin_types(sl: ModuleType, source: object, cache: dict,
               lib_id: str) -> tuple[dict | None, str]:
    """lib_id's {pin number: electrical type}, or None and why it failed."""
    lib, _, name = lib_id.partition(":")
    try:
        if not lib or not name:
            raise sl.SymbolLibError("lib_id is not 'Library:Symbol'")
        return sl.pin_etypes(sl.flatten(cache, source, lib, name)), ""
    except Exception as e:  # noqa: BLE001 - best-effort W2: name the lib_id, never crash lint
        return None, (str(e) if isinstance(e, sl.SymbolLibError) else f"{type(e).__name__}: {e}")


def _w2_power_in(comps: list, netted: set, symbols: object) -> list[str]:
    """W2 warnings: power_in pins that are neither netted nor no_connect.

    Each component's symbol resolves on its own (once per lib_id), so one
    unresolvable lib_id skips W2 for that lib_id's parts only, never for the
    rest of the design. The unresolvable lib_ids are reported together in ONE
    trailing warning, ``W2 skipped (symbol lookup failed: <lib_id> [<refs>]:
    <reason>; ...)``, each lib_id once, in first-seen order."""
    try:
        sl = _import_symlib()
    except ImportError as e:
        return [f"W2 skipped (symbol lookup failed: {e})"]
    source = symbol_source(sl, symbols)
    warnings: list[str] = []
    unresolved: dict[str, list[str]] = {}       # lib_id -> refs
    reasons: dict[str, str] = {}                # lib_id -> failure
    types: dict[str, tuple[dict | None, str]] = {}
    cache: dict = {}
    for c in comps:
        lib_id = str(c.get("lib_id", ""))
        if lib_id not in types:
            types[lib_id] = _pin_types(sl, source, cache, lib_id)
        etypes, reason = types[lib_id]
        if etypes is None:
            unresolved.setdefault(lib_id, []).append(str(c["ref"]))
            reasons.setdefault(lib_id, reason)
            continue
        warnings += [f"W2 {c['ref']}.{num} is a power_in pin but not connected"
                     for num, etype in etypes.items()
                     if etype == "power_in" and (c["ref"], str(num)) not in netted]
    if unresolved:
        detail = "; ".join(f"{lib_id} [{', '.join(refs)}]: {reasons[lib_id]}"
                           for lib_id, refs in unresolved.items())
        warnings.append(f"W2 skipped (symbol lookup failed: {detail})")
    return warnings
