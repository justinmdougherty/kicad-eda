#!/usr/bin/env python3
"""LGA inner-pad strategies as netlist hints (F2-S1b; ROADMAP D1, spike F2-S1).

An LGA module's inner ground-pad grid (the LG290P's 55) must reach the GND plane. F2-S1 built
an LG290P breakout through the real pipeline on KiCad 9.0.4 and 10.0.5 with each strategy
(F2S1_MEASURED): a = nothing added, b = an F.Cu GND zone (fails), c = a designer via outside
each lattice corner pad, d = emc.stitch, e1..e4 = b+d, b+c, f+d, f+b, and the winner f =
GENERATED escape vias. ``netlist_hints`` returns the netlist keys a strategy adds.

Frame: ``pads`` and hints are in the FOOTPRINT frame (mm, footprint origin, +Y down,
unrotated). ``place_hints`` maps preroute xy points and vias to board = origin + R(rot)(x, y):
origin = the part's footprint origin (D3 ``at``), R = KiCad's rotation (degrees, counter-
clockwise on screen: x' = x cos + y sin, y' = -x sin + y cos, as pcbnew 9.0.4 places pads). A
part placed by x/y sits by its courtyard-bbox centre, its footprint origin only when the
courtyard is centred on it (the LG290P's and LC29H's are). ``apply_hints`` merges placed hints
into a NEW netlist dict. Front-side parts only.

Strategy (f), F2-S1's generator: (1) the lattice = the largest one-size group of ``gnd_pads``
with F.Cu copper (ties: the larger pad); (2) each adjacent column x row pair whose 4 corners
are lattice pads is a channel, a via site when they are the copper nearest its centre c;
(3) via d = min(max_via, 2g - 0.05), g = distance from c to the nearest copper, drill =
max(min_via_drill, d - 2 min_via_annular); skipped and reported when d < min_via_diameter, the
annulus < min_via_annular, or other-net copper is within min_clearance of the via or a stub;
(4) two F.Cu stubs (0.25 mm, >= min_track_width) through c join the diagonal corners, the
first with the via: A -> c -> C, B -> c -> D, A..D = (x0,y0), (x1,y0), (x1,y1), (x0,y1);
(5) sorted by (y, x) of c: one output for any pad order. Stdlib only, Python 3.11.
"""
from __future__ import annotations

import copy
import difflib
import math
from collections.abc import Iterable, Mapping
from itertools import pairwise
from types import MappingProxyType
from typing import Any, NamedTuple

WINNER = "f"
STRATEGIES: Mapping[str, str] = MappingProxyType({
    "a": "a_default", "b": "b_fcu_zone", "c": "c_designer_vias", "d": "d_emc_stitch",
    "e1": "e1_fcu_zone_emc", "e2": "e2_fcu_zone_designer_vias", "e3": "e3_escape_vias_emc",
    "e4": "e4_escape_vias_fcu_zone", "f": "f_escape_vias"})
_PARTS: Mapping[str, tuple[str, ...]] = MappingProxyType({
    "a": (), "b": ("b",), "c": ("c",), "d": ("d",), "e1": ("b", "d"), "e2": ("b", "c"),
    "e3": ("f", "d"), "e4": ("f", "b"), "f": ("f",)})
_DESCRIBE: Mapping[str, str] = MappingProxyType({
    "a": "nothing added: the builder's default planes (In1.Cu GND) and the router's own vias",
    "b": "an F.Cu GND zone poured before routing", "d": "GND stitching vias (emc.stitch)",
    "c": "one designer via diagonally outside each corner pad of the ground lattice",
    "f": "one GND via per 2x2 ground-pad channel of the inner grid, two F.Cu stubs joining "
         "its diagonal corner pads; keep the design's planes (In1.Cu GND)"})
_OK, _ZONE = "PASS, DRC 0 / unconnected 0 on KiCad 9.0.4 (Freerouting", "(F.Cu zone)"
# F2-S1 (SCR wave2/F2-S1/results.json): LG290P breakout, 48 x 32 mm, 4 layers, parts fixed
F2S1_MEASURED: Mapping[str, str] = MappingProxyType({
    "a": f"{_OK} 35.1 s) and 10.0.5 (66.8 s)",
    "b": "FAIL: 4 unconnected on KiCad 9.0.4 and 2 on 10.0.5; Freerouting placed no GND vias "
         "and the F.Cu zone broke into islands",
    "c": f"{_OK} 45.7 s) and 10.0.5 (59.4 s)",
    "d": f"{_OK} 46.6 s, 60 stitching vias) and 10.0.5 (71.7 s, 62)",
    "e1": f"FAIL: 3 unconnected on KiCad 9.0.4 and 10.0.5 {_ZONE}",
    "e2": f"FAIL: 1 unconnected on KiCad 9.0.4 and 10.0.5 {_ZONE}",
    "e3": f"{_OK} 11.4 s, 55 stitching vias) and 10.0.5 (14.0 s)",
    "e4": f"FAIL: 2 unconnected on KiCad 9.0.4 and 3 on 10.0.5 {_ZONE}",
    "f": f"the winner, {_OK} 12.0 s; 10.2 s run alone) and 10.0.5 (15.3 s); 43 / 42 GND "
         "vias under the module against 6 for (a)"})
# = swig_build_board.FAB_RULES_MM (m_ViasMinSize, m_MinThroughDrill, m_MinClearance,
# m_TrackMinWidth; annulus (0.45 - 0.2) / 2), named like pcb_api's board minimums (mm)
DEFAULT_FAB: Mapping[str, float] = MappingProxyType({
    "min_via_diameter": 0.45, "min_via_drill": 0.2, "min_via_annular": 0.125,
    "min_clearance": 0.15, "min_track_width": 0.15})
VIA_D_MAX, VIA_PAD_GAP, STUB_W = 0.5, 0.05, 0.25      # F2-S1: largest via, annulus gap, stub
DESIGNER_VIA, DESIGNER_W = (0.6, 0.3), 0.3            # strategy (c): F2-S1's hand-written via
FRONT_COPPER = ("F.Cu", "*.Cu", "F&B.Cu")
SAMPLE_MM, EPS = 0.05, 1e-9                           # stub clearance sampling step
FRAME_NOTE = ("footprint frame: mm, footprint origin, +Y down, unrotated; place_hints / "
              "apply_hints map xy points and vias to the board (origin + KiCad rotation)")


class PadRec(NamedTuple):
    """A copper pad in the footprint frame (rotation in degrees, KiCad's sense)."""
    number: str
    x: float
    y: float
    w: float
    h: float
    shape: str = "rect"
    rotation: float = 0.0
    front: bool = True             # copper on F.Cu
    net: str | None = None


def _field(p: Any, name: str, default: Any = None) -> Any:
    return p.get(name, default) if isinstance(p, Mapping) else getattr(p, name, default)


def _r(v: float) -> float:
    return round(v, 4) + 0.0


def pad_records(pads: Iterable[Any]) -> tuple[PadRec, ...]:
    """A PadRec per copper pad of fp_reader ParsedPads, fp_author_templates Pads or dicts
    {number, x, y, w, h, shape?, rotation?, layers?, net?} (no layers: F.Cu)."""
    out = []
    for p in pads:
        if isinstance(p, PadRec):
            out.append(p)
            continue
        layers = tuple(_field(p, "pad_layers") or _field(p, "layers") or ("F.Cu",))
        if not any(str(layer).endswith(".Cu") for layer in layers):
            continue
        number, geom = str(_field(p, "number", "") or ""), [_field(p, k) for k in "xywh"]
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in geom):
            raise ValueError(f"pad {number!r} needs x, y, w and h (numbers, mm)")
        out.append(PadRec(number, *(float(v) for v in geom),
                          str(_field(p, "shape", "rect") or "rect"),
                          float(_field(p, "rotation", 0.0) or 0.0),
                          any(layer in FRONT_COPPER for layer in layers), _field(p, "net")))
    return tuple(out)


def rotate(x: float, y: float, deg: float) -> tuple[float, float]:
    """(x, y) rotated by ``deg`` in KiCad's sense (counter-clockwise on screen, +Y down)."""
    a = math.radians(deg)
    c, s = math.cos(a), math.sin(a)
    return x * c + y * s, -x * s + y * c


def place_point(x: float, y: float, origin: Iterable[float], rot: float = 0.0) -> tuple:
    """A footprint-frame point in the board frame of a part at ``origin`` rotated ``rot``."""
    ox, oy = origin
    rx, ry = rotate(x, y, rot) if rot else (x, y)
    return _r(ox + rx), _r(oy + ry)


def pad_gap(x: float, y: float, pad: PadRec) -> float:
    """Distance from (x, y) to the pad's copper, 0 inside it (rect for roundrect and custom
    shapes: never larger than the true distance)."""
    dx, dy = rotate(x - pad.x, y - pad.y, -pad.rotation)
    hw, hh = pad.w / 2, pad.h / 2
    if pad.shape == "circle":
        return max(0.0, math.hypot(dx, dy) - hw)
    r = min(hw, hh) if pad.shape == "oval" else 0.0
    return max(0.0, math.hypot(max(abs(dx) - hw + r, 0.0), max(abs(dy) - hh + r, 0.0)) - r)


def fab_rules(fab: Mapping[str, float] | None = None) -> dict[str, float]:
    """DEFAULT_FAB overridden by ``fab`` (ValueError: an unknown rule or a negative value)."""
    given = dict(fab or {})
    for key, value in sorted(given.items()):
        if key not in DEFAULT_FAB:
            near = difflib.get_close_matches(key, list(DEFAULT_FAB), n=1)
            hint = f" (did you mean {near[0]!r}?)" if near else ""
            raise ValueError(f"unknown fab rule {key!r}{hint}; rules: {', '.join(DEFAULT_FAB)}")
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            raise ValueError(f"fab rule {key} must be a number >= 0 (mm), got {value!r}")
    return {**DEFAULT_FAB, **{k: float(v) for k, v in given.items()}}


def _ground(recs: tuple[PadRec, ...], gnd_pads: Iterable[str] | None,
            gnd_net: str) -> frozenset[str]:
    if gnd_pads is None:
        found = frozenset(p.number for p in recs if p.number and p.net == gnd_net)
        if not found:
            raise ValueError(f"no pad record carries net {gnd_net!r}: pass gnd_pads=[the pad "
                             f"numbers on {gnd_net}]")
        return found
    gnd = frozenset(str(n) for n in ([gnd_pads] if isinstance(gnd_pads, str) else gnd_pads))
    unknown = sorted(gnd - {p.number for p in recs})
    if unknown:
        raise ValueError(f"gnd_pads not pads of this footprint: {unknown}")
    return gnd


def _lattice(recs: tuple[PadRec, ...], gnd: frozenset[str]) -> dict[tuple[float, float], PadRec]:
    groups: dict[tuple[float, float], list[PadRec]] = {}
    for p in sorted(recs, key=lambda q: (q.y, q.x, q.number)):
        if p.number in gnd and p.front:
            groups.setdefault((_r(p.w), _r(p.h)), []).append(p)
    if not groups:
        return {}
    size = max(sorted(groups), key=lambda k: (len(groups[k]), k[0] * k[1]))
    return {(_r(p.x), _r(p.y)): p for p in reversed(groups[size])}      # the first pad wins


def _stubs_clear(c: tuple[float, float], corners: list[PadRec], foreign: list[PadRec],
                 need: float) -> bool:
    longest = max(math.hypot(p.x - c[0], p.y - c[1]) for p in corners)
    if not foreign or min(pad_gap(*c, q) for q in foreign) >= longest + need:
        return True
    for p in corners:
        n = max(2, math.ceil(math.hypot(p.x - c[0], p.y - c[1]) / SAMPLE_MM))
        for i in range(n + 1):
            x, y = p.x + (c[0] - p.x) * i / n, p.y + (c[1] - p.y) * i / n
            if any(pad_gap(x, y, q) < need - EPS for q in foreign):
                return False
    return True


def _channel(c: tuple[float, float], corners: list[PadRec], recs: tuple[PadRec, ...],
             gnd: frozenset[str], rules: Mapping[str, float], max_via: float) -> dict:
    """{"at", "via_size", "corner_pads", "gap"} or {"at", "reason"} (skipped)."""
    at = [c[0], c[1]]
    g = min(pad_gap(*c, p) for p in corners)
    others = [p for p in recs if all(p is not q for q in corners)]
    if others and min(pad_gap(*c, p) for p in others) < g - EPS:
        return {"at": at, "reason": "a pad other than its 4 corner pads is nearer to it"}
    d = round(min(max_via, 2 * g - VIA_PAD_GAP), 3)
    if d < rules["min_via_diameter"] - EPS:
        return {"at": at, "reason": f"pad gap {g:.3f} mm leaves a {d:.3f} mm via, below "
                                    f"min_via_diameter {rules['min_via_diameter']:g}"}
    drill = round(max(rules["min_via_drill"], d - 2 * rules["min_via_annular"]), 3)
    if (d - drill) / 2 < rules["min_via_annular"] - EPS:
        return {"at": at, "reason": f"via {d:g}/{drill:g}: annulus below min_via_annular "
                                    f"{rules['min_via_annular']:g}"}
    foreign = [p for p in others if p.number not in gnd]
    if foreign and min(pad_gap(*c, p) for p in foreign) - d / 2 < rules["min_clearance"] - EPS:
        return {"at": at, "reason": "other-net copper within min_clearance of the via"}
    stub = max(STUB_W, rules["min_track_width"])
    if not _stubs_clear(c, corners, foreign, stub / 2 + rules["min_clearance"]):
        return {"at": at, "reason": "other-net copper within min_clearance of a stub"}
    return {"at": at, "via_size": [d, drill], "corner_pads": [p.number for p in corners],
            "gap": _r(g)}


def escape_channels(pads: Iterable[Any], gnd_pads: Iterable[str], *,
                    fab: Mapping[str, float] | None = None, max_via: float = VIA_D_MAX) -> dict:
    """Strategy (f) sites, footprint frame: {"lattice": [columns, rows], "pad_size", "vias":
    [{"at", "via_size", "corner_pads": [A, B, C, D], "gap"}], "skipped": [{"at", "reason"}]}."""
    rules, recs = fab_rules(fab), pad_records(pads)
    gnd = _ground(recs, gnd_pads, "")
    lattice = _lattice(recs, gnd)
    xs, ys = sorted({x for x, _ in lattice}), sorted({y for _, y in lattice})
    vias, skipped = [], []
    for y0, y1 in pairwise(ys):
        for x0, x1 in pairwise(xs):
            corners = [lattice.get(k) for k in ((x0, y0), (x1, y0), (x1, y1), (x0, y1))]
            if all(corners):
                site = _channel((_r((x0 + x1) / 2), _r((y0 + y1) / 2)), corners, recs, gnd,
                                rules, max_via)
                (vias if "via_size" in site else skipped).append(site)
    first = next(iter(lattice.values()), None)
    return {"lattice": [len(xs), len(ys)], "pad_size": [_r(first.w), _r(first.h)] if first
            else [], "vias": vias, "skipped": skipped}


def _escape(recs: tuple[PadRec, ...], gnd: frozenset[str], ref: str, net: str,
            rules: dict[str, float], max_via: float) -> tuple[list[dict], list[str]]:
    ch = escape_channels(recs, gnd, fab=rules, max_via=max_via)
    width = max(STUB_W, rules["min_track_width"])
    out = []
    for v in ch["vias"]:
        a, b, c, d = v["corner_pads"]
        xy = ["xy", *v["at"]]
        out += [{"net": net, "width": width, "layer": "F.Cu",
                 "path": [["pad", ref, a], xy, ["pad", ref, c]],
                 "vias": [list(v["at"])], "via_size": list(v["via_size"])},
                {"net": net, "width": width, "layer": "F.Cu",
                 "path": [["pad", ref, b], list(xy), ["pad", ref, d]]}]
    return out, _escape_notes(ch)


def _escape_notes(ch: dict) -> list[str]:
    cols, rows = ch["lattice"]
    notes = [f"skipped channel at ({s['at'][0]:g}, {s['at'][1]:g}): {s['reason']}"
             for s in ch["skipped"]]
    if not ch["vias"]:
        return [*notes, "no 2x2 ground-pad channel takes a via: nothing generated; the ground "
                        "pads route as ordinary pads to the In1.Cu GND plane (strategy a)"]
    sizes = sorted({f"{d:g}/{h:g}" for d, h in (v["via_size"] for v in ch["vias"])})
    w, h = ch["pad_size"]
    web = min(v["gap"] - v["via_size"][0] / 2 for v in ch["vias"])
    return [f"lattice {cols} x {rows} of {w:g} x {h:g} mm pads: {len(ch['vias'])} vias "
            f"{', '.join(sizes)} mm, {len(ch['skipped'])} channels skipped", *notes,
            f"DFM: via annulus to same-net pad copper {web:.3f} mm; the vias are tented, but a "
            f"fab DFM check may flag the mask web (max_via=0.45 widens it)",
            "never pour GND on F.Cu or B.Cu before routing: F2-S1's F.Cu zone variants "
            "(b, e1, e2, e4) all failed"]


def _designer(recs: tuple[PadRec, ...], gnd: frozenset[str], ref: str, net: str,
              rules: dict[str, float]) -> tuple[list[dict], list[str]]:
    lattice = _lattice(recs, gnd)
    xs, ys = sorted({x for x, _ in lattice}), sorted({y for _, y in lattice})
    d, drill = DESIGNER_VIA
    out, notes = [], []
    corners = [(x, sx, y, sy) for y, sy in ((ys[0], -1), (ys[-1], 1))
               for x, sx in ((xs[0], -1), (xs[-1], 1))] if len(xs) > 1 and len(ys) > 1 else []
    for x, sx, y, sy in corners:
        p = lattice.get((x, y))
        if p is None:
            continue
        step = d / 2 + rules["min_clearance"]
        v = [_r(x + sx * (p.w / 2 + step)), _r(y + sy * (p.h / 2 + step))]
        if min((pad_gap(*v, q) for q in recs if q is not p), default=math.inf) < step - EPS:
            notes.append(f"corner via at ({v[0]:g}, {v[1]:g}) skipped: copper too near")
            continue
        out.append({"net": net, "width": DESIGNER_W, "layer": "F.Cu",
                    "path": [["pad", ref, p.number], ["xy", *v]], "vias": [v],
                    "via_size": [d, drill]})
    return out, [f"{len(out)} corner vias {d:g}/{drill:g} mm", *notes]


def strategy_id(strategy: str) -> str:
    """'f' for 'f' or 'f_escape_vias' (ValueError for an unknown strategy)."""
    for sid, name in STRATEGIES.items():
        if strategy in (sid, name):
            return sid
    raise ValueError(f"unknown strategy {strategy!r}; strategies: "
                     f"{', '.join(f'{k} ({v})' for k, v in STRATEGIES.items())}")


def netlist_hints(pads: Iterable[Any], *, gnd_net: str = "GND", strategy: str = WINNER,
                  fab: Mapping[str, float] | None = None, gnd_pads: Iterable[str] | None = None,
                  ref: str = "U1", max_via: float = VIA_D_MAX,
                  emc_max_freq_hz: float | None = None) -> dict:
    """The netlist keys F2-S1 strategy ``strategy`` adds for a part with these pads, footprint
    frame: {"planes"?: {layer: net} to merge over the design's, "preroutes"?: [...] for part
    ``ref``, "emc"?, "notes"}. ``gnd_pads`` = the pads on ``gnd_net`` (default: records whose
    ``net`` is ``gnd_net``); ``fab`` overrides DEFAULT_FAB. A fresh dict every call."""
    sid, rules, recs = strategy_id(strategy), fab_rules(fab), pad_records(pads)
    gnd = _ground(recs, gnd_pads, gnd_net)
    winner = ", the F2-S1 winner" if sid == WINNER else ""
    hints: dict[str, Any] = {}
    notes = [f"strategy {sid} ({STRATEGIES[sid]}{winner}): "
             + "; ".join(_DESCRIBE[p] for p in _PARTS[sid] or ("a",)),
             f"F2-S1, LG290P breakout: {F2S1_MEASURED[sid]}", FRAME_NOTE]
    for part in _PARTS[sid]:
        if part == "b":
            hints["planes"] = {"F.Cu": gnd_net}
        elif part == "d":
            freq = {"max_freq_hz": float(emc_max_freq_hz)} if emc_max_freq_hz else {}
            hints["emc"] = {"stitch": True, **freq}
        else:
            made = (_escape(recs, gnd, ref, gnd_net, rules, max_via) if part == "f"
                    else _designer(recs, gnd, ref, gnd_net, rules))
            hints["preroutes"] = [*hints.get("preroutes", []), *made[0]]
            notes.extend(made[1])
    if not hints.get("preroutes", True):
        del hints["preroutes"]
    return {**hints, "notes": notes}


def _place_preroute(pr: Mapping, origin: Iterable[float], rot: float, ref: str | None) -> dict:
    path = [["xy", *place_point(p[1], p[2], origin, rot), *p[3:]] if p[0] == "xy"
            else ["pad", ref or p[1], *p[2:]] for p in pr["path"]]
    vias = {"vias": [list(place_point(v[0], v[1], origin, rot)) for v in pr["vias"]]} \
        if "vias" in pr else {}
    return {**copy.deepcopy(dict(pr)), "path": path, **vias}


def place_hints(hints: Mapping, *, origin: Iterable[float], rot: float = 0.0,
                ref: str | None = None) -> dict:
    """A board-frame copy of footprint-frame hints for a part whose footprint origin is at
    ``origin`` rotated ``rot`` (KiCad degrees); ``ref`` renames the pad points' part."""
    origin = tuple(float(v) for v in origin)
    out = copy.deepcopy(dict(hints))
    if "preroutes" in out:
        out["preroutes"] = [_place_preroute(pr, origin, float(rot), ref)
                            for pr in hints["preroutes"]]
    return out


def builder_default_planes(design: Mapping) -> dict[str, str]:
    """build_features.default_planes, stdlib-only (a test pins the parity)."""
    planes = design.get("planes")
    if planes is not None:
        return dict(planes)
    nets_avail = [n for n in design.get("nets", {}) if n != "GND"]
    power = (next((n for n in nets_avail if n.startswith("+")), None)
             or next((n for n in nets_avail if "V" in n.upper()), None))
    return {"In1.Cu": "GND", **({"In2.Cu": power} if power else {})}


def apply_hints(design: Mapping, hints: Mapping, *, ref: str,
                origin: Iterable[float] | None = None, rot: float | None = None) -> dict:
    """A NEW netlist: ``hints`` placed at part ``ref`` (its ``at``, else its x/y = courtyard
    centre, unless ``origin``/``rot`` are given), preroutes appended, planes merged over the
    design's (or the builder default), emc merged over the design's. Notes are not copied."""
    comp = next((c for c in design.get("components", []) if c.get("ref") == ref), None)
    if comp is None:
        raise ValueError(f"no component {ref!r} in the design")
    if str(comp.get("side", "F")).upper() == "B":
        raise ValueError(f"{ref} is on side B: lga_strategy hints are for front-side parts")
    at = comp.get("at") or ([comp["x"], comp["y"], comp.get("rot") or 0.0]
                            if "x" in comp and "y" in comp else [None, None, comp.get("rot")])
    if origin is None and at[0] is None:
        raise ValueError(f"{ref} has no placement ('at' or x/y): pass origin=")
    placed = place_hints(hints, origin=at[:2] if origin is None else origin,
                         rot=float(at[2] or 0.0) if rot is None else rot, ref=ref)
    out = copy.deepcopy(dict(design))
    if placed.get("preroutes"):
        out["preroutes"] = [*out.get("preroutes", []), *placed["preroutes"]]
    if placed.get("planes"):
        out["planes"] = {**builder_default_planes(design), **placed["planes"]}
    if placed.get("emc"):
        out["emc"] = {**(design.get("emc") or {}), **placed["emc"]}
    return out
