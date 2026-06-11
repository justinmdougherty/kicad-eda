#!/usr/bin/env python3
"""Post-route board-level corrective actions — runs under KiCad's Python.

The G4 gate's self-feeding loop calls this to APPLY fixes for blockers that
have an unambiguous board-level remedy, instead of re-placing and re-routing
the whole board:

  heal   micro-gap healer: a DRC "unconnected items" pair where BOTH items
         are the same net and sit within ~2 mm is a router/SES artifact, not
         a routing failure — bridge the nearest same-net attach points with
         a short track on the items' layer. (Freerouting is deterministic;
         re-routing the identical board can never clear these.)
  vias   IPC-2221 via-ampacity fixer: a net whose spec current exceeds the
         summed barrel ampacity at its layer transition gets parallel vias
         dropped ON its fat track next to the existing transition via. When
         the net owns a plane the new barrel shares load through it; on a
         plane-less net a same-net tie track is added on the via's far layer.

Usage:  kicad-python swig_board_fixes.py <pcb> <netlist.json> <drc.json> <heal|vias|all>
Emits JSON {"applied": [...]} on stdout; saves the board only when changed.
"""
import json
import math
import re
import sys

import pcbnew

import ipc_rules

MAX_GAP = 2.0          # mm — beyond this it's a real routing failure
TOL = 0.02             # mm — endpoint coincidence tolerance


def _to(v):
    return pcbnew.ToMM(v)


def _mm(v):
    return pcbnew.FromMM(v)


def _dist(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _min_clearance(board):
    """The board's real minimum copper clearance (mm) — margins derived from
    an assumed 0.15 mm fail DRC on a 0.2 mm-rule project."""
    try:
        return max(_to(board.GetDesignSettings().GetSmallestClearanceValue()),
                   0.15)
    except Exception:
        return 0.2


def _via_w(v):
    """PCB_VIA pad width. KiCad 10's padstack API asserts (and, headless on
    Windows, WEDGES the process in a wx dialog) when GetWidth() is called
    with no layer — a through via is uniform, so F_Cu is always right."""
    try:
        return v.GetWidth(pcbnew.F_Cu)
    except TypeError:
        return v.GetWidth()


def _net_tracks(board, netcode):
    return [t for t in board.GetTracks()
            if t.GetClass() == "PCB_TRACK" and t.GetNetCode() == netcode]


def _attach_points(board, netcode, near, reach=1.5):
    """Same-net copper attach points within `reach` mm of `near`:
    track endpoints, via centers, pad centers. Returns [(x, y, layer_id, w)]
    (layer_id None for pads/vias = any copper layer)."""
    pts = []
    for t in board.GetTracks():
        if t.GetNetCode() != netcode:
            continue
        if t.GetClass() == "PCB_TRACK":
            for p in (t.GetStart(), t.GetEnd()):
                xy = (_to(p.x), _to(p.y))
                if _dist(xy, near) <= reach:
                    pts.append((xy[0], xy[1], t.GetLayer(), _to(t.GetWidth())))
        elif t.GetClass() == "PCB_VIA":
            xy = (_to(t.GetPosition().x), _to(t.GetPosition().y))
            if _dist(xy, near) <= reach:
                pts.append((xy[0], xy[1], None, _to(_via_w(t))))
    for fp in board.GetFootprints():
        for pad in fp.Pads():
            if pad.GetNetCode() != netcode:
                continue
            xy = (_to(pad.GetPosition().x), _to(pad.GetPosition().y))
            if _dist(xy, near) > reach:
                continue
            if pad.GetAttribute() == pcbnew.PAD_ATTRIB_PTH:
                layer = None                  # barrel — reachable on any layer
            elif pad.IsOnLayer(pcbnew.F_Cu):
                layer = pcbnew.F_Cu
            else:
                layer = pcbnew.B_Cu
            pts.append((xy[0], xy[1], layer, 0.25))
    return pts


def _via_template(board, netcode):
    """(width, drill) for a stitching via: copy a same-net via, else the
    smallest via on the board, else the 0.6/0.3 signal default."""
    same = [t for t in board.GetTracks()
            if t.GetClass() == "PCB_VIA" and t.GetNetCode() == netcode]
    pool = same or [t for t in board.GetTracks() if t.GetClass() == "PCB_VIA"]
    if pool:
        v = min(pool, key=_via_w)
        return _via_w(v), v.GetDrill()
    return _mm(0.6), _mm(0.3)


def _add_via(board, net, xy, w, drill):
    v = pcbnew.PCB_VIA(board)
    v.SetPosition(pcbnew.VECTOR2I(_mm(xy[0]), _mm(xy[1])))
    v.SetWidth(w)
    v.SetDrill(drill)
    v.SetViaType(pcbnew.VIATYPE_THROUGH)
    v.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
    v.SetNet(net)
    board.Add(v)
    return v


def heal_micro_gaps(board, drc):
    """Two remedies for a same-net DRC 'unconnected items' pair:
    - SHORT BRIDGE: nearest attach points <= MAX_GAP apart on a shared layer
      get a clearance-checked track (router/SES artifact).
    - ISLAND LINK: connect the two clusters anchor-to-anchor (via centers =
      any layer, track ends / pads = their layer) — straight, L, then a BFS
      maze route. On plane nets this repairs what the DSN plane abstraction
      hid from the router (fill voids/islands)."""
    applied = []
    plane_nets = {z.GetNetname() for z in board.Zones()
                  if not z.GetIsRuleArea()}
    for item in drc.get("unconnected_items", []):
        its = item.get("items", [])
        if len(its) != 2:
            continue
        nets = [re.search(r"\[(.+?)\]", i.get("description", "")) for i in its]
        if not all(nets) or nets[0].group(1) != nets[1].group(1):
            continue
        netname = nets[0].group(1)
        net = board.FindNet(netname)
        if net is None:
            continue
        pos = []
        for i in its:
            p = i.get("pos") or {}
            pos.append((float(p.get("x", 0)), float(p.get("y", 0))))

        nc = net.GetNetCode()

        # --- remedy 1: SHORT BRIDGE between the nearest attach points -----
        cands = [_attach_points(board, nc, p) for p in pos]
        if cands[0] and cands[1]:
            a, b = min(((a, b) for a in cands[0] for b in cands[1]),
                       key=lambda ab: _dist(ab[0][:2], ab[1][:2]))
            gap = _dist(a[:2], b[:2])
            same_layer = (a[2] is None or b[2] is None or a[2] == b[2])
            if gap <= MAX_GAP and same_layer:
                layer = next((p[2] for p in (a, b) if p[2] is not None),
                             pcbnew.F_Cu)
                width = min((w for w in (a[3], b[3]) if w), default=0.25)
                if _clear_path(board, nc, a[:2], b[:2],
                               width / 2 + _min_clearance(board) + 0.05,
                               layer):
                    t = pcbnew.PCB_TRACK(board)
                    t.SetStart(pcbnew.VECTOR2I(_mm(a[0]), _mm(a[1])))
                    t.SetEnd(pcbnew.VECTOR2I(_mm(b[0]), _mm(b[1])))
                    t.SetWidth(_mm(width))
                    t.SetLayer(layer)
                    t.SetNet(net)
                    board.Add(t)
                    applied.append(
                        f"healed {gap:.2f} mm same-net gap on {netname} "
                        f"({board.GetLayerName(layer)}, {width:.2f} mm bridge)")
                    continue

        # --- remedy 2: ISLAND LINK. Lay a same-net track between the two
        # disconnected clusters. Anchors: same-net via centers near each item
        # (reach any layer) and track endpoints (their own layer). Try every
        # compatible (anchor pair, layer) straight/L first; escalate to a
        # coarse BFS maze route for the nearest pairs — that threads via
        # fanout fields no fixed shape can. On plane nets this is the
        # DSN-abstraction fix (router believed the plane connected them). ---
        zlayers = [z.GetLayer() for z in board.Zones()
                   if not z.GetIsRuleArea() and z.GetNetname() == netname]
        anchors = []
        for p in pos:
            a_list = [(v, None) for v in _net_vias_near(board, nc, p)]
            a_list += [((x, y), layer)
                       for x, y, layer, _w in _attach_points(board, nc, p)
                       if layer is not None]
            anchors.append(a_list)
        if not (anchors[0] and anchors[1]):
            continue

        def layers_for(la, lb):
            if la is None and lb is None:
                return zlayers + [pcbnew.F_Cu, pcbnew.B_Cu]
            if la is None:
                return [lb]
            if lb is None:
                return [la]
            return [la] if la == lb else []

        pairs = sorted(((a, b) for a in anchors[0] for b in anchors[1]),
                       key=lambda ab: _dist(ab[0][0], ab[1][0]))
        # BFS phase: try the pairs with the most LAYER FREEDOM first (via-via
        # can route on any layer; track-end pairs are locked to one) — the
        # nearest pairs are usually layer-locked stubs that already failed.
        bfs_pairs = sorted(pairs, key=lambda ab: (
            (ab[0][1] is not None) + (ab[1][1] is not None),
            _dist(ab[0][0], ab[1][0])))
        done = False
        for use_bfs, limit in ((False, 10), (True, 6)):
            for (pa, la), (pb, lb) in (bfs_pairs if use_bfs else pairs)[:limit]:
                for layer in layers_for(la, lb):
                    if _try_link(board, net, pa, pb, layer, bfs=use_bfs):
                        applied.append(
                            f"{netname}: linked disconnected clusters "
                            f"({_dist(pa, pb):.2f} mm apart) on "
                            f"{board.GetLayerName(layer)}"
                            + (" via maze route" if use_bfs else "")
                            + (" — DSN plane abstraction hid the fill void"
                               if netname in plane_nets else ""))
                        done = True
                        break
                if done:
                    break
            if done:
                break

        # --- remedy 3: NO SHARED LAYER. One cluster lives on a single layer
        # with no via (e.g. an F.Cu-only stub) and the other side's anchors
        # are elsewhere — no link is even attemptable. Drop a same-net via on
        # a track-end anchor to buy layer freedom, then link from it. The
        # via is removed again if no link lands. ---------------------------
        if not done:
            w, drill = _via_template(board, nc)
            keep_via = _to(w) / 2 + 0.25
            for side in (0, 1):
                for pa, la in anchors[side]:
                    if la is None or not _clear_spot(board, nc, pa, keep_via):
                        continue
                    v = _add_via(board, net, pa, w, drill)
                    for use_bfs in (False, True):
                        for pb, lb in anchors[1 - side][:6]:
                            for layer in layers_for(None, lb):
                                if _try_link(board, net, pa, pb, layer,
                                             bfs=use_bfs):
                                    applied.append(
                                        f"{netname}: stitched a via at "
                                        f"({pa[0]:.2f},{pa[1]:.2f}) and linked "
                                        f"clusters on "
                                        f"{board.GetLayerName(layer)}"
                                        + (" via maze route" if use_bfs
                                           else ""))
                                    done = True
                                    break
                            if done:
                                break
                        if done:
                            break
                    if done:
                        break
                    board.Remove(v)        # via bought nothing — take it back
                if done:
                    break
    return applied


def _seg_dist(p, a, b):
    """Distance from point p to segment a-b (all (x,y) mm tuples)."""
    ax, ay = a
    vx, vy = b[0] - ax, b[1] - ay
    L2 = vx * vx + vy * vy
    if L2 < 1e-12:
        return _dist(p, a)
    t = max(0.0, min(1.0, ((p[0] - ax) * vx + (p[1] - ay) * vy) / L2))
    return _dist(p, (ax + t * vx, ay + t * vy))


def _clear_spot(board, netcode, xy, keep_out, layer=None):
    """True when nothing FOREIGN (other-net copper) comes within keep_out mm
    of xy. layer=None means a through-item (via) — copper on EVERY layer
    blocks; a specific layer tests a track path, so foreign tracks and SMD
    pads on other layers are ignored (through-items always block). Tracks
    are tested as SEGMENTS, not endpoints."""
    for t in board.GetTracks():
        if t.GetNetCode() == netcode:
            continue
        if t.GetClass() == "PCB_VIA":
            p = t.GetPosition()
            if _dist((_to(p.x), _to(p.y)), xy) < keep_out + _to(_via_w(t)) / 2:
                return False
        else:
            if layer is not None and t.GetLayer() != layer:
                continue
            s, e = t.GetStart(), t.GetEnd()
            d = _seg_dist(xy, (_to(s.x), _to(s.y)), (_to(e.x), _to(e.y)))
            if d < keep_out + _to(t.GetWidth()) / 2:
                return False
    for fp in board.GetFootprints():
        for pad in fp.Pads():
            if pad.GetNetCode() == netcode:
                continue
            if (layer is not None
                    and pad.GetAttribute() != pcbnew.PAD_ATTRIB_PTH
                    and not pad.IsOnLayer(layer)):
                continue
            bb = pad.GetBoundingBox()
            # rectangle distance, not circumscribed circle — a circle test
            # around a large inductor pad vetoes everything within ~3 mm
            dx = max(abs(xy[0] - _to(bb.GetCenter().x)) - _to(bb.GetWidth()) / 2, 0)
            dy = max(abs(xy[1] - _to(bb.GetCenter().y)) - _to(bb.GetHeight()) / 2, 0)
            if math.hypot(dx, dy) < keep_out:
                return False
    return True


def _clear_path(board, netcode, a, b, keep_out, layer=None, step=0.3):
    """True when the whole a->b segment stays keep_out clear of foreign
    copper on `layer` (sampled every `step` mm)."""
    n = max(1, int(_dist(a, b) / step))
    for i in range(n + 1):
        f = i / n
        if not _clear_spot(board, netcode,
                           (a[0] + (b[0] - a[0]) * f,
                            a[1] + (b[1] - a[1]) * f), keep_out, layer):
            return False
    return True


def _nearest_net_via(board, netcode, pos, reach=2.0):
    """Center (x, y) of the closest same-net via within reach, else None."""
    best, bd = None, reach
    for t in board.GetTracks():
        if t.GetClass() != "PCB_VIA" or t.GetNetCode() != netcode:
            continue
        xy = (_to(t.GetPosition().x), _to(t.GetPosition().y))
        d = _dist(xy, pos)
        if d < bd:
            best, bd = xy, d
    return best


def _net_vias_near(board, netcode, pos, reach=3.0):
    """Same-net via centers within reach of pos, nearest first."""
    out = []
    for t in board.GetTracks():
        if t.GetClass() != "PCB_VIA" or t.GetNetCode() != netcode:
            continue
        xy = (_to(t.GetPosition().x), _to(t.GetPosition().y))
        if _dist(xy, pos) <= reach:
            out.append(xy)
    return sorted(out, key=lambda xy: _dist(xy, pos))


def _bfs_path(board, nc, a, b, layer, keep, step=0.4):
    """Coarse maze route from a to b on one layer: BFS over a step-mm grid,
    cells blocked by foreign copper. Returns a rectilinear polyline (incl.
    endpoints) or None. This is the escape when every fixed path shape is
    blocked — e.g. threading a via fanout field under an MCU."""
    x0 = min(a[0], b[0]) - 5.0
    y0 = min(a[1], b[1]) - 5.0
    x1 = max(a[0], b[0]) + 5.0
    y1 = max(a[1], b[1]) + 5.0
    bb = board.GetBoardEdgesBoundingBox()
    x0 = max(x0, _to(bb.GetLeft()) + 0.5)
    y0 = max(y0, _to(bb.GetTop()) + 0.5)
    x1 = min(x1, _to(bb.GetRight()) - 0.5)
    y1 = min(y1, _to(bb.GetBottom()) - 0.5)
    nx = int((x1 - x0) / step) + 1
    ny = int((y1 - y0) / step) + 1
    if nx * ny > 40000:
        return None

    def cell(p):
        return (min(nx - 1, max(0, int(round((p[0] - x0) / step)))),
                min(ny - 1, max(0, int(round((p[1] - y0) / step)))))

    def pt(c):
        return (x0 + c[0] * step, y0 + c[1] * step)

    start, goal = cell(a), cell(b)
    blocked_cache = {}

    def is_blocked(c):
        if c not in blocked_cache:
            blocked_cache[c] = not _clear_spot(board, nc, pt(c), keep, layer)
        return blocked_cache[c]

    from collections import deque
    came = {start: None}
    q = deque([start])
    while q:
        c = q.popleft()
        if c == goal:
            break
        for d in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            n = (c[0] + d[0], c[1] + d[1])
            if (0 <= n[0] < nx and 0 <= n[1] < ny and n not in came
                    and (n == goal or not is_blocked(n))):
                came[n] = c
                q.append(n)
    if goal not in came:
        return None
    cells = []
    c = goal
    while c is not None:
        cells.append(c)
        c = came[c]
    cells.reverse()
    # merge colinear runs into corner points
    pts = [a]
    for i in range(1, len(cells) - 1):
        dp = (cells[i][0] - cells[i - 1][0], cells[i][1] - cells[i - 1][1])
        dn = (cells[i + 1][0] - cells[i][0], cells[i + 1][1] - cells[i][1])
        if dp != dn:
            pts.append(pt(cells[i]))
    pts.append(b)
    return pts


def _try_link(board, net, a, b, layer, width=0.3, bfs=False):
    """Lay a same-net link from a to b on layer — straight, L-shaped, or
    (when bfs=True) maze-routed. Returns True when tracks were added."""
    nc = net.GetNetCode()
    keep = width / 2 + _min_clearance(board) + 0.05
    path = None
    for cand in ((a, b),
                 (a, (a[0], b[1]), b),
                 (a, (b[0], a[1]), b)):
        if all(_clear_path(board, nc, p, q, keep, layer)
               for p, q in zip(cand, cand[1:])):
            path = cand
            break
    if path is None and bfs:
        path = _bfs_path(board, nc, a, b, layer, keep)
    if path is None:
        return False
    for p, q in zip(path, path[1:]):
        if _dist(p, q) < 1e-3:
            continue
        t = pcbnew.PCB_TRACK(board)
        t.SetStart(pcbnew.VECTOR2I(_mm(p[0]), _mm(p[1])))
        t.SetEnd(pcbnew.VECTOR2I(_mm(q[0]), _mm(q[1])))
        t.SetWidth(_mm(width))
        t.SetLayer(layer)
        t.SetNet(net)
        board.Add(t)
    return True


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


def main():
    pcb, netlist, drc_path, mode = sys.argv[1:5]
    board = pcbnew.LoadBoard(pcb)
    try:
        design = json.load(open(netlist, encoding="utf-8"))
    except Exception:
        design = {}
    try:
        drc = json.load(open(drc_path, encoding="utf-8"))
    except Exception:
        drc = {}
    applied, open_tasks = [], []
    if mode in ("heal", "all"):
        applied += heal_micro_gaps(board, drc)
    if mode in ("vias", "all"):
        applied += add_parallel_vias(board, design, open_tasks)
    if applied:
        board.BuildConnectivity()
        try:
            pcbnew.ZONE_FILLER(board).Fill(board.Zones())
        except Exception:
            pass
        pcbnew.SaveBoard(pcb, board)
    print(json.dumps({"applied": applied, "open": open_tasks}))


if __name__ == "__main__":
    main()
