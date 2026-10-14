# Stackups and controlled impedance in the GUI flow

Reference for Step 3 (layer stackup) and the controlled-impedance work of the
pcb-design flow: the stackup the netlist declares is the one source for the
layer count, KiCad's Board Setup values, the impedance widths and the fab
package. The full model, the artifact, the managed rules and every ST-* code
with its remedy are in `${CLAUDE_PLUGIN_ROOT}/docs/STACKUP.md`.

## The layer count is a decision, with a rationale

- Decide it per board and write it down: `copper_layers` plus
  `stackup.rationale` in `netlist.json` (density, routability, plane / return
  paths, RF, cost). 4 is the usual default, 2 when cheap and simple fits, 6+
  for density or RF. The count must agree with the stackup (ST-E7); napkin
  mode warns ST-W12 without the rationale.
- `check_layer_count(netlist)` lists the floors your design triggers
  (impedance needs the fab's controlled-impedance count, a stripline needs an
  inner signal layer between planes, a fine-pitch BGA needs escape layers).
- `record_decision(netlist, stage="requirements", question="How many copper layers, and which stackup?", decision="4L jlcpcb:JLC04161H-7628", rationale="GND under the RF feed and the fine-pitch MCU", confidence="high", source="design review", alternatives=["2L generic:2L-1.6"], override="stackup.preset")`
  keeps the reasoning with the design.

## Declare it in the netlist, enter it in Board Setup

- A fab **preset** (`"stackup": {"preset": "jlcpcb:JLC04161H-3313", "rationale": "..."}`;
  `list_stackup_presets(fab="jlcpcb", copper_layers=4)` shows them with their
  default roles) or an explicit **layers** list (any count, any material,
  hybrid Rogers-on-FR-4 builds; `list_materials()` names the laminates). A
  preset's `roles` override turns a layer into `mixed` (pour plus routing)
  when you need another routing layer; a `plane` layer carries one net and no
  foreign copper.
- `resolve_stackup(netlist, board="board.kicad_pcb")` resolves it, sizes every
  impedance net and writes `<stem>.stackup.json` plus the managed block of
  `<board stem>.kicad_dru` beside your board: the canary, the plane-layer track
  bans (R8), the per-net layer bans (R7) and the width windows (R6). KiCad's
  DRC reads that file beside the board, so the rules hold in Pcbnew, in
  `drc_pcb(pcb)` and in every `route_check` of the agent-routing flow. Keep
  your own rules outside the `# kicad-eda managed begin` / `end` markers.
- In **Board Setup > Board Stackup > Physical Stackup** enter the layers the
  resolved stackup lists (the fab notes and `<stem>_stackup.csv` give material,
  thickness and Dk per layer; the solder **mask** is part of the model and of
  every impedance, so enter it too), or let `build_board_from_netlist` write
  the board with the stackup block spliced in. Set the layer types to the
  roles (plane = power, mixed = mixed).

## Sizing lines on the declared stackup

- `impedance_calc(target_ohm=90, kind="diff", pair_gap_mm=0.15, preset="jlcpcb:JLC04161H-7628")`
  for a USB pair, `impedance_calc(target_ohm=50, line="cpwg", ground_gap_mm=0.15, netlist="netlist.json")`
  for a coplanar RF feed on the netlist's own stackup, or
  `impedance_calc(netlist="netlist.json", net="USB_DP")` for a declared net on
  every allowed layer. The result gives the width, gaps, achieved Z, the model
  and accuracy class, the status (`fd`, `closed_form`, `fallback_unverified`)
  and the width window the DRU enforces (solved +/- max(0.005 mm, 1.5 %)).
- Enter the width and gap in the net class (`USB_DP` class, differential pair
  gap) exactly as reported; a coplanar line's `ground_gap` is its class
  clearance so the GND pour lands at the gap.
- Every impedance net needs a reference plane on each layer it may use
  (ST-E9); referencing a power plane is allowed with stitching caps at every
  layer change (ST-W10 / ST-W11); keep the pads' layer among the net's
  `layers` when Freerouting will route it (ST-W13).

## Fab notes

`export_fab_package(pcb, out_dir, zip_name, netlist, stackup_json)` adds the
job file with one thickness row per copper, ply and mask, `<stem>_ipc2581.xml`
for more than 2 layers, non-FR-4 or impedance builds, `<stem>_stackup.csv` and
`<stem>_fab_notes.md` (the layer-count rationale, the mask source, the
impedance table with tolerances and the hybrid bonding callout) to the zip.
The `fab-checker` subagent reads them.
