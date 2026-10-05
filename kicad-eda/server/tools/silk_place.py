#!/usr/bin/env python3
"""Silkscreen reference-designator placement — runs under KiCad's Python.

Places each chip part's ref ADJACENT to its courtyard (above, below, right,
left — first position that is clear of every other part, every already-placed
ref, and the board edge). A ref the assembler can't see is useless, so
center-on-body is only the last-resort fallback when the board is too dense
for an adjacent label. Connectors / mounting holes / test points keep their
authored label but participate as obstacles and in the edge clamp.

Importable (place_refs(board)) and runnable:
    kicad-python silk_place.py <board.kicad_pcb>
The runnable form prints the summary dict as JSON on its last stdout line:
    {"adjacent": n, "centered": n, "clamped": n, "errors": ["<ref>: <why>", ...]}
"""
import json
import sys

import kicad_quiet  # noqa: F401  (before pcbnew: no modal wx dialogs)
import pcbnew

MARGIN = 0.8          # keep silk this far inside Edge.Cuts (fab clips at edge)
GAP = 0.15            # courtyard-to-text gap
TEXT_MM = 0.7
STROKE_MM = 0.11


def _mm(v):
    return pcbnew.FromMM(v)


def _to(v):
    return pcbnew.ToMM(v)


def _courtyard_bbox(fp, errors):
    """The footprint's courtyard bbox, or its body bbox when the courtyard
    cannot be read. The fallback is a DEGRADED obstacle model (a body bbox is
    usually smaller than the courtyard, so a neighbour's ref may land inside
    this part's courtyard), so it is recorded in ``errors`` rather than
    silently used. A footprint with NO courtyard outline (common for test
    points / holes) falls back without a record — that is not a failure."""
    layer = (pcbnew.F_CrtYd if fp.GetLayer() == pcbnew.F_Cu
             else pcbnew.B_CrtYd)
    try:
        cy = fp.GetCourtyard(layer)
        if cy and cy.OutlineCount() > 0:
            return cy.BBox()
    except (AttributeError, RuntimeError, TypeError) as e:
        errors.append(f"{fp.GetReference()}: courtyard unavailable "
                      f"({type(e).__name__}: {e}); footprint bbox used")
    return fp.GetBoundingBox(False, False)


def _side(fp):
    return "B" if fp.GetLayer() == pcbnew.B_Cu else "F"


def place_refs(board):
    """Re-place every reference designator for legibility. Returns a summary
    dict {adjacent: n, centered: n, clamped: n, errors: [str]} — a footprint
    whose text could not be handled is skipped and named in ``errors``."""
    edges = board.GetBoardEdgesBoundingBox()
    bx0, by0 = _to(edges.GetLeft()) + MARGIN, _to(edges.GetTop()) + MARGIN
    bx1, by1 = _to(edges.GetRight()) - MARGIN, _to(edges.GetBottom()) - MARGIN

    stats = {"adjacent": 0, "centered": 0, "clamped": 0, "errors": []}
    fps = sorted(board.GetFootprints(), key=lambda f: f.GetReference())
    courtyards = {"F": [], "B": []}
    own_bbox = {}                      # ref -> its own obstacle bbox (measured once)
    for fp in fps:
        bb = _courtyard_bbox(fp, stats["errors"])
        own_bbox[fp.GetReference()] = bb
        courtyards[_side(fp)].append(bb)
    placed = {"F": [], "B": []}        # accepted ref-text bboxes

    # fixed-label parts (J/H/TP) are obstacles for the chip refs
    def is_chip(ref):
        return ref[:1] not in ("J", "H") and not ref.startswith("TP")

    for fp in fps:
        if not is_chip(fp.GetReference()):
            try:
                placed[_side(fp)].append(fp.Reference().GetBoundingBox())
            except Exception as e:
                stats["errors"].append(f"{fp.GetReference()}: fixed-label bbox "
                                       f"unavailable ({type(e).__name__}: {e})")

    for fp in fps:
        if not is_chip(fp.GetReference()):
            continue
        try:
            t = fp.Reference()
            t.SetTextSize(pcbnew.VECTOR2I(_mm(TEXT_MM), _mm(TEXT_MM)))
            t.SetTextThickness(_mm(STROKE_MM))
            t.SetTextAngleDegrees(0)
            side = _side(fp)
            own = own_bbox[fp.GetReference()]
            ccx, ccy = own.GetCenter().x, own.GetCenter().y

            # text half-extents at this size (measure once, in place)
            t.SetPosition(pcbnew.VECTOR2I(ccx, ccy))
            tb = t.GetBoundingBox()
            hw, hh = tb.GetWidth() // 2, tb.GetHeight() // 2

            cands = [
                (ccx, own.GetTop() - _mm(GAP) - hh),       # above
                (ccx, own.GetBottom() + _mm(GAP) + hh),    # below
                (own.GetRight() + _mm(GAP) + hw, ccy),     # right
                (own.GetLeft() - _mm(GAP) - hw, ccy),      # left
            ]
            others = [bb for bb in courtyards[side] if bb is not own]
            chosen = None
            for cx, cy in cands:
                t.SetPosition(pcbnew.VECTOR2I(int(cx), int(cy)))
                tb = t.GetBoundingBox()
                if (_to(tb.GetLeft()) < bx0 or _to(tb.GetRight()) > bx1 or
                        _to(tb.GetTop()) < by0 or _to(tb.GetBottom()) > by1):
                    continue
                if any(tb.Intersects(bb) for bb in others):
                    continue
                if any(tb.Intersects(bb) for bb in placed[side]):
                    continue
                chosen = (cx, cy)
                break
            if chosen is None:
                t.SetPosition(pcbnew.VECTOR2I(ccx, ccy))   # dense fallback
                stats["centered"] += 1
            else:
                stats["adjacent"] += 1
            placed[side].append(t.GetBoundingBox())
        except Exception as e:
            stats["errors"].append(f"{fp.GetReference()}: ref not placed "
                                   f"({type(e).__name__}: {e})")

    # clamp EVERY ref (incl. connectors/holes) inside the outline
    for fp in fps:
        try:
            t = fp.Reference()
            bb = t.GetBoundingBox()
            dx = dy = 0.0
            if _to(bb.GetLeft()) < bx0:
                dx = bx0 - _to(bb.GetLeft())
            elif _to(bb.GetRight()) > bx1:
                dx = bx1 - _to(bb.GetRight())
            if _to(bb.GetTop()) < by0:
                dy = by0 - _to(bb.GetTop())
            elif _to(bb.GetBottom()) > by1:
                dy = by1 - _to(bb.GetBottom())
            if dx or dy:
                p = t.GetPosition()
                t.SetPosition(pcbnew.VECTOR2I(p.x + _mm(dx), p.y + _mm(dy)))
                stats["clamped"] += 1
        except Exception as e:
            stats["errors"].append(f"{fp.GetReference()}: ref not clamped "
                                   f"({type(e).__name__}: {e})")
    return stats


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    pcb = sys.argv[1]
    board = pcbnew.LoadBoard(pcb)
    stats = place_refs(board)
    pcbnew.SaveBoard(pcb, board)
    print(json.dumps(stats))
    return 0


if __name__ == "__main__":
    sys.exit(main())
