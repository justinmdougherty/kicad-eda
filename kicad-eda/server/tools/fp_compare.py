#!/usr/bin/env python3
"""Compare two KiCad footprints pad by pad (F2, ROADMAP D1).

``compare_footprints(a, b)`` reads both .kicad_mod files and pairs their pads
by position, nearest first within ``match_radius``. Copper pads and aperture
pads (paste-only windows, mask-only openings: no copper layer) are paired
separately, so a paste window never stands in for a copper pad. Each pair
reports its position / size / shape / type / layer / number deltas; the
result also lists unmatched pads and the copper pads' numbering (duplicates,
numbers only one side has). ``identical`` is True only when nothing differs
beyond ``tolerance``. fp_author re-exports compare_footprints and uses it to
check that KiCad re-serialises an authored footprint unchanged.

Units: mm. Stdlib-only (Python 3.11).
"""
from __future__ import annotations

import math
import os
from collections import Counter
from typing import Any, Sequence

from fp_author_templates import has_copper, natural_key, pad_bbox, r6
from fp_reader import read_footprint


def _extent(p: Any) -> tuple[float, float]:
    x0, y0, x1, y1 = pad_bbox(p)
    return x1 - x0, y1 - y0


def _candidates(pa: Sequence[Any], pb: Sequence[Any],
                match_radius: float) -> list[tuple[float, bool, bool, int, int]]:
    """Every (distance, layers differ, numbers differ, i, j) pair within the
    radius, best first: nearest, then same layers, then same number."""
    return sorted((math.hypot(p.x - q.x, p.y - q.y), set(p.pad_layers) != set(q.pad_layers),
                   p.number != q.number, i, j)
                  for i, p in enumerate(pa) for j, q in enumerate(pb)
                  if abs(p.x - q.x) <= match_radius and abs(p.y - q.y) <= match_radius)


def _deviation(p: Any, q: Any, dist: float, tolerance: float) -> dict | None:
    """What differs between paired pads p (A) and q (B); None when nothing does."""
    (wa, ha), (wb, hb) = _extent(p), _extent(q)
    issues = [name for name, hit in (
        ("moved", dist > tolerance), ("resized", max(abs(wb - wa), abs(hb - ha)) > tolerance),
        ("shape", p.shape != q.shape), ("type", p.type != q.type),
        ("layers", set(p.pad_layers) != set(q.pad_layers)),
        ("renumbered", p.number != q.number)) if hit]
    if not issues:
        return None
    return {"a": p.number, "b": q.number, "at": [p.x, p.y], "dx": r6(q.x - p.x),
            "dy": r6(q.y - p.y), "offset": r6(dist), "dw": r6(wb - wa), "dh": r6(hb - ha),
            "shape": [p.shape, q.shape], "issues": issues}


def match_pads(pa: Sequence[Any], pb: Sequence[Any], *, tolerance: float,
               match_radius: float) -> dict:
    """Pair pads greedily (best candidate first, each pad once): {"pads",
    "matched", "deviations", "unmatched_a", "unmatched_b", "max_offset",
    "renumbered"}."""
    used_a: set[int] = set()
    used_b: set[int] = set()
    deviations, renumbered, max_offset = [], 0, 0.0
    for dist, _, _, i, j in _candidates(pa, pb, match_radius):
        if dist > match_radius or i in used_a or j in used_b:
            continue
        used_a.add(i)
        used_b.add(j)
        max_offset = max(max_offset, dist)
        renumbered += pa[i].number != pb[j].number
        deviation = _deviation(pa[i], pb[j], dist, tolerance)
        if deviation:
            deviations.append(deviation)
    return {"pads": [len(pa), len(pb)], "matched": len(used_a), "deviations": deviations,
            "unmatched_a": [{"number": p.number, "at": [p.x, p.y]}
                            for i, p in enumerate(pa) if i not in used_a],
            "unmatched_b": [{"number": q.number, "at": [q.x, q.y]}
                            for j, q in enumerate(pb) if j not in used_b],
            "max_offset": r6(max_offset), "renumbered": renumbered}


def _numbering(pa: Sequence[Any], pb: Sequence[Any], renumbered: int) -> dict:
    na, nb = Counter(p.number for p in pa), Counter(q.number for q in pb)
    return {"duplicates_a": {n: c for n, c in na.items() if c > 1 and n},
            "duplicates_b": {n: c for n, c in nb.items() if c > 1 and n},
            "only_in_a": sorted(set(na) - set(nb), key=natural_key),
            "only_in_b": sorted(set(nb) - set(na), key=natural_key),
            "renumbered": renumbered}


def _differs(m: dict) -> bool:
    return bool(m["deviations"] or m["unmatched_a"] or m["unmatched_b"])


def _summary(cu: dict, numbering: dict, apertures: dict) -> str:
    dup_b = ", ".join(f'"{n}" x{c}' for n, c in numbering["duplicates_b"].items())
    text = (f"{len(cu['deviations'])} of {cu['matched']} matched pads differ "
            f"(max offset {cu['max_offset']:.3f} mm, {cu['renumbered']} renumbered); "
            f"{len(cu['unmatched_a'])} pads only in A, {len(cu['unmatched_b'])} only in B"
            + (f"; B repeats pad numbers {dup_b}" if dup_b else ""))
    if _differs(apertures):
        text += (f"; apertures: {len(apertures['deviations'])} of {apertures['matched']} "
                 f"matched differ, {len(apertures['unmatched_a'])} only in A, "
                 f"{len(apertures['unmatched_b'])} only in B")
    return text


def compare_footprints(a_path: str | os.PathLike, b_path: str | os.PathLike, *,
                       tolerance: float = 0.001, match_radius: float = 1.0) -> dict:
    """Pair the pads of A and B by position (nearest first, within
    ``match_radius`` mm; copper and aperture pads separately) and report
    per-pad position / size / shape deltas, pads whose numbers differ,
    duplicate numbers and unmatched pads. ``identical`` is True only when
    nothing differs beyond ``tolerance``; ``apertures`` holds the paste-only /
    mask-only pads' pairing."""
    fa, fb = read_footprint(a_path), read_footprint(b_path)
    pa, pb = [p for p in fa.pads if has_copper(p)], [q for q in fb.pads if has_copper(q)]
    cu = match_pads(pa, pb, tolerance=tolerance, match_radius=match_radius)
    apertures = match_pads([p for p in fa.pads if not has_copper(p)],
                           [q for q in fb.pads if not has_copper(q)],
                           tolerance=tolerance, match_radius=match_radius)
    numbering = _numbering(pa, pb, cu["renumbered"])
    identical = not (_differs(cu) or _differs(apertures) or numbering["only_in_a"]
                     or numbering["only_in_b"]
                     or numbering["duplicates_a"] != numbering["duplicates_b"])
    return {"a": str(a_path), "b": str(b_path), "pads": cu["pads"], "matched": cu["matched"],
            "identical": identical, "tolerance": tolerance, "max_offset": cu["max_offset"],
            "deviations": cu["deviations"], "numbering": numbering,
            "unmatched_a": cu["unmatched_a"], "unmatched_b": cu["unmatched_b"],
            "apertures": apertures,
            "summary": "identical" if identical else _summary(cu, numbering, apertures)}
