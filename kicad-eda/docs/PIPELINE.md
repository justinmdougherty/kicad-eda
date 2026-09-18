# The Gated Headless Pipeline — concept → Gerbers, no GUI

This is the authoritative run-book for the netlist-first pipeline. It is the
process that produced the RPi 5 battery-emulator HAT fab package (65×56.5 mm,
68 parts, two-sided, 4-layer, 0 DRC errors, JLCPCB zip + BOM/CPL) and that the
10-board regression suite proves on every tool change.

```
netlist.json ──> G1 lint ──> G2 schematic+ERC ──> G3 place (SWIG) ──> G4 route
 (source of      structure    KiCad 11 API or      swig_build_board    agent routing on the
  truth)         + intent     sch_build.py (§8)    courtyard DRC = 0   KiCad board API (§4a)
                              ERC must be 0                            or Freerouting (harness)
                                                                       + DRC + checks
                                                                          │
                                                          G5 fab package <─┘
                                                          Gerbers/drill/BOM/CPL zip
```

Every gate is a go/no-go with explicit blockers (IEEE 15288.2 style). The
process **feeds itself**: blockers become tasks; tasks with an unambiguous
remedy are auto-applied (missing bypass cap, looser placement, micro-gap heal,
plane-island link, parallel via) and the gate re-tests. Only design-intent
tasks stop the run, with the open task list attached.

---

## 1. Prerequisites

| Dependency | Why | Install |
|---|---|---|
| **KiCad 10** (9 works) | bundled `pcbnew` Python (SWIG board build), `kicad-cli` (ERC/DRC/Gerbers), stock symbol+footprint libs | `winget install KiCad.KiCad` / `brew install --cask kicad` / distro package |
| **KiCad 11 nightly** (optional) | `kicad-cli api-server`: the schematic is built through KiCad's own IPC API instead of hand-written S-expressions (§8) | <https://downloads.kicad.org/kicad/windows/explore/nightlies> — installs beside 10 as `Program Files\KiCad\10.99` |
| **Python 3.10+** | MCP server + harness venv | python.org / OS package |
| **Java 17–21 JRE** | Freerouting 1.9.0 (bundled at `server/tools/freerouting-1.9.0.jar`) | `winget install EclipseAdoptium.Temurin.21.JRE` / `brew install temurin@21` |

Run `scripts/install-deps.ps1` (Windows) or `scripts/install-deps.sh`
(macOS/Linux) to check and install the three required tools (the nightly is
reported, never installed). The server discovers **every** KiCad on the
machine (`server/kicad_env.py`; `KICAD_PATH` adds a root or bin dir), Java
(`JAVA_HOME`, then a bundled `server/tools/jre*`), and the Freerouting jar
(`FREEROUTING_JAR` overrides — **do not use Freerouting 2.x: it phones home
and hangs headless; 1.9.0 is the pinned router**).

## 2. Author the netlist (`netlist.json`)

The single source of truth. Pad-level, hand-authored or generated:

```jsonc
{
  "name": "my_board",                  // file/project stem
  "title": "Human title",
  "class": 2,                          // IPC producibility class (1/2/3)

  // BOARD — omit entirely for auto-sized boards. For a FIXED outline:
  "board": {
    "w": 65.0, "h": 56.5,              // fixed outline, mm
    "auto_x": 8.5, "auto_y": 21.0,     // free-field rectangle where the
    "auto_w": 23.0, "auto_h": 25.0,    //   auto-placer may put TOP parts
    "auto_b": {"x":30,"y":26,"w":25,"h":22}  // ditto for BOTTOM-side parts
  },

  "mounting_holes": true,              // 4 corner M3 NPTH, refs H1-H4 — or:
  "mounting_holes": {
    "footprint": "MountingHole:MountingHole_2.7mm_M2.5",
    "positions": [[3.5,3.5],[61.5,3.5],[3.5,52.5],[61.5,52.5]]
  },

  "components": [
    // auto-placed (default):
    {"ref":"U1","lib_id":"Interface_USB:STUSB4500QTR","value":"STUSB4500",
     "footprint":"Package_DFN_QFN:QFN-24-1EP_4x4mm_P0.5mm_EP2.7x2.7mm"},
    // fixed (mixed placement — connectors, power chain in current-flow order):
    {"ref":"J1", "...": "...", "x":3.0, "y":43.0, "rot":270},
    // bottom side (auto-placed into auto_b, or fixed with x/y):
    {"ref":"U4", "...": "...", "side":"B"},
    // a J* part that is NOT an edge connector (jumper, Kelvin header):
    {"ref":"J4", "...": "...", "interior":true},
    // assembly sourcing (carried into BOM.csv):
    {"ref":"C1", "...": "...", "lcsc":"C15849", "mpn":"CL21B104KBCNNNC"}
  ],

  "nets":      {"GND": [["U1","13"],["C1","2"], "..."], "...": []},
  "no_connect": [["U1","7"], "..."],

  // Design intent → IPC-2221/2141A net classes (track width, clearance,
  // via size) and the G4 ampacity checks:
  "net_specs": {
    "5V_PI":  {"voltage":5.0, "current":4.0},
    "RF_OUT": {"impedance":50},         // RF: controlled-impedance class
    // diff pairs: G4 measures intra-pair length mismatch (dp_tol_mm,
    // default 1.5) and the coupled fraction (within dp_gap_mm, default
    // 1.0; <70% flags). WARNING-grade: Freerouting cannot pair-route —
    // preroute real high-speed pairs as a pair and let the numbers verify.
    "USB_DP": {"diff_pair":"USB_DM"}
  },

  // PREROUTES — designer-authored fat copper laid before the router runs.
  // Use for every high-current path: Freerouting 1.9 cannot neck down, so a
  // wide class into a small pad is THE unroutable pattern. Prerouted (and
  // plane) nets are width-class-EXEMPT: the router connects remaining taps
  // at signal width. Point forms: ["pad",ref,pad,(width)] | ["xy",x,y,(width)]
  // — a width on a point applies to the segment STARTING there.
  "preroutes": [
    {"net":"5V_PI","width":1.5,"layer":"F.Cu",
     "path":[["pad","L2","2",2.0],["xy",24.5,11.5,2.0],["xy",24.5,17.7,1.5]],
     "vias":[[6.4,21.9],[6.4,23.4]], "via_size":[1.2,0.9]},
    {"net":"5V_PI","width":1.4,"layer":"B.Cu",
     "path":[["xy",6.4,24.9],["xy",6.4,2.28]]}
  ],

  "antenna_keepout": {"edge":"top","depth":6},  // optional no-fill rule area

  // EMC POST-PASS (opt-in, runs after G4 passes, then re-DRCs):
  // perimeter GND via fence + interior stitching grid (Ott/Montrose).
  // Pitch = guided lambda/20 from max_freq_hz, else a fixed 3 mm.
  // NOTE: "teardrops" only emits a reminder — KiCad's teardrop generator
  // has no headless API; generate in the GUI before fab if required.
  "emc": {"stitch": true, "max_freq_hz": 0, "fence_only": false},

  // MECHANICAL CEILINGS — courtyard DRC is 2D-blind. Board-relative mm;
  // side "F"|"B"|"both". Part height: component "height" key, else a
  // package table (XAL6060 6.36, CP_Elec 10.5+, PinSocket 8.7, TSSOP 1.2,
  // 0603 0.7, ...). Taller-than-ceiling = G3 blocker; unknown height
  // inside a zone = warning. "exempt" for the part that IS the standoff:
  "height_zones": [
    {"name":"clearance to Pi", "side":"B", "x":0, "y":0, "w":65, "h":56.5,
     "max_height":4.0, "exempt":["J5"]}
  ]
}
```

Hard-won authoring rules:

- **Pad-correct or bust.** QFN/HSOP exposed pads have their own number (e.g.
  pin 25 on a QFN-24). `Device:LED` pin 1 = cathode. Multi-element thermal
  pads all share one number — the builder nets them all.
- Check real pin maps against the datasheet, not the symbol name
  (MCP45HV51-ST TSSOP-14 ≠ its QFN pinout; INA228 lib symbol carries SPI
  names — INA226 was the usable I²C part).
- Pin coordinates: on the API backend (§8) KiCad itself reports them for the
  placed instance, any rotation. On the file backend both `sch_build` and the
  GUI-flow wiring tools derive them from the flattened symbol (keyed by pin
  number and name); a **pin sidecar** in the central
  `server/pins/<Lib_Symbol>.pins.json` (`python server/tools/build_pin_sidecars.py`
  or the `build_pin_sidecars` MCP tool, optional) overrides it for both. On
  either backend a (ref, pad) that resolves to no pin fails G2 with
  `missing_pins` instead of silently dropping the label.
- Size continuous vs burst current honestly: a 5 A ms-scale burst fed by bulk
  capacitance is IPC-2152 transient — spec the *continuous* number or the
  class width physically cannot thread the board and the router thrashes.
- **Hand-place the power chain in current-flow order** (input → switcher →
  inductor → bulk → FETs → shunts → output connector). The auto-clusterer
  optimizes connectivity, not current flow; routing never converges on a
  scattered chain.

## 3. Lint early

```powershell
<venv>\python server\tools\lint_netlist.py path\to\netlist.json
```
1 s instead of a 2-minute pipeline run. Catches single-pin nets,
double-assigned pads, net/no_connect overlap, undriven `power_in` rails.

## 4. Run the gated pipeline

```powershell
<venv>\python pipeline\run_pipeline.py path\to\netlist.json <board_w> <board_h>
# fixed-outline boards take w/h from the netlist "board" block; the CLI dims
# are hints/fallback for auto-sized boards
```

What happens, gate by gate:

| Gate | Stage | Auto-remediation on failure |
|---|---|---|
| **G1** Requirements | `lint_netlist` + intent check | — (designer fixes the netlist) |
| **G2** Schematic | `build_schematic_from_netlist` → `.kicad_sch` (KiCad 11 API backend when a nightly is installed, else `sch_build` — §8; the report's `schematic.backend` says which), `kicad-cli sch erc` (newest kicad-cli), design-intent review (`design_report`: decoupling coverage, floating pins, pulls, test points), **thermal review** (`thermal_check`: shunt I²R vs 50%-derated rating, FET conduction loss, linear-reg (Vin−Vout)·I, switcher loss estimate; Tj = Tamb + P·θJA ≤ 80% Tj_max) | missing bypass → 100 nF auto-added at the IC, netlist rewritten, stage re-runs. Thermal: declared numbers (`p_diss`, `rds_mohm`, value "3W") block; estimates warn. Overrides per part: `"thermal": {"p_diss", "theta_ja", "tj_max", "i_max"}` (`i_max` for topology-capped parts, e.g. a bypassed sleep shunt); board-level: `"thermal": {"t_ambient", "tj_margin", "ep_vias_min"}` |
| **G3** Placement | `swig_build_board` (KiCad bundled python) → outline, mixed/two-sided placement, zones (In1=GND, In2=first power rail), preroutes, silk; courtyard DRC must be **0**; connector mouths must face off-board at the edge; dissipating exposed-pad parts must have ≥4 thermal vias under the EP (IPC-7093 — footprint `...ThermalVias` variants count); **plane integrity**: a power plane whose pad population is split across separate fill islands is a BLOCKER (the DSN export hides voids — the router will return the net broken); corridors under 1.8 mm between pad groups (one router via from sealing) are a warning. GND is exempt (post-route pours + stitching unify it) | courtyard overlap → re-place at next `pack_scale` (1.0→1.5→2.0→2.6→3.2); a retry that no longer fits restores the last good ROUTED snapshot (`.attempt` file); **EP via farm**: a dissipating EP part whose footprint has no `...ThermalVias` variant (e.g. TO-263) gets an IPC-7093 via farm auto-dropped under the tab (0.3 mm drill, ~1.2 mm pitch, pad's net) before the review runs |
| **G4** Layout | **Agent routing** (§4a, the default when Claude drives the board): the agent lays every track and via through the KiCad board API, `route_check` = zone refill + kicad-cli DRC, `route_status` = connectivity + routed-width + via-ampacity gate. **Harness** (`run_pipeline.py`, unattended): DSN export → **Freerouting 1.9** → SES import → DRC + routed-width check + via-ampacity check (`swig_gate_checks`) | Agent: the repair loop is the agent's (`remove_routing` + re-route per violation). Harness: 1–4 unconnected → **heal in place** (`swig_board_fixes`): ≤2 mm same-net gaps bridged; plane-net orphans get island-link tracks (straight-or-L, plane layer preferred); under-ampacity transitions get parallel vias. 2-round fix→re-DRC→re-gate loop. Scale escalation only for real congestion. |
| **G5** Fab package | `kicad-cli` Gerbers + drill + pos; BOM.csv + CPL.csv (JLCPCB format) zipped | — |

Exit 0 = every gate passed; the JSON report (stdout) and gate summary
(stderr) name every blocker and every remediation applied.

Useful environment variables:

| Var | Effect |
|---|---|
| `PACK_SCALE` | placement looseness multiplier (harness drives this itself) |
| `PACK_DEBUG=1` | print cluster/shelf packing decisions |
| `FREEROUTING_JAR` | pin a specific router jar (keep 1.9.0) |
| `KICAD_PATH` | an extra KiCad root (or bin dir) to probe when auto-detect misses it; every install found is still used per job (§8) |
| `PIPELINE_NO_CACHE=1` | bypass the route cache (always rebuild + re-route) |

**Route cache:** after a successful run, `<netlist>.cache.json` records a hash
of everything that moves copper (geometry, nets, net_specs, preroutes, board,
emc). A re-run with only value/sourcing/thermal edits reuses the routed board
— the G3/G4 **gates still re-run** against it (the cache skips work, never
judgement) — and goes straight to silk/checks/fab. The G3 re-validation uses
the `placement_routed` check variant: plane integrity is skipped (it is a
PRE-route prediction; on a routed board the plane is legitimately carved by
via antipads and any stranded islands are already tied by healed tracks —
G4's DRC re-run proves the connectivity it exists to predict). Any net,
footprint, position, or preroute change invalidates automatically, as does a
G2 netlist remediation.

## 4a. G4 — agent routing (KiCad board API)

The default G4 when Claude drives a board (the `agent-routing` skill, the
`router` subagent): no autorouter — the agent reads the board, lays copper
net by net, and KiCad's connectivity plus kicad-cli DRC verify it. The
backend is KiCad's IPC board API through the vendored kipy
(`server/backends/pcb_session.py` / `pcb_items.py` / `pcb_api.py`), the
geometry is pure Python (`server/route_planner.py`), the MCP surface is
`server/board_route_tools.py`.

**Session modes.** `mode="auto"` joins a running KiCad 9/10/11 PCB editor
that has the file open (API server enabled; edits land in the live document
and a save keeps that KiCad's format), else it launches a private headless
`kicad-cli api-server` from the KiCad 11 nightly with the board preloaded
(`server_status().board_api`, else `board_api_reason`). One headless board at
a time: another path drops an idle (saved) board — `released_boards` in the
response — and refuses while it has unsaved edits. KiCad's
`GetConnectedItems` (≥ 10.0.1) supplies real connectivity including filled
zones; a KiCad 9 GUI session reports `connectivity_available: false` and
`unrouted_connections` cannot count clusters there.

**Tools** (every one takes `pcb` first; responses carry `ok`, `backend: "api"`, `mode`; a user error is `{ok: false, reason}`):

| Tool | Returns |
|---|---|
| `open_board(pcb, mode="auto")` | overview + `{opened, mode, kicad_version, connectivity_available}` |
| `board_overview(pcb)` | `{copper_layers, outline, rules, netclasses, planes, footprints, counts, unrouted}` — layers, which inner layer carries which plane (`In1.Cu` GND, `In2.Cu` first power rail on the G3 build), board minimums, per-net class sizes |
| `board_pads(pcb, net="", ref="")` | `{pads: [{id, ref, pad, net, x, y, layers, through_hole, shape, w, h, rotation, drill}]}` |
| `unrouted_connections(pcb, net="", with_obstacles=True, clearance=0)` | `{unrouted, connections: [{net, from, to, length, obstacles: {"F.Cu": [...], "B.Cu": [...]}}], connectivity_available}` — N clusters of a net → N−1 closest-pair links; obstacles = foreign copper within clearance + width/2 of the straight line, with `distance` (centreline to copper edge) and `t` |
| `route_net(pcb, net, path, width=0, allow_narrow=False, via_diameter=0, via_drill=0)` | `{created: {tracks, vias}, ids, length, warnings}` — `path` = `[{layer, points: [[x, y], ...], width?}, {via: [x, y], diameter?, drill?}, ...]` in mm; validated (via exactly at the join, sizes ≥ board rules and class unless `allow_narrow`; non-45° bends, acute turns and same-layer vias warn) then created |
| `add_vias(pcb, net, points, diameter=0, drill=0)` / `remove_routing(pcb, net="", ids=[])` | `{created, ids}` / `{removed: {tracks, vias}}` |
| `route_check(pcb, refill=True, save=True, max_items=40)` | refill zones, save if the board has unsaved changes (an untouched board is never rewritten), kicad-cli DRC at error severity with the writer's kicad-cli → `{ok, errors, unconnected, types, violations: [{type, severity, description, items: [{description, x, y}]}], unconnected_items, kicad_cli, report, saved, save_note?, zones_refilled}` |
| `route_status(pcb, netlist="")` | `{nets: {net: {pads, clusters, complete, tracks, vias, length, min_width, class_width, width_ok}}, unrouted, complete, width_issues, via_ampacity_issues, passed}` — the routed gate; `check_design_gates(stage="routed")` answers with it on a board the SWIG checks cannot read |
| `save_board(pcb)` / `close_board(pcb, save=True)` / `list_open_boards()` | `{saved, path}` / `{closed, saved, discarded_changes}` / `{boards: [{path, mode, unsaved_changes}]}` |

**Loop.** `board_overview` → `unrouted_connections` → route 3–6 nets with
`route_net` (decoupling and clocks first, then signals, then power; plane
nets by a short stub + via) → `route_check` → fix every violation by
`remove_routing` + re-route → repeat until `ok` and `unrouted` 0 →
`route_status(netlist).passed` → `export_fab_package`. The skill holds the
engineering rules (exit direction, 45° geometry, clearance math on the
obstacle list, via placement, plane stubs, rip-up limits, when to stop).

**File format — the rule that bites.** A board saved through the headless
KiCad 11 session is a KiCad 11 file (`(version 20260901)`,
`(generator_version "10.99")`). KiCad 9/10, their `kicad-cli` and their pcbnew
Python cannot load it (`Failed to load board`). So: `autoroute_board`,
`check_design_gates(stage="placement")` and `add_ground_stitching` refuse it
(`toolchain.swig_readable`, the reason names `route_check` / `route_status`),
DRC / Gerbers / render run through kicad-cli 11 because `toolchain.board_cli(pcb)`
picks the writer's kicad-cli from the header (§8), and the file opens in
KiCad 11 only. Freerouting is therefore a fallback for a KiCad 9 format board
only — copy the `.kicad_pcb` + `.kicad_pro` before the first `route_check` if
that fallback must stay available. A GUI session keeps the editor's format.

## 5. Regression suite

```powershell
<venv>\python pipeline\run_all.py
```
Runs every board in `pipeline_tests/` (QFN, LQFP, multi-unit op-amps, buck
converter, RF coupon + LNA, ESP32 node with antenna overhang, the fixed-size
two-sided HAT pattern) **plus a negative fixture**: a buck with its input
decoupling deliberately stripped, which must FAIL G2, auto-remediate, and
then pass — proving the self-feeding loop on every run. Green = safe to ship
a tool change.

## 6. Post-route fixers (standalone)

Both run under KiCad's bundled python and never touch routed copper logic —
safe on a finished board, then re-run DRC and re-export:

```powershell
# silkscreen: refs adjacent to their part, clamped inside the outline
<kicad>\bin\python.exe server\tools\silk_place.py board.kicad_pcb

# heal micro-gaps / link plane islands / add ampacity vias
<kicad>\bin\python.exe server\tools\swig_board_fixes.py board.kicad_pcb netlist.json board_drc.json all
```

## 7. Known traps (cost real hours — read before debugging)

1. **Freerouting is deterministic per-DSN.** Re-running an identical board
   never clears a stuck item. Perturb placement (`pack_scale`) or fix the
   actual cause.
2. **Freerouting 1.9 cannot neck down.** Wide width-class into a small pad =
   unroutable. Preroute fat copper; exempt those nets from width classes
   (the harness does this automatically for preroute/plane nets).
3. **DSN plane abstraction lies.** KiCad exports zones as full-rect polygons;
   Freerouting cannot see fill voids, so plane-connected nets can come back
   with orphan islands. Division of labor: G3 statically catches planes that
   are ALREADY fractured (blocker) or pinched below 1.8 mm (warning) — but
   the worst case (HAT VBUS_PD) was created by the ROUTER's own via antipads
   sealing a healthy ≥3 mm corridor, which no static pre-route check can
   see. That class belongs to the G4 healer (bridge / island-link / BFS maze
   route), which the `negative_g4_heal` fixture proves on every regression
   run. For chronic nets, author a corridor preroute.
4. **Freerouting 2.x is broken headless** (phones home, never routes; also
   re-splits CLI paths on spaces). The server routes from a temp dir and pins
   1.9.0.
5. **KiCad 10 SWIG padstack traps:** `PCB_VIA.GetWidth()` without a layer arg
   asserts and **wedges the process** on Windows — pass `pcbnew.F_Cu`.
   Headless `ZONE_FILLER` segfaults on an in-memory `CreateEmptyBoard` board —
   save, `LoadBoard` fresh, then fill. `fp.Flip()` before `board.Add()`
   segfaults (flip after adding).
6. **`.kicad_pro` overrides board rules in `kicad-cli` DRC** — the project
   file must carry the fab profile limits or a clean board still fails.
7. **kicad-cli `-o "dir\"`** (quoted trailing backslash) mangles the path.
8. **Via ampacity (IPC-2221):** one 0.3 mm-drill via ≈ 0.9 A, 0.9 mm ≈ 2 A at
   ΔT 10 °C. High-current layer transitions need parallel barrels — THT
   component legs on the net count too.
9. **Two-sided placement:** PTH obstacles (THT pads, thermal-via farms) exist
   on BOTH faces — carve `auto_b` away from them or G3 reports
   "PTH inside courtyard".
10. **No headless teardrops.** KiCad 10's teardrop generator is not exposed
    to SWIG at all — `"emc": {"teardrops": true}` can only remind you to
    generate them in the GUI (Edit → Edit Teardrops) before fab.
11. **HAT/fixed-board pattern:** fixed connector coordinates + `auto_x/y/w/h`
    free field + hand-placed power chain + preroutes is the proven recipe for
    a dense fixed-outline board.
12. **A schematic written by KiCad 11 is a KiCad 11 file.** KiCad 9/10 and
    their `kicad-cli` cannot open it (§8). And a KiCad 11 that has never been
    opened has no library tables: the headless server then reports
    `symbol Device:R not found` (§8, first-run gotcha).
13. **A board saved by the headless KiCad 11 board session is a KiCad 11
    file** (§4a). Freerouting, `check_design_gates(stage="placement")`,
    `add_ground_stitching` and KiCad 9/10 cannot read it afterwards; DRC and
    Gerbers still work (kicad-cli 11 via `board_cli`). Copy the KiCad 9 file
    before the first `route_check` if the Freerouting fallback must survive.

## 8. Schematic backends

G2 (and every schematic MCP tool) runs on one of two backends. Every response
and the pipeline report carry `backend`:

| Backend | What does the work | When |
|---|---|---|
| **`api`** | KiCad 11's IPC schematic API through the vendored `kipy` (`server/backends/`): KiCad places the library symbols, wires, labels, no-connects and PWR_FLAGs and writes the file. Headless — the server launches its own private `kicad-cli api-server` — or **live in a running KiCad 11 editor** that has the schematic open (`load_schematic` reports `mode: "gui"`; edits land in the open document) | a KiCad ≥ 10.99 whose `kicad-cli api-server --help` works is installed (`server_status().schematic_backend == "api"`) |
| **`file`** | `server/tools/sch_build.py` (pipeline) / `server/sch_file_model.py` (interactive tools): S-expressions written by us, symbols flattened and embedded from the newest install's libraries, KiCad 9/10 file format | no KiCad 11, or `backend="file"` requested |

**Selection.** `backend="auto"` (the default everywhere, including
`run_pipeline.py`) picks `api` when `server/kicad_env.py` finds an install with
`has_api_server` and the vendored kipy imports, else `file`; `"api"` insists
(error when unavailable), `"file"` forces the fallback. Both backends produce
the same topology (grid cell per unit, a label at every net pin, no-connect
markers, PWR_FLAG on every undriven power net) so G2's ERC-0 contract and
everything downstream are identical.

**Which kicad-cli.** `kicad_env` discovers every KiCad (`KICAD_PATH`,
`Program Files\KiCad\*`, `LOCALAPPDATA\Programs\KiCad\*`, PATH, `/usr/bin`,
`/usr/local/bin`, the macOS bundle), probes each once (`kicad-cli version`,
bundled Python `import pcbnew`, `kicad-cli api-server --help`) and picks per
job: the **newest** kicad-cli runs the **schematic** jobs — ERC and SVG render
(`pick_cli` / `toolchain.kicad_cli`: a file written by KiCad N needs kicad-cli
≥ N, and the API backend writes KiCad 11 files); the **newest install with a
pcbnew-capable Python** builds the board and supplies its footprint libraries
(`pick_swig` — KiCad 9/10; KiCad 11 dropped the SWIG bindings). **Board jobs
use the kicad-cli of the KiCad that WROTE the board file:** DRC, Gerbers,
drill, pos, PNG render go through `toolchain.board_cli(pcb)`, which reads the
file header (`toolchain.board_file_info`: `(version N)`, `(generator_version
"9.0" | "10.99")`) and picks the install whose version starts with that
generator version — 9.0.4 for a pcbnew-built board, so a nightly never changes
the DRC semantics or the Gerber output of a stable-built board; the 10.99
nightly for a board saved by the KiCad 11 board API (§4a). Without that
release it takes the oldest install that can read the file (a KiCad N cli
reads files up to N), else the newest with a `kicad_cli_note` on the result;
without a readable header, the pcbnew install's kicad-cli, else the newest.
The **newest with `api-server`** serves the schematic API and the headless
board API (`pick_api`). A mixed machine (9.0.4 stable + 10.99 nightly)
therefore builds and ERCs the schematic with 11, builds, checks and exports a
Freerouting-routed board entirely with 9, and checks and exports an
agent-routed board with 11. `server_status()` reports `kicad_cli_schematic`,
`kicad_cli_board`, `kicad_cli_policy`, `board_api` and `installs`; the
pipeline report's `drc.kicad_cli` and every board job's `kicad_cli` record
which binary ran.

**Files written by 11 need 11.** The API backend saves KiCad 11 format
(schematic `version 20260830`, board `version 20260901`); KiCad 9/10 and their
`kicad-cli` cannot load it — which is why schematic ERC/render always use the
newest kicad-cli and board jobs follow the file's writer. Open such a
schematic or board in KiCad 11 only. If a schematic must stay a 9/10 file,
pass `backend="file"`; a board stays a 9/10 file as long as it is never saved
through the headless board session (§4a) — route it in a KiCad 9/10 GUI
session or with Freerouting instead. The SWIG tools (`swig_readable`) refuse
a board they cannot read with the reason and the next step.

**First-run gotcha (sym-lib-table).** A freshly installed nightly has no
library tables in its config dir (`%APPDATA%\kicad\10.99`, `~/.config/kicad/10.99`),
so the headless server finds no libraries: `symbol Device:R not found`. Open
KiCad 11 once (it writes the tables) **or** copy `sym-lib-table` and
`fp-lib-table` from `<install>/share/kicad/template` into that config dir.

**Headless limits.** One headless document at a time: opening another path
drops an idle (saved) session — reported in `released_sessions` — and refuses
while the earlier one has unsaved edits. Paper size and label rotation are not
settable through the API (the response carries a `note`).

**Vendored kipy.** `server/vendor/kipy/` is kicad-python `main` (0.9.0.dev0)
with protobuf classes generated from KiCad master's `api/proto` — the PyPI
0.8.0 release predates the schematic API. `kicad_server.py` puts `server/vendor`
first on `sys.path`, so never `pip install kicad-python` into the server venv;
its runtime deps (`protobuf`, `pynng`, `jsonschema`, `zstandard`,
`typing_extensions`) are in `server/requirements.txt`. Refresh with
`server/tools/update_kipy.py` (clone, fetch protos, generate, rewrite imports,
vendor — `server/vendor/README.md` records the origin and the recipe).

## 9. Where things live

```
server/kicad_server.py        MCP server + schematic session registry (also importable:
                              run_pipeline uses it as a library)
server/kicad_env.py           discovers every KiCad install; pick_cli / pick_swig / pick_api
server/toolchain.py           per-job picks (kicad-cli by job and by board-file writer,
                              pcbnew Python, libraries, Java, jar); board_file_info,
                              swig_readable (the SWIG refusal for KiCad 11 boards)
server/kicad_ipc.py           GUI connection + open-document guard
server/backends/              KiCad 11 API schematic backend (sch_api, sch_session,
                              sch_netlist_build, sch_geometry, sch_items, sch_erc) and the
                              board API backend (pcb_session, pcb_items, pcb_api; errors)
server/route_planner.py       pure routing geometry: connections, obstacles, validate_path,
                              width_compliance, via_ampacity (no kipy)
server/board_route_tools.py   agent-routing MCP tools (open_board ... route_status, §4a)
server/sch_file_model.py      file-model fallback for the interactive schematic tools
server/vendor/kipy/           vendored kicad-python (see server/vendor/README.md)
server/pcb_ipc_tools.py       live PCB tools over IPC;  server/board_pipeline_tools.py: G3-G5 shell-outs
server/tools/                 sch_build, swig_build_board, gates, remediate,
                              swig_gate_checks, swig_board_fixes, silk_place,
                              netclass_writer, ipc_rules (IPC-2221/2141A math),
                              lint_netlist, design_report, emc_stitch, update_kipy,
                              freerouting-1.9.0.jar
server/pins/                  pin sidecars (file-backend wiring coordinates)
pipeline/run_pipeline.py      the gated harness (single board, Freerouting at G4)
pipeline/run_all.py           regression suite + negative fixture
skills/agent-routing/         the routing method the agent follows (§4a);  agents/router.md: the
                              routing subagent
tests/                        pytest tier (pure + live API tests, skipped without KiCad 11)
docs/PIPELINE.md              this file
```
