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

Pure standard library; symbol pin types via sch_build.flatten (best-effort).
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

# how many identical passives before an array part is worth suggesting
_ARRAY_THRESHOLD = 4
# nets that should usually carry a pull-up/pull-down resistor
_PULL_HINTS = ("SDA", "SCL", "I2C", "NRST", "RESET", "BOOT")


def _pin_types(design: dict, symbols_dir: str) -> dict:
    """{ref: {pad_num: electrical_type}} from the KiCad symbols, best-effort."""
    out: dict = {}
    if not symbols_dir:
        return out
    try:
        sys.path.insert(0, str(Path(__file__).parent))
        import sch_build as sb
        cache: dict = {}
        for c in design.get("components", []):
            lib, _, name = c["lib_id"].partition(":")
            try:
                flat = sb.flatten(cache, symbols_dir, lib, name)
                out[c["ref"]] = sb.pin_etypes(flat)
            except Exception:
                out[c["ref"]] = {}
    except Exception:
        pass
    return out


def _prefix(ref: str) -> str:
    return "".join(ch for ch in ref if ch.isalpha())


def analyze(design: dict, symbols_dir: str = "") -> dict:
    comps = design.get("components", [])
    nets = design.get("nets", {})
    by_ref = {c["ref"]: c for c in comps}
    ptypes = _pin_types(design, symbols_dir)
    net_of = {}                                   # (ref,pad) -> net
    for net, conns in nets.items():
        for r, p in conns:
            net_of[(r, str(p))] = net
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
            t = ptypes.get(r, {}).get(str(p), "")
            if t in ("power_out", "output"):
                drivers.append(f"{r}.{p}")
            else:
                loads += 1
        is_rail = net in specs or any(ptypes.get(r, {}).get(str(p)) == "power_in"
                                      for r, p in conns)
        if is_rail:
            rails[net] = {"drivers": drivers or ["(passive/flagged source)"],
                          "loads": loads,
                          "spec": specs.get(net, {})}

    # --- intent flags: floating pins + missing pulls ---
    no_connect = {(r, str(p)) for r, p in design.get("no_connect", [])}
    # a pin the symbol itself marks no-connect/unconnected is intentionally open
    _nc_types = {"no_connect", "unconnected", "free", "not_connected"}
    floating = []
    for c in comps:
        for p, t in ptypes.get(c["ref"], {}).items():
            key = (c["ref"], str(p))
            if key not in net_of and key not in no_connect and t not in _nc_types:
                floating.append(f"{c['ref']}.{p}")
    pulls = []
    has_res = any(_prefix(c["ref"]) == "R" for c in comps)
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
        "flags": {"floating_pins": sorted(floating),
                  "nets_missing_pull": sorted(pulls),
                  "rails_missing_testpoint": sorted(tp_missing)},
        "symbols_available": bool(ptypes and any(ptypes.values())),
    }


def to_markdown(rep: dict) -> str:
    L = [f"# Design Report — {rep['title']}", ""]
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
    design = json.load(open(sys.argv[1], encoding="utf-8"))
    symbols_dir = sys.argv[2] if len(sys.argv) > 2 else ""
    rep = analyze(design, symbols_dir)
    if "--json" in sys.argv:
        print(json.dumps(rep, indent=2))
    else:
        print(to_markdown(rep))
    return 0


if __name__ == "__main__":
    sys.exit(main())
