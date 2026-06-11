#!/usr/bin/env python3
"""Placement engine for the headless board builder — courtyard-aware
shelf packing (FFDH + backfill), functional clustering (support passives
at the IC they serve), connector edge handling, and mixed fixed+auto
fields. Pure placement math; the orchestrator applies the results."""
import os

import pcbnew


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


