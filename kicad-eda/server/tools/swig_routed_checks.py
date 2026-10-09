#!/usr/bin/env python3
"""Routed-board (G4) design-intent checks, run under KiCad's bundled Python.

Moved out of swig_gate_checks.py unchanged (F3-13a); swig_gate_checks
re-exports all three and its ``routed`` mode calls them:
  - check_routed_widths: every net with a net-class width (resolved through
    netclass_read as KiCad does) is routed at >= that width outside pad-entry
    necks, and every prerouted power path survives the router;
  - check_via_ampacity: a high-current net's layer transitions carry its
    current (IPC-2221 barrel ampacity, vias and PTH legs together);
  - check_diff_pairs: declared pairs are length-matched and routed coupled
    (warnings only: Freerouting cannot pair-route).
stdlib + pcbnew only (ipc_rules and netclass_read are stdlib-only).
"""
import kicad_quiet  # noqa: F401  (before pcbnew: no modal wx dialogs)
import pcbnew


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
    import netclass_read
    classed = netclass_read.net_class_widths(
        p, nets={t.GetNetname() for t in board.GetTracks()})
    bound = {net: cls for net, (cls, _w) in classed.items()}
    targets = {net: w for net, (_cls, w) in classed.items()}
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
        for (x0, y0, w0), (x1, y1, _w1) in zip(pts, pts[1:], strict=False):
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

        def length(name, segs=segs):     # bound now: called in this iteration only
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
