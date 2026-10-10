# Custom parts in the headless flow (project libraries)

Working summary for authoring a netlist whose parts are not all in KiCad's
stock libraries. The reference (resolution order, managed table rows, pin
sidecars, LIB-* lint codes, route-cache digest, KiCad 9/10 dialect limits) is
`${CLAUDE_PLUGIN_ROOT}/docs/LIBRARIES.md`.

## The flow for a part with no stock symbol or footprint

1. **Stock first.** A stock symbol with the right, datasheet-verified pin map
   beats a generated one; `build_pin_sidecars(symbols="Lib:Symbol")` fails
   for a symbol id stock lacks. Generate only what stock does not have.
2. **netlist.json first.** The library tools take its path (`netlist`) and
   register what they write in its `libraries` block. Before the full
   netlist exists, a minimal `{"name": "<board>"}` file is enough; keep the
   `libraries` block they add when you write the rest.
3. **Footprint**, from a preset or a template (section "Presets" /
   "Templates" below); it lands in `libs/<name>_parts.pretty`, nickname
   `<name>_parts`, unless `library` names another.
4. **Symbol**: `author_symbol(netlist, preset="lg290p")` or
   `author_symbol(netlist, spec={...}, footprint="<nick>:<footprint name>")`;
   it lands in `libs/<name>_parts.kicad_sym` with its pin sidecar in
   `<netlist dir>/pins/`.
5. **Use the part** with the nicknames the tools returned (`lib_id` of each
   result). Net it by pad NUMBER (the board build matches pad numbers; a pin
   name used as a pad key is LIB-E4). Put every number of
   `pin_groups["GND"]` (and of any other group of one net) on that net: the
   stacked copies are hidden on the symbol and float otherwise; the RESERVED
   group goes to `no_connect`.
6. **Apply a preset's `netlist_hints`** once the part has its placement
   (section "Presets").
7. **Check**: `resolve_libraries(netlist)` (every nickname's source and
   errors), then lint (the LIB-* rules run against the project libraries).
8. **Record** each part choice:
   `record_decision(netlist, stage="parts", question="...", decision="...", rationale="...", confidence="high", source="<datasheet URL>", alternatives=["..."])`.

Every authored file is validated before it is written (the oldest installed
kicad-cli must re-read it, pcbnew must load the footprint); a missing KiCad is
a failure, never a pass. A different existing file is replaced only with
`overwrite=True`; a rerun with the same input reports `state` "unchanged".
`register=False` skips the `libraries` block and the tables.

## The `libraries` block

```json
"libraries": {
  "symbols":    {"SPS_parts": "libs/SPS_parts.kicad_sym"},
  "footprints": {"SPS_parts": "libs/SPS_parts.pretty"}
}
```

Paths are relative to the netlist's directory (`${KIPRJMOD}`; absolute works).
A nickname resolves in the order block > project tables (`sym-lib-table` /
`fp-lib-table` beside the netlist) > stock. The same nickname with another
path in a project table is a G1 error; so is a declared library whose file is
missing (it never falls back to stock).

## Presets

Part presets carry a datasheet-checked footprint and symbol (pad number = pin
number) for the SPS GNSS modules:

| Preset | Footprint | Symbol | Notes |
|---|---|---|---|
| `lg290p` | `Quectel_LG290P` (LGA-79) | `LG290P` | 59 GND pads stacked on the symbol; `netlist_hints`: 40 GND escape vias with two F.Cu stubs each (80 preroutes) |
| `lc29h` | `Quectel_LC29H` (LCC-24) | `LC29H` | no inner pad grid, so no hints; I/O at 2.8 V |
| `lg290p_lc29h` | `Quectel_LG290P_LC29H_StencilLG290P` / `_StencilLC29H` | `LG290P_LC29H` | opt-in land pattern for either module; the conflicting pins are N/C |

```
author_footprint(netlist, preset="lg290p")
author_symbol(netlist, preset="lg290p")
```

- `author_footprint(netlist, preset="lg290p_lc29h", params={"stencil": "lc29h"}, opt_in=True)`
  takes the combined pattern; separate footprints stay the default.
- A preset's result carries `pin_groups` (e.g. `GND` -> its 59 pad numbers),
  `strategy` (the F2-S1 measurement behind the hints) and `netlist_hints`:
  the LGA inner-pad breakout as netlist `preroutes` in the FOOTPRINT frame
  for part U1. Place them at the part once it has `at` (or x/y):

```python
import json, sys
sys.path.insert(0, r"<plugin root>/server/tools")
import lga_strategy

design = json.load(open("netlist.json", encoding="utf-8"))
design = lga_strategy.apply_hints(design, hints, ref="U1")   # hints = netlist_hints
```

  `apply_hints` returns a new netlist with the preroutes appended (front-side
  parts only). Keep the builder's planes (In1.Cu GND); never pour GND on
  F.Cu or B.Cu before routing.
- `part_presets.preset_info(name)` describes a preset (pin notes, conflicts,
  hints) without writing anything.

## Templates

Without a preset, a footprint comes from a template and its keyword
arguments as a JSON object (`params`); the footprint name is `name`:

| Template | Required params |
|---|---|
| `qfn` | body, pitch, pins_x, pins_y, span_x, span_y, pad_w, pad_l (optional `ep`) |
| `dfn` | body, pitch, pins_per_side, span, pad_w, pad_l (optional `ep`) |
| `sop` | body, pins, pitch, span, pad_w, pad_l |
| `lcc_castellated` | body, pins, pitch, pad_w, pad_l |
| `lga_grid` | body (plus `grid`, `edge_rows`, `numbering`) |
| `pin_header` | rows (optional cols, pitch, drill, pad) |
| `lg290p` | none (the preset's fixed geometry) |

`pad_w` runs along the pitch, `pad_l` across it; `span` is the centre-to-
centre distance of opposite rows. A 6-pin 2 x 2 mm DFN and its symbol:

```
author_footprint(netlist, template="dfn", params={"body": {"w": 2.0, "h": 2.0}, "pins_per_side": 3, "pitch": 0.65, "span": 1.9, "pad_w": 0.35, "pad_l": 0.6}, name="DFN-6_2x2mm_P0.65mm")
author_symbol(netlist, spec={"name": "SENSOR_X", "reference": "U", "pins": [{"number": "1", "name": "SCL", "type": "input"}, {"number": "2", "name": "GND", "type": "power_in"}, {"number": "3", "name": "ALERT", "type": "output"}, {"number": "4", "name": "ADD0", "type": "input"}, {"number": "5", "name": "V+", "type": "power_in"}, {"number": "6", "name": "SDA", "type": "bidirectional"}]}, footprint="<name>_parts:DFN-6_2x2mm_P0.65mm")
```

Pin `type` is a KiCad electrical type (input, output, bidirectional,
tri_state, passive, power_in, power_out, open_collector, open_emitter,
no_connect, free, unspecified); `side` (left, right, top, bottom) and `group`
are optional. A malformed footprint `params` or symbol `spec` is refused
(nothing written), every problem named; footprint problems name their JSON
path, e.g. `params.body`.

## Checking a library from elsewhere

- `validate_library(path, expected_pads=79)`: a `.pretty`, `.kicad_mod` or
  `.kicad_sym` loaded by KiCad itself. A footprint newer than KiCad 9's
  dialect (`(version N)` above 20241229) is a problem here: KiCad 9's pcbnew
  cannot load it (convert it, see the reference).
- `compare_footprints(a, b, netlist)`: a generated footprint against a
  vendor's (`nick:name` lib_ids or .kicad_mod paths), pad by pad.
- `resolve_libraries(netlist, materialize=True)` writes the managed table rows
  beside the netlist (for opening the project in KiCad).
