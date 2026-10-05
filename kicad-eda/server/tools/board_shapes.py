#!/usr/bin/env python3
"""Board-level shapes for the headless builder: outline, copper planes,
keep-out rule areas."""
import kicad_quiet  # noqa: F401  (before pcbnew: no modal wx dialogs)
import pcbnew


def mm(v):
    return pcbnew.FromMM(float(v))


def vec(x, y):
    return pcbnew.VECTOR2I(mm(x), mm(y))


def add_keepout(board, x0, y0, w, h, name="ANTENNA"):
    """Add an antenna keep-out rule area over the rectangle (all copper layers):
    no copper pour and no vias, so the ground/power planes clip AROUND it and the
    radiator sits over bare substrate (the #1 PCB-antenna layout rule — a ground
    plane under the antenna detunes and shorts the near field). Tracks are still
    allowed so the antenna/feed conductor itself can live in the region."""
    zone = pcbnew.ZONE(board)
    try:
        zone.SetIsRuleArea(True)
        zone.SetDoNotAllowZoneFills(True)   # KiCad 10 name for "no copper pour"
        zone.SetDoNotAllowVias(True)
        zone.SetDoNotAllowTracks(False)
        try:
            zone.SetDoNotAllowPads(True)
        except Exception:
            pass
    except Exception:
        return None
    ls = pcbnew.LSET()
    for lid in (pcbnew.F_Cu, pcbnew.In1_Cu, pcbnew.In2_Cu, pcbnew.B_Cu):
        ls.AddLayer(lid)
    zone.SetLayerSet(ls)
    outline = zone.Outline()
    outline.NewOutline()
    for cx, cy in ((x0, y0), (x0 + w, y0), (x0 + w, y0 + h), (x0, y0 + h)):
        outline.Append(mm(cx), mm(cy))
    zone.SetZoneName(name)
    board.Add(zone)
    return zone


def add_outline(board, w, h, x0=0.0, y0=0.0):
    pts = [(x0, y0), (x0 + w, y0), (x0 + w, y0 + h), (x0, y0 + h), (x0, y0)]
    for (ax, ay), (bx, by) in zip(pts, pts[1:]):
        seg = pcbnew.PCB_SHAPE(board)
        seg.SetShape(pcbnew.SHAPE_T_SEGMENT)
        seg.SetStart(vec(ax, ay))
        seg.SetEnd(vec(bx, by))
        seg.SetLayer(pcbnew.Edge_Cuts)
        seg.SetWidth(mm(0.1))
        board.Add(seg)


def add_plane(board, layer_id, ni, w, h, x0, y0, inset=0.3):
    """Add a rectangular copper pour on `layer_id` tied to net `ni`, inset from
    the board edge by `inset` mm (keeps copper off the edge for clean milling)."""
    zone = pcbnew.ZONE(board)
    zone.SetLayer(layer_id)
    zone.SetNetCode(ni.GetNetCode())
    zone.SetIsFilled(False)
    zone.SetAssignedPriority(0)
    # pad-connection: thermal reliefs by default give better hand-solder/escape,
    # but for solid planes we want full (solid) connections to vias.
    try:
        zone.SetPadConnection(pcbnew.ZONE_CONNECTION_FULL)
    except Exception:
        pass
    corners = [(x0 + inset, y0 + inset), (x0 + w - inset, y0 + inset),
               (x0 + w - inset, y0 + h - inset), (x0 + inset, y0 + h - inset)]
    outline = zone.Outline()
    outline.NewOutline()
    for cx, cy in corners:
        outline.Append(mm(cx), mm(cy))
    board.Add(zone)
    return zone


