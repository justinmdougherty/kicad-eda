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


def fp_extent(fp):
    """Return (w, h, cx_off, cy_off) in mm for a footprint sitting at the origin:
    its courtyard bounding-box size and the offset of that box's center from the
    footprint anchor (anchors are not always the part center — e.g. pin headers
    anchor on pin 1). Falls back to the pad/graphics bbox if no courtyard."""
    fp.SetPosition(pcbnew.VECTOR2I(0, 0))
    bb = None
    for layer in (pcbnew.F_CrtYd, pcbnew.B_CrtYd):
        try:
            cy = fp.GetCourtyard(layer)
            if cy and cy.OutlineCount() > 0:
                bb = cy.BBox()
                break
        except Exception:
            pass
    if bb is None or bb.GetWidth() == 0:
        bb = fp.GetBoundingBox()
    to = pcbnew.ToMM
    return (to(bb.GetWidth()), to(bb.GetHeight()),
            to(bb.GetCenter().x), to(bb.GetCenter().y))


def fp_mouth(fp):
    """Approx direction a connector's opening faces, in its rot-0 local frame:
    away from the pad centroid toward the open side of the courtyard, snapped to
    the dominant axis. KiCad +Y is down. Returns (mx, my); (0,0) if indeterminate
    (e.g. a symmetric vertical pin header, which has no real mating direction)."""
    fp.SetPosition(pcbnew.VECTOR2I(0, 0))
    pads = list(fp.Pads())
    if not pads:
        return (0.0, 0.0)
    to = pcbnew.ToMM
    pcx = sum(to(p.GetPosition().x) for p in pads) / len(pads)
    pcy = sum(to(p.GetPosition().y) for p in pads) / len(pads)
    try:
        bb = fp.GetCourtyard(pcbnew.F_CrtYd).BBox()
        ccx, ccy = to(bb.GetCenter().x), to(bb.GetCenter().y)
    except Exception:
        return (0.0, 0.0)
    dx, dy = ccx - pcx, ccy - pcy
    if abs(dx) >= abs(dy):
        return (1.0 if dx > 0 else -1.0, 0.0) if abs(dx) > 0.4 else (0.0, 0.0)
    return (0.0, 1.0 if dy > 0 else -1.0) if abs(dy) > 0.4 else (0.0, 0.0)


def shelf_pack(sizes, margin=1.2, aspect=1.3, row_limit=0.0):
    """Shelf-pack parts into rows (first-fit decreasing height). `sizes` =
    [(ref, w, h)]. Returns {ref: (bbox_cx, bbox_cy)} target bbox-center
    positions and the (total_w, total_h) extent. Tallest-first reduces wasted
    row height; each item BACKFILLS the first earlier row with width to spare
    (a short item after a tall row would otherwise open a new row and leave
    the tall row half empty). `row_limit` overrides the aspect-derived row
    width (e.g. to fill the board width set by a connector row instead of
    leaving an empty half-board)."""
    items = sorted(sizes, key=lambda s: -s[2])
    area = sum((w + margin) * (h + margin) for _, w, h in items) or 1.0
    row_limit = row_limit or (area * aspect) ** 0.5
    # extents are CONTENT-true: rows start at 0 and only inter-item gaps
    # count, so the returned size compares directly against a free field
    # (the caller owns any outer margin / board edge clearance).
    pos, max_x = {}, 0.0
    rows = []  # [x_cursor, y_top, height]
    for ref, w, h in items:
        row = next((r for r in rows if r[0] + w <= row_limit and h <= r[2]),
                   None)
        if row is None:
            y = rows[-1][1] + rows[-1][2] + margin if rows else 0.0
            row = [0.0, y, h]
            rows.append(row)
        pos[ref] = (round(row[0] + w / 2, 3), round(row[1] + h / 2, 3))
        max_x = max(max_x, row[0] + w)
        row[0] += w + margin
    total_h = rows[-1][1] + rows[-1][2] if rows else 0.0
    return pos, round(max_x, 2), round(total_h, 2)


def functional_place(design, extents, pincount, mouth=None, margin=1.5,
                     width_limit=0.0):
    """Functional placement from the net map: every support passive is clustered
    immediately next to the IC / crystal it serves (decoupling at the power pins,
    crystal load caps at the crystal, series-R at its LED, pulls at their IC), and
    connectors are pushed to the top edge. Returns ({ref:(bbox_cx,bbox_cy)},
    total_w, total_h) — same contract as shelf_pack so the caller is unchanged.

    Heuristics: an *anchor* is a multi-pin part (pincount>4) or a crystal (Y*); a
    *connector* is J*; everything else is a *satellite*. Each satellite attaches to
    the anchor it shares its most-specific (lowest-fanout) net with, following
    satellite->satellite chains (e.g. R->LED->GPIO) until an anchor/connector is
    reached; power-only caps with no specific link round-robin onto the anchors so
    decoupling spreads across the ICs instead of piling up."""
    from collections import defaultdict

    refs = [c["ref"] for c in design["components"]]
    size = {r: (extents[r][0], extents[r][1]) for r in refs}
    nets = design.get("nets", {})
    net_refs = {n: [r for r, _ in conns] for n, conns in nets.items()}
    fanout = {n: len(rs) for n, rs in net_refs.items()}
    ref_nets = defaultdict(set)
    for n, rs in net_refs.items():
        for r in rs:
            ref_nets[r].add(n)

    # edge_row: a component the netlist pins to the top board edge alongside the
    # connectors (e.g. an RF module whose antenna must sit at/over the edge).
    # interior: a J* part that does NOT mate through the board edge (a config
    # jumper, a Kelvin header) — packs as an ordinary satellite instead of
    # spending a connector row on it.
    edge_set = {c["ref"] for c in design["components"] if c.get("edge_row")}
    interior = {c["ref"] for c in design["components"] if c.get("interior")}

    def is_conn(r):
        return (r[:1] == "J" or r in edge_set) and r not in interior

    def is_anchor(r):
        # multi-pin actives anchor a cluster. Crystals (Y) are deliberately NOT
        # anchors — they must travel WITH the MCU and their load caps, so they
        # attach as satellites of whatever IC their OSC pins connect to.
        return (not is_conn(r)) and pincount.get(r, 0) > 4

    anchors = [r for r in refs if is_anchor(r)]
    connectors = [r for r in refs if is_conn(r)]
    satellites = [r for r in refs if r not in anchors and r not in connectors]
    aset, cset = set(anchors), set(connectors)

    # satellites may cluster at an anchor OR at a multi-pin edge-row head (a
    # USB-C's CC pulldowns belong at the USB-C; an RF module's decoupling
    # belongs at the module, not at whatever IC shares its rail)
    head_pull = aset | {r for r in cset if pincount.get(r, 0) > 4}

    def associate(s):
        # 1) direct attach through a SPECIFIC net (low fanout): the FB divider
        #    at its buck, the load caps at their crystal. A rail net (GND,
        #    3V3) is NOT specific — any anchor it names is coincidence, so
        #    those only count in step 3 when nothing better exists.
        lo = [n for n in sorted(ref_nets[s], key=lambda n: fanout.get(n, 99))
              if fanout.get(n, 0) <= 10]
        for n in lo:
            near = [r for r in net_refs[n] if r != s and r in head_pull]
            if near:
                return near[0]
        # 2) chain search through LOW-fanout nets only (cap->inductor->buck,
        #    R->LED->GPIO). Chaining through GND would attach a rail cap to
        #    whatever anchor happens to share its ground.
        seen, frontier = {s}, [s]
        while frontier:
            nxt = []
            for cur in frontier:
                for n in sorted(ref_nets[cur], key=lambda n: fanout.get(n, 99)):
                    if fanout.get(n, 0) > 10:
                        continue
                    for r in net_refs[n]:
                        if r in seen:
                            continue
                        if r in aset or r in cset:
                            return r
                        seen.add(r)
                        nxt.append(r)
            frontier = nxt
        # 3) last resort: a rail net direct attach (a 3V3-only bypass lands on
        #    a 3V3 anchor; pure-GND parts on any). Rails sorted by fanout so
        #    the most specific rail wins; None -> caller round-robins.
        for n in sorted(ref_nets[s], key=lambda n: fanout.get(n, 99)):
            near = [r for r in net_refs[n] if r != s and r in head_pull]
            if near:
                return near[0]
        return None

    assoc = {s: associate(s) for s in satellites}
    rr = 0
    for s in satellites:                      # power-only caps -> spread over ICs
        if assoc[s] is None and anchors:
            assoc[s] = anchors[rr % len(anchors)]
            rr += 1

    members = defaultdict(list)
    heads = anchors + connectors
    for h in heads:
        members[h] = [h]
    for s in satellites:
        t = assoc[s] or (anchors[0] if anchors else connectors[0])
        members[t].append(s)

    # Orient every connector so its mating mouth faces OFF the top board edge (-Y)
    # — an edge-launch part (SMA, USB-C, barrel/terminal) must hang off the edge
    # facing outward, never sit in the interior pointing in. Rotation is chosen by
    # the mouth direction; a mouthless vertical header stays at 0. rsize is the
    # footprint extent AFTER that rotation (90/270 swap w/h), so the cluster and
    # board sizing account for the turned part.
    mouth = mouth or {}

    def _rot_face_up(m):
        # pick the rotation whose mouth ends up pointing off the top edge (0,-1).
        # KiCad rotation is clockwise-positive (Y-down): R(a)*(mx,my) =
        # (mx cos a + my sin a, -mx sin a + my cos a); dot with (0,-1) below.
        mx, my = m
        if abs(mx) < 0.4 and abs(my) < 0.4:
            return 0
        dots = {0: -my, 90: mx, 180: my, 270: -mx}
        return max(dots, key=dots.get)

    rots = {c: _rot_face_up(mouth.get(c, (0, 0))) for c in connectors}
    rsize = {r: ((size[r][1], size[r][0]) if rots.get(r, 0) in (90, 270)
                 else size[r]) for r in refs}

    # Lay out one cluster the way a hand layout does: satellites WRAP AROUND
    # the IC — side columns first (decoupling 1-2 mm from the pins, per every
    # placement guide), overflow in tight shelf rows below. Gaps are assembly-
    # class: sat_gap between passives (courtyards already carry clearance),
    # head_gap as the via-fanout ring next to the IC. Only head_gap grows with
    # the pack_scale `margin` — passive spacing never needs to balloon.
    sat_gap = 0.5
    head_gap = max(0.9, 0.6 * margin)

    def layout_cluster(head):
        sats = sorted((m for m in members[head] if m != head),
                      key=lambda s: -(size[s][0] * size[s][1]))
        hw, hh = rsize[head]
        if not sats:
            return {head: (hw / 2, hh / 2)}, hw, hh
        left, right, below = [], [], []
        lh = rh = 0.0
        for s in sats:
            sw, sh = size[s]
            if lh + sh + sat_gap <= hh and lh <= rh:
                left.append(s)
                lh += sh + sat_gap
            elif rh + sh + sat_gap <= hh:
                right.append(s)
                rh += sh + sat_gap
            else:
                below.append(s)
        lw = max((size[s][0] for s in left), default=0.0)
        rw = max((size[s][0] for s in right), default=0.0)
        core_w = (lw + head_gap if left else 0) + hw + (head_gap + rw if right else 0)
        if below:
            # row width: at least the core, but aspect-aware — big satellites
            # (bulk caps, shunts) below a tiny head (an SO-8 FET) must spread
            # into square-ish rows, not stack into a tower the field can't fit
            below_area = sum(size[s][0] * size[s][1] for s in below)
            row_w = max(core_w, 4.0, (below_area * 2.2) ** 0.5)
            satpos, sbw, sbh = shelf_pack([(s, *size[s]) for s in below],
                                          sat_gap, row_limit=row_w)
        else:
            satpos, sbw, sbh = {}, 0.0, 0.0
        cw = max(core_w, sbw)
        ch = max(hh, lh, rh) + (head_gap + sbh if below else 0.0)
        hx = (cw - core_w) / 2 + (lw + head_gap if left else 0)  # head left edge
        local = {head: (hx + hw / 2, hh / 2)}
        y = 0.0
        for s in left:
            sw, sh = size[s]
            local[s] = (hx - head_gap - sw / 2, y + sh / 2)
            y += sh + sat_gap
        y = 0.0
        for s in right:
            sw, sh = size[s]
            local[s] = (hx + hw + head_gap + sw / 2, y + sh / 2)
            y += sh + sat_gap
        sx0 = (cw - sbw) / 2
        for s, (px, py) in satpos.items():
            local[s] = (sx0 + px, max(hh, lh, rh) + head_gap + py)
        return local, cw, ch

    clusters = {h: layout_cluster(h) for h in heads}
    if os.environ.get("PACK_DEBUG"):
        for h in heads:
            _, cw, ch = clusters[h]
            sys.stderr.write(f"cluster {h}: {cw:.1f} x {ch:.1f} "
                             f"({len(members[h]) - 1} sats) "
                             f"{[m for m in members[h] if m != h]}\n")

    # connectors in a row along the top board edge (each already rotated above so
    # its mouth faces off that edge); anchor clusters packed below.
    pos = {}
    conn_w = sum(clusters[c][1] + margin for c in connectors)
    cx = 0.0
    conn_row_h = max([clusters[c][2] for c in connectors], default=0.0)
    for c in connectors:
        local, cw, ch = clusters[c]
        for r, (lx, ly) in local.items():
            pos[r] = (cx + lx, ly)
        cx += cw + margin

    # anchor field: block-to-block gap is one routing channel (scaled by the
    # pack_scale margin), and rows fill the width the connector row already
    # set — a board should be as wide as its connectors and no emptier below.
    # `width_limit` (a fixed-board free width) overrides a narrow connector
    # row: on a mixed-placement board the big connectors are FIXED parts, so
    # conn_w here may be one tiny jumper — packing 60 parts to that width
    # would produce a one-column tower that fits no board.
    anchor_sizes = [(a, clusters[a][1], clusters[a][2]) for a in anchors]
    apos, abw, abh = (shelf_pack(anchor_sizes, margin,
                                 row_limit=max(conn_w, width_limit))
                      if anchors else ({}, 0.0, 0.0))
    y_off = conn_row_h + margin if connectors else 0.0
    total_w = max(conn_w, abw)
    cxoff = (total_w - abw) / 2
    for a in anchors:
        local, cw, ch = clusters[a]
        # apos gives the cluster bbox-center; convert to its top-left
        bcx, bcy = apos[a]
        tlx, tly = cxoff + bcx - cw / 2, y_off + bcy - ch / 2
        for r, (lx, ly) in local.items():
            pos[r] = (tlx + lx, tly + ly)

    # normalise so the leftmost/topmost bbox edge sits at the origin, then report
    # the true bounding extent — using ROTATED extents so a turned connector's
    # real footprint is what the board is sized to (positions are bbox-centers).
    minx = min(pos[r][0] - rsize[r][0] / 2 for r in refs)
    miny = min(pos[r][1] - rsize[r][1] / 2 for r in refs)
    pos = {r: (round(px - minx, 3), round(py - miny, 3)) for r, (px, py) in pos.items()}
    used_w = round(max(pos[r][0] + rsize[r][0] / 2 for r in refs), 2)
    used_h = round(max(pos[r][1] + rsize[r][1] / 2 for r in refs), 2)
    return pos, rots, used_w, used_h


def add_keepout(board, x0, y0, w, h, name="ANTENNA"):
    """Add an antenna keep-out rule area over the rectangle (all copper layers):
    no copper pour and no vias, so the ground/power planes clip AROUND it and the
    radiator sits over bare substrate (the #1 PCB-antenna layout rule — a ground
    plane under the antenna detunes and shorts the near field). Tracks are still
    allowed so the antenna/feed conductor itself can live in the region."""
    zone = pcbnew.ZONE(board)
    try:
        zone.SetIsRuleArea(True)
        zone.SetDoNotAllowZoneFills(True)   # KiCad 10 name for "no copper pour"
        zone.SetDoNotAllowVias(True)
        zone.SetDoNotAllowTracks(False)
        try:
            zone.SetDoNotAllowPads(True)
        except Exception:
            pass
    except Exception:
        return None
    ls = pcbnew.LSET()
    for lid in (pcbnew.F_Cu, pcbnew.In1_Cu, pcbnew.In2_Cu, pcbnew.B_Cu):
        ls.AddLayer(lid)
    zone.SetLayerSet(ls)
    outline = zone.Outline()
    outline.NewOutline()
    for cx, cy in ((x0, y0), (x0 + w, y0), (x0 + w, y0 + h), (x0, y0 + h)):
        outline.Append(mm(cx), mm(cy))
    zone.SetZoneName(name)
    board.Add(zone)
    return zone


def add_outline(board, w, h, x0=0.0, y0=0.0):
    pts = [(x0, y0), (x0 + w, y0), (x0 + w, y0 + h), (x0, y0 + h), (x0, y0)]
    for (ax, ay), (bx, by) in zip(pts, pts[1:]):
        seg = pcbnew.PCB_SHAPE(board)
        seg.SetShape(pcbnew.SHAPE_T_SEGMENT)
        seg.SetStart(vec(ax, ay))
        seg.SetEnd(vec(bx, by))
        seg.SetLayer(pcbnew.Edge_Cuts)
        seg.SetWidth(mm(0.1))
        board.Add(seg)


def add_plane(board, layer_id, ni, w, h, x0, y0, inset=0.3):
    """Add a rectangular copper pour on `layer_id` tied to net `ni`, inset from
    the board edge by `inset` mm (keeps copper off the edge for clean milling)."""
    zone = pcbnew.ZONE(board)
    zone.SetLayer(layer_id)
    zone.SetNetCode(ni.GetNetCode())
    zone.SetIsFilled(False)
    zone.SetAssignedPriority(0)
    # pad-connection: thermal reliefs by default give better hand-solder/escape,
    # but for solid planes we want full (solid) connections to vias.
    try:
        zone.SetPadConnection(pcbnew.ZONE_CONNECTION_FULL)
    except Exception:
        pass
    corners = [(x0 + inset, y0 + inset), (x0 + w - inset, y0 + inset),
               (x0 + w - inset, y0 + h - inset), (x0 + inset, y0 + h - inset)]
    outline = zone.Outline()
    outline.NewOutline()
    for cx, cy in corners:
        outline.Append(mm(cx), mm(cy))
    board.Add(zone)
    return zone


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

    # JLCPCB-class design rules (so default-rule DRC reflects the real fab).
    try:
        ds = board.GetDesignSettings()
        rules_mm = {"m_MinClearance": 0.15, "m_TrackMinWidth": 0.15,
                    "m_ViasMinSize": 0.45, "m_ViasMinDrill": 0.2,
                    "m_MinThroughDrill": 0.2, "m_MicroViasMinSize": 0.2,
                    "m_MicroViasMinDrill": 0.15, "m_CopperEdgeClearance": 0.3}
        for attr, val in rules_mm.items():
            if hasattr(ds, attr):
                setattr(ds, attr, pcbnew.FromMM(val))
    except Exception:
        pass

    SHEET_W, SHEET_H = 297.0, 210.0
    try:
        ps = board.GetPageSettings()
        ps.SetType("A4")
        board.SetPageSettings(ps)
    except Exception:
        pass

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
        silk_place.place_refs(board)
    except Exception:
        traceback.print_exc()

    # connectivity rebuild + save
    try:
        board.BuildConnectivity()
    except Exception:
        pass
    pcbnew.SaveBoard(out_path, board)

    # Fill zones on a FRESHLY LOADED board — the in-memory board built from
    # CreateEmptyBoard lacks the connectivity the headless ZONE_FILLER needs and
    # segfaults; a reloaded board fills cleanly.
    if zones_added:
        try:
            b2 = pcbnew.LoadBoard(out_path)
            b2.BuildConnectivity()
            pcbnew.ZONE_FILLER(b2).Fill(b2.Zones())
            pcbnew.SaveBoard(out_path, b2)
        except Exception:
            pass

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
    }))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e),
                          "trace": traceback.format_exc()}))
        sys.exit(1)
