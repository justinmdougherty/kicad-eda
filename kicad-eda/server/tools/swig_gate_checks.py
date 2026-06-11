#!/usr/bin/env python3
"""Board-side gate checks — runs under KiCad's bundled Python (needs pcbnew).

Two modes, both emit JSON {"issues": [...]} on stdout:

  placement <pcb> [netlist.json]
      Pre-route placement-review checks (the G3 gate):
      - every connector's mating mouth faces OFF the board (not into the
        interior) and sits at the edge it faces (<= 3 mm)
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


def check_placement(board, design):
    issues = []
    to = pcbnew.ToMM
    bb = board.GetBoardEdgesBoundingBox()
    bx0, by0 = to(bb.GetLeft()), to(bb.GetTop())
    bx1, by1 = to(bb.GetRight()), to(bb.GetBottom())
    bcx, bcy = (bx0 + bx1) / 2, (by0 + by1) / 2

    for fp in board.GetFootprints():
        ref = fp.GetReference()
        if not ref.startswith("J"):
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
    return issues


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
    for pr in (design or {}).get("preroutes", []):
        pre_w[pr["net"]] = max(pre_w.get(pr["net"], 0), float(pr["width"]))
    to = pcbnew.ToMM
    for net, w_req in pre_w.items():
        fat_len = sum(
            (to(t.GetLength()) if hasattr(t, "GetLength") else 0)
            for t in board.GetTracks()
            if t.GetClass() == "PCB_TRACK" and t.GetNetname() == net
            and to(t.GetWidth()) + 1e-3 >= w_req)
        if fat_len < 1.0:
            issues.append(f"{net}: prerouted {w_req} mm power path missing "
                          f"from the routed board (router removed it?)")
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
    if mode == "placement":
        issues = check_placement(board, design)
    elif mode == "routed":
        pro = pcb.rsplit(".", 1)[0] + ".kicad_pro"
        issues = check_routed_widths(board, pro, design)
        issues += check_via_ampacity(board, design)
    else:
        issues = [f"unknown mode {mode}"]
    print(json.dumps({"issues": issues}))


if __name__ == "__main__":
    main()
