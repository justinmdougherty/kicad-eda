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
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import symlib  # noqa: E402

# how many identical passives before an array part is worth suggesting
_ARRAY_THRESHOLD = 4
# nets that should usually carry a pull-up/pull-down resistor
_PULL_HINTS = ("SDA", "SCL", "I2C", "NRST", "RESET", "BOOT")
# a pin the symbol itself marks no-connect/unconnected is intentionally open
_NC_TYPES = {"no_connect", "unconnected", "free", "not_connected"}
# pin names that carry no name (KiCad's "~")
_ANON_PIN_NAMES = ("~", "")


class DesignReportError(Exception):
    """The netlist is malformed in a way the report cannot work around."""


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


def _symbol_maps(flat: list) -> tuple[dict, dict, dict]:
    """(types, aliases, names) of one flattened symbol: {pad_num: electrical
    type}, {pin_name: pad_num} (first name wins) and {pad_num: pin_name}."""
    aliases = {k: v["number"] for k, v in symlib.pin_offsets(flat).items()
               if v.get("number") and k != v["number"]}
    return symlib.pin_etypes(flat), aliases, _pin_names(flat)


def _pin_types(design: dict, symbols_dir: str) -> tuple[dict, dict, dict, list]:
    """({ref: {pad_num: electrical_type}}, {ref: {pin_name: pad_num}},
    {ref: {pad_num: pin_name}}, errors) from the KiCad symbols. The alias map
    lets a netlist that keys pads by pin NAME ("V+") resolve to the NUMBER the
    type map is keyed by; the name map names every pin (stacked ones too).

    An empty symbols_dir yields an empty map and no errors (the caller chose
    not to supply symbols; the report marks symbols_available False). A symbol
    that cannot be loaded/parsed is reported once per lib_id in ``errors`` and
    its refs are left OUT of the map, so the caller can see exactly which
    parts the pin-type checks did not cover. A component with no usable
    ``lib_id`` is likewise reported and skipped (lint does not reject it, and
    the pipeline calls analyze() unconditionally, so it must not raise).

    Raises DesignReportError only when ``components`` is not a list of dicts."""
    out: dict = {}
    aliases: dict = {}
    names: dict = {}
    errors: list = []
    if not symbols_dir:
        return out, aliases, names, errors
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
            flat = symlib.flatten(cache, symbols_dir, lib, name)
            out[ref], aliases[ref], names[ref] = _symbol_maps(flat)
        except (symlib.SymbolLibError, ValueError, IndexError, TypeError) as e:
            for m in (out, aliases, names):
                m.pop(ref, None)
            failed[lib_id] = (f"{type(e).__name__}: {e}", [ref])
    for lib_id, (reason, refs) in failed.items():
        errors.append(f"pin types unavailable for {lib_id} "
                      f"({', '.join(refs)}): {reason}")
    return out, aliases, names, errors


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


def _floating(comps: list, ptypes: dict, names: dict, net_of: dict,
              no_connect: set) -> tuple[list, list]:
    """(floating_pins, floating_detail), both sorted by "<ref>.<pad>": pins
    neither netted, nor declared no_connect, nor typed no-connect by the
    symbol. A detail entry is {pin, ref, pad, type, name, same_name_net,
    lib_id, footprint} (the napkin floating-pin policy's input, D5)."""
    detail = []
    for c in comps:
        ref, pin_names = c["ref"], names.get(c["ref"], {})
        for p, t in ptypes.get(ref, {}).items():
            pad = str(p)
            key = (ref, pad)
            if key in net_of or key in no_connect or t in _NC_TYPES:
                continue
            name = pin_names.get(pad)
            detail.append({"pin": f"{ref}.{p}", "ref": ref, "pad": pad, "type": t,
                           "name": name,
                           "same_name_net": _same_name_net(ref, pad, name, pin_names, net_of),
                           "lib_id": c.get("lib_id"), "footprint": c.get("footprint")})
    detail.sort(key=lambda d: d["pin"])
    return [d["pin"] for d in detail], detail


def analyze(design: dict, symbols_dir: str = "") -> dict:
    comps = design.get("components", [])
    nets = design.get("nets", {})
    ptypes, aliases, names, errors = _pin_types(design, symbols_dir)
    net_of = {}                                   # (ref,pad_number) -> net
    for net, conns in nets.items():
        for r, p in conns:
            net_of[(r, _canon(aliases, r, p))] = net
    ref_nets = defaultdict(set)
    for (r, _p), net in net_of.items():
        ref_nets[r].add(net)

    # --- BOM / DFA ---
    by_prefix = defaultdict(int)
    line_items = defaultdict(list)                # (value,footprint) -> [refs]
    for c in comps:
        by_prefix[_prefix(c["ref"])] += 1
        line_items[(c.get("value", ""), c.get("footprint", ""))].append(c["ref"])
    array_candidates = [
        {"value": v, "footprint": f, "count": len(rs), "refs": sorted(rs)}
        for (v, f), rs in line_items.items()
        if len(rs) >= _ARRAY_THRESHOLD and _prefix(rs[0]) in ("R", "C")
    ]
    dfa = {
        "placements": len(comps),
        "unique_line_items": len(line_items),
        "by_prefix": dict(sorted(by_prefix.items())),
        "array_candidates": sorted(array_candidates, key=lambda a: -a["count"]),
    }

    # --- decoupling coverage: every IC power_in net should see a cap on it ---
    cap_nets = set()
    for c in comps:
        if _prefix(c["ref"]) == "C":
            cap_nets |= ref_nets[c["ref"]]
    decoupling = {"checked": [], "uncovered": []}
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

    # --- power tree: each rail's driver (power_out/output) + load count ---
    rails = {}
    specs = design.get("net_specs", {})
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

    # --- intent flags: floating pins + missing pulls ---
    no_connect = {(r, _canon(aliases, r, p)) for r, p in design.get("no_connect", [])}
    floating, floating_detail = _floating(comps, ptypes, names, net_of, no_connect)
    pulls = []
    for net in nets:
        if any(h in net.upper() for h in _PULL_HINTS):
            on_net_res = any(_prefix(r) == "R" for r, _ in nets[net])
            if not on_net_res:
                pulls.append(net)

    # bring-up: every declared power rail should carry a test point (TP*) so
    # first-article power checks don't need to probe component pads
    tp_missing = []
    for net, s in specs.items():
        if s.get("voltage", 0) and net in nets:
            if not any(_prefix(r) == "TP" for r, _ in nets[net]):
                tp_missing.append(net)

    return {
        "title": design.get("title", design.get("name", "")),
        "dfa": dfa,
        "decoupling": decoupling,
        "power_tree": rails,
        "flags": {"floating_pins": floating,
                  "floating_detail": floating_detail,
                  "nets_missing_pull": sorted(pulls),
                  "rails_missing_testpoint": sorted(tp_missing)},
        "symbols_available": bool(ptypes and any(ptypes.values())),
        "errors": errors,
    }


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
    L.append(f"- Nets that may need a pull resistor: "
             f"{', '.join(f['nets_missing_pull']) or 'none'}")
    L.append(f"- Power rails without a test point: "
             f"{', '.join(f.get('rails_missing_testpoint', [])) or 'none'}")
    return "\n".join(L)


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: design_report.py <netlist.json> [symbols_dir] [--json]")
        return 2
    design = json.load(open(sys.argv[1], encoding="utf-8"))
    symbols_dir = sys.argv[2] if len(sys.argv) > 2 and sys.argv[2] != "--json" else ""
    rep = analyze(design, symbols_dir)
    if "--json" in sys.argv:
        print(json.dumps(rep, indent=2))
    else:
        print(to_markdown(rep))
    return 1 if rep["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
