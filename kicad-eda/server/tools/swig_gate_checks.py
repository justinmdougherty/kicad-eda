#!/usr/bin/env python3
"""Board-side gate checks — runs under KiCad's bundled Python (needs pcbnew).

Three modes, each emits JSON {"issues": [...], "warnings": [...]} on stdout:

  placement <pcb> [netlist.json]
      Pre-route placement-review checks (the G3 gate):
      - every connector's mating mouth faces OFF the board (not into the
        interior) and sits at the edge it faces (<= 3 mm); a J* part the
        netlist flags "interior": true (jumper, Kelvin header) is exempt
      - if the netlist requests an antenna_keepout, a no-fill rule area exists
      - exposed-pad via farms, pre-route plane integrity, height zones
  placement_routed <pcb> [netlist.json]
      The placement checks re-run on a routed board (plane integrity skipped).
  routed <pcb> [netlist.json]
      Post-route layout-review checks (part of the G4 gate):
      - every net bound to a net class is routed at >= the class track width
        (verifies the IPC-2221/2141A rules engine wasn't silently ignored)
      - high-current layer transitions have enough via ampacity; declared
        differential pairs are length-matched and coupled (warnings)
      These live in swig_routed_checks.py and are re-exported here.

main() loads the board through board_outline.load_board (a missing or
unreadable file is one JSON issue, never a None board) and builds every
footprint's courtyard cache: KiCad 9.0.4's LoadBoard leaves them EMPTY, so the
connector rule never fired and check_heights measured the silk/text bbox (T02).

These complement (not replace) kicad-cli DRC: DRC checks geometry against
rules; these check the *design intent* a DRC can't see.
"""
import json
import sys
from typing import Any

import kicad_quiet  # noqa: F401  (before pcbnew: no modal wx dialogs)
import pcbnew

# the routed (G4) checks: callers and tests import them from this module
from swig_routed_checks import check_diff_pairs, check_routed_widths, check_via_ampacity


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
        # the "no copper pour" getter differs between pcbnew 9.x and 10.x: read it
        # through board_outline. Imported here: board_outline binds pcbnew when first
        # imported, and unit tests import this module with a stub pcbnew briefly.
        import board_outline
        ok = any(board_outline.blocks_pour(z) for z in board.Zones())
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

        # parent / islands are bound per zone: the helpers run in this iteration only
        def find(a, parent=parent):
            while parent[a] != a:
                parent[a] = parent[parent[a]]
                a = parent[a]
            return a

        def union(a, b, parent=parent):
            parent[find(a)] = find(b)

        def island_at(pos, reach=0.0, islands=islands):
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
            for a, b in zip(ends, ends[1:], strict=False):
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


def load_checked_board(pcb: str) -> Any:
    """The board the checks read: board_outline.load_board (FileNotFoundError /
    RuntimeError instead of a None board), then every footprint's courtyard
    cache built. KiCad 9.0.4's LoadBoard leaves those caches EMPTY: without
    this _world_mouth sees no courtyard (no J* was ever judged, T02) and
    check_heights falls back to GetBoundingBox (silk and text included)."""
    import board_outline
    board = board_outline.load_board(pcb)
    for fp in board.GetFootprints():
        fp.BuildCourtyardCaches()
    return board


def read_design(path: str) -> tuple[dict, list[str]]:
    """(netlist, warnings). A missing or unreadable netlist still runs the
    checks without design intent, as before, but now says so in a warning."""
    try:
        with open(path, encoding="utf-8") as f:
            design = json.load(f)
    except (OSError, ValueError) as e:
        return {}, [f"netlist {path} not read ({type(e).__name__}: {e}): checks ran "
                    f"without design intent"]
    if not isinstance(design, dict):
        return {}, [f"netlist {path} is not a JSON object: checks ran without design intent"]
    return design, []


def run_mode(mode: str, pcb: str, board: Any, design: dict) -> tuple[list, list]:
    """(issues, warnings) of one main() mode."""
    if mode == "placement":
        return check_placement(board, design)
    if mode == "placement_routed":
        return check_placement(board, design, routed=True)
    if mode == "routed":
        pro = pcb.rsplit(".", 1)[0] + ".kicad_pro"
        issues = check_routed_widths(board, pro, design)
        issues += check_via_ampacity(board, design)
        return issues, check_diff_pairs(board, design)
    return [f"unknown mode {mode}"], []


def main() -> int:
    """argv: mode pcb [netlist.json]. Prints {"issues": [...], "warnings": [...]};
    rc 1 (with one issue saying why) when the board cannot be loaded."""
    mode, pcb = sys.argv[1], sys.argv[2]
    design, warnings = read_design(sys.argv[3]) if len(sys.argv) > 3 else ({}, [])
    try:
        board = load_checked_board(pcb)
    except (OSError, RuntimeError) as e:
        print(json.dumps({"issues": [f"cannot load the board: {e}"], "warnings": warnings}))
        return 1
    issues, found = run_mode(mode, pcb, board, design)
    print(json.dumps({"issues": issues, "warnings": warnings + found}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
