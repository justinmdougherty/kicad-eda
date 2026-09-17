#!/usr/bin/env python3
"""Import a Freerouting .ses session back into the board, add GND pours on the
outer layers to unify the ground net, refill all zones, and save.

The inner GND plane (In1.Cu) can be pinched into isolated islands by the
antipads of the non-GND vias the router drops. Adding GND pours on F.Cu/B.Cu
*after* routing — so the router still used both signal layers freely — lets the
router's existing GND vias bridge those islands through the outer copper, which
reliably clears the "Zone GND <-> Zone GND" unconnected flag.

Output: the LAST stdout line is JSON
  {"ok": bool, "tracks": int, "vias": int, "warnings": [str], ["error": str]}
where "tracks" counts track/arc segments and "vias" counts vias. A GND-pour or
zone-fill problem is a warning (the routed copper is still saved); a failed SES
import is ok=false + "error" and exit 1 (the board is left untouched). The
legacy human line ``ses_import True tracks N`` (N = tracks + vias, as before)
is still printed BEFORE the JSON for older callers.

Usage: <kicad-python> swig_import_ses.py <board.kicad_pcb> <routed.ses>"""
import json
import os
import sys

import pcbnew

GND_NET = "GND"
OUTER_LAYERS = ("F.Cu", "B.Cu")
POUR_INSET_MM = 0.3
USAGE_EXIT = 2


def mm(v):
    return pcbnew.FromMM(float(v))


def add_gnd_pour(board, layer_name, gnd_code, inset=POUR_INSET_MM):
    """Add an unfilled full-board GND pour on ``layer_name``. Returns a list
    of warnings (empty on a clean add)."""
    warnings = []
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
    except Exception as e:  # API differs across pcbnew versions
        warnings.append(f"{layer_name} GND pour: full pad connection not set "
                        f"({type(e).__name__}: {e}); thermal reliefs remain")
    pts = [(x0 + inset, y0 + inset), (x0 + w - inset, y0 + inset),
           (x0 + w - inset, y0 + h - inset), (x0 + inset, y0 + h - inset)]
    o = z.Outline()
    o.NewOutline()
    for cx, cy in pts:
        o.Append(mm(cx), mm(cy))
    board.Add(z)
    return warnings


def import_ses(board, ses) -> bool:
    """Run ImportSpecctraSES with whichever signature this pcbnew exposes.
    Returns its bool result; raises RuntimeError if neither signature works."""
    last = None
    for attempt in (lambda: pcbnew.ImportSpecctraSES(board, ses),
                    lambda: pcbnew.ImportSpecctraSES(ses)):
        try:
            return bool(attempt())
        except TypeError as e:
            last = e
    raise RuntimeError(f"ImportSpecctraSES accepted neither (board, ses) nor "
                       f"(ses): {last}")


def unify_gnd(board) -> list:
    """Outer-layer GND pours so the router's GND vias bridge inner-plane
    islands. Returns warnings; never raises."""
    try:
        gnd_code = board.GetNetcodeFromNetname(GND_NET)
    except Exception as e:
        return [f"GND pour skipped: cannot look up net '{GND_NET}' "
                f"({type(e).__name__}: {e})"]
    if gnd_code <= 0:
        return [f"GND pour skipped: no '{GND_NET}' net on the board"]
    warnings = []
    try:
        existing = {(board.GetLayerName(z.GetLayer()), z.GetNetname())
                    for z in board.Zones()}
        for layer in OUTER_LAYERS:
            if (layer, GND_NET) not in existing:
                warnings += add_gnd_pour(board, layer, gnd_code)
    except Exception as e:
        warnings.append(f"GND pour failed: {type(e).__name__}: {e}")
    return warnings


def refill_zones(board) -> list:
    """Rebuild connectivity and fill every zone. Returns warnings."""
    try:
        board.BuildConnectivity()
        pcbnew.ZONE_FILLER(board).Fill(board.Zones())
    except Exception as e:
        return [f"zone fill failed: {type(e).__name__}: {e} — zones saved "
                f"unfilled; refill in KiCad (B) or re-run DRC to verify"]
    return []


def count_copper(board):
    """(track/arc segments, vias) on the board."""
    vias = sum(1 for t in board.GetTracks() if t.GetClass() == "PCB_VIA")
    return len(board.GetTracks()) - vias, vias


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return USAGE_EXIT
    pcb, ses = sys.argv[1], sys.argv[2]
    # pcbnew reports a missing/unparseable SES through a GUI error dialog,
    # which blocks forever headless — check what we can before calling it.
    if not os.path.isfile(ses):
        print(json.dumps({"ok": False, "tracks": 0, "vias": 0, "warnings": [],
                          "error": f"SES file not found: {ses}"}))
        return 1
    board = pcbnew.LoadBoard(pcb)
    try:
        ok = import_ses(board, ses)
    except Exception as e:
        print(json.dumps({"ok": False, "tracks": 0, "vias": 0, "warnings": [],
                          "error": f"SES import raised {type(e).__name__}: {e}"}))
        return 1
    if not ok:
        print(json.dumps({"ok": False, "tracks": 0, "vias": 0, "warnings": [],
                          "error": f"ImportSpecctraSES returned False for {ses}"}))
        return 1
    warnings = unify_gnd(board) + refill_zones(board)
    pcbnew.SaveBoard(pcb, board)
    tracks, vias = count_copper(board)
    print("ses_import", ok, "tracks", tracks + vias)   # legacy line, same as before
    print(json.dumps({"ok": True, "tracks": tracks, "vias": vias,
                      "warnings": warnings}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
