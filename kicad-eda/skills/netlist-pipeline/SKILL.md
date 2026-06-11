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
6. New symbols need a pin sidecar: `build_pin_sidecars.py` ->
   `server/pins/<Lib_Symbol>.pins.json`.
7. Lint first (`lint_netlist.py`, 1 s) — never debug a 2-minute pipeline run
   for a single-pin net.

## When something fails

`docs/PIPELINE.md` section 7 lists the known traps (deterministic router,
DSN plane abstraction, KiCad 10 SWIG padstack asserts, `.kicad_pro` rule
override...). Check it before debugging from scratch. Post-route fixers
(`silk_place.py`, `swig_board_fixes.py`) run standalone on a routed board and
never require re-routing.
