#!/usr/bin/env python3
"""Board-side gate checks — runs under KiCad's bundled Python (needs pcbnew).

Two modes, both emit JSON {"issues": [...]} on stdout:

  placement <pcb> [netlist.json]
      Pre-route placement-review checks (the G3 gate):
      - every connector's mating mouth faces OFF the board (not into the
        interior) and sits at the edge it faces (<= 3 mm); a J* part the
        netlist flags "interior": true (jumper, Kelvin header) is exempt
      - if the netlist requests an antenna_keepout, a no-fill rule area exists
  routed <pcb> [netlist.json]
      Post-route layout-review checks (part of the G4 gate):
      - every net bound to a net class is routed at >= the class track width
        (verifies the IPC-2221/2141A rules engine wasn't silently ignored)

These complement (not replace) kicad-cli DRC: DRC checks geometry against
rules; these check the *design intent* a DRC can't see.
"""
import json
import sys

import kicad_quiet  # noqa: F401  (before pcbnew: no modal wx dialogs)
import pcbnew


def _world_mouth(fp):
    """Connector mouth direction in the WORLD frame for a placed footprint:
    (courtyard center - pad centroid), snapped to the dominant axis. Returns
    ((mx,my), courtyard_bbox) or None when indeterminate (vertical headers)."""
    pads = list(fp.Pads())
    if not pads:
        return None
    to = pcbnew.ToMM
    pcx = sum(to(p.GetPosition().x) for p in pads) / len(pads)
    pcy = sum(to(p.GetPosition().y) for p in pads) / len(pads)
    try:
        cy = fp.GetCourtyard(pcbnew.F_CrtYd)
        if not cy or cy.OutlineCount() == 0:
            return None
        bb = cy.BBox()
    except Exception:
        return None
    dx = to(bb.GetCenter().x) - pcx
    dy = to(bb.GetCenter().y) - pcy
    if abs(dx) < 0.4 and abs(dy) < 0.4:
        return None
    if abs(dx) >= abs(dy):
        return ((1.0 if dx > 0 else -1.0, 0.0), bb)
    return ((0.0, 1.0 if dy > 0 else -1.0), bb)


def interior_refs(design: dict) -> set:
    """Refs the netlist flags ``"interior": true``: J* parts that do NOT mate
    through the board edge (a config jumper, a Kelvin header, a pin-socket
    wall). place_engine already packs them as ordinary parts; the edge rule
    must not report them either. Pure (no pcbnew)."""
    return {c["ref"] for c in design.get("components", []) if c.get("interior")}


def is_edge_connector(ref: str, interior: set) -> bool:
    """True for a part the connector-at-edge rule applies to: a J* reference
    not flagged interior. Pure (no pcbnew)."""
    return ref.startswith("J") and ref not in interior


def check_placement(board, design, routed=False):
    """routed=True re-validates a cached ROUTED board: plane integrity is
    skipped — it is a PRE-route check (its premise is "the router will trust
    the DSN plane rectangle and return the net broken"); on a routed board
    the plane is legitimately carved by via antipads and any stranded
    islands are already tied by healed tracks, proven by DRC unconnected=0
    at G4. Every other placement check still applies to the artifact."""
    issues = []
    to = pcbnew.ToMM
    bb = board.GetBoardEdgesBoundingBox()
    bx0, by0 = to(bb.GetLeft()), to(bb.GetTop())
    bx1, by1 = to(bb.GetRight()), to(bb.GetBottom())
    bcx, bcy = (bx0 + bx1) / 2, (by0 + by1) / 2

    interior = interior_refs(design)
    for fp in board.GetFootprints():
        ref = fp.GetReference()
        if not is_edge_connector(ref, interior):
            continue
        m = _world_mouth(fp)
        if m is None:
            continue                       # vertical header — no mating direction
        (mx, my), cybb = m
        ccx, ccy = to(cybb.GetCenter().x), to(cybb.GetCenter().y)
        if mx * (ccx - bcx) + my * (ccy - bcy) <= 0:
            issues.append(f"{ref}: mating face points INTO the board interior "
                          f"(unusable in an enclosure)")
            continue
        if mx > 0:
            dist = bx1 - to(cybb.GetRight())
        elif mx < 0:
            dist = to(cybb.GetLeft()) - bx0
        elif my > 0:
            dist = by1 - to(cybb.GetBottom())
        else:
            dist = to(cybb.GetTop()) - by0
        if dist > 3.0:
            issues.append(f"{ref}: mating face is {dist:.1f} mm from the board "
                          f"edge it faces (must be at the edge, <=3 mm)")

    if design.get("antenna_keepout"):
        ok = any(z.GetIsRuleArea() and z.GetDoNotAllowZoneFills()
                 for z in board.Zones())
        if not ok:
            issues.append("antenna_keepout requested in the netlist but no "
                          "no-fill rule area exists on the board")

    issues += check_ep_via_farms(board, design)
    pi_issues, pi_warnings = ([], []) if routed else check_plane_integrity(board)
    h_issues, h_warnings = check_heights(board, design)
    return issues + pi_issues + h_issues, pi_warnings + h_warnings


# component height defaults by footprint hint (mm), specific before generic
PKG_HEIGHTS = [
    ("XAL6060", 6.36), ("CP_Elec_8x10", 10.5), ("CP_Elec", 11.0),
    ("D_7343", 3.1), ("7343", 3.1), ("PinSocket", 8.7), ("PinHeader", 9.0),
    ("TerminalBlock", 12.0), ("USB_C", 3.5), ("SOT-223", 1.8),
    ("SOT-23", 1.2), ("SOT-89", 1.6), ("PowerPAK", 1.1), ("HSOP", 1.7),
    ("TSSOP", 1.2), ("SOIC", 1.75), ("SO-8", 1.75), ("LQFP", 1.6),
    ("QFN", 1.0), ("DFN", 0.8), ("SOD-123", 1.2), ("SOD-323", 1.0),
    ("LED", 0.9), ("0402", 0.5), ("0603", 0.7), ("0805", 0.85),
    ("1206", 0.95), ("1210", 1.1), ("2010", 0.7), ("2512", 0.7),
]


def check_heights(board, design):
    """Mechanical ceiling check — courtyard DRC is 2D-blind. The netlist
    declares "height_zones": [{x,y,w,h,max_height, side: "F"|"B"|"both",
    exempt: [refs]}] in board-relative mm (a HAT's underside clearance to
    the Pi, an enclosure lid, a heatsink shadow). Part height comes from
    the component's "height" key, else the package table; a part inside a
    zone with UNKNOWN height is a warning ("declare it"), a part TALLER
    than the ceiling is a blocker. Returns (issues, warnings)."""
    zones = design.get("height_zones") or []
    if not zones:
        return [], []
    issues, warnings = [], []
    to = pcbnew.ToMM
    bb = board.GetBoardEdgesBoundingBox()
    ox, oy = to(bb.GetLeft()), to(bb.GetTop())
    comps = {c["ref"]: c for c in design.get("components", [])}

    def part_height(c):
        if c.get("height") is not None:
            return float(c["height"])
        fp_name = c.get("footprint", "")
        for hint, h in PKG_HEIGHTS:
            if hint.lower() in fp_name.lower():
                return h
        return None

    for fp in board.GetFootprints():
        ref = fp.GetReference()
        c = comps.get(ref)
        if c is None:
            continue                       # mounting holes etc.
        side = "B" if fp.GetLayer() == pcbnew.B_Cu else "F"
        try:
            cy = fp.GetCourtyard(pcbnew.F_CrtYd if side == "F"
                                 else pcbnew.B_CrtYd)
            fbb = (cy.BBox() if cy and cy.OutlineCount() > 0
                   else fp.GetBoundingBox())
        except Exception:
            fbb = fp.GetBoundingBox()
        fx0, fy0 = to(fbb.GetLeft()) - ox, to(fbb.GetTop()) - oy
        fx1, fy1 = to(fbb.GetRight()) - ox, to(fbb.GetBottom()) - oy
        for z in zones:
            zside = z.get("side", "F")
            if zside != "both" and zside != side:
                continue
            if ref in (z.get("exempt") or []):
                continue
            if (fx1 < z["x"] or fx0 > z["x"] + z["w"]
                    or fy1 < z["y"] or fy0 > z["y"] + z["h"]):
                continue
            lim = float(z["max_height"])
            h = part_height(c)
            zname = z.get("name", f"height zone @({z['x']},{z['y']})")
            if h is None:
                warnings.append(
                    f"{ref}: inside {zname} (ceiling {lim} mm) but its "
                    f"height is unknown — declare \"height\" on the part")
            elif h > lim + 1e-6:
                issues.append(
                    f"{ref}: {h} mm tall inside {zname} with a {lim} mm "
                    f"ceiling ({side} side) — move it out, use a lower "
                    f"package, or exempt it if it IS the standoff")
    return issues, warnings


def check_plane_integrity(board):
    """Pre-route plane fracture detection — the failure DRC can only catch
    after routing and Freerouting can never see at all (the DSN exports a
    plane as a full-board rectangle, hiding fill voids).

    For each plane net: take the FILLED zone's islands, then union-find over
    [islands + the net's pads + existing same-net copper (preroutes)]. Pads
    attach to the island under them (a router via will land there); preroute
    tracks join the pads they enter; preroute vias join their island.

    Severity calibration (from the HAT): a TRUE multi-island split of the
    pad population is a BLOCKER — the router will return the net broken.
    A PINCH split (corridors a single router via could seal, found by
    deflating the fill) is a WARNING — the router only seals corridors it
    actually routes through, and the shipped HAT works with several. GND is
    exempt: the post-route outer pours + stitching own its unification.
    Returns (issues, warnings)."""
    import math
    issues = []
    warnings = []
    to = pcbnew.ToMM
    MIN_ISLAND_MM2 = 5.0
    PAD_REACH = 3.0          # pad attaches to an island within this distance

    for z in board.Zones():
        if z.GetIsRuleArea():
            continue
        net = z.GetNetname()
        nc = z.GetNetCode()
        layer = z.GetLayer()
        if net == "GND":
            continue
        if not board.GetLayerName(layer).startswith("In"):
            # outer-layer pours are post-route artifacts, unified by
            # stitching vias — only the INNER planes ride the DSN export
            continue
        try:
            polys = pcbnew.SHAPE_POLY_SET(z.GetFilledPolysList(layer))
            # KiCad stores fills FRACTURED (holes slit into the outline, so
            # HoleCount()==0) — unfracture or the pinch deflate sees no voids
            polys.Unfracture()
        except Exception:
            continue
        n_out = polys.OutlineCount()
        raw = []                           # per-island SHAPE_POLY_SET + holes
        for i in range(n_out):
            o = polys.Outline(i)
            if abs(o.Area()) / 1e12 < MIN_ISLAND_MM2:
                continue
            sp = pcbnew.SHAPE_POLY_SET()
            sp.AddOutline(o)
            try:
                for j in range(polys.HoleCount(i)):
                    sp.AddHole(polys.Hole(i, j))
            except Exception:
                pass
            raw.append(sp)
        if not raw:
            continue
        fractured = len(raw) > 1

        # PINCH detection: the fresh fill usually survives as ONE island —
        # the fracture completes only when the ROUTER's via antipads seal a
        # narrow corridor (the HAT's VBUS_PD died through a 1.18 mm gap).
        # Deflating by PINCH erases any corridor narrower than 2*PINCH —
        # i.e. anything a single 1.2 mm via + clearance would sever.
        PINCH = 0.9
        islands = []
        for sp in raw:
            d = pcbnew.SHAPE_POLY_SET(sp)
            try:
                d.Deflate(pcbnew.FromMM(PINCH),
                          pcbnew.CORNER_STRATEGY_CHAMFER_ALL_CORNERS,
                          pcbnew.FromMM(0.02))
            except Exception:
                islands.append(sp)
                continue
            kept = 0
            for j in range(d.OutlineCount()):
                o = d.Outline(j)
                if abs(o.Area()) / 1e12 < MIN_ISLAND_MM2:
                    continue
                p2 = pcbnew.SHAPE_POLY_SET()
                p2.AddOutline(o)
                islands.append(p2)
                kept += 1
            if kept == 0:
                islands.append(sp)         # deflate ate it — keep original
        if len(islands) <= 1:
            continue

        # --- union-find nodes: islands then pads ---
        pads = [p for fp in board.GetFootprints() for p in fp.Pads()
                if p.GetNetCode() == nc]
        parent = list(range(len(islands) + len(pads)))

        def find(a):
            while parent[a] != a:
                parent[a] = parent[parent[a]]
                a = parent[a]
            return a

        def union(a, b):
            parent[find(a)] = find(b)

        def island_at(pos, reach=0.0):
            """Index of the island containing pos, else (with reach) the one
            with the smallest TRUE polygon distance — bbox distance is
            meaningless when one outline spans the whole board."""
            best, bd = None, max(reach, 1e-9)
            for k, sp in enumerate(islands):
                if sp.Contains(pos):
                    return k
                if reach > 0:
                    d = math.sqrt(sp.SquaredDistance(pos)) / 1e6
                    if d < bd:
                        best, bd = k, d
            return best

        for pi, pad in enumerate(pads):
            isl = island_at(pad.GetPosition(), PAD_REACH)
            if isl is not None:
                union(len(islands) + pi, isl)

        # existing same-net copper (preroutes laid at build time): a track
        # joins the pads its endpoints enter; a via joins its island
        for t in board.GetTracks():
            if t.GetNetCode() != nc:
                continue
            if t.GetClass() == "PCB_VIA":
                isl = island_at(t.GetPosition())
                if isl is not None:
                    for pi, pad in enumerate(pads):
                        p, q = pad.GetPosition(), t.GetPosition()
                        if math.hypot(to(p.x - q.x), to(p.y - q.y)) < 1.0:
                            union(len(islands) + pi, isl)
                continue
            ends = []
            for ep in (t.GetStart(), t.GetEnd()):
                for pi, pad in enumerate(pads):
                    p = pad.GetPosition()
                    if math.hypot(to(p.x - ep.x), to(p.y - ep.y)) < 1.0:
                        ends.append(len(islands) + pi)
            for a, b in zip(ends, ends[1:]):
                union(a, b)

        groups = {}
        for pi in range(len(pads)):
            groups.setdefault(find(len(islands) + pi), []).append(
                pads[pi].GetParentFootprint().GetReference())
        groups = {g: refs for g, refs in groups.items() if refs}
        if len(groups) >= 2:
            parts = sorted(groups.values(), key=len, reverse=True)
            if fractured:
                issues.append(
                    f"{net}: plane on {board.GetLayerName(layer)} is "
                    f"fractured into separate islands, splitting the net's "
                    f"pads into {len(groups)} groups (largest: "
                    f"{sorted(set(parts[0]))[:6]}, stranded: "
                    f"{sorted(set(parts[1]))[:6]}) — the router trusts the "
                    f"DSN plane rectangle and will return this net broken. "
                    f"Add a corridor preroute joining the groups, or "
                    f"re-place the wall-forming THT part")
            else:
                warnings.append(
                    f"{net}: plane on {board.GetLayerName(layer)} has "
                    f"corridors under {2 * PINCH:.1f} mm between pad groups "
                    f"(e.g. {sorted(set(parts[0]))[:4]} vs "
                    f"{sorted(set(parts[1]))[:4]}) — a single router via can "
                    f"seal one and orphan the group. If routing returns this "
                    f"net unconnected, add a corridor preroute (the G4 "
                    f"healer will also attempt an island link)")
    return issues, warnings


def check_ep_via_farms(board, design):
    """Exposed-pad parts that thermal_check flags as real dissipators must
    have a thermal via farm under the EP — either built into the footprint
    (PTH pads, e.g. the Texas ...ThermalVias variants) or added as board
    vias. IPC-7093 practice; bare EP copper alone cannot move the watts."""
    import thermal_check
    issues = []
    refs = thermal_check.check(design).get("ep_via_parts", [])
    if not refs:
        return issues
    need = int((design.get("thermal") or {}).get("ep_vias_min", 4)) \
        if isinstance(design.get("thermal"), dict) else 4
    to = pcbnew.ToMM
    for ref in refs:
        fp = board.FindFootprintByReference(ref)
        if fp is None:
            continue
        pads = list(fp.Pads())
        if not pads:
            continue
        ep = max(pads, key=lambda p: p.GetBoundingBox().GetWidth()
                 * p.GetBoundingBox().GetHeight())
        bb = ep.GetBoundingBox()
        n = 0
        for p in pads:                       # footprint's own via-holes
            try:
                drilled = to(p.GetDrillSize().x) > 0
            except Exception:
                drilled = False
            if drilled and bb.Contains(p.GetPosition()):
                n += 1
        for t in board.GetTracks():          # board-level vias under the EP
            if t.GetClass() == "PCB_VIA" and bb.Contains(t.GetPosition()):
                n += 1
        if n < need:
            issues.append(
                f"{ref}: dissipating exposed-pad part has only {n} thermal "
                f"via(s) under the EP (needs >= {need}, IPC-7093) — use a "
                f"...ThermalVias footprint variant or add stitching vias")
    return issues


def check_routed_widths(board, pro_path, design=None):
    """Every net bound to a net class must be routed at >= the class width.

    Exemption: a sub-width segment whose endpoint lands on (or within 0.5 mm
    of) a pad of the same net is an accepted PAD-ENTRY NECK — the pad itself
    is narrower than the IPC trace, so the router necks down to enter it and
    the short local section has negligible thermal effect. Only sub-width
    copper in the open field is a real current-capacity violation."""
    from pathlib import Path
    issues = []
    p = Path(pro_path)
    if not p.is_file():
        return issues
    ns = json.loads(p.read_text(encoding="utf-8")).get("net_settings", {})
    cls_w = {c.get("name"): c.get("track_width", 0) for c in ns.get("classes", [])}
    bound = {pt.get("pattern"): pt.get("netclass")
             for pt in ns.get("netclass_patterns", [])}
    targets = {net: cls_w.get(cls, 0) for net, cls in bound.items()}
    bound = dict(bound)
    # preroute nets carry no class (their taps route at signal width), but
    # the designer-authored fat path itself must survive the router intact:
    # police it by checking the LONGEST run of >=width copper exists, via
    # the same open-field rule — sub-width TAP stubs are exempted by the
    # narrow-entry rule below only near pads, so restrict the preroute
    # check to segments WIDER than half the target (the fat path), and
    # verify their total length is non-trivial.
    pre_w = {}
    authored = {}
    for pr in (design or {}).get("preroutes", []):
        net = pr["net"]
        pre_w[net] = max(pre_w.get(net, 0), float(pr["width"]))
        # authored fat length: segments at >= the net's preroute width
        # (the router may legitimately TRIM a fat run where it enters a
        # wide pad — only substantial REMOVAL is a violation)
        pts = []
        for p in pr.get("path", []):
            if p[0] == "xy":
                pts.append((float(p[1]), float(p[2]),
                            float(p[3]) if len(p) > 3 else None))
            else:
                pts.append((None, None,
                            float(p[3]) if len(p) > 3 else None))
        for (x0, y0, w0), (x1, y1, _w1) in zip(pts, pts[1:]):
            w_seg = w0 if w0 is not None else float(pr["width"])
            if (w_seg + 1e-3 >= pre_w[net] and None not in (x0, y0, x1, y1)):
                import math as _m
                authored[net] = authored.get(net, 0) + _m.hypot(x1 - x0,
                                                                y1 - y0)
    to = pcbnew.ToMM
    for net, w_req in pre_w.items():
        fat_len = sum(
            (to(t.GetLength()) if hasattr(t, "GetLength") else 0)
            for t in board.GetTracks()
            if t.GetClass() == "PCB_TRACK" and t.GetNetname() == net
            and to(t.GetWidth()) + 1e-3 >= w_req)
        need = min(1.0, 0.5 * authored.get(net, 2.0))
        if fat_len + 1e-3 < need:
            issues.append(f"{net}: prerouted {w_req} mm power path missing "
                          f"from the routed board ({round(fat_len, 2)} mm of "
                          f"fat copper survives, authored "
                          f"{round(authored.get(net, 0), 2)} mm — router "
                          f"removed it?)")
            bound[net] = "preroute"

    # Entry-neck exemption: a router MUST neck where the copper it enters (pad
    # or via) is itself narrower than the class trace — e.g. a 1.1 mm-tall FB
    # pad or a 1.0 mm via on a 1.375 mm conductor — and it carries that neck a
    # short distance clear of the pad. So a sub-width segment is exempt when it
    # sits within reach of a same-net NARROW entry (reach = the entry's long
    # dimension + 1 mm, at least 3 mm). A long open-field run stays flagged —
    # that is the real current-capacity violation.
    to = pcbnew.ToMM
    entries_by_net = {}
    for fp in board.GetFootprints():
        for pad in fp.Pads():
            entries_by_net.setdefault(pad.GetNetCode(), []).append(
                pad.GetBoundingBox())
    for t in board.GetTracks():
        if t.GetClass() == "PCB_VIA":
            entries_by_net.setdefault(t.GetNetCode(), []).append(
                t.GetBoundingBox())

    def near_narrow_entry(track, target_mm):
        for ebb in entries_by_net.get(track.GetNetCode(), []):
            ew, eh = to(ebb.GetWidth()), to(ebb.GetHeight())
            if min(ew, eh) >= target_mm:        # wide entry — no neck needed
                continue
            cx, cy = to(ebb.GetCenter().x), to(ebb.GetCenter().y)
            reach = max(3.0, max(ew, eh) + 1.0)
            for pt in (track.GetStart(), track.GetEnd()):
                dx, dy = to(pt.x) - cx, to(pt.y) - cy
                if dx * dx + dy * dy <= reach * reach:
                    return True
        return False

    flagged = set()
    for t in board.GetTracks():
        if t.GetClass() != "PCB_TRACK":
            continue
        net = t.GetNetname()
        target = targets.get(net, 0)
        if not target:
            continue
        w = to(t.GetWidth())
        if (w + 1e-3 < target and net not in flagged
                and not near_narrow_entry(t, target)):
            flagged.add(net)
            issues.append(f"{net}: open-field segment routed at {w:.3f} mm but "
                          f"class {bound[net]} requires {target:.3f} mm")
    return issues


def check_diff_pairs(board, design):
    """Differential pairs must be ROUTED as pairs. Declared in net_specs:
    "USB_DP": {"diff_pair": "USB_DM", "dp_tol_mm": 1.5, "dp_gap_mm": 1.0}.
    Measures (a) intra-pair length mismatch and (b) the fraction of each
    net's length that runs coupled (within dp_gap of its partner).

    WARNING-grade by design: Freerouting cannot pair-route, so a blocker
    would make every diff-pair board unbuildable headless — but a pair
    routed as two independent meanders must never pass silently. For real
    high-speed pairs, preroute them as a pair and let these numbers verify.
    Returns warnings list."""
    import math
    warnings = []
    to = pcbnew.ToMM
    specs = design.get("net_specs", {})
    seen = set()
    for net_a, s in specs.items():
        net_b = s.get("diff_pair")
        if not net_b or tuple(sorted((net_a, net_b))) in seen:
            continue
        seen.add(tuple(sorted((net_a, net_b))))
        tol = float(s.get("dp_tol_mm", 1.5))
        gap = float(s.get("dp_gap_mm", 1.0))
        segs = {}
        for name in (net_a, net_b):
            segs[name] = [t for t in board.GetTracks()
                          if t.GetClass() == "PCB_TRACK"
                          and t.GetNetname() == name]
        if not segs[net_a] or not segs[net_b]:
            continue                      # unrouted / plane-carried

        def length(name):
            return sum(to(t.GetLength()) for t in segs[name])

        la, lb = length(net_a), length(net_b)
        if abs(la - lb) > tol:
            warnings.append(
                f"diff pair {net_a}/{net_b}: length mismatch "
                f"{abs(la - lb):.2f} mm ({la:.2f} vs {lb:.2f}) exceeds "
                f"{tol} mm — add serpentine or preroute the pair")

        def seg_pts(t):
            s_, e_ = t.GetStart(), t.GetEnd()
            return ((to(s_.x), to(s_.y)), (to(e_.x), to(e_.y)))

        def dist_pt_seg(p, a, b):
            ax, ay = a
            vx, vy = b[0] - ax, b[1] - ay
            L2 = vx * vx + vy * vy
            if L2 < 1e-12:
                return math.hypot(p[0] - ax, p[1] - ay)
            t_ = max(0.0, min(1.0, ((p[0] - ax) * vx + (p[1] - ay) * vy) / L2))
            return math.hypot(p[0] - (ax + t_ * vx), p[1] - (ay + t_ * vy))

        other = [seg_pts(t) for t in segs[net_b]]
        coupled = total = 0.0
        for t in segs[net_a]:
            (x0, y0), (x1, y1) = seg_pts(t)
            ln = math.hypot(x1 - x0, y1 - y0)
            if ln < 1e-6:
                continue
            total += ln
            mid = ((x0 + x1) / 2, (y0 + y1) / 2)
            d = min((dist_pt_seg(mid, a, b) for a, b in other), default=1e9)
            if d <= gap + 0.2:            # gap is edge-ish; small slack
                coupled += ln
        frac = coupled / total if total else 0.0
        if frac < 0.7:
            warnings.append(
                f"diff pair {net_a}/{net_b}: only {frac:.0%} of the routed "
                f"length runs coupled (within {gap} mm of its partner) — "
                f"the pair is routed as two independent traces; preroute it "
                f"as a pair for impedance/EMI integrity")
    return warnings


def check_via_ampacity(board, design):
    """High-current nets must not bottleneck at a layer transition: the via
    barrels on the net (taken together) must carry the net's specified
    current per the IPC-2221 internal-conductor ampacity of one barrel.
    Approximation: total barrel count vs. the per-transition requirement —
    exact per-transition clustering is overkill when power nets tie into
    planes, where every barrel shares the load."""
    import ipc_rules
    issues = []
    to = pcbnew.ToMM
    specs = design.get("net_specs", {})
    preroutes = design.get("preroutes", [])
    pre_nets = {p["net"] for p in preroutes}
    for net, s in specs.items():
        current = float(s.get("current", 0) or 0)
        if current < 2.0:
            continue
        if net in pre_nets:
            # the designer prerouted this net's current path. Router-added
            # vias on it are signal TAPS (a gate divider on the back, a
            # sense pin) that never see the chain current — only the
            # AUTHORED via cluster (if the preroute itself changes layers)
            # must carry it, judged by summed barrel ampacity.
            layers = {p.get("layer", "F.Cu") for p in preroutes
                      if p["net"] == net}
            if len(layers) < 2:
                continue                  # current path is single-layer
            cap = 0.0
            n_vias = 0
            for p in preroutes:
                if p["net"] != net:
                    continue
                d = (p.get("via_size") or [1.2, 0.9])[1]
                n_vias += len(p.get("vias", []))
                cap += len(p.get("vias", [])) * ipc_rules.via_ampacity_a(d)
            if cap + 1e-6 < current:
                issues.append(
                    f"{net}: prerouted layer transition carries {current} A "
                    f"but the authored via cluster ({n_vias} via(s)) totals "
                    f"only {round(cap, 2)} A (IPC-2221) — add parallel vias")
            continue
        drills = [to(t.GetDrillValue()) for t in board.GetTracks()
                  if t.GetClass() == "PCB_VIA" and t.GetNetname() == net]
        if not drills:
            continue                      # net never changes layers
        # PTH pads on the net are barrels too — a THT inductor/connector leg
        # bridging the same layers shares the transition current exactly like
        # a via (the check's stated all-barrels-share approximation)
        n_vias = len(drills)
        for fp in board.GetFootprints():
            for pad in fp.Pads():
                if (pad.GetNetname() == net
                        and pad.GetAttribute() == pcbnew.PAD_ATTRIB_PTH):
                    try:
                        d = to(pad.GetDrillSize().x)
                    except Exception:
                        d = 0
                    if d > 0:
                        drills.append(d)
        cap = sum(ipc_rules.via_ampacity_a(d) for d in drills)
        if cap + 1e-6 < current:
            issues.append(
                f"{net}: {current} A crosses layers through {n_vias} via(s) "
                f"+ {len(drills) - n_vias} PTH barrel(s) totaling "
                f"{round(cap, 2)} A (IPC-2221) — add parallel vias at the "
                f"transition")
    return issues


def main():
    mode, pcb = sys.argv[1], sys.argv[2]
    design = {}
    if len(sys.argv) > 3:
        try:
            design = json.load(open(sys.argv[3], encoding="utf-8"))
        except Exception:
            pass
    board = pcbnew.LoadBoard(pcb)
    warnings = []
    if mode == "placement":
        issues, warnings = check_placement(board, design)
    elif mode == "placement_routed":
        issues, warnings = check_placement(board, design, routed=True)
    elif mode == "routed":
        pro = pcb.rsplit(".", 1)[0] + ".kicad_pro"
        issues = check_routed_widths(board, pro, design)
        issues += check_via_ampacity(board, design)
        warnings += check_diff_pairs(board, design)
    else:
        issues = [f"unknown mode {mode}"]
    print(json.dumps({"issues": issues, "warnings": warnings}))


if __name__ == "__main__":
    main()
