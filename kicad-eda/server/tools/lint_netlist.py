#!/usr/bin/env python3
"""Lint a pad-level netlist.json for the structural mistakes that otherwise only
surface after a full (slow) board+route+DRC run.

Checks (all pure data — no KiCad needed):
  E1  a pad (ref,num) assigned to more than one net
  E2  a pad in both a net and the no_connect list
  E3  a net with fewer than 2 pads (floating / single-ended)
  E4  a net/no_connect referencing a ref not in components
  E5  duplicate component ref
  W1  a net name reused with different casing (likely a typo split)
  W2  a power_in pin (per symbol) left unconnected — would trip ERC
       (only when a sidecar/symbol lookup is available; otherwise skipped).
       Symbols resolve per component: an unresolvable lib_id skips W2 for
       its own parts only and is named in one "W2 skipped" warning.

Exit code 0 if no errors (warnings allowed), 1 otherwise.

Usage: <python> lint_netlist.py <netlist.json> [symbols_dir]
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path


def lint(design: dict, symbols_dir: str = "") -> dict:
    errors: list[str] = []
    warnings: list[str] = []

    comps = design.get("components", [])
    refs = [c["ref"] for c in comps]
    ref_set = set(refs)

    # E5 duplicate refs
    seen = set()
    for r in refs:
        if r in seen:
            errors.append(f"E5 duplicate component ref: {r}")
        seen.add(r)

    nets = design.get("nets", {})
    no_connect = {(r, str(p)) for r, p in design.get("no_connect", [])}

    # E1/E3/E4 over nets
    pad_to_nets: dict[tuple, list] = defaultdict(list)
    for net, conns in nets.items():
        if len(conns) < 2:
            errors.append(f"E3 net '{net}' has {len(conns)} pad(s) (needs >=2)")
        for r, p in conns:
            if r not in ref_set:
                errors.append(f"E4 net '{net}' references unknown ref {r}")
            pad_to_nets[(r, str(p))].append(net)

    for pad, nlist in pad_to_nets.items():
        if len(nlist) > 1:
            errors.append(f"E1 pad {pad[0]}.{pad[1]} assigned to multiple nets: {nlist}")

    # E2 pad in a net AND no_connect
    for pad in pad_to_nets:
        if pad in no_connect:
            errors.append(f"E2 pad {pad[0]}.{pad[1]} is both netted and no_connect")
    for r, p in no_connect:
        if r not in ref_set:
            errors.append(f"E4 no_connect references unknown ref {r}")

    # W1 case-collision net names
    lower_map: dict[str, set] = defaultdict(set)
    for net in nets:
        lower_map[net.lower()].add(net)
    for low, variants in lower_map.items():
        if len(variants) > 1:
            warnings.append(f"W1 net names differ only by case: {sorted(variants)}")

    # W2 undriven/unconnected power_in pins (best-effort; needs symbol access)
    if symbols_dir:
        warnings += _w2_power_in(comps, set(pad_to_nets) | no_connect, symbols_dir)

    return {"ok": not errors, "errors": errors, "warnings": warnings,
            "components": len(comps), "nets": len(nets)}


def _w2_power_in(comps: list, netted: set, symbols_dir: str) -> list[str]:
    """W2 warnings: power_in pins that are neither netted nor no_connect.

    Each component's symbol resolves on its own, so one unresolvable lib_id
    skips W2 for that lib_id's parts only, never for the rest of the design.
    The unresolvable lib_ids are reported together in ONE trailing warning,
    ``W2 skipped (symbol lookup failed: <lib_id> [<refs>]: <reason>; ...)``,
    each lib_id once, in first-seen order."""
    try:
        sys.path.insert(0, str(Path(__file__).parent))
        import sch_build as sb  # noqa
    except ImportError as e:
        return [f"W2 skipped (symbol lookup failed: {e})"]
    warnings: list[str] = []
    unresolved: dict[str, list[str]] = {}       # lib_id -> refs
    reasons: dict[str, str] = {}                # lib_id -> first failure
    cache: dict = {}
    for c in comps:
        lib_id = str(c.get("lib_id", ""))
        lib, _, name = lib_id.partition(":")
        try:
            if not lib or not name:
                raise sb.SymbolLibError("lib_id is not 'Library:Symbol'")
            etypes = sb.pin_etypes(sb.flatten(cache, symbols_dir, lib, name))
        except Exception as e:  # noqa: BLE001 - best-effort W2: name the lib_id, never crash lint
            unresolved.setdefault(lib_id, []).append(str(c["ref"]))
            reasons.setdefault(lib_id, str(e) if isinstance(e, sb.SymbolLibError)
                               else f"{type(e).__name__}: {e}")
            continue
        for num, etype in etypes.items():
            if etype == "power_in" and (c["ref"], str(num)) not in netted:
                warnings.append(f"W2 {c['ref']}.{num} is a power_in pin but not connected")
    if unresolved:
        detail = "; ".join(f"{lib_id} [{', '.join(refs)}]: {reasons[lib_id]}"
                           for lib_id, refs in unresolved.items())
        warnings.append(f"W2 skipped (symbol lookup failed: {detail})")
    return warnings


def main() -> int:
    design = json.load(open(sys.argv[1], encoding="utf-8"))
    symbols_dir = sys.argv[2] if len(sys.argv) > 2 else ""
    rep = lint(design, symbols_dir)
    print(json.dumps(rep, indent=2))
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
