#!/usr/bin/env python3
"""Placement engine for the headless board builder — courtyard-aware
shelf packing (FFDH + backfill), functional clustering (support passives
at the IC they serve), connector edge handling (place_conn: connectors on
any board edge), and mixed fixed+auto fields. Pure placement math; the
orchestrator applies the results."""
from __future__ import annotations

import os
import sys
from collections import defaultdict
from collections.abc import Mapping, Sequence

import kicad_quiet  # noqa: F401  (before pcbnew: no modal wx dialogs)
import pcbnew

from place_conn import (DEFAULT_EDGE, anchor_row_limit, band_extents, edge_groups,
                        orient_cluster, place_edge_clusters, resolve_edges, rot_face,
                        transposed)


def _build_courtyard_caches(fp) -> None:
    """KiCad 9.0.4 leaves a footprint's courtyard cache EMPTY after FootprintLoad
    (even at the origin), SetPosition, board.Add and LoadBoard: GetCourtyard()
    has no outline until this runs (10.0 builds it lazily). Without it the
    courtyard reads below silently fell back to the silk+text bounding box.
    Objects without the method (stubs) are left alone."""
    if hasattr(fp, "BuildCourtyardCaches"):
        fp.BuildCourtyardCaches()


def fp_extent(fp):
    """Return (w, h, cx_off, cy_off) in mm for a footprint sitting at the origin:
    its courtyard bounding-box size and the offset of that box's center from the
    footprint anchor (anchors are not always the part center — e.g. pin headers
    anchor on pin 1). Falls back to the pad/graphics bbox if no courtyard."""
    fp.SetPosition(pcbnew.VECTOR2I(0, 0))
    _build_courtyard_caches(fp)
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
    (e.g. a symmetric vertical pin header, which has no real mating direction,
    or a footprint without a front courtyard)."""
    fp.SetPosition(pcbnew.VECTOR2I(0, 0))
    _build_courtyard_caches(fp)
    pads = list(fp.Pads())
    if not pads:
        return (0.0, 0.0)
    to = pcbnew.ToMM
    pcx = sum(to(p.GetPosition().x) for p in pads) / len(pads)
    pcy = sum(to(p.GetPosition().y) for p in pads) / len(pads)
    try:
        cy = fp.GetCourtyard(pcbnew.F_CrtYd)
        if not cy or cy.OutlineCount() == 0:
            return (0.0, 0.0)          # an empty set's bbox would sit at the origin
        bb = cy.BBox()
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


def _roles(design: dict, pincount: Mapping[str, int]) -> tuple[list, list, list]:
    """(anchors, connectors, satellites), each in design order.

    edge_row: a component the netlist pins to a board edge alongside the
    connectors (e.g. an RF module whose antenna must sit at/over the edge) — a
    connector here. interior: a J* part that does NOT mate through the board
    edge (a config jumper, a Kelvin header) — packs as an ordinary satellite
    instead of spending a connector slot on it."""
    refs = [c["ref"] for c in design["components"]]
    edge_set = {c["ref"] for c in design["components"] if c.get("edge_row")}
    interior = {c["ref"] for c in design["components"] if c.get("interior")}

    def is_conn(r: str) -> bool:
        return (r[:1] == "J" or r in edge_set) and r not in interior

    def is_anchor(r: str) -> bool:
        # multi-pin actives anchor a cluster. Crystals (Y) are deliberately NOT
        # anchors — they must travel WITH the MCU and their load caps, so they
        # attach as satellites of whatever IC their OSC pins connect to.
        return (not is_conn(r)) and pincount.get(r, 0) > 4

    anchors = [r for r in refs if is_anchor(r)]
    connectors = [r for r in refs if is_conn(r)]
    satellites = [r for r in refs if r not in anchors and r not in connectors]
    if not anchors and not connectors:
        # nothing to cluster around (an all-passive auto field, e.g. the back
        # side of a mixed board whose ICs are fixed): every part is its own
        # block in the shelf-packed field instead of a satellite of a head
        # that does not exist
        anchors, satellites = satellites, []
    return anchors, connectors, satellites


def _associate(s: str, net_refs: Mapping[str, list], fanout: Mapping[str, int],
               ref_nets: Mapping[str, set], head_pull: set, heads: set) -> str | None:
    """The head satellite `s` clusters at, or None (the caller round-robins)."""
    # 1) direct attach through a SPECIFIC net (low fanout): the FB divider
    #    at its buck, the load caps at their crystal. A rail net (GND,
    #    3V3) is NOT specific — any anchor it names is coincidence, so
    #    those only count in step 3 when nothing better exists.
    lo = [n for n in sorted(ref_nets.get(s, ()), key=lambda n: fanout.get(n, 99))
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
            for n in sorted(ref_nets.get(cur, ()), key=lambda n: fanout.get(n, 99)):
                if fanout.get(n, 0) > 10:
                    continue
                for r in net_refs[n]:
                    if r in seen:
                        continue
                    if r in heads:
                        return r
                    seen.add(r)
                    nxt.append(r)
        frontier = nxt
    # 3) last resort: a rail net direct attach (a 3V3-only bypass lands on
    #    a 3V3 anchor; pure-GND parts on any). Rails sorted by fanout so
    #    the most specific rail wins; None -> caller round-robins.
    for n in sorted(ref_nets.get(s, ()), key=lambda n: fanout.get(n, 99)):
        near = [r for r in net_refs[n] if r != s and r in head_pull]
        if near:
            return near[0]
    return None


def _members(design: dict, pincount: Mapping[str, int], anchors: list,
             connectors: list, satellites: list) -> dict[str, list]:
    """{head: [head, its satellites...]} for every anchor and connector."""
    net_refs = {n: [r for r, _ in conns] for n, conns in design.get("nets", {}).items()}
    fanout = {n: len(rs) for n, rs in net_refs.items()}
    ref_nets = defaultdict(set)
    for n, rs in net_refs.items():
        for r in rs:
            ref_nets[r].add(n)
    aset, cset = set(anchors), set(connectors)
    # satellites may cluster at an anchor OR at a multi-pin edge-row head (a
    # USB-C's CC pulldowns belong at the USB-C; an RF module's decoupling
    # belongs at the module, not at whatever IC shares its rail)
    head_pull = aset | {r for r in cset if pincount.get(r, 0) > 4}
    assoc = {s: _associate(s, net_refs, fanout, ref_nets, head_pull, aset | cset)
             for s in satellites}
    orphans = [s for s in satellites if assoc[s] is None]
    if anchors:                              # power-only caps -> spread over ICs
        assoc = {**assoc, **{s: anchors[i % len(anchors)] for i, s in enumerate(orphans)}}
    members = {h: [h] for h in anchors + connectors}
    for s in satellites:
        members[assoc[s] or (anchors[0] if anchors else connectors[0])].append(s)
    return members


def _side_columns(sats: Sequence[str], size: Mapping[str, tuple], hh: float,
                  sat_gap: float) -> tuple[list, list, list, float, float]:
    """Split satellites into the columns left / right of a head of height hh
    and the overflow below it: (left, right, below, left_h, right_h)."""
    left, right, below = [], [], []
    lh = rh = 0.0
    for s in sats:
        sh = size[s][1]
        if lh + sh + sat_gap <= hh and lh <= rh:
            left.append(s)
            lh += sh + sat_gap
        elif rh + sh + sat_gap <= hh:
            right.append(s)
            rh += sh + sat_gap
        else:
            below.append(s)
    return left, right, below, lh, rh


def _layout_cluster(head: str, sats: Sequence[str], size: Mapping[str, tuple],
                    hsize: tuple, sat_gap: float, head_gap: float) -> tuple[dict, float, float]:
    """Lay out one cluster the way a hand layout does: satellites WRAP AROUND
    the head — side columns first (decoupling 1-2 mm from the pins, per every
    placement guide), overflow in tight shelf rows below. The head sits at
    y = 0 (the top-edge orientation). Returns ({ref: bbox-centre}, w, h)."""
    sats = sorted(sats, key=lambda s: -(size[s][0] * size[s][1]))
    hw, hh = hsize
    if not sats:
        return {head: (hw / 2, hh / 2)}, hw, hh
    left, right, below, lh, rh = _side_columns(sats, size, hh, sat_gap)
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
    for side, x_of in ((left, lambda sw: hx - head_gap - sw / 2),
                       (right, lambda sw: hx + hw + head_gap + sw / 2)):
        y = 0.0
        for s in side:
            sw, sh = size[s]
            local[s] = (x_of(sw), y + sh / 2)
            y += sh + sat_gap
    sx0 = (cw - sbw) / 2
    for s, (px, py) in satpos.items():
        local[s] = (sx0 + px, max(hh, lh, rh) + head_gap + py)
    return local, cw, ch


def _head_cluster(head: str, sats: Sequence[str], size: Mapping[str, tuple], hsize: tuple,
                  edge: str, sat_gap: float, head_gap: float) -> tuple[dict, float, float]:
    """`head`'s cluster facing its board edge (anchors use the top layout):
    left/right clusters are laid out on transposed sizes, then turned."""
    if transposed(edge):
        size = {s: size[s][::-1] for s in sats}
        hsize = hsize[::-1]
    return orient_cluster(_layout_cluster(head, sats, size, hsize, sat_gap, head_gap), edge)


def _debug_clusters(heads: Sequence[str], clusters: Mapping[str, tuple],
                    members: Mapping[str, list]) -> None:
    """PACK_DEBUG=1 (a diagnostic only): one stderr line per cluster."""
    if not os.environ.get("PACK_DEBUG"):
        return
    for h in heads:
        _, cw, ch = clusters[h]
        sys.stderr.write(f"cluster {h}: {cw:.1f} x {ch:.1f} "
                         f"({len(members[h]) - 1} sats) "
                         f"{[m for m in members[h] if m != h]}\n")


def _arrange(anchors: Sequence[str], connectors: Sequence[str], edge_of: Mapping[str, str],
             clusters: Mapping[str, tuple], margin: float, width_limit: float) -> dict:
    """Connector rows/columns on their edges (place_conn) around the anchor
    clusters, which are shelf-packed in the core: block-to-block gap is one
    routing channel (the pack_scale margin) and rows fill the width the
    connector rows set, or `width_limit` (a fixed board's free width) when that
    is wider — on a mixed-placement board the big connectors are FIXED parts, so
    the auto row may be one tiny jumper. Returns {ref: bbox-centre}."""
    groups = edge_groups(connectors, edge_of)
    bands = band_extents(groups, clusters, margin)
    anchor_sizes = [(a, clusters[a][1], clusters[a][2]) for a in anchors]
    apos, abw, abh = (shelf_pack(anchor_sizes, margin,
                                 row_limit=anchor_row_limit(groups, bands, width_limit, margin))
                      if anchors else ({}, 0.0, 0.0))
    pos, (x0, y0, core_w) = place_edge_clusters(groups, clusters, bands, (abw, abh), margin)
    cxoff = x0 + (core_w - abw) / 2
    for a in anchors:
        local, cw, ch = clusters[a]
        bcx, bcy = apos[a]           # the cluster bbox-center; convert to its top-left
        tlx, tly = cxoff + bcx - cw / 2, y0 + bcy - ch / 2
        pos.update({r: (tlx + lx, tly + ly) for r, (lx, ly) in local.items()})
    return pos


def _normalise(pos: dict, rsize: Mapping[str, tuple],
               refs: Sequence[str]) -> tuple[dict, float, float]:
    """Shift so the leftmost/topmost bbox edge sits at the origin and report the
    true bounding extent — using ROTATED extents so a turned connector's real
    footprint is what the board is sized to (positions are bbox-centers)."""
    minx = min(pos[r][0] - rsize[r][0] / 2 for r in refs)
    miny = min(pos[r][1] - rsize[r][1] / 2 for r in refs)
    pos = {r: (round(px - minx, 3), round(py - miny, 3)) for r, (px, py) in pos.items()}
    used_w = round(max(pos[r][0] + rsize[r][0] / 2 for r in refs), 2)
    used_h = round(max(pos[r][1] + rsize[r][1] / 2 for r in refs), 2)
    return pos, used_w, used_h


def functional_place(design: dict, extents: Mapping[str, tuple],
                     pincount: Mapping[str, int], mouth: Mapping[str, tuple] | None = None,
                     margin: float = 1.5, width_limit: float = 0.0,
                     conn_edges: Mapping[str, str] | None = None
                     ) -> tuple[dict, dict, float, float]:
    """Functional placement from the net map: every support passive is clustered
    next to the IC it serves (decoupling at the power pins, load caps with their
    crystal, series-R at its LED, pulls at their IC) and connectors go to the
    board edges. Returns ({ref: (bbox_cx, bbox_cy)}, {connector: rot}, w, h).
    An *anchor* is a multi-pin part (pincount > 4); a *connector* is a J* part
    that is not `interior`, or any `edge_row` part; the rest are *satellites*,
    each attached to the head it shares its most specific (lowest-fanout) net
    with (_associate); power-only caps round-robin over the anchors.

    conn_edges: optional {ref: 'top'|'bottom'|'left'|'right'}, the board edge each
    connector mates through (the builder passes the component key "edge").
    edge_row parts count as connectors, so an edge_row RF module with "edge":
    "right" lands on the right edge. Unnamed connectors stay on the top edge:
    None, {} or all-'top' reproduce the original top connector row bit for bit.
    Top/bottom connectors form rows along x, left/right ones columns along y;
    each is turned (rot_face) so its mouth points along its edge's outward
    normal, satellites inward. Rotations are for the front side (a back-side
    part's left-right flip mirrors a left/right mouth; the caller compensates).
    Entries naming parts outside `design` are ignored (one mapping serves the F
    and B halves); an unknown edge or a non-connector part raises ValueError."""
    refs = [c["ref"] for c in design["components"]]
    size = {r: (extents[r][0], extents[r][1]) for r in refs}
    anchors, connectors, satellites = _roles(design, pincount)
    members = _members(design, pincount, anchors, connectors, satellites)
    edge_of = resolve_edges(conn_edges, connectors, set(refs))
    # Every connector's mating mouth faces OFF its edge (an SMA / USB-C / terminal
    # hangs off the edge facing outward, never points into the interior). rsize
    # is the extent AFTER that rotation (90/270 swap w/h).
    mouth = mouth or {}
    rots = {c: rot_face(mouth.get(c, (0, 0)), edge_of[c]) for c in connectors}
    rsize = {r: ((size[r][1], size[r][0]) if rots.get(r, 0) in (90, 270)
                 else size[r]) for r in refs}
    # Gaps are assembly-class: sat_gap between passives (courtyards already
    # carry clearance), head_gap as the via-fanout ring next to the IC. Only
    # head_gap grows with the pack_scale `margin`.
    sat_gap, head_gap = 0.5, max(0.9, 0.6 * margin)
    heads = anchors + connectors
    clusters = {h: _head_cluster(h, [m for m in members[h] if m != h], size, rsize[h],
                                 edge_of.get(h, DEFAULT_EDGE), sat_gap, head_gap)
                for h in heads}
    _debug_clusters(heads, clusters, members)
    pos = _arrange(anchors, connectors, edge_of, clusters, margin, width_limit)
    pos, used_w, used_h = _normalise(pos, rsize, refs)
    return pos, rots, used_w, used_h
