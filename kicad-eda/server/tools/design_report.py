#!/usr/bin/env python3
"""Design-intent + DFA report for a pad-level netlist.

Where lint catches structural errors and DRC catches geometry violations, this
audits the things a reviewer checks by eye: is every IC decoupled, is the power
tree sound, are there cheap part-count reductions, are any pins left floating.
It reads design INTENT off the netlist (and pin electrical types off the KiCad
symbols) and reports — it does not modify the design.

DFA grounding: Boothroyd-Dewhurst — ~70% of assembly cost is fixed at design,
and part count is the dominant lever, so the report surfaces consolidation
opportunities (repeated passives -> arrays) and counts placements. Decoupling
and power-tree checks encode IPC/standard review practice.

Symbol pin types come from symlib (sexpdata). A symbol that cannot be resolved
is NOT silently treated as pin-less: it is listed in report["errors"] (and under
"## Errors" in the Markdown), because a pin-type-dependent check that runs
against an empty map passes vacuously.

Symbol sources (ROADMAP_DECISIONS D1): analyze() takes a symlib.SymbolSource --
a symbols directory (``<dir>/<nick>.kicad_sym``, the stock naming convention),
a libtable.LibraryResolver (netlist ``libraries`` block, project
``sym-lib-table``, then stock) or a library map dict
(LibraryResolver.library_map()). The command line resolves its <symbols>
argument through the netlist's resolver, as sch_build's does.

Stacked pins (D1, KLC S4.3): a hidden ``passive`` pin on the connection point
of an earlier pin of the same unit (sym_author stacks every power pin of a
name on the first one, e.g. the LG290P's 59 GND pads). KiCad joins them in
the schematic, but the board takes pad nets from the netlist, so a floating
stacked pin's floating_detail entry carries ``stacked_on`` and a ``hint``
that suggests netting the whole pin group at once (its pads listed; for a
sym_author part, the pin_groups entry of that name).
"""
from __future__ import annotations

import json
import os
import sys
from collections import defaultdict
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import NamedTuple

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import symlib  # noqa: E402
from symlib import SymbolSource  # noqa: E402

# how many identical passives before an array part is worth suggesting
_ARRAY_THRESHOLD = 4
# nets that should usually carry a pull-up/pull-down resistor
_PULL_HINTS = ("SDA", "SCL", "I2C", "NRST", "RESET", "BOOT")
# a pin the symbol itself marks no-connect/unconnected is intentionally open
_NC_TYPES = {"no_connect", "unconnected", "free", "not_connected"}
# pin names that carry no name (KiCad's "~")
_ANON_PIN_NAMES = ("~", "")
# a stacked pin (D1, KLC S4.3) is hidden and typed passive
_STACKED_TYPE = "passive"
_USAGE = "usage: design_report.py <netlist.json> [symbols_dir | library_map.json] [--json]"


class DesignReportError(Exception):
    """The netlist is malformed in a way the report cannot work around."""


class PinMaps(NamedTuple):
    """What the symbols say, per ref: {pad: electrical type}, {pin name: pad}
    (first name wins), {pad: pin name} (stacked pins too) and {stacked pad:
    the pad it is stacked on}; plus one error per unreadable lib_id."""
    types: dict
    aliases: dict
    names: dict
    stacked: dict
    errors: list


def _checked_source(source: object) -> SymbolSource | None:
    """``source`` when it names symbols, None for none ('' / None / an empty
    map: the caller chose not to supply symbols). TypeError for another kind."""
    if source is None:
        return None
    if isinstance(source, (str, os.PathLike)):
        return source if os.fspath(source) else None
    if isinstance(source, Mapping):
        return source or None
    if callable(getattr(source, "symbol_lib_path", None)):
        return source
    raise TypeError(f"symbol source must be a directory, a libtable.LibraryResolver or a "
                    f"library map dict, got {type(source).__name__}")


def _pin_names(flat: list) -> dict:
    """{pin number: pin name} over every unit of a flattened symbol, anonymous
    names left out. Unlike the alias map (first name wins), every STACKED pin
    keeps its shared name (D1: the LG290P's GND pads)."""
    names = {}
    for sub in symlib.kids(flat, "symbol"):
        for pin in symlib.kids(sub, "pin"):
            num, name = symlib.first(pin, "number"), symlib.first(pin, "name")
            if (num is not None and name is not None and isinstance(name[1], str)
                    and name[1] not in _ANON_PIN_NAMES):
                names[str(num[1])] = name[1]
    return names


def _hidden(pin: list) -> bool:
    """``(hide yes)`` (KiCad 9+) or the bare ``hide`` token (KiCad 6-8)."""
    node = symlib.first(pin, "hide")
    if node is not None:
        return len(node) < 2 or (isinstance(node[1], symlib.S) and node[1].value() == "yes")
    return any(isinstance(c, symlib.S) and c.value() == "hide" for c in pin[1:])


def _stacked_pins(flat: list) -> dict:
    """{pad: anchor pad} of a flattened symbol's stacked pins: per unit
    sub-symbol, a hidden passive pin on the connection point of an EARLIER pin
    of that sub-symbol (the anchor, normally the one visible pin)."""
    stacked: dict = {}
    for sub in [flat, *symlib.kids(flat, "symbol")]:
        anchors: dict = {}                          # (x, y) -> first pad there
        for pin in symlib.kids(sub, "pin"):
            at, num = symlib.first(pin, "at"), symlib.first(pin, "number")
            if at is None or num is None:
                continue
            pad = str(num[1])
            anchor = anchors.setdefault((round(float(at[1]), 4), round(float(at[2]), 4)), pad)
            etype = pin[1].value() if len(pin) > 1 and isinstance(pin[1], symlib.S) else ""
            if anchor != pad and etype == _STACKED_TYPE and _hidden(pin):
                stacked[pad] = anchor
    return stacked


def _symbol_maps(flat: list) -> tuple[dict, dict, dict, dict]:
    """(types, aliases, names, stacked) of one flattened symbol: {pad_num:
    electrical type}, {pin_name: pad_num} (first name wins), {pad_num:
    pin_name} and {stacked pad_num: anchor pad_num}."""
    aliases = {k: v["number"] for k, v in symlib.pin_offsets(flat).items()
               if v.get("number") and k != v["number"]}
    return symlib.pin_etypes(flat), aliases, _pin_names(flat), _stacked_pins(flat)


def _pin_types(design: dict, source: object) -> PinMaps:
    """PinMaps from the KiCad symbols of ``source`` (a directory, a
    LibraryResolver or a library map). The alias map lets a netlist that keys
    pads by pin NAME ("V+") resolve to the NUMBER the type map is keyed by; the
    name map names every pin (stacked ones too).

    No source ('' / None / an empty map) yields empty maps and no errors (the
    caller chose not to supply symbols; the report marks symbols_available
    False). A symbol that cannot be loaded/parsed is reported once per lib_id
    in ``errors`` and its refs are left OUT of the maps, so the caller can see
    exactly which parts the pin-type checks did not cover. A component with no
    usable ``lib_id`` is likewise reported and skipped (lint does not reject
    it, and the pipeline calls analyze() unconditionally, so it must not raise).

    Raises DesignReportError only when ``components`` is not a list of dicts,
    TypeError when ``source`` is none of the three kinds."""
    tables: tuple[dict, dict, dict, dict] = ({}, {}, {}, {})   # types, aliases, names, stacked
    errors: list = []
    symbols = _checked_source(source)
    if symbols is None:
        return PinMaps(*tables, errors)
    comps = design.get("components", [])
    if not isinstance(comps, list) or not all(isinstance(c, dict) for c in comps):
        raise DesignReportError("netlist 'components' must be a list of objects")
    cache: dict = {}
    failed: dict = {}                              # lib_id -> (reason, [refs])
    for c in comps:
        ref, lib_id = c.get("ref"), c.get("lib_id")
        if not ref or not lib_id:
            errors.append(f"pin types unavailable for {ref or '(no ref)'}: "
                          f"component has no lib_id ({c!r})")
            continue
        if lib_id in failed:
            failed[lib_id][1].append(ref)
            continue
        lib, _, name = lib_id.partition(":")
        try:
            per_ref = _symbol_maps(symlib.flatten(cache, symbols, lib, name))
        except (symlib.SymbolLibError, ValueError, IndexError, TypeError) as e:
            failed[lib_id] = (f"{type(e).__name__}: {e}", [ref])
            for table in tables:
                table.pop(ref, None)                # a duplicate ref read earlier
            continue
        for table, value in zip(tables, per_ref, strict=True):
            table[ref] = value
    for lib_id, (reason, refs) in failed.items():
        errors.append(f"pin types unavailable for {lib_id} "
                      f"({', '.join(refs)}): {reason}")
    return PinMaps(*tables, errors)


def _canon(aliases: dict, ref: str, pad) -> str:
    """The pin NUMBER for a netlist pad key that may be a pin name."""
    key = str(pad)
    return aliases.get(ref, {}).get(key, key)


def _prefix(ref: str) -> str:
    return "".join(ch for ch in ref if ch.isalpha())


def _same_name_net(ref: str, pad: str, name: str | None, pin_names: dict,
                   net_of: dict) -> str | None:
    """The one net the OTHER pins named ``name`` on ``ref`` sit on, else None
    (no such pin netted, or they disagree)."""
    if name is None:
        return None
    nets = {net_of[(ref, n)] for n, nm in pin_names.items()
            if nm == name and n != pad and (ref, n) in net_of}
    return next(iter(nets)) if len(nets) == 1 else None


def _pad_key(pad: str) -> tuple:
    """Numeric pads first, in number order; then the others by name."""
    return (0, int(pad), "") if pad.isascii() and pad.isdigit() else (1, 0, pad)


def _follows(prev: str, pad: str) -> bool:
    return _pad_key(prev)[0] == _pad_key(pad)[0] == 0 and int(pad) == int(prev) + 1


def _pad_ranges(pads: Iterable[str]) -> str:
    """Pad numbers sorted and compacted: "10, 12, 13, 24-79"."""
    runs: list[list[str]] = []
    for pad in sorted(set(pads), key=_pad_key):
        if runs and _follows(runs[-1][-1], pad):
            runs[-1].append(pad)
        else:
            runs.append([pad])
    return ", ".join(", ".join(r) if len(r) < 3 else f"{r[0]}-{r[-1]}" for r in runs)


def _stack_hint(entry: dict, maps: PinMaps, net_of: dict) -> dict:
    """{} for a pin that is not stacked; else {stacked_on, hint}: net the
    whole pin group at once -- the symbol's pads of that name (what a
    sym_author part returns as pin_groups[name]; a stock part has no such
    output, so the pads are always listed) or, unnamed, that stack's pads."""
    ref, pad, name = entry["ref"], entry["pad"], entry["name"]
    stacks = maps.stacked.get(ref, {})
    anchor = stacks.get(pad)
    if anchor is None:
        return {}
    net = net_of.get((ref, anchor)) or entry["same_name_net"]
    target = f"net {net}" if net else "one net"
    head = f"hidden pin stacked on {ref}.{anchor}"
    if name is not None:
        group = [p for p, n in maps.names.get(ref, {}).items() if n == name]
        todo = (f"{head} ('{name}'): put every pad named '{name}' ({_pad_ranges(group)}) "
                f"on {target} (a sym_author part returns them as pin_groups['{name}'])")
    else:
        group = [anchor, *(p for p, a in stacks.items() if a == anchor)]
        todo = (f"{head}: put every pad of the stack at {ref}.{anchor} "
                f"({_pad_ranges(group)}) on {target}")
    return {"stacked_on": anchor, "hint": (
        f"{todo}; KiCad joins stacked pins in the schematic, but the board takes pad nets "
        f"from the netlist")}


def _floating(comps: list, maps: PinMaps, net_of: dict, no_connect: set) -> tuple[list, list]:
    """(floating_pins, floating_detail), both sorted by "<ref>.<pad>": pins
    neither netted, nor declared no_connect, nor typed no-connect by the
    symbol. A detail entry is {pin, ref, pad, type, name, same_name_net,
    lib_id, footprint} (the napkin floating-pin policy's input, D5); a
    stacked pin's entry also has stacked_on and hint (_stack_hint)."""
    detail = []
    for c in comps:
        ref, pin_names = c["ref"], maps.names.get(c["ref"], {})
        for p, t in maps.types.get(ref, {}).items():
            pad = str(p)
            key = (ref, pad)
            if key in net_of or key in no_connect or t in _NC_TYPES:
                continue
            name = pin_names.get(pad)
            entry = {"pin": f"{ref}.{p}", "ref": ref, "pad": pad, "type": t, "name": name,
                     "same_name_net": _same_name_net(ref, pad, name, pin_names, net_of),
                     "lib_id": c.get("lib_id"), "footprint": c.get("footprint")}
            detail.append({**entry, **_stack_hint(entry, maps, net_of)})
    detail.sort(key=lambda d: d["pin"])
    return [d["pin"] for d in detail], detail


def analyze(design: dict, symbols_dir: SymbolSource | None = "", *,
            symbols: SymbolSource | None = None) -> dict:
    """The design review of ``design``. Pin types come from ``symbols_dir``,
    a symlib.SymbolSource: a directory, a libtable.LibraryResolver or a
    library map ('' = none: the pin-type checks are skipped). ``symbols``,
    when given, replaces it (the harness passes its stock directory
    positionally and a feature's resolver by keyword)."""
    comps = design.get("components", [])
    nets = design.get("nets", {})
    specs = design.get("net_specs", {})
    maps = _pin_types(design, symbols_dir if symbols is None else symbols)
    net_of = _net_index(nets, maps.aliases)
    no_connect = {(r, _canon(maps.aliases, r, p)) for r, p in design.get("no_connect", [])}
    dfa = _dfa(comps)
    decoupling = _decoupling(comps, maps.types, net_of)
    rails = _power_tree(nets, specs, maps)
    floating, floating_detail = _floating(comps, maps, net_of, no_connect)
    return {
        "title": design.get("title", design.get("name", "")),
        "dfa": dfa,
        "decoupling": decoupling,
        "power_tree": rails,
        "flags": {"floating_pins": floating,
                  "floating_detail": floating_detail,
                  "nets_missing_pull": _nets_missing_pull(nets),
                  "rails_missing_testpoint": _rails_missing_testpoint(nets, specs)},
        "symbols_available": bool(maps.types and any(maps.types.values())),
        "errors": maps.errors,
    }


def _net_index(nets: dict, aliases: dict) -> dict:
    """{(ref, pad number): net}; a pad keyed by pin name resolves to its number."""
    net_of = {}
    for net, conns in nets.items():
        for r, p in conns:
            net_of[(r, _canon(aliases, r, p))] = net
    return net_of


def _dfa(comps: list) -> dict:
    """BOM / DFA: placements, line items, and repeated passives -> arrays."""
    by_prefix: dict = defaultdict(int)
    line_items: dict = defaultdict(list)          # (value,footprint) -> [refs]
    for c in comps:
        by_prefix[_prefix(c["ref"])] += 1
        line_items[(c.get("value", ""), c.get("footprint", ""))].append(c["ref"])
    array_candidates = [
        {"value": v, "footprint": f, "count": len(rs), "refs": sorted(rs)}
        for (v, f), rs in line_items.items()
        if len(rs) >= _ARRAY_THRESHOLD and _prefix(rs[0]) in ("R", "C")
    ]
    return {
        "placements": len(comps),
        "unique_line_items": len(line_items),
        "by_prefix": dict(sorted(by_prefix.items())),
        "array_candidates": sorted(array_candidates, key=lambda a: -a["count"]),
    }


def _decoupling(comps: list, ptypes: dict, net_of: dict) -> dict:
    """Decoupling coverage: every IC power_in net should see a cap on it."""
    ref_nets: dict = defaultdict(set)
    for (r, _p), net in net_of.items():
        ref_nets[r].add(net)
    cap_nets: set = set()
    for c in comps:
        if _prefix(c["ref"]) == "C":
            cap_nets |= ref_nets[c["ref"]]
    decoupling: dict = {"checked": [], "uncovered": []}
    for c in comps:
        ref = c["ref"]
        pwr_nets = {net_of.get((ref, str(p)))
                    for p, t in ptypes.get(ref, {}).items() if t == "power_in"}
        pwr_nets.discard(None)
        if not pwr_nets:
            continue
        missing = sorted(n for n in pwr_nets if n not in cap_nets and n != "GND")
        covered = sorted(n for n in pwr_nets if n in cap_nets)
        decoupling["checked"].append(
            {"ref": ref, "rails": sorted(pwr_nets), "decoupled": covered})
        if missing:
            decoupling["uncovered"].append({"ref": ref, "rails_without_cap": missing})
    return decoupling


def _power_tree(nets: dict, specs: dict, maps: PinMaps) -> dict:
    """Each rail's driver (power_out/output) + load count."""
    ptypes, aliases = maps.types, maps.aliases
    rails = {}
    for net, conns in nets.items():
        drivers, loads = [], 0
        for r, p in conns:
            t = ptypes.get(r, {}).get(_canon(aliases, r, p), "")
            if t in ("power_out", "output"):
                drivers.append(f"{r}.{p}")
            else:
                loads += 1
        is_rail = net in specs or any(
            ptypes.get(r, {}).get(_canon(aliases, r, p)) == "power_in"
            for r, p in conns)
        if is_rail:
            rails[net] = {"drivers": drivers or ["(passive/flagged source)"],
                          "loads": loads,
                          "spec": specs.get(net, {})}
    return rails


def _nets_missing_pull(nets: dict) -> list:
    """Bus / reset nets (_PULL_HINTS) with no resistor on them, sorted."""
    return sorted(net for net in nets
                  if any(h in net.upper() for h in _PULL_HINTS)
                  and not any(_prefix(r) == "R" for r, _ in nets[net]))


def _rails_missing_testpoint(nets: dict, specs: dict) -> list:
    """Bring-up: every declared power rail (non-zero voltage) should carry a
    test point (TP*) so first-article power checks need not probe pads."""
    return sorted(net for net, s in specs.items()
                  if s.get("voltage", 0) and net in nets
                  and not any(_prefix(r) == "TP" for r, _ in nets[net]))


def to_markdown(rep: dict) -> str:
    L = [f"# Design Report — {rep['title']}", ""]
    if rep.get("errors"):
        L += ["## Errors",
              "The following symbols could not be read; decoupling, power-tree "
              "and floating-pin checks did NOT cover their parts:"]
        L += [f"- {e}" for e in rep["errors"]]
        L.append("")
    d = rep["dfa"]
    L += ["## DFA / BOM",
          f"- Placements: **{d['placements']}**  |  unique line items: "
          f"**{d['unique_line_items']}**",
          f"- By type: {', '.join(f'{k}×{v}' for k, v in d['by_prefix'].items())}"]
    if d["array_candidates"]:
        L.append("- **Consolidation opportunities** (repeated passives -> arrays):")
        for a in d["array_candidates"]:
            L.append(f"  - {a['count']}× `{a['value']}` {a['footprint'].split(':')[-1]} "
                     f"({', '.join(a['refs'])})")
    else:
        L.append("- No passive-array consolidation opportunities.")

    dec = rep["decoupling"]
    L += ["", "## Decoupling coverage"]
    if not dec["checked"]:
        L.append("- (no symbol pin types available — skipped)")
    elif dec["uncovered"]:
        for u in dec["uncovered"]:
            L.append(f"- [WARN] **{u['ref']}**: rail(s) without a bypass cap: "
                     f"{', '.join(u['rails_without_cap'])}")
    else:
        L.append(f"- [OK] all {len(dec['checked'])} powered IC(s) have a cap on every rail.")

    L += ["", "## Power tree"]
    for net, r in rep["power_tree"].items():
        spec = r["spec"]
        s = (f" — {spec.get('voltage')}V / {spec.get('current')}A"
             if spec.get("voltage") or spec.get("current") else "")
        L.append(f"- `{net}`{s}: source {', '.join(r['drivers'])} -> {r['loads']} load pin(s)")

    f = rep["flags"]
    L += ["", "## Flags"]
    L.append(f"- Floating pins: {', '.join(f['floating_pins']) or 'none'}")
    L += _stack_lines(f.get("floating_detail") or [])
    L.append(f"- Nets that may need a pull resistor: "
             f"{', '.join(f['nets_missing_pull']) or 'none'}")
    L.append(f"- Power rails without a test point: "
             f"{', '.join(f.get('rails_missing_testpoint', [])) or 'none'}")
    return "\n".join(L)


def _stack_lines(detail: list) -> list[str]:
    """One Markdown line per stack with floating pins (its hint once)."""
    stacks: dict = {}                              # (ref, anchor) -> [entries]
    for d in detail:
        if d.get("stacked_on") is not None:
            stacks.setdefault((d["ref"], d["stacked_on"]), []).append(d)
    return [f"- Stacked pins floating on {ref}: pad(s) "
            f"{_pad_ranges(d['pad'] for d in ds)} — {ds[0]['hint']}"
            for (ref, _anchor), ds in stacks.items()]


def _cli_source(netlist: str, symbols: str) -> SymbolSource:
    """The CLI's <symbols>: '' = none; a .json library map; else the stock
    symbols directory behind the netlist's resolver (sch_build's rule)."""
    if not symbols:
        return ""
    from sch_build import symbol_source
    return symbol_source(netlist, symbols)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else list(argv)
    as_json = "--json" in args
    paths = [a for a in args if a != "--json"]
    if not 1 <= len(paths) <= 2:
        print(_USAGE)
        return 2
    try:
        design = json.loads(Path(paths[0]).read_text(encoding="utf-8"))
        rep = analyze(design, _cli_source(paths[0], paths[1] if len(paths) > 1 else ""))
    except (symlib.SymbolLibError, DesignReportError, ValueError, OSError) as e:
        print(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}) if as_json
              else f"error: {type(e).__name__}: {e}")
        return 1
    print(json.dumps(rep, indent=2) if as_json else to_markdown(rep))
    return 1 if rep["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
