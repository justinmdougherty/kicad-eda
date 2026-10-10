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
| **Java 17+ JRE** | Freerouting 1.9.0 (bundled at `server/tools/freerouting-1.9.0.jar`; identical routes on Java 21 and 24) | `winget install EclipseAdoptium.Temurin.21.JRE` / `brew install temurin@21` |

Run `scripts/install-deps.ps1` (Windows) or `scripts/install-deps.sh` (macOS/Linux) to check and install the three required tools (the nightly is reported, never installed). The server discovers **every** KiCad on the machine (`server/kicad_env.py`; `KICAD_PATH` adds a root or bin dir), Java (a bundled `server/tools/jre*` first, then `PATH`, then `JAVA_HOME`) and the Freerouting jar (`FREEROUTING_JAR` overrides).

**Freerouting 1.9.0 is the pinned router.** Measured against 2.x on the ten example boards: 2.1.x is not reproducible (it reshuffles its work list with an unseeded random on every pass); 2.4.x is reproducible but needs Java 25 and routes worse on 4 of the 10 boards; 1.9.0 writes byte-identical SES files for one DSN on Windows and Linux. Every version phones home (1.9: Segment analytics and a GitHub version check that no flag turns off), so the server runs each Freerouting child offline and out of sight (§7, trap 4).

## 2. Author the netlist (`netlist.json`)

The single source of truth. Pad-level, hand-authored or generated. The
examples in this section are literal designs: `tests/test_netlist_schema.py`
strips the `//` comments and validates them against the schema.

```jsonc
{
  "name": "my_board",                  // file/project stem
  "title": "Human title",
  "class": 2,                          // IPC producibility class (1/2/3)
  "mode": "napkin",                    // hands-off run: decide + record, never
                                       //   stop to ask (see "assumptions")
  "copper_layers": 4,                  // 1 | even 2..32 (default 4); 1 = single-
                                       //   sided; must match the stackup (ST-E7)

  // PROJECT LIBRARIES — nickname -> path relative to this file (${KIPRJMOD}
  // = its directory; absolute allowed). Looked up before the project
  // sym-lib-table / fp-lib-table, then the stock libraries; the same nickname
  // in a project table with another URI is a lint error. Authored parts
  // default to libs/<name>_parts.kicad_sym + libs/<name>_parts.pretty.
  "libraries": {
    "symbols":    {"SPS_parts": "libs/SPS_parts.kicad_sym"},
    "footprints": {"SPS_parts": "libs/SPS_parts.pretty"}
  },

  // BOARD — omit entirely (or null) for auto-sized boards. Frame: mm, Y down,
  // origin = outline bbox top-left. For a FIXED outline:
  "board": {
    "w": 65.0, "h": 56.5,              // fixed rectangle, mm (both > 0)
    "corner_radius": 3.0,              // optional: round its corners (w/h only)
    // ...or any shape (w/h then optional) — exactly ONE of:
    //   "outline": {"loop": [[0,0],[60,0],[60,37,0.4142],[57,40],[0,40]]},
    //   "outline": {"dxf": "mech/outline.dxf", "layer": "OUTLINE", "units": "mm"},
    // loop vertex = [x, y] or [x, y, bulge] (DXF bulge = tan(theta/4) of the
    // arc to the next vertex); a DXF (Fusion: Y up) is normalised into this
    // frame; units mm|cm|in|mil, layer/units only with "dxf".
    "cutouts": [{"loop": [[20,10],[30,10],[30,20],[20,20]]},
                {"circle": [45.0, 20.0, 1.6]}],   // [cx, cy, r]
    "panel_cutouts": false,            // true: a connector may mate through a
                                       //   cutout edge (panel mount); default
                                       //   false: only the outer edge counts
    "datum": [32.5, 28.25],            // source (Fusion) origin in this frame ->
                                       //   KiCad aux/drill origin (STEP, pos);
                                       //   default: the bbox bottom-left (0, h)
    "auto_x": 8.5, "auto_y": 21.0,     // free-field rectangle where the
    "auto_w": 23.0, "auto_h": 25.0,    //   auto-placer may put TOP parts (w/h > 0)
    "auto_b": {"x":30,"y":26,"w":25,"h":22}  // ditto for BOTTOM-side parts
  },

  "mounting_holes": true,              // 4 corner M3 NPTH, refs H1-H4 — or
  //   "mounting_holes": {"footprint": "MountingHole:MountingHole_2.7mm_M2.5",
  //     "positions": [[3.5,3.5],[61.5,3.5],[3.5,52.5],[61.5,52.5]]},

  "components": [
    // auto-placed (default):
    {"ref":"U1","lib_id":"Interface_USB:STUSB4500QTR","value":"STUSB4500",
     "footprint":"Package_DFN_QFN:QFN-24-1EP_4x4mm_P0.5mm_EP2.7x2.7mm"},
    // fixed (mixed placement — connectors, power chain in current-flow order):
    {"ref":"J1","lib_id":"Connector:USB_C_Receptacle_USB2.0_16P","value":"USB-C",
     "footprint":"Connector_USB:USB_C_Receptacle_GCT_USB4105-xx-A_16P_TopMnt_Horizontal",
     "x":3.0, "y":43.0, "rot":270},
    // auto-placed connector on a chosen board edge: "edge" is top | bottom |
    // left | right (null or absent = top); its mouth faces off that edge.
    // Only on connectors (J* not "interior", or "edge_row" parts):
    {"ref":"J2","lib_id":"Connector:Conn_Coaxial","value":"SMA",
     "footprint":"Connector_Coaxial:SMA_Samtec_SMA-J-P-H-ST-EM1_EdgeMount",
     "edge":"right"},
    // bottom side (auto-placed into auto_b, or fixed with x/y):
    {"ref":"U4","lib_id":"Regulator_Linear:AMS1117-3.3","value":"AMS1117-3.3",
     "footprint":"Package_TO_SOT_SMD:SOT-223-3_TabPin2", "side":"B"},
    // a J* part that is NOT an edge connector (jumper, Kelvin header):
    {"ref":"J4","lib_id":"Connector_Generic:Conn_01x03","value":"SENSE",
     "footprint":"Connector_PinHeader_2.54mm:PinHeader_1x03_P2.54mm_Vertical",
     "interior":true},
    // module on the top edge, antenna 4 mm past Edge.Cuts:
    {"ref":"U2","lib_id":"RF_Module:ESP32-C3-WROOM-02","value":"ESP32-C3-WROOM-02",
     "footprint":"RF_Module:ESP32-C3-WROOM-02", "edge_row":true, "overhang":4.0},
    // assembly sourcing (carried into BOM.csv):
    {"ref":"C1","lib_id":"Device:C","value":"100nF",
     "footprint":"Capacitor_SMD:C_0805_2012Metric",
     "lcsc":"C15849", "mpn":"CL21B104KBCNNNC"},
    // assembly flags: "dnp": true = in the design but not populated (marked
    // DNP, left out of the assembly files); "in_bom": false = left out of the
    // BOM (default true):
    {"ref":"R7","lib_id":"Device:R","value":"0R",
     "footprint":"Resistor_SMD:R_0603_1608Metric", "dnp":true},
    {"ref":"TP1","lib_id":"Connector:TestPoint","value":"GND",
     "footprint":"TestPoint:TestPoint_Pad_D1.0mm", "in_bom":false},
    // G2 thermal / G3 height inputs (thermal keys: §4 table):
    {"ref":"Q1","lib_id":"Device:Q_NMOS","value":"PowerPAK NMOS",
     "footprint":"Package_SO:PowerPAK_SO-8_Single", "rds_mohm":4.5, "height":1.1,
     "thermal":{"p_diss":0.4, "theta_ja":60, "tj_max":150, "i_max":3.0}},
    // fixed by footprint ORIGIN + final rotation (as KiCad shows it; for
    // side "B" the post-flip one) — Fusion coordinates, keep-mode rebuilds.
    // A part has "at" OR "x"/"y"/"rot", never both:
    {"ref":"U5","lib_id":"MCU_Microchip_SAMD:ATSAMD21G18A-A","value":"ATSAMD21G18A",
     "footprint":"Package_QFP:TQFP-48_7x7mm_P0.5mm", "at":[30.0, 20.0, 90]},
    // board-only part (hole, logo, fiducial): on the board, not in the
    // schematic — "lib_id" may be omitted:
    {"ref":"H5", "footprint":"MountingHole:MountingHole_3.2mm_M3",
     "board_only":true, "at":[3.5, 3.5, 0]}
  ],

  // pad-level connectivity: net name (non-empty) -> [[ref, pad], ...]
  "nets":      {"GND":  [["U1","10"],["U1","25"],["C1","2"],["TP1","1"],
                         ["J1","A1"],["J1","B1"]],
                "VBUS": [["J1","A4"],["J1","B4"],["U1","24"],["C1","1"]]},
  "no_connect": [["U1","3"]],
  "power_flags": ["VBUS"],             // extra PWR_FLAG nets (ERC)
  "planes": {"In1.Cu":"GND", "In2.Cu":"+3V3"},  // default: In1 GND, In2 1st rail
  "stackup": {"h":0.20, "er":4.2},     // legacy shorthand (ST-W7): one unmasked
                                       //   outer ply; the stackup model (preset or
                                       //   layers): see the stackup / RF example
  "require_sourcing": true,            // G5: missing lcsc/mpn = blocker
  "thermal": {"t_ambient":50, "tj_margin":0.8, "ep_vias_min":4},  // board-level;
                                       //   ep_vias_min: whole number (4.0 passes)

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
  ],

  // IMPORTED / REBUILT DESIGNS add "source", "rebuild", "variant" and
  // "blocks": see the second example below.

  // ASSUMPTIONS — every requirement-level guess of a napkin run, listed first
  // in <stem>_review.md (low confidence first); "override" (optional) names
  // what to change to overrule it:
  "assumptions": [
    {"id":"A1", "topic":"input supply", "value":"5 V USB-C",
     "rationale":"concept says 'USB powered'", "confidence":"med",  // high|med|low
     "override":"net_specs.VBUS.voltage"}
  ]
}
```

An **imported or rebuilt design** carries its provenance. The converters,
`rebuild_variant` and `design_blocks.compose` write these keys; nobody types
them by hand:

```jsonc
{
  "name": "logger_v2",
  "rebuild": "reflow",                 // keep = original placement via "at";
                                       //   reflow = re-place (new form factor)
  "board": {"w": 70.0, "h": 45.0, "corner_radius": 2.0},
  "mounting_holes": true,

  // SOURCE — provenance. Paths are relative to this file (or absolute).
  // kind: kicad | eagle | altium | easyeda | vision (PDF/image schematic) |
  // gerber (Gerber-only reconstruction):
  "source": {
    "kind": "altium", "path": "orig/logger.PcbDoc",
    "sha256": "a53e676586fa0875b6e4b0b8bf99ba4233a1491cb61077d9a2b6f8b59d09b01c",
    "tool": "kicad-cli 10.99 import + kicad_import",
    "files": {                         // every original input: name -> sha256
      "logger.PcbDoc": "a53e676586fa0875b6e4b0b8bf99ba4233a1491cb61077d9a2b6f8b59d09b01c",
      "logger.SchDoc": "a6de381c3562450ead9bd7930fbe7921d8c571ee125f327aeb589bf163c5dc56"},
    "via": "_foreign/logger.kicad_pro",      // the KiCad project a foreign
                                             //   import went through
    "reference": "reference_padnets.json",   // source pad nets as imported:
                                             //   the equivalence target
    "erc_baseline": "source_erc.json",       // ERC judged against it (no NEW
                                             //   violations)
    "drc_baseline": "source_drc.json",       // source DRC, informational
    "import_report": "import_report.json",   // default <netlist dir>/import_report.json
    "board": "orig/logger.PcbDoc",           // a source board KiCad 9/10 pcbnew or
                                             //   swig_load_foreign can load
    "copper": "reroute"                // carry = copy the source copper instead
                                       //   of routing (keep rebuilds) | reroute
                                       //   (default: route afresh)
  },

  // VARIANT (rebuild_variant) — how this design was derived from the import:
  // "from" = the netlist it was made from; "dropped_refs" = parts it removed
  // (artwork, holes); "holes" = what became of the source's mounting holes:
  // source (mounting_holes with their footprint) | corners (mounting_holes:
  // true) | none (dropped):
  "variant": {"mode": "reflow", "from": "../logger_import/netlist.json",
              "dropped_refs": ["LOGO1", "H1", "H2"], "holes": "corners"},

  // BLOCKS (design_blocks.compose) — one entry per composed block: its own
  // "source" (or null) and "rebuild"; "ref_map" = EVERY block ref old -> new
  // (identity entries included); "net_map" = block net -> net here;
  // "drop_refs" = BASE refs this compose step removed (here the micro-USB
  // connector the USB-C block replaces):
  "blocks": [{"name": "USBC", "source": null, "rebuild": "reflow",
              "ref_map": {"J1": "J10", "R1": "R30"},
              "net_map": {"GND": "GND", "CC1": "USBC_CC1"},
              "drop_refs": ["J1"]}],

  "components": [
    {"ref":"U1","lib_id":"MCU_Microchip_SAMD:ATSAMD21G18A-A","value":"ATSAMD21G18A",
     "footprint":"Package_QFP:TQFP-48_7x7mm_P0.5mm"},
    {"ref":"C1","lib_id":"Device:C","value":"1uF",
     "footprint":"Capacitor_SMD:C_0603_1608Metric"},
    {"ref":"J10","lib_id":"Connector:USB_C_Receptacle_USB2.0_16P","value":"USB-C",
     "footprint":"Connector_USB:USB_C_Receptacle_GCT_USB4105-xx-A_16P_TopMnt_Horizontal",
     "edge":"left"},
    {"ref":"R30","lib_id":"Device:R","value":"5.1k",
     "footprint":"Resistor_SMD:R_0603_1608Metric"}
  ],
  "nets": {"GND":      [["U1","18"],["C1","2"],["J10","A1"],["J10","B1"],["R30","2"]],
           "+3V3":     [["U1","17"],["C1","1"]],
           "USBC_CC1": [["J10","A5"],["R30","1"]]}
}
```

A **stackup / controlled-impedance / RF design** (ADR D7) declares its layer
count with a rationale, an arbitrary stackup and the RF intent of its nets.
The `rf` block, the RF `net_specs` keys and `clock_hz` are accepted and typed
now; the RF rule families that act on them arrive in wave 2b:

```jsonc
{
  "name": "lora_gnss_node",
  "mode": "napkin",
  "copper_layers": 4,                  // 1 | even 2..32; must match the stackup
  "layer_policy": {"max_layers": 6, "fab": "pcbway"},  // napkin hints (layer
                                       //   scoring is wave 3); optional

  // STACKUP — exactly one of two shapes:
  //  * legacy shorthand {"h": 0.20, "er": 4.2, "t": 0.035} (no other key): one
  //    unmasked outer microstrip over the 4-layer default (ST-W7);
  //  * the model (unknown keys rejected) with at most one of
  //    "preset": "jlcpcb:JLC04161H-3313" (+ "roles": {"In2.Cu": {"role":
  //    "mixed", "net": "+3V3"}} overriding the preset's roles) or "layers"
  //    (below); neither = the default geometry for copper_layers.
  "stackup": {
    "fab": "pcbway",                   // capability lint + Dk / mask basis; null = none
    "board_thickness": 1.4,            // ordered nominal (mm) vs the computed sum (ST-W1)
    "finish": "ENIG",                  // HASL | LF-HASL | ENIG | OSP | ImAg | ImSn | null
    "mask": {"material": "lpi", "dk": 3.8, "df": 0.025, "color": "Green",
             "over_substrate": 0.0305, "over_trace": 0.0152, "between_traces": 0.0305},
                                       // omitted = the fab's mask, else generic LPI;
                                       //   null = no mask (ST-W9)
    "dk_policy": "design",             // auto | fab | design | process | typical
    "design_freq_hz": 1.575e9,         // default: the highest net_specs freq_hz, else 1e9
    "impedance_control": {"required": true, "tolerance_pct": 10},
    "via_spans": [["F.Cu", "B.Cu"]],   // through vias only (ST-E8)
    "rationale": "4L: Rogers under the RF feeds, solid GND under both feeds",
    "origin": "designer",              // layer_policy = written by the napkin default
    "layers": [                        // top -> bottom, copper first and last
      // copper: "name" is positional (F.Cu, In1.Cu.., B.Cu); "oz" defaults to 1
      // outer / 0.5 inner, "thickness" to oz x 0.035 mm; "role" signal | plane
      // | mixed | unused (B.Cu of copper_layers 1); "net" on plane / mixed:
      {"type": "copper", "name": "F.Cu", "oz": 1, "role": "signal"},
      // dielectric: "kind" core | prepreg; one ply ("thickness", "material",
      // "construction", "dk", "df", "dk_freq_hz", "label", "locked",
      // "dielectric_model") or "plies"; an explicit dk wins over the library:
      {"type": "dielectric", "kind": "core", "material": "rogers.ro4350b",
       "thickness": 0.254, "locked": true},
      {"type": "copper", "oz": 1, "role": "plane", "net": "GND"},
      {"type": "dielectric", "kind": "prepreg", "plies": [
         {"material": "rogers.ro4450f", "thickness": 0.101},
         {"material": "isola.370hr", "construction": "2116", "thickness": 0.12}]},
      {"type": "copper", "oz": 0.5, "thickness": 0.0175, "role": "plane", "net": "+3V3"},
      {"type": "dielectric", "kind": "core", "material": "FR-4 core", "label": "FR-4 0.71",
       "dk": 4.6, "df": 0.02, "dk_freq_hz": 1e9, "thickness": 0.71},
      {"type": "copper", "oz": 1, "role": "signal"}]
  },

  "components": [
    {"ref":"U1","lib_id":"MCU_Microchip_SAMD:ATSAMD21G18A-A","value":"ATSAMD21G18A",
     "footprint":"Package_QFP:TQFP-48_7x7mm_P0.5mm",
     "clock_hz":[4.8e7, 32768]},       // clocks (Hz) for the GNSS harmonic check
    {"ref":"U2","lib_id":"RF_Module:RFM95W-915S2","value":"RFM95W",
     "footprint":"RF_Module:HOPERF_RFM9XW_SMD"},
    {"ref":"U3","lib_id":"SPS_parts:LG290P","value":"LG290P",
     "footprint":"SPS_parts:Quectel_LG290P_LGA-79"},
    {"ref":"J2","lib_id":"Connector:Conn_Coaxial","value":"U.FL",
     "footprint":"Connector_Coaxial:U.FL_Hirose_U.FL-R-SMT-1_Vertical"},
    {"ref":"AE1","lib_id":"Device:Antenna","value":"IFA 915 MHz",
     "footprint":"SPS_parts:IFA_915MHz"},
    {"ref":"C10","lib_id":"Device:C","value":"NM","footprint":"Capacitor_SMD:C_0402_1005Metric"},
    {"ref":"R2","lib_id":"Device:R","value":"0R","footprint":"Resistor_SMD:R_0402_1005Metric"},
    {"ref":"C11","lib_id":"Device:C","value":"NM","footprint":"Capacitor_SMD:C_0402_1005Metric"}
  ],
  "nets": {"GND":      [["U1","18"],["U2","1"],["U3","10"],["J2","2"],["C10","2"],["C11","2"]],
           "+3V3":     [["U1","17"],["U2","13"],["U3","1"]],
           "LORA_RF1": [["U2","9"],["C10","1"],["R2","1"]],
           "LORA_ANT": [["R2","2"],["C11","1"],["AE1","1"]],
           "GNSS_RF":  [["J2","1"],["U3","25"]]},

  // RF intent per net: "freq_hz" turns the RF rules on; every key is optional.
  // Diff pairs add "pair": "edge" (default) | "broadside" ("layers": [La, Lb]).
  "net_specs": {
    "LORA_ANT": {"impedance": 50, "freq_hz": 9.15e8, "role": "tx",   // tx | rx | trx
                 "line": "cpwg",       // auto | microstrip | cpwg | stripline |
                                       //   dual_stripline | embedded_microstrip | cpw
                 "layers": ["F.Cu"],   // allowed layers (RF default: the primary outer)
                 "ref_layers": {"F.Cu": ["In1.Cu"]}, "void_layers": {"F.Cu": []},
                 "ref_nets": ["GND"],  // RF references must carry these (ST-E10)
                 "width": null, "ground_gap": null,   // explicit = checked, not solved
                 "z_tol_pct": 5, "max_vias": 0, "max_len_mm": 25, "stub_max_mm": null,
                 "fence": {"pitch_mm": null, "offset_mm": null, "via": [0.6, 0.3],
                           "rows": 1, "freq_hz": null},          // or false
                 "isolation_mm": 1.0, "rf_group": "LORA", "ref_plane_margin_h": 5,
                 "ref_plane_exempt": [{"rect": [2.0, 10.0, 3.0, 2.5], "layers": ["In1.Cu"],
                                       "why": "SMA pin relief"}],
                 "allow_branches": [["C11", "1"]],
                 "route": "preroute"}, // auto | preroute | agent (never Freerouting)
    "LORA_RF1": {"impedance": 50, "freq_hz": 9.15e8, "rf_group": "LORA"},
    "GNSS_RF":  {"impedance": 50, "freq_hz": 1.57542e9, "role": "rx", "rf_group": "GNSS"}
  },

  // RF BLOCK — chains, group isolation, antennas, coexistence (entries reject
  // unknown keys):
  "rf": {
    "chains": [{"name": "LORA_TX", "group": "LORA", "from": ["U2", "9"], "to": ["AE1", "1"],
                "nets": ["LORA_RF1", "LORA_ANT"], "series": ["R2"], "shunts": ["C10", "C11"],
                "match_max_dist_mm": 2.0, "max_len_mm": 25, "tx_power_dbm": 20}],
    "isolation": [{"groups": ["LORA", "GNSS"], "min_mm": 15, "edges_differ": true}],
    "antennas": [{"ref": "AE1",
                  "kind": "pcb_trace",           // pcb_trace | chip | module | connector
                  "preset": null,
                  "generator": {"family": "ifa", // ifa | mifa | meander | chip_land
                                "band_hz": [9.02e8, 9.28e8], "params": {}},
                  "feed_net": "LORA_ANT", "ground_net": "GND",
                  "keepout": {"layers": "all", "from_footprint": true,  // or [layers]
                              "rect": null, "polygon": null},
                  "ground_under": false, "edge": "top", "clearance_mm": 5,
                  "license_ack": false,
                  "match": {"topology": "pi", "refs": ["C10", "R2", "C11"], "max_dist_mm": 2.0,
                            "fit": {"C10": "NM", "R2": "0R", "C11": "NM"}},
                  "sim": "none",
                  "human_steps": ["VNA-tune the pi network on the assembled board"]}],
    "coexistence": {
      "check": true,
      "tx": [{"group": "LORA", "band_hz": [9.02e8, 9.28e8], "power_dbm": 20}],
      "rx": [{"group": "GNSS", "bands_hz": [[1.559e9, 1.606e9], [1.164e9, 1.189e9]],
              "abs_max_dbm": 10, "oob_rejection_db": 30, "lna_gain_db": 43}],
      "isolation_db": [{"from": "LORA", "to": "GNSS", "db": 20}],
      "margin_db": 6}}
}
```

**Schema.** `server/tools/netlist_schema.py` is the machine-checked form of
these blocks (JSON Schema 2020-12). Every key above is typed. Unknown keys
pass at the top level, on components and on the provenance records
(`source`, `variant`, a `blocks` entry). `libraries`, `board.outline` and a
cutout reject them, so a typo cannot silently fall back to a rectangle. The D7
keys come from `server/tools/netlist_schema_d7.py`: the stackup model, its
layer entries, `mask`, `impedance_control`, `layer_policy`, `rf` and every
entry of `rf` (and a `fence` object) reject unknown keys too; the legacy
shorthand takes only `h`/`er`/`t`. The stackup semantics (alternation,
positional names, roles vs `planes`, references, fab capability, layer-count
floors) are G1 lint's, `server/tools/lint_rules_stackup.py`: `ST-E*` errors and
`ST-W*` warnings, each with its JSON path.
Required: `components` (each with `ref`, `footprint`, and `lib_id` unless
`board_only`); `source` needs `kind` + `path`; `variant` needs `mode`; a
`blocks` entry needs `name` + a non-empty `ref_map`; an assumption needs `id`,
`topic`, `value`, `rationale`, `confidence`. Board `w`/`h`, `auto_w`/`auto_h`
and `auto_b` `w`/`h` must be > 0; a net name must not be empty. Integers are
strict (`4.0` is not a layer count) except `thermal.ep_vias_min`, which its
consumers read via `int()`. `null` means "absent" for component `edge` and for
a block's `source` / `rebuild`. Check a file in a second; every error carries
its JSON path (`$.components[3].at: ... (component U5)`):

```powershell
<venv>\python server\tools\netlist_schema.py path\to\netlist.json   # --schema prints it
```

**Board block contracts.** The schema checks types; `server/tools/outline.py`
(`from_board_block`) enforces the geometry when the board is resolved:

- **Outline loop bbox starts at (0, 0).** An `"outline"` loop is in the board
  frame (translate it; the DXF loader normalises Fusion outlines for you), and
  a `"w"`/`"h"` next to it must equal that bbox.
- **corner_radius only with w/h.** It rounds the `"w"` x `"h"` rectangle; with an
  `"outline"`, draw the fillets into the loop as bulge vertices.
- **Default datum = the outline bbox bottom-left**, (0, h) in this frame, when
  `"datum"` is absent. The exported outline DXF uses it as its origin, and it
  is the point that becomes the KiCad aux/drill origin.
- **An `auto_*`-only board block is rejected on purpose.** The `"auto_*"`
  fields are windows inside a FIXED board, so a block needs `"w"` + `"h"` or
  an `"outline"`. For an auto-sized board, omit the block (or set it to null).

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

```text
<venv>\python pipeline\run_pipeline.py <netlist.json> [<board_w> <board_h>] [placement.json]
              [--strict] [--route auto|freerouting|agent] [--resume routed|stalled]
```

- **Board size.** `<board_w> <board_h>` (mm) are optional, both or neither: without them the netlist `board` w/h, else a feature's `board_dims` hook (a drawn outline), else the builder fits an auto-placed board to its parts. A design whose every part is fixed and that has no board block is a usage error (nothing to size the board from). `placement.json` (`{ref: [x, y, rot]}`, `-` = none) overrides positions and gets one try at pack_scale 1.0.
- **Mode.** Napkin behaviour (§4b) is the default; `--strict` turns its policies off (a netlist `"mode": "napkin"` still means napkin).
- **Routing.** `--route auto` (default): Freerouting first, the router subagent when G4 then fails; `--route freerouting`: a G4 failure is final; `--route agent`: the placed board goes to the agent as soon as it passes G3, without Freerouting. The modes act on a route-cache miss (a hit is re-judged as usual); the handoff is §4a.
- **Resume.** `--resume routed|stalled` continues a run that handed routing off (§4a). It takes no placement file and no `--route`, and never hands off again.

| Exit | Meaning |
|---|---|
| `0` | PASS: every gate passed |
| `1` | FAIL: a gate failed, a resumed run could not continue, or the run crashed |
| `2` | usage error: bad arguments, an unreadable netlist or placement file, no board size to start from |
| `3` | handoff: routing went to the agent (`<stem>_route_handoff.json`, §4a); never a PASS |

**Artifacts**, beside the netlist (`<stem>` = its path without `.json`). On **every** exit — pass, gate failure, handoff, crash, or a usage error found once the command line parsed (when the netlist's folder exists) — the run writes `<stem>_pipeline_report.json` and `<stem>_review.md` (a finished run also prints the report on stdout and the gate summary on stderr), and its decisions are in `<stem>.decisions.jsonl` (§4b). Along the way: `<stem>.kicad_sch` / `.kicad_pcb` / `.kicad_pro`, `<stem>_drc.json`, renders `<stem>_place_{top,bottom}.png` (G3) and `<stem>_final_{top,bottom}.png` (G5), the fab zip `<stem>_<fab profile>.zip`, the route-cache manifest `<stem>.cache.json`, `<stem>_route_handoff.json` (exit 3), `<stem>.k9.*` and `<stem>.agent[.<n>].*` (§4a), `<stem>.napkin.*` (§4b) and the git-ignored near-miss copies `<stem>.kicad_pcb.placed` / `.alt<seed>` / `.preheal` / `.attempt`.

What happens, gate by gate:

| Gate | Stage | Auto-remediation on failure |
|---|---|---|
| **G1** Requirements | `lint_netlist` + intent check + the `assumptions` block (§4b) | — (designer fixes the netlist) |
| **G2** Schematic | `build_schematic_from_netlist` → `.kicad_sch` (KiCad 11 API backend when a nightly is installed, else `sch_build` — §8; the report's `schematic.backend` says which), `kicad-cli sch erc` (newest kicad-cli), design-intent review (`design_report`: decoupling coverage, floating pins, pulls, test points), **thermal review** (`thermal_check`: shunt I²R vs 50%-derated rating, FET conduction loss, linear-reg (Vin−Vout)·I, switcher loss estimate; Tj = Tamb + P·θJA ≤ 80% Tj_max) | missing bypass → 100 nF auto-added at the IC, netlist rewritten, stage re-runs; napkin: the floating-pin policy (§4b). Thermal: declared numbers (`p_diss`, `rds_mohm`, value "3W") block; estimates warn. Overrides per part: `"thermal": {"p_diss", "theta_ja", "tj_max", "i_max"}` (`i_max` for topology-capped parts, e.g. a bypassed sleep shunt); board-level: `"thermal": {"t_ambient", "tj_margin", "ep_vias_min"}` |
| **G3** Placement | `swig_build_board` (KiCad bundled python) → outline, mixed/two-sided placement, zones (In1=GND, In2=first power rail), preroutes, silk; courtyard DRC must be **0**; connector mouths must face off-board at the edge; dissipating exposed-pad parts must have ≥4 thermal vias under the EP (IPC-7093 — footprint `...ThermalVias` variants count); **plane integrity**: a power plane whose pad population is split across separate fill islands is a BLOCKER (the DSN export hides voids — the router will return the net broken); corridors under 1.8 mm between pad groups (one router via from sealing) are a warning. GND is exempt (post-route pours + stitching unify it) | courtyard overlap → re-place at next `pack_scale` (1.0→1.5→2.0→2.6→3.2); a retry that no longer fits restores the last good ROUTED snapshot (`.attempt` file); napkin: connector face-out / thermal-footprint policy rounds at the same pack_scale (§4b); **EP via farm**: a dissipating EP part whose footprint has no `...ThermalVias` variant (e.g. TO-263) gets an IPC-7093 via farm auto-dropped under the tab (0.3 mm drill, ~1.2 mm pitch, pad's net) before the review runs |
| **G4** Layout | **Agent routing** (§4a, the default when Claude drives the board): the agent lays every track and via through the KiCad board API, `route_check` = zone refill + kicad-cli DRC, `route_status` = connectivity + routed-width + via-ampacity gate. **Harness** (`run_pipeline.py`, unattended): DSN export → canonical DSN order (`dsn_canon`) → **Freerouting 1.9** → SES import → DRC + routed-width check + via-ampacity check (`swig_gate_checks`) | Agent: the repair loop is the agent's (`remove_routing` + re-route per violation; a stall falls back, §4a). Harness: a near miss (1–4 unrouted) is recovered on the same placement — alternate DSN orders (`dsn_seed` 1, 2), then the verified heal (`swig_heal`: ≤2 mm same-net gaps bridged, plane-net islands linked, at the net-class width; kept only when DRC unconnected is 0 with no new DRC error or routed-check issue, else restored byte for byte); then a 2-round fix→re-DRC→re-gate loop (heal + parallel vias for under-ampacity transitions); real congestion re-places at the next pack_scale; still failing: `--route auto` hands the board off (§4a). EMC stitching is a transaction: a stitch that breaks G4 is undone. |
| **G5** Fab package | `kicad-cli` Gerbers + drill + pos; BOM.csv + CPL.csv (JLCPCB format) zipped; BOM sourcing audit; the D5 safety warning (§4b) | — |

**Report keys** (top level of `<stem>_pipeline_report.json`):

| Report key | What it holds |
|---|---|
| `PASS`, `status`, `error`, `gates`, `gate_summary` | the verdict and each gate's `{gate, passed, blockers, warnings, tasks}`; `status` is `handoff` on exit 3 (never a PASS); `error` = a crash, a usage error or a resume that could not continue |
| `mode`, `hard_stop`, `feature_halt` | napkin or strict and why; a file held by a KiCad editor (§4b); the feature hook that stopped the run |
| `lint`, `schematic`, `design_report`, `thermal`, `remediation` | G1 lint; the G2 build (`backend`, its `fallback`, `libraries`), design and thermal review; every corrective-action round |
| `route_cache`, `route_cache_reason`, `route_cache_error` | hit or miss, why it missed, a manifest that could not be written |
| `board`, `autoroute`, `routing`, `drc`, `emc` | the G3 build; the route (`pack_scale`, `tracks`, `unconnected`, `dsn_sha256`, `dsn_seed`, `alternates`); `{method, requested, why[, handoff]}`; the last DRC; the EMC pass |
| `handoff`, `handoff_resume`, `superseded_handoff`, `resume`, `stall_chain` | the handoff and its two resume command lines; an old handoff this run retired; a resumed run (§4a) |
| `agent_board_preserved`, `agent_board_previous`, `board_file`, `pre_k11_snapshot` | the agent's KiCad 11 board kept as `X.agent.*` at load (and the copy it rotated); the board's path, format and writer (`toolchain.board_file_info`); the `X.k9` snapshot |
| `fab`, `sourcing`, `renders_placement`, `renders_final` | the G5 package, the BOM sourcing audit, the PNGs |
| `decisions_log`, `decision_log_errors`, `review`, `review_error`, `library_sources_error` | the decision log and why an entry could not be written; the review, why it could not be rendered or written, and why its parts table has no library sources — none of them stops a run |
| `pipeline_report`, `pipeline_report_error` | this report's own path, or why it could not be written (also on stderr and in the review) |

**Route cache.** A run that passes G4 writes `<stem>.cache.json` with the route hash (`pipeline/rp_route_hash.py`: each component's position, footprint, side and packer hints, plus the design keys that move copper — `nets`, `net_specs`, `preroutes`, `board`, `planes`, `stackup`, `copper_layers`, `emc`, ...; a feature declares its own). A re-run with the same hash reuses the routed board and skips build and route, but G3 (the `placement_routed` variant: no plane-integrity prediction on a routed board) and G4 **still re-run**: the cache skips work, never judgement. Values, sourcing, thermal annotations, assumptions and the mode do not move copper. A miss says why in `route_cache_reason` and deletes the stale manifest; **a board saved by KiCad 11 is never reused** (the SWIG gate scripts cannot read it). `PIPELINE_NO_CACHE=1` bypasses the cache.

Useful environment variables:

| Var | Effect |
|---|---|
| `PACK_SCALE` | placement looseness multiplier (harness drives this itself) |
| `PACK_DEBUG=1` | print cluster/shelf packing decisions |
| `FREEROUTING_JAR` | pin a specific router jar (keep 1.9.0) |
| `KICAD_PATH` | an extra KiCad root (or bin dir) to probe when auto-detect misses it; every install found is still used per job (§8) |
| `PIPELINE_NO_CACHE=1` | bypass the route cache (always rebuild + re-route) |
| `KICAD_EDA_PIPELINE_FEATURES` | `none` or a comma list of feature names: restrict the pipeline features that run (default: all) |

**Feature hooks.** A feature acts inside the run through one module `pipeline/rp_feature_<name>.py` — `ORDER` (default 100), functions named after the hooks below and its `ROUTE_HASH_*` key tuples — never by editing the stage modules; its state lives in `ctx.ext[<name>]`. A hook raises `rp_hooks.Halt(gate, message)` to fail that gate, and one that cannot judge a resumed KiCad 11 board returns a blocker naming why (never a crash or a skip). With no feature the run and its report are exactly the harness's. Kinds and contracts are in the `rp_hooks` docstring. Rule for features: a board with a D7 stackup or RF nets is never handed off (its G4 checks need a KiCad 9/10 board), so its feature answers the `handoff_veto` hook.

| Stage | Hooks, in call order |
|---|---|
| load, G1 | `on_load`, `g1_prepare`, `lint_kwargs`, `g1_verdict` |
| G2 | `g2_policy`, `analysis_kwargs`, `g2_verdict`, `after_g2` |
| route cache, G3 | `route_hash_extra`, `pack_scales`, `g3_inputs`, `g3_verdict` |
| routing | `route_provider` (a router used instead of Freerouting: its board is never handed off, its failure is a G4 FAIL), `after_route`, `on_exhausted` (re-run once from `after_g2`), `handoff_veto` (a reason turns a would-be handoff into a G4 FAIL) |
| G4, G5, command line | `g4_inputs`, `g4_verdict`, `emc_args`, `g5_verdict`, `board_dims` |

## 4a. G4 — agent routing (KiCad board API)

The default G4 when Claude drives a board (the `agent-routing` skill, the `router` subagent) and the target of the harness's routing handoff: no autorouter — the agent reads the board, lays copper net by net, and KiCad's connectivity plus kicad-cli DRC verify it. Backend: KiCad's IPC board API through the vendored kipy (`server/backends/pcb_session.py` / `pcb_items.py` / `pcb_api.py`); geometry: `server/route_planner.py` (pure Python); sessions and their D5 policies: `server/board_sessions.py`; MCP tools: `server/board_route_tools.py`.

**Session modes.** `mode="auto"` joins a running KiCad 9/10/11 PCB editor that has the file open (API server enabled; edits land in the live document and a save keeps that KiCad's format), else it launches a private headless `kicad-cli api-server` from the KiCad 11 nightly with the board preloaded (`server_status().board_api`, else `board_api_reason`). One headless board at a time: another path drops an idle (saved) board (`released_boards`) and refuses while it has unsaved edits. KiCad's `GetConnectedItems` (≥ 10.0.1) supplies real connectivity including filled zones; a KiCad 9 GUI session reports `connectivity_available: false`, where `unrouted_connections` cannot count clusters.

**Tools** (all but `list_open_boards` take `pcb` first; each answers `{ok, backend: "api", mode, ...}` or `{ok: false, reason}`; signatures and the routing rules: [skills/agent-routing/SKILL.md](../skills/agent-routing/SKILL.md)): `open_board`; `board_overview` (layers, which inner layer carries which plane, rules, net classes, counts); `board_pads`; `unrouted_connections` (N clusters of a net → N−1 closest-pair links, with the foreign copper near each line); `route_net` (tracks and vias, validated against the board rules and the net class first); `add_vias`; `remove_routing`; `route_check` (zone refill, save, kicad-cli DRC at error severity with the writer's kicad-cli, the stall verdict); `route_status` (the routed gate: connectivity, class widths, via ampacity; `check_design_gates(stage="routed")` answers with it on a board SWIG cannot read); `save_board`; `close_board`; `list_open_boards`. **Loop:** `board_overview` → `unrouted_connections` → route 3–6 nets (decoupling and clocks first, then signals, then power; plane nets by a stub + via) → `route_check` → fix each violation (`remove_routing` + re-route) → repeat until `ok` with nothing unconnected → `route_status(pcb, netlist).passed` → `export_fab_package`. With the defaults `route_check` refills the zones, which counts as a change, so even an untouched board is saved (headless: as a KiCad 11 file); only `refill=False` leaves an unmodified board as it is.

**File format and the automatic snapshot.** A board saved through a KiCad ≥ 10.99 session (the headless server or a KiCad 11 GUI) is a KiCad 11 file (`(version 20260901)`, `(generator_version "10.99")`): KiCad 9/10, their kicad-cli and their pcbnew cannot load it, so the SWIG tools (`autoroute_board`, `check_design_gates(stage="placement")`, `add_ground_stitching`) refuse it (`toolchain.swig_readable` names `route_check` / `route_status` instead) and DRC, Gerbers and renders use the kicad-cli of the KiCad that wrote it (`toolchain.board_cli`, §8). Right **before the first such save** (and at `open_board` for a KiCad 11 GUI session) the pre-11 set is copied to `X.k9.kicad_pcb` + `X.k9.kicad_pro` (+ `.kicad_prl` / `.kicad_dru` when present; never the `~X.kicad_pro.lck` lock), and again whenever that snapshot is older than the file; a failed copy refuses the save. Board and project travel together: restored without its project, a board loses its net classes and Freerouting lays power nets at the default width while DRC still passes. Never copy board files by hand; the board tools report `pre_k11_snapshot`. A KiCad 9/10 GUI session keeps the editor's format.

**Stall rule.** `route_check` scores each judged round as DRC errors + unconnected. The first round is the baseline; a round improves when it beats every earlier score; **3 rounds in a row without a new best** on a board that is not clean is `stalled` (with `stall_note`), on the fourth call at the earliest, and the agent stops. A round that judged a stale disk copy (`save=False` with unsaved copper) or whose DRC failed (the old report is deleted first, never re-read) is not counted (`progress_note`), but the verdict so far is still returned. The history is per board path, survives a session release and reopen, and restarts when Freerouting or a rebuild rewrites the board (`release_for_rewrite`).

**Handoff** (`--route auto` after a failed G4, `--route agent` once G3 passed; exit 3). `<stem>_route_handoff.json` (`kind` "kicad-eda route handoff", `version` 1) holds what the agent and a resumed run need: `netlist`, `pcb`, `project`, `requested` (the route mode), `why`, `pack_scale`, `route_hash` (of the design), `placed_sha256`, `board_file`, `freerouting` (every attempt of the ladder and the handed one; null in agent mode), `unconnected_items` (the first 40 of `<stem>_drc.json`), `net_classes` (`{net: [class, track width]}` from the `.kicad_pro`), `g3`, `board` (the build summary), `instructions` and `resume` (the two command lines below). The agent routes the board with the `router` subagent through the board API, never runs `autoroute_board` or a rebuild itself, closes the board (`close_board`), then runs one of:

- `--resume routed` (once `route_status` passed): G1 and G2 re-run (a G2 remediation is an error: the routed board would be stale); G3 carries the handoff's verdict with a warning plus a courtyard DRC of the board; G4 judges the agent's board (a KiCad 11 board through the board API's routed gate instead of the SWIG check, with the SWIG fix loop and EMC stitching skipped, a warning each); G5 as usual. The handoff stays, so the agent may fix the board and resume again.
- `--resume stalled` (after a stall): Freerouting on the snapshot (`autoroute_board(pcb, from_snapshot=True)` moves the agent's KiCad 11 set to `X.agent.*`, an older one rotating to `X.agent.<n>.*` with the first free n, copies `X.k9.*` back and routes it, near-miss recovery included), then the pack_scale ladder from the handoff's pack_scale + 1, then stop. Each step is an entry of the report's `stall_chain` with a routing decision; the consumed handoff is retired.

Both refuse (exit 1, the report's `error`) a missing or malformed handoff, a board other than the one it names, and a netlist whose route hash changed since. Every fresh run that reaches routing retires an old handoff (report `superseded_handoff`). At load, a board SWIG cannot read (the agent's KiCad 11 board, which always misses the route cache) is kept as `X.agent.*` before G2 rewrites the project (report `agent_board_preserved`, and `agent_board_previous` for a rotated copy); a copy that cannot be made stops the run before anything is built.

## 4b. Napkin mode (hands-off)

Napkin mode builds a board from a concept without asking anyone: every open question is decided by a policy, and every decision is recorded for a human to review afterwards (ROADMAP D5). `/kicad-eda:napkin` runs the whole flow (requirements and their assumptions, parts, the netlist, this harness and its routing handoff, the review) through the [napkin-mode skill](../skills/napkin-mode/SKILL.md), which also holds the policy table for every gate task; the GUI flow (`/kicad-eda:new-board`) stays interactive.

**Switch.** The harness runs napkin by default; `--strict` turns the policies off (every policy task stays a designer task, and a file open in a KiCad editor halts the run); a netlist `"mode": "napkin"` always means napkin. The report's `mode` says which and why, and a napkin run records it as a G1 decision.

**Assumptions.** Requirement-level guesses live in the netlist (§2): `"assumptions": [{"id", "topic", "value", "rationale", "confidence": "high|med|low", "override"}]`, where `override` names the netlist path a human edits to overrule one. G1 blocks a malformed block (blank text, a duplicate id) in every mode and warns when a napkin netlist has none; the review lists them first, low confidence first.

**Decision log.** `<stem>.decisions.jsonl` beside the netlist, append-only, one JSON object per line: `{id, ts, stage, question, decision, rationale, alternatives, confidence, source, reversible, override}` with `id` "dec-" + 12 hex digits, `ts` ISO 8601 UTC, `stage` one of requirements | parts | G1..G5 | routing, and `override` the netlist path that changes it. The harness records the mode, every applied remedy (with its own confidence and alternatives), a hard stop, the schematic fallback, the routing method and the G5 sourcing and safety calls; agents record theirs with the `record_decision` MCP tool. Each append is a single write of one line of pure ASCII (non-ASCII text is JSON-escaped, so a U+2028 pasted from a datasheet cannot split a line), a torn last line is closed before the next entry, and writers are serialised by an exclusive `<log>.lock` (one older than 60 s belongs to a dead writer and is broken). A decision that cannot be written never stops a run: report `decision_log_errors`.

**Review.** `<stem>_review.md` is written on every exit (the `review_report` MCP tool re-renders it from the files), with the sections of `review_report.SECTIONS`: **Summary**, **Assumptions**, **Decisions** (grouped by stage, low confidence first), **Parts** (library source stock | project | generated | unknown), **Gate verdicts**, **Auto-remediations**, **Accepted risks and warnings**, **Routing** (method and why), **Board file** (format and the `X.k9` snapshot), **Renders**, **Fab package** and **Open items**. A review that cannot be rendered or written is report `review_error`; the JSON report is still written.

**Safety.** A design that runs on mains or > 50 V, or charges Li-ion cells, is still built hands-off but flagged **HUMAN REVIEW REQUIRED** at the top of the review, with a G5 warning per kind and a G5 decision. The heuristics (net voltages and names, AC-DC parts, mains-rated fuses and varistors, charger ICs, battery and charge nets, the assumptions) are documented rule by rule in the docstring of `safety_screen.detect_safety`, the source of truth.

**Policies** (`server/tools/napkin_policy.py`; `gates.py` turns what they allow into auto tasks, and each applied remedy is a decision):

- G2 floating pins: `declare_no_connect` only for output, bidirectional, passive or NC-named pins on ICs and modules; `net_same_name_pin` joins a stacked supply-named pin to the netted pin of the same name on that part; `input` / `power_in` pins, other supply-named pins and a pin named like another netted pin of the part (an unused op-amp unit's input) stay designer tasks, which the agent nets with a recorded rationale.
- G3, up to 2 policy rounds at the same pack_scale (netlist rewritten, board rebuilt and re-judged): `face_connector_out` turns a connector that faces the interior or sits off its edge outward (geometry from `swig_connector_probe.py`), `swap_thermal_footprint` gives an exposed-pad part short of thermal vias its `...ThermalVias` footprint when one resolves (the D1 library order), and a fractured plane becomes `loosen_placement` (the next pack_scale). A policy task that cannot be applied stays open and stops the ladder.

**Hard stops** in napkin mode: installing software, spending money or ordering (the agent stops and says so), and overwriting a file open in a KiCad editor: the harness then writes `<stem>.napkin.kicad_sch` / `<stem>.napkin.kicad_pcb` instead (their project and cache manifest follow the board; the report, review and log stay at the netlist stem) and records a G1 decision (report `hard_stop`); `--strict`, or a sibling that is open too, halts at G1.

**G2 schematic fallback.** When the KiCad 11 nightly has no global library tables (one never opened), the `auto` schematic backend builds G2 with the file backend and records that as a G2 decision (report `schematic`: `fallback` = {from, to, reason, decision}); the user's KiCad configuration is never edited.

## 5. Regression suite

```text
<venv>\python pipeline\run_all.py [--list] [boards_dir]
<venv>\python pipeline\determinism_check.py <netlist.json> --runs N [--jobs J] [--keep DIR]
              [--verdict-only] [--timeout S] [-- <run_pipeline args>]
```

**run_all.py** runs, in order: the static boards of `examples/` (cp2102n_uart, stm32f103_dev, lm358_buffer, usb_afe, lm324_quad, buck_5v3a, rf_coupon_50r, rf_lna_gainblock, esp32c3_env_node, and negative_gate_test: the buck rebuilt each run with its input decoupling stripped, which must FAIL G2, auto-remediate and pass); every `examples/<name>/run_all.json` marker, by name (how a new board joins without editing run_all.py); and three fixtures derived each run: negative_g3 (strict G3 must DETECT an inward connector and a plane pinch), napkin_g3 (the same netlist in napkin mode must turn the connector out, with a G3 `face_connector_out` decision) and negative_g4_heal (a track cut on this run's routed lm358 board must be healed). Every board routes in the freerouting route mode unless its marker's `"args"` say otherwise (CI never hands routing to an agent), and a fixture without its source or KiCad's Python is a SKIP. A FAIL row prints the run's gate summary under it, the last line is a JSON summary with each report path, and `decisions=<n>` counts the decisions each run appended. Exit 0 = ALL PASS (no FAIL or MISSING row, at least one row ran), 1 = a row failed, 2 = usage. Green = safe to ship a tool change.

**Marker** `examples/<name>/run_all.json` (beside its `netlist.json`; every key optional): `"w"` + `"h"` (mm, both or neither: neither = the harness's own size rule), `"expect": "PASS" | "FAIL"` (default PASS), `"gate"` + `"blocker_contains"` (expect FAIL only, and required there: the gate that must fail and a text one of its blockers holds), `"args"` (extra harness arguments such as `["--strict", "--route", "agent", "place.json"]`; a placement file is relative to the marker; no `--resume`: every board starts fresh), `"post_checks": [{"script": "pipeline/<x>.py", "python": "venv" | "kicad", "args": ["{pcb}", "{netlist}", "{dir}", "{report}"]}]` (run once the board met its expectation, stdin closed; a non-zero exit fails the row; a `kicad` script imports `kicad_quiet` before `pcbnew`). A marker that does not validate is a FAIL row naming every problem; `run_all.py --list` prints the matrix without running it.

**determinism_check.py** runs the harness N times (J at a time) on fresh copies of one netlist's inputs (`PIPELINE_NO_CACHE=1`, stdin closed) and compares the exit code, PASS, gate summary, remediation, `pack_scale`, `dsn_sha256`, `dsn_seed`, `alternates`, the DSN and SES file hashes and the board fingerprint (`server/tools/board_fingerprint.py`: copper and placement normalised, UUIDs and item order ignored); `--verdict-only` compares the exit code, gate summary and remediation only, and `--keep DIR` keeps the run folders. It prints a table, the differing fields with a board diff, and one JSON line; exit 0 = every run agrees, 1 = a field differs or a run left no readable report or board, 2 = usage.

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

1. **Freerouting is deterministic only on a canonical DSN.** KiCad writes footprints, vias and zones in random-UUID order and Freerouting 1.9 is order-sensitive: before canonicalisation 5 of 20 runs of the same buck board failed G4. `board_autoroute` rewrites every DSN in `dsn_canon`'s canonical order (`dsn_seed` 0) through `dsn_sexpr` (never regexes on DSN text: a quoted `Net-(D1-A)` breaks them), so one placement routes the same way on every run and machine. Re-running an identical board never clears a stuck item: a near miss tries alternate orders (`dsn_seed` 1, 2) and the verified heal, real congestion needs a new placement (`pack_scale`); otherwise fix the actual cause.
2. **Freerouting 1.9 cannot neck down.** Wide width-class into a small pad = unroutable. Preroute fat copper; exempt those nets from width classes (the harness does this automatically for preroute/plane nets).
3. **DSN plane abstraction lies.** KiCad exports zones as full-rect polygons; Freerouting cannot see fill voids, so plane-connected nets can come back with orphan islands. G3 statically catches planes that are ALREADY fractured (blocker) or pinched below 1.8 mm (warning), but the worst case (HAT VBUS_PD) was created by the ROUTER's own via antipads sealing a healthy ≥3 mm corridor, which no pre-route check can see: that class belongs to the G4 healer (bridge / island-link / maze route), which the `negative_g4_heal` fixture proves on every regression run. For chronic nets, author a corridor preroute.
4. **Freerouting phones home and opens windows.** Every version contacts the network (1.9: Segment analytics plus a GitHub version check no flag disables), and a stale `freerouting-<crc>.frb` in a shared temp dir pops a modal dialog that hangs a headless run. So every Freerouting child starts in a fresh temp dir (cwd, `java.io.tmpdir`, `user.home`) with JVM proxy settings on a dead local port (127.0.0.1:9: no outbound connection, the same SES), `-da` (no analytics) and `-dct 0` (no countdown dialog), under `xvfb-run` on a display-less Linux and on a private desktop on Windows (1.9 always opens Swing windows; `-Djava.awt.headless=true` makes it fail). Keep 2.x jars out of `server/tools`: the jar picker takes the highest-sorting `freerouting*.jar`, and a renamed 2.x jar runs as 1.x in GUI mode; pin `FREEROUTING_JAR` instead.
5. **KiCad 10 SWIG padstack traps:** `PCB_VIA.GetWidth()` without a layer arg asserts and **wedges the process** on Windows — pass `pcbnew.F_Cu`. Headless `ZONE_FILLER` segfaults on an in-memory `CreateEmptyBoard` board — save, `LoadBoard` fresh, then fill. `fp.Flip()` before `board.Add()` segfaults (flip after adding).
6. **`.kicad_pro` overrides board rules in `kicad-cli` DRC** — the project file must carry the fab profile limits or a clean board still fails.
7. **kicad-cli `-o "dir\"`** (quoted trailing backslash) mangles the path.
8. **Via ampacity (IPC-2221):** one 0.3 mm-drill via ≈ 0.9 A, 0.9 mm ≈ 2 A at ΔT 10 °C. High-current layer transitions need parallel barrels — THT component legs on the net count too.
9. **Two-sided placement:** PTH obstacles (THT pads, thermal-via farms) exist on BOTH faces — carve `auto_b` away from them or G3 reports "PTH inside courtyard".
10. **No headless teardrops.** KiCad 10's teardrop generator is not exposed to SWIG at all — `"emc": {"teardrops": true}` can only remind you to generate them in the GUI (Edit → Edit Teardrops) before fab.
11. **HAT/fixed-board pattern:** fixed connector coordinates + `auto_x/y/w/h` free field + hand-placed power chain + preroutes is the proven recipe for a dense fixed-outline board.
12. **A schematic written by KiCad 11 is a KiCad 11 file.** KiCad 9/10 and their `kicad-cli` cannot open it (§8). And a KiCad 11 that has never been opened has no library tables: the headless server then reports `symbol Device:R not found` (§8, first-run gotcha; the pipeline falls back to the file backend, §4b).
13. **A board saved through a KiCad 11 session is a KiCad 11 file** (§4a): Freerouting, `check_design_gates(stage="placement")`, `add_ground_stitching` and KiCad 9/10 cannot read it; DRC and Gerbers still work (kicad-cli 11 via `board_cli`). The `X.k9.*` snapshot taken before the first such save keeps the Freerouting fallback open (`autoroute_board(pcb, from_snapshot=True)`); never copy board files by hand.
14. **kicad-cli 10.99 `sch erc` rewrites `<stem>.kicad_prl` into KiCad 11 form** and leaves the board and `.kicad_pro` alone, so a KiCad 11 `.kicad_prl` can sit beside a pre-11 board that no KiCad 11 session ever saved. A board's format is judged by its `.kicad_pcb` header only, and the `.kicad_prl` is optional in the snapshot.
15. **The G4 healer works at the net-class width and lays nothing otherwise.** `swig_heal` reads each net's class width from `<pcb>.kicad_pro` (`netclass_read`: pcbnew's `GetNetClass()` is unusable on KiCad 10 SWIG) and bridges or links a classed net at max(attach width, class width); with no clear path at that width it lays no copper and leaves the open entry `<net>: no clear path at the <w> mm class width`, so the heal is rolled back and pack_scale escalates. Islands and anchors are ordered by geometry, never by board or DRC listing order, so a replay heals the same way.
16. **No modal dialogs from KiCad's Python.** A wx assert or wxLogError in a KiCad-python child opens a dialog nobody can close and the run hangs: every SWIG script imports `kicad_quiet` before `pcbnew` (`tests/test_kicad_quiet.py` walks `server/tools`), and every KiCad, kicad-cli or Java child gets stdin closed, UTF-8 decoding with replacement and a timeout. kicad-cli output is scanned for `Error:` lines: rc 0 alone is not success.

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
server/kicad_server.py        MCP server, schematic session registry, TOOL_MODULES (run_pipeline imports it)
server/tool_registry.py       registers TOOL_MODULES: a tool is a plain function appended to its module's
                              TOOLS tuple (kicad_server, board_route_tools, feature modules such as
                              review_tools and physics_tools); only a NEW module joins TOOL_MODULES
server/kicad_env.py           discovers every KiCad install; pick_cli / pick_swig / pick_api
server/toolchain.py           per-job picks (kicad-cli by job and by board-file writer, pcbnew Python,
                              libraries, Java, jar); board_file_info, swig_readable; the X.k9 snapshot,
                              the X.agent.* copies, route_progress (the stall rule)
server/kicad_ipc.py           GUI connection + open-document guard;  pcb_ipc_tools.py: live PCB tools
server/backends/              KiCad 11 API backends: schematic (sch_api, sch_session, sch_netlist_build,
                              sch_geometry, sch_items, sch_erc), board (pcb_session, pcb_items, pcb_api)
server/route_planner.py       pure routing geometry: connections, obstacles, validate_path, via_ampacity
server/board_route_tools.py   agent-routing MCP tools (§4a); board_sessions.py: sessions, snapshot, stalls
server/board_pipeline_tools.py  G3-G5 MCP tools; board_autoroute.py: DSN, canonical order, Freerouting
server/review_tools.py        record_decision + review_report MCP tools (§4b)
server/sch_file_model.py      file-model fallback for the interactive schematic tools
server/vendor/kipy/           vendored kicad-python (see server/vendor/README.md)
server/tools/                 sch_build, swig_build_board, gates, remediate, swig_gate_checks,
                              swig_board_fixes, silk_place, netclass_writer, ipc_rules, lint_netlist,
                              design_report, emc_stitch, update_kipy, freerouting-1.9.0.jar
server/tools/kicad_quiet.py   imported before pcbnew by every SWIG script (no modal dialogs)
server/tools/dsn_sexpr.py     the one DSN/SES parser + printer; dsn_canon.py: the canonical DSN order
server/tools/swig_heal.py     G4 healer at the class width; swig_copper.py: copper geometry;
                              netclass_read.py: net-class widths from the .kicad_pro
server/tools/decisions.py     decision log + assumptions; review_report.py, review_sources.py: the
                              review; safety_screen.py: the safety screen; napkin_policy.py: policies
server/tools/board_fingerprint.py  board hash (determinism); swig_connector_probe.py: connector geometry
server/pins/                  pin sidecars (file-backend wiring coordinates)
pipeline/run_pipeline.py      the gated harness: STAGES, run(), main() (module map in its docstring)
pipeline/rp_cli.py            command line + exit codes; rp_context.py: RunContext, report, review and
                              run decisions; rp_hooks.py: feature hooks; rp_route_hash.py: cache key
pipeline/rp_stage_schematic.py  load, G1, G2; rp_stage_place.py: route cache, G3, ladder, stall chain;
                              rp_stage_layout.py: G4; rp_stage_fab.py: G5
pipeline/rp_route.py          Freerouting + near-miss recovery; rp_route_modes.py: modes, handoff, resume
pipeline/rp_napkin.py         napkin mode; rp_kicad.py: KiCad subprocesses; rp_paths.py: sys.path setup
pipeline/rp_feature_<name>.py one pipeline feature (its hooks, §4)
pipeline/run_all.py           regression suite + fixtures (§5); determinism_check.py: N-run comparison
skills/agent-routing/         the routing method (§4a); agents/router.md: the routing subagent
skills/napkin-mode/           the hands-off flow and its policy table (§4b); commands/napkin.md
tests/                        pytest tier (pure + live API tests, skipped without KiCad 11)
docs/PIPELINE.md              this file
```
