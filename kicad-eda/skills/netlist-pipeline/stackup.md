# Stackups and controlled impedance in the headless flow

Working summary for authoring a netlist that declares its stackup, its layer
count and its controlled-impedance nets. The reference (the full key tables,
the artifact, the managed rules, the DSN policy, the fab outputs, every ST-*
code with its remedy) is `${CLAUDE_PLUGIN_ROOT}/docs/STACKUP.md`; the
annotated netlist example is `docs/PIPELINE.md` section 2.

## Always state the count and why

1. Decide the copper layer count per board and write the reason:
   `"copper_layers": 4` plus `"stackup": {"preset": "...", "rationale": "..."}`
   (or `stackup.layers` with the rationale, or an `assumptions` entry with
   topic `copper_layers` and a rationale). 4 is the usual default (easier to
   route than 2, a solid GND next to the outer layers), 2 when cheap and
   simple fits, 6+ for density or RF. `copper_layers` must agree with the
   stackup (ST-E7); in napkin mode a missing rationale is ST-W12.
2. `list_stackup_presets(fab="jlcpcb", copper_layers=4)` lists the builds with
   their default roles (`preset` = `"<fab>:<template>"`); `list_materials(category="rf-laminate")`
   the laminates a custom `layers` list may name.
3. `check_layer_count(netlist)` before the run: the floors above your count
   (F1 impedance needs the fab's controlled-impedance count, JLC 4; F2 a
   stripline needs 6 with the default roles; F3 a fine-pitch BGA needs 4 / 6)
   and, in napkin mode with nothing declared, the default the harness will
   write (`jlcpcb:JLC04161H-3313` with impedance / RF nets, else
   `jlcpcb:JLC04161H-7628`; 6 layers `jlcpcb:JLC06161H-2116`), recorded as a
   `requirements` decision with override `stackup.preset`.
4. Record the choice:
   `record_decision(netlist, stage="requirements", question="How many copper layers, and which stackup?", decision="4L jlcpcb:JLC04161H-7628", rationale="GND plane under the module and the I2C bus", confidence="med", source="D7.11", alternatives=["2L generic:2L-1.6", "6L jlcpcb:JLC06161H-2116"], override="stackup.preset")`.

## Declaring the stackup: preset or layers

- **Preset**: `{"preset": "jlcpcb:JLC04161H-3313", "impedance_control": {"required": true}, "rationale": "..."}`.
  `roles` overrides the preset's roles per copper layer:
  `"roles": {"In2.Cu": {"role": "mixed", "net": "+3V3"}}`.
- **Layers**: the explicit list top to bottom, copper and dielectric
  alternating, copper first and last; a copper entry
  `{"type": "copper", "oz": 1, "role": "signal" | "plane" | "mixed" | "unused", "net": "GND"}`,
  a dielectric `{"type": "dielectric", "kind": "core" | "prepreg", "material": "rogers.ro4350b", "thickness": 0.254, "locked": true}`
  (or `"plies": [...]`; an explicit `dk` / `df` wins over the library). Any
  count, any material, hybrid builds (Rogers on FR-4) included: a preset is a
  convenience, never a limit.
- **Roles**: `plane` = one net, a full pour, no foreign copper (R8, DRU-
  enforced on every router); `mixed` = the pour of `net` plus routing of any
  net (the way to get another routing layer without opening a plane);
  `signal`; `unused` only for B.Cu of `copper_layers: 1`. `planes`
  (`{"In1.Cu": "GND"}`) still assigns the nets; a plane / mixed layer without a
  net gets GND, then the first power rail, as a recorded decision.
- **Mask default on**: the fab's published mask (JLC) or generic LPI is part
  of every impedance; `"mask": null` turns it off (ST-W9) and the fab notes ask
  to open it over the lines. Only the legacy shorthand `{"h", "er", "t"}` is
  unmasked (ST-W7); prefer the model.
- **Other keys**: `fab` (capability lint, mask and Dk basis), `finish`,
  `board_thickness` (ST-W1 vs the computed sum), `dk_policy`,
  `design_freq_hz`, `via_spans` (through vias only in wave 2, ST-E8).
- Nothing declared but `copper_layers`: the fab's default template for that
  count with the D7.2.4 roles (2L: F signal / B mixed GND; 4L: SIG / GND /
  rail / SIG; 6L: SIG / GND / SIG / SIG / rail / SIG).

## Controlled impedance keys (`net_specs`)

```jsonc
"USB_DP": {"diff": 90, "diff_pair": "USB_DM", "gap": 0.15},
"CLK":    {"impedance": 50, "layers": ["F.Cu", "In2.Cu"], "line": "auto"},
"RF_ANT": {"impedance": 50, "freq_hz": 9.15e8, "line": "cpwg", "ground_gap": null,
           "width": null, "ref_nets": ["GND"], "ref_layers": {"F.Cu": ["In1.Cu"]},
           "z_tol_pct": 5, "route": "preroute"}
```

`impedance` (Z0) or `diff` (Zdiff with `diff_pair` and `gap`); `layers` = the
allowed routing layers (default: every layer where the line resolves; an RF
net's primary outer layer); `line` auto | microstrip | cpwg | cpw | stripline |
dual_stripline | embedded_microstrip; `width` / `ground_gap` explicit = checked,
not solved; `freq_hz` makes the net RF (CPWG by default, GND references only,
never Freerouted across planes; the RF rule families are wave 2b); `route`
auto | preroute | agent. A Freerouted impedance net needs its SMD pad layer in
`layers` (ST-W13, and `autoroute_board` refuses with `pad_layer_excluded`): add
the pad layer or route it by preroute / agent. `impedance_calc(netlist="netlist.json", net="CLK")`
shows the width the resolver will use on every allowed layer;
`impedance_calc(target_ohm=50, line="cpwg", ground_gap_mm=0.15, preset="jlcpcb:JLC04161H-7628")`
sizes a free line.

## What the run does with it

- G1 lint: the ST-* codes (errors ST-E1..ST-E12, warnings ST-W1..ST-W13; each
  message names its JSON path and the fix; the remedy table is in the
  reference). Every `net_specs` line needs a reference plane on each allowed
  layer (ST-E9).
- After G2, `stage_physics` writes `<stem>.stackup.json` (the resolved
  stackup, every line's width / window / references, the DSN layer policy, the
  managed-rules summary; byte-stable) and the managed block of
  `<stem>.kicad_dru` beside the board (the canary, R8 plane disallows, R7
  off-layer disallows, R6 width windows), proves the file compiles with the
  canary, and appends new decisions once. `resolve_stackup(netlist, write=False)`
  does the same as a dry run; `resolve_stackup(netlist, fd="force")` with
  numpy + scipy verifies every line with the field solver (budget
  `KICAD_EDA_FD_BUDGET_S`, default 300 s).
- G3 builds the board with the stackup's copper count, layer types, planes
  and the spliced KiCad stackup block, and fails when the saved board does not
  match (R2). G4 routes with the DSN layer policy (plane nets in their own
  classes, impedance nets on their layers at their widths), judges R8 as a
  blocker and R27 / R28 as warnings, and never hands a board with impedance
  lines to the router subagent (its checks need a KiCad 9/10 board; G4 fails
  instead and the stall chain runs). G5 writes the job file with every
  thickness row, `<stem>_ipc2581.xml` for > 2 layers / non-FR-4 / impedance
  builds, `<stem>_stackup.csv` and `<stem>_fab_notes.md` (rationale, mask
  source, the impedance table, hybrid and single-sided notes) into the zip.
- The review's stackup section and the report keys `layer_policy`,
  `physics`, `dru`, `layer_roles`, `dsn_policy` and `fab.stackup` say what was
  resolved, judged and shipped.

## Rules of thumb

- Declare `role: mixed` on an inner layer when the signal layers run out;
  never route on a plane layer (R8 fails G4).
- Keep stitching capacitors at every layer change of a line that references
  a power plane (ST-W10 / ST-W11, R27).
- Single-sided (`copper_layers: 1`): no B-side parts, no vias, no stitching,
  no impedance nets (ST-E12).
- Hybrid builds: give the Rogers core an explicit thickness, let the library
  resolve Dk at `design_freq_hz` (ST-W5 / ST-W6 tell you when a value is used
  outside its band), and expect the fab-notes bonding callout.
