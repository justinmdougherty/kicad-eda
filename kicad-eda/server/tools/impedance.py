#!/usr/bin/env python3
"""Controlled impedance: the ADR D7.6 facade over tline, plus the legacy Hammerstad helpers.

resolve_impedance(design) reads each net that asks for controlled impedance
(net_specs[net]["impedance"] = Z0, or ["diff"] = Zdiff) and sizes its copper on the
resolved stackup (stackup_model): tline.geometry finds the line on every allowed layer
(references, plies, mask, ST-E9/E10/W10/W11), tline.synth solves the width on the fab's
0.005 mm grid inside the fab's minimum track / clearance and applies the D7.6 accuracy
policy (class A final; B FD-verified; C FD-synthesised; without FD: closed_form /
fallback_unverified plus a G3 decision draft). Allowed layers: net_specs.layers, else the
primary outer layer for an RF net (freq_hz set), else every eligible layer. The primary
layer is the first declared layer, else F.Cu when allowed, else the first allowed one.

Every per-net record keeps today's keys (mode, z0 | zdiff, track_width, diff_pair_width,
diff_pair_gap, zdiff_achieved, z0_se, stackup {h, er} of the primary layer's substrate)
and adds kind, pair (single | edge | broadside), z_target, line, freq_hz, primary_layer,
layers {layer: line record}, eligible_layers, model, accuracy_class, fd, status,
width_window, z_achieved, issues, warnings and decision_drafts. A broadside pair's legacy
diff_pair_gap is the vertical face gap (warned). Records are plain JSON with no run times,
so two runs on the same inputs give the same bytes; FD cache entries ride in fd['cache']
(fd_cache()). A malformed net_specs value the facade reads (a non-numeric or non-positive
target, width, gap, ground_gap, freq_hz or z_tol_pct; a malformed layers, ref_nets,
ref_layers, void_layers or pair) is issue IMP-E2 naming the net and key; a line the closed
forms cannot size is IMP-E1. errors='raise' raises them, errors='record' returns them.
chain_pads {net: the narrowest pad (mm) on the net's chain}, supplied by a caller that knows
the footprints, applies the D7.6 CPWG rule: a width above 1.2 x that pad shrinks the default
ground_gap toward the fab minimum (a declared ground_gap is kept, with a warning).

fd: 'off' (default: stdlib only, deterministic, no numpy needed in KiCad's Python),
'auto' (fieldsolve2d when numpy/scipy are present and the budget allows) or 'force' (FD
also verifies class A lines). The legacy helpers below (microstrip_z0, microstrip_width,
coupled_zdiff, diff_pair: IPC-2141A / Hammerstad, no thickness or mask term) are kept
unchanged for callers that want the quick hand formulas:
  Eeff = (Er+1)/2 + (Er-1)/2 * (1 + 12 H/W)^-0.5
  W/H <= 1:  Z0 = 60/sqrt(Eeff) * ln(8 H/W + W/(4 H))
  W/H >= 1:  Z0 = 120*pi/sqrt(Eeff) / ( W/H + 1.393 + 0.667 ln(W/H + 1.444) )
  Zdiff = 2 Z0(W) * (1 - 0.48 exp(-0.96 S/H))
"""
from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

import fab_presets
import stackup_model
from stackup_roles import json_path
from tline import geometry, synth

# The legacy helpers' stackup: h is the outer dielectric (L1 -> L2). resolve_impedance
# uses the resolved stackup instead (the default is JLC04161H-7628 with the JLC mask).
DEFAULT_STACKUP = {"h": 0.20, "er": 4.20, "t": 0.035}  # mm, -, mm

FD_MODES = ("off", "auto", "force")
ERROR_MODES = ("raise", "record")
Z_TOL_PCT = 5.0                 # net_specs z_tol_pct default (ADR D7.7)
WIDTH_MAX_MM, GAP_MAX_MM = 5.0, 2.0
FAB_MIN_MM = 0.1                # min track / clearance when the fab publishes none
OUTER = ("F.Cu", "B.Cu")
LEGACY_KEYS = frozenset(DEFAULT_STACKUP)
SYNTH_ERROR = "IMP-E1"          # a line the closed forms cannot size (no ST-* id covers it)
SPEC_ERROR = "IMP-E2"           # a net_specs value the facade cannot read (type / range)
POSITIVE_KEYS = {"width": "mm", "gap": "mm", "ground_gap": "mm", "freq_hz": "Hz",
                 "z_tol_pct": "%"}
PAIRS = ("edge", "broadside")
CHAIN_PAD_FACTOR = 1.2          # ADR D7.6: a CPWG wider than 1.2 x the narrowest chain pad


# ---------------------------------------------------------------- legacy helpers
def microstrip_z0(w: float, h: float, er: float) -> float:
    """Characteristic impedance (ohm) of a microstrip of width `w` over dielectric
    height `h` (both mm) with relative permittivity `er`."""
    if w <= 0 or h <= 0:
        return float("inf")
    u = w / h
    eeff = (er + 1) / 2 + (er - 1) / 2 * (1 + 12 / u) ** -0.5
    if u <= 1:
        return 60 / math.sqrt(eeff) * math.log(8 / u + u / 4)
    return 120 * math.pi / math.sqrt(eeff) / (u + 1.393 + 0.667 * math.log(u + 1.444))


def microstrip_width(z0: float, h: float, er: float,
                     w_lo: float = 0.05, w_hi: float = 5.0) -> float:
    """Solve for the microstrip width (mm) giving impedance `z0`. Bisection —
    Z0 decreases monotonically with width, so the search is well-behaved."""
    for _ in range(80):
        w = (w_lo + w_hi) / 2
        z = microstrip_z0(w, h, er)
        if z > z0:          # too narrow -> widen
            w_lo = w
        else:
            w_hi = w
    return round((w_lo + w_hi) / 2, 4)


def coupled_zdiff(w: float, s: float, h: float, er: float) -> float:
    """Differential impedance of an edge-coupled microstrip pair (width w, gap s)
    via the standard coupling approximation Zdiff = 2 Z0(w)(1 - 0.48 e^(-0.96 s/h))."""
    return 2 * microstrip_z0(w, h, er) * (1 - 0.48 * math.exp(-0.96 * s / h))


def diff_pair(zdiff: float, h: float, er: float, gap: float | None = None) -> dict:
    """Size an edge-coupled differential pair for target `zdiff` (ohm) with the
    industry default S = W (overridable). Returns {width, gap, zdiff_target,
    zdiff_achieved, z0_se}."""
    w_lo, w_hi = 0.05, 5.0
    for _ in range(80):
        w = (w_lo + w_hi) / 2
        s = gap if gap is not None else w
        zd = coupled_zdiff(w, s, h, er)
        if zd > zdiff:       # impedance too high -> widen trace
            w_lo = w
        else:
            w_hi = w
    w = round((w_lo + w_hi) / 2, 4)
    s = round(gap if gap is not None else w, 4)
    return {"width": w, "gap": s, "zdiff_target": zdiff,
            "zdiff_achieved": round(coupled_zdiff(w, s, h, er), 1),
            "z0_se": round(microstrip_z0(w, h, er), 2)}


# ---------------------------------------------------------------- requests
@dataclass(frozen=True)
class _Request:
    """One net's impedance intent: mode single | diff and the line spec tline reads."""

    net: str
    mode: str
    target: float | None                # None: malformed (see problems)
    spec: Mapping[str, Any]
    path: str
    problems: tuple[geometry.Issue, ...] = ()

    @property
    def target_kind(self) -> str:
        return "z0" if self.mode == "single" else "zdiff"

    @property
    def freq_hz(self) -> float | None:
        return geometry.rf_freq(self.spec)


@dataclass(frozen=True)
class _Ctx:
    """What every net shares: the stackup, the fab bounds and the FD tier."""

    rs: Any
    bounds: synth.Bounds
    tier: Any
    mode: str
    budget: Any
    chain_pads: Mapping[str, float]


def _impedance_value(value: Any) -> float | None:
    """A target in ohm (a number or a numeric string, as before), else None."""
    try:
        z = float(value)
    except (TypeError, ValueError):
        return None
    return z if math.isfinite(z) and z > 0 else None


def _positive(value: Any) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value > 0)


def _names(value: Any, empty: bool = False) -> bool:
    return (isinstance(value, (list, tuple)) and (empty or bool(value))
            and all(isinstance(x, str) for x in value))


def _layer_lists(value: Any) -> bool:
    return isinstance(value, Mapping) and all(
        isinstance(k, str) and _names(v, empty=True) for k, v in value.items())


def _spec_issues(net: str, spec: Mapping[str, Any], key: str) -> tuple[geometry.Issue, ...]:
    """IMP-E2 for every malformed value the facade reads (the schema lint rejects them too)."""
    bad = [] if _impedance_value(spec[key]) is not None else [(key, "an impedance > 0 ohm")]
    bad += [(k, f"a number > 0 {unit}") for k, unit in POSITIVE_KEYS.items()
            if spec.get(k) is not None and not _positive(spec[k])]
    checks = (("layers", _names, "a non-empty list of copper layer names"),
              ("ref_nets", _names, "a non-empty list of net names"),
              ("ref_layers", _layer_lists, "a {layer: [copper layer names]} map"),
              ("void_layers", _layer_lists, "a {layer: [copper layer names]} map"),
              ("pair", PAIRS.__contains__, f"one of {', '.join(PAIRS)}"))
    bad += [(k, what) for k, ok, what in checks if spec.get(k) is not None and not ok(spec[k])]
    return tuple(geometry.Issue(SPEC_ERROR, "error",
                                f"net_specs.{net}.{k} must be {what}, got {spec[k]!r}",
                                json_path("net_specs", net, k)) for k, what in bad)


def _requests(design: Mapping[str, Any]) -> list[_Request]:
    """The nets asking for controlled impedance ('impedance' wins over 'diff', as before),
    each with its IMP-E2 problems (a malformed request keeps target None if that is bad)."""
    specs = design.get("net_specs") or {}
    out = []
    for net, spec in specs.items():
        if not isinstance(spec, Mapping) or not ("impedance" in spec or "diff" in spec):
            continue
        mode = "single" if "impedance" in spec else "diff"
        key = "impedance" if mode == "single" else "diff"
        line_spec = {k: v for k, v in spec.items() if mode == "diff" or k != "diff"}
        out.append(_Request(net, mode, _impedance_value(spec[key]), line_spec,
                            json_path("net_specs", net), _spec_issues(net, line_spec, key)))
    return out


def _with_legacy(design: Mapping[str, Any], stackup: Mapping[str, Any] | None
                 ) -> Mapping[str, Any]:
    """The design with the legacy `stackup` argument applied (it beats the design's)."""
    if stackup is None:
        return design
    base = design.get("stackup")
    merge = (isinstance(base, Mapping) and set(base) <= LEGACY_KEYS
             and set(stackup) <= LEGACY_KEYS)
    return {**design, "stackup": {**base, **stackup} if merge else dict(stackup)}


# ---------------------------------------------------------------- fab bounds
def _count_in(key: str, count: int) -> bool:
    """Does a fab capability key ('1-2', 'multi', '2', '4-6') cover `count` layers?"""
    if key == "multi":
        return count > 2
    lo, _, hi = key.partition("-")
    if not lo.isdigit() or (hi and not hi.isdigit()):
        return False
    return int(lo) <= count <= int(hi or lo)


def fab_minimum(fabs: Any, fab: str | None, copper_layers: int, key: str) -> float:
    """The fab's min track / clearance (key min_track_mm | min_clearance_mm) for a board of
    `copper_layers`; per-count tables pick their row, unknown rows the largest (safe) value."""
    fab_data = fabs.fabs.get(fab) if fab else None
    value = ((fab_data or {}).get("capabilities") or {}).get(key)
    if isinstance(value, (int, float)) and value > 0:
        return float(value)
    if not isinstance(value, Mapping) or not value:
        return FAB_MIN_MM
    rows = [float(v) for k, v in value.items() if _count_in(str(k), copper_layers)]
    return max(rows) if rows else max(float(v) for v in value.values())


def _bounds(rs: Any, fabs: Any) -> synth.Bounds:
    count = max(rs.copper_layers, 1)
    track = fab_minimum(fabs, rs.fab, count, "min_track_mm")
    clear = fab_minimum(fabs, rs.fab, count, "min_clearance_mm")
    return synth.Bounds(width=(track, WIDTH_MAX_MM), gap=(clear, GAP_MAX_MM))


def _fd_tier(fd: str, budget: Any) -> tuple[Any, Any]:
    """(fieldsolve2d or None, budget); imported lazily so KiCad's Python never needs numpy."""
    if fd == "off":
        return None, budget
    import fieldsolve2d
    return fieldsolve2d, budget if budget is not None else fieldsolve2d.FdBudget.from_env()


# ---------------------------------------------------------------- layers
def _allowed(req: _Request, eligible: Sequence[str]) -> tuple[str, ...]:
    declared = req.spec.get("layers")
    if declared:
        return tuple(declared)
    if req.freq_hz is not None:
        return tuple(layer for layer in OUTER if layer in eligible)[:1]
    return tuple(eligible)


def _primary(req: _Request, allowed: Sequence[str]) -> str | None:
    if req.spec.get("layers") or "F.Cu" not in allowed:
        return allowed[0] if allowed else None
    return "F.Cu"


def _no_layer_issues(req: _Request, rs: Any) -> list[geometry.Issue]:
    """Why no layer is eligible: every routable layer's own errors."""
    found = [i for layer in rs.routable_layers()
             for i in geometry.resolve_line(rs, layer, req.spec, path=req.path).issues
             if i.severity == "error"]
    return found or [geometry.Issue("ST-E9", "error", "no routable copper layer for the line",
                                    req.path)]


# ---------------------------------------------------------------- one net
def _synthesize(setup: geometry.LineSetup, req: _Request, ctx: _Ctx,
                cache: Mapping[str, Any]) -> synth.Synthesis:
    spec, pad = req.spec, ctx.chain_pads.get(req.net)
    tol, declared_gap = spec.get("z_tol_pct"), spec.get("ground_gap") is not None
    label = f"{req.net} on {setup.layer}"
    s = synth.synthesize(
        setup.geom, req.target, target_kind=req.target_kind, bounds=ctx.bounds,
        freq_hz=req.freq_hz or 0.0, fd=ctx.tier, budget=ctx.budget, cache=cache,
        fd_mode=ctx.mode, tie_gap=req.mode == "diff" and spec.get("gap") is None,
        max_width=None if pad is None or declared_gap else CHAIN_PAD_FACTOR * pad,
        fixed=spec.get("width") is not None,
        z_tol_pct=float(tol) if isinstance(tol, (int, float)) else Z_TOL_PCT, label=label)
    if (pad is None or not declared_gap or s.geom.ground_gap is None     # coplanar lines only
            or s.geom.width <= CHAIN_PAD_FACTOR * pad + 1e-9):
        return s
    return replace(s, warnings=(*s.warnings, (
        f"{label}: width {s.geom.width:g} mm exceeds {CHAIN_PAD_FACTOR:g} x the narrowest chain "
        f"pad ({pad:g} mm); the declared net_specs.{req.net}.ground_gap {s.geom.ground_gap:g} mm "
        "is kept (without it the gap shrinks toward the fab minimum, ADR D7.6)")))


def _layer_record(setup: geometry.LineSetup, s: synth.Synthesis) -> dict[str, Any]:
    rec = s.record()
    return {**rec, "line": setup.line, "ref": setup.ref_dict(),
            "void_layers": list(setup.void_layers),
            "dielectric_layers": list(setup.dielectric_layers),
            "warnings": [*setup.warnings, *rec["warnings"]]}


def _pair_warnings(req: _Request, geom: Any) -> list[str]:
    """A broadside pair's legacy diff_pair_gap is its vertical face gap, not a KiCad gap."""
    if geom.pair != "broadside":
        return []
    return [f"{req.net}: broadside pair on {'/'.join(geom.layers)}: diff_pair_gap "
            f"{geom.pair_gap:g} mm is the vertical face gap between the layers, not a "
            "same-layer spacing (KiCad's diff-pair gap does not apply)"]


def _substrate(geom: Any) -> dict[str, float]:
    """The legacy {h, er}: the plies under the line, Er as their series (harmonic) mean."""
    h = sum(p.thickness for p in geom.below)
    er = h / sum(p.thickness / p.er for p in geom.below) if h > 0 else 0.0
    return {"h": round(h, 6), "er": round(er, 4)}


def _z0_se(req: _Request, s: synth.Synthesis) -> tuple[float | None, list[str]]:
    """(the legacy z0_se: one line of the pair alone, warnings); None is always explained."""
    try:
        single = replace(s.geom, pair="single", pair_gap=None)
        return round(synth.evaluate(single, req.freq_hz or 0.0).z0, 2), []
    except (ValueError, ZeroDivisionError, OverflowError) as exc:
        return None, [f"{req.net}: z0_se (the single-ended Z0 of one line of the pair) is not "
                      f"available, the closed forms cannot evaluate it: {exc}"]


def _legacy(req: _Request, s: synth.Synthesis) -> tuple[dict[str, Any], list[str]]:
    """(today's per-net keys, warnings about them)."""
    width = s.geom.width
    base = {"mode": req.mode, "track_width": width, "stackup": _substrate(s.geom)}
    if req.mode == "single":
        return {**base, "z0": req.target}, []
    z0_se, warnings = _z0_se(req, s)
    return {**base, "zdiff": req.target, "zdiff_achieved": round(s.z_achieved, 1),
            "diff_pair_width": width, "diff_pair_gap": s.geom.pair_gap, "z0_se": z0_se}, warnings


def _issue_dicts(issues: Sequence[Any]) -> list[dict[str, str]]:
    seen, out = set(), []
    for i in issues:
        d = {"id": i.id, "severity": i.severity, "message": i.message, "path": i.path}
        key = tuple(d.values())
        if key not in seen:
            seen.add(key)
            out.append(d)
    return out


def _drafts(req: _Request, synths: Mapping[str, synth.Synthesis]) -> list[dict[str, Any]]:
    override = f"net_specs.{req.net}.width"
    return [{**s.decision_draft, "override": override} for s in synths.values()
            if s.decision_draft is not None]


def _record(req: _Request, eligible: Sequence[str], primary: str,
            setups: Mapping[str, geometry.LineSetup], synths: Mapping[str, synth.Synthesis]
            ) -> dict[str, Any]:
    p, layers = synths[primary], {k: _layer_record(setups[k], synths[k]) for k in synths}
    issues = [i for st in setups.values() for i in st.issues]
    issues += geometry.transition_issues(list(setups.values()), path=req.path)
    legacy, legacy_warnings = _legacy(req, p)
    return {**legacy, "kind": req.mode, "pair": p.geom.pair, "z_target": req.target,
            "line": setups[primary].line, "freq_hz": req.freq_hz, "primary_layer": primary,
            "layers": layers, "eligible_layers": list(eligible), "model": p.model,
            "accuracy_class": p.accuracy_class, "fd": layers[primary]["fd"],
            "status": p.status, "width_window": list(p.width_window),
            "z_achieved": round(p.z_achieved, 3), "issues": _issue_dicts(issues),
            "warnings": [*(w for k in synths for w in layers[k]["warnings"]),
                         *_pair_warnings(req, p.geom), *legacy_warnings],
            "decision_drafts": _drafts(req, synths)}


def _error_record(req: _Request, issues: Sequence[Any], eligible: Sequence[str] = ()
                  ) -> dict[str, Any]:
    target_key = "z0" if req.mode == "single" else "zdiff"
    pair = {} if req.mode == "single" else {"zdiff_achieved": None, "diff_pair_width": None,
                                             "diff_pair_gap": None, "z0_se": None}
    return {"mode": req.mode, target_key: req.target, "track_width": None, "stackup": None,
            **pair, "kind": req.mode, "pair": None, "z_target": req.target, "line": None,
            "freq_hz": req.freq_hz, "primary_layer": None, "layers": {},
            "eligible_layers": list(eligible), "model": None, "accuracy_class": None,
            "fd": None, "status": "error", "width_window": None, "z_achieved": None,
            "issues": _issue_dicts(issues), "warnings": [], "decision_drafts": []}


def _resolve_net(req: _Request, ctx: _Ctx, cache: Mapping[str, Any]
                 ) -> tuple[dict[str, Any], dict[str, Any]]:
    """(net record, cache grown by this net's new FD entries)."""
    if req.problems:
        return _error_record(req, req.problems), dict(cache)
    eligible = geometry.eligible_layers(ctx.rs, req.spec)
    allowed = _allowed(req, eligible)
    primary = _primary(req, allowed)
    if primary is None:
        return _error_record(req, _no_layer_issues(req, ctx.rs), eligible), dict(cache)
    setups = {layer: geometry.resolve_line(ctx.rs, layer, req.spec, path=req.path)
              for layer in allowed}
    bad = [i for st in setups.values() if not st.ok for i in st.issues if i.severity == "error"]
    if bad:
        return _error_record(req, bad, eligible), dict(cache)
    synths, grown = {}, dict(cache)
    for layer, setup in setups.items():
        try:
            synths[layer] = _synthesize(setup, req, ctx, grown)
        except ValueError as exc:
            issue = geometry.Issue(SYNTH_ERROR, "error", f"{layer}: {exc}", req.path)
            return _error_record(req, [issue], eligible), grown
        grown = {**grown, **synths[layer].cache}
    return _record(req, eligible, primary, setups, synths), grown


# ---------------------------------------------------------------- facade
def _check_options(fd: str, errors: str, chain_pads: Any) -> None:
    """Caller arguments (not design data): a bad one raises in either errors mode."""
    if fd not in FD_MODES:
        raise ValueError(f"fd must be one of {FD_MODES}, got {fd!r}")
    if errors not in ERROR_MODES:
        raise ValueError(f"errors must be one of {ERROR_MODES}, got {errors!r}")
    if chain_pads is not None and not (isinstance(chain_pads, Mapping) and all(
            isinstance(k, str) and _positive(v) for k, v in chain_pads.items())):
        raise ValueError("chain_pads must map net names to the narrowest chain pad width "
                         f"(a number > 0 mm), got {chain_pads!r}")


def _raise_errors(out: Mapping[str, Mapping[str, Any]]) -> None:
    failed = [f"{net}: " + "; ".join(f"{i['id']} {i['message']}" for i in rec["issues"]
                                     if i["severity"] == "error")
              for net, rec in out.items() if rec["status"] == "error"]
    if failed:
        raise ValueError("controlled impedance cannot be resolved: " + " | ".join(failed))


def resolve_impedance(design: Mapping[str, Any], stackup: Mapping[str, Any] | None = None, *,
                      fd: str = "off", budget: Any = None,
                      cache: Mapping[str, Mapping[str, Any]] | None = None,
                      errors: str = "raise", materials: Any = None,
                      fabs: Any = None, chain_pads: Mapping[str, float] | None = None
                      ) -> dict[str, dict[str, Any]]:
    """{net: record} for every net asking for controlled impedance (module docstring).

    `stackup` (the legacy {h, er, t} shorthand) overrides the design's; `budget` is a
    fieldsolve2d.FdBudget (default from KICAD_EDA_FD_BUDGET_S), shared by every net; `cache`
    holds FD entries by cross-section hash (fd_cache() of an earlier result); `chain_pads`
    {net: narrowest chain pad mm} turns on the CPWG pad rule. errors='raise' raises
    ValueError (naming the net and issue id) for a malformed spec value, a net or a stackup
    that cannot be resolved; 'record' returns each such net with status 'error' and its
    issues and resolves the rest. A bad option raises either way. Never mutates `design`."""
    _check_options(fd, errors, chain_pads)
    requests = _requests(design)
    if not requests:
        return {}
    if errors == "raise":               # malformed values fail before any stackup or FD work
        _raise_errors({r.net: _error_record(r, r.problems) for r in requests if r.problems})
    fabset = fabs if fabs is not None else fab_presets.load_fabs()
    rs, st_issues = stackup_model.resolve(_with_legacy(design, stackup), materials=materials,
                                          fabs=fabset)
    if rs is None:
        bad = [i for i in st_issues if i.severity == "error"]
        out = {r.net: _error_record(r, r.problems or bad) for r in requests}
    else:
        tier, budget = _fd_tier(fd, budget)
        ctx = _Ctx(rs, _bounds(rs, fabset), tier, fd, budget, dict(chain_pads or {}))
        out, grown = {}, dict(cache or {})
        for req in requests:
            out[req.net], grown = _resolve_net(req, ctx, grown)
    if errors == "raise":
        _raise_errors(out)
    return out


def fd_cache(resolved: Mapping[str, Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """Every FD cache entry a resolve_impedance result used, by cross-section hash (feed it
    back as `cache` to skip those solves)."""
    out: dict[str, dict[str, Any]] = {}
    for rec in resolved.values():
        for line in (rec.get("layers") or {}).values():
            fd = line.get("fd") or {}
            out.update({k: dict(v) for k, v in (fd.get("cache") or {}).items()})
    return out


# ---------------------------------------------------------------- CLI
def _legacy_table() -> None:
    st = DEFAULT_STACKUP
    print(f"Stackup: h={st['h']}mm Er={st['er']} (outer microstrip to L2)")
    for z in (50, 75, 90):
        print(f"  {z} ohm single-ended -> W = {microstrip_width(z, st['h'], st['er'])} mm")
    for zd in (90, 100):
        dp = diff_pair(zd, st["h"], st["er"])
        print(f"  {zd} ohm diff -> W={dp['width']} mm  gap={dp['gap']} mm  "
              f"(achieved {dp['zdiff_achieved']} ohm, Z0_se={dp['z0_se']})")


def main(argv: Sequence[str] | None = None) -> int:
    """impedance.py [netlist.json] [--fd off|auto|force]: the facade as JSON, or (no netlist)
    the legacy hand-formula table."""
    import argparse
    import json
    parser = argparse.ArgumentParser(description=main.__doc__)
    parser.add_argument("netlist", nargs="?")
    parser.add_argument("--fd", choices=FD_MODES, default="off")
    args = parser.parse_args(argv)
    if args.netlist is None:
        _legacy_table()
        return 0
    with open(args.netlist, encoding="utf-8") as fh:
        design = json.load(fh)
    print(json.dumps(resolve_impedance(design, fd=args.fd), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
