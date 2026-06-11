#!/usr/bin/env python3
"""Generate an openable, ERC-checkable .kicad_sch (KiCad 10, format 20250114) from
the pad-level netlist.json.

Approach: embed each used symbol into lib_symbols (flattening `extends` symbols so
inherited pins/graphics are present), place components on a grid, and drop a local
net label at every pin (same-named local labels are electrically connected on a
sheet). Power-source nets get a PWR_FLAG so ERC sees them driven. Unused pins get
no_connect flags. Coordinates come from the same sidecars the wiring tools use.

Usage:
  <venv-python> sch_build.py <netlist.json> <out.kicad_sch> <symbols_dir> <sidecars_dir>
"""
from __future__ import annotations

import json
import sys
import uuid as uuidlib
from pathlib import Path

import sexpdata

S = sexpdata.Symbol


def is_(n, t):
    return isinstance(n, list) and n and isinstance(n[0], S) and n[0].value() == t


def kids(n, t):
    return [c for c in n if is_(c, t)]


def first(n, t):
    for c in n:
        if is_(c, t):
            return c
    return None


def uid():
    return str(uuidlib.uuid4())



def load_lib(symbols_dir, lib):
    data = sexpdata.loads(Path(f"{symbols_dir}/{lib}.kicad_sym").read_text(encoding="utf-8"))
    return {c[1]: c for c in kids(data, "symbol")}


def flatten(cache, symbols_dir, lib, name):
    """Return a fully-resolved symbol sexp, merging an `extends` base."""
    if lib not in cache:
        cache[lib] = load_lib(symbols_dir, lib)
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


# tags valid in a .kicad_sym symbol-editor library but NOT accepted inside a
# .kicad_sch <lib_symbols> block — they must be stripped or KiCad refuses to load.
SYM_ONLY_TAGS = {"in_pos_files"}


def embed(flat, lib_id):
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


def pin_numbers(flat):
    """All pin numbers declared in a flattened symbol's unit sub-symbols."""
    nums = []
    for sub in kids(flat, "symbol"):
        for pin in kids(sub, "pin"):
            num = first(pin, "number")
            if num is not None and num[1] not in nums:
                nums.append(num[1])
    return nums


def pin_etypes(flat):
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


def unit_pins(flat):
    """Map unit number -> [pin numbers], parsed from sub-symbol names of the form
    ``<bare>_<unit>_<bodystyle>``. Unit 0 (common-to-all-units graphics) is folded
    into unit 1. A single-unit part returns {1: [...]}; a dual op-amp returns
    {1: [...], 2: [...], 3: [...]} (the 3rd being the power unit)."""
    bare = flat[1]
    units: dict[int, list] = {}
    for sub in kids(flat, "symbol"):
        rest = str(sub[1])[len(bare):].strip("_").split("_")
        try:
            u = int(rest[0])
        except (ValueError, IndexError):
            u = 1
        if u == 0:
            u = 1
        for pin in kids(sub, "pin"):
            num = first(pin, "number")
            if num is not None:
                units.setdefault(u, [])
                if num[1] not in units[u]:
                    units[u].append(num[1])
    return units or {1: []}


def dumps(node):
    return sexpdata.dumps(node)


def main():
    netlist_path, out_path, symbols_dir, sidecars_dir = sys.argv[1:5]
    design = json.load(open(netlist_path, encoding="utf-8"))
    root = uid()
    project = design.get("name", "design")

    # --- collect unique lib_ids and embed (flattened) symbols + PWR_FLAG ---
    cache = {}
    lib_ids = []
    for c in design["components"]:
        if c["lib_id"] not in lib_ids:
            lib_ids.append(c["lib_id"])
    lib_blocks = []
    pins_of, etypes_of, units_of, pin_unit_of = {}, {}, {}, {}
    for lib_id in lib_ids + ["power:PWR_FLAG"]:
        lib, _, name = lib_id.partition(":")
        flat = flatten(cache, symbols_dir, lib, name)
        pins_of[lib_id] = pin_numbers(flat)
        etypes_of[lib_id] = pin_etypes(flat)
        units_of[lib_id] = unit_pins(flat)
        pin_unit_of[lib_id] = {p: u for u, ps in units_of[lib_id].items() for p in ps}
        lib_blocks.append("\t\t" + dumps(embed(flat, lib_id)))

    # --- which nets need a PWR_FLAG: any net with a power-input pin but no
    #     power-output pin driving it (exactly KiCad's power_pin_not_driven rule) ---
    by_ref_lib = {c["ref"]: c["lib_id"] for c in design["components"]}
    drive_flag_nets = set()
    has_driver_net = set()      # nets with any output/power_out source pin
    for net, conns in design.get("nets", {}).items():
        types = [etypes_of.get(by_ref_lib.get(ref, ""), {}).get(str(pad), "")
                 for ref, pad in conns]
        has_pwr_in = any(t == "power_in" for t in types)
        has_pwr_out = any(t == "power_out" for t in types)
        if any(t in ("power_out", "output") for t in types):
            has_driver_net.add(net)
        if has_pwr_in and not has_pwr_out:
            drive_flag_nets.add(net)
    # A regulator's *output* rail is driven through passives (the inductor /
    # divider), so its feedback pin reads as an undriven input to ERC even though
    # it is a real supply. Declaring the rail electrically — in net_specs (a real
    # voltage/current) or an explicit power_flags list — makes it a power source,
    # exactly as dropping a PWR_FLAG on the regulator output does in KiCad. Skip
    # any rail that already has an output/power_out driver, or the flag and the
    # driver both source the net and ERC reports a conflict.
    specs = design.get("net_specs", {})
    declared = {n for n, s in specs.items()
                if s.get("voltage", 0) or s.get("current", 0)}
    declared |= set(design.get("power_flags", []))
    drive_flag_nets |= {n for n in declared
                        if n in design.get("nets", {}) and n not in has_driver_net}

    sidecar = {}
    for lib_id in lib_ids:
        safe = lib_id.replace(":", "_")
        p = Path(sidecars_dir) / f"{safe}.pins.json"
        sidecar[lib_id] = json.loads(p.read_text()) if p.is_file() else {}

    # --- grid layout: one cell per (component, unit). Multi-unit parts (op-amps,
    #     logic) place each unit separately, all sharing one reference. Origins/
    #     spacings are multiples of 1.27 mm so pins land on KiCad's connection grid.
    by_ref = {c["ref"]: c for c in design["components"]}
    cells = []  # (ref, unit) in placement order
    for c in design["components"]:
        for u in sorted(units_of[c["lib_id"]]):
            cells.append((c["ref"], u))
    pos = {}
    cols, gx, gy, x0, y0 = 5, 63.5, 50.8, 50.8, 38.1
    for i, key in enumerate(cells):
        pos[key] = (round(x0 + (i % cols) * gx, 2), round(y0 + (i // cols) * gy, 2))

    L = ['(kicad_sch', '\t(version 20250114)', '\t(generator "kicad-eda")',
         '\t(generator_version "10.0")', f'\t(uuid "{root}")', '\t(paper "A4")',
         '\t(lib_symbols']
    L += lib_blocks
    L.append('\t)')

    # --- component instances (one placement symbol per unit) ---
    for ref, u in cells:
        c = by_ref[ref]
        x, y = pos[(ref, u)]
        fp = c.get("footprint", "")
        L += [
            '\t(symbol',
            f'\t\t(lib_id "{c["lib_id"]}")',
            f'\t\t(at {x} {y} 0)',
            f'\t\t(unit {u})',
            '\t\t(exclude_from_sim no)', '\t\t(in_bom yes)', '\t\t(on_board yes)', '\t\t(dnp no)',
            f'\t\t(uuid "{uid()}")',
            f'\t\t(property "Reference" "{c["ref"]}" (at {x} {y - 7.62} 0)'
            ' (effects (font (size 1.27 1.27))))',
            f'\t\t(property "Value" "{c.get("value","")}" (at {x} {y + 7.62} 0)'
            ' (effects (font (size 1.27 1.27))))',
            f'\t\t(property "Footprint" "{fp}" (at {x} {y} 0)'
            ' (effects (font (size 1.27 1.27)) (hide yes)))',
        ]
        for num in units_of[c["lib_id"]].get(u, []):
            L.append(f'\t\t(pin "{num}" (uuid "{uid()}"))')
        L += [
            f'\t\t(instances (project "{project}" (path "/{root}"'
            f' (reference "{c["ref"]}") (unit {u}))))',
            '\t)',
        ]

    # --- labels at every pin (resolved to the pin's own unit placement) ---
    def pin_xy(ref, pad):
        lib_id = by_ref[ref]["lib_id"]
        u = pin_unit_of[lib_id].get(str(pad), 1)
        cell = pos.get((ref, u))
        off = sidecar[lib_id].get(str(pad))
        if cell is None or off is None:
            return None
        return round(cell[0] + off["x"], 2), round(cell[1] + off["y"], 2)

    for net, conns in design.get("nets", {}).items():
        for ref, pad in conns:
            xy = pin_xy(ref, pad)
            if xy is None:
                continue
            L.append(f'\t(label "{net}" (at {xy[0]} {xy[1]} 0)'
                     ' (effects (font (size 1.27 1.27)) (justify left bottom))'
                     f' (uuid "{uid()}"))')

    # --- no_connect flags ---
    for ref, pad in design.get("no_connect", []):
        xy = pin_xy(ref, pad)
        if xy is None:
            continue
        L.append(f'\t(no_connect (at {xy[0]} {xy[1]}) (uuid "{uid()}"))')

    # --- PWR_FLAG per undriven power net (placed in a column on the right;
    #     grid-aligned to keep pins on the 1.27 mm connection grid) ---
    fx, fy = 363.22, 38.1
    flagn = 0
    for net in sorted(drive_flag_nets):
        if net not in design.get("nets", {}):
            continue
        flagn += 1
        ref = f"#FLG{flagn:02d}"
        L += [
            '\t(symbol',
            '\t\t(lib_id "power:PWR_FLAG")',
            f'\t\t(at {fx} {fy} 0)', '\t\t(unit 1)',
            '\t\t(exclude_from_sim no)', '\t\t(in_bom yes)', '\t\t(on_board yes)', '\t\t(dnp no)',
            f'\t\t(uuid "{uid()}")',
            f'\t\t(property "Reference" "{ref}" (at {fx} {fy-2.54} 0) (effects (font (size 1.27 1.27)) (hide yes)))',
            f'\t\t(property "Value" "PWR_FLAG" (at {fx} {fy+2.54} 0) (effects (font (size 1.27 1.27))))',
            f'\t\t(pin "1" (uuid "{uid()}"))',
            f'\t\t(instances (project "{project}" (path "/{root}" (reference "{ref}") (unit 1))))',
            '\t)',
            f'\t(label "{net}" (at {fx} {fy} 0) (effects (font (size 1.27 1.27))) (uuid "{uid()}"))',
        ]
        fy += 15.24

    # --- required top-level closers ---
    L += [
        '\t(sheet_instances',
        '\t\t(path "/" (page "1"))',
        '\t)',
        '\t(embedded_fonts no)',
        ')',
    ]
    Path(out_path).write_text("\n".join(L) + "\n", encoding="utf-8")
    print(json.dumps({"ok": True, "sch": out_path,
                      "components": len(design["components"]),
                      "nets": len(design.get("nets", {}))}))


if __name__ == "__main__":
    main()
