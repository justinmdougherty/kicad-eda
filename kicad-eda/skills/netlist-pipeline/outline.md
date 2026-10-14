# Board outlines in the headless flow (shapes, DXF, anchors, edges)

Working summary for authoring a netlist whose board is not a plain rectangle,
or whose shape and part positions come from a mechanical design (Fusion 360).
The reference (frames and bulges, the DXF reader's rules, the kicad-cli
exports, every G1 / G3 / G4 code, the fab rules, the DSN rewrite, the KiCad
9 / 10 facts, troubleshooting) is `${CLAUDE_PLUGIN_ROOT}/docs/MECHANICAL.md`;
the annotated netlist example is `docs/PIPELINE.md` section 2.

## The `board` block: pick one form

Frame: mm, Y down, origin = the outline bbox **top-left**.

| Form | Write | Notes |
|---|---|---|
| rectangle | `"board": {"w": 60, "h": 40}` | the legacy board: no outline rules, no mechanical exports |
| rounded rectangle | `{"w": 60, "h": 40, "corner_radius": 3.0}` | `corner_radius` only with `w`/`h`, at most `min(w, h) / 2` |
| any shape | `{"outline": {"loop": [[x, y], [x, y, bulge], ...]}}` | the loop bbox must start at (0, 0); `w`/`h` optional and must match |
| a Fusion DXF | `{"outline": {"dxf": "mech/outline.dxf", "layer": "OUTLINE", "units": "mm"}}` | path relative to the netlist (or `${KIPRJMOD}` / absolute); `layer` / `units` only here; no `cutouts` / `datum` / `corner_radius` beside it |

- A loop vertex is `[x, y]` or `[x, y, bulge]`: the bulge is `tan(theta/4)`
  of the arc to the next vertex, `0.41421356` for a 90 degree arc, `1` for a
  half circle; positive sweeps math-CCW in the frame the numbers are in
  (clockwise on screen in the Y-down board frame). Direction is free.
- `"cutouts": [{"loop": [...]}, {"circle": [cx, cy, r]}]`: strictly inside
  the outline, apart from each other. A round hole no wider than 1 mm is a
  drill (`mounting_holes`), not a cutout.
- `"datum": [x, y]` (board frame): the Fusion / mechanical origin; it becomes
  the KiCad aux (drill) origin, so the STEP, the outline DXF and the datum CPL
  land in the Fusion frame (mm, Y up, origin = datum). Default: the bbox
  bottom-left, `[0, h]`.
- `"panel_cutouts": true` lets a connector mate through a cutout edge.
- `mounting_holes` on a non-rectangular outline needs explicit `positions`
  (the default corner holes assume a rectangle: G1 error EO3).

Check it without building: `validate_board_outline(netlist)` (resolver, ring
validity, the JLCPCB outline rules, bbox / area / narrowest web, fixed parts
and holes). The run's G1 lint reports the same as EO1..EO5 / WO1..WO5.

## Placing parts on the shape

- **`"at": [x, y, rot]`** = the footprint **origin** in the board frame and
  its final rotation as KiCad displays it (for `"side": "B"` the post-flip
  rotation). Use it for coordinates measured in the mechanical design. A part
  has `"at"` or `x`/`y`/`rot`, never both.
- **`x` / `y`** = the courtyard-bbox centre (unchanged).
- **`"edge": "top" | "bottom" | "left" | "right"`** on an auto-placed
  connector: the packer puts it on that side, then the builder turns it to
  face the edge and slides it onto the **outer** board edge, hanging its
  `overhang` past it. Ignored on a fixed part (`"at"` / x/y place it).
- **`overhang`** (mm): how far the courtyard may hang over the outer edge
  (never over a cutout). Leave it out for an edge-launch footprint that
  carries a `PCB Edge` marker (stock SMA / USB-C edge mounts): the builder
  derives the overhang from the marker and G3 requires the marker to sit on
  the outline within 0.25 mm (`edge_mount_off_edge`); a fixed edge-mount part
  placed inside the board is a blocker, an auto one without an `"edge"` a
  warning naming the key.
- **`interior: true`** on a J* part that does not mate through an edge.
- Auto parts pack into the largest inscribed rectangle (edge clearance + 0.5 mm
  in, mounting-hole keepouts out); explicit `auto_x` / `auto_y` / `auto_w` /
  `auto_h` / `auto_b` windows are clipped to the board. A field under 20% of
  the board area is WO2: fix more parts or give `auto_*`.
- A fixed part in a cutout or more than 10 mm off the material is EO2; one
  straddling the outer edge (a mid-mount connector in a notch) is a warning
  and G3 judges its courtyard (`courtyard_outside` unless its `overhang`
  covers it).

## The Fusion round trip

1. **In**: export the sketch as DXF (one flat sketch, construction lines
   deleted or on another layer, the datum at the sketch origin, holes as
   circles), then
   `import_outline_dxf(netlist, dxf="mech/outline.dxf", parts={"J1": [12.5, -4.0, 90]})`:
   `mode="reference"` (default) writes `board.outline.dxf`, `mode="embed"` the
   loops; `parts` are the fixed parts **in the Fusion frame** (mm, Y up,
   origin = the sketch origin) and become `"at"` anchors with their rotation
   unchanged. The tool validates before it writes and keeps `<netlist>.bak`.
   Units follow `units=`, else `$INSUNITS` (Fusion: 5 = cm), else
   `$MEASUREMENT`; gaps wider than 0.01 mm, branches and stray loops are
   errors that name the entities.
2. **Out, before the board exists**: `write_outline_dxf(netlist, out_dxf="mech/outline_from_netlist.dxf")`
   (R2010, mm, Y up, origin = the datum, exact arcs, layer `OUTLINE`).
3. **Out, after the build** (G5 does all three into `<pcb stem>_mech/` and
   the review's Mechanical section lists them):
   `export_step(pcb, out_step="board.step", board_only=True)` (`--drill-origin`,
   bbox in the Fusion frame), `export_outline_dxf(pcb, out_dxf="outline.dxf")`
   (read back against Edge.Cuts, Hausdorff <= 0.01 mm) and
   `export_pos_datum(pcb, out_csv="datum_cpl.csv")` (positions relative to
   the datum; the fab CPL of `export_fab_package` stays absolute).
4. The route cache keys on the outline (`outline_fp`, the DXF's bytes
   included) and on every `"edge"` key: a re-exported DXF re-routes.

## What the gates add on an outline board

- G1 EO1..EO5 / WO1..WO5 (block resolves, fixed parts, holes, preroute points
  on the material, fab rules, fields, keep-outs, DXF warnings).
- G3: `outline_open` (Edge.Cuts not closed), `courtyard_outside`,
  `pad_edge_clearance`, `hole_edge`, `fab_outline`, `connector_edge` (the
  nearest edge must face the mouth within 45 degrees and lie within 3 mm),
  `edge_mount_off_edge`, `plane_islands` (a cutout or notch splits a pour:
  corridor preroute or stitching).
- G4: `copper_outside` for copper inside a cutout (KiCad DRC misses it);
  Freerouting's DSN boundary is shrunk and its hole keepouts grown so the
  routed copper keeps the 0.3 mm edge clearance; agent routing sees the edge
  and every cutout as obstacles.
- G5: the STEP / outline DXF / datum CPL exports (warnings only when one
  fails) and the review's `## Mechanical` section.

## Fab limits judged (JLCPCB)

Board >= 3 x 3 mm; milled slots and gaps >= 1 mm (errors); internal corners
tighter than the R0.5 bit and necks under 0.8 mm (the latter an ASSUMPTION,
JLCPCB publishes none) are warnings; a drill needs 0.25 mm to the edge. Draw
fillets as arcs (bulges), not polylines, so they are judged by radius.

Record the shape decision (`record_decision(netlist, stage="requirements", question="Board outline and datum?", decision="Fusion DXF mech/outline.dxf, datum at the sketch origin", rationale="enclosure sketch v3", confidence="high", source="mechanical")`)
when the netlist is written by hand rather than through `import_outline_dxf`.
