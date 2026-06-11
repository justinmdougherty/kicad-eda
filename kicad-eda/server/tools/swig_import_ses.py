#!/usr/bin/env python3
"""Import a Freerouting .ses session back into the board, add GND pours on the
outer layers to unify the ground net, refill all zones, and save.

The inner GND plane (In1.Cu) can be pinched into isolated islands by the
antipads of the non-GND vias the router drops. Adding GND pours on F.Cu/B.Cu
*after* routing — so the router still used both signal layers freely — lets the
router's existing GND vias bridge those islands through the outer copper, which
reliably clears the "Zone GND <-> Zone GND" unconnected flag.

Usage: <kicad-python> swig_import_ses.py <board.kicad_pcb> <routed.ses>"""
import sys
import pcbnew


def mm(v):
    return pcbnew.FromMM(float(v))


def add_gnd_pour(board, layer_name, gnd_code, inset=0.3):
    bb = board.GetBoardEdgesBoundingBox()
    x0, y0 = pcbnew.ToMM(bb.GetLeft()), pcbnew.ToMM(bb.GetTop())
    w, h = pcbnew.ToMM(bb.GetWidth()), pcbnew.ToMM(bb.GetHeight())
    z = pcbnew.ZONE(board)
    z.SetLayer(board.GetLayerID(layer_name))
    z.SetNetCode(gnd_code)
    z.SetIsFilled(False)
    z.SetAssignedPriority(0)
    # solid (full) pad connection, matching the inner planes — thermal-relief
    # spokes on a signal-layer pour can starve near tight pads (starved_thermal).
    try:
        z.SetPadConnection(pcbnew.ZONE_CONNECTION_FULL)
    except Exception:
        pass
    pts = [(x0 + inset, y0 + inset), (x0 + w - inset, y0 + inset),
           (x0 + w - inset, y0 + h - inset), (x0 + inset, y0 + h - inset)]
    o = z.Outline()
    o.NewOutline()
    for cx, cy in pts:
        o.Append(mm(cx), mm(cy))
    board.Add(z)


pcb, ses = sys.argv[1], sys.argv[2]
board = pcbnew.LoadBoard(pcb)
ok = None
for attempt in (lambda: pcbnew.ImportSpecctraSES(board, ses),
                lambda: pcbnew.ImportSpecctraSES(ses)):
    try:
        ok = attempt()
        break
    except TypeError:
        continue

# unify GND across the board: outer-layer pours bridge inner-plane islands.
try:
    gnd_code = board.GetNetcodeFromNetname("GND")
    if gnd_code > 0:
        existing = {(board.GetLayerName(z.GetLayer()), z.GetNetname())
                    for z in board.Zones()}
        for layer in ("F.Cu", "B.Cu"):
            if (layer, "GND") not in existing:
                add_gnd_pour(board, layer, gnd_code)
except Exception:
    pass

try:
    board.BuildConnectivity()
    pcbnew.ZONE_FILLER(board).Fill(board.Zones())
except Exception:
    pass
pcbnew.SaveBoard(pcb, board)
print("ses_import", ok, "tracks", len(board.GetTracks()))
