#!/usr/bin/env python3
"""Silkscreen reference-designator placement — runs under KiCad's Python.

Chip refs go ADJACENT to their courtyard (above, below, right, left: the first spot
clear of every part, placed ref and the edge; centred on the body on a dense board).
J/H/TP parts keep their authored label (obstacles, and edge-clamped too).
Outline-aware (ROADMAP D2), Edge.Cuts read once per call: text lies on the material
(inside the outline, outside every cutout) MARGIN inside the edge, measured as before
from KiCad's Edge.Cuts bbox, so a rectangle gives exactly the old result. A ref left
off it walks toward its part (STEP_MM), else is centred on its body and shifted into
the bbox (the old dense fallback), else kept where it clears the outline more (_clamp).
Open Edge.Cuts: the bbox rule + NOT_CLOSED. Courtyard caches are rebuilt (9.0.4).

Runnable: kicad-python silk_place.py <board> prints place_refs' summary as JSON:
    {"adjacent", "centered", "clamped", "stepped", "body_fallback", "off_outline": n,
     "containment": "outline"|"bbox", "errors": [...], "warnings": [...]}
or {"ok": false, "errors": [...]} with exit code 1 when the board cannot load.
"""
from __future__ import annotations

import json
import math
import sys
from dataclasses import dataclass
from typing import Any

import kicad_quiet  # noqa: F401  (before pcbnew: no modal wx dialogs)
import pcbnew

import board_outline as bo
import outline as ol

MARGIN = 0.8          # keep silk this far inside Edge.Cuts (fab clips at edge)
GAP = 0.15            # courtyard-to-text gap
TEXT_MM = 0.7
STROKE_MM = 0.11
STEP_MM = 0.25        # clamp: walk a ref toward its part's centre in these steps
FIT_SLACK_MM = 0.001  # the bbox clamp's FromMM truncation leaves text 1 nm short
NOT_CLOSED = "silk: Edge.Cuts not closed, using bbox"
CANNOT = f"ref text cannot sit {MARGIN} mm inside the outline near its part"

Box = tuple[float, float, float, float]
_mm, _to = pcbnew.FromMM, pcbnew.ToMM


def _box_mm(bb: Any) -> Box:
    return (_to(bb.GetLeft()), _to(bb.GetTop()), _to(bb.GetRight()), _to(bb.GetBottom()))


@dataclass(frozen=True)
class Fence:
    """Where text may sit: ``box`` = the legacy rule (MARGIN inside KiCad's Edge.Cuts bbox,
    sheet mm); ``outline`` = the board-frame Outline at sheet ``origin`` (None: bbox only),
    whose edges text keeps ``clearance`` mm from (MARGIN - stroke - FIT_SLACK_MM)."""
    box: Box
    outline: ol.Outline | None = None
    origin: tuple[float, float] = (0.0, 0.0)
    clearance: float = 0.0

    def in_box(self, tb: Any) -> bool:
        x0, y0, x1, y1 = self.box
        left, top, right, bottom = _box_mm(tb)
        return not (left < x0 or right > x1 or top < y0 or bottom > y1)

    def on_board(self, tb: Any, clearance: float | None = None) -> bool:
        """The text bbox lies on the material with the (fence's) clearance; bbox mode: True."""
        if self.outline is None:
            return True
        (ox, oy), (x0, y0, x1, y1) = self.origin, _box_mm(tb)
        rect = (x0 - ox, y0 - oy, max(x1, x0 + 1e-6) - ox, max(y1, y0 + 1e-6) - oy)
        c = self.clearance if clearance is None else clearance   # 1 nm min: empty text
        return ol.contains_rect(self.outline, rect, c)

    def keeps(self, tb: Any) -> float:
        """The clearance kept from the outline (to 1 um; the fence's if it fits, -1.0 off it)."""
        if self.on_board(tb):
            return self.clearance
        lo, hi = (0.0, self.clearance) if self.on_board(tb, 0.0) else (-1.0, -1.0)
        while hi - lo > 1e-3:
            mid = (lo + hi) / 2
            lo, hi = (mid, hi) if self.on_board(tb, mid) else (lo, mid)
        return lo


def make_fence(board: Any) -> tuple[Fence, list[str]]:
    """The board's Fence (built once per place_refs call) and its warnings."""
    edges = _box_mm(board.GetBoardEdgesBoundingBox())
    box = (edges[0] + MARGIN, edges[1] + MARGIN, edges[2] - MARGIN, edges[3] - MARGIN)
    try:
        o, (ox, oy) = bo.outline_from_board(board)
    except (bo.OutlineBuildError, ol.OutlineError) as e:
        return Fence(box), [f"{NOT_CLOSED} ({e})"]
    x0, y0, x1, y1 = ol.bbox(o)
    # KiCad's edge bbox includes the stroke (at most the widest line's half-width)
    half = max((_to(s.GetWidth()) for s in bo.edge_shapes(board)), default=0.0) / 2
    stroke = min(max(ox + x0 - edges[0], oy + y0 - edges[1],
                     edges[2] - (ox + x1), edges[3] - (oy + y1), 0.0), max(half, 0.0))
    return Fence(box, o, (ox, oy), max(MARGIN - stroke - FIT_SLACK_MM, 0.0)), []


def _courtyard_bbox(fp: Any) -> tuple[Any, str | None]:
    """The courtyard bbox (caches rebuilt), else the body bbox: with an error when the
    courtyard cannot be read (a degraded obstacle model), silently when it has none."""
    layer = pcbnew.F_CrtYd if fp.GetLayer() == pcbnew.F_Cu else pcbnew.B_CrtYd
    err = None
    try:
        cy = bo.courtyard(fp, layer)
        if cy and cy.OutlineCount() > 0:
            return cy.BBox(), None
    except (AttributeError, RuntimeError, TypeError) as e:
        err = (f"{fp.GetReference()}: courtyard unavailable "
               f"({type(e).__name__}: {e}); footprint bbox used")
    return fp.GetBoundingBox(False, False), err


def _side(fp: Any) -> str:
    return "B" if fp.GetLayer() == pcbnew.B_Cu else "F"


def _is_chip(ref: str) -> bool:
    """Fixed-label parts (J/H/TP) keep their label and are obstacles."""
    return ref[:1] not in ("J", "H") and not ref.startswith("TP")


def _place_chip(fp: Any, own: Any, others: list, placed: list, fence: Fence) -> bool:
    """Put fp's ref at the first clear candidate (True) or centre it (False)."""
    t = fp.Reference()
    t.SetTextSize(pcbnew.VECTOR2I(_mm(TEXT_MM), _mm(TEXT_MM)))
    t.SetTextThickness(_mm(STROKE_MM))
    t.SetTextAngleDegrees(0)
    ccx, ccy = own.GetCenter().x, own.GetCenter().y
    t.SetPosition(pcbnew.VECTOR2I(ccx, ccy))    # text half-extents at this size
    tb = t.GetBoundingBox()
    hw, hh = tb.GetWidth() // 2, tb.GetHeight() // 2
    cands = [(ccx, own.GetTop() - _mm(GAP) - hh),       # above
             (ccx, own.GetBottom() + _mm(GAP) + hh),    # below
             (own.GetRight() + _mm(GAP) + hw, ccy),     # right
             (own.GetLeft() - _mm(GAP) - hw, ccy)]      # left
    for cx, cy in cands:
        t.SetPosition(pcbnew.VECTOR2I(int(cx), int(cy)))
        tb = t.GetBoundingBox()
        if (fence.in_box(tb) and not any(tb.Intersects(bb) for bb in others + placed)
                and fence.on_board(tb)):
            return True
    t.SetPosition(pcbnew.VECTOR2I(ccx, ccy))   # dense fallback
    return False


def _xy(v: Any) -> tuple[int, int]:   # plain ints: never keep a SWIG ref across a Set
    return int(v.x), int(v.y)


def _walk_in(t: Any, centre: tuple[int, int], fence: Fence) -> bool:
    """Step the text toward `centre` (STEP_MM at a time, the last step on it)
    until it fits the outline; False when none does."""
    px, py = _xy(t.GetPosition())
    dx, dy = centre[0] - px, centre[1] - py
    dist, step = math.hypot(dx, dy), _mm(STEP_MM)
    for k in range(1, (math.ceil(dist / step) if dist > 0 else 0) + 1):
        f = min(k * step / dist, 1.0)
        t.SetPosition(pcbnew.VECTOR2I(px + round(dx * f), py + round(dy * f)))
        if fence.on_board(t.GetBoundingBox()):
            return True
    return False


def _bbox_shift(t: Any, box: Box) -> bool:
    """The legacy clamp: shift the text into `box` (sheet mm); True when it moved."""
    sx, sy = _xy(t.GetPosition())
    x0, y0, x1, y1 = box
    left, top, right, bottom = _box_mm(t.GetBoundingBox())
    dx = (x0 - left) if left < x0 else (x1 - right) if right > x1 else 0.0
    dy = (y0 - top) if top < y0 else (y1 - bottom) if bottom > y1 else 0.0
    if dx or dy:
        t.SetPosition(pcbnew.VECTOR2I(sx + _mm(dx), sy + _mm(dy)))
    return bool(dx or dy)


def _clamp(fp: Any, own: Any, fence: Fence) -> tuple[bool, str, float | None]:
    """Clamp fp's ref into the edge bbox (legacy shift), then onto the material: (shifted,
    how, kept): "" fits, "stepped" toward its part, "body" (centred, then shifted into the
    bbox) or "edge" (the bbox-clamped spot clears the outline more); kept when short."""
    t = fp.Reference()
    shifted = _bbox_shift(t, fence.box)
    if fence.on_board(t.GetBoundingBox()):
        return shifted, "", None
    edge_spot, centre = _xy(t.GetPosition()), _xy(own.GetCenter())
    if _walk_in(t, centre, fence):
        return shifted, "stepped", None
    t.SetPosition(pcbnew.VECTOR2I(*centre))
    _bbox_shift(t, fence.box)
    body = (fence.keeps(t.GetBoundingBox()), _xy(t.GetPosition()), "body")
    if body[0] >= fence.clearance:
        return shifted, "body", None
    t.SetPosition(pcbnew.VECTOR2I(*edge_spot))
    edge = (fence.keeps(t.GetBoundingBox()), edge_spot, "edge")
    kept, spot, how = edge if edge[0] > body[0] else body    # a tie: centre-on-body
    t.SetPosition(pcbnew.VECTOR2I(*spot))
    return shifted, how, kept


def _obstacles(fps: list) -> tuple[dict, dict, dict, list[str]]:
    """{ref: own courtyard bbox}, {side: [courtyard bboxes]}, {side: [fixed-label
    (J/H/TP) text bboxes]} and the errors (courtyard ones first)."""
    own_bbox, courtyards, placed = {}, {"F": [], "B": []}, {"F": [], "B": []}
    cy_errors, label_errors = [], []
    for fp in fps:
        bb, err = _courtyard_bbox(fp)
        own_bbox[fp.GetReference()] = bb
        courtyards[_side(fp)].append(bb)
        cy_errors += [err] if err else []
        if not _is_chip(fp.GetReference()):
            try:
                placed[_side(fp)].append(fp.Reference().GetBoundingBox())
            except Exception as e:
                label_errors.append(f"{fp.GetReference()}: fixed-label bbox "
                                    f"unavailable ({type(e).__name__}: {e})")
    return own_bbox, courtyards, placed, cy_errors + label_errors


def _place_chips(fps: list, own_bbox: dict, courtyards: dict, placed: dict,
                 fence: Fence) -> tuple[int, int, list[str]]:
    """Place every chip ref; (adjacent, centered, errors). Each accepted text
    joins a copy of `placed` as an obstacle for the refs after it."""
    adjacent = centered = 0
    errors: list[str] = []
    placed = {side: list(boxes) for side, boxes in placed.items()}
    for fp in fps:
        if not _is_chip(fp.GetReference()):
            continue
        try:
            side, own = _side(fp), own_bbox[fp.GetReference()]
            others = [bb for bb in courtyards[side] if bb is not own]
            ok = _place_chip(fp, own, others, placed[side], fence)
            adjacent, centered = adjacent + int(ok), centered + int(not ok)
            placed[side].append(fp.Reference().GetBoundingBox())
        except Exception as e:
            errors.append(f"{fp.GetReference()}: ref not placed "
                          f"({type(e).__name__}: {e})")
    return adjacent, centered, errors


def _clamp_all(fps: list, own_bbox: dict, fence: Fence) -> dict:
    """Clamp EVERY ref (incl. connectors/holes) onto the board: "clamped" counts
    the refs it moved, "stepped" / "body_fallback" how, "off_outline" the refs
    left short of the outline clearance (each fallback named in "warnings")."""
    counts = {"clamped": 0, "stepped": 0, "body_fallback": 0, "off_outline": 0}
    errors, warnings = [], []
    for fp in fps:
        ref = fp.GetReference()
        try:
            before = _xy(fp.Reference().GetPosition())
            shifted, how, kept = _clamp(fp, own_bbox[ref], fence)
            moved = shifted or _xy(fp.Reference().GetPosition()) != before
        except Exception as e:
            errors.append(f"{ref}: ref not clamped ({type(e).__name__}: {e})")
            continue
        flags = {"clamped": moved, "stepped": how == "stepped", "body_fallback": how == "body",
                 "off_outline": kept is not None}
        counts = {k: n + int(flags[k]) for k, n in counts.items()}
        if how in ("body", "edge"):
            where = "centred on its body" if how == "body" else "left at its edge-clamped spot"
            short = ("" if kept is None else ", off the board material" if kept < 0
                     else f", clearing the edge by only {kept:.3f} mm")
            warnings.append(f"{ref}: {CANNOT}; {where}{short}")
    return {**counts, "errors": errors, "warnings": warnings}


def place_refs(board: Any) -> dict:
    """Re-place every reference designator for legibility; the summary dict (module
    docstring) names unhandled refs in ``errors`` and degraded ones in ``warnings``."""
    fence, warnings = make_fence(board)
    fps = sorted(board.GetFootprints(), key=lambda f: f.GetReference())
    own_bbox, courtyards, placed, errors = _obstacles(fps)
    adjacent, centered, chip_errors = _place_chips(fps, own_bbox, courtyards, placed, fence)
    clamp = _clamp_all(fps, own_bbox, fence)
    return {"adjacent": adjacent, "centered": centered, **clamp,
            "containment": "bbox" if fence.outline is None else "outline",
            "errors": errors + chip_errors + clamp["errors"],
            "warnings": warnings + clamp["warnings"]}


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    try:
        board = bo.load_board(sys.argv[1])
    except (FileNotFoundError, RuntimeError) as e:
        print(json.dumps({"ok": False, "errors": [f"{type(e).__name__}: {e}"]}))
        return 1
    stats = place_refs(board)
    pcbnew.SaveBoard(sys.argv[1], board)
    print(json.dumps(stats))
    return 0


if __name__ == "__main__":
    sys.exit(main())
