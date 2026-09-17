---
name: netlist-pipeline
description: >-
  Headless concept-to-Gerbers PCB flow: author a pad-level netlist.json, then
  drive it through five self-remediating review gates (lint -> schematic+ERC
  -> SWIG placement -> Freerouting+DRC -> fab package). Use this for any board
  that should be produced WITHOUT the KiCad GUI — including fixed-outline,
  two-sided, and high-current designs. For the GUI-assisted 10-stage flow use
  the pcb-design skill instead.
---

# Headless netlist-first pipeline

The complete run-book lives at `${CLAUDE_PLUGIN_ROOT}/docs/PIPELINE.md` —
**read it before authoring**. This skill is the working summary.

## The flow

```
netlist.json -> G1 lint -> G2 schematic+ERC -> G3 placement -> G4 routing -> G5 fab zip
```

Run: `<venv-python> ${CLAUDE_PLUGIN_ROOT}/pipeline/run_pipeline.py <netlist.json> <w> <h>`
Regression: `<venv-python> ${CLAUDE_PLUGIN_ROOT}/pipeline/run_all.py` (examples/)

Gates self-feed: missing bypass caps are auto-added (G2), courtyard overlap
re-places at a looser pack scale (G3), same-net micro-gaps and plane islands
are healed and under-ampacity transitions get parallel vias (G4). Only
design-intent blockers stop the run — fix them in the netlist and re-run.
Never hand-edit the generated board to silence a gate.

## MCP tools for the headless flow

`run_pipeline.py` is primary (it owns the remediation loop). The same steps
exist as `kicad-generator` MCP tools for driving a board one stage at a time:

- `build_schematic_from_netlist(netlist, out_sch, symbols_dir, sidecars_dir, run_erc)` — G2: ERC-clean `.kicad_sch` (embedded symbols, no_connects, PWR_FLAGs); fails with `missing_pins` when a (ref, pad) resolves to no coordinate.
- `design_report(netlist)` — G2 design-intent review (decoupling, power tree, floating pins, pulls); writes `<netlist>_report.md`.
- `build_board_from_netlist(netlist, board_w, board_h, out_pcb, footprints_dir, placement, pack_scale)` — G3: placed board, planes, Edge.Cuts, IPC net classes written to the `.kicad_pro`.
- `check_design_gates(pcb, netlist, stage="placement"|"routed")` — the G3 / G4 review; `passed` must be true before the next step.
- `autoroute_board(pcb, max_passes, jar, route_timeout)` — G4: Freerouting DSN→SES round trip, zones refilled; re-run until `unconnected` is 0.
- `add_ground_stitching(pcb, gnd_net, max_freq_hz, er, interior, via_diameter_mm, via_drill_mm)` — transactional GND via fence/grid on a DRC-clean routed board.
- `export_fab_package(pcb, out_dir, zip_name, netlist)` — G5: Gerbers, drill, CPL, BOM, zipped.
- `build_pin_sidecars("Lib:Symbol,...")` — optional: both flows derive pin coordinates from the symbol; a sidecar in `server/pins/` overrides for both. Still a valid symbol-id check (exits 1 on an unknown symbol).
- `server_status()` — kicad-cli, bundled Python, Java/Freerouting, IPC, open documents, capabilities.

Every writer above refuses a file that is open in a KiCad editor — close it
first (`test_kicad_connection().open_documents` shows what is open).

## Authoring rules that decide success

1. **Pad-correct nets.** Exposed pads have their own pad number (QFN-24 EP =
   pin 25). `Device:LED` pin 1 = cathode. Verify real pin maps against the
   datasheet, not the symbol name.
2. **Design intent in `net_specs`**: `{"5V": {"voltage":5, "current":4}}`
   drives IPC-2221/2141A track widths, via sizes, and the G4 ampacity gate.
   Spec CONTINUOUS current — bursts ride the bulk caps (IPC-2152).
3. **Preroute every high-current path** (`"preroutes"`): Freerouting 1.9
   cannot neck down, so a wide width-class into a small pad is THE
   unroutable pattern. Prerouted and plane nets are width-class-exempt; the
   router connects their remaining taps at signal width.
4. **Hand-place the power chain in current-flow order** with fixed `x/y/rot`
   on those components; let the auto-placer handle the rest. Fixed-outline
   boards declare `"board": {w, h, auto_x/y/w/h, auto_b}` (free fields for
   auto-placed top/bottom parts).
5. `"side": "B"` puts a part on the bottom; `"interior": true` exempts a J*
   part from edge-connector treatment; `"lcsc"/"mpn"` flow into BOM.csv.
6. Both flows (`sch_build` and the GUI-flow `wire_by_ref` / `label_pin` /
   `power_pin`) derive pin coordinates from the symbol itself; a sidecar in
   `server/pins/` (`build_pin_sidecars`, optional) overrides for both. A pad
   that resolves to no coordinate fails the run with `missing_pins` — fix
   the pad number, don't guess.
7. Lint first (`lint_netlist.py`, 1 s) — never debug a 2-minute pipeline run
   for a single-pin net.

## When something fails

`docs/PIPELINE.md` section 7 lists the known traps (deterministic router,
DSN plane abstraction, KiCad 10 SWIG padstack asserts, `.kicad_pro` rule
override...). Check it before debugging from scratch. Post-route fixers
(`silk_place.py`, `swig_board_fixes.py`) run standalone on a routed board and
never require re-routing.
