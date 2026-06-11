#!/usr/bin/env python3
"""EMC ground stitching — add a GND via fence + interior stitching grid to a
routed board, DRC-safely. Runs under KiCad's bundled Python (needs pcbnew).

Why: on a multi-layer board the return current and the shield both depend on the
ground planes being tied together densely. A perimeter via fence (a "via wall"
just inside the board edge) contains fields and gives edge currents a low-
impedance path; an interior stitching grid bonds the GND pours/planes so the
return path follows the signal and cavity resonances are pushed up in frequency.
This is the standard EMC practice (Ott; Montrose, *EMC and the PCB*) and is one
of the few EMC measures that is purely GEOMETRIC and so achievable headless.

Pitch: stitching should be <= lambda/20 at the highest frequency of concern so
the wall reads as continuous. Pass `max_freq_hz` and the pitch is derived
(lambda_guided / 20); otherwise a conservative fixed pitch is used.

Every via is placed only where it clears all non-GND copper (pads + tracks) and
every existing hole, so the board stays DRC-clean. Vias land on GND, span all
copper layers, and connect the planes/pours already present.
"""
from __future__ import annotations

import json
import math
import sys

import pcbnew

C0 = 299_792_458.0  # m/s


def _pitch_mm(max_freq_hz: float, er: float, default: float) -> float:
    if max_freq_hz and max_freq_hz > 0:
        lam_g = C0 / (max_freq_hz * math.sqrt(er)) * 1000.0  # mm, guided
        return max(1.0, round(lam_g / 20.0, 2))
    return default


def _gnd_netcode(board, gnd_name: str):
    for code, ni in board.GetNetInfo().NetsByNetcode().items():
        if ni.GetNetname() == gnd_name:
            return code
    return None


def _obstacles(board, gnd_code: int):
    """(x,y,clear_radius_nm) circles for every non-GND pad/track to avoid, plus
    (x,y,drill_radius_nm) holes for hole-to-hole spacing."""
    ds = board.GetDesignSettings()
    base = ds.GetSmallestClearanceValue() if hasattr(ds, "GetSmallestClearanceValue") \
        else pcbnew.FromMM(0.2)
    # be conservative: clear at least 0.2 mm plus a safety margin, so a stitching
    # via near (but not on) signal copper still passes DRC on a dense board. It is
    # better to drop a via than to add one that violates.
    clr = max(base, pcbnew.FromMM(0.2)) + pcbnew.FromMM(0.1)
    circles, holes = [], []
    for fp in board.GetFootprints():
        for pad in fp.Pads():
            pos = pad.GetPosition()
            r = max(pad.GetBoundingRadius(), 1)
            if pad.GetNetCode() != gnd_code:
                circles.append((pos.x, pos.y, r + clr))
            d = pad.GetDrillSize()
            if d.x > 0:
                holes.append((pos.x, pos.y, d.x // 2))
    for t in board.GetTracks():
        if t.GetClass() == "PCB_VIA":
            pos = t.GetPosition()
            holes.append((pos.x, pos.y, t.GetDrillValue() // 2))
            if t.GetNetCode() != gnd_code:
                # PCB_VIA.GetWidth() needs a copper-layer arg in KiCad 10 (vias can
                # be per-layer); a through via is uniform, so query F.Cu.
                try:
                    vw = t.GetWidth(pcbnew.F_Cu)
                except TypeError:
                    vw = t.GetWidth()
                circles.append((pos.x, pos.y, vw // 2 + clr))
        elif t.GetClass() == "PCB_TRACK" and t.GetNetCode() != gnd_code:
            # sample the track densely (every 0.5 mm) so a stitching via can't slip
            # between samples next to a thin trace
            a, b = t.GetStart(), t.GetEnd()
            n = max(1, int(math.hypot(b.x - a.x, b.y - a.y) // pcbnew.FromMM(0.5)))
            for i in range(n + 1):
                x = a.x + (b.x - a.x) * i // n
                y = a.y + (b.y - a.y) * i // n
                circles.append((x, y, t.GetWidth() // 2 + clr))
    return circles, holes, clr


def add_stitching(board, gnd_name="GND", via_d_mm=0.6, via_drill_mm=0.3,
                  fence_inset_mm=0.5, max_freq_hz=0.0, er=4.2,
                  default_pitch_mm=3.0, interior=True) -> dict:
    gnd_code = _gnd_netcode(board, gnd_name)
    if gnd_code is None:
        return {"ok": False, "reason": f"no '{gnd_name}' net on board"}
    bb = board.GetBoardEdgesBoundingBox()
    x0, y0, x1, y1 = bb.GetLeft(), bb.GetTop(), bb.GetRight(), bb.GetBottom()
    pitch = pcbnew.FromMM(_pitch_mm(max_freq_hz, er, default_pitch_mm))
    via_r = pcbnew.FromMM(via_d_mm) // 2
    drill = pcbnew.FromMM(via_drill_mm)
    circles, holes, clr = _obstacles(board, gnd_code)
    ds = board.GetDesignSettings()
    # hole-to-hole: the real min plus margin so a stitch hole can't crowd a routed
    # via or pad hole on a dense board.
    try:
        h2h = ds.GetHoleToHoleMin() + pcbnew.FromMM(0.1)
    except Exception:
        h2h = pcbnew.FromMM(0.35)

    # copper must stay this far from the board edge — derive the via center keep-out
    # from the real constraint so the fence can't violate edge clearance.
    try:
        edge_clr = ds.GetCopperEdgeClearance()
    except Exception:
        edge_clr = pcbnew.FromMM(0.5)
    edge_keepout = edge_clr + via_r
    # fence inset: honor the caller's request but never closer than the keep-out.
    inset = max(pcbnew.FromMM(fence_inset_mm), edge_keepout + pcbnew.FromMM(0.1))

    def clears(x, y):
        if not (x0 + edge_keepout <= x <= x1 - edge_keepout
                and y0 + edge_keepout <= y <= y1 - edge_keepout):
            return False
        for cx, cy, r in circles:
            if (x - cx) ** 2 + (y - cy) ** 2 < (r + via_r) ** 2:
                return False
        for cx, cy, r in holes:
            if (x - cx) ** 2 + (y - cy) ** 2 < (r + drill // 2 + h2h) ** 2:
                return False
        return True

    placed = {"fence": 0, "interior": 0}
    new_holes = []

    def place(x, y, kind):
        if not clears(x, y):
            return
        for hx, hy in new_holes:                 # keep our own vias apart
            if (x - hx) ** 2 + (y - hy) ** 2 < (pitch * 0.7) ** 2:
                return
        v = pcbnew.PCB_VIA(board)
        v.SetPosition(pcbnew.VECTOR2I(x, y))
        v.SetWidth(pcbnew.FromMM(via_d_mm))
        v.SetDrill(drill)
        v.SetNetCode(gnd_code)
        v.SetViaType(pcbnew.VIATYPE_THROUGH)
        board.Add(v)
        new_holes.append((x, y))
        placed[kind] += 1

    # perimeter fence (inset rectangle)
    fx0, fy0, fx1, fy1 = x0 + inset, y0 + inset, x1 - inset, y1 - inset
    x = fx0
    while x <= fx1:
        place(x, fy0, "fence")
        place(x, fy1, "fence")
        x += pitch
    y = fy0 + pitch
    while y < fy1:
        place(fx0, y, "fence")
        place(fx1, y, "fence")
        y += pitch

    # interior grid (offset half a pitch so it interleaves with the fence)
    if interior:
        y = fy0 + pitch
        while y < fy1:
            x = fx0 + pitch
            while x < fx1:
                place(x, y, "interior")
                x += pitch
            y += pitch

    return {"ok": True, "gnd": gnd_name, "pitch_mm": round(pcbnew.ToMM(pitch), 2),
            "vias_added": placed["fence"] + placed["interior"], **placed}


def main():
    pcb = sys.argv[1]
    max_freq = float(sys.argv[2]) if len(sys.argv) > 2 else 0.0
    interior = (sys.argv[3].lower() != "fence") if len(sys.argv) > 3 else True
    board = pcbnew.LoadBoard(pcb)
    res = add_stitching(board, max_freq_hz=max_freq, interior=interior)
    if res.get("ok"):
        board.BuildConnectivity()
        # refill zones so every non-GND plane (e.g. an In2.Cu power pour) opens a
        # clearance hole around each new GND stitching via — without this the vias
        # short/violate the power plane they pass through.
        try:
            pcbnew.ZONE_FILLER(board).Fill(board.Zones())
        except Exception:
            pass
        pcbnew.SaveBoard(pcb, board)
    print(json.dumps(res))


if __name__ == "__main__":
    main()
