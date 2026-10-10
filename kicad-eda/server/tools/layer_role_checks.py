"""G4 layer-role checks on plain dicts (ADR D7.8 R8, R27, R28; task D7-18a).

Pure: no pcbnew, no kipy. swig_layer_role_checks.py extracts the board and calls run_checks;
tests call the checks directly. Every check returns {issues, warnings, metrics}: issues are
G4 blockers, warnings G4 warnings, all plain text led by the rule id, metrics plain JSON.

Inputs (lengths in mm):
  board  {tracks: [{net, layer, start [x, y], end [x, y], width, mid? [x, y] (an arc)}],
          vias: [{net, at [x, y], layers [top, bottom], diameter}],
          pads: [{ref, pad, net, at [x, y], layers}],
          zones: [{name, net, layers [...], rule_area}]}      (swig_stackup.zone_layers)
  roles  {layer: {role, net}} of every stackup copper layer (roles_of)
  lines  {net: {layers: {layer: {line, ref {nets, ...}, dielectric_layers}}}}: the
         physical-design artifact's `lines` section, or line_layers(stackup, design)

check_plane_layers (R8, blocker): track segments (arcs too) of any other net, or of no net,
  on a plane-role layer, and copper pours of another net there (rule areas are not copper).
check_ref_transitions (R27, warning): a via of an impedance net whose tracks change between
  layers resting on different reference nets needs a return path within max_dist_mm (2 mm,
  an engineering default) of the signal via: a via of a reference net both layers share, or
  a decoupling capacitor (reference C<n>) with pads on a reference net of each side. A GND via
  cannot join GND to +3V3: with disjoint reference nets only the capacitor helps. A via
  touching a layer without a resolved line is counted as unjudged (R7 / the DRU covers it).
check_dual_stripline_parallel (R28, warning): on the layer a dual stripline takes as
  dielectric, tracks of other nets running over the line (plan-view overlap, or within
  max_offset_mm of its centreline) at an angle below max_angle_deg (30) for more than
  min_len_mm (5, an engineering default) in total per net.
Stdlib only and Python 3.11-compatible (KiCad's bundled Python imports it).
"""
from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping, Sequence
from itertools import pairwise
from typing import Any, NamedTuple

from tline import geometry

MAX_DIST_MM = 2.0
MIN_LEN_MM = 5.0
MAX_ANGLE_DEG = 30.0
ARC_STEPS = 16
NO_NET = "(no net)"
CAPACITOR = re.compile(r"^C[0-9]")
_INNER = re.compile(r"^In([0-9]+)\.Cu$")
_EPS = 1e-9


class Seg(NamedTuple):
    """A straight track piece (an arc is split into ARC_STEPS of them)."""

    net: str
    layer: str
    x0: float
    y0: float
    x1: float
    y1: float
    width: float

    @property
    def length(self) -> float:
        return math.hypot(self.x1 - self.x0, self.y1 - self.y0)


# ---------------------------------------------------------------- input
def _items(board: Any, key: str) -> list[Mapping[str, Any]]:
    if not isinstance(board, Mapping):
        raise ValueError(f"board must be a dict of tracks / vias / pads / zones, got "
                         f"{type(board).__name__}")
    items = board.get(key) or []
    if not isinstance(items, list) or not all(isinstance(i, Mapping) for i in items):
        raise ValueError(f"board[{key!r}] must be a list of dicts")
    return items


def _point(item: Mapping[str, Any], key: str, what: str) -> tuple[float, float]:
    p = item.get(key)
    if not isinstance(p, (list, tuple)) or len(p) != 2:
        raise ValueError(f"{what} needs {key} [x, y] (mm), got {p!r} in {dict(item)!r}")
    return float(p[0]), float(p[1])


class Arc(NamedTuple):
    cx: float
    cy: float
    r: float
    t0: float
    span: float                       # signed sweep (rad) from the start through mid to the end


def _arc(a: tuple, m: tuple, b: tuple) -> Arc | None:
    """The circle arc from a through m to b (None when the three points are collinear)."""
    d = 2 * (a[0] * (m[1] - b[1]) + m[0] * (b[1] - a[1]) + b[0] * (a[1] - m[1]))
    if abs(d) < _EPS:
        return None
    sq = [p[0] ** 2 + p[1] ** 2 for p in (a, m, b)]
    cx = (sq[0] * (m[1] - b[1]) + sq[1] * (b[1] - a[1]) + sq[2] * (a[1] - m[1])) / d
    cy = (sq[0] * (b[0] - m[0]) + sq[1] * (a[0] - b[0]) + sq[2] * (m[0] - a[0])) / d
    t0, tm, t1 = (math.atan2(p[1] - cy, p[0] - cx) for p in (a, m, b))
    span = (t1 - t0) % (2 * math.pi)
    if (tm - t0) % (2 * math.pi) > span:          # m lies the other way round
        span -= 2 * math.pi
    return Arc(cx, cy, math.hypot(a[0] - cx, a[1] - cy), t0, span)


def _track_points(track: Mapping[str, Any]) -> tuple[list[tuple[float, float]], float]:
    """(polyline, exact length) of one board track dict (ValueError when it is malformed)."""
    if not isinstance(track.get("layer"), str) or "net" not in track:
        raise ValueError(f"a track needs net and layer: {dict(track)!r}")
    a, b = _point(track, "start", "a track"), _point(track, "end", "a track")
    arc = None if track.get("mid") is None else _arc(a, _point(track, "mid", "an arc"), b)
    if arc is None:
        return [a, b], math.hypot(b[0] - a[0], b[1] - a[1])
    pts = [(arc.cx + arc.r * math.cos(arc.t0 + arc.span * k / ARC_STEPS),
            arc.cy + arc.r * math.sin(arc.t0 + arc.span * k / ARC_STEPS))
           for k in range(ARC_STEPS + 1)]
    return pts, arc.r * abs(arc.span)


def track_segments(track: Mapping[str, Any]) -> list[Seg]:
    """The straight pieces of one board track dict (an arc gives ARC_STEPS of them)."""
    pts, _ = _track_points(track)
    net, width = track.get("net") or "", float(track.get("width") or 0.0)
    return [Seg(net, track["layer"], *p, *q, width) for p, q in pairwise(pts)]


def track_length(track: Mapping[str, Any]) -> float:
    """The exact length (mm) of a board track dict, arcs included."""
    return _track_points(track)[1]


def layer_order(name: str) -> tuple[int, str]:
    """Stack order of a copper layer name: F.Cu, In1.Cu .. In30.Cu, B.Cu, then the rest."""
    if name == "F.Cu":
        return 0, name
    inner = _INNER.match(name)
    if inner:
        return int(inner.group(1)), name
    return (1000, name) if name == "B.Cu" else (2000, name)


def _result(issues: Iterable[str] = (), warnings: Iterable[str] = (),
            metrics: Mapping[str, Any] | None = None) -> dict[str, Any]:
    return {"issues": list(issues), "warnings": list(warnings), "metrics": dict(metrics or {})}


# ---------------------------------------------------------------- R8
def _plane_map(roles: Mapping[str, Any]) -> dict[str, str | None]:
    return {layer: r.get("net") for layer, r in sorted(roles.items(), key=lambda kv:
                                                       layer_order(kv[0]))
            if isinstance(r, Mapping) and r.get("role") == "plane"}


def _foreign_tracks(board: Any, planes: Mapping[str, str | None]) -> dict[str, dict[str, Any]]:
    """{plane layer: {net, foreign_tracks, foreign_length_mm, nets {net: tracks}}}."""
    found: dict[str, list[tuple[str, float]]] = {layer: [] for layer in planes}
    for track in _items(board, "tracks"):
        length = track_length(track)               # validates the track first
        layer, net = track["layer"], track.get("net") or ""
        if layer in planes and not (net and net == planes[layer]):
            found[layer].append((net or NO_NET, length))
    out = {}
    for layer, hits in found.items():
        nets: dict[str, int] = {}
        for name, _ in hits:
            nets[name] = nets.get(name, 0) + 1
        out[layer] = {"net": planes[layer], "foreign_tracks": len(hits),
                      "foreign_length_mm": round(sum(length for _, length in hits), 3),
                      "nets": dict(sorted(nets.items()))}
    return out


def _foreign_zones(board: Any, planes: Mapping[str, str | None]) -> list[tuple[str, str, str]]:
    """(plane layer, zone name, zone net) of every copper pour of another net on a plane."""
    out = []
    for zone in _items(board, "zones"):
        if zone.get("rule_area"):
            continue
        for layer in zone.get("layers") or ():
            if layer in planes and (zone.get("net") or "") != (planes[layer] or ""):
                out.append((layer, zone.get("name") or "", zone.get("net") or NO_NET))
    return sorted(out, key=lambda z: (layer_order(z[0]), z[1], z[2]))


def _plane_issue(layer: str, e: Mapping[str, Any]) -> str:
    nets = ", ".join(f"{n} ({c})" for n, c in e["nets"].items())
    return (f"R8: {layer} is the {e['net'] or 'no-net'} plane but carries {e['foreign_tracks']} "
            f"foreign track segment(s) ({e['foreign_length_mm']:.1f} mm) of {nets}: a plane "
            "layer carries only its own net - reroute them on the signal layers (the DSN "
            "use_layer policy and the managed .kicad_dru keep the router off plane layers)")


def _zone_issue(layer: str, plane_net: str | None, name: str, net: str) -> str:
    what = f"zone {name!r}" if name else "an unnamed zone"
    return (f"R8: {layer} is the {plane_net or 'no-net'} plane but {what} pours {net}: remove "
            f"the pour or declare {layer} a mixed layer")


def check_plane_layers(board: Any, roles: Mapping[str, Any]) -> dict[str, Any]:
    """R8: foreign-net copper on plane-role layers (module docstring)."""
    planes = _plane_map(roles)
    by_layer = _foreign_tracks(board, planes)
    zones = _foreign_zones(board, planes)
    keyed = [(layer_order(layer), 0, _plane_issue(layer, e)) for layer, e in by_layer.items()
             if e["foreign_tracks"]]
    keyed += [(layer_order(layer), 1, _zone_issue(layer, planes[layer], name, net))
              for layer, name, net in zones]
    issues = [text for *_, text in sorted(keyed)]
    metrics = {"plane_layers": dict(planes),
               "foreign_tracks": sum(e["foreign_tracks"] for e in by_layer.values()),
               "foreign_length_mm": round(sum(e["foreign_length_mm"] for e in by_layer.values()),
                                          3),
               "foreign_zones": len(zones), "by_layer": by_layer}
    return _result(issues, (), metrics)


# ---------------------------------------------------------------- R27
def _ref_nets(lines: Mapping[str, Any], net: str) -> dict[str, frozenset[str]]:
    """{layer: reference nets} of one line record (layers without a reference left out)."""
    rec = lines.get(net)
    layers = rec.get("layers") if isinstance(rec, Mapping) else None
    out = {}
    for layer, line in (layers if isinstance(layers, Mapping) else {}).items():
        ref = line.get("ref") if isinstance(line, Mapping) else None
        nets = ref.get("nets") if isinstance(ref, Mapping) else None
        if nets:
            out[layer] = frozenset(nets)
    return out


def _endpoints(board: Any) -> dict[str, list[tuple[str, tuple[float, float]]]]:
    """{net: [(layer, track end point)]} of every track end."""
    out: dict[str, list[tuple[str, tuple[float, float]]]] = {}
    for track in _items(board, "tracks"):
        track_length(track)                        # validates the track
        ends = [(track["layer"], _point(track, key, "a track")) for key in ("start", "end")]
        out.setdefault(track.get("net") or "", []).extend(ends)    # a local accumulator
    return out


def _vias(board: Any) -> list[tuple[str, tuple[float, float], float]]:
    vias = [(v.get("net") or "", _point(v, "at", "a via"), float(v.get("diameter") or 0.0))
            for v in _items(board, "vias")]
    return sorted(vias, key=lambda v: (v[0], v[1]))


def _capacitors(board: Any) -> list[tuple[str, list[tuple[str, tuple[float, float]]]]]:
    """(ref, [(pad net, pad centre)]) of every capacitor (reference C<n>)."""
    caps: dict[str, list[tuple[str, tuple[float, float]]]] = {}
    for pad in _items(board, "pads"):
        ref = str(pad.get("ref") or "")
        if CAPACITOR.match(ref):
            caps.setdefault(ref, []).append((pad.get("net") or "", _point(pad, "at", "a pad")))
    return sorted(caps.items())


def _return_path(at: tuple[float, float], ra: frozenset[str], rb: frozenset[str],
                 vias: Sequence[tuple[str, tuple, float]],
                 caps: Sequence[tuple[str, list]], max_dist: float) -> str | None:
    """What carries the return current across the transition at `at`, or None."""
    shared = ra & rb
    for net, p, _ in vias:
        if net in shared and math.dist(at, p) <= max_dist + _EPS:
            return f"{net} via"
    for ref, pads in caps:
        nets = {net for net, _ in pads}
        joins = any(p != q for p in nets & ra for q in nets & rb)
        if joins and min(math.dist(at, p) for _, p in pads) <= max_dist + _EPS:
            return f"capacitor {ref}"
    return None


def _transition_warning(net: str, at: tuple, a: str, b: str, ra: frozenset[str],
                        rb: frozenset[str], max_dist: float) -> str:
    shared = "/".join(sorted(ra & rb))
    fix = (f"a {shared} via or " if shared else "") + \
        f"a decoupling capacitor between {'/'.join(sorted(ra))} and {'/'.join(sorted(rb))}"
    return (f"R27: {net} changes layer {a} -> {b} at the via ({at[0]:.2f}, {at[1]:.2f}) mm and "
            f"its reference {'/'.join(sorted(ra))} -> {'/'.join(sorted(rb))} with no return "
            f"path within {max_dist:g} mm of the via: add {fix} next to it")


def _touched(ends: Sequence[tuple[str, tuple]], at: tuple, diameter: float) -> list[str]:
    tol = max(diameter / 2.0, 0.01)
    return sorted({layer for layer, p in ends if math.dist(at, p) <= tol + _EPS},
                  key=layer_order)


def check_ref_transitions(board: Any, lines: Mapping[str, Any], *,
                          max_dist_mm: float = MAX_DIST_MM) -> dict[str, Any]:
    """R27: reference-net changes at the layer transitions of impedance nets (docstring)."""
    ends, vias, caps = _endpoints(board), _vias(board), _capacitors(board)
    counts = {"impedance_vias": 0, "reference_changes": 0, "unsupported": 0, "unjudged_vias": 0}
    warnings = []
    for net, at, diameter in vias:
        if net not in lines:
            continue
        counts["impedance_vias"] += 1
        refs, touched = _ref_nets(lines, net), _touched(ends.get(net, ()), at, diameter)
        if any(layer not in refs for layer in touched):
            counts["unjudged_vias"] += 1
            continue
        for a, b in pairwise(touched):
            if refs[a] == refs[b]:
                continue
            counts["reference_changes"] += 1
            if _return_path(at, refs[a], refs[b], vias, caps, max_dist_mm) is None:
                counts["unsupported"] += 1
                warnings.append(_transition_warning(net, at, a, b, refs[a], refs[b],
                                                    max_dist_mm))
    return _result((), warnings, counts)


# ---------------------------------------------------------------- R28
def _angle_deg(a: Seg, b: Seg) -> float:
    """The acute angle (deg) between two pieces' directions (90 for a zero-length one)."""
    la, lb = a.length, b.length
    if la < _EPS or lb < _EPS:
        return 90.0
    dot = abs((a.x1 - a.x0) * (b.x1 - b.x0) + (a.y1 - a.y0) * (b.y1 - b.y0)) / (la * lb)
    return math.degrees(math.acos(min(1.0, dot)))


def _overlap(a: Seg, b: Seg, off: float) -> float:
    """Length of b inside the band 0 <= u <= len(a), |v| <= off around a's centreline
    (Liang-Barsky clipping in a's frame)."""
    la = a.length
    if la < _EPS:
        return 0.0
    ux, uy = (a.x1 - a.x0) / la, (a.y1 - a.y0) / la
    u0 = (b.x0 - a.x0) * ux + (b.y0 - a.y0) * uy
    v0 = -(b.x0 - a.x0) * uy + (b.y0 - a.y0) * ux
    du = (b.x1 - b.x0) * ux + (b.y1 - b.y0) * uy
    dv = -(b.x1 - b.x0) * uy + (b.y1 - b.y0) * ux
    t0, t1 = 0.0, 1.0
    for p, q in ((-du, u0), (du, la - u0), (-dv, v0 + off), (dv, off - v0)):
        if abs(p) < _EPS:
            if q < -_EPS:
                return 0.0
            continue
        r = q / p
        t0, t1 = (max(t0, r), t1) if p < 0 else (t0, min(t1, r))
    return max(0.0, t1 - t0) * b.length


def _runs_over(mine: Sequence[Seg], others: Iterable[Seg], net: str, max_angle: float,
               max_offset: float | None) -> dict[str, float]:
    """{other net: total length running over `mine`} on one adjacent layer."""
    totals: dict[str, float] = {}
    for b in others:
        if b.net == net:
            continue
        for a in mine:
            if _angle_deg(a, b) >= max_angle:
                continue
            off = max_offset if max_offset is not None else (a.width + b.width) / 2.0
            name = b.net or NO_NET
            totals[name] = totals.get(name, 0.0) + _overlap(a, b, off)
    return totals


def _segments_by_layer(board: Any) -> dict[str, list[Seg]]:
    out: dict[str, list[Seg]] = {}
    for track in _items(board, "tracks"):
        out.setdefault(track["layer"], []).extend(track_segments(track))   # local accumulator
    return out


def _parallel_warning(r: Mapping[str, Any], min_len: float, max_angle: float) -> str:
    return (f"R28: {r['net']} on {r['layer']} is a dual stripline and {r['other_net']} on "
            f"{r['over_layer']} runs parallel over it for {r['length_mm']:.1f} mm (over "
            f"{min_len:g} mm at under {max_angle:g} deg): that copper changes the line's "
            "impedance - move it off the line or cross it at right angles")


def check_dual_stripline_parallel(board: Any, lines: Mapping[str, Any], *,
                                  min_len_mm: float = MIN_LEN_MM,
                                  max_angle_deg: float = MAX_ANGLE_DEG,
                                  max_offset_mm: float | None = None) -> dict[str, Any]:
    """R28: parallel tracks on the layer a dual stripline takes as dielectric (docstring)."""
    by_layer = _segments_by_layer(board)
    runs, checked = [], 0
    for net in sorted(lines):
        layers = (lines[net] or {}).get("layers") or {}
        for layer in sorted(layers, key=layer_order):
            mine = [s for s in by_layer.get(layer, []) if s.net == net]
            adjacent = (layers[layer] or {}).get("dielectric_layers") or []
            if not mine or not adjacent:
                continue
            checked += 1
            for over in sorted(adjacent, key=layer_order):
                totals = _runs_over(mine, by_layer.get(over, []), net, max_angle_deg,
                                    max_offset_mm)
                runs += [{"net": net, "layer": layer, "over_layer": over, "other_net": other,
                          "length_mm": round(length, 3)}
                         for other, length in sorted(totals.items()) if length > min_len_mm]
    warnings = [_parallel_warning(r, min_len_mm, max_angle_deg) for r in runs]
    return _result((), warnings, {"dual_stripline_lines": checked, "parallel_runs": runs})


# ---------------------------------------------------------------- stackup helpers
def _get(obj: Any, key: str) -> Any:
    return obj.get(key) if isinstance(obj, Mapping) else getattr(obj, key, None)


def roles_of(stackup: Any) -> dict[str, dict[str, Any]]:
    """{layer: {role, net}} of a stackup_model.ResolvedStackup or its to_dict()."""
    return {_get(c, "name"): {"role": _get(c, "role"), "net": _get(c, "net")}
            for c in _get(stackup, "copper") or ()}


def _partners(specs: Mapping[str, Any]) -> dict[str, str]:
    """{net: partner} both ways from net_specs[*].diff_pair (first declaration wins)."""
    out: dict[str, str] = {}
    for net in sorted(specs):
        partner = specs[net].get("diff_pair") if isinstance(specs[net], Mapping) else None
        if isinstance(partner, str) and partner and partner != net:
            out.setdefault(net, partner)
            out.setdefault(partner, net)
    return out


def _is_line(spec: Any) -> bool:
    return isinstance(spec, Mapping) and any(spec.get(k) is not None
                                             for k in ("impedance", "diff", "freq_hz"))


def line_layers(stackup: Any, design: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """The artifact `lines` shape ({net: {layers: {layer: {line, ref, dielectric_layers}}}})
    of every impedance / RF net (and its diff partner) on every routable layer where its line
    resolves (tline.geometry.resolve_line; no width synthesis)."""
    specs = design.get("net_specs") if isinstance(design, Mapping) else None
    specs = specs if isinstance(specs, Mapping) else {}
    routable = [_get(c, "name") for c in _get(stackup, "copper") or ()
                if _get(c, "role") in geometry.ROUTABLE]
    out: dict[str, dict[str, Any]] = {}
    for net in sorted(n for n, s in specs.items() if _is_line(s)):
        setups = [geometry.resolve_line(stackup, layer, specs[net]) for layer in routable]
        out[net] = {"layers": {s.layer: {"line": s.line, "ref": s.ref_dict(),
                                         "dielectric_layers": list(s.dielectric_layers)}
                               for s in setups if s.ok}}
    for net, partner in _partners(specs).items():
        if net in out and partner not in out:
            out[partner] = {"layers": {k: {**v, "ref": dict(v["ref"])}
                                       for k, v in out[net]["layers"].items()}}
    return dict(sorted(out.items()))


# ---------------------------------------------------------------- all
def run_checks(board: Any, roles: Mapping[str, Any], lines: Mapping[str, Any], *,
               max_dist_mm: float = MAX_DIST_MM, min_len_mm: float = MIN_LEN_MM,
               max_angle_deg: float = MAX_ANGLE_DEG,
               max_offset_mm: float | None = None) -> dict[str, Any]:
    """R8 + R27 + R28: {issues (R8 blockers), warnings (R27 / R28), metrics {plane_layers,
    ref_transitions, dual_stripline}}."""
    parts = {"plane_layers": check_plane_layers(board, roles),
             "ref_transitions": check_ref_transitions(board, lines, max_dist_mm=max_dist_mm),
             "dual_stripline": check_dual_stripline_parallel(
                 board, lines, min_len_mm=min_len_mm, max_angle_deg=max_angle_deg,
                 max_offset_mm=max_offset_mm)}
    return {"issues": [i for p in parts.values() for i in p["issues"]],
            "warnings": [w for p in parts.values() for w in p["warnings"]],
            "metrics": {name: p["metrics"] for name, p in parts.items()}}
