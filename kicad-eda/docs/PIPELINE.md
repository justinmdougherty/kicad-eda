# The Gated Headless Pipeline — concept → Gerbers, no GUI

This is the authoritative run-book for the netlist-first pipeline. It is the
process that produced the RPi 5 battery-emulator HAT fab package (65×56.5 mm,
68 parts, two-sided, 4-layer, 0 DRC errors, JLCPCB zip + BOM/CPL) and that the
10-board regression suite proves on every tool change.

```
netlist.json ──> G1 lint ──> G2 schematic+ERC ──> G3 place (SWIG) ──> G4 route
 (source of      structure    sch_build.py         swig_build_board    Freerouting
  truth)         + intent     ERC must be 0        courtyard DRC = 0   + DRC + checks
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
| **Python 3.10+** | MCP server + harness venv | python.org / OS package |
| **Java 17–21 JRE** | Freerouting 1.9.0 (bundled at `server/tools/freerouting-1.9.0.jar`) | `winget install EclipseAdoptium.Temurin.21.JRE` / `brew install temurin@21` |

Run `scripts/install-deps.ps1` (Windows) or `scripts/install-deps.sh`
(macOS/Linux) to check and install all three. The server auto-discovers
KiCad (`KICAD_PATH` env overrides), Java (`JAVA_HOME`, then a bundled
`server/tools/jre*`), and the Freerouting jar (`FREEROUTING_JAR` overrides —
**do not use Freerouting 2.x: it phones home and hangs headless; 1.9.0 is the
pinned router**).

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
- New parts need a **pin sidecar** for the schematic:
  `python server/tools/build_pin_sidecars.py` writes
  `server/pins/<Lib_Symbol>.pins.json`. The server reads the central
  `server/pins/` dir.
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
| **G2** Schematic | `sch_build` → `.kicad_sch`, `kicad-cli sch erc`, design-intent review (`design_report`: decoupling coverage, floating pins, pulls, test points), **thermal review** (`thermal_check`: shunt I²R vs 50%-derated rating, FET conduction loss, linear-reg (Vin−Vout)·I, switcher loss estimate; Tj = Tamb + P·θJA ≤ 80% Tj_max) | missing bypass → 100 nF auto-added at the IC, netlist rewritten, stage re-runs. Thermal: declared numbers (`p_diss`, `rds_mohm`, value "3W") block; estimates warn. Overrides per part: `"thermal": {"p_diss", "theta_ja", "tj_max", "i_max"}` (`i_max` for topology-capped parts, e.g. a bypassed sleep shunt); board-level: `"thermal": {"t_ambient", "tj_margin", "ep_vias_min"}` |
| **G3** Placement | `swig_build_board` (KiCad bundled python) → outline, mixed/two-sided placement, zones (In1=GND, In2=first power rail), preroutes, silk; courtyard DRC must be **0**; connector mouths must face off-board at the edge; dissipating exposed-pad parts must have ≥4 thermal vias under the EP (IPC-7093 — footprint `...ThermalVias` variants count); **plane integrity**: a power plane whose pad population is split across separate fill islands is a BLOCKER (the DSN export hides voids — the router will return the net broken); corridors under 1.8 mm between pad groups (one router via from sealing) are a warning. GND is exempt (post-route pours + stitching unify it) | courtyard overlap → re-place at next `pack_scale` (1.0→1.5→2.0→2.6→3.2); a retry that no longer fits restores the last good ROUTED snapshot (`.attempt` file) |
| **G4** Layout | DSN export → **Freerouting 1.9** → SES import → DRC + routed-width check + via-ampacity check (`swig_gate_checks`) | 1–4 unconnected → **heal in place** (`swig_board_fixes`): ≤2 mm same-net gaps bridged; plane-net orphans get island-link tracks (straight-or-L, plane layer preferred); under-ampacity transitions get parallel vias. 2-round fix→re-DRC→re-gate loop. Scale escalation only for real congestion. |
| **G5** Fab package | `kicad-cli` Gerbers + drill + pos; BOM.csv + CPL.csv (JLCPCB format) zipped | — |

Exit 0 = every gate passed; the JSON report (stdout) and gate summary
(stderr) name every blocker and every remediation applied.

Useful environment variables:

| Var | Effect |
|---|---|
| `PACK_SCALE` | placement looseness multiplier (harness drives this itself) |
| `PACK_DEBUG=1` | print cluster/shelf packing decisions |
| `FREEROUTING_JAR` | pin a specific router jar (keep 1.9.0) |
| `KICAD_PATH` | KiCad install root when auto-detect fails |
| `PIPELINE_NO_CACHE=1` | bypass the route cache (always rebuild + re-route) |

**Route cache:** after a successful run, `<netlist>.cache.json` records a hash
of everything that moves copper (geometry, nets, net_specs, preroutes, board,
emc). A re-run with only value/sourcing/thermal edits reuses the routed board
— the G3/G4 **gates still re-run** against it (the cache skips work, never
judgement) — and goes straight to silk/checks/fab. Any net, footprint,
position, or preroute change invalidates automatically, as does a G2 netlist
remediation.

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

## 8. Where things live

```
server/kicad_server.py        MCP server (also importable: run_pipeline uses it as a library)
server/tools/                 sch_build, swig_build_board, gates, remediate,
                              swig_gate_checks, swig_board_fixes, silk_place,
                              netclass_writer, ipc_rules (IPC-2221/2141A math),
                              lint_netlist, design_report, emc_stitch,
                              freerouting-1.9.0.jar
server/pins/                  pin sidecars (schematic wiring coordinates)
pipeline/run_pipeline.py      the gated harness (single board)
pipeline/run_all.py           regression suite + negative fixture
docs/PIPELINE.md              this file
```
