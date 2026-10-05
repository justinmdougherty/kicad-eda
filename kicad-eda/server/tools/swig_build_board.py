#!/usr/bin/env python3
"""Headless board builder — runs under KiCad's bundled Python (has `pcbnew`).

Reads a netlist JSON (components + pad-level nets) and produces a .kicad_pcb with
every footprint loaded from the library, placed, and every pad assigned to its net.
This is the path the IPC API cannot do (it cannot instantiate library footprints),
so the server shells out to KiCad's interpreter to run this.

Usage:
  <kicad-python> swig_build_board.py <netlist.json> <out.kicad_pcb> <footprints_dir> \
                                     [board_w_mm] [board_h_mm]
Emits a JSON result on stdout.
"""
import json
import os
import sys
import traceback

import kicad_quiet  # noqa: F401  (before pcbnew: no modal wx dialogs)
import pcbnew


def mm(v):
    return pcbnew.FromMM(float(v))


def vec(x, y):
    return pcbnew.VECTOR2I(mm(x), mm(y))


def load_footprint(footprints_dir, lib_id):
    lib, _, name = lib_id.partition(":")
    libpath = f"{footprints_dir}/{lib}.pretty"
    fp = pcbnew.FootprintLoad(libpath, name)
    if fp is None:
        raise RuntimeError(f"FootprintLoad failed for {lib_id} (looked in {libpath})")
    return fp


from board_shapes import add_keepout, add_outline, add_plane  # noqa: E402
from place_engine import (fp_extent, fp_mouth, functional_place,  # noqa: E402
                          shelf_pack)

# JLCPCB-class fab floors (mm), applied to the board design settings.
# The via drill floor is ``m_MinThroughDrill`` (KiCad >= 9); the pre-9 field
# ``m_ViasMinDrill`` is deliberately absent so a current pcbnew applies every
# rule with no "skipped" warning.
FAB_RULES_MM = {"m_MinClearance": 0.15, "m_TrackMinWidth": 0.15,
                "m_ViasMinSize": 0.45, "m_MinThroughDrill": 0.2,
                "m_MicroViasMinSize": 0.2, "m_MicroViasMinDrill": 0.15,
                "m_CopperEdgeClearance": 0.3}


def apply_design_rules(board, rules_mm=FAB_RULES_MM):
    """Set the fab floors on the board's design settings. Returns the list of
    fields this pcbnew does not expose so the caller can report them; setattr
    on a missing SWIG field would silently create a dead Python attribute, so
    those are skipped, never faked. Raises RuntimeError if the settings object
    exposes none of the fields or a set fails."""
    ds = board.GetDesignSettings()
    skipped = [attr for attr in rules_mm if not hasattr(ds, attr)]
    if len(skipped) == len(rules_mm):
        raise RuntimeError(f"design settings expose none of {sorted(rules_mm)} "
                           f"in this pcbnew ({pcbnew.GetBuildVersion()}); "
                           f"fab rules not applied")
    for attr, val in rules_mm.items():
        if attr in skipped:
            continue
        try:
            setattr(ds, attr, pcbnew.FromMM(val))
        except Exception as e:
            raise RuntimeError(f"cannot set design rule {attr}={val} mm: "
                               f"{type(e).__name__}: {e}") from e
    return skipped


def main():
    netlist_path, out_path, fp_dir = sys.argv[1], sys.argv[2], sys.argv[3]
    board_w = float(sys.argv[4]) if len(sys.argv) > 4 else 50.0
    board_h = float(sys.argv[5]) if len(sys.argv) > 5 else 25.0
    placement = {}
    if len(sys.argv) > 6 and sys.argv[6] not in ("", "-"):
        placement = json.load(open(sys.argv[6], encoding="utf-8"))
    design = json.load(open(netlist_path, encoding="utf-8"))
    # merge placement overrides (ref -> [x, y, rot]) into components
    for c in design["components"]:
        if c["ref"] in placement:
            x, y, rot = placement[c["ref"]]
            c["x"], c["y"], c["rot"] = x, y, rot

    board = pcbnew.CreateEmptyBoard()
    board.SetCopperLayerCount(4)
    warnings = []   # non-fatal degradations, reported in the result JSON

    # JLCPCB-class design rules (so default-rule DRC reflects the real fab).
    # A rule that silently fails to apply means every later DRC runs against
    # the wrong fab floors, so a failed set is a hard error and a field this
    # pcbnew version lacks is reported, not hidden.
    skipped_rules = apply_design_rules(board)
    if skipped_rules:
        warnings.append(f"design rules not exposed by pcbnew "
                        f"{pcbnew.GetBuildVersion()} and skipped: "
                        f"{', '.join(skipped_rules)}")

    # The board is centred on an A4 sheet. CreateEmptyBoard() already defaults
    # the page to A4 (verified: the saved file carries ``(paper "A4")`` under
    # pcbnew 9.0.4, whose GetPageSettings() is an opaque SWIG object with no
    # PAGE_INFO API at all). The explicit set is only attempted where a real
    # SetType exists, so a version that genuinely fails to apply it is
    # reported and one that simply has no API is not.
    SHEET_W, SHEET_H = 297.0, 210.0
    ps = board.GetPageSettings()
    if hasattr(ps, "SetType"):
        try:
            ps.SetType("A4")
            board.SetPageSettings(ps)
        except Exception as e:
            warnings.append(f"page settings not applied ({type(e).__name__}: "
                            f"{e}); sheet stays at the pcbnew default")

    # --- load + measure every footprint (at the origin) ---
    placed, missing_pads = {}, []
    fps, extents = {}, {}
    pincount, mouth = {}, {}
    for c in design["components"]:
        fp = load_footprint(fp_dir, c["footprint"])
        fp.SetReference(c["ref"])
        fp.SetValue(c.get("value", ""))
        fps[c["ref"]] = fp
        extents[c["ref"]] = fp_extent(fp)  # (w, h, cx_off, cy_off) at rot 0
        pincount[c["ref"]] = fp.GetPadCount()
        mouth[c["ref"]] = fp_mouth(fp)

    auto_rots = {}
    board_spec = design.get("board") or {}
    fixed_refs = {c["ref"] for c in design["components"] if "x" in c and "y" in c}
    auto_comps = [c for c in design["components"] if c["ref"] not in fixed_refs]
    have_full_placement = not auto_comps
    if have_full_placement:
        # caller supplied coords + board size: honor them verbatim.
        targets = {c["ref"]: (c["x"], c["y"]) for c in design["components"]}
        if board_spec:
            board_w, board_h = float(board_spec["w"]), float(board_spec["h"])
    elif fixed_refs:
        # MIXED placement: mechanically-spec'd parts (a HAT's header, holes,
        # connectors) sit at their exact board-relative coordinates; everything
        # else is functionally packed into the remaining field. Requires a
        # fixed board size — a spec'd part position is meaningless on an
        # auto-sized outline. Collisions surface at the G3 courtyard gate.
        if not board_spec:
            print(json.dumps({"ok": False, "error":
                  'mixed placement requires "board": {"w": mm, "h": mm}'}))
            sys.exit(1)
        board_w, board_h = float(board_spec["w"]), float(board_spec["h"])
        scale = float(os.environ.get("PACK_SCALE", "1.0"))
        edge = 2.0 * scale
        has_conn = any(c["ref"][:1] == "J" and not c.get("interior")
                       for c in auto_comps)
        edge_top = 1.2 if has_conn else edge
        # The auto field is the rectangle left over after the FIXED parts (a
        # HAT's header along the top, connectors down an edge, corner holes).
        # The packer can't see those, so the netlist may carve the field
        # explicitly: auto_x/auto_w bound it horizontally, auto_y drops its
        # top below the fixed header, auto_h bounds its height. Defaults are
        # the full board inside the edge margins.
        auto_y = float(board_spec.get("auto_y", 0.0))
        y0 = edge_top + auto_y
        x0_spec = board_spec.get("auto_x")
        avail_w = float(board_spec.get("auto_w", board_w - 2 * edge))
        avail_h = float(board_spec.get("auto_h", board_h - y0 - edge))
        # Two-sided: components flagged "side":"B" pack into their own field
        # on the back of the board (the dense-HAT idiom — power stage and
        # connectors on top, support logic and passives underneath). Each side
        # is an independent functional placement over the SAME field rect;
        # F/B courtyards don't collide, so the field is effectively doubled.
        b_refs = {c["ref"] for c in auto_comps
                  if str(c.get("side", "F")).upper() == "B"}
        f_comps = [c for c in auto_comps if c["ref"] not in b_refs]
        b_comps = [c for c in auto_comps if c["ref"] in b_refs]
        # The B side may carve its OWN field (board-absolute x/y/w/h via
        # "auto_b") to dodge front-side through copper — THT connector pads
        # and thermal-via arrays exist on BOTH faces, so the back field must
        # avoid them where the front field need not.
        ab = board_spec.get("auto_b") or {}
        fields = {
            "F": (x0_spec, y0, avail_w, avail_h),
            "B": (ab.get("x", x0_spec), float(ab.get("y", y0)),
                  float(ab.get("w", avail_w)), float(ab.get("h", avail_h))),
        }
        targets = {}
        for side_comps, side_name in ((f_comps, "F"), (b_comps, "B")):
            if not side_comps:
                continue
            sx0, sy0, s_w, s_h = fields[side_name]
            sub = {**design, "components": side_comps}
            packed, rots_s, used_w, used_h = functional_place(
                sub, extents, pincount, mouth, margin=1.5 * scale,
                width_limit=s_w)
            if used_w > s_w or used_h > s_h:
                print(json.dumps({"ok": False, "error":
                      f"{side_name}-side auto cluster {used_w}x{used_h} mm does "
                      f"not fit the free field {s_w}x{s_h} mm of the "
                      f"fixed board {board_w}x{board_h} mm at pack_scale {scale}"}))
                sys.exit(1)
            x0 = (float(sx0) + (s_w - used_w) / 2.0
                  if sx0 is not None else (board_w - used_w) / 2.0)
            targets.update({ref: (x0 + bx, sy0 + by)
                            for ref, (bx, by) in packed.items()})
            auto_rots.update(rots_s)
        for c in design["components"]:
            if c["ref"] in fixed_refs:
                targets[c["ref"]] = (c["x"], c["y"])
            ov = float(c.get("overhang", 0) or 0)
            if ov and c["ref"] in targets and c["ref"] not in fixed_refs:
                tx, ty = targets[c["ref"]]
                targets[c["ref"]] = (tx, ty - ov)
    else:
        # auto: FUNCTIONAL placement — cluster each support passive at the IC it
        # serves, connectors mouth-out on the edge — then size the board to fit.
        # PACK_SCALE loosens spacing on a retry so a deterministic router that left
        # one pad unrouted gets more room.
        scale = float(os.environ.get("PACK_SCALE", "1.0"))
        packed, auto_rots, used_w, used_h = functional_place(
            design, extents, pincount, mouth, margin=1.5 * scale)
        edge = 2.0 * scale  # board margin around the placed cluster
        # Connectors must stay FLUSH to the edge they mate through, regardless
        # of pack_scale — a USB/SMA recessed 3 mm into the board can't mate
        # through an enclosure panel. The connector row sits along the top, so
        # only the top margin is pinned; the parts cluster keeps the scaled
        # breathing room on the other three sides.
        has_conn = any(c["ref"][:1] == "J" and not c.get("interior")
                       for c in design["components"])
        edge_top = 1.2 if has_conn else edge
        # corner mounting holes need a clear ring: hole at inset 4.0 with a
        # ~6.4 mm courtyard reaches 7.2 mm in from each edge
        if design.get("mounting_holes"):
            edge = max(edge, 7.5)
        board_w = round(used_w + 2 * edge, 2)
        board_h = round(used_h + edge_top + edge, 2)
        targets = {ref: (bx + edge, by + edge_top) for ref, (bx, by) in packed.items()}
        # antenna overhang: shift an edge_row part UP so its radiator section
        # extends past Edge.Cuts (the vendor-preferred RF geometry — antenna
        # over air, ground plane under the module body only)
        for c in design["components"]:
            ov = float(c.get("overhang", 0) or 0)
            if ov and c["ref"] in targets:
                tx, ty = targets[c["ref"]]
                targets[c["ref"]] = (tx, ty - ov)

    ox = round((SHEET_W - board_w) / 2.0, 3)
    oy = round((SHEET_H - board_h) / 2.0, 3)

    # --- place: anchor so each part's bbox-center lands on its target cell ---
    import math
    for c in design["components"]:
        ref = c["ref"]
        fp = fps[ref]
        rot = float(c.get("rot", auto_rots.get(ref, 0)))
        if rot:
            fp.SetOrientationDegrees(rot)
        w, h, cxo, cyo = extents[ref]
        tx, ty = targets[ref]
        # the bbox-center offset (cxo,cyo) was measured at rot 0; rotate it so the
        # bbox-center still lands on the target after the footprint is rotated.
        # NOTE: KiCad's footprint rotation is clockwise-positive (Y-down), the
        # opposite sin sign from textbook rotation — get this wrong and a 90/270
        # part's bbox lands 2x its anchor offset away. (Harmless at 0/180.)
        a = math.radians(rot)
        rx = cxo * math.cos(a) + cyo * math.sin(a)
        ry = -cxo * math.sin(a) + cyo * math.cos(a)
        ax = tx - rx + ox
        ay = ty - ry + oy
        fp.SetPosition(vec(ax, ay))
        board.Add(fp)
        # back-side part: flip around its own bbox-center so the courtyard
        # stays on the target cell, mirrored onto B.Cu. Flip needs the board
        # parent set (a parentless Flip segfaults pcbnew), hence after Add.
        if str(c.get("side", "F")).upper() == "B":
            fp.Flip(vec(tx + ox, ty + oy), True)
        placed[ref] = fp

    # --- nets + pad assignment ---
    netmap = {}

    def get_net(name):
        if name not in netmap:
            ni = pcbnew.NETINFO_ITEM(board, name)
            board.Add(ni)
            netmap[name] = ni
        return netmap[name]

    assigned = 0
    for net_name, conns in design.get("nets", {}).items():
        ni = get_net(net_name)
        for ref, pad_num in conns:
            fp = placed.get(ref)
            if fp is None:
                missing_pads.append(f"{ref}.{pad_num} (no footprint)")
                continue
            # a pad "number" can map to several copper elements (e.g. a split
            # thermal/EP pad) — net ALL of them, or they short on the empty net.
            matched = [p for p in fp.Pads() if p.GetNumber() == str(pad_num)]
            if not matched:
                missing_pads.append(f"{ref}.{pad_num} (no pad)")
                continue
            for p in matched:
                p.SetNet(ni)
            assigned += len(matched)

    add_outline(board, board_w, board_h, ox, oy)

    # --- preroutes: designer-authored power copper laid BEFORE the router.
    # A deterministic router cannot neck down, so an IPC-width class makes
    # signal taps on power nets (an FB divider leg, a boot cap, a sense pin)
    # unreachable. The hand layout answer: the designer routes the fat
    # current path; the autorouter then connects the taps at signal width
    # (these nets carry NO width class — netclass_writer exempts them).
    # Spec: "preroutes": [{"net", "width", "layer", "path": [point...],
    #   "vias": [[x,y],...], "via_size": [dia,drill]}] where point is
    #   ["pad", ref, padnum] (pad center after placement) or ["xy", x, y]
    #   (board-relative mm). G4 still polices these nets at the preroute
    #   width via the gate checks.
    preroute_count = 0
    for pr in design.get("preroutes", []):
        ni = get_net(pr["net"])
        layer = getattr(pcbnew, pr.get("layer", "F.Cu").replace(".", "_"))

        def _resolve(pt):
            # ["pad", ref, num, (width)] or ["xy", x, y, (width)] — the
            # optional trailing width applies to the segment STARTING here
            # (neck-down out of a small pin, widen in the open field)
            if pt[0] == "pad":
                fp = placed[pt[1]]
                pad = next(p for p in fp.Pads()
                           if p.GetNumber() == str(pt[2]))
                w = float(pt[3]) if len(pt) > 3 else None
                return pad.GetPosition(), w
            w = float(pt[3]) if len(pt) > 3 else None
            return vec(float(pt[1]) + ox, float(pt[2]) + oy), w

        pts = [_resolve(p) for p in pr["path"]]
        for (a, wa), (b, _) in zip(pts, pts[1:]):
            t = pcbnew.PCB_TRACK(board)
            t.SetStart(a)
            t.SetEnd(b)
            t.SetWidth(mm(wa if wa is not None else pr["width"]))
            t.SetLayer(layer)
            t.SetNet(ni)
            board.Add(t)
            preroute_count += 1
        via_d, via_drill = pr.get("via_size", [1.2, 0.9])
        for vx, vy in pr.get("vias", []):
            v = pcbnew.PCB_VIA(board)
            v.SetPosition(vec(float(vx) + ox, float(vy) + oy))
            v.SetWidth(mm(via_d))
            v.SetDrill(mm(via_drill))
            v.SetViaType(pcbnew.VIATYPE_THROUGH)
            v.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
            v.SetNet(ni)
            board.Add(v)
            preroute_count += 1

    # --- mounting holes (a practical board must be mechanically fixable) ---
    # netlist: "mounting_holes": true  (or {"footprint": ..., "inset": mm}).
    # Four corner NPTH holes, M3 by default. Refs H1-H4.
    mh = design.get("mounting_holes")
    holes_added = []
    if mh:
        spec = mh if isinstance(mh, dict) else {}
        mh_fp = spec.get("footprint", "MountingHole:MountingHole_3.2mm_M3")
        inset = float(spec.get("inset", 4.0))
        # exact spec coordinates (e.g. the RPi HAT pattern) override the
        # uniform corner-inset default
        corners = spec.get("positions") or [
            (inset, inset), (board_w - inset, inset),
            (board_w - inset, board_h - inset), (inset, board_h - inset)]
        for i, (hx, hy) in enumerate(corners, 1):
            fp = load_footprint(fp_dir, mh_fp)
            fp.SetReference(f"H{i}")
            fp.SetPosition(vec(hx + ox, hy + oy))
            board.Add(fp)
            holes_added.append(f"H{i}")

    # --- copper planes (4-layer Sig/GND/PWR/Sig) ---
    # Default: In1.Cu = solid GND plane, In2.Cu = first power rail. Signal layers
    # are left un-poured: pouring GND on the outer layers fragments it into islands
    # that need stitching vias, so for a clean autorouted flow we keep the return
    # current on the inner plane. Override via netlist "planes": {"<layer>":"<net>"}.
    nets_avail = [n for n in design.get("nets", {}) if n != "GND"]
    # prefer a regulated logic rail (+3V3, +5V, +1V8) over a raw input (VBUS/VIN)
    default_power = (next((n for n in nets_avail if n.startswith("+")), None)
                     or next((n for n in nets_avail if "V" in n.upper()), None))
    planes = design.get("planes")
    if planes is None:
        planes = {"In1.Cu": "GND"}
        if default_power:
            planes["In2.Cu"] = default_power
    zones_added = []
    for layer_name, net_name in planes.items():
        if net_name not in netmap:
            continue
        lid = board.GetLayerID(layer_name)
        if lid < 0:
            continue
        add_plane(board, lid, netmap[net_name], board_w, board_h, ox, oy)
        zones_added.append(f"{layer_name}:{net_name}")

    # --- antenna keep-out (no ground/power pour under the radiator) ---
    # Spec in the netlist: "antenna_keepout": {"edge":"top|bottom|left|right",
    # "depth": mm} for an edge strip, or {"rect":[x,y,w,h]} board-relative.
    ako = design.get("antenna_keepout")
    keepout_added = None
    if ako:
        if "rect" in ako:
            kx, ky, kw, kh = ako["rect"]
            kx, ky = ox + kx, oy + ky
        else:
            depth = float(ako.get("depth", 8.0))
            edge = ako.get("edge", "top")
            kx, ky, kw, kh = {
                "top": (ox, oy, board_w, depth),
                "bottom": (ox, oy + board_h - depth, board_w, depth),
                "left": (ox, oy, depth, board_h),
                "right": (ox + board_w - depth, oy, depth, board_h),
            }.get(edge, (ox, oy, board_w, depth))
        if add_keepout(board, kx, ky, kw, kh, ako.get("name", "ANTENNA")):
            keepout_added = ako

    # --- silkscreen hygiene: refs placed ADJACENT to their part (visible
    # after assembly), center-on-body only as the dense fallback, everything
    # clamped inside Edge.Cuts. Shared with the post-route fixer.
    try:
        import silk_place
        silk = silk_place.place_refs(board)
        warnings += [f"silkscreen: {e}" for e in silk.get("errors", [])]
    except Exception as e:
        traceback.print_exc()
        warnings.append(f"silkscreen ref placement failed ({type(e).__name__}: "
                        f"{e}); refs left at their footprint defaults")

    # connectivity rebuild + save
    try:
        board.BuildConnectivity()
    except Exception as e:
        warnings.append(f"BuildConnectivity failed before save "
                        f"({type(e).__name__}: {e}); ratsnest rebuilt on load")
    pcbnew.SaveBoard(out_path, board)

    # Fill zones on a FRESHLY LOADED board — the in-memory board built from
    # CreateEmptyBoard lacks the connectivity the headless ZONE_FILLER needs and
    # segfaults; a reloaded board fills cleanly. An unfilled plane is a board
    # with no ground/power copper, so a fill failure is fatal (the __main__
    # wrapper reports it as ok=false with the trace).
    if zones_added:
        b2 = pcbnew.LoadBoard(out_path)
        b2.BuildConnectivity()
        pcbnew.ZONE_FILLER(b2).Fill(b2.Zones())
        pcbnew.SaveBoard(out_path, b2)

    print(json.dumps({
        "ok": not missing_pads,
        "pcb": out_path,
        "footprints": len(placed),
        "nets": len(netmap),
        "pads_assigned": assigned,
        "missing_pads": missing_pads,
        "zones": zones_added,
        "antenna_keepout": keepout_added,
        "mounting_holes": holes_added,
        "board_mm": [board_w, board_h],
        "warnings": warnings,
    }))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e),
                          "trace": traceback.format_exc()}))
        sys.exit(1)
