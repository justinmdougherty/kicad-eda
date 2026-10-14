# Board outlines and mechanical data (the Fusion 360 round trip)

A board whose shape comes from a mechanical design: any outline with arcs and
cutouts, a DXF exported from a Fusion 360 sketch, parts fixed at the
coordinates the mechanical designer measured, and the STEP / outline DXF /
datum CPL that go back to Fusion. The binding decisions are D2 (outlines) and
D3 (`"at"` anchors) in [ROADMAP_DECISIONS.md](ROADMAP_DECISIONS.md); the
annotated netlist example is [PIPELINE.md](PIPELINE.md) section 2; the
agent-facing summary is
[skills/netlist-pipeline/outline.md](../skills/netlist-pipeline/outline.md).
Every claim below is read from the modules it names (the outline engine is
`server/tools/outline.py` and its parts; the tools are in
`server/mechanical_tools.py`).

## 1. Frames and the bulge rule

| Frame | Units | Y | Origin | Who uses it |
|---|---|---|---|---|
| **Board frame** | mm | down | the outline bbox **top-left** = (0, 0) | the netlist `board` block, every component `x`/`y` and `"at"`, preroutes, lint, the gates, all outline_audit messages ("board frame, mm") |
| **External / Fusion / DXF frame** | mm | up | `board.datum` (the Fusion sketch origin) | the DXF in and out, `--drill-origin` STEP, the datum CPL |
| **Sheet frame** | mm | down | KiCad's page corner | pcbnew; sheet = board frame + (ox, oy), the Edge.Cuts bbox top-left on the page |

The builder makes the datum the KiCad **aux (drill / place-file) origin and
grid origin** (`board_outline.set_datum`), so every `kicad-cli` export that
takes the drill origin lands in the Fusion frame. A point converts with
`outline.external_to_board(p, datum)`: `x_board = x_fusion + datum_x`,
`y_board = -y_fusion + datum_y`; `outline.board_to_external` is the inverse.

**Bulges.** A loop vertex is `[x, y]` or `[x, y, bulge]`; the bulge describes
the arc from this vertex to the next, `b = tan(theta/4)` of the sweep angle
`theta` (0 = straight, `0.41421356` = a 90 degree arc, `1` = a half circle).
A positive bulge sweeps **math-CCW in the frame the numbers live in** (the arc
bows to the right of the chord direction, positive shoelace area); in the
Y-down board frame that looks clockwise on screen. **Flipping Y negates every
bulge**: the DXF reader negates them when it flips a Fusion outline into the
board frame and the DXF writer negates them again. A circle is two vertices
with bulge 1 (or a `{"circle": [cx, cy, r]}` cutout). An `Outline` stores
its outer ring math-CCW and every cutout math-CW (material on the left of
travel), whatever direction you wrote; `outline.from_board_block(board)`
orients them.

Exact arcs go to KiCad (`outline.to_kicad_primitives`); flattened polylines
(`outline.flatten`, chord error 0.05 mm) are only ever used for the math.

## 2. The board block: its five forms

`outline_resolve.resolve_board(design, base_dir)` turns any `board` block into
one normalised block and names its `source`:

| `source` | Block | Legacy? |
|---|---|---|
| `none` | absent, `null` or `{}`: the builder sizes the board to its parts | yes |
| `rect` | `w`/`h` only (`corner_radius` 0) | yes, unless `cutouts` or `datum` are present |
| `rounded_rect` | `w`/`h` + `corner_radius` > 0 (exact 90 degree arcs) | no |
| `loop` | `"outline": {"loop": [...]}` (+ `cutouts`, `datum`) | no |
| `dxf` | `"outline": {"dxf": "mech/outline.dxf", "layer": "OUTLINE", "units": "mm"}` | no |

A **legacy** board (`ResolvedBoard.is_legacy`) keeps the pre-outline builder
path bit for bit: no outline lint rules, no outline gates, no mechanical
exports (G5 records `rectangular board without datum`). Everything else is an
**outline board** with exact Edge.Cuts, the aux origin at its datum and planes
that follow the outline.

```jsonc
"board": {
  "outline": {"loop": [[4,0],[56,0,0.41421356],[60,4],[60,36,0.41421356],
                       [56,40],[4,40,0.41421356],[0,36],[0,4,0.41421356]]},
  "cutouts": [{"loop": [[27,7],[27,11,-1],[33,11],[33,7,-1]]},   // a stadium slot
              {"circle": [30, 20, 2.5]}],                          // [cx, cy, r]
  "datum": [10, 30],               // the Fusion origin, board frame (default: [0, h])
  "panel_cutouts": false           // true: a connector may mate through a cutout edge
}
```

(`examples/outline_rounded_cutouts`: 60 x 40 r=4; `examples/outline_lshape`
is a 60 x 45 L with a 28 x 20 notch and a filleted inner corner.)

Contracts, enforced by `outline.from_board_block(board)` and the resolver
(the schema only types the keys):

- **The loop bbox starts at (0, 0)** within 0.001 mm; a `w`/`h` next to it
  must equal that bbox. Translate the loop (and every coordinate in the
  netlist); `outline.normalize_external(loops, y_up=True)` does it for DXF /
  Fusion geometry and reports where the source origin landed (the datum).
- **`corner_radius` only with `w`/`h`**, `0 <= r <= min(w, h) / 2`; with an
  `outline`, draw the fillets into the loop as bulge vertices.
- **Cutouts** are `{"loop": ...}` or `{"circle": [cx, cy, r]}`, each strictly
  inside the outline and apart from the others (crossing, touching or nesting
  is refused); a ring must be simple (no self-crossing) and enclose area.
- **`datum`** is `[x, y]` in the board frame. **Default = the outline bbox
  bottom-left**, (0, h), when absent (`build_features.datum_board`,
  `write_outline_dxf`, the e2e check all use the same rule).
- **A `dxf` outline defines the board**: `cutouts`, `datum` and
  `corner_radius` beside it are refused (draw them in the DXF); a `w`/`h`
  must match its bbox; `layer` / `units` apply only to a `dxf` (a loop is
  always mm). `board.outline` has exactly one of `loop` / `dxf` and no other
  key.
- `auto_x` / `auto_y` / `auto_w` / `auto_h` / `auto_b` are windows inside a
  fixed board (section 6); a block of `auto_*` keys alone is rejected.

`validate_board_outline(netlist)` checks a netlist's block without building
anything: the resolver, `outline.check` (closed simple rings, cutouts inside
and apart), the fab rules of section 8, the outline summary (bbox, area,
cutouts, datum, narrowest web), every fixed part's anchor and the mounting
holes (section 6). Issues are `{code, severity, ref, message, at}` in the
board frame.

```powershell
<venv>\python server\tools\outline_resolve.py <netlist.json> [--sidecar out.json]
```

prints `{ok, source, fingerprint, bbox, size, is_legacy, warnings, sidecar}`.
The **fingerprint** is sha256 of the canonical block plus, for a DXF, the
file bytes: the route cache keys on it as `outline_fp`
(`rp_feature_outline.route_hash_extra`), so a re-exported DXF re-routes the
board. The builder reads the block through a **sidecar**
`<pcb stem>.outline.json` (version 1: `{version, board, source, dxf,
fingerprint, warnings}`) that `build_board_from_netlist(netlist)` writes
beside the board and names in the environment variable `KICAD_EDA_OUTLINE`,
because KiCad's bundled Python cannot read DXF.

## 3. DXF in (Fusion 360 to the netlist)

`dxf_io.read_board_outline(path, layer=None, units=None)` (venv, `ezdxf`
1.4.4 from `server/requirements.txt`) reads LINE, ARC (a full turn is a
circle), CIRCLE, LWPOLYLINE / 2D POLYLINE (bulges stay exact arcs), 3D
POLYLINE, SPLINE / ELLIPSE (flattened within the curve tolerance 0.02 mm) and
INSERT / MINSERT (every array cell exploded); POINT, TEXT and meshes are
counted as skipped. Then:

1. **Units**: `units=` (`mm`, `cm`, `in`, `mil`) > a non-zero `$INSUNITS` >
   `$MEASUREMENT` (0 = inch, 1 = mm; all KiCad 9's own DXF export writes) with
   a warning > mm with a warning. The Fusion exports the reader was built
   against write `$INSUNITS 5` (cm) with `$MEASUREMENT 0` even for a metric
   sketch; the reader follows `$INSUNITS` (the `examples/outline_fusion_dxf`
   file is a plain mm DXF: `$INSUNITS` 4, layer `0`, no sketch transform).
   An outline under 1 mm or over 1000 mm a side gets a "check the units"
   warning.
2. **Plane and origin**: the layer's `NFusion_SketchTransform` (Fusion's 4 x 4
   sketch transform, tried first) when the geometry lies in that plane, so
   the 2D frame is the **sketch frame and its origin is the sketch origin**
   (what the Fusion user sees); else XY; else the common arc normal; else,
   for lines only, a fitted plane with a warning (the outline may be mirrored
   against the sketch). Sketches on different planes in one file are refused
   (pick one with `layer=`).
3. **Layer**: `layer=` keeps one layer (any case); blank = every layer. KiCad
   9's own DXF export names layers by colour (`WHITE`, `BLACK`), KiCad 10.99
   writes `Edge.Cuts`.
4. **Chaining**: duplicates dropped with a warning; pieces joined end to end
   within the join tolerance 0.01 mm; three ends meeting is a branch (error:
   construction or duplicate geometry); chains left open raise an error that
   pairs every dangling end and says whether a larger `tol` is export noise
   or would merge real geometry; degenerate loops (area < tol^2 or mean width
   < tol) are dropped.
5. **Classification**: the outer loop is the largest |area|; every other loop
   must lie strictly inside it and apart from the other cutouts, else the read
   fails naming them (`allow_stray` drops them with a warning). A circle
   cutout no wider than the fab's 1 mm milled slot is warned as a drill hole
   that KiCad exported (`--drill-shape-opt 0` avoids that).
6. **Normalisation**: Y flipped (bulges negated), the bbox translated to
   (0, 0), the datum = where the drawing origin landed; `.to_board_block()`
   gives the D2 keys (`w`/`h` = the bbox, `board.outline.loop`, `cutouts`,
   `datum`).

**Fusion export checklist.**

- One flat sketch; delete construction lines or move them to another layer
  (Fusion exports a center rectangle's diagonals as plain LINEs, which chain
  into branches or open paths), or pass `layer=`.
- The datum is the **sketch origin**: put it where the mechanical reference
  is; everything in the STEP and the datum CPL is measured from it.
- Coincident constraints at every corner (gaps wider than 0.01 mm are
  reported with both entity handles). KiCad itself closes Edge.Cuts gaps only
  up to 0.01 mm (10000 nm) and 9.0.4 is not deterministic at exactly that
  value, so the builder gives meeting primitives the identical point.
- Draw holes as circles (a single-arc loop is refused: "draw the hole as a
  CIRCLE"); mounting holes that are drilled, not milled, belong in the netlist
  (`mounting_holes`), not in the outline.
- Fusion's `$INSUNITS 5` is read as cm correctly; only pass `units=` for a
  file whose header is wrong.

**The tool.** `import_outline_dxf(netlist, dxf, layer="", units="", mode="reference", parts="", backup=True)`
rewrites the netlist atomically (`<netlist>.bak` keeps the previous file):

- `mode="reference"` writes `board.outline = {"dxf": <path relative to the
  netlist>, layer?, units?}`, re-read at every build; `mode="embed"` writes the
  loops, cutouts, datum and `w`/`h`. Conflicting board keys (`w`, `h`,
  `corner_radius`, `cutouts`, `datum`) are removed or replaced with a note;
  the other keys (`auto_*`, `panel_cutouts`, ...) stay.
- `parts` is `{ref: [x, y, rot]}` (an object, JSON text or a JSON file path)
  of fixed parts **in the Fusion frame** (mm, Y up, origin = the datum). Each
  becomes the component's `"at"` through `mech_export.fusion_anchors`: x/y via
  `outline.external_to_board`, `rot` unchanged (Fusion's CCW-positive in a
  Y-up frame is KiCad's displayed sense), `[x, y]` keeps the part's current
  rotation; its `x` / `y` / `rot` are removed. An unknown ref is refused.
- The result is validated (resolver + fab rules) **before** anything is
  written; it returns `{ok, netlist, backup, mode, source, board, bbox, size,
  datum, parts, notes, warnings, fab_profile, fab_issues, fingerprint}`.

`examples/outline_fusion_dxf` was made this way: a Fusion DXF of the 60 x 40
r=4 board with the stadium slot and the round hole, parts placed in the
Fusion frame.

```powershell
<venv>\python server\tools\dxf_io.py read <file.dxf> [--layer L] [--units mm|cm|in|mil] [--tol MM] [--curve-tol MM] [--allow-stray]
```

prints the board block, size, units and their source, the plane, the layers,
the skipped entities and the warnings as JSON.

## 4. DXF out (the outline to Fusion)

**Before the board exists**: `write_outline_dxf(netlist, out_dxf)` writes the
resolved outline (any of the five forms) as DXF R2010, mm (`$INSUNITS` 4,
`$MEASUREMENT` 1), Y up, origin = the datum (default: the bbox bottom-left),
closed LWPOLYLINEs with exact bulges and CIRCLEs on layer `OUTLINE`. The CLI
is `dxf_io.py write`:

```powershell
<venv>\python server\tools\dxf_io.py write <board.json|netlist.json> <out.dxf> [--layer L]
```

**After the build**: `export_outline_dxf(pcb, out_dxf)` runs, with the
kicad-cli of the KiCad that wrote the board,

```text
kicad-cli pcb export dxf --layers Edge.Cuts --use-drill-origin --ou mm --drill-shape-opt 0 --mode-single -o <out> <pcb>
```

**never `--use-contours`** (it turns every arc into a polyline). A flag this
install's `pcb export dxf --help` does not list is dropped with a warning
(`mech_export.dxf_command`); without `--ou mm` the file is in kicad-cli's
default inches and is read by its header. The DXF is read back and compared
with the board's Edge.Cuts (read as text by `mech_export.board_file_outline`,
aux origin = datum) in the datum frame: `ok` only when the Hausdorff distance
of the edges is <= 0.01 mm (`mech_export.frame_hausdorff`). The result carries
`hausdorff_mm`, `tolerance_mm`, `command`, `kicad_cli` and the warnings.

## 5. STEP and the datum CPL

`export_step(pcb, out_step, board_only=True)` runs
`kicad-cli pcb export step --drill-origin --force [--board-only] -o <out> <pcb>`:
with the aux origin on the datum the board lands exactly where the Fusion
sketch had it (the 60 x 40 board with datum (10, 30) spans x -10..50,
y -10..30). `bbox` is a text scan of the STEP's B-rep edges
(`mech_export.step_bbox`: every VERTEX_POINT plus the axis extremes of each
CIRCLE edge, scaled to mm from the file's unit); assembly transforms are not
applied, so the bbox is the board body's only with `--board-only`
(`board_only=False` adds a warning saying so). A STEP without 3D models still
exports; missing-model lines come back as warnings.

`export_pos_datum(pcb, out_csv)` runs
`kicad-cli pcb export pos --use-drill-file-origin --units mm --format csv`:
the **mechanical datum CPL**, every part position relative to the datum in
the Fusion frame, for the mechanical round trip only. **The fab CPL stays
absolute**: `export_fab_package` writes the pick-and-place in absolute sheet
coordinates to match the Gerbers, and nothing here changes that.

In the harness these three exports are G5's mechanical step (section 7).

## 6. Placement on an outline

- **`"at": [x, y, rot]`** (D3) is the **footprint origin** in the board frame
  and its **final rotation as KiCad displays it**; for `"side": "B"` that is
  the post-flip rotation, the value the saved board stores. The builder
  (`swig_anchor.place_at`) does `SetPosition(origin + at)`, `board.Add`, the
  flip about the origin for side B, then `SetOrientationDegrees(rot)`, so the
  displayed rotation is exactly `rot` on both sides. A component has `"at"`
  or `x`/`y`/`rot`, never both.
- **`x`/`y`** keep their meaning: the **courtyard-bbox centre**; `rot` is the
  packer's rotation.
- **`"edge"`** (`top` | `bottom` | `left` | `right`, `null` = top): the board
  edge a connector mates through. The packer puts it on that side
  (`build_layout`); after placement an **auto** part with an `edge` is turned
  to face it and slid onto the **outer** ring (`board_field.snap_to_edge`,
  cutouts ignored), its courtyard hanging `overhang` past the edge, else
  flush. On a fixed part (`"at"` or x/y) `edge` is ignored with a note. The
  key is in the route hash (moving a connector moves copper).
- **`overhang`** (mm): how far a courtyard may hang past the **outer** edge
  (G3, `outline_audit.courtyard_issues`); crossing a cutout is always an
  error. An auto
  `overhang` part without an `edge` is lifted up by it (the pre-outline
  behaviour, for a radiator).
- **Derived overhang (edge-mount footprints).** Stock edge-launch footprints
  mark the board edge with a `PCB Edge` text on Dwgs.User / Cmts.User.
  `edge_mount.edge_from_primitives(prims, courtyard_bbox)` reads that marker
  (a parallel marker line within 1.5 mm of the text, else the text itself,
  else an `EdgeMount` name / tag) and the courtyard extent beyond it; without
  a netlist `overhang` the builder snaps an `edge` part by that derived
  overhang (reported as `derived_overhang` in the build result) and G3
  requires the marker to sit within 0.25 mm of the outline
  (`edge_mount_off_edge`, section 7). A derived overhang that points away
  from the named edge after placement snaps flush with a warning.
- **`interior: true`** exempts a J* part from the connector rule (it does not
  mate through an edge); `panel_cutouts: true` on the board lets a connector
  face a cutout edge.
- **Fields** (`board_field.auto_fields(board_block, outline, edge_clearance, mounting_holes)`):
  a legacy w x h board keeps the pre-outline field arithmetic; explicit
  `auto_*` windows are kept when they lie on the board with the copper-to-
  edge clearance 0.3 mm, else **clipped** to the largest rectangle inside
  them on the board (with a warning saying what is left); without `auto_*`
  the field is the **largest inscribed axis-aligned rectangle**
  (`outline.largest_inscribed_rect(o, clearance, grid)`) inset by the
  clearance + 0.5 mm, minus a square keepout of drill / 2 + 1 mm around every
  mounting hole. G1 warns (WO2) when that field is under 20% of the board
  area. True polygon packing is out of scope (D2).
- **Mounting holes**: `mounting_holes.positions` are judged by their drill
  circle (on the material: error; nearer the edge than the fab's
  `hole_to_edge`: warning). Without positions the four default corner holes
  (inset 4 mm) exist only on a `rect` / `rounded_rect` board; any other
  outline needs explicit positions (EO3 at G1, a build refusal at G3). The
  drill comes from a `MountingHole_<d>mm` footprint name, else 3.2 mm.
- **Fixed-part checks** before the build. G1's EO2 (section 7): an `"at"` or
  x/y point inside a cutout or more than 10 mm off the material is an error;
  nearer past an edge (a mid-mount connector in a notch) or within 1 mm of an
  edge on the material is a warning (G3 judges the courtyard).
  `validate_board_outline(netlist)` is stricter about the outer edge: a point
  off the board is `part_outside` unless the part's `overhang` reaches it,
  inside a cutout `part_in_cutout`, under 1 mm from an edge `part_near_edge`
  (not for `overhang` / `edge` parts).
- **Planes** follow the outline: the GND / power zones are the Edge.Cuts
  polygon inset (round corners) by the copper-to-edge clearance 0.3 mm
  (`board_outline.plane_polyset`), filled in place; a narrow neck can split a
  zone into islands (G3 warns `plane_islands`).

## 7. Gates

**G1 (lint)**: `server/tools/lint_rules_outline.py` (ORDER 30), on the
resolved block; legacy boards get no outline rule.

| Code | Level | Finding |
|---|---|---|
| EO1 | error | the board block does not resolve (the resolver's / outline's message: a loop off the frame, `corner_radius` with an outline, a cutout outside or crossing, an unreadable DXF, ...) |
| EO2 | error | a fixed part's point (`"at"` = footprint origin, x/y = courtyard centre) inside a cutout (unless `panel_cutouts`) or more than 10 mm off the material; **warning** when it is nearer past an edge or within 1 mm of an edge on the material |
| EO3 | error | mounting holes: a drill off the material; default corner holes on a non-rectangular outline ("give `mounting_holes.positions`"); a malformed inset or position; **warning** when a drill is nearer the edge than `hole_to_edge` |
| EO4 | error | a preroute `["xy", x, y]` point, the copper between two xy points or a via off the board or in a cutout |
| EO5 | error | outline fab errors (section 8); a minimum missed by <= 0.001 mm is KiCad's grid rounding, not an error |
| WO1 | warning | outline fab warnings, an assumption rule keeping its `ASSUMPTION` marker |
| WO2 | warning | auto-placed parts but no auto-placement field, or an inscribed one under 20% of the board area; an explicit field clipped or dropped |
| WO3 | warning | an `antenna_keepout` or a height zone reaching outside the outline bbox |
| WO4 | warning | DXF reader warnings, passed through |
| WO5 | warning | a DXF outline but no netlist directory given to lint: nothing checked |

**G3 (placement)**: `swig_outline_checks.placement_findings(board, design, shape)`
inside `check_design_gates` (the harness) and the SWIG gate script, with the
outline read from the board's own Edge.Cuts (`board_outline.outline_from_board`,
board and footprint shapes, chained at 0.001 mm). Each finding has a stable
code (the gate JSON's `codes` / `warning_codes`, parallel to its texts):

| Code | Level | Finding |
|---|---|---|
| `outline_open` | blocker | KiCad cannot close Edge.Cuts into the board polygon (a gap over 0.01 mm, an overlap, a stray shape) |
| `outline_missing` | warning | no Edge.Cuts at all: the outline rules are skipped, the connector rule uses the bbox |
| `outline_unmodelled` | warning | KiCad closes it but the outline model cannot: rules skipped |
| `courtyard_outside` | blocker | a courtyard off the board, in a cutout, or past its `overhang` (netlist or derived) |
| `pad_edge_clearance` | blocker | a pad off the board or nearer an edge than the board's copper-to-edge clearance (0 for an edge-mount part: its pads run to the edge it straddles) |
| `hole_edge` | blocker / warning | a mounting-hole drill (refs H<n>, or every pad NPTH) off the board / nearer the edge than the fab's `hole_to_edge`; never judged by its courtyard |
| `fab_outline` | blocker / warning | `outline_audit.fab_issues` for `FAB_PROFILE` (section 8): errors block, warnings warn |
| `connector_edge` | blocker | the mating face's nearest edge must face the mouth (outward normal within 45 degrees) and lie within 3 mm; a cutout edge counts only with `panel_cutouts`; texts keep the pre-outline prefixes "mating face points INTO the board interior" / "mating face is <d> mm from the board edge it faces" |
| `edge_mount_off_edge` | blocker / warning | an edge-mount part's `PCB Edge` marker more than 0.25 mm from the outline; a warning naming the `"edge"` key when the netlist left the part to the auto placer |
| `edge_mount_unread` | warning | edge-mount detection failed on a footprint; judged as not edge-mount |
| `plane_islands` | warning | a zone that fills as more than one island on a board with cutouts or a non-convex outline (GND included) |
| `plane_islands_unread` | warning | pcbnew could not hand a zone layer's fill back |

**G4 (routed)**: `swig_outline_checks.routed_findings(board, shape)` gives
`copper_outside` for a track / arc / via / pad off the material, because
**KiCad DRC does not flag copper lying wholly inside an Edge.Cuts cutout**
(edge clearance 0 here: kicad-cli DRC owns the clearance band). A board
pcbnew cannot load (a resumed KiCad 11 board) gets the same containment check
from the board text (`rp_feature_outline.text_copper_blockers`, through
`board_text_copper`); a board it cannot judge gets one blocker saying why.
Agent routing judges the outline exactly on every path it lays:
`route_outline.edge_obstacles` lists the outline and each cutout grown by the
edge clearance as obstacles, `route_outline.path_outline_errors` names copper
that crosses an edge or comes nearer than the clearance (the board API reads
the Edge.Cuts with `outline_geometry` of `server/backends/pcb_items.py`).

**G5 (fab)**: `rp_feature_outline.g5_mechanical(pcb, netlist, design)`
writes `<pcb stem>_mech/` beside the board: `<stem>.step` (board only,
`--drill-origin`), `<stem>_outline.dxf` (read back: `hausdorff_mm`) and
`<stem>_datum_cpl.csv`, through the section 4 / 5 tools. Its problems are G5
**warnings** only (a STEP export without 3D models must not fail a run);
the record is `report['mechanical']` (`{ok, dir, files, step, dxf, datum_cpl,
hausdorff_mm, step_bbox, warnings}`, or `{"skipped": "rectangular board
without datum"}`), rendered as the review's `## Mechanical` section
(`review_mechanical.render(report, design)`).

**Route cache**: `outline_fp` (the resolver fingerprint, DXF bytes included)
and the component `edge` key.

**Running it.** The harness takes an outline board like any other (the
`board_dims` hook sizes it from the outline bbox, so no `<board_w> <board_h>`):

```powershell
<venv>\python pipeline\run_pipeline.py examples\outline_lshape\netlist.json
```

(`/kicad-eda:pipeline` and `/kicad-eda:napkin` do the same.) The **e2e
check** then judges the built board against the netlist outline under
KiCad's bundled Python:

```text
<kicad-python> pipeline\e2e_outline_check.py <pcb> <netlist> [<report>] [--caught <REF>]
```

the Edge.Cuts polygon area within 0.05 mm^2, the aux origin = datum + the
sheet offset within 0.001 mm, no courtyard / pad / copper error on the final
board, and with the report the STEP bbox in the Fusion frame within 0.05 mm,
the Hausdorff <= 0.01 mm and a datum CPL with rows (`--caught REF`: the G3
rules must flag REF `courtyard_outside`, as `examples/outline_negative_cutout`
expects). It runs as the `post_checks` of `examples/outline_*/run_all.json`
in `pipeline/run_all.py`.

## 8. Fab rules (`outline.validate_fab(o, profile, holes)`)

`outline_ops.FAB_LIMITS` holds one profile, `JLCPCB`; `FAB_PROFILE` (the
toolchain's / the environment's) selects it and any other profile is judged
as JLCPCB with a warning. Each problem is `{rule, severity, message, at,
value, limit, source, assumption}`; `outline_audit.fab_issues` turns them into
`fab_<rule>` issues and an assumption rule keeps its source in the text.

| Rule | Limit | Severity | Source |
|---|---|---|---|
| `board_size` | 3 mm x 3 mm minimum | error | JLCPCB capabilities table |
| `slot_width` | 1 mm minimum milled gap (cutouts, notches; a round cutout this small "should be an NPTH drill") | error | capabilities table ("Non-Plated Slots: 1.0mm") |
| `hole_size` | 0.5 mm minimum NPTH | error | capabilities table |
| `hole_inside` | a drill off the board or in a cutout | error | geometry |
| `cusp` | a ring doubling back on itself (a zero-angle spike or slit) | error | geometry: no router bit fits the tip |
| `internal_corner_radius` | 0.5 mm router bit: an internal arc tighter than it, or a sharp reflex corner where the bit leaves > 0.05 mm of material (a 90 degree corner: 0.207 mm; a 45 degree turn: 0.041 mm, not reported) | warning | JLCPCB support ("normally 0.5 mm milling tool radius"), **not in the capabilities table** |
| `hole_to_edge` | 0.25 mm from a drill's edge to the board edge | warning (error when the hole breaks the edge) | a JLCPCB support answer, **not in the capabilities table** |
| `feature_width` | 0.8 mm minimum board material (necks, webs, fingers) | warning | **ASSUMPTION**: JLCPCB publishes no minimum web width; 0.8 = 2 x the +/-0.2 mm routed-edge tolerance + 0.4 mm of FR4 |

Widths are measured where they are narrowest: between two edge interiors
(rays along the edge normals) and exactly at the vertices (a web under a
V-notch tip, a waist between two tips, two hole corners); a measurement across
the wedge of a single corner is not a width, so a 60 degree tip is clean
while a sliver sharper than 10 degrees is judged.

Known limitations:

- **Polyline fillets.** A fillet drawn as a polyline (a flattened SPLINE or
  ELLIPSE, a faceted DXF export) is judged vertex by vertex: each small reflex
  turn where the bit leaves <= 0.05 mm is not reported, so a fine polyline
  fillet passes, a coarse one is reported as sharp corners, and a polyline
  fillet **tighter than 0.5 mm is not reported** as an internal arc (only an
  exact arc gets the "tighter than the router bit" warning). Draw fillets as
  arcs (bulges) so they are judged by radius.
- **Largest inscribed rectangle, 160-step grid.** `outline.largest_inscribed_rect`
  searches a 160 x 160 grid over the flattened outline with grid lines
  through every vertex (exact for rectilinear outlines), then pushes each side
  out with the exact containment test; for a curved outline the answer is
  within one grid cell (bbox / 160) of the optimum. An outline whose
  flattened vertex count exceeds the grid (a tessellated DXF) falls back to
  the uniform grid alone.

## 9. Freerouting: the DSN outline rewrite

Freerouting keeps copper only its clearance-class distance (0.2 mm) from the
DSN `(boundary ...)` and from every hole keepout, while KiCad's DRC wants the
board's copper-to-edge clearance (0.3 mm): on a rounded board with a slot and
a round hole that left copper 0.21-0.27 mm from the hole on 4 of 5 routes. So
`autoroute_board(pcb)` has the DSN export (`server/tools/swig_export_dsn.py`)
rewrite a **polygon board's** DSN before routing
(`dsn_outline.rewrite_outline(dsn_text, outline, origin_mm, edge_clearance_mm)`):

```text
delta = edge_clearance - min(untyped DSN clearance) + margin
      = 0.3 - 0.2 + 0.02 mm = 0.12 mm
```

- The boundary is **deflated** and every Edge.Cuts hole keepout **inflated**
  by `delta` through the exact `outline.inset(...)`: holes grow, holes that
  meet merge (the first keepout gets the merged ring, the rest are deleted),
  a hole that meets the edge becomes a bay of the boundary; keepouts are
  matched to cutouts by centroid within 0.5 mm, an unmatched one stays as
  exported. Rings are flattened at 0.005 mm and written in KiCad's own form
  (closed, wrapped, the file's newline).
- Only polygon boards are rewritten (`swig_export_dsn.polygon_reasons`: any
  cutout, a circular outline, any arc, more than 4 vertices, a non-axis-
  aligned edge); a rectangle's DSN stays byte-identical to KiCad's export.
  The existing boundary's bbox must match the outline's (else nothing is
  rewritten blindly); a boundary already rewritten is left alone.
- When the inset cannot shrink the outline by `delta` (a narrow neck) KiCad's
  raw export is kept with a warning; DRC and the G4 `copper_outside` check
  still judge the routed copper. Only the untyped `(rule (clearance N))` of
  the structure, classes and nets count (a typed `smd_smd` clearance never
  holds against the boundary); the class split of plane nets is D7's
  `dsn_layers`, not this step. The autoroute result carries `outline_rewrite`
  (`{changed, reason, delta, min_clearance_mm, edge_clearance_mm,
  boundary_points, holes_replaced, holes_unmatched, keepouts_deleted,
  warnings}`).

## 10. KiCad 9 vs 10 (pcbnew facts the outline code absorbs)

The SWIG board work runs on KiCad 9.0 locally and 10.0 in Docker; both are
probed, and `server/tools/board_outline.py` is the one adapter:

| Topic | KiCad 9.0.4 | KiCad 10.0.5 | What the code does |
|---|---|---|---|
| `GetBoardPolygonOutlines` | a positional 2nd argument is read as `aErrorHandler` | a required 2nd parameter `aInferOutlineIfNecessary` | `board_outline.polygon_outlines(board)` tries the keyword form, then the bare call; success is the returned bool only (9.0.4 fills a bbox fallback when it returns False) |
| Courtyard caches | **empty** after `LoadBoard` and after `FootprintLoad` + `SetPosition` + `Add` | filled | `board_outline.courtyard(fp, layer)` calls `BuildCourtyardCaches()` first; every gate script does so on every footprint |
| Rule-area pour flag | `Set`/`GetDoNotAllowCopperPour` | `Set`/`GetDoNotAllowZoneFills` | `board_outline.set_no_pour(zone)` / `board_outline.blocks_pour(zone)` |
| Page settings | an opaque SWIG object | the same | the paper is spliced into the saved file after `SaveBoard` (`build_features.paper_sexpr`); legacy boards keep A4, an outline board gets the smallest landscape A4..A0 holding it with 20 mm margins, else a User sheet capped at 1219.2 mm a side |
| `LoadBoard` of a missing / unreadable file | returns `None`; passing it on segfaults | the same | `board_outline.load_board(path)` raises instead |
| Zone outlines | `SetOutline(<Python SHAPE_POLY_SET>)` saves zero area | segfaults in `Area()` | `board_outline.fill_zone_outline(zone, polyset)` fills `zone.Outline()` in place |
| Edge.Cuts gaps | closed up to 10000 nm, not deterministic at exactly 10000 nm | closed up to 10000 nm | `board_outline.draw_outline(board, outline, ox, oy)` gives meeting primitives the identical VECTOR2I and checks KiCad's polygon area against the model (0.05 mm^2 + 0.1 %) |
| Rounded rectangle shapes | `SHAPE_T_RECT` | `SHAPE_T_RECTANGLE` with `GetCornerRadius` | `board_outline.shape_records` reads both |
| Zone layer names | `board.GetLayerName(zone.GetLayer())` | the same (`ZONE.GetLayerName` is not it) | every reader uses the board's name |
| Saved board format | `(version 20241229)` | `(version 20260206)` | a KiCad 11 board is neither: pcbnew cannot load it, G4 falls back to the text check |
| Own DXF export | layers named by colour (`WHITE`, `BLACK`), header `$MEASUREMENT` only | 10.99: `Edge.Cuts` | the reader follows `$MEASUREMENT` with a warning; `export_outline_dxf` passes `--ou mm` when the install lists it |

## 11. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| EO1 "board.outline.loop's bbox starts at (x, y)" | the loop is not in the board frame: translate it (and every coordinate) by the amounts named, or let `import_outline_dxf` normalise a DXF |
| EO1 "corner_radius only rounds the w x h rectangle" | put the fillets into the loop as bulge vertices |
| "the outline does not close: N gaps wider than the 0.01 mm join tolerance" | join the ends in the sketch (coincident constraints); when every gap is export noise the message names a `tol` to pass; an "open path" is construction geometry or a missing edge |
| "N entity ends meet at (x, y)" | a branch: construction lines (a center rectangle's diagonals) or duplicate geometry; delete them or move them to another layer and pass `layer=` |
| "no outline geometry on layer 'X'" | the layer name differs (KiCad 9 writes `WHITE` / `BLACK`, 10.99 `Edge.Cuts`, Fusion the sketch's layer); the message lists the layers with geometry; pass `layer=""` for every layer |
| "the outline is W x H mm, implausible for a PCB" | wrong units: pass `units=` (the message says what was read and why) |
| "N loop(s) cannot be cutouts of the outer loop" | stray loops (a second outline, a loop crossing the edge): move them to another layer, or `allow_stray=True` drops them |
| "a d mm circle ... looks like a drill hole; export with --drill-shape-opt 0" | KiCad exported the drills as circles; re-export with that flag or delete them |
| "closes on itself with a single arc edge" | a hole drawn as one 360 degree LWPOLYLINE arc: draw it as a CIRCLE |
| "sketches on different planes" / "not in one plane" | export one flat sketch, or pick its layer with `layer=` |
| "board.cutouts / board.datum cannot be combined with a dxf outline" | the DXF supplies them: draw the holes in the DXF, make the datum the sketch origin, or `mode="embed"` and edit the loops |
| EO3 "mounting_holes has no positions ... this outline is 'loop'" / G3 build refusal "mounting_holes needs explicit positions" | default corner holes exist only on a rectangle: give `mounting_holes.positions` |
| G3 `outline_open` | KiCad cannot chain the Edge.Cuts (a gap over 0.01 mm, an overlap, a stray shape, a footprint's own Edge.Cuts graphic): close it and re-run |
| G3 `courtyard_outside` | the part's courtyard leaves the board: move it, or give it an `overhang` when it is meant to hang over the outer edge; crossing a cutout is never allowed (`part_in_cutout` from `validate_board_outline` names the same part before the build) |
| G3 `edge_mount_off_edge` | an edge-launch footprint placed inside the board: give the auto part an `"edge"`, or move the fixed part so the `PCB Edge` marker sits on the outline |
| G3 `connector_edge` "points INTO the board interior" | the mouth faces the wrong way or a cutout edge: rotate it, give it an `"edge"`, `interior: true` when it does not mate through an edge, or `panel_cutouts: true` |
| G3 `plane_islands` | a cutout or notch splits a plane: a corridor preroute or stitching, or check every island reaches its pads |
| G4 `copper_outside` after a clean DRC | copper lying wholly inside a cutout (KiCad DRC misses it): re-route that net or add a keep-out there |
| "the DSN outline was not rewritten (cannot deflate ...)" | a neck narrower than 2 x delta; KiCad's raw export is routed, DRC and G4 still judge the result; widen the neck or lower the edge clearance |
| `export_outline_dxf` not ok, "differs from the board's Edge.Cuts by d mm" | an old kicad-cli dropped a flag (see `warnings`), or `--use-contours` was used: the read-back must match within 0.01 mm |
| STEP `bbox` far from the outline | the export was not `board_only`: component models sit in their own product frames the scan does not transform |
| G5 "STEP export failed" | kicad-cli could not write it (missing 3D models only warn): a G5 warning, the fab package is unaffected; the review's Mechanical section lists the reason |
| WO5 "DXF outline not checked: lint ran without base_dir" | lint was called without the netlist directory; the harness and `validate_board_outline(netlist)` pass it |
| `build_board_from_netlist` "board.outline.dxf must be resolved first" | the builder was started by hand on a DXF netlist: run it through the tool / harness, which writes the `KICAD_EDA_OUTLINE` sidecar |
