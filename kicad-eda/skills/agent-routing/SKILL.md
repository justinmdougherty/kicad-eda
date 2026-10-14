---
name: agent-routing
description: >-
  Route a placed KiCad board yourself, net by net, through the KiCad board
  API (no Freerouting): read pads, rules, planes and the obstacle list, lay
  tracks and vias with route_net, and clear real kicad-cli DRC in a
  route_check / remove_routing repair loop until route_status passes. Use
  whenever the user says "route the board", "routing", "finish routing",
  "unrouted", "ratsnest", "clear the DRC on the routing", or the G4 / Stage 8
  gate must pass. Needs a KiCad 11 nightly (headless) or the board open in a
  running KiCad GUI with its API server on. Freerouting (autoroute_board) is
  the fallback for KiCad 9 format boards only.
---

# Agent routing — you are the router

The `kicad-generator` MCP server exposes KiCad's own board API (`server/backends/pcb_api.py`
on the vendored `kipy`). You read the board as plain mm dicts, lay copper with
`route_net`, and KiCad's connectivity + kicad-cli DRC judge the result. There
is no autorouter in this loop: **you** decide every segment and via. Work
like a layout engineer, not like a maze solver — short, deliberate, 45°.

## Prerequisites

- `server_status().board_api` is true. When false, `board_api_reason` says
  why (no KiCad ≥ 10.99 with `kicad-cli api-server`, or the vendored kipy
  failed to import). Alternative: the board is open in a running KiCad GUI
  (9.0 or newer) PCB editor with **Preferences → Plugins → Enable API
  server** on (`mode: "gui"`, which keeps that KiCad's file format). An older
  GUI answers fewer queries and `board_overview.notes` names each
  degradation: connectivity needs KiCad 10.0.1 (`connectivity_available` is
  false below it and `unrouted_connections` cannot count clusters — route by
  pads and rely on `route_check`), pad → footprint references need 10.0.6
  (older GUIs take them from the footprint definitions), the copper-layer
  query needs 9.0.5 (older GUIs read the file's layer table) and the board
  minimums (`rules`) need KiCad 11 (older GUIs report `null`; the net classes
  then come from the project file).
- A placed board: `build_board_from_netlist` (G3 passed) or the GUI flow's
  Stage 7 gate. Placement that fights you is fixed in placement, not routing.
- Never copy or edit the board files yourself. **The first `route_check` /
  `save_board` / `close_board` through a headless session rewrites the file
  as a KiCad 11 board** (see "File format" below); just before that save the
  server snapshots the pre-KiCad-11 file as `X.k9.kicad_pcb` +
  `X.k9.kicad_pro` (+ `.kicad_prl` / `.kicad_dru` when present), and the board
  tools' responses carry `pre_k11_snapshot` while it exists. A KiCad 11 GUI
  session takes the snapshot at `open_board` (`pre_k11_snapshot_taken`).

## The tools (every one takes `pcb` first; every response carries `ok`, `backend: "api"`, `mode`)

| Tool | Returns | Use |
|---|---|---|
| `open_board(pcb, mode="auto")` | overview + `{opened, mode, kicad_version, connectivity_available}` | first call. `auto` joins a GUI that has the file open, else launches a private headless KiCad 11 with the board preloaded. Every other tool opens on demand, so this is mostly for the mode report |
| `board_overview(pcb)` | `{path, mode, kicad_version, copper_layers, outline{bbox,width,height}, rules, netclasses{net: {name, clearance, track_width, via_diameter, via_drill}}, planes[zone dicts], footprints[{ref, value, x, y, rot, side, bbox, pads}], counts{pads, tracks, vias, zones, nets}, unrouted}` | the board sheet: layers, which inner layer carries which plane, board minimums, per-net class sizes, outline, part bboxes; `unrouted` is null when connectivity is unavailable |
| `board_pads(pcb, net="", ref="")` | `{pads: [{id, ref, pad, net, x, y, layers, through_hole, shape, w, h, rotation, drill}]}` | pad geometry for exits and clearance math; filter by net or ref |
| `unrouted_connections(pcb, net="", with_obstacles=True, clearance=0)` | `{unrouted, connections: [{net, from: pad, to: pad, length, obstacles: {"F.Cu": [...], "B.Cu": [...]}}], connectivity_available, note}` | the work list: one entry per missing link (N clusters of a net → N−1 links, closest pad pairs). `clearance` 0 = the net's class clearance; obstacles assume the class track width |
| `route_net(pcb, net, path, width=0, allow_narrow=False, via_diameter=0, via_drill=0)` | `{created: {tracks, vias}, ids, length, warnings}` | lay one path on one net (contract below). Validation failures come back as `ok: false, reason` with nothing created |
| `add_vias(pcb, net, points, diameter=0, drill=0)` | `{created, ids}` | stitching / stub vias without a track (`points` = `[[x, y], ...]`) |
| `remove_routing(pcb, net="", ids=[])` | `{removed: {tracks, vias}}` | rip-up: everything on a net, or specific ids from `route_net` |
| `route_check(pcb, refill=True, save=True, max_items=40)` | `{ok, errors, unconnected, types, violations: [{type, severity, description, items: [{description, x, y}]}], unconnected_items: [same], kicad_cli, report, saved, save_note?, unsaved_note?}` | refill zones, save when the board has unsaved changes (an untouched board is never rewritten: `saved: false` + `save_note`), real kicad-cli DRC (error severity). `ok` only at 0 errors and 0 unconnected. The item descriptions name net, ref, pad and layer: `Pad 1 [P5V] of C1 on F.Cu`, `Track [OUT_A] on B.Cu` |
| `route_status(pcb, netlist="")` | `{nets: {net: {pads, clusters, complete, tracks, vias, length, min_width, class_width, width_ok}}, unrouted, complete, width_issues, via_ampacity_issues, passed}` | the routed gate: KiCad connectivity per net + the IPC width / via-ampacity checks (`netlist` supplies `net_specs`). `passed` is what G4 / Stage 8 needs |
| `save_board(pcb)` / `close_board(pcb, save=True)` | `{saved, path}` / `{closed, saved, discarded_changes}` | save; release the session (a headless server holds one board — closing stops it). `close_board(save=false)` drops unsaved copper on a headless session (`discarded_changes: true`, the file is untouched); on a GUI session the copper stays in the editor (`discarded_changes: false` + `note`) |
| `list_open_boards()` | `{boards: [{path, mode, unsaved_changes}]}` | what the server holds |

One headless board at a time: a tool on another path drops an idle (saved)
board — the response lists it in `released_boards` — and refuses while that
board has unsaved changes (`blocking_board` names it: `save_board` /
`close_board` it first). A board open in a running KiCad GUI is served by
that GUI regardless (the mode is resolved before the headless housekeeping).

## The path contract (`route_net`)

`path` is a list of elements, in order:

```jsonc
[
  {"layer": "F.Cu", "points": [[x, y], [x, y], ...], "width": 0.25},   // polyline, width optional
  {"via": [x, y], "diameter": 0.6, "drill": 0.3},                      // sizes optional
  {"layer": "B.Cu", "points": [[x, y], ...]}
]
```

Rules the server enforces (a violation is `ok: false, reason` naming the element):

- ≥ 1 polyline; a polyline needs ≥ 2 distinct points (duplicates are dropped).
- Two consecutive polylines must be joined by a via placed **exactly**
  (≤ 0.001 mm) at the last point of the first and the first point of the
  second. A via before the first polyline or after the last one is a stub to
  a plane. A via between two polylines on the same layer is allowed but warned.
- `width` 0 → the net's class `track_width` (else `rules.min_track_width`).
  Below the board minimum → error. Below the class width → error unless
  `allow_narrow=True` (only for a pad-entry neck; the reason names the class width).
- `via_diameter` / `via_drill` 0 → class values, else board minimums. Drill ≥
  diameter or annular ring < `rules.min_via_annular` → error.
- A bend that is not a multiple of 45° → warning; an acute turn → warning.
  Treat warnings as defects to fix before the next batch.
- Points are board mm, 4 decimals. A polyline may start or end on existing
  same-net copper (a pad centre, a via centre, a vertex of a track you laid):
  overlapping same-net copper on one layer is connected.

## Method

1. **Read the board.** `board_overview(pcb)`: note `copper_layers`, which
   `planes` zone sits on which layer and net (a `build_board_from_netlist`
   4-layer build: `In1.Cu` = GND, `In2.Cu` = the first power rail), `rules`
   (`min_clearance`, `min_track_width`, `min_via_diameter`, `min_via_drill`,
   `min_via_annular`, `copper_edge_clearance`, `hole_to_hole`), `netclasses`
   per net, `outline.bbox`, and every footprint's `bbox` and `side`.
2. **Get the work list.** `unrouted_connections(pcb)`; sort it yourself:
   1. decoupling cap → IC pin (shortest, F.Cu only, never a via on the signal side);
   2. crystal / clock / reference nets;
   3. differential pairs (both traces in one batch, same bends, constant gap);
   4. the remaining signals, shortest first;
   5. power tracks (widest class, hardest to thread — route them while the
      board still has room, but after the local stubs above);
   6. plane-net stubs (GND, the In2 rail) as you go: every SMD pad on a plane
      net gets a short stub + through via next to it (rule 5 below). A PTH pad
      on a plane net is already connected through the fill — it will not be
      listed once the zones are filled.
3. **Route a batch of 3–6 nets** with `route_net`, using the geometry rules
   below and each connection's `obstacles` list. Re-call
   `unrouted_connections(net=...)` for a net when earlier batches changed
   its surroundings — obstacles include the copper you have laid.
4. **Verify the batch:** `route_check(pcb)` (refills the planes, saves, runs
   kicad-cli DRC). Read **every** `violations[*].items[*]` `(x, y, description)`
   and every `unconnected_items` pair. Fix per the repair rules below, then
   `route_check` again. Never advance with `errors > 0`.
5. **Repeat** 2–4 until `unrouted` is 0 and `route_check.ok` is true.
6. **Gate:** `route_status(pcb, netlist)` must report `passed: true`
   (`complete` and no `width_issues` / `via_ampacity_issues`). Then
   `export_fab_package(pcb, out_dir, zip_name, netlist)` — or hand to
   `drc-reviewer` / `fab-checker` in the GUI flow. `close_board(pcb)` when done.

## Geometry rules (execute these literally)

**1. Exit direction.** Start every path at the pad centre `(x, y)` from
`board_pads`. For an SMD pad leave along its **long axis** (the axis of the
larger of `w` / `h`, rotated by `rotation`), in the direction **away from the
footprint's bbox centre** (`board_overview.footprints[ref].bbox`), and go at
least `max(w, h)/2 + width` beyond the pad centre before the first bend so
the bend sits outside the pad. Passives (0603 pads ≈ 1.05 × 0.95): exit away
from the other pad. SOIC / QFP pins (≈ 1.55 × 0.6): exit outward,
perpendicular to the body. PTH pads: any direction, but leave on 0°/45°/90°
and never through a neighbouring PTH pad of the same header. Between two
1.7 mm pads on a 2.54 mm header the gap is 0.84 mm: a 0.25 mm track needs
0.25 + 2 × 0.2 = 0.65 mm and fits with 0.19 mm to spare (`obstacles` agrees:
each pad's `distance` is 0.42 against a limit of 0.325); anything wider than
0.44 mm does not — power goes around the row, not through it.

**2. 45° geometry between two points** `A=(xa, ya)`, `B=(xb, yb)`,
`dx = xb − xa`, `dy = yb − ya`:

- `|dx| ≥ |dy|`: diagonal-first bend point `P = (xa + sgn(dx)·|dy|, yb)`;
  straight-first bend point `P = (xb − sgn(dx)·|dy|, ya)`.
- `|dy| > |dx|`: diagonal-first `P = (xb, ya + sgn(dy)·|dx|)`; straight-first
  `P = (xa, yb − sgn(dy)·|dx|)`.

Pick the variant whose two segments have no obstacle with
`distance < clearance + width/2`. Bends are multiples of 45°; prefer two 45°
turns (a chamfer) over one 90° corner on signal nets. No acute angles ever.

**3. Clearance math.** `obstacles[layer]` for a connection lists foreign
copper near the **straight line** from→to on that layer: `distance` is from
your centreline to the item's copper edge (a pad by its true outline — a
circle's radius, a rectangle's edges at its rotation, an oval's stadium;
roundrect / chamfered pads by their rectangle, a custom pad by half its
diagonal, conservative; a track by its edge; a via on every layer), negative
when the item sits on the line (how deep across it), `t` where along the
line (0 = from, 1 = to). Your
track is legal when every item's `distance ≥ clearance + width/2`. Add a
margin of 0.05 mm. When an item is closer, detour: offset the path on the
side with more room by at least `(clearance + width/2 + 0.05) − distance`,
bending 45° in and out around `t`. A pad you must pass between two pins of:
the centre-to-centre gap must be ≥ pad width + 2·clearance + track width.
Never run a track over a foreign pad on the same layer, never cross a foreign
track on the same layer (cross on the other layer with a via each side, or
go around). Keep `copper_edge_clearance` (rules) from `outline.bbox`.

**4. Vias.** A via exists on **every** copper layer. Place its centre so that
it is ≥ `via_diameter/2 + clearance` from every foreign copper edge on every
layer — from a foreign pad centre that is `via_diameter/2 + clearance +
half the pad diagonal`; from a foreign track centreline `via_diameter/2 +
clearance + track width/2`; from a foreign via centre `via_diameter +
clearance`; drill-to-drill ≥ `hole_to_hole`. Never inside any pad, never
under a footprint's pads, never inside a courtyard of a part on the other
side unless that part has no pads there. Vias on a foreign net punch antipads
through the planes: keep them ≥ 1 mm apart from each other and never in a row
across a narrow plane corridor (that slices the plane). Prefer F.Cu for
top-side parts, B.Cu for crossings and bottom-side parts; one via per
crossing, then come back. Signal nets: ≤ 2 vias per connection.

**5. Plane stubs (GND and the In2 rail on the 4-layer build).** Never route a
plane net as a long surface track. For each SMD pad on a plane net:
`[{"layer": <pad layer>, "points": [[px, py], [sx, sy]]}, {"via": [sx, sy]}]`
with the stub along the pad's exit direction and the via centre at
`≥ max(w, h)/2 + clearance + via_diameter/2` from the pad centre (0603 pad
1.05 × 0.95, 0.2 mm clearance, 0.6 mm via: ≥ 1.03 mm → use 1.2 mm). The refill in
`route_check` connects the via to the plane; if the pad still shows in
`unrouted_connections` afterwards, the via landed in a plane void (antipad
cluster, keepout, outside the zone) — move it. A GND stub belongs beside its
decoupling cap's IC pin, not on the far side of the cap. Power nets that have
**no** plane (`planes` lists none for the net): route them at the class width
like signals, widest first.

**6. Widths.** `width=0` gives the class width — use it. `allow_narrow` only
when a wide class must enter a pad narrower than the track: neck down for
≤ 3 mm right at the pad (the `route_status` width check exempts an entry neck
and flags an open-field sub-width run). Diff pairs: identical point lists
offset by the class gap, same layer, via both at the same `t`.

**7. Zones.** Do not draw copper on a plane layer (`In1.Cu`, `In2.Cu`
carrying a plane); the planes are zones and `route_check` refills them.

## Layer roles and the managed rules (stackups, ADR D7)

A board built from a netlist that declares a stackup (`stackup`,
`copper_layers`; `${CLAUDE_PLUGIN_ROOT}/docs/STACKUP.md`) comes with the
physical-design artifact `<stem>.stackup.json` beside the netlist (and a
byte-identical `<board stem>.stackup.json` when the board's stem differs) and
the managed rules file `<board stem>.kicad_dru` beside the board. Read the
artifact before the first batch; it replaces the 4-layer assumptions above:

- `stackup.copper[*]` gives every layer's `role` and `net`: `plane` layers
  (one net, a full pour, no foreign copper: rule R8) replace the fixed
  "In1.Cu = GND, In2.Cu = rail" picture, `mixed` layers pour `net` and may be
  routed by any net, `signal` layers route, an `unused` layer (single-sided
  B.Cu) is never touched. `dsn.plane_nets` lists the plane net per layer.
- `lines[<net>].layers[<layer>]` gives each impedance net its allowed layers,
  the solved `width` and `width_window` per layer, and `ref` (the reference
  layers and nets). Route such a net only on those layers, at that layer's
  width (`route_net(pcb, net, path, width=<layer width>)`), with a
  return-path via or decoupling capacitor within 2 mm of every layer change
  whose references differ (R27), and never run another net parallel over a
  dual stripline for more than 5 mm (R28).
- The DRU is the guard: every `route_check` DRC reads `<board stem>.kicad_dru`
  (a KiCad 11 save never rewrites it, and the `X.k9` snapshot copies it), so a
  foreign track on a plane layer (R8, `kicad_eda_plane_<layer>`), an impedance
  net off its layers (R7, `kicad_eda_offlayer_<net>_<layer>`) or outside its
  width window (R6, `kicad_eda_width_<net>_<layer>`) comes back as a
  `track_width` / `items_not_allowed` violation — fix it like any other, by
  rip-up and re-route on the right layer at the right width. Keep your own
  rules outside the `# kicad-eda managed begin` / `end` markers.
- Never move, rip up or re-lay locked copper: it is a preroute (a width-class
  power path, an LGA escape, in 2b an RF feed); route around it.
- Never change the layer set or the stackup through the API (no
  `set_enabled_layers`, no stackup edits): the stackup is authored in the
  netlist and spliced by the builder. The API-level guards are wave 3
  (`board_overview` reporting the stackup and roles, `route_net` refusing
  foreign copper on plane layers and enforcing the RF mode); until then the
  managed rules and this text are the guard.

In waves 2a / 2b the harness never hands a board with impedance-controlled
nets, R27 / R28 role rules or RF chains to this flow (its G4 checks need a
KiCad 9/10 board; G4 fails instead of handing off), so you meet such boards
only when a user drives you on them directly.

## Batch → `route_check` → repair

Read each violation's `items` (two per clearance violation: the offender and
the victim, each with net in `[...]`, ref/pad or `Track`, and layer, plus
`x, y`). Then:

| DRC `type` / item text | Fix |
|---|---|
| `clearance` between a track you just laid and a pad/track of another net | `remove_routing(net=<your net>)`, re-route with the detour from rule 3 (or on the other layer with a via each side) |
| `clearance` / `hole_clearance` at a via | move the via (rule 4); re-lay only that path: `remove_routing(ids=[...])` from the `route_net` response |
| `track_width` / `via_diameter` / `annular_width` / `via_hole` | your sizes are below the board rules — use 0 (class) or the `rules` minimums |
| `copper_edge_clearance` | the path is inside `copper_edge_clearance` of the outline — pull it in |
| `unconnected_items` pair still listed after routing that net | the copper does not touch the pad: the endpoint is not the pad centre, or the via landed off the plane — compare the item `x, y` with `board_pads` |
| `shorting_items` / a track through a foreign pad | you crossed foreign copper on one layer — rip up and cross on the other layer |
| `items_not_allowed` / `keepout` | inside a rule area (antenna keepout) — go around |

Rip-up and reroute: when a connection has no clean corridor on either layer,
(1) try the other layer with two vias, (2) move the via, (3) rip up the
blocking net (`remove_routing(net=<blocker>)`), route the constrained
connection first, then re-route the blocker around it — at most two rip-up
rounds per connection, and never rip up a net you did not lay in this session
without saying so. Vias and stubs of plane nets are cheap to move; power
tracks are expensive — route around them.

## When stuck — and when to stop

Every `route_check` reports `progress` (`rounds`, `score` = errors +
unconnected, `best`, `rounds_without_improvement`) and `stalled`: true after
3 rounds without a new best score while the board is not clean, with a
`stall_note` naming the fallback (ROADMAP D5); a round that is not counted
(unsaved copper, a failed DRC) says so in `progress_note`. A single
connection that has failed twelve attempts counts as a stall too. On a
stall, stop routing, `close_board(pcb, save=True)`, and say which
connections remain and which foreign copper blocks each. The fix is
upstream: move the part (`move_footprint` in the GUI flow; fixed `x/y` or
`pack_scale` in the netlist for the pipeline), swap the part's side, or add
a corridor preroute. **Freerouting is the fallback, on the pre-KiCad-11
snapshot**: `autoroute_board` (and `check_design_gates`,
`add_ground_stitching`) run under KiCad 9/10's pcbnew Python and refuse a
board saved by the KiCad 11 session (`ok: false` with `swig_readable`'s
reason). After a harness handoff the orchestrator runs the handoff's
`--resume stalled` (Freerouting from the snapshot, then a higher
pack_scale, then stop) — never call `autoroute_board` yourself there. In a
conversation, `autoroute_board(pcb, from_snapshot=True)` restores
`X.k9.kicad_pcb` over the board (the agent's KiCad 11 copy moves to
`X.agent.kicad_pcb`) and routes it with Freerouting.

## File format — read this before the first `route_check`

A board saved by the **headless KiCad 11 session** becomes a **KiCad 11
file** (`(version 20260901)`, `(generator_version "10.99")`). KiCad 9/10,
their `kicad-cli` and their pcbnew Python **cannot read it** ("Failed to
load board"). Consequences:

- `drc_pcb`, `export_gerbers`, `export_fab_package`, `render_pcb` keep
  working: `toolchain.board_cli(pcb)` reads the file header and runs the
  kicad-cli of the KiCad that wrote it (11 for this board; 9 for a
  pcbnew-built one), so DRC semantics and fab output follow the writer.
- `autoroute_board`, `check_design_gates`, `add_ground_stitching` (SWIG) refuse
  the file; the routed gate is `route_status` instead (`check_design_gates`
  with `stage="routed"` answers with it automatically).
- Open the board in KiCad 11 only (the snapshot `X.k9.kicad_pcb` beside it is
  the board as it was before the first KiCad 11 save, in KiCad 9/10 format).
  Tell the user this before they double-click the file in KiCad 9/10.
- A **GUI** session keeps the running editor's format (a KiCad 9 GUI saves a
  KiCad 9 file) — routing in a KiCad 9 GUI session keeps every SWIG tool usable.

## Worked example — `examples/lm358_buffer` (numbers are illustrative)

Board from `build_board_from_netlist(netlist, 30, 25)`: U1 SOIC-8 at (16,
12.5), J1 1×6 header at x = 5 (pins at y = 6.15 … 18.85, 2.54 pitch), C1/C2
0603 beside U1 pin 8. `board_overview` says: `copper_layers` F.Cu / In1.Cu /
In2.Cu / B.Cu, `planes` = In1.Cu GND + In2.Cu P5V, `rules.min_clearance`
0.2, `min_track_width` 0.2, class `Default` = 0.25 mm track, 0.2 mm
clearance, 0.6/0.3 via. `unrouted_connections` lists IN_A (J1.3 → U1.3,
8.7 mm), OUT_A (U1.1 ↔ U1.2, 1.27 mm; U1.2 → J1.4), IN_B, OUT_B, and the
SMD pads of GND / P5V on C1, C2, U1.4, U1.8 (the PTH J1 pins are already in
the plane fill).

**IN_A** — U1.3 at (13.525, 13.135) exits left (long axis, away from the
body) to (12.2, 13.135); J1.3 is at (5, 11.23): `dx = −7.2`, `dy = −1.905`,
diagonal-first `P = (12.2 − 1.905, 11.23) = (10.295, 11.23)`. `obstacles["F.Cu"]`
is empty:

```
route_net(pcb, "IN_A", [{"layer": "F.Cu",
  "points": [[13.525, 13.135], [12.2, 13.135], [10.295, 11.23], [5, 11.23]]}])
→ {ok: true, created: {tracks: 3, vias: 0}, length: 9.31, warnings: []}
```

**OUT_A** — pins 1 and 2 (1.27 mm apart) get a chamfered U on F.Cu; the leg
to J1.4 at (5, 13.77) must cross IN_A (it heads down-left while IN_A heads
up-left): `obstacles["F.Cu"]` for that connection shows
`{"kind": "track", "net": "IN_A", "distance": -0.1, "t": 0.42}`, so the leg
goes to B.Cu through one via, and ends on J1.4's PTH pad from below:

```
route_net(pcb, "OUT_A", [{"layer": "F.Cu", "points": [[13.525, 10.595], [12.7, 10.595],
  [12.4, 10.895], [12.4, 11.565], [12.7, 11.865], [13.525, 11.865]]}])
route_net(pcb, "OUT_A", [
  {"layer": "F.Cu", "points": [[12.4, 11.23], [11.5, 11.23]]},
  {"via": [11.5, 11.23]},
  {"layer": "B.Cu", "points": [[11.5, 11.23], [8.96, 13.77], [5, 13.77]]}])
```

The via at (11.5, 11.23) is 0.85 mm from IN_A's diagonal centreline (needs
0.3 + 0.2 + 0.125 = 0.625) and 1.29 mm from the edge of U1 pad 2 (needs 0.5).

**GND / P5V stubs** — C1 pad 2 (GND) at (21.5, 10.275), exit +y away from the
body, via 1.2 mm out:

```
route_net(pcb, "GND", [{"layer": "F.Cu", "points": [[21.5, 10.275], [21.5, 11.5]]},
                       {"via": [21.5, 11.5]}])
```

Same pattern for C1.1 / C2.1 / U1.8 on P5V (via → In2.Cu plane) and C2.2 /
U1.4 on GND. IN_B / OUT_B mirror IN_A / OUT_A around the bottom of U1
(U1.5–U1.7 exit right, go under the body on B.Cu, back up to J1.5 / J1.6).

**Check:** `route_check(pcb)` → `{ok: true, errors: 0, unconnected: 0, saved: true,
kicad_cli: ".../KiCad/10.99/bin/kicad-cli.exe"}`. Had the OUT_A leg been laid
on F.Cu first, it would have come back as
`violations: [{type: "clearance", items: [{description: "Track [OUT_A] on F.Cu", x: 10.9, y: 11.83},
{description: "Track [IN_A] on F.Cu", ...}]}]` → `remove_routing(pcb, net="OUT_A")`
and the B.Cu version above.

**Gate:** `route_status(pcb, netlist)` → `{complete: true, unrouted: 0,
width_issues: [], passed: true}` → `export_fab_package(pcb, out_dir,
"lm358_buffer_JLCPCB.zip", netlist)` → `close_board(pcb)`.
