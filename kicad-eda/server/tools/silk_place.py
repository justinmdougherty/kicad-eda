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
"""
import sys

import pcbnew

MARGIN = 0.8          # keep silk this far inside Edge.Cuts (fab clips at edge)
GAP = 0.15            # courtyard-to-text gap
TEXT_MM = 0.7
STROKE_MM = 0.11


def _mm(v):
    return pcbnew.FromMM(v)


def _to(v):
    return pcbnew.ToMM(v)


def _courtyard_bbox(fp):
    layer = (pcbnew.F_CrtYd if fp.GetLayer() == pcbnew.F_Cu
             else pcbnew.B_CrtYd)
    try:
        cy = fp.GetCourtyard(layer)
        if cy and cy.OutlineCount() > 0:
            return cy.BBox()
    except Exception:
        pass
    return fp.GetBoundingBox(False, False)


def _side(fp):
    return "B" if fp.GetLayer() == pcbnew.B_Cu else "F"


def place_refs(board):
    """Re-place every reference designator for legibility. Returns a summary
    dict {adjacent: n, centered: n, clamped: n}."""
    edges = board.GetBoardEdgesBoundingBox()
    bx0, by0 = _to(edges.GetLeft()) + MARGIN, _to(edges.GetTop()) + MARGIN
    bx1, by1 = _to(edges.GetRight()) - MARGIN, _to(edges.GetBottom()) - MARGIN

    fps = sorted(board.GetFootprints(), key=lambda f: f.GetReference())
    courtyards = {"F": [], "B": []}
    for fp in fps:
        courtyards[_side(fp)].append(_courtyard_bbox(fp))
    placed = {"F": [], "B": []}        # accepted ref-text bboxes

    # fixed-label parts (J/H/TP) are obstacles for the chip refs
    def is_chip(ref):
        return ref[:1] not in ("J", "H") and not ref.startswith("TP")

    for fp in fps:
        if not is_chip(fp.GetReference()):
            try:
                placed[_side(fp)].append(fp.Reference().GetBoundingBox())
            except Exception:
                pass

    stats = {"adjacent": 0, "centered": 0, "clamped": 0}
    for fp in fps:
        if not is_chip(fp.GetReference()):
            continue
        try:
            t = fp.Reference()
            t.SetTextSize(pcbnew.VECTOR2I(_mm(TEXT_MM), _mm(TEXT_MM)))
            t.SetTextThickness(_mm(STROKE_MM))
            t.SetTextAngleDegrees(0)
            side = _side(fp)
            own = _courtyard_bbox(fp)
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
        except Exception:
            pass

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
        except Exception:
            pass
    return stats


def main():
    pcb = sys.argv[1]
    board = pcbnew.LoadBoard(pcb)
    stats = place_refs(board)
    pcbnew.SaveBoard(pcb, board)
    print(stats)


if __name__ == "__main__":
    main()
