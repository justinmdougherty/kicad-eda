#!/usr/bin/env python3
"""Board-level shapes for the headless builder: outline, copper planes and
keep-out rule areas, built on board_outline (the one pcbnew 9/10 adapter).

The legacy rectangle helpers (add_outline, add_plane, add_keepout) keep their
signatures and their rectangle geometry, so existing boards do not change;
add_keepout now works on KiCad 9 (it used a 10.x-only setter and silently
returned None there) and raises instead of returning None. The outline-aware
helpers (add_plane_outline, add_keepout_polygon) follow Edge.Cuts. Zone
outlines are always filled in place (zone.Outline()), never replaced.
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

import kicad_quiet  # noqa: F401  (before pcbnew: no modal wx dialogs)
import pcbnew

import board_outline as bo
import outline as ol

PLANE_INSET_MM = 0.3


def mm(v: float) -> int:
    return pcbnew.FromMM(float(v))


def vec(x: float, y: float) -> Any:
    return pcbnew.VECTOR2I(mm(x), mm(y))


def _rect_corners(x0: float, y0: float, w: float, h: float) -> list[tuple[float, float]]:
    return [(x0, y0), (x0 + w, y0), (x0 + w, y0 + h), (x0, y0 + h)]


def _fill_points(zone: Any, points: Iterable[Sequence[float]]) -> Any:
    """One outline through `points` (sheet mm, quantised with pcbnew.FromMM as the
    legacy code was), filled in place."""
    outline = zone.Outline()
    outline.RemoveAllContours()
    outline.NewOutline()
    for x, y in points:
        outline.Append(mm(x), mm(y))
    if not outline.Area() > 0:
        raise bo.OutlineBuildError(f"zone {zone.GetZoneName()!r} outline has zero area")
    return zone


def _layer_set(board: Any, layers: Any) -> Any:
    """None -> every enabled copper layer; an LSET as is; else layer ids or names."""
    if layers is None:
        return bo.copper_lset(board)
    if isinstance(layers, pcbnew.LSET):
        return layers
    ls = pcbnew.LSET()
    for layer in layers:
        lid = board.GetLayerID(layer) if isinstance(layer, str) else int(layer)
        if lid < 0:
            raise ValueError(f"unknown layer {layer!r}")
        ls.AddLayer(lid)
    return ls


def add_outline(board: Any, w: float, h: float, x0: float = 0.0, y0: float = 0.0) -> list:
    """The w x h rectangle on Edge.Cuts at sheet (x0, y0): four 0.1 mm segments,
    drawn and checked by board_outline.draw_outline. The corners are quantised
    with pcbnew.FromMM (it truncates) exactly as the legacy code was, so the
    saved coordinates do not move by even 1 nm."""
    xa, xb, ya, yb = (mm(v) / bo.NM_PER_MM for v in (x0, x0 + w, y0, y0 + h))
    rect = ol.Loop(((xa, ya), (xb, ya), (xb, yb), (xa, yb)))
    return bo.draw_outline(board, ol.Outline(rect), 0.0, 0.0)


def _plane_zone(board: Any, layer_id: int, net: Any) -> Any:
    zone = pcbnew.ZONE(board)
    zone.SetLayer(layer_id)
    zone.SetNetCode(net.GetNetCode() if hasattr(net, "GetNetCode") else int(net))
    zone.SetIsFilled(False)
    zone.SetAssignedPriority(0)
    # solid (not thermal-relief) connections: planes tie straight into vias
    zone.SetPadConnection(pcbnew.ZONE_CONNECTION_FULL)
    return zone


def add_plane(board: Any, layer_id: int, ni: Any, w: float, h: float, x0: float, y0: float,
              inset: float = PLANE_INSET_MM) -> Any:
    """A rectangular copper pour on `layer_id` tied to net `ni`, inset from the
    w x h board edge by `inset` mm (keeps copper off the edge for clean milling)."""
    zone = _plane_zone(board, layer_id, ni)
    # the legacy expressions verbatim: FromMM truncates, so another float
    # summation order could move a corner by 1 nm
    _fill_points(zone, [(x0 + inset, y0 + inset), (x0 + w - inset, y0 + inset),
                        (x0 + w - inset, y0 + h - inset), (x0 + inset, y0 + h - inset)])
    board.Add(zone)
    return zone


def add_plane_outline(board: Any, layer_id: int, net: Any, inset_mm: float = PLANE_INSET_MM,
                      *, local_clearance_mm: float | None = None) -> Any:
    """A copper pour on `layer_id` (net: NETINFO_ITEM or net code) covering the
    Edge.Cuts polygon inset by `inset_mm`, cutouts and every piece of a narrow
    neck included. `local_clearance_mm` sets the zone's own clearance (an
    outer mixed pour must not exceed the smallest CPWG ground gap there).
    Raises board_outline.OutlineBuildError when Edge.Cuts is not closed."""
    zone = _plane_zone(board, layer_id, net)
    bo.fill_zone_outline(zone, bo.plane_polyset(board, inset_mm))
    if local_clearance_mm is not None:
        if not local_clearance_mm >= 0:
            raise ValueError(f"local_clearance_mm must be >= 0, got {local_clearance_mm!r}")
        zone.SetLocalClearance(bo.nm(local_clearance_mm))
    board.Add(zone)
    return zone


def add_keepout_polygon(board: Any, polyset_or_points_sheet_mm: Any, name: str,
                        layers: Any = None, no_tracks: bool = False, no_vias: bool = False,
                        no_pour: bool = True, *, no_pads: bool = False,
                        no_footprints: bool = False) -> Any:
    """A keep-out rule area over a SHAPE_POLY_SET or a list of (x, y) sheet-mm
    points, on `layers` (default: every enabled copper layer). The pour flag
    is set version-safely (board_outline.set_no_pour); never returns None."""
    zone = pcbnew.ZONE(board)
    zone.SetIsRuleArea(True)
    bo.set_no_pour(zone, no_pour)
    zone.SetDoNotAllowTracks(bool(no_tracks))
    zone.SetDoNotAllowVias(bool(no_vias))
    zone.SetDoNotAllowPads(bool(no_pads))
    zone.SetDoNotAllowFootprints(bool(no_footprints))
    zone.SetLayerSet(_layer_set(board, layers))
    if isinstance(polyset_or_points_sheet_mm, pcbnew.SHAPE_POLY_SET):
        bo.fill_zone_outline(zone, polyset_or_points_sheet_mm)
    else:
        _fill_points(zone, polyset_or_points_sheet_mm)
    zone.SetZoneName(name)
    board.Add(zone)
    return zone


def add_keepout(board: Any, x0: float, y0: float, w: float, h: float,
                name: str = "ANTENNA") -> Any:
    """An antenna keep-out rule area over the rectangle on every copper layer:
    no copper pour and no vias, so the ground/power planes clip AROUND it and
    the radiator sits over bare substrate (the #1 PCB-antenna layout rule: a
    ground plane under the antenna detunes and shorts the near field). Tracks
    stay allowed so the antenna/feed conductor itself can live in the region;
    pads are not. Raises on failure (it never returns None)."""
    return add_keepout_polygon(board, _rect_corners(x0, y0, w, h), name, no_tracks=False,
                               no_vias=True, no_pour=True, no_pads=True)
