#!/usr/bin/env python3
"""Import a Freerouting .ses session back into the board, add GND pours on the
outer layers to unify the ground net, refill all zones, and save.

The inner GND plane (In1.Cu) can be pinched into isolated islands by the
antipads of the non-GND vias the router drops. Adding GND pours on F.Cu/B.Cu
*after* routing — so the router still used both signal layers freely — lets the
router's existing GND vias bridge those islands through the outer copper, which
reliably clears the "Zone GND <-> Zone GND" unconnected flag.

The pours follow the board outline (F3-16): the Edge.Cuts polygon inset by
POUR_INSET_MM (board_outline.plane_polyset; cutouts become holes, each piece
of a narrow neck gets a zone of its own), filled in place. A plain rectangle
keeps the legacy pour, the Edge.Cuts bounding box inset by POUR_INSET_MM, bit
for bit (that bbox includes half the edge line width, so the polygon inset
would move it by 0.05 mm); an outline that cannot be built falls back to that
rectangle with a warning.

Output: the LAST stdout line is JSON
  {"ok": bool, "tracks": int, "vias": int, "warnings": [str], ["error": str]}
where "tracks" counts track/arc segments and "vias" counts vias. A GND-pour or
zone-fill problem is a warning (the routed copper is still saved); a missing or
unreadable board or SES and a failed SES import are ok=false + "error" and exit
1 (the board is left untouched). The legacy human line ``ses_import True
tracks N`` (N = tracks + vias, as before) is still printed BEFORE the JSON for
older callers.

Usage: <kicad-python> swig_import_ses.py <board.kicad_pcb> <routed.ses>"""
from __future__ import annotations

import json
import os
import sys

import kicad_quiet  # noqa: F401  (before pcbnew: no modal wx dialogs)
import pcbnew

import board_outline as bo

GND_NET = "GND"
OUTER_LAYERS = ("F.Cu", "B.Cu")
POUR_INSET_MM = 0.3
USAGE_EXIT = 2


def mm(v):
    return pcbnew.FromMM(float(v))


def bbox_rect(board: pcbnew.BOARD, inset: float) -> list[tuple[float, float]]:
    """The legacy pour: the Edge.Cuts bounding box inset by `inset` mm (sheet
    mm, the expressions kept verbatim: FromMM truncates)."""
    bb = board.GetBoardEdgesBoundingBox()
    x0, y0 = pcbnew.ToMM(bb.GetLeft()), pcbnew.ToMM(bb.GetTop())
    w, h = pcbnew.ToMM(bb.GetWidth()), pcbnew.ToMM(bb.GetHeight())
    return [(x0 + inset, y0 + inset), (x0 + w - inset, y0 + inset),
            (x0 + w - inset, y0 + h - inset), (x0 + inset, y0 + h - inset)]


def _plain_rect(ps: pcbnew.SHAPE_POLY_SET) -> bool:
    """KiCad's Edge.Cuts polygon is one axis-aligned 4-corner ring, no holes."""
    if ps.OutlineCount() != 1 or ps.HoleCount(0) != 0 or ps.COutline(0).PointCount() != 4:
        return False
    ring = ps.COutline(0)
    pts = [ring.CPoint(i) for i in range(4)]
    return all(a.x == b.x or a.y == b.y for a, b in zip(pts, pts[1:] + pts[:1], strict=True))


def pour_polyset(board: pcbnew.BOARD, inset: float) -> pcbnew.SHAPE_POLY_SET | None:
    """The outline pour: Edge.Cuts inset by `inset` mm, or None for a plain
    rectangle (the legacy bbox rectangle). Raises board_outline.
    OutlineBuildError when Edge.Cuts is not one closed outline."""
    if _plain_rect(bo.polygon_outlines(board)):
        return None
    return bo.plane_polyset(board, inset)


def pieces(ps: pcbnew.SHAPE_POLY_SET) -> list[pcbnew.SHAPE_POLY_SET]:
    """One polygon set per outline of `ps` (with its holes). A zone persists
    only its first outline (SaveBoard writes Polygon(0), probed on 9.0.4), so
    every piece of a split pour needs a zone of its own."""
    out = []
    for i in range(ps.OutlineCount()):
        piece = pcbnew.SHAPE_POLY_SET()
        idx = piece.AddOutline(ps.COutline(i))
        for h in range(ps.HoleCount(i)):
            piece.AddHole(ps.CHole(i, h), idx)
        out.append(piece)
    return out


def _gnd_zone(board: pcbnew.BOARD, layer_name: str, gnd_code: int) -> tuple:
    """(an empty unfilled GND zone on `layer_name`, warnings)."""
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
        return z, [f"{layer_name} GND pour: full pad connection not set "
                   f"({type(e).__name__}: {e}); thermal reliefs remain"]
    return z, []


def add_gnd_pour(board: pcbnew.BOARD, layer_name: str, gnd_code: int,
                 inset: float = POUR_INSET_MM) -> list[str]:
    """Add unfilled GND pours on ``layer_name`` over the board outline inset
    by ``inset`` mm (see the module doc), one zone per piece of it. Returns
    a list of warnings (empty on a clean add)."""
    warnings = []
    try:
        polyset = pour_polyset(board, inset)
    except bo.OutlineBuildError as e:
        polyset = None
        warnings.append(f"{layer_name} GND pour: the board outline is unusable ({e}); "
                        f"poured the Edge.Cuts bounding box inset by {inset:g} mm instead")
    for piece in [None] if polyset is None else pieces(polyset):
        z, zone_warnings = _gnd_zone(board, layer_name, gnd_code)
        warnings += [w for w in zone_warnings if w not in warnings]
        if piece is None:
            o = z.Outline()
            o.NewOutline()
            for cx, cy in bbox_rect(board, inset):
                o.Append(mm(cx), mm(cy))
        else:
            bo.fill_zone_outline(z, piece)    # in place: never ZONE.SetOutline
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


def _fail(error: str) -> int:
    print(json.dumps({"ok": False, "tracks": 0, "vias": 0, "warnings": [], "error": error}))
    return 1


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return USAGE_EXIT
    pcb, ses = sys.argv[1], sys.argv[2]
    # pcbnew reports a missing/unparseable SES through a GUI error dialog,
    # which blocks forever headless — check what we can before calling it.
    if not os.path.isfile(ses):
        return _fail(f"SES file not found: {ses}")
    try:
        board = bo.load_board(pcb)        # missing: refused before pcbnew; None: an error
    except (FileNotFoundError, RuntimeError) as e:
        return _fail(f"cannot load the board: {e}")
    try:
        ok = import_ses(board, ses)
    except Exception as e:
        return _fail(f"SES import raised {type(e).__name__}: {e}")
    if not ok:
        return _fail(f"ImportSpecctraSES returned False for {ses}")
    warnings = unify_gnd(board) + refill_zones(board)
    pcbnew.SaveBoard(pcb, board)
    tracks, vias = count_copper(board)
    print("ses_import", ok, "tracks", tracks + vias)   # legacy line, same as before
    print(json.dumps({"ok": True, "tracks": tracks, "vias": vias,
                      "warnings": warnings}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
