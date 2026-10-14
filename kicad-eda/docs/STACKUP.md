# Stackups, layer roles and controlled impedance (wave 2a)

A design declares **any** stackup: 1 or an even number 2..32 of copper layers,
per-layer copper weight and role, per-dielectric material, thickness, Dk and
Df, Rogers / Taconic / Isola laminates and hybrid builds. Fab presets are a
convenience that expand into the same model, never a limit. The layer count is
a **design decision made per board with a recorded rationale**. Controlled
impedance is computed from the declared stackup, the managed design rules keep
every router honest, and the fab package carries the stackup. The binding
decision is D7 in [ROADMAP_DECISIONS.md](ROADMAP_DECISIONS.md); the annotated
netlist example with every key is [PIPELINE.md](PIPELINE.md) section 2; the
working summaries are
[skills/netlist-pipeline/stackup.md](../skills/netlist-pipeline/stackup.md)
(headless authoring) and
[skills/pcb-design/stackup.md](../skills/pcb-design/stackup.md) (the GUI flow).
RF intent (chains, antennas, coexistence) is accepted and typed now and acted
on in wave 2b (section 4).

## 1. Declaring the stackup

### `copper_layers`

`1` (single-sided) or an even number 2..32 (`ST-E2` otherwise). With
`stackup.layers` or `stackup.preset` the count is derived from the stackup and
a declared value must agree (`ST-E7`). `1` builds a KiCad 2-layer board whose
B.Cu has role `unused` (section 1, "Single-sided").

### The two shapes of `stackup`

- **Legacy shorthand** `{"h": 0.20, "er": 4.2, "t": 0.035}` (no other key):
  one unmasked outer microstrip over the default geometry (4 layers unless
  `copper_layers` says otherwise), today's examples. `ST-W7`; the only form
  without solder mask; no KiCad stackup block is written.
- **The model**: a fixed-shape object (unknown keys rejected by the schema)
  with at most one of `preset` and `layers`; neither means the default
  geometry for `copper_layers` (section 1, "Defaults"). Legacy keys mixed into
  the model are `ST-E3`.

| Model key | Meaning |
|---|---|
| `preset` | `"<fab>:<template>"`, e.g. `jlcpcb:JLC04161H-3313`; `list_stackup_presets(fab="jlcpcb", copper_layers=4)` lists them (154 JLC 4-layer templates of 577) |
| `roles` | only with `preset`: `{"In2.Cu": {"role": "mixed", "net": "+3V3"}}` overrides the preset's roles per copper layer |
| `layers` | the explicit list, top to bottom, copper and dielectric alternating, copper first and last (`ST-E1`) |
| `fab` | capability lint (`ST-W3`) and the Dk / solder-mask basis; default = the preset's fab, else `jlcpcb`; `null` = none |
| `board_thickness` | the ordered nominal (mm); G1 compares it with the computed sum (`ST-W1`, JLC: +/-10 % at >= 1.0 mm, +/-0.1 mm below) |
| `finish` | `HASL`, `LF-HASL`, `ENIG`, `OSP`, `ImAg`, `ImSn` or `null` (goes into the KiCad block and the job file) |
| `mask` | `{"material", "dk", "df", "color", "over_substrate", "over_trace", "between_traces"}`; omitted = the fab's mask, else generic LPI; `null` = no mask (`ST-W9`) |
| `dk_policy` | `auto` (fab when the fab controls impedance for this build, else design), `fab`, `design`, `process`, `typical` |
| `design_freq_hz` | the frequency Dk is resolved at; default the highest `net_specs` `freq_hz`, else 1e9 |
| `impedance_control` | `{"required": true, "tolerance_pct": 10}`: `dielectric_constraints yes` in the KiCad block, `ImpedanceControlled` in the job file, floor F1 |
| `via_spans` | `[["F.Cu", "B.Cu"]]`; anything but through vias is `ST-E8` in wave 2 |
| `rationale` | why this count and build; napkin mode warns `ST-W12` without it (section 2) |
| `origin` | `designer` (default) or `layer_policy` (written only by the napkin default) |

### Copper entries and roles

`{"type": "copper", "name": "F.Cu", "oz": 1, "thickness": 0.035, "role": "signal", "net": null}`

- `name` is optional and positional (`F.Cu`, `In1.Cu` .. `In<n-2>.Cu`, `B.Cu`);
  a name that does not match its position is `ST-E2`.
- `oz` defaults to 1 on the outer layers and 0.5 inside; `thickness` to
  `oz x 0.035` mm.

| Role | Meaning | KiCad layer type |
|---|---|---|
| `signal` | a routing layer | signal |
| `plane` | one net, a full pour, no foreign copper (rule R8) | power |
| `mixed` | the pour of `net` plus routing of any net | mixed |
| `unused` | B.Cu of a single-sided design only: no copper, never routed | signal, no copper |

### Dielectric entries and plies

`{"type": "dielectric", "kind": "core", "thickness": 0.254, "material": "rogers.ro4350b", "construction": "7628", "dk": 3.66, "df": 0.0037, "dk_freq_hz": 1e10, "label": "RO4350B 10 mil", "locked": true, "dielectric_model": "djordjevic_sarkar"}`

- `kind` is `core` or `prepreg`. A multi-ply dielectric gives the values per
  ply in `"plies": [ply, ...]` (same keys minus `kind` and `plies`; each ply
  needs a `thickness`).
- An explicit `dk` / `df` always wins. `material` names the library
  (`list_materials()`; `ST-E4` for an unknown material without an explicit
  `dk`, when it is only a label). Without `df` the default is 0.02 with an
  `ST-W6` info. `locked` marks an impedance-defining thickness in the KiCad
  block.
- Dk resolution order (`materials.resolve_dk`): the explicit value, the
  chosen fab's own `fab` entry, `design`, `process`, `typical`, with
  log-frequency interpolation between published points; out of the
  characterised band the nearest value is used with `ST-W6`. Conflicting
  published values more than 2 % apart on a reference dielectric give
  `ST-W8` (JLC core 4.41 / 4.43 vs 4.6). The resolved value, its basis,
  frequency and source go into the artifact, the CSV and the review.
- `dielectric_model` is accepted and kept for KiCad 11; wave 2 writes KiCad 9
  dialect blocks and does not emit it.

### `planes`, roles and the default plane nets

`planes` (`{"In1.Cu": "GND"}`) stays the net assignment. A layer named there
must have role `plane` or `mixed` (a defaulted signal layer becomes a plane; an
outer layer becomes `mixed`, because it carries every SMD pad); naming an
explicit signal layer or a layer the board lacks, or disagreeing with a
copper entry's `net`, is `ST-E6`. A plane or mixed layer with no net gets one,
recorded as a G1 decision (confidence `med`, override `planes.<layer>`): the
first gets `GND`, the second the first power rail (a net starting with `+`,
else one containing `V`; `GND` when the design has no rail), every further one
`GND`.

### Defaults when no stackup is declared

With `copper_layers` only, the geometry is the fab's default template for that
count (`fab_presets.default_template`) and the roles the D7.2.4 table, fab
`jlcpcb`:

| Count | Geometry | Default roles (top to bottom) |
|---|---|---|
| 1 | `generic:2L-1.6` | F signal / B unused |
| 2 | `generic:2L-1.6` (core 1.51 mm, Dk 4.5) | F signal / B mixed GND |
| 4 | `jlcpcb:JLC04161H-7628` | SIG / GND / first rail (else GND) / SIG |
| 6 | `jlcpcb:JLC06161H-2116` | SIG / GND / SIG / SIG / first rail / SIG |
| >= 8 | the JLC `NOREQ` template of that count (`jlcpcb:NOREQ-8L-1.6mm-1oz-0.5oz`, ...) | S G S P G then (S G) repeated, ending S |

No template for the count is `ST-E4`. Nothing declared at all: outside
napkin mode the 4-layer `jlcpcb:JLC04161H-7628` default with an `ST-W12`
warning; in napkin mode the layer-count policy of section 2.

### Solder mask (default on)

Every outer copper layer of a model stackup is coated: `stackup.mask` when
declared (missing keys from the base), else the fab's published mask (JLC:
0.0305 mm over substrate, 0.0152 mm over traces, 0.0305 mm between traces,
Er 3.8), else generic LPI with the same thicknesses, labelled `generic`.
`"mask": null` turns it off (`ST-W9`) and the fab notes ask to open the mask
over the impedance lines. Ignoring the mask shifts a 50 ohm microstrip by
3.5 %, CPWG by 5.8 % and a USB pair by 6.9 %, so only the legacy shorthand is
unmasked. The computed board thickness is copper + every ply + both masks
(`over_substrate`): JLC06161H-2116 gives 1.6554 mm, the D7.2.2 4L hybrid
1.3685 mm.

### Single-sided (`copper_layers: 1`)

B.Cu has role `unused`: no zones, never routable (the DSN excludes it), no
vias, no EMC stitching. `ST-E12` for an SMD part on side B, a preroute or via
on B.Cu, `emc.stitch: true`, or any impedance / RF net (no reference, so also
`ST-E9`). The fab notes say that B.Cu holds only through-hole pad lands.

### Examples

Each of these resolves on the shipped data (the tests check it). A 2-layer
LoRa node, CPWG on F.Cu, the bottom a mixed GND layer (1.641 mm with the JLC
mask):

```json
{"stackup": {"fab": "jlcpcb", "finish": "ENIG", "rationale": "2L: one RF feed, low density",
  "layers": [
    {"type": "copper", "oz": 1, "role": "signal"},
    {"type": "dielectric", "kind": "core", "material": "generic.fr4", "thickness": 1.51, "dk": 4.5},
    {"type": "copper", "oz": 1, "role": "mixed", "net": "GND"}]}}
```

A JLC preset with In2.Cu turned into a mixed +3V3 layer (SIG / GND / mixed
+3V3 / SIG, 1.6252 mm):

```json
{"stackup": {"preset": "jlcpcb:JLC04161H-3313",
  "roles": {"In2.Cu": {"role": "mixed", "net": "+3V3"}},
  "impedance_control": {"required": true}, "rationale": "4L: USB + 50 ohm feeds"}}
```

Six layers with two inner routing layers (SIG / GND / SIG / SIG / +3V3 / SIG,
1.6554 mm, 16 job-file thickness rows):

```json
{"stackup": {"preset": "jlcpcb:JLC06161H-2116", "impedance_control": {"required": true},
  "rationale": "6L: two inner routing layers for the MCU fan-out"}}
```

A 4-layer RO4350B / FR-4 hybrid (Rogers clad core under the RF feeds; PCBWay
publishes no mask, so generic LPI applies: 1.3685 mm, 10 job-file rows;
`ST-W2` warns about the asymmetric copper, `ST-W5` / `ST-W6` about the thin
core and the Dk band):

```json
{"stackup": {"fab": "pcbway", "dk_policy": "design", "design_freq_hz": 1.575e9, "finish": "ENIG",
  "impedance_control": {"required": true, "tolerance_pct": 10},
  "rationale": "Rogers under the RF feeds only",
  "layers": [
    {"type": "copper", "oz": 1, "role": "signal"},
    {"type": "dielectric", "kind": "core", "material": "rogers.ro4350b", "thickness": 0.254, "locked": true},
    {"type": "copper", "oz": 1, "role": "plane", "net": "GND"},
    {"type": "dielectric", "kind": "prepreg", "plies": [
      {"material": "rogers.ro4450f", "thickness": 0.101},
      {"material": "isola.370hr", "construction": "2116", "thickness": 0.12}]},
    {"type": "copper", "oz": 0.5, "role": "plane", "net": "+3V3"},
    {"type": "dielectric", "kind": "core", "material": "isola.370hr", "thickness": 0.71},
    {"type": "copper", "oz": 1, "role": "signal"}]}}
```

Single-sided, and the 4-layer default with the plane nets assigned by hand:

```json
{"copper_layers": 1}
```

```json
{"copper_layers": 4, "planes": {"In1.Cu": "GND", "In2.Cu": "+3V3"}}
```

## 2. Layer count: a design decision (D7.11)

The design states the count (`copper_layers`, a preset or `layers`) and
`stackup.rationale` (or an `assumptions` entry with topic `copper_layers` or
`stackup` and a rationale). `check_layer_count(netlist)` shows the declared
count, the resolved roles, the floors that apply and, in napkin mode, the
default the harness would write. The floors (`layer_policy.floors`, checked at
G1):

| Floor | Condition | Minimum |
|---|---|---|
| F0 | the count is 1 or even 2..32 | `ST-E2` (error) |
| F1 | an `impedance` / `diff` target or `impedance_control.required` on the chosen fab | the fab's smallest impedance-controlled count (JLC: 4; `ST-W3`; 90 ohm on 2L 1.6 mm FR-4 needs lines about 3 mm wide) |
| F2 | a `stripline`, `dual_stripline` or `embedded_microstrip` line, or an auto line allowed on an inner layer | an inner signal layer beside its reference planes: 6 with the default role templates (`ST-W12`); a declared role layout is judged by `ST-E9` instead |
| F3 | a BGA with pitch <= 0.8 mm and >= 3 signal rows / pitch <= 0.5 mm | 4 / 6 (heuristic, confidence med; `ST-W12`) |

**Napkin default** (`"mode": "napkin"`, nothing declared; `ST-W12` names it):
4 layers raised to the floors (the usual default: easier to route than 2, a
solid GND next to the outer layers), preset `jlcpcb:JLC04161H-3313` when any
impedance / RF net exists (50 ohm about 0.155 mm wide with the JLC mask,
matching 0402 and module pads), else `jlcpcb:JLC04161H-7628`; 6 layers give
`jlcpcb:JLC06161H-2116`. The harness (the `g1_prepare` hook of the `d7`
feature) writes `copper_layers`, `stackup.preset`, `stackup.origin:
"layer_policy"` and `stackup.rationale` into the netlist atomically (a G1
rewrite, like the G2 decoupling remediation), logs one remediation and
records one decision in `<stem>.decisions.jsonl` (stage `requirements`,
confidence `med`, override `stackup.preset`, the alternatives with their
trade-offs). The default is applied only to a netlist that says
`"mode": "napkin"`: the harness's default napkin *behaviour* never rewrites a
layer count, and a declared count is never touched. A declared count without
a rationale in napkin mode is `ST-W12` too. Outside napkin mode nothing is
written. Wave 3 adds layer-count scoring and the runtime escalation; a
designer who runs out of routing layers today declares `role: mixed` on an
inner layer or more layers, never a silently opened plane.

## 3. Controlled impedance (`net_specs`)

A net asks for a line with `impedance` (Z0) or `diff` (Zdiff, with
`diff_pair` naming the partner and `gap` the pair gap); `freq_hz` makes it an
RF net (section 4). The keys the resolver reads in wave 2a:

| Key | Meaning |
|---|---|
| `impedance`, `diff` | the single-ended / differential target (ohm) |
| `diff_pair`, `gap` | the partner net (it gets the same class and rules) and the edge gap (mm) |
| `layers` | the allowed routing layers; default: an RF net's primary outer layer, else every layer where the line resolves |
| `line` | `auto` (outer + `freq_hz` = cpwg, outer = microstrip, inner = stripline), `microstrip`, `cpwg`, `cpw`, `stripline`, `dual_stripline`, `embedded_microstrip` |
| `pair` | `edge` (default) or `broadside` (`layers` = the two adjacent layers) |
| `width`, `ground_gap` | explicit values are checked against the target, not solved; `ground_gap` is the CPWG gap |
| `ref_layers`, `void_layers`, `ref_nets` | per allowed layer the reference layers / the copper cut out between trace and reference (RF, 2b) / the nets a reference may carry (impedance nets default `["GND", every plane net]`; RF nets `["GND"]`, `ST-E10` otherwise) |
| `z_tol_pct` | the net's own tolerance (default `stackup.impedance_control.tolerance_pct`) |
| `route` | `auto` (Freerouting), `preroute`, `agent` (RF nets are never Freerouted, 2b); the pad-layer rule (section 8) applies to Freerouted nets only |

**Reference resolution** (`tline.geometry`, per allowed layer): walk up and
down from the trace's layer; each side's reference is the first plane-role
copper, or mixed copper whose net is in `ref_nets`, unless `ref_layers` names
it. Copper in between is in `void_layers`, or a signal / mixed layer treated as
dielectric under the **dual-stripline** assumption (non-RF nets; G4 warns R28
when tracks run parallel over the line), else `ST-E9`. Outer layer with one
reference: coated microstrip; inner with two: stripline (symmetric within 5 %,
else offset) or dual stripline; inner with one: embedded microstrip. A power
plane as reference is valid for impedance (AC ground) and gives `ST-W10`;
allowed layers resting on different reference nets give `ST-W11` and the G4
check R27 (a return path within 2 mm of each layer change).

**Synthesis and accuracy** (`tline.synth`, `impedance.resolve_impedance`):
bisection on the fab's 0.005 mm grid inside the fab's minimum track and
clearance; CPWG solves the width at `ground_gap` = max(fab minimum clearance,
0.15 mm), narrowing the gap when the width exceeds 1.2 x the narrowest chain
pad. Every line record carries `model`, `accuracy_class` (A: the closed form is
final; B: closed form, FD-verified when available; C: FD-synthesised from a
calibrated fallback), `status` (`fd`, `closed_form`, `fallback_unverified`),
`z_achieved`, `width_window` = solved width +/- max(0.005 mm, 1.5 %) (the R6
DRU window), `eeff_f` and `delay_ps_per_mm`. A line that FD could not verify
(class B / C without FD or over budget) keeps its closed form or fallback and
becomes a G3 decision draft (confidence `low`, alternatives "install
numpy+scipy", "raise the budget"); the fab notes flag it `UNVERIFIED`.

**The gap in wave 2a** (`netclass_writer`): the impedance net's class gets
the line's width (diff width and gap for a pair) and, for a coplanar line, the
`ground_gap` as clearance; the reference net beside it gets a class whose
clearance is the smallest coplanar gap next to it, so KiCad's pour (filled at
max of the two class clearances) lands exactly at the gap. A microstrip's
`pour_clearance` is reported, never written as a class clearance (it would
bind the line's own 0402 pads and fail DRC); the RF <-> GND rule R10 is 2b's.

**FD tier and budget**: `fieldsolve2d` (numpy + scipy SuperLU, else a
numpy-only PCG about 17x slower) verifies class B and synthesises class C
lines once per unique cross-section; results are cached in the artifact by
cross-section hash and reused by the next run. Mode `off` (stdlib only,
deterministic), `auto` (the harness: FD when numpy / scipy are present) or
`force` (FD also checks class A). The per-run wall-clock budget is
`KICAD_EDA_FD_BUDGET_S` (default 300 s); one evaluation is 4 solves for a
single line and 8 for a pair, 1-15 s each on thin dielectrics and about 55 s
on a 1.5 mm substrate, so thick-board class C synthesis usually falls back.
Seconds and solve counts go to the run report (`physics.fd`), never the
artifact. A malformed `net_specs` value the resolver reads is `IMP-E2`; a line
the closed forms cannot size is `IMP-E1`. `impedance_calc(target_ohm=50, kind="single", layer="F.Cu", line="cpwg", ground_gap_mm=0.15, preset="jlcpcb:JLC04161H-7628")`
sizes one line on any stackup without a netlist (0.265 mm here);
`impedance_calc(netlist="netlist.json", net="USB_DP")` reports a netlist's net
as the resolver sizes it, on every allowed layer.

## 4. RF intent: accepted and typed now, acted on in wave 2b

`freq_hz` turns the RF rules on; the keys below are checked by the schema
(fixed-shape entries) and carried through the artifact, and the rule families
that act on them (preroutes, corridors, fences, antenna keepouts, the
coexistence budget) arrive in wave 2b. They move the route hash already
(`rf`, `layer_policy`); a part's `clock_hz` does not (it moves no copper).

```jsonc
"net_specs": {"LORA_ANT": {"impedance": 50, "freq_hz": 9.15e8, "role": "tx",   // tx | rx | trx
  "line": "cpwg", "layers": ["F.Cu"], "ref_layers": {"F.Cu": ["In1.Cu"]},
  "void_layers": {"F.Cu": []}, "ref_nets": ["GND"], "width": null, "ground_gap": null,
  "z_tol_pct": 5, "max_vias": 0, "max_len_mm": 25, "stub_max_mm": null,
  "fence": {"pitch_mm": null, "offset_mm": null, "via": [0.6, 0.3], "rows": 1, "freq_hz": null},
  "isolation_mm": 1.0, "rf_group": "LORA", "ref_plane_margin_h": 5,
  "ref_plane_exempt": [{"rect": [2.0, 10.0, 3.0, 2.5], "layers": ["In1.Cu"], "why": "SMA pin relief"}],
  "allow_branches": [["C11", "1"]], "route": "preroute"}},
"components": [{"ref": "U1", "clock_hz": [4.8e7, 32768]}],   // clocks for the harmonic check
"layer_policy": {"max_layers": 6, "fab": "pcbway"},          // napkin hints (wave-3 scoring)
"rf": {
  "chains": [{"name": "LORA_TX", "group": "LORA", "from": ["U2", "9"], "to": ["AE1", "1"],
              "nets": ["LORA_RF1", "LORA_ANT"], "series": ["R2"], "shunts": ["C10", "C11"],
              "match_max_dist_mm": 2.0, "max_len_mm": 25, "tx_power_dbm": 20}],
  "isolation": [{"groups": ["LORA", "GNSS"], "min_mm": 15, "edges_differ": true}],
  "antennas": [{"ref": "AE1", "kind": "pcb_trace", "preset": null,
                "generator": {"family": "ifa", "band_hz": [9.02e8, 9.28e8], "params": {}},
                "feed_net": "LORA_ANT", "ground_net": "GND",
                "keepout": {"layers": "all", "from_footprint": true, "rect": null, "polygon": null},
                "ground_under": false, "edge": "top", "clearance_mm": 5, "license_ack": false,
                "match": {"topology": "pi", "refs": ["C10", "R2", "C11"], "max_dist_mm": 2.0,
                          "fit": {"C10": "NM", "R2": "0R", "C11": "NM"}},
                "sim": "none", "human_steps": ["VNA-tune the pi network on the assembled board"]}],
  "coexistence": {"check": true,
    "tx": [{"group": "LORA", "band_hz": [9.02e8, 9.28e8], "power_dbm": 20}],
    "rx": [{"group": "GNSS", "bands_hz": [[1.559e9, 1.606e9]], "abs_max_dbm": 10,
            "oob_rejection_db": 30, "lna_gain_db": 43}],
    "isolation_db": [{"from": "LORA", "to": "GNSS", "db": 20}], "margin_db": 6}}
```

What 2a already does with an RF net: it is sized as a line (CPWG by default on
an outer layer), its references must be GND (`ST-E10`), it is never
Freerouted across plane layers, and the legacy `antenna_keepout` (`{edge,
depth}` or `{rect}`) becomes a rule area on every enabled copper layer (no
pour, no vias, tracks allowed) on KiCad 9 and 10 (rule R20's 2a half).

## 5. `stage_physics` and the physical-design artifact

The harness feature `pipeline/rp_feature_d7.py` (ORDER 40, logic in
`pipeline/stages_d7.py`) runs `stage_physics` from the `after_g2` hook: right
after G2 passes and before G3 builds the board, `physics_resolve.write_artifact`
resolves the design and writes

- `<netlist stem>.stackup.json`, schema `kicad-eda/physical@1`, with the
  sections `stackup` (the canonical resolved stackup: `source` preset | layers
  | legacy | default | policy, `origin`, `fab`, `copper_layers`,
  `kicad_copper_count`, `copper` [{name, kicad_id, thickness, oz, role, net,
  kicad_type}], `dielectrics` with their plies and resolved Dk / Df, basis,
  frequency and source, `mask` {source: declared | fab | generic | none},
  `finish`, `impedance_controlled`, `board_thickness_computed`,
  `design_freq_hz`, `dk_policy`, `rationale`, `warnings`), `lines` (one
  impedance record per net, section 3, with `layers` per allowed copper
  layer), `dsn` (the Freerouting layer policy: `signal_layers`, `plane_nets`,
  `unused_layers`, `net_layers` {net: {primary, window, widths}}; section 8),
  `dru` (`path`, `sha256` of the managed block, `rules`, `families` {R8, R7,
  R6}), `decision_drafts`, `warnings` and `errors`, plus `design_digest` (a
  sha256 of the physical keys: `stackup`, `copper_layers`, `planes`,
  `net_specs`, `rf`, `layer_policy`, `mode`, `preroutes`, `emc`, the net names
  and each part's ref / footprint / side / position / `clock_hz`) and
  `data_version` (a sha256 of the materials, fab and tline-fallback data);
- the managed block of `<board stem>.kicad_dru` beside the board the run
  builds (section 6);
- the new decision drafts into `<stem>.decisions.jsonl`.

The artifact is **byte-stable**: no timestamps, ids or run times, so two runs
on the same inputs give identical bytes and the file is rewritten only when it
changes. Decisions are stored as drafts (fields without `id` / `ts`) and
appended to the log only when they are not in the previous artifact, so each
is recorded once (a 6-layer USB design: 8 decisions on the first write, 0 on
the second, `changed: false`). Errors in `errors` (an `ST-E*` stackup error,
a plane net with an impedance target, a rule KiCad cannot take) halt G2 with
the physical-design reason; the report key `physics` holds the write result,
the FD report and the three artifact sections.

**Mirror and board-side lookup.** `build_board_from_netlist` re-resolves a
missing or stale artifact right before its SWIG child (its errors refuse the
build) and, when the board's stem or folder differs from the netlist's, writes
a byte-identical mirror `<board stem>.stackup.json` beside the board. The
tools that receive only a board (`autoroute_board`, `add_ground_stitching`,
`export_fab_package`, `check_design_gates`) take an explicit `stackup_json`
argument, else `<netlist stem>.stackup.json`, else `<board stem>.stackup.json`;
without any they run exactly as before and say so (`physics.note`). The SWIG
scripts get the artifact as `KICAD_EDA_STACKUP` in their environment and fall
back to `stackup_model.resolve` of the netlist (stdlib) when it is missing or
stale; the artifact is stale when `design_digest` or `data_version` differ, or
the board's rules file no longer holds the artifact's managed block.

**The route cache and its one-time miss.** The `route_hash_extra` hook adds
`physics_data_version` (the materials / fab / tline data version) to the route
hash, and the netlist keys `rf` and `layer_policy` are hashed with the other
copper-moving keys (`clock_hz` is not). A board routed before the feature
existed, or after a data file changed, therefore misses the cache **once**
(`route_cache_reason` says the hash changed) and is rebuilt and re-routed with
the layer policy; later runs hit as usual. `PIPELINE_NO_CACHE=1` forces the
miss.

## 6. The managed `.kicad_dru`

KiCad's SWIG `LoadBoard`, the zone filler and `kicad-cli pcb drc` (9.0 to 11)
read `<board stem>.kicad_dru` beside the **board**, never beside the netlist,
so `dru_writer` writes it there, before the SWIG build (the build's fresh load
and fill must see the rules). Between the markers
`# kicad-eda managed begin` and `# kicad-eda managed end` the block is
rewritten on every resolve; everything outside them (user rules, CRLF
included) stays byte-identical, and a `(version 1)` header is added only when
the file has none. The block is appended after the user's rules because for
one constraint KiCad applies the **last matching rule** (last matching rule
wins), so a user rule below the end marker overrides a managed one. The block holds, in this
order:

1. the canary `(rule "__kicad_eda_canary" (condition "A.NetName == '__KICAD_EDA_CANARY__'") (constraint track_width (min 10mm)))`,
   which fires only on the canary net and proves the file compiled (section 7);
2. **R8** `kicad_eda_plane_<layer>` per plane-role layer:
   `(layer "In1.Cu") (condition "A.Type == 'Track' && A.NetName != 'GND'") (constraint disallow track)`;
3. **R7** `kicad_eda_offlayer_<net>_<layer>` per impedance net and every
   routable layer it is not allowed on: `disallow track`;
4. **R6** `kicad_eda_width_<net>_<layer>` per impedance net and allowed
   layer: `track_width (min lo) (opt w) (max hi)` from the solved width and its
   window; a diff line's rules also go to its partner net.

A 6-layer JLC06161H-2116 board with a USB pair and one 50 ohm clock on
F.Cu / In2.Cu gets 15 managed rules: the canary, 2 x R8, 2 x R7, 10 x R6.
Rules are ordered and numbered so that the same inputs give the same bytes.
`dru_writer.render` refuses, before anything is written, what KiCad would
drop or misread: a function call that is not A./B.-qualified (a bare
`intersectsArea` crashes 9.0.4, is dropped by 10.0.5 and is a parse error on
10.99), `disallow zone` (10.99 flags the zone outlines), a `deg` unit
(`track_angle (min 135)` is written without it), a layer clause on an
assertion, a layer outside `F.Cu` / `In1..In30.Cu` / `B.Cu` / the keywords
`outer` and `inner`, and a net name KiCad cannot quote: a `'` or `\` is
`ST-E11` (rename the net), a `*` or `?` would be a wildcard. The KiCad 11 agent
path is guarded by this file alone in wave 2: every `route_check` DRC reads
it (a KiCad 11 save never rewrites `.kicad_dru`, and the pre-11 snapshot
copies it).

## 7. Guarded DRC and the canary (R23)

KiCad 9.0.4 and 10.0.5 drop a rules file that does not compile **silently**
(rc 0, the board checked as if the file were absent); only 10.99 exits 3.
`kicad-cli` writes `<stem>_drc.json` only when the check ran, so an unchecked
run used to be judged on the previous report. `drc_guard.run_drc` (behind
`toolchain.drc_counts`, the harness's `rp_kicad.drc` and `drc_pcb`) deletes
the stale report first, then refuses a non-zero rc, a crash (exit 0xC0000374
is heap corruption on 9.0.4), a timeout, a missing or unparseable report or an
`Error:` line in the output; `could not compile custom design rules` in the
output is named as the cause. `drc_guard.rules_compile` is the proof that a
rules file compiled: the shipped canary board (`server/data/drc_canary.*`, a
10 x 10 mm 2-layer board with one 0.2 mm F.Cu track on net
`__KICAD_EDA_CANARY__`) is copied to a temp dir with the text and checked
with `--severity-all`; it is `ok` only with rc 0, a fresh report and at least
one `track_width` hit on the canary net (rule names are never matched: 9.0.4
reports a custom width hit as "board setup constraints"). Signatures: a broken
file (`(min 135deg)`, an unknown `Bogus.Cu` layer) gives rc 0 and `fired: 0`
on 9.0.4 / 10.0.5 and rc 3 on 10.99; a bare area call crashes 9.0.4. The
harness runs it from `after_g2` on the board's whole rules file with every
kicad-cli whose DRC will read it (the pcbnew install's, and the writer's of a
resumed KiCad 11 board); a file that does not compile halts G2 (report key
`dru`: `ok`, `rc`, `crash`, `fired`, `canary_added`, `types`, the artifact's
`rules`, `families` and `sha256`).

## 8. Routing: the DSN layer policy, the pad-layer rule, no handoff

**Freerouting** routes signals across plane layers unless told otherwise, and
KiCad exports **every** zone as a DSN plane, outer pours included.
`swig_export_dsn` therefore runs `dsn_layers.postprocess` on KiCad's raw
export (after the outline rewrite, before the canonical order) with the
artifact's `dsn` section:

- `(type power)` layers become `(type signal)` (typed power leaves plane-net
  SMD pads unconnected);
- every non-plane class gets `(circuit (use_layer <signal and mixed layers>))`;
- each plane net moves into its own class `kicad_eda_plane_<net>`, restricted
  to the routable layers plus its own plane layer(s) (`restricted`, the
  default) or unrestricted (`unrestricted`, the T10 variant);
- each impedance net moves into `kicad_eda_net_<net>` at its primary width,
  on its primary layer plus every allowed layer whose solved width lies inside
  the primary layer's R6 window; the other allowed layers join with their own
  widths through a per-class `(layer_rule L (rule (width W)))`, which one
  recorded Freerouting 1.9.0 run verified (`LAYER_RULE_VERIFIED`, evidence in
  `tests/fixtures/dsn_layers/layer_rule_evidence.json`);
- `unused` layers are in no `use_layer`: a single-sided board routes on F.Cu
  only, hence without vias.

Locked preroutes travel as `(wire ... (type fix))` and survive the round trip.
The `autoroute_board` result and the pipeline report carry `dsn_policy`
(`variant`, `routed_layers`, `layer_rules`, `excluded_layers`,
`pad_layer_excluded`, warnings); G4 compares the routed `<pcb stem>.dsn` with
the artifact's policy and warns when the DSN carries none or another one
(re-route with `PIPELINE_NO_CACHE=1`). Without an artifact nothing is
post-processed in 2a (a bare policy derived from the DSN takes planes from
`(type power)` layers and inner pours only, never from outer pours).

**Pad-layer rule.** A Freerouted impedance net needs the layer of its SMD pads
among its allowed layers: G1 warns `ST-W13` when `layers` excludes it, and
`autoroute_board` refuses before routing (step `dsn`) with `pad_layer_excluded`
and the fix: add the pad layer to `net_specs.<net>.layers`, or route the net by
preroute or agent. The same for a plain net is a warning
(`pad_layer_blocked`).

**T10 fallback.** When the restricted plane classes leave a plane-net pad
unconnected (the fresh DRC report), the pre-route board is restored and
routed **once** more with the unrestricted variant; the route with fewer
unconnected items is kept, `dsn_policy.fallback` says why, and a G4 decision
draft records it.

**No handoff for D7 boards in waves 2a / 2b.** The D7 G4 checks are SWIG-only
and cannot load a KiCad 11 board, and the KiCad 11 agent path's API guards are
wave 3. The `d7` feature answers the `handoff_veto` hook: a board with
impedance-controlled nets, R27 reference transitions or R28 dual-stripline
runs (role rules the DRU cannot express) or RF chains is never handed to the
`router` subagent, with `--route auto` or `--route agent` alike; G4 fails
instead with `routing handoff refused: D7 layer-role / impedance checks need a KiCad 9/10 board; the KiCad 11 agent path is wave 3 (D7-26)`
and the stall chain (Freerouting alternates, a higher pack scale) runs.
A plain board (planes, no impedance lines, no RF) is handed off as before; R8
stays DRU-enforced on it through every `route_check`. The agent-routing skill
and the `router` subagent read the artifact when a user drives them on such a
board directly (their "Layer roles" sections).

## 9. G4 layer-role checks and EMC stitching

From the `g4_verdict` hook, `swig_layer_role_checks.py` (KiCad's Python) loads
the routed board, extracts tracks, arcs, vias, pads and zones (zone layers via
`GetLayerSet`, never `ZONE.GetLayerName`) and runs `layer_role_checks`
once per board content (a sha256 of the file; a re-judged G4 reuses the
result), report key `layer_roles`:

- **R8** (blocker): a track segment of any other net, or of no net, on a
  plane-role layer, or a copper pour of another net there; a board whose
  enabled copper differs from the stackup's is an R8 blocker too (reroute on
  the signal layers; the DSN policy and the DRU keep routers off plane layers).
- **R27** (warning): an impedance net's via whose layers rest on different
  reference nets without a return path within 2 mm: a via of a reference net
  both layers share, or a decoupling capacitor (`C<n>`) with pads on a
  reference net of each side.
- **R28** (warning): on the layer a dual stripline takes as dielectric, tracks
  of other nets running over the line at under 30 degrees for more than 5 mm
  per net.

A board with no plane-role layer and no impedance line is skipped (nothing to
judge); a check that cannot run (no pcbnew, an unreadable board) is a G4
blocker naming why, never a skip. A resumed **KiCad 11** board gets the blocker
`D7 layer-role checks cannot run on a KiCad 11 board` when the design has such
rules (route it with Freerouting from the snapshot so they can be judged),
else the warning `R8 was judged by the managed DRU only`. EMC stitching
(`emc_stitch.py`, through the `emc_args` hook and `add_ground_stitching`)
takes `--stackup=<artifact>`: fence and grid vias follow the roles (a spot
needs GND fill on enough layers), never land in rule areas, and a single-sided
board is refused before anything is placed (rules R22, R26).

## 10. Fab outputs (G5, R24)

`export_fab_package(pcb, out_dir, zip_name, netlist, stackup_json)` (and the
harness's G5) writes, with the artifact, before the zip:

- `<stem>-job.gbrjob`: the job file's copper `FileFunction` values are
  rewritten from each Gerber's own `%TF.FileFunction` header (KiCad 9.0.4
  numbers inner copper by internal layer id, so In1 / In2 of a 4-layer board
  come out as L5 / L7; a byte-identical no-op on 10.x). Its `MaterialStackup`
  must have one `Thickness` row per copper layer, per ply and per mask
  (copper + plies + 2 masks: 16 for JLC06161H-2116, 10 for the 4L hybrid),
  `LayerNumber`, `ImpedanceControlled`, `Finish` and `BoardThickness` must
  match the stackup (`fab_stackup.check_gbrjob`); the legacy shorthand writes
  no block, so only the counts and numbering are checked;
- `<stem>_ipc2581.xml` (`kicad-cli pcb export ipc2581`) when the board has
  more than 2 copper layers, any non-FR-4 material (an unidentified label
  counts) or `impedance_control.required`;
- `<stem>_stackup.csv`: masks, copper and every ply top to bottom with the
  columns `layer`, `type`, `kind`, `material`, `material_id`, `construction`, `thickness_mm`, `dk`, `dk_basis`, `dk_freq_ghz`, `df`, `role`, `net`, `oz`, `locked`, `source`,
  then a `total` row;
- `<stem>_fab_notes.md`: the build (preset / template / custom layer list /
  legacy), fab, copper count, the layer-count rationale (marked when it is the
  napkin default), ordered vs computed thickness, finish, impedance control,
  the mask source (fab / generic LPI / declared / none, and with `mask: null`
  "open the solder mask over the listed impedance lines"), the impedance table
  per net and layer (target, width, gap, tolerance, reference layers, accuracy
  class and status; unverified lines flagged for the fab's field solver), fab
  instructions ("select FR-4 TG155 for JLC impedance builds", the hybrid
  bonding callout "fab to confirm with Rogers", the single-sided note) and the
  human steps.

G5 checks the four files on disk and in the zip (`fab_stackup_check`, report
key `fab` -> `stackup`); every problem is a G5 blocker. Without an artifact
the tool runs as before and the harness blocks G5 naming the missing artifact.

## 11. The five physics tools

MCP tools of the `kicad-generator` server (`server/physics_tools.py`); every
result carries `ok`, a refusal `{ok: false, reason}` with what was learnt
before it. Relative board paths resolve against the netlist's folder.

| Tool | Does |
|---|---|
| `resolve_stackup(netlist, board, fd, write)` | resolves the stackup, every line and the DSN / DRU rules; writes `<netlist stem>.stackup.json`, the managed `<board stem>.kicad_dru` (board default `<netlist stem>.kicad_pcb`) and the new decisions; `write=False` is a dry run; `fd` `off`, `auto`, `force`. Returns the stackup summary, one line summary per net (width, gaps, window, achieved Z, status), `dsn`, `dru`, warnings and errors (`ok` false with design errors, recorded in the artifact) |
| `impedance_calc(target_ohm, kind, layer, line, width_mm, pair_gap_mm, ground_gap_mm, freq_hz, netlist, net, preset, stackup_json, fd)` | sizes (or, with `width_mm`, checks) one line on a stackup: a netlist's, a preset, a stackup object as JSON (the legacy shorthand included) or the default 4-layer build; `kind` single or diff; with `netlist` and `net` the netlist's own net on every allowed layer. 50 ohm on `jlcpcb:JLC04161H-3313` with the JLC mask: 0.155 mm, class B |
| `list_materials(category, query, material)` | the materials library: id, vendor, product, family, category (`fr4`, `high-speed`, `rf-laminate`, `mask`), forms, Dk / Df points with frequency and basis, standard thicknesses; `material=<id>` gives one record with its sources (22 materials shipped) |
| `list_stackup_presets(fab, copper_layers, query, limit)` | the fab presets usable as `stackup.preset`: copper layers, thickness, copper weights, default roles, the counts each is the fab's default for; plus each fab's layer counts, impedance-controlled counts (JLC: 4 to 32) and tolerance |
| `check_layer_count(netlist)` | the layer count as a recorded decision: the declared count and rationale, the resolved stackup's count and roles, the floors F1-F3 that apply, the `ST-W12` / `ST-W3` findings and, in napkin mode with nothing declared, the default the harness applies (count, preset, rationale, decision draft, netlist patch); `ok` false only when the count itself is invalid |

## 12. ST-* lint codes and remedies

`server/tools/lint_rules_stackup.py` (G1, ORDER 50). Findings read
`ID at <JSON path>: message`; info findings go with the warnings. The line
checks (`ST-E9` .. `ST-W13`) run only when the stackup itself resolves.

| Code | Level | Finding | Remedy |
|---|---|---|---|
| `ST-E1` | error | copper and dielectric layers do not alternate, or the list does not start and end with copper | reorder `stackup.layers` copper / dielectric / ... / copper |
| `ST-E2` | error | the copper count is not 1 or an even 2..32, or a copper `name` does not match its position | fix `copper_layers` / the list; name layers `F.Cu`, `In1.Cu`.., `B.Cu` in order or drop the names |
| `ST-E3` | error | both `preset` and `layers`, legacy `h` / `er` / `t` mixed with model keys, or a malformed stackup value | keep one shape: the shorthand alone, or the model with one of `preset` / `layers` |
| `ST-E4` | error | an unknown preset, template or fab, or a material not in the library without an explicit `dk` | pick a preset from `list_stackup_presets`, a material from `list_materials`, or give `dk` (the material is then a label) |
| `ST-E5` | error | a thickness <= 0, a `dk` outside [1, 15] or a `df` outside [0, 0.1] | correct the value (mm, Dk, Df) |
| `ST-E6` | error | `planes` names a signal-role or missing layer, or a plane / role net disagrees | name a `plane` / `mixed` layer in `planes`, or set the same `net` on the copper entry |
| `ST-E7` | error | `copper_layers` disagrees with the stackup | set `copper_layers` to the stackup's count or drop it |
| `ST-E8` | error | a via span that is not a through via | use `[["F.Cu", "B.Cu"]]` (blind / buried vias are out of scope in wave 2) |
| `ST-E9` | error | an impedance or RF net with no resolvable reference plane on an allowed layer | give the layer a plane / mixed reference (roles, `planes`), restrict `layers`, or name `ref_layers` / `void_layers` |
| `ST-E10` | error | an RF net whose reference net is not in its `ref_nets` (default GND) | reference it to GND, or add the net to `ref_nets` knowingly |
| `ST-E11` | error | an impedance / RF net (or its diff partner) named with `'` or `\` | rename the net (letters, digits, `_ + - /`): a DRU condition cannot quote it |
| `ST-E12` | error | single-sided: an SMD part on side B, a B.Cu preroute or via, `emc.stitch`, or an impedance / RF net | move the part to side F, drop the B.Cu copper and stitching, or use 2 layers |
| `IMP-E2` | error | a malformed `net_specs` value the line check reads (target, width, gap, `ground_gap`, `freq_hz`, `z_tol_pct`, `layers`, `ref_nets`, `ref_layers`, `void_layers`, `pair`) | fix the value (numbers > 0, lists of names) |
| `ST-W1` | warning | the computed thickness is outside the fab tolerance of `board_thickness` | order the computed thickness or change the build |
| `ST-W2` | warning | an asymmetric build (warp risk) | mirror the copper weights and dielectrics about the centre |
| `ST-W3` | warning | fab capability: layer count, impedance control (floor F1), material, hybrid or PTFE multilayer | choose a count / material the fab offers, another fab, or accept and record it |
| `ST-W4` | warning | a bondply ply must fill more copper than it can (RO4450F 45.7 um per ply) | add a ply or thicker bondply, or reduce the copper weight |
| `ST-W5` | warning | thin-core Dk drop (RO4350B 4 mil process Dk 3.33) | give the thin core its explicit `dk` or use a thicker core |
| `ST-W6` | info | Dk used outside its characterised band, or a defaulted Df | give `dk` / `df` at `dk_freq_hz` near `design_freq_hz` |
| `ST-W7` | warning | legacy stackup shorthand | declare a preset or `layers` (masked, with roles) |
| `ST-W8` | warning | Dk sources more than 2 % apart on a reference dielectric | set `dk_policy` or an explicit `dk`; tell the fab which value the impedance assumes |
| `ST-W9` | warning | solder mask off (`mask: null`) | keep the mask, or ask the fab to open it over the listed lines (the fab notes say so) |
| `ST-W10` | warning | an impedance net referenced to a power plane on one or both sides | keep stitching capacitors near every via transition, or reference it to GND |
| `ST-W11` | warning | an impedance net allowed on layers with different reference nets | restrict `layers`, or add a return-path via / capacitor at every layer change (R27 checks it) |
| `ST-W12` | warning | no layer count declared, napkin mode without a rationale, or below floor F2 / F3 | declare `copper_layers` or a stackup with `stackup.rationale` at or above the floor |
| `ST-W13` | warning | an impedance net whose allowed `layers` exclude its SMD pad layer (Freerouting cannot fan it out) | add the pad layer to `layers`, or set `route` to `preroute` / `agent` |

## 13. Rules and where they are enforced (wave 2a)

| Rule | What | Enforced where |
|---|---|---|
| `R1` | the stackup is well-formed (`ST-E1`..`ST-E8`, `ST-E11`, `ST-E12`) | G1 lint |
| `R2` | board copper = stackup copper; the saved KiCad block = the emitted one; `(general (thickness))` = the computed sum; zone layers match the roles | the G3 build verification (`swig_stackup.finalize`: a mismatch fails the build); G5 re-checks the job-file rows |
| `R3` | roles agree with `planes` | G1 lint (`ST-E6`) |
| `R4` | fab capability, hybrid and bondply constraints | G1 warnings (`ST-W1`..`ST-W9`) |
| `R5` | a reference exists on every impedance / RF layer; RF references GND | G1 lint (`ST-E9` / `ST-E10`) |
| `R6` | the solved geometry per layer and the width window | `physics_resolve`; DRU `track_width (min opt max)` per net and layer (every router, every `route_check`) |
| `R7` | impedance nets only on their solved layers | DRU per-layer `disallow track`; DSN `use_layer` |
| `R8` | no foreign-net tracks on plane-role layers | DSN `use_layer`; DRU `disallow track` on the plane layer; G4 `check_plane_layers` blocker; wave 3: `route_net` refusal |
| `R20` | antenna keepout on every enabled copper layer | the legacy `antenna_keepout` rule area in the G3 build (no pour, no vias); the strict `rf.antennas` rules are 2b |
| `R22` | EMC stitching never drops vias in rule areas, on spots without GND fill or on single-sided boards | `emc_stitch --stackup` |
| `R23` | the rules file compiles; the DRC report is fresh; rc and crashes are handled | `drc_guard` (the canary, `run_drc`), G2 halt |
| `R24` | the fab outputs carry the stackup | G5 `fab_stackup` (job-file rows and numbering, IPC-2581, notes, CSV) |
| `R26` | single-sided: no B.Cu copper, vias or stitching | G1 `ST-E12`; DSN F.Cu only; `emc_stitch` refusal |
| `R27` | a reference-net change at a layer transition has a return path within 2 mm | G1 `ST-W11`; G4 `check_ref_transitions` warning |
| `R28` | no parallel tracks over a dual stripline (> 5 mm under 30 degrees) | G4 `check_dual_stripline_parallel` warning |

The RF families R9-R19, R21 and R25 (preroutes, corridors, fences, vias,
bends, stubs, lengths, chain placement, isolation, antenna gates, coexistence)
are wave 2b.

## 14. Harness feature, report keys and hooks

`pipeline/rp_feature_d7.py` (ORDER 40) attaches through `pipeline/rp_hooks.py`
only; its state lives in `ctx.ext['d7']` and it writes these report keys:

| Hook | Stage | Report key |
|---|---|---|
| `g1_prepare` hook | the napkin layer default (section 2) | report key `layer_policy`: `{applied, copper_layers, preset, rationale, floors, decision}` or `{applied: false, reason}` |
| `after_g2` hook | `stage_physics` (section 5) then the canary (section 7) | report key `physics`, report key `dru` |
| `route_hash_extra` hook | the data version in the route hash (section 5) | `physics_data_version` (a hash input, not a report key) |
| `handoff_veto` hook | no handoff for D7 boards (section 8) | the G4 blocker `routing handoff refused: ...` and a routing decision |
| `g4_verdict` hook | the layer-role checks and the DSN policy comparison (sections 8, 9) | report key `layer_roles`, report key `dsn_policy` |
| `emc_args` hook | `--stackup=<artifact>` for the stitching pass | (the `emc` entry) |
| `g5_verdict` hook | the fab outputs (section 10) | report key `fab` -> `stackup` |

The hands-off review renders a "Stackup and impedance" section from these
keys (`server/tools/review_stackup.py`: the stackup table with its mask rows,
one impedance row per net and layer with its status, the design-rules and
canary result, the G4 role checks, the fab outputs; unverified lines are open
items). The wiring of that renderer into `review_report.SECTIONS` is X-03's.

## 15. Where things live

```
server/tools/netlist_schema_d7.py   the D7 schema fragments (copper_layers, stackup, rf, layer_policy, net_specs, clock_hz)
server/tools/lint_rules_stackup.py  G1 ST-* lint; layer_policy.py: floors, ST-W12, the napkin default
server/tools/stackup_model.py       netlist -> ResolvedStackup; stackup_roles.py: layers, roles, plane nets
server/tools/materials.py           Dk / Df library (server/data/materials); fab_presets.py: fabs and templates
                                    (server/data/fabs; convert_jlc_stackups.py converts the JLC snapshot)
server/tools/tline/                 the line models, geometry, synthesis, fallbacks; impedance.py: the facade;
                                    fieldsolve2d.py + fd_sections.py: the FD tier (numpy / scipy)
server/tools/physics_resolve.py     the artifact and the managed DRU; dru_writer.py: the block; drc_guard.py: the canary
server/tools/stackup_kicad.py       the KiCad (stackup ...) block; swig_stackup.py: layers, splice, verify
server/tools/dsn_layers.py          the Freerouting layer policy (swig_export_dsn.py argv[3])
server/tools/layer_role_checks.py   R8 / R27 / R28 on plain dicts; swig_layer_role_checks.py: the KiCad-python entry
server/tools/fab_stackup.py         job file, IPC-2581 rule, CSV, fab notes; netclass_writer.py: classes from the lines
server/tools/review_stackup.py      the review section
server/physics_tools.py             the five MCP tools; board_physics.py: the board tools' artifact lookup
pipeline/rp_feature_d7.py           the harness feature; stages_d7.py: its stage logic
tests/fixtures/stackups/            resolved-stackup fixtures; tests/fixtures/dru, dsn_layers, fab_stackup, tline
```
