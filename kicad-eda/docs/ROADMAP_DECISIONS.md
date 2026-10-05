# Roadmap decisions (F2-F6)

The binding contract for the roadmap work on branch `feature/roadmap-f2-f6`.
Every implementer follows it; a change to a decision lands here first.

Goals: (1) completely hands-off napkin -> schematic -> Gerbers;
(2) rebuild open-source boards from their schematics/layouts, optionally in
a new form factor. Platform: KiCad (Fusion 360 is mechanical only — its
Electronics API is read-only).

Background maps (read-only research, 2026-10-04) live outside the repo; the
facts they verified that matter are restated here.

## D0. Toolchain facts every task relies on

- SWIG board work runs on the oldest supported pcbnew: **KiCad 9.0** locally,
  KiCad 10.0 in Docker/CI. Files the SWIG path must load (authored or
  imported footprints, boards) must be in a dialect that pcbnew can read:
  footprint `(version 20241229)`, symbol `(version 20241209)`. KiCad 9
  pcbnew returns `None` for KiCad 10+ footprints (`20260206`, `20260901`).
- KiCad 9 bundled Python is stdlib + numpy + wx + PIL only (no sexpdata,
  ezdxf, shapely). Code imported by SWIG scripts must be stdlib-only.
- **No modal dialogs, ever.** KiCad-python children can pop wx assert or
  wxLogError dialogs that block forever. Every KiCad-python entry point
  imports `server/tools/kicad_quiet.py` before `pcbnew`, which does
  `import wx; wx.DisableAsserts(); wx.Log.SetActiveTarget(wx.LogStderr())`
  (no-op if wx is missing). All KiCad children also get `stdin=DEVNULL`.
- Never call `SHAPE_POLY_SET.SquaredDistance(p, True)` (asserts on 9.0;
  returns a bogus 0.0 when asserts are suppressed). Outline math is ours.
- KiCad 11 nightly CLI has `import`, `pcb import`, `sch import`,
  `pcb/sch upgrade`, `gerber info/diff`, `*/diff`; KiCad 10.0 has `pcb import`
  only; KiCad 9 has none. `fp upgrade` / `sym upgrade` convert Eagle `.lbr`,
  Altium `.PcbLib`, legacy `.lib` on 9 and 11.
- The KiCad 11 headless api-server honours **project** `sym-lib-table` /
  `fp-lib-table` files (with `${KIPRJMOD}`) present when a document is
  created/opened. The library-table RPCs have no handler on 10.99.0-4025:
  **write table files, never call AddLibraryTableEntry**. A headless board
  session needs `load_all_libraries([LT_FOOTPRINT])` before
  `place_footprint_from_library`.
- `kicad-cli sch erc` honours project tables only when `<stem>.kicad_pro`
  exists beside the schematic; the file backend must write one.
- An imported/foreign tool's `rc=0` and JSON report are not proof of
  success: also scan stdout/stderr for `Error:` lines and run ERC/parity.
- One document per headless api-server (`session_release.py`). Flows
  sequence their API use; library validation uses kicad-cli, not the API.

## D1. Project-local libraries (F2)

- Netlist block (paths relative to the netlist.json directory; `${KIPRJMOD}`
  = that directory; absolute paths allowed):
  ```json
  "libraries": {
    "symbols":    {"SPS_parts": "libs/SPS_parts.kicad_sym"},
    "footprints": {"SPS_parts": "libs/SPS_parts.pretty"}
  }
  ```
- Resolution order per nickname: netlist `libraries` block, then the project
  `sym-lib-table` / `fp-lib-table` beside the netlist, then the stock library
  of the install the consumer uses. The same nickname in the block and a
  project table with a different URI is a lint **error**.
- The pipeline materialises the block: it merges rows into project
  `sym-lib-table` / `fp-lib-table` next to the generated outputs, tagging its
  rows `(descr "kicad-eda managed")`, never touching other rows; and the file
  backend writes a minimal `<stem>.kicad_pro` when none exists.
- Default home for authored parts: `<netlist dir>/libs/<name>_parts.kicad_sym`
  and `<name>_parts.pretty` (nickname `<name>_parts`). Shared libraries
  outside the project (e.g. `../libs`) are allowed by relative path.
- Authored files are written in the D0 dialect and validated by loading them
  (kicad-cli `fp upgrade`/`sym upgrade` to a temp dir as a parse check, and
  pcbnew `FootprintLoad` in the live tier).
- The SWIG builder stamps the library nickname on every footprint it loads
  (`SetFPID(LIB_ID(nick, name))`), stock included.
- Pin sidecars for project parts go to `<project>/pins/` (already searched by
  `sch_file_model`); the plugin's central `server/pins/` is for stock parts.
- Large modules (LG290P: 79 pads, 55 GND): one symbol pin per pad, pad
  number = pin number. GND pins beyond the first are stacked on the first
  GND pin's position (KiCad stacked-pin convention), so the symbol stays
  readable. The authoring tool also returns `pin_groups` (e.g.
  `{"GND": ["10","12",...]}`) so a netlist can be written without typing 55
  pads by hand.
- First authoring target: **Quectel LG290P(03), LGA-79** (Hardware Design
  V1.0, Fig. 18): 24 edge pads 1.5 x 0.8 mm at 1.1 mm pitch, x = +/-5.05;
  55 inner pads 1.0 x 1.0 mm on a 5 x 11 grid, x pitch 1.4 (x = -2.8..2.8),
  y = -7.0 + 1.4k; pad 25 is a 1.0 mm round; numbering
  pad = 25 + 11*col + row; body 12.2 x 16.0 mm. (The SparkFun footprint's
  inner grid is wrong and numbers all inner pads "25" — do not copy it.)

## D2. Board outlines and mechanical data (F3)

- Board frame stays as today: mm, Y down, origin = outline bbox top-left.
- `board` block grows (existing `w`/`h`/`auto_*` stay valid):
  ```json
  "board": {
    "w": 60, "h": 40, "corner_radius": 3.0,
    "outline": {"loop": [[x, y, bulge], ...]}        // or
    "outline": {"dxf": "mech/outline.dxf", "layer": "OUTLINE", "units": "mm"},
    "cutouts": [{"loop": [[x, y, bulge], ...]}, {"circle": [cx, cy, r]}],
    "datum": [x, y]
  }
  ```
  A loop vertex is `[x, y]` or `[x, y, bulge]` (DXF bulge = tan(theta/4) of
  the arc from this vertex to the next). `w`/`h` alone = rectangle.
- External outlines (DXF from Fusion, Y up) are normalised to the board
  frame (flip Y, translate bbox to 0,0). The source origin is kept as
  `board.datum` (board frame) and set as the KiCad aux/drill origin, so
  `kicad-cli pcb export step --drill-origin` lands in the Fusion frame.
- Geometry lives in `server/tools/outline.py`, stdlib-only so both the venv
  and SWIG scripts import it: loops with arcs, flattening (0.05 mm chord),
  contains / inside-with-clearance, distance to edge, bbox, inset, largest
  inscribed axis-aligned rectangle, fab-rule validation. Exact arcs are
  emitted to KiCad; flattened polylines are only for math.
- DXF is parsed in the venv with **ezdxf** (added to requirements) and handed
  to SWIG as normalised JSON loops. DXF out: kicad-cli `pcb export dxf`.
- Auto placement on non-rectangular boards: rectangular auto fields
  (explicit `auto_*`, else the largest inscribed rectangle) plus containment
  validation (courtyards inside the outline inset by edge clearance, outside
  cutouts). True polygon packing is out of scope.
- Fusion round trip: in = outline DXF + fixed parts/holes with `at`
  coordinates (D3); out = STEP (`--drill-origin`) + outline DXF.

## D3. Placement anchors (F3/F4)

- Existing `x`/`y`/`rot` keep their meaning (courtyard-bbox centre).
- New per-component `"at": [x, y, rot]`: the footprint origin in the board
  frame and its final rotation as KiCad displays it (for `side: "B"`, the
  post-flip rotation). Used by Fusion coordinates and F4 keep-mode rebuilds.
  A component has `at` or `x`/`y`, never both.

## D4. Importing existing designs (F4/F6)

- Converter output carries `"source": {"kind": "kicad"|"eagle"|"altium"|
  "easyeda"|"vision", "path", "sha256", "tool": "kicad-cli 10.99..."}` and
  `"rebuild": "keep"|"reflow"` (keep = original placement via `at`, reflow =
  re-place, e.g. new form factor).
- Source of truth on disagreement: the board for pad nets, footprints and
  placement; the schematic for symbols, values and fields; every mismatch
  goes in the import report.
- `"copper_layers": 2|4|6` top-level (default 4, today's behaviour).
- Board-only parts (holes, logos, fiducials, board-only pads):
  per-component `"board_only": true` (on the board, not in the schematic).
- Unflagged unconnected pins become `no_connect` plus an assumption entry.
- Net names: strip the leading `/`, hierarchical `/CH1/OUT` -> `CH1_OUT`;
  KiCad auto names are kept.
- Equivalence is a partition comparison of `(ref, pad)` sets — never net
  names. Strict ref+pad identity by default; optional `ref_map` for renamed
  references. Implemented in `server/tools/net_equivalence.py`.
- Faithful rebuilds (`source` set): G2 `add_decoupling` remediation off,
  J*-at-edge downgraded to a warning, ERC judged against the source's
  baseline (no new violations), mounting holes not required.
- Footprints for a rebuild: if the source board is in the D0 dialect use its
  embedded footprints (extracted to `libs/<name>_imported.pretty`); else
  prefer the stock footprint of the same name when its pads match exactly;
  else extract and record that the board must go through the KiCad 11 path.
- Foreign formats go through KiCad's own importers first (KiCad 11
  `kicad-cli import` / `pcb import` / `sch import`; KiCad 9 SWIG
  `LoadBoard(path, ALTIUM_DESIGNER|EASYEDA)` fallback — never Eagle via SWIG,
  it aborts), then through the KiCad converter. Multi-sheet Altium: one
  SchDoc per `sch import`. EasyEDA Pro boards and `.PrjPcb` are GUI-only:
  napkin mode records that as a human step.
- PDF/image schematics: rasterize, two independent vision extractions,
  compare them with the equivalence checker, then lint + ERC; disagreements
  are listed for review. Gerber-only reconstruction: feasibility spike only
  in this round.

## D5. Hands-off napkin mode (F5)

- Switch: netlist top-level `"mode": "napkin"` and a `/kicad-eda:napkin`
  command; `/kicad-eda:pipeline` defaults to napkin behaviour. The GUI flow
  (`/kicad-eda:new-board`) stays interactive.
- Requirements-level assumptions: netlist top-level
  `"assumptions": [{"id","topic","value","rationale","confidence":"high|med|low","override"}]`.
- Runtime decisions: append-only `<stem>.decisions.jsonl`, entry
  `{"id","ts","stage":"requirements|parts|G1..G5|routing","question",
  "decision","rationale","alternatives","confidence","source","reversible",
  "override"}`; written by the harness and by agents via a `record_decision`
  MCP tool.
- Every harness exit (pass or fail) writes `<stem>_pipeline_report.json` and
  `<stem>_review.md` (assumptions, decisions grouped by stage with low
  confidence first, parts with library source stock|project|generated, gate
  verdicts, remediations, accepted risks, routing method and why, board file
  format + snapshot path, renders, fab zip, open items).
- Still a hard stop in napkin mode: installing software, spending money or
  ordering, overwriting a file open in a KiCad editor (write a sibling and
  record it instead). Safety-relevant designs (mains or > 50 V, Li-ion
  charging) are built but flagged **HUMAN REVIEW REQUIRED** at the top of the
  review report and as a G5 warning.
- Floating pins: auto `no_connect` only for output / bidirectional / passive /
  NC-named pins on ICs and modules; `input` / `power_in` stay blockers the
  agent must net, with a recorded rationale.
- Missing nightly library tables: fall back to `backend="file"` for G2 and
  record it; never edit the user's KiCad config.
- Routing in the harness: `--route auto|freerouting|agent`, default `auto` =
  Freerouting first (deterministic, no tokens); on failure hand off to agent
  routing (the router subagent) with a handoff report.
- Pre-KiCad-11 snapshot: sibling `X.k9.kicad_pcb` + `X.k9.kicad_pro`
  (+ `.kicad_prl`/`.kicad_dru` when present), taken whenever a save through a
  KiCad >= 10.99 session would overwrite a pre-11 file and the snapshot is
  older than that file; also taken at `open_board` for KiCad 11 GUI sessions.
- Stall = 3 non-improving `route_check` rounds; fallback order: Freerouting
  from the snapshot (canonical `X.kicad_pcb` restored; the agent's KiCad 11
  copy moved to `X.agent.kicad_pcb`), then a higher `pack_scale`, then stop
  with a report.

## D7. Stackups, controlled impedance and RF (direction set 2026-10-04)

- **No fixed set of stackups.** A design declares an arbitrary stackup:
  any copper layer count (1-32, even counts for multilayer), per-layer copper
  weight, and per-dielectric material, thickness, Dk and Df (at a stated
  frequency), including Rogers / Taconic / Isola RF laminates and hybrid
  stackups (e.g. RO4350B outer core on FR-4). Fab presets (JLCPCB standard
  stackups etc.) are conveniences that expand into the same model, never a
  limit.
- **Layer count is a design decision**, made per board with a recorded
  rationale (density, routability, plane/return-path needs, RF/EMC, cost).
  4 layers is the usual default (easier to route than 2), not a rule; 2
  layers when cheap and simple fits, 6+ when density or RF demands it.
- **Controlled impedance is computed from the declared stackup** for
  microstrip, embedded microstrip, symmetric/asymmetric stripline, grounded
  coplanar waveguide (CPWG) and coplanar, single-ended and differential, with
  the reference planes taken from the stackup — not from one hard-coded
  4-layer default.
- **RF is first class in the pipeline:** transmission-line type per net
  (microstrip / stripline / CPWG), reference-plane continuity, via fences
  and transitions, keepouts on every layer where required, and **on-board
  antennas** (PCB trace antennas generated as footprints, chip-antenna
  reference layouts, feed line + matching network placeholders, antenna
  clearance and ground-plane rules).
- The detailed model and task split come from the D7 design pass; this
  section is extended when it lands.

## D6. Process rules for implementers

- Own only the files your task lists; read anything.
- Write tests first; run only your targeted tests (`.venv\Scripts\python.exe
  -m pytest tests/<file> -q`), never the whole suite (other agents are editing
  in parallel and the live tier starts KiCad servers).
- Do not commit; do not push; do not edit `docs/ROADMAP_DECISIONS.md`
  (report a needed change instead).
- Match the surrounding code's style; type-annotate new functions; files stay
  under 800 lines.
- Scratch files go in the session scratchpad, never in the repo.
