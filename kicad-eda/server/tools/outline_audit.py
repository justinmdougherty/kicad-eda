#!/usr/bin/env python3
"""Pure outline audit rules (ROADMAP D2) shared by the G3/G4 SWIG gate adapters, the silk
placer, G1 lint and the mechanical tools. KiCad DRC misses copper lying wholly inside an
Edge.Cuts hole and the old G3 connector rule used the board bbox; these use the outline.
Stdlib only, Python 3.11 (KiCad's bundled Python imports it), never pcbnew; built on
outline.py's public API alone. Records are in the BOARD FRAME (mm, Y down, origin = outline
bbox top-left) and every message says so:
  parts  [{ref, polygon: [[x, y], ...], overhang: float | None}]   (courtyards)
  pads   [{ref, pad, polygon} | {ref, pad, center, radius}]
  items  [{kind: track | arc, net, start, end, width (, mid)}
          | {kind: via, net, center, radius} (or PCB_VIA-style {kind: via, start, width})]
  holes  [{ref, center, drill}]
Connector texts keep the G3 prefixes '<ref>: mating face points INTO the board interior' /
'<ref>: mating face is <d> mm from the board edge it faces' (napkin_policy, faithful).
"""
from __future__ import annotations

import math
import sys
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from itertools import pairwise
from pathlib import Path

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from outline import (FAB_LIMITS, Arc, Circle, FabLimits, Outline,  # noqa: E402
                     arc_from_three_points, area, bbox, contains, distance_to_edge, edges,
                     nearest_edge, ray_to_edge, validate_fab)

Point = tuple[float, float]
Seg = tuple[Point, Point]

FRAME = "board frame, mm"
TOL_MM = 1e-3                  # slack on every clearance / overhang limit
SAMPLE_MM = 0.25               # spacing of the samples along a track or polygon edge
ARC_SAGITTA_MM = 2e-4          # chord error when an arc track is split into segments
FACING_MIN_COS = math.cos(math.radians(45.0))
CONNECTOR_MAX_DIST_MM = 3.0
MIN_FEATURE_REACH_MM = 10.0    # outline_summary reports material webs narrower than this
SEVERITIES = ("error", "warning")
_MAX_HITS = 256                # boundary crossings looked at along one segment


@dataclass(frozen=True)
class Issue:
    """A finding: stable code, 'error' | 'warning', ref (None: the outline), message, at."""
    code: str
    severity: str
    ref: str | None
    message: str
    at: Point | None = None

    def __post_init__(self) -> None:
        if self.severity not in SEVERITIES:
            raise ValueError(f"Issue severity must be one of {SEVERITIES}, got {self.severity!r}")

    def to_dict(self) -> dict:
        return {"code": self.code, "severity": self.severity, "ref": self.ref,
                "message": self.message,
                "at": None if self.at is None else [round(float(v), 6) for v in self.at]}


def _num(v: object, what: str) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        raise ValueError(f"{what} must be a finite number, got {v!r}")
    return float(v)


def _pt(raw: object, what: str) -> Point:
    if not isinstance(raw, (list, tuple)) or len(raw) != 2:
        raise ValueError(f"{what} must be [x, y] ({FRAME}), got {raw!r}")
    return (_num(raw[0], what), _num(raw[1], what))


def _get(rec: object, key: str, where: str) -> object:
    if not isinstance(rec, Mapping) or rec.get(key) is None:
        raise ValueError(f"{where} needs '{key}', got {rec!r}")
    return rec[key]


def _mm(rec: Mapping, key: str, where: str) -> float:
    v = _num(_get(rec, key, where), f"{where}.{key}")
    if v <= 0:
        raise ValueError(f"{where}.{key} must be > 0 mm, got {v!r}")
    return v


def _polygon(rec: Mapping, where: str) -> list[Point]:
    raw = _get(rec, "polygon", where)
    if not isinstance(raw, (list, tuple)) or len(raw) < 3:
        raise ValueError(f"{where}.polygon needs >= 3 [x, y] points, got {raw!r}")
    return [_pt(p, f"{where}.polygon[{i}]") for i, p in enumerate(raw)]


def _f(p: Sequence[float]) -> str:
    return "({:.3f}, {:.3f})".format(*(round(v, 3) + 0.0 for v in p))   # no '-0.000'


def _ring_name(k: int) -> str:
    return "the board outline" if k == 0 else f"cutout {k}"


def _lerp(a: Point, b: Point, t: float) -> Point:
    return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)


def _foot(a: Point, b: Point, q: Point) -> Point:
    """The point of the segment a-b nearest q."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    dd = dx * dx + dy * dy
    t = 0.0 if dd <= 0 else min(1.0, max(0.0, ((q[0] - a[0]) * dx + (q[1] - a[1]) * dy) / dd))
    return _lerp(a, b, t)


def _samples(a: Point, b: Point) -> list[Point]:
    n = max(1, math.ceil(math.dist(a, b) / SAMPLE_MM))
    return [_lerp(a, b, i / n) for i in range(n + 1)]


def _segments(pts: Sequence[Point], closed: bool) -> list[Seg]:
    segs = list(pairwise(pts))
    return segs + [(pts[-1], pts[0])] if closed and len(pts) > 2 else segs


@lru_cache(maxsize=16)
def _anchors(o: Outline) -> tuple[tuple[Point, float], ...]:
    """Outline vertices (radius 0) and arc centres (with radius): a segment is nearest the
    boundary at an end, at its point nearest a vertex or at the foot of an arc centre."""
    out: list[tuple[Point, float]] = []
    for e in edges(o):
        out.append((e.prim.start, 0.0))
        if isinstance(e.prim, Arc):
            out.append((e.prim.c, e.prim.r))
    return tuple(out)


def _off_pieces(o: Outline, a: Point, b: Point) -> Iterator[Point]:
    """Midpoints of the off-material pieces of a-b, split at ray_to_edge hits (touching is in)."""
    length, ts = math.dist(a, b), {0.0, 1.0}
    if length > 1e-9:
        d, skip = ((b[0] - a[0]) / length, (b[1] - a[1]) / length), 0.0
        while len(ts) < _MAX_HITS:
            hit = ray_to_edge(o, a, d, skip)
            if hit is None or hit.distance >= length:
                break
            ts.add(hit.distance / length)
            skip = hit.distance + 1e-9
    for t0, t1 in pairwise(sorted(ts)):
        m = _lerp(a, b, (t0 + t1) / 2.0)
        if t1 - t0 > 1e-12 and not contains(o, m):
            yield m


def _enclosed_cutout(o: Outline, pts: Sequence[Point]) -> tuple[int, Point] | None:
    """A cutout wholly inside the polygon (call once its edges are known not to cross)."""
    for k, ring in enumerate(o.cutouts, start=1):
        q = (ring.cx + ring.r, ring.cy) if isinstance(ring, Circle) else ring.vertices[0][:2]
        inside = False
        for (ax, ay), (bx, by) in _segments(pts, True):
            if (ay > q[1]) != (by > q[1]) and q[0] < ax + (q[1] - ay) * (bx - ax) / (by - ay):
                inside = not inside
        if inside:
            return k, q
    return None


def _min_gap(o: Outline, pts: Sequence[Point], segs: Sequence[Seg]) -> tuple[float, Point]:
    """Least distance from the polyline to the outline and where: end points and SAMPLE_MM
    samples, then each segment's point nearest a vertex / arc centre that may beat them."""
    cands = [p for a, b in segs for p in _samples(a, b)] or list(pts[:1])
    best = min(((distance_to_edge(o, p), p) for p in cands), key=lambda g: g[0])
    for a, b in segs:
        for q, r in _anchors(o):
            p = _foot(a, b, q)
            s = math.dist(p, q) - r
            if ((s < best[0]) if r == 0 else (0.0 <= s < best[0])) \
                    and (d := distance_to_edge(o, p)) < best[0]:
                best = (d, p)
    return best


@dataclass(frozen=True)
class _Verdict:
    kind: str      # 'outside' (off the material) | 'clearance' | 'overhang'
    ring: int      # the ring crossed, entered or too near (0 = off the board)
    at: Point
    gap: float     # shape edge to outline, mm (< 0: past the edge)


def _fast_ok(o: Outline, pts: Sequence[Point], need: float) -> bool:
    """The vertices' bounding disc lies on the material with `need` mm to spare."""
    c = (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))
    reach = max(math.dist(c, p) for p in pts)
    return distance_to_edge(o, c) > reach + max(need, 0.0) + 1e-9 and contains(o, c)


def _shape_verdict(o: Outline, pts: Sequence[Point], closed: bool, half: float,
                   clearance: float) -> _Verdict | None:
    """`pts` (a polygon if `closed`, a point if alone) swept by a disc of radius `half`: None
    when it lies on the board material with `clearance` mm to spare (TOL_MM slack)."""
    if _fast_ok(o, pts, half + clearance):
        return None
    segs = _segments(pts, closed)
    off = next((m for a, b in segs for m in _off_pieces(o, a, b)), None) if segs else \
        (None if contains(o, pts[0]) else pts[0])
    if off is not None:
        return _Verdict("outside", nearest_edge(o, off).ring, off, -half)
    enclosed = _enclosed_cutout(o, pts) if closed else None
    if enclosed is not None:
        return _Verdict("outside", enclosed[0], enclosed[1], -half)
    gap, at = _min_gap(o, pts, segs)
    hit = nearest_edge(o, at)
    if half > 0 and gap > 0:                     # the copper point nearest the edge
        at = _lerp(at, hit.point, half / gap)
    if gap < half - TOL_MM:
        return _Verdict("outside", hit.ring, at, gap - half)
    if gap - half < clearance - TOL_MM:
        return _Verdict("clearance", hit.ring, at, gap - half)
    return None


def _overhang_verdict(o: Outline, pts: Sequence[Point], overhang: float,
                      clearance: float) -> _Verdict | None:
    """Hanging over the OUTER edge by <= overhang mm; never crossing / near a cutout."""
    segs = _segments(pts, True)
    for m in (m for a, b in segs for m in _off_pieces(o, a, b)):
        if (k := nearest_edge(o, m).ring) > 0:
            return _Verdict("outside", k, m, 0.0)
    enclosed = _enclosed_cutout(o, pts)
    if enclosed is not None:
        return _Verdict("outside", enclosed[0], enclosed[1], 0.0)
    samples = [p for a, b in segs for p in _samples(a, b)]
    if clearance > TOL_MM:
        feet = [_foot(a, b, q) for a, b in segs for q, _r in _anchors(o)]
        hits = ((nearest_edge(o, p), p) for p in samples + feet)
        near = min(((h.distance, h.ring, p) for h, p in hits if h.ring > 0), default=None)
        if near is not None and near[0] < clearance - TOL_MM:
            return _Verdict("clearance", near[1], near[2], near[0])
    off = [(distance_to_edge(o, p), p) for p in samples if not contains(o, p)]
    if len(off) == len(samples):
        return _Verdict("outside", 0, samples[0], 0.0)
    worst = max(off, key=lambda g: g[0], default=(0.0, samples[0]))
    return _Verdict("overhang", 0, worst[1], -worst[0]) if worst[0] > overhang + TOL_MM else None


_COURTYARD = {   # verdict kind -> (code, message after '<ref>: courtyard ')
    "cutout": ("courtyard_in_cutout", "overlaps cutout {ring} at {at}; no part may cross one"),
    "outside": ("courtyard_outside", "extends off the board at {at}; give the part an "
                "'overhang' (mm) if it is meant to hang over the edge"),
    "overhang": ("courtyard_overhang", "hangs {over:.3f} mm over the board outline at {at}, "
                 "more than its overhang {oh:g} mm"),
    "clearance": ("courtyard_edge_clearance", "is {gap:.3f} mm from {name} at {at} (min {cl:g})"),
}


def courtyard_issues(outline: Outline, parts: Sequence[Mapping],
                     clearance: float = 0.0) -> list[Issue]:
    """Courtyards inside the outer ring, outside every cutout and `clearance` mm from every
    edge. A part with overhang > 0 may cross ONLY the outer ring (ring 0), by at most its
    overhang; crossing a cutout is always an error."""
    clearance = _num(clearance, "clearance")
    out = []
    for i, rec in enumerate(parts):
        where = f"parts[{i}]"
        ref, pts = str(_get(rec, "ref", where)), _polygon(rec, where)
        oh = 0.0 if rec.get("overhang") is None else _num(rec["overhang"], f"{where}.overhang")
        if oh < 0:
            raise ValueError(f"{where}.overhang must be >= 0 mm or None, got {oh!r}")
        v = (_overhang_verdict(outline, pts, oh, clearance) if oh > 0
             else _shape_verdict(outline, pts, True, 0.0, clearance))
        if v is not None:
            code, text = _COURTYARD["cutout" if v.kind == "outside" and v.ring else v.kind]
            text = text.format(ring=v.ring, at=_f(v.at), over=-v.gap, oh=oh, gap=v.gap,
                               name=_ring_name(v.ring), cl=clearance)
            out.append(Issue(code, "error", ref, f"{ref}: courtyard {text} ({FRAME})", v.at))
    return out


def _copper_issue(prefix: str, subject: str, ref: str | None, v: _Verdict,
                  clearance: float) -> Issue:
    if v.kind == "outside":
        place = "past the board outline" if v.ring == 0 else f"into cutout {v.ring}"
        return Issue(f"{prefix}_outside", "error", ref,
                     f"{subject}: copper extends {place} at {_f(v.at)} ({FRAME})", v.at)
    return Issue(f"{prefix}_edge_clearance", "error", ref,
                 f"{subject}: copper is {v.gap:.3f} mm from {_ring_name(v.ring)} at {_f(v.at)}, "
                 f"under the {clearance:g} mm edge clearance ({FRAME})", v.at)


def pad_issues(outline: Outline, pads: Sequence[Mapping], edge_clearance: float) -> list[Issue]:
    """Every pad (polygon or circle) on the material, >= edge_clearance - 1e-3 mm from edges."""
    clearance = _num(edge_clearance, "edge_clearance")
    out = []
    for i, rec in enumerate(pads):
        where = f"pads[{i}]"
        ref = str(_get(rec, "ref", where))
        if rec.get("polygon") is not None:
            v = _shape_verdict(outline, _polygon(rec, where), True, 0.0, clearance)
        else:
            c = _pt(_get(rec, "center", where), f"{where}.center")
            v = _shape_verdict(outline, [c], False, _mm(rec, "radius", where), clearance)
        if v is not None:
            out.append(_copper_issue("pad", f"{ref} pad {rec.get('pad', '?')}", ref, v, clearance))
    return out


def _copper_shape(rec: Mapping, where: str) -> tuple[list[Point], float]:
    """(polyline, half width); an arc with `mid` becomes chords within ARC_SAGITTA_MM."""
    kind = rec.get("kind") if isinstance(rec, Mapping) else None
    if kind == "via":      # {center, radius}, or as SWIG's PCB_VIA: {start, width}
        c = _pt(rec.get("center") or _get(rec, "start", where), f"{where}.center")
        return [c], (_mm(rec, "radius", where) if rec.get("radius") is not None
                     else _mm(rec, "width", where) / 2.0)
    if kind not in ("track", "arc"):
        raise ValueError(f"{where}.kind must be 'track', 'arc' or 'via', got {kind!r}")
    a, b = (_pt(_get(rec, k, where), f"{where}.{k}") for k in ("start", "end"))
    half = _mm(rec, "width", where) / 2.0
    if kind != "arc" or rec.get("mid") is None:
        return [a, b], half
    arc = arc_from_three_points(a, _pt(rec["mid"], f"{where}.mid"), b)
    step = 2.0 * math.acos(1.0 - ARC_SAGITTA_MM / arc.r) if ARC_SAGITTA_MM < arc.r else 1.0
    n = max(1, math.ceil(abs(arc.sweep) / step))
    return [arc.point_at(k / n) for k in range(n + 1)], half


def copper_issues(outline: Outline, items: Sequence[Mapping],
                  edge_clearance: float) -> list[Issue]:
    """Tracks (segments swept by width / 2), arcs (start / mid / end) and vias (circles) on
    the material, >= edge_clearance - 1e-3 mm from every edge. A track crossing a hole is
    found even when both its ends lie outside it. ref = the item's ref, else its net."""
    clearance = _num(edge_clearance, "edge_clearance")
    out = []
    for i, rec in enumerate(items):
        pts, half = _copper_shape(rec, f"items[{i}]")
        v = _shape_verdict(outline, pts, False, half, clearance)
        if v is not None:
            net = str(rec.get("net") or "")
            out.append(_copper_issue("copper", f"{rec['kind']} on net {net!r}",
                                     rec.get("ref") or net or None, v, clearance))
    return out


def mounting_hole_issues(outline: Outline, holes: Sequence[Mapping],
                         profile: str | FabLimits = "JLCPCB") -> list[Issue]:
    """Judge the DRILL circle, never the courtyard: on the material (hole_outside, error)
    and >= the fab's hole_to_edge from every edge (hole_edge, warning: JLCPCB's 0.25 mm
    is a support answer, not a published capability)."""
    lim = profile if isinstance(profile, FabLimits) else FAB_LIMITS.get(str(profile).upper())
    if lim is None:
        raise ValueError(f"unknown fab profile {profile!r}; known: {sorted(FAB_LIMITS)}")
    out = []
    for i, rec in enumerate(holes):
        where = f"holes[{i}]"
        c, drill = _pt(_get(rec, "center", where), f"{where}.center"), _mm(rec, "drill", where)
        ref = str(rec.get("ref") or f"hole {i}")
        v = _shape_verdict(outline, [c], False, drill / 2.0, lim.min_hole_to_edge)
        if v is None:
            continue
        text = (("reaches off the board" if v.ring == 0 else f"reaches into cutout {v.ring}")
                if v.kind == "outside" else
                f"is {v.gap:.3f} mm from {_ring_name(v.ring)} (minimum {lim.min_hole_to_edge:g} "
                f"mm, {lim.name} hole_to_edge: {lim.sources.get('hole_to_edge', 'no source')})")
        out.append(Issue("hole_outside" if v.kind == "outside" else "hole_edge",
                         "error" if v.kind == "outside" else "warning", ref,
                         f"{ref}: drill {drill:g} mm at {_f(c)} {text} ({FRAME})", v.at))
    return out


def connector_edge_issue(outline: Outline, ref: str, mouth_point: Sequence[float],
                         mouth_dir: Sequence[float], max_dist: float = CONNECTOR_MAX_DIST_MM,
                         panel_cutouts: bool = False) -> Issue | None:
    """A connector mates through the board edge: the edge nearest its mouth point
    (nearest_edge) must face the way the mouth opens (outward normal within 45 deg of
    mouth_dir) and lie within max_dist mm. A cutout edge counts only with panel_cutouts."""
    p, (dx, dy) = _pt(mouth_point, "mouth_point"), _pt(mouth_dir, "mouth_dir")
    if math.hypot(dx, dy) < 1e-9:
        raise ValueError(f"mouth_dir must be a non-zero direction, got {mouth_dir!r}")
    u = (dx / math.hypot(dx, dy), dy / math.hypot(dx, dy))
    hit = nearest_edge(outline, p)
    cos = u[0] * hit.normal[0] + u[1] * hit.normal[1]
    counts, faces = hit.ring == 0 or panel_cutouts, cos >= FACING_MIN_COS - 1e-9
    if counts and faces and hit.distance <= max_dist + 1e-9:
        return None
    edge = (f"nearest edge: {_ring_name(hit.ring)} at {_f(hit.point)}, outward normal "
            f"{_f(hit.normal)}; mouth {_f(u)} at {_f(p)} ({FRAME})")
    if counts and faces:
        return Issue("connector_edge", "error", ref,
                     f"{ref}: mating face is {hit.distance:.1f} mm from the board edge it faces "
                     f"(must be at the edge, <={max_dist:g} mm); {edge}", p)
    why = (f"it opens onto cutout {hit.ring} (cutout edges count only with "
           f"board.panel_cutouts)" if not counts else
           f"the mouth is {math.degrees(math.acos(max(-1.0, min(1.0, cos)))):.0f} deg off the "
           "edge's outward normal (max 45)")
    return Issue("connector_edge", "error", ref, f"{ref}: mating face points INTO the board "
                 f"interior (unusable in an enclosure): {why}; {edge}", p)


def fab_issues(outline: Outline, holes: Sequence | None = None,
               profile: str | FabLimits = "JLCPCB") -> list[Issue]:
    """outline.validate_fab as Issues (code fab_<rule>). `holes`: {center, drill(, ref)}
    records or (x, y, diameter). An assumption-based rule keeps its 'ASSUMPTION' source."""
    rows, refs = [], {}
    for i, h in enumerate(holes or ()):
        where, rec = f"holes[{i}]", isinstance(h, Mapping)
        row = (*_pt(_get(h, "center", where), where), _get(h, "drill", where)) if rec else h
        if not isinstance(row, (list, tuple)) or len(row) != 3:
            raise ValueError(f"{where} must be {{center, drill}} or (x, y, diameter), got {h!r}")
        rows.append(tuple(_num(v, where) for v in row))
        refs[tuple(round(v, 6) for v in rows[-1][:2])] = h.get("ref") if rec else None
    out = []
    for prob in validate_fab(outline, profile, rows):
        at = None if prob["at"] is None else (prob["at"][0], prob["at"][1])
        msg, src = prob["message"], prob["source"]
        if prob["assumption"]:
            msg += f" [{src}]" if "ASSUMPTION" in src else f" [ASSUMPTION: {src}]"
        ref = refs.get(at) if prob["rule"].startswith("hole") else None
        out.append(Issue(f"fab_{prob['rule']}", prob["severity"], ref, f"{msg} ({FRAME})", at))
    return out


_SUMMARY_LIMITS = FabLimits(name="summary", min_board_dim=0.0, min_slot_width=0.0,
                            min_internal_radius=0.0, min_feature_width=MIN_FEATURE_REACH_MM,
                            min_npth_diameter=0.0, min_hole_to_edge=0.0)


def _ring_summary(ring: object) -> dict:
    alone = Outline(ring)
    extra = {"center": [ring.cx, ring.cy], "radius": ring.r} if isinstance(ring, Circle) else {}
    return {"kind": "circle" if extra else "loop", "bbox": [round(v, 6) for v in bbox(alone)],
            "area": round(area(alone), 6), **extra}


def outline_summary(outline: Outline) -> dict:
    """{bbox [x0, y0, x1, y1], area (mm^2, cutouts removed), cutouts [{kind, bbox, area(,
    center, radius)}], datum [x, y] | None, min_feature: the narrowest board-material web or
    neck (mm) when under MIN_FEATURE_REACH_MM else None, min_feature_at, frame}."""
    webs = [p for p in validate_fab(outline, _SUMMARY_LIMITS) if p["rule"] == "feature_width"]
    narrowest = min(webs, key=lambda p: p["value"], default=None)
    return {"bbox": [round(v, 6) for v in bbox(outline)], "area": round(area(outline), 6),
            "cutouts": [_ring_summary(c) for c in outline.cutouts],
            "datum": None if outline.datum is None else list(outline.datum),
            "min_feature": None if narrowest is None else narrowest["value"],
            "min_feature_at": None if narrowest is None else narrowest["at"], "frame": FRAME}
