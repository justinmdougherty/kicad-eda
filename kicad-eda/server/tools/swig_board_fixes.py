#!/usr/bin/env python3
"""Post-route board-level corrective actions — runs under KiCad's Python.

The G4 gate's self-feeding loop calls this to APPLY fixes for blockers that
have an unambiguous board-level remedy, instead of re-placing and re-routing
the whole board:

  heal     micro-gap healer (swig_heal): a DRC "unconnected items" break on
           one net is a router/SES artifact, not a routing failure — bridge
           or link the two copper islands at the net's class width (read
           from <pcb>.kicad_pro by netclass_read), or lay nothing and report
           it in "open". (Freerouting is deterministic; re-routing the
           identical board can never clear these.)
  vias     IPC-2221 via-ampacity fixer: a net whose spec current exceeds the
           summed barrel ampacity at its layer transition gets parallel vias
           dropped ON its fat track next to the existing transition via. When
           the net owns a plane the new barrel shares load through it; on a
           plane-less net a same-net tie track is added on the via's far layer.
  ep_vias  thermal via farm under the EP of a dissipating exposed-pad part.

Usage:  kicad-python swig_board_fixes.py <pcb> <netlist.json> <drc.json> <heal|vias|ep_vias|all>
Emits JSON {"applied": [...], "open": [...]} on stdout; saves the board only
when changed.
"""
import json
import math
import sys
from pathlib import Path

import kicad_quiet  # noqa: F401  (before pcbnew: no modal wx dialogs)
import pcbnew

import ipc_rules
import netclass_read
import swig_copper
import swig_heal

TOL = 0.02             # mm — endpoint coincidence tolerance


# The shared SWIG copper helpers live in swig_copper (F5-03); these aliases
# keep the via adders below unchanged.
_to = swig_copper.to
_mm = swig_copper.mm
_dist = swig_copper.dist
_min_clearance = swig_copper.min_clearance
_via_w = swig_copper.via_w
_net_tracks = swig_copper.net_tracks
_attach_points = swig_copper.attach_points
_via_template = swig_copper.via_template
_add_via = swig_copper.add_via
_seg_dist = swig_copper.seg_dist
_clear_spot = swig_copper.clear_spot
_clear_path = swig_copper.clear_path
_nearest_net_via = swig_copper.nearest_net_via
_net_vias_near = swig_copper.net_vias_near
_bfs_path = swig_copper.bfs_path


def add_parallel_vias(board, design, open_tasks):
    applied = []
    specs = design.get("net_specs", {})
    pre_nets = {p["net"] for p in design.get("preroutes", [])}
    plane_nets = {z.GetNetname() for z in board.Zones()
                  if not z.GetIsRuleArea()}
    for netname, s in specs.items():
        current = float(s.get("current", 0) or 0)
        if current < 2.0 or netname in pre_nets:
            continue
        net = board.FindNet(netname)
        if net is None:
            continue
        nc = net.GetNetCode()
        vias = [t for t in board.GetTracks()
                if t.GetClass() == "PCB_VIA" and t.GetNetCode() == nc]
        if not vias:
            continue
        # same barrel math as swig_gate_checks.check_via_ampacity: vias AND
        # PTH pad legs share the transition current
        drills = [_to(v.GetDrillValue()) for v in vias]
        for fp in board.GetFootprints():
            for pad in fp.Pads():
                if (pad.GetNetCode() == nc
                        and pad.GetAttribute() == pcbnew.PAD_ATTRIB_PTH):
                    try:
                        d = _to(pad.GetDrillSize().x)
                    except Exception:
                        d = 0
                    if d > 0:
                        drills.append(d)
        cap = sum(ipc_rules.via_ampacity_a(d) for d in drills)
        if cap + 1e-6 >= current:
            continue
        one = ipc_rules.via_ampacity_a(min(_to(v.GetDrillValue())
                                           for v in vias))
        deficit = max(1, math.ceil((current - cap) / max(one, 0.1)))
        for v in list(vias):
            if deficit <= 0:
                break
            vp = (_to(v.GetPosition().x), _to(v.GetPosition().y))
            via_d = _to(_via_w(v))
            # tracks landing on this via, longest first — the fat feed
            feeds = sorted(
                (t for t in _net_tracks(board, nc)
                 if any(_dist((_to(p.x), _to(p.y)), vp) < TOL
                        for p in (t.GetStart(), t.GetEnd()))),
                key=lambda t: -t.GetLength())
            for t in feeds:
                if deficit <= 0:
                    break
                s_, e_ = t.GetStart(), t.GetEnd()
                a = (_to(s_.x), _to(s_.y))
                b = (_to(e_.x), _to(e_.y))
                if _dist(a, vp) < TOL:
                    a, b = b, a            # b is the via end, a the far end
                tlen = _dist(a, b)
                for d in (1.2, 1.6, 2.0, 2.6):
                    if tlen < d + via_d / 2 + 0.2:
                        continue
                    ux, uy = (a[0] - b[0]) / tlen, (a[1] - b[1]) / tlen
                    nxy = (b[0] + ux * d, b[1] + uy * d)
                    if not _clear_spot(board, nc, nxy, via_d / 2 + 0.25):
                        continue
                    if (netname not in plane_nets
                            and not _clear_path(board, nc, nxy, vp,
                                                _to(t.GetWidth()) / 2 + 0.2)):
                        continue
                    nv = pcbnew.PCB_VIA(board)
                    nv.SetPosition(pcbnew.VECTOR2I(_mm(nxy[0]), _mm(nxy[1])))
                    nv.SetWidth(_via_w(v))
                    nv.SetDrill(v.GetDrill())
                    nv.SetViaType(pcbnew.VIATYPE_THROUGH)
                    nv.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
                    nv.SetNet(net)
                    board.Add(nv)
                    deficit -= 1
                    note = "shares load through the net's plane"
                    if netname not in plane_nets:
                        # no plane: tie the new barrel to the original via on
                        # the far side so the parallel path actually exists
                        far = (pcbnew.B_Cu if t.GetLayer() == pcbnew.F_Cu
                               else pcbnew.F_Cu)
                        tie = pcbnew.PCB_TRACK(board)
                        tie.SetStart(nv.GetPosition())
                        tie.SetEnd(v.GetPosition())
                        tie.SetWidth(t.GetWidth())
                        tie.SetLayer(far)
                        tie.SetNet(net)
                        board.Add(tie)
                        note = f"tied to the transition on {board.GetLayerName(far)}"
                    applied.append(
                        f"{netname}: added parallel via "
                        f"({_to(v.GetDrillValue()):.2f} mm drill, "
                        f"{ipc_rules.via_ampacity_a(_to(v.GetDrillValue()))} A "
                        f"IPC-2221) {d} mm up the feed track — {note}")
                    break
        if deficit > 0:
            open_tasks.append(f"{netname}: still {deficit} via(s) short — no "
                              f"safe spot on the feed tracks (designer task)")
    return applied


def add_ep_via_farms(board, design):
    """Thermal via farm under the EP of every dissipating exposed-pad part
    (thermal_check ep_via_parts) that lacks one — the auto-remedy for the
    G3 check_ep_via_farms blocker when the library has no ...ThermalVias
    footprint variant (e.g. TO-263). IPC-7093: a grid of 0.3 mm-drill vias
    fully inside the EP copper, same net, sunk into the planes. Caller
    re-fills zones so plane antipads regenerate around the new barrels."""
    import thermal_check
    applied = []
    refs = thermal_check.check(design).get("ep_via_parts", [])
    if not refs:
        return applied
    need = int((design.get("thermal") or {}).get("ep_vias_min", 4)) \
        if isinstance(design.get("thermal"), dict) else 4
    W, DRILL = _mm(0.6), _mm(0.3)
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
        have = sum(1 for t in board.GetTracks()
                   if t.GetClass() == "PCB_VIA" and bb.Contains(t.GetPosition()))
        for p in pads:
            try:
                drilled = p.GetDrillSize().x > 0
            except Exception:
                drilled = False
            if drilled and bb.Contains(p.GetPosition()):
                have += 1
        if have >= need:
            continue
        # grid fully inside the pad: margin = via radius + annular slack
        m = _to(W) / 2 + 0.15
        x0, y0 = _to(bb.GetLeft()) + m, _to(bb.GetTop()) + m
        pw, ph = _to(bb.GetRight()) - m - x0, _to(bb.GetBottom()) - m - y0
        if pw <= 0 or ph <= 0:
            continue
        cols = max(1, min(int(pw / 1.2) + 1, 6))   # ~1.2 mm IPC-7093 pitch
        rows = max(1, min(int(ph / 1.2) + 1, 6))
        while cols * rows < need and (cols < 6 or rows < 6):
            if pw / cols >= ph / rows and cols < 6:
                cols += 1
            elif rows < 6:
                rows += 1
            else:
                break
        taken = [(_to(t.GetPosition().x), _to(t.GetPosition().y))
                 for t in board.GetTracks() if t.GetClass() == "PCB_VIA"]
        net, added = ep.GetNet(), 0
        for i in range(cols):
            for j in range(rows):
                xy = (x0 + pw * (i + 0.5) / cols, y0 + ph * (j + 0.5) / rows)
                if any(_dist(xy, t) < 0.55 for t in taken):  # hole-to-hole
                    continue
                _add_via(board, net, xy, W, DRILL)
                taken.append(xy)
                added += 1
        if added:
            applied.append(
                f"{ref}: {added} thermal via(s) 0.3 mm drill under the EP "
                f"on net {net.GetNetname()} (IPC-7093 via farm)")
    return applied


def _read_json(path: str) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def board_nets(board: pcbnew.BOARD) -> set[str]:
    """Every net name carried by the board's tracks, pads and zones."""
    names = {t.GetNetname() for t in board.GetTracks()}
    names |= {p.GetNetname() for fp in board.GetFootprints() for p in fp.Pads()}
    names |= {z.GetNetname() for z in board.Zones()}
    return names - {""}


def heal(board: pcbnew.BOARD, pcb: str, drc: dict) -> tuple[list[str], list[str]]:
    """swig_heal at the class widths of <pcb>.kicad_pro. An unreadable project
    heals nothing (no copper without its class width) and says so in open."""
    pro = Path(pcb).with_suffix(".kicad_pro")
    try:
        widths = netclass_read.net_class_widths(pro, board_nets(board))
    except ValueError as e:
        return [], [f"heal skipped: net classes unreadable ({e})"]
    return swig_heal.heal_micro_gaps(board, drc, widths)


def main():
    pcb, netlist, drc_path, mode = sys.argv[1:5]
    board = pcbnew.LoadBoard(pcb) if Path(pcb).is_file() else None
    if board is None:
        print(json.dumps({"applied": [], "open": [f"could not load board {pcb}"]}))
        sys.exit(1)
    design, drc = _read_json(netlist), _read_json(drc_path)
    applied, open_tasks = [], []
    if mode in ("heal", "all"):
        healed, still_open = heal(board, pcb, drc)
        applied += healed
        open_tasks += still_open
    if mode in ("vias", "all"):
        applied += add_parallel_vias(board, design, open_tasks)
    if mode in ("ep_vias", "all"):
        applied += add_ep_via_farms(board, design)
    if applied:
        board.BuildConnectivity()
        try:
            pcbnew.ZONE_FILLER(board).Fill(board.Zones())
        except Exception as e:     # pcbnew raises its own wrapped types
            open_tasks.append(f"zone refill after the fixes failed ({type(e).__name__}: {e})")
        pcbnew.SaveBoard(pcb, board)
    print(json.dumps({"applied": applied, "open": open_tasks}))


if __name__ == "__main__":
    main()
