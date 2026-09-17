#!/usr/bin/env python3
"""Generate an openable, ERC-checkable .kicad_sch (KiCad 10, format 20250114) from
the pad-level netlist.json.

Approach: embed each used symbol into lib_symbols (flattening `extends` symbols so
inherited pins/graphics are present), place components on a grid, and drop a local
net label at every pin (same-named local labels are electrically connected on a
sheet). Power-source nets get a PWR_FLAG so ERC sees them driven. Unused pins get
no_connect flags.

Pin coordinates are derived from the flattened symbol itself (symlib.pin_offsets),
so no sidecar is needed. A ``<sidecars_dir>/<Lib_Symbol>.pins.json`` file, when
present, overrides the derived offset per key. A pin that resolves to NO
coordinate is an error: the script prints ``{"ok": false, "missing_pins": ...}``
and exits 1 instead of silently dropping the label. A sidecar key the symbol
itself does not have (a pad id from another KiCad version) is honoured but
listed in the success result's ``warnings``.

Usage:
  <venv-python> sch_build.py <netlist.json> <out.kicad_sch> <symbols_dir> [sidecars_dir]
"""
from __future__ import annotations

import json
import sys
import uuid as uuidlib
from pathlib import Path

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import symlib  # noqa: E402
from symlib import (SYM_ONLY_TAGS, S, SymbolLibError, dumps, embed, first,  # noqa: E402,F401
                    flatten, is_, kids, load_lib, pin_etypes, pin_numbers,
                    pin_offsets, unit_pins)

# public re-exports (lint_netlist / design_report / older callers import these)
__all__ = ["S", "SYM_ONLY_TAGS", "SymbolLibError", "dumps", "embed", "first",
           "flatten", "is_", "kids", "load_lib", "pin_etypes", "pin_numbers",
           "pin_offsets", "unit_pins", "uid", "load_sidecars", "main"]

PWR_FLAG_LIB_ID = "power:PWR_FLAG"
USAGE_EXIT = 2


def uid() -> str:
    return str(uuidlib.uuid4())


def load_sidecars(sidecars_dir: str, lib_ids: list) -> dict:
    """{lib_id: {key: {x, y}}} from ``<dir>/<Lib_Symbol>.pins.json`` files.
    A missing file means "no override" (empty map); a malformed one is an error."""
    out: dict = {}
    for lib_id in lib_ids:
        out[lib_id] = {}
        if not sidecars_dir:
            continue
        p = Path(sidecars_dir) / f"{lib_id.replace(':', '_')}.pins.json"
        if not p.is_file():
            continue
        try:
            out[lib_id] = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise ValueError(f"malformed pin sidecar {p}: {e}") from e
    return out


def build(netlist_path: str, out_path: str, symbols_dir: str,
          sidecars_dir: str = "") -> dict:
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
    pins_of, etypes_of, units_of, pin_unit_of, offsets_of = {}, {}, {}, {}, {}
    for lib_id in lib_ids + [PWR_FLAG_LIB_ID]:
        lib, _, name = lib_id.partition(":")
        flat = flatten(cache, symbols_dir, lib, name)
        pins_of[lib_id] = pin_numbers(flat)
        etypes_of[lib_id] = pin_etypes(flat)
        units_of[lib_id] = unit_pins(flat)
        pin_unit_of[lib_id] = {p: u for u, ps in units_of[lib_id].items() for p in ps}
        offsets_of[lib_id] = pin_offsets(flat)
        lib_blocks.append("\t\t" + dumps(embed(flat, lib_id)))

    # --- which nets need a PWR_FLAG: any net with a power-input pin but no
    #     power-output pin driving it (exactly KiCad's power_pin_not_driven rule) ---
    by_ref_lib = {c["ref"]: c["lib_id"] for c in design["components"]}

    def pin_type(ref, pad) -> str:
        """Electrical type of a pad key that may be a pin NUMBER or a pin NAME
        (the type map is number-keyed; a name resolves through its offset)."""
        lib_id = by_ref_lib.get(ref, "")
        key = str(pad)
        types = etypes_of.get(lib_id, {})
        if key not in types:
            key = str((offsets_of.get(lib_id, {}).get(key) or {}).get("number") or key)
        return types.get(key, "")

    drive_flag_nets = set()
    has_driver_net = set()      # nets with any output/power_out source pin
    for net, conns in design.get("nets", {}).items():
        types = [pin_type(ref, pad) for ref, pad in conns]
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

    # a sidecar, when present, overrides the symbol-derived offset per key
    sidecar = load_sidecars(sidecars_dir, lib_ids)

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
    missing_pins: list[list[str]] = []
    warnings: list[str] = []
    overridden: set = set()          # (lib_id, key) sidecar overrides reported

    def pin_xy(ref, pad):
        key = str(pad)
        comp = by_ref.get(ref)
        if comp is None:
            missing_pins.append([str(ref), key])
            return None
        lib_id = comp["lib_id"]
        derived = offsets_of[lib_id].get(key)
        override = sidecar[lib_id].get(key)
        off = override or derived
        # The unit comes from the pin NUMBER map when the key is a number; a pin
        # NAME (e.g. "V+" on a dual op-amp) carries its unit in the derived
        # offset. Defaulting to unit 1 would drop the label in the wrong cell.
        u = pin_unit_of[lib_id].get(key) or (derived or {}).get("unit", 1)
        cell = pos.get((ref, u))
        if cell is None or off is None:
            missing_pins.append([str(ref), key])
            return None
        if override and derived is None and (lib_id, key) not in overridden:
            overridden.add((lib_id, key))
            warnings.append(f"{ref}.{key}: not a pin of {lib_id} in this symbol "
                            f"library; sidecar override used (the board stage "
                            f"will report the pad missing if the footprint "
                            f"lacks it too)")
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

    # a label that silently vanished would leave a pin electrically open in an
    # ERC-"clean" schematic — refuse to write the file instead.
    if missing_pins:
        return {"ok": False,
                "error": f"{len(missing_pins)} pins have no coordinates",
                "missing_pins": missing_pins}

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
            f'\t\t(lib_id "{PWR_FLAG_LIB_ID}")',
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
    return {"ok": True, "sch": out_path,
            "components": len(design["components"]),
            "nets": len(design.get("nets", {})),
            "warnings": warnings}


def main() -> int:
    if len(sys.argv) < 4:
        print(__doc__)
        return USAGE_EXIT
    netlist_path, out_path, symbols_dir = sys.argv[1:4]
    sidecars_dir = sys.argv[4] if len(sys.argv) > 4 else ""
    try:
        res = build(netlist_path, out_path, symbols_dir, sidecars_dir)
    except (SymbolLibError, ValueError, KeyError, OSError) as e:
        res = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    print(json.dumps(res))
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
