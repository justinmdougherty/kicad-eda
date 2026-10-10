"""Line geometry from a resolved stackup (ADR D7.6 'Reference resolution' 1-6; task D7-07).

resolve_line(stackup, layer, spec) walks up and down from the trace's copper layer (stackup: a
stackup_model.ResolvedStackup or its to_dict()). (1) A side's reference is the first plane-role
copper, or mixed copper whose net is in ref_nets (impedance ["GND", every plane/mixed net]; RF -
rf_freq: freq_hz a number > 0 - ["GND"]), unless ref_layers {layer: [...]} names it. (2) Copper
in between is in void_layers (dielectric, cut out) or routed copper taken as dielectric (dual
stripline; non-RF only), else ST-E9. (3) Plies run outward; passed copper is a ply filled with
the adjacent prepreg (else the lower-Er neighbour). (4) Outer + one reference: microstrip
(masked); inner + two: stripline / dual_stripline; inner + one: embedded microstrip (cover up to
the next copper; warned when it is routed). line auto: outer + freq_hz -> cpwg; cpwg adds
same-layer grounds on any layer; cpw: outer, no back plane. pair: edge (gap = the netlist gap)
or broadside (pair 'broadside', layers [La, Lb] adjacent). (5) Power references: ST-W10; RF
references outside ref_nets: ST-E10. (6) Heights come from plies only. Width = the spec's
explicit width or 0 (tline.synth fills it). Warnings ('<layer>: ...') flag a request the stackup
overrides or a routed layer over the line. Copper rules for tline.synth: width_window,
default_ground_gap and pour_clearance (ADR D7.6). Stdlib only.
"""
from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, NamedTuple

from . import evaluate as _closed_form
from .fallback import apply as _fallback
from .stripline import dielectric_copper_ply
from .types import Issue, LineGeometry, MaskModel, Ply

LINES = ("auto", "microstrip", "cpwg", "stripline", "dual_stripline", "embedded_microstrip", "cpw")
CLOSED = ("stripline", "dual_stripline")
ROUTABLE = ("signal", "mixed")
NET_ROLES = ("plane", "mixed")
RF_REF_NETS = ("GND",)
GROUND_NAMES = ("GND", "VSS", "0V", "GROUND")
E9_FIX = "declare a plane/mixed GND layer next to it, route on another layer, or set ref_layers"
WINDOW_MIN_MM, WINDOW_PCT = 0.005, 1.5
CPWG_MIN_GAP_MM = 0.15
POUR_Z_RATIO = 0.99


def is_ground(net: str | None) -> bool:
    """A ground net by name: GND / VSS / 0V / GROUND, or containing GND (AGND, GND_RF)."""
    up = (net or "").upper()
    return bool(up) and (up in GROUND_NAMES or "GND" in up)


def rf_freq(spec: Mapping[str, Any]) -> float | None:
    """The net's RF frequency: freq_hz when it is a finite number > 0, else None (not RF)."""
    f = spec.get("freq_hz")
    ok = isinstance(f, (int, float)) and not isinstance(f, bool) and math.isfinite(f) and f > 0
    return float(f) if ok else None


def _get(obj: Any, key: str, default: Any = None) -> Any:
    return obj.get(key, default) if isinstance(obj, Mapping) else getattr(obj, key, default)


Copper = NamedTuple("Copper", [("name", str), ("thickness", float), ("role", str),
                               ("net", "str | None")])
Dielectric = NamedTuple("Dielectric", [("kind", str), ("plies", tuple)])     # plies top down
Stack = NamedTuple("Stack", [("copper", tuple), ("dielectrics", tuple),
                             ("mask", "MaskModel | None")])


def _ply(p: Any) -> Ply:
    return Ply(float(_get(p, "thickness")), float(_get(p, "er")), float(_get(p, "df") or 0.0),
               float(_get(p, "er_freq_hz") or 1e9))


def _mask(m: Any) -> MaskModel | None:
    if m is None or _get(m, "source") == "none" or _get(m, "dk") is None:
        return None
    return MaskModel(float(_get(m, "dk")), float(_get(m, "over_trace")),
                     float(_get(m, "over_substrate")), float(_get(m, "between_traces")))


def stack_view(stackup: Any) -> Stack:
    """The parts of a ResolvedStackup (or its dict form) that line geometry needs."""
    copper = tuple(Copper(_get(c, "name"), float(_get(c, "thickness")), _get(c, "role"),
                          _get(c, "net")) for c in _get(stackup, "copper"))
    dies = tuple(Dielectric(_get(d, "kind"), tuple(_ply(p) for p in _get(d, "plies")))
                 for d in _get(stackup, "dielectrics"))
    return Stack(copper, dies, _mask(_get(stackup, "mask")))


class Side(NamedTuple):
    """One side of the trace: its reference (None: none) and the plies outward to it."""

    ref: str | None
    net: str | None
    plies: tuple[Ply, ...]                  # to the reference; without one, up to the next copper
    passed: tuple[tuple[str, Ply], ...]     # routed layers taken as dielectric (dual stripline)
    voided: tuple[str, ...]
    first: Copper | None                    # the next copper on this side
    blocked: str | None = None              # an impassable layer with a reference beyond it


@dataclass(frozen=True)
class LineSetup:
    """A line resolved on one layer: its geometry (None on an error) and what it rests on."""

    layer: str
    geom: LineGeometry | None
    line: str | None = None
    ref_above: tuple[str, ...] = ()
    ref_below: tuple[str, ...] = ()
    ref_nets: tuple[str, ...] = ()
    void_layers: tuple[str, ...] = ()
    dielectric_layers: tuple[str, ...] = ()
    coplanar: bool = False
    rf: bool = False
    issues: tuple[Issue, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.geom is not None and not any(i.severity == "error" for i in self.issues)

    def ref_dict(self) -> dict[str, list[str]]:
        return {"below": list(self.ref_below), "above": list(self.ref_above),
                "nets": list(self.ref_nets)}


Rules = NamedTuple("Rules", [("rf", bool), ("ref_nets", frozenset), ("override", "tuple | None"),
                             ("voids", frozenset)])


def _per_layer(value: Any, layer: str) -> tuple[str, ...] | None:
    found = value.get(layer) if isinstance(value, Mapping) else None
    return None if found is None else tuple(found)


def _rules(stack: Stack, spec: Mapping[str, Any], layer: str) -> Rules:
    rf = rf_freq(spec) is not None
    planes = [c.net for c in stack.copper if c.role in NET_ROLES and c.net]
    ref_nets = spec.get("ref_nets") or (RF_REF_NETS if rf else ["GND", *planes])
    return Rules(rf, frozenset(ref_nets), _per_layer(spec.get("ref_layers"), layer),
                 frozenset(_per_layer(spec.get("void_layers"), layer) or ()))


def _is_ref(cu: Copper, rules: Rules) -> bool:
    if rules.override is not None:
        return cu.name in rules.override and cu.role in NET_ROLES
    return cu.role == "plane" or (cu.role == "mixed" and cu.net in rules.ref_nets)


def _steps(stack: Stack, k: int, up: bool) -> list[tuple[tuple[Ply, ...], str, Copper]]:
    """(plies outward, dielectric kind, the copper beyond) from copper k to F.Cu or B.Cu."""
    if up:
        return [(stack.dielectrics[j].plies[::-1], stack.dielectrics[j].kind, stack.copper[j])
                for j in range(k - 1, -1, -1)]
    return [(stack.dielectrics[j].plies, stack.dielectrics[j].kind, stack.copper[j + 1])
            for j in range(k, len(stack.copper) - 1)]


def _fill(thickness: float, near: Sequence[tuple[Ply, str]]) -> Ply:
    """The ply standing for a passed copper layer: the adjacent prepreg, else the lower Er."""
    return dielectric_copper_ply(thickness, min(near, key=lambda pk: (pk[1] != "prepreg",
                                                                      pk[0].er))[0])


def walk(stack: Stack, k: int, up: bool, rules: Rules) -> Side:
    """One side of copper k walked to its reference (D7.6 steps 1-3)."""
    steps = _steps(stack, k, up)
    first = steps[0][2] if steps else None
    plies, passed, voided, blocked = [], [], [], None
    for i, (outward, kind, cu) in enumerate(steps):
        plies.extend(outward)
        if _is_ref(cu, rules):
            if blocked:
                return Side(None, None, (), (), (), first, blocked)
            return Side(cu.name, cu.net, tuple(plies), tuple(passed), tuple(voided), first)
        beyond = [(steps[i + 1][0][0], steps[i + 1][1])] if i + 1 < len(steps) else []
        ply = _fill(cu.thickness, [(outward[-1], kind), *beyond])
        plies.append(ply)
        if cu.name in rules.voids:
            voided.append(cu.name)
        elif cu.role in ROUTABLE and not rules.rf:
            passed.append((cu.name, ply))
        else:
            blocked = blocked or cu.name
    return Side(None, None, steps[0][0] if steps else (), (), (), first)


# ---------------------------------------------------------------- classification
def _error(layer: str, message: str, path: str) -> LineSetup:
    return LineSetup(layer, None, issues=(Issue("ST-E9", "error", message, path),))


def _base_kind(outer: bool, up: Side, down: Side) -> str:
    if up.ref and down.ref:
        return "dual_stripline" if up.passed or down.passed else "stripline"
    return "microstrip" if outer else "embedded_microstrip"


def _requested(line: Any, base: str, outer: bool, rf: bool) -> tuple[str | None, bool, str]:
    """(kind, or None when impossible; coplanar; note) for the spec's line request."""
    if line in (None, "auto"):
        return ("cpwg", True, "") if outer and rf else (base, False, "")
    if line not in LINES:
        return None, False, f"unknown line {line!r} (one of {', '.join(LINES)})"
    if line == "cpwg":
        return ("cpwg", True, "") if outer else (
            base, True, f"line cpwg on an inner layer: a {base} with same-layer grounds")
    if line == "cpw":
        return ("cpw", True, "") if outer else (None, False, "line cpw needs an outer layer")
    if line == base or (line in CLOSED and base in CLOSED):
        return base, False, ("" if line == base else
                             f"line {line} taken as {base}: a routed layer lies between")
    return None, False, f"line {line} is impossible on this layer: the stackup gives a {base}"


def _ref_issues(rules: Rules, refs: Sequence[Side], layer: str, path: str) -> tuple[Issue, ...]:
    if rules.rf:
        bad = [s for s in refs if s.net not in rules.ref_nets]
        if bad:
            return tuple(Issue("ST-E10", "error", f"{layer}: RF reference {s.ref} carries "
                               f"{s.net}, not in ref_nets {sorted(rules.ref_nets)}: reference "
                               "GND or add the net to ref_nets", f"{path}.ref_nets") for s in bad)
    power = [s for s in refs if not is_ground(s.net)]
    if not power:
        return ()
    names = ", ".join(f"{s.ref} ({s.net})" for s in power)
    return (Issue("ST-W10", "warning", f"{layer}: references a power plane {names}: return "
                  f"current crosses to {power[0].net} at every via transition; keep stitching "
                  "caps near transitions", path),)


def _warnings(layer: str, kind: str, below: Side, other: Side, coplanar: bool,
              spec: Mapping[str, Any], note: str) -> tuple[str, ...]:
    """'<layer>: ...' for a line request the stackup overrides or a routed layer over the line."""
    out = [note] if note else []
    if kind == "embedded_microstrip" and other.first and other.first.role in ROUTABLE:
        out.append(f"embedded microstrip: the next copper {other.first.name} is a routed layer: "
                   "its tracks over the line change Z (keep them off it)")
    if not coplanar and spec.get("ground_gap") is not None:
        out.append(f"ground_gap ignored: a {kind} has no same-layer grounds (set line cpwg)")
    if kind == "cpw" and below.ref:
        out.append(f"cpw over the {below.ref} plane is a CPWG (line cpwg); the substrate is "
                   "taken down to that plane")
    return tuple(f"{layer}: {w}" for w in out)


def _geometry(stack: Stack, k: int, kind: str, below: Side, above: tuple[Ply, ...],
              passed: tuple[tuple[str, Ply], ...], coplanar: bool,
              spec: Mapping[str, Any]) -> LineGeometry:
    outer = k in (0, len(stack.copper) - 1)
    diff = spec.get("diff") is not None
    return LineGeometry(
        kind=kind, pair="edge" if diff else "single", width=float(spec.get("width") or 0.0),
        t=stack.copper[k].thickness, below=below.plies, above=above,
        pair_gap=spec.get("gap") if diff else None,
        ground_gap=spec.get("ground_gap") if coplanar else None,
        mask=stack.mask if outer else None, layers=(stack.copper[k].name,),
        dielectric_copper=tuple((name, ply.er) for name, ply in passed))


def _classify(stack: Stack, k: int, up: Side, down: Side, spec: Mapping[str, Any],
              rules: Rules, path: str) -> LineSetup:
    layer, outer = stack.copper[k].name, k in (0, len(stack.copper) - 1)
    blocked = up.blocked or down.blocked
    if blocked:
        why = "an RF line cannot take a routed layer as dielectric" if rules.rf else "a plane"
        return _error(layer, f"{layer}: {blocked} lies between the line and its reference "
                      f"({why}): list it in void_layers or route on another layer", path)
    base = _base_kind(outer, up, down)
    kind, coplanar, note = _requested(spec.get("line"), base, outer, rules.rf)
    if kind is None:
        return _error(layer, f"{layer}: {note}", f"{path}.line")
    refs = [s for s in (up, down) if s.ref]
    if not refs and kind != "cpw":
        return _error(layer, f"{layer}: no reference plane above or below: {E9_FIX}", path)
    if base in CLOSED:
        below, other, above = down, up, up.plies
    else:
        below, other = (up, down) if up.ref or (kind == "cpw" and k) else (down, up)
        above = other.plies if base == "embedded_microstrip" else ()
    passed = up.passed + down.passed
    geom = _geometry(stack, k, kind, below, above, passed, coplanar, spec)
    return LineSetup(layer, geom, kind, (up.ref,) if up.ref else (),
                     (down.ref,) if down.ref else (), tuple(s.net for s in refs),
                     up.voided + down.voided, tuple(name for name, _ in passed), coplanar,
                     rules.rf, _ref_issues(rules, refs, layer, path),
                     _warnings(layer, kind, below, other, coplanar, spec, note))


def _broadside(stack: Stack, k: int, spec: Mapping[str, Any], path: str) -> LineSetup:
    names = tuple(c.name for c in stack.copper)
    layer, pair = names[k], tuple(spec.get("layers") or ())
    idx = sorted(names.index(n) for n in pair if n in names)
    if len(pair) != 2 or len(idx) != 2 or idx[1] - idx[0] != 1 or layer not in pair:
        return _error(layer, f"{layer}: a broadside pair needs layers [La, Lb] on two adjacent "
                      f"copper layers including this one, got {list(pair)}", f"{path}.layers")
    hi_k, lo_k = idx
    up = walk(stack, hi_k, True, _rules(stack, spec, names[hi_k]))
    down = walk(stack, lo_k, False, _rules(stack, spec, names[lo_k]))
    if not (up.ref and down.ref) or up.blocked or down.blocked:
        return _error(layer, f"{layer}: a broadside pair needs a reference above {names[hi_k]} "
                      f"and below {names[lo_k]}: {E9_FIX}", path)
    face = stack.dielectrics[hi_k].plies
    rules = _rules(stack, spec, layer)
    geom = LineGeometry(kind="stripline", pair="broadside", width=float(spec.get("width") or 0.0),
                        t=stack.copper[hi_k].thickness, below=face + down.plies, above=up.plies,
                        pair_gap=round(sum(p.thickness for p in face), 9),
                        layers=(names[hi_k], names[lo_k]),
                        dielectric_copper=tuple((n, p.er) for n, p in up.passed + down.passed))
    return LineSetup(layer, geom, "stripline", (up.ref,), (down.ref,), (up.net, down.net),
                     up.voided + down.voided, tuple(n for n, _ in up.passed + down.passed),
                     False, rules.rf, _ref_issues(rules, (up, down), layer, path))


# ---------------------------------------------------------------- public
def resolve_line(stackup: Any, layer: str, spec: Mapping[str, Any] | None = None, *,
                 path: str = "$") -> LineSetup:
    """The line of net spec `spec` (net_specs keys) on copper `layer` (module docstring);
    issues carry `path` (the net's JSON path)."""
    spec = spec or {}
    stack = stack_view(stackup)
    names = tuple(c.name for c in stack.copper)
    if layer not in names:
        return _error(layer, f"no copper layer {layer!r} in the stackup ({', '.join(names)})",
                      f"{path}.layers")
    k = names.index(layer)
    role = stack.copper[k].role
    if role not in ROUTABLE:
        return _error(layer, f"{layer} has role {role}: impedance and RF lines run on signal or "
                      "mixed layers", f"{path}.layers")
    if spec.get("pair") == "broadside" and spec.get("diff") is not None:
        return _broadside(stack, k, spec, path)
    rules = _rules(stack, spec, layer)
    return _classify(stack, k, walk(stack, k, True, rules), walk(stack, k, False, rules), spec,
                     rules, path)


def line_geometry(stackup: Any, layer: str, spec: Mapping[str, Any] | None = None, *,
                  path: str = "$") -> tuple[LineGeometry | None, tuple[Issue, ...]]:
    """(LineGeometry, issues) of resolve_line; the geometry is None on an ST-E9/E10 error."""
    setup = resolve_line(stackup, layer, spec, path=path)
    return (setup.geom if setup.ok else None), setup.issues


def eligible_layers(stackup: Any, spec: Mapping[str, Any] | None = None) -> tuple[str, ...]:
    """The routable layers on which the net's line resolves without an error (top down)."""
    stack = stack_view(stackup)
    return tuple(c.name for c in stack.copper if c.role in ROUTABLE
                 and resolve_line(stackup, c.name, spec).ok)


def transition_issues(setups: Sequence[LineSetup], *, path: str = "$") -> tuple[Issue, ...]:
    """ST-W11 when a net's allowed layers rest on different reference nets (D7.6 step 5)."""
    nets = {s.layer: frozenset(s.ref_nets) for s in setups if s.ok}
    if len(set(nets.values())) < 2:
        return ()
    detail = "; ".join(f"{layer}: {'/'.join(sorted(n))}" for layer, n in nets.items())
    return (Issue("ST-W11", "warning", f"allowed layers rest on different reference nets "
                  f"({detail}): a layer change needs a return path - a via of the same reference "
                  "net or a decoupling capacitor between the reference nets within 2 mm (R27)",
                  path),)


# ---------------------------------------------------------------- copper rules
def width_window(width: float) -> tuple[float, float]:
    """The DRU width window: width +/- max(0.005 mm, 1.5 %) (ADR D7.6, rule R6)."""
    d = max(WINDOW_MIN_MM, width * WINDOW_PCT / 100.0)
    return round(width - d, 6), round(width + d, 6)


def default_ground_gap(min_clearance: float) -> float:
    """CPWG default gap: max(fab min clearance, 0.15 mm) (ADR D7.6)."""
    return max(min_clearance, CPWG_MIN_GAP_MM)


def pour_clearance(geom: LineGeometry, *, gap: tuple[float, float] = (0.1, 2.0),
                   grid: float = 0.005) -> tuple[float | None, str]:
    """(RF<->GND pour clearance, note) of a microstrip: the smallest grid gap in `gap` whose
    CPWG Z is >= 0.99 x the microstrip Z; (None, '') for other kinds."""
    if geom.kind != "microstrip":
        return None, ""
    single = replace(geom, pair="single", pair_gap=None, ground_gap=None)
    floor = POUR_Z_RATIO * _fallback(single, _closed_form(single)).z0

    def far(k: int) -> bool:
        g = replace(single, kind="cpwg", ground_gap=k * grid)
        try:
            return _fallback(g, _closed_form(g)).z0 >= floor
        except ValueError:
            return False

    lo, hi = math.ceil(gap[0] / grid - 1e-9), math.floor(gap[1] / grid + 1e-9)
    if not far(hi):
        return round(hi * grid, 6), f"a {hi * grid:g} mm pour gap still lowers Z by over 1 %"
    while lo < hi:                                # the smallest k with far(k): far is monotone
        lo, hi = (lo, (lo + hi) // 2) if far((lo + hi) // 2) else ((lo + hi) // 2 + 1, hi)
    return round(lo * grid, 6), ""
