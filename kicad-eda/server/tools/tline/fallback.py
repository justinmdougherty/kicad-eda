"""Class C fallback models (ADR D7.6 'Class C fallback models', critique M11; task D7-05c).

Corrects a class C closed-form result (the FD seed; without FD the answer) per cause. Forms
only: coefficients, calibrated domains and every est_error_pct come from
server/data/tline_fallback.json, written by tools/tline_calibrate.py from FD runs.
- offset_stripline (offset / dual stripline, single or edge): per mode Z_image / (1 + e),
  e = sum_f c_f(a) x_f, c_f piecewise-linear in a = h_near / (h_near + h_far) (the centred
  knot fitted too: the mapped-Cohn pair has its own error); eeff kept.
- masked_coupled_microstrip (masked edge pair, no covering plies): per mode the bare KJ84 pair
  x r^k e^d, r = Z_coated / Z_bare of the single-ended Wan-Hoorfar line, k / d = sum of
  coefficient x feature; eeff_odd scales as (Zodd_bare / Zodd)^2 (air capacitance kept).
- multi_ply_er_contrast, cpw_finite_ground, nearest_closed_form: identity; est = max(tabled,
  the closed form's own).
Features: 'one' or '*'-joined factors, each a domain coordinate or 'ln_' + one. Coordinates
are clamped to the calibrated domain (the box tline_calibrate samples uniformly); outside it,
or with coplanar grounds / etch (never calibrated), est is never below the closed form's and a
warning says why. Results stay class C, model '<model>+fallback:<cause>'. Stdlib only;
registers no model (tline/__init__ lists the module).
"""
from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, replace
from functools import lru_cache
from itertools import pairwise
from pathlib import Path
from types import MappingProxyType
from typing import Any

from .microstrip import CONTRAST_LIMIT, evaluate_microstrip, substrate
from .stripline import ASYM_EPS, pair_line_result, sides
from .types import LineGeometry, LineResult, Verdict, single_line_result

SCHEMA = "kicad-eda/tline-fallback@2"
DATA_PATH = Path(__file__).resolve().parents[2] / "data" / "tline_fallback.json"
OFFSET, MASKED = "offset_stripline", "masked_coupled_microstrip"
MULTI_PLY, CPW_FINITE, NEAREST = "multi_ply_er_contrast", "cpw_finite_ground", "nearest_closed_form"
CAUSES = (OFFSET, MASKED, MULTI_PLY, CPW_FINITE, NEAREST)
IDENTITY_CAUSES = (MULTI_PLY, CPW_FINITE, NEAREST)
OFFSET_KINDS = ("stripline", "dual_stripline")
OFFSET_MODES = ("single", "even", "odd")
PAIR_COORDS = ("s_over_b", "t_over_s")
OFFSET_COORDS = ("a", "w_over_b", "t_over_b", "t_over_w", "er_near_over_far", "side_contrast",
                 *PAIR_COORDS)
MASK_COORDS = ("w_over_h", "s_over_w", "t_over_h", "t_over_w", "t_over_s", "er", "contrast",
               "mask_er", "mask_c1_over_h", "mask_c2_over_c1", "mask_c3_over_c1", "s_over_h",
               "mask_c2_over_h", "mask_c3_over_s", "mask_gap_fill")
LABELS = {"a": "asymmetry a", "t_over_w": "t/W", "t_over_s": "t/S"}  # else 'mask c1/h' style
OUTSIDE = "outside the FD-calibrated range"


@dataclass(frozen=True)
class OffsetFit:
    knots: tuple[float, ...]
    terms: Mapping[str, Mapping[str, tuple[float, ...]]]   # mode -> feature -> per knot
    est: Mapping[str, float]                               # 'single' (z0) | 'pair' (z_diff)
    even_est: float
    domain: Mapping[str, tuple[float, float]]


@dataclass(frozen=True)
class MaskFit:
    terms: Mapping[str, Mapping[str, float]]               # k_even/k_odd/d_even/d_odd
    est: float
    even_est: float
    domain: Mapping[str, tuple[float, float]]


@dataclass(frozen=True)
class FallbackTable:
    """The parsed data file; `digest` is the sha256 of its canonical JSON."""
    offset: OffsetFit
    masked: MaskFit
    identity_est: Mapping[str, float]
    digest: str
    source: str


def _number(value: Any, where: str, *, positive: bool = False) -> float:
    ok = isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    if not ok or (positive and not value > 0):
        raise ValueError(f"{where}: expected a finite {'positive ' if positive else ''}number, "
                         f"got {value!r}")
    return float(value)


def _numbers(values: Any, where: str, count: int | None = None) -> tuple[float, ...]:
    if not isinstance(values, list) or (count is not None and len(values) != count):
        raise ValueError(f"{where}: expected a list of {count or 'some'} numbers, got {values!r}")
    return tuple(_number(v, f"{where}[{i}]") for i, v in enumerate(values))


def _object(value: Any, where: str, allowed: tuple[str, ...] | None = None) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{where}: expected an object, got {value!r}")
    unknown = sorted(set(value) - set(allowed)) if allowed is not None else []
    if unknown:
        raise ValueError(f"{where}: unknown names {unknown} (allowed: {list(allowed)})")
    return value


def _domain(value: Any, where: str, coords: tuple[str, ...]) -> Mapping[str, tuple[float, float]]:
    given, out = _object(value, f"{where}.domain", coords), {}
    for name in coords:
        lo, hi = _numbers(given.get(name), f"{where}.domain.{name}", 2)
        if lo > hi:
            raise ValueError(f"{where}.domain.{name}: lower bound {lo} above upper bound {hi}")
        out[name] = (lo, hi)
    return MappingProxyType(out)


def _check_feature(name: str, coords: tuple[str, ...], domain: Mapping[str, tuple[float, float]],
                   where: str) -> str:
    """A feature name: 'one' or '*'-joined factors '<coord>' / 'ln_<coord>' (ln only of a
    coordinate whose calibrated range is positive)."""
    for factor in ([] if name == "one" else name.split("*")):
        log, coord = factor.startswith("ln_"), factor.removeprefix("ln_")
        if coord not in coords or (log and not domain[coord][0] > 0):
            raise ValueError(f"{where}: feature {name!r}: {factor!r} is neither a coordinate "
                             f"of this cause {list(coords)} nor ln of a positive one")
    return name


def _parse_offset(doc: Mapping[str, Any], where: str) -> OffsetFit:
    knots = _numbers(doc.get("knots"), f"{where}.knots")
    if len(knots) < 2 or any(b <= a for a, b in pairwise(knots)):
        raise ValueError(f"{where}.knots: expected at least two increasing values, got {knots}")
    domain = _domain(doc.get("domain"), where, OFFSET_COORDS)
    terms = _object(doc.get("terms"), f"{where}.terms", OFFSET_MODES)
    parsed = {}
    for mode in OFFSET_MODES:
        coords = tuple(c for c in OFFSET_COORDS if mode != "single" or c not in PAIR_COORDS)
        given = _object(terms.get(mode), f"{where}.terms.{mode}")
        parsed[mode] = MappingProxyType({
            _check_feature(f, coords, domain, f"{where}.terms.{mode}"):
                _numbers(v, f"{where}.terms.{mode}.{f}", len(knots)) for f, v in given.items()})
    est = _object(doc.get("est_error_pct"), f"{where}.est_error_pct", ("single", "pair"))
    return OffsetFit(knots, MappingProxyType(parsed), MappingProxyType(
        {k: _number(est.get(k), f"{where}.est_error_pct.{k}", positive=True)
         for k in ("single", "pair")}),
        _number(doc.get("z_even_error_pct"), f"{where}.z_even_error_pct", positive=True), domain)


def _parse_masked(doc: Mapping[str, Any], where: str) -> MaskFit:
    domain = _domain(doc.get("domain"), where, MASK_COORDS)
    terms = {}
    for key in ("k_even", "k_odd", "d_even", "d_odd"):
        given = _object(doc.get(key), f"{where}.{key}")
        terms[key] = MappingProxyType({
            _check_feature(f, MASK_COORDS, domain, f"{where}.{key}"):
                _number(v, f"{where}.{key}.{f}") for f, v in given.items()})
    est, even = (_number(doc.get(k), f"{where}.{k}", positive=True)
                 for k in ("est_error_pct", "z_even_error_pct"))
    return MaskFit(MappingProxyType(terms), est, even, domain)


def parse_table(doc: Mapping[str, Any], *, source: str = "<dict>") -> FallbackTable:
    """Validate a tline_fallback document (ValueError naming the bad entry)."""
    if doc.get("schema") != SCHEMA:
        raise ValueError(f"{source}: schema must be {SCHEMA!r}, got {doc.get('schema')!r}")
    causes = doc.get("causes")
    if not isinstance(causes, dict) or set(causes) != set(CAUSES):
        raise ValueError(f"{source}: causes must be exactly {list(CAUSES)}, "
                         f"got {sorted(causes) if isinstance(causes, dict) else causes!r}")
    entries = {c: _object(causes[c], f"{source}: {c}") for c in CAUSES}
    identity = {c: _number(entries[c].get("est_error_pct"), f"{source}: {c}.est_error_pct",
                           positive=True) for c in IDENTITY_CAUSES}
    text = json.dumps(doc, sort_keys=True, separators=(",", ":"))
    return FallbackTable(_parse_offset(entries[OFFSET], f"{source}: {OFFSET}"),
                         _parse_masked(entries[MASKED], f"{source}: {MASKED}"),
                         MappingProxyType(identity),
                         hashlib.sha256(text.encode("utf-8")).hexdigest(), source)


def load_table(path: Path = DATA_PATH) -> FallbackTable:
    """Read and validate a tline_fallback.json file."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"{path}: no fallback data file; regenerate it with "
                                "server/tools/tline_calibrate.py") from exc
    return parse_table(json.loads(text), source=str(path))


@lru_cache(maxsize=1)
def default_table() -> FallbackTable:
    """The committed data file, loaded once on first use."""
    return load_table(DATA_PATH)


def _identity_causes(geom: LineGeometry) -> tuple[str, ...]:
    closed = geom.kind in OFFSET_KINDS or geom.pair == "broadside"
    ers = [p.er for p in (geom.below + geom.above if closed else geom.below)]
    found = ([MULTI_PLY] if ers and (max(ers) - min(ers)) / min(ers) > CONTRAST_LIMIT else []) + (
        [CPW_FINITE] if geom.kind == "cpw" and geom.ground_width is not None else [])
    return tuple(found) or (NEAREST,)


def cause_of(geom: LineGeometry) -> str:
    """The fallback cause a class C result of `geom` is corrected for (identity causes that
    apply together are joined by ',')."""
    if geom.kind in OFFSET_KINDS and geom.pair in ("single", "edge") and geom.below and geom.above:
        s = sides(geom)
        if not math.isclose(s.h_below, s.h_above, rel_tol=ASYM_EPS):
            return OFFSET
    m = geom.mask
    coated = m is not None and max(m.over_trace, m.over_substrate, m.between) > 0
    if geom.kind == "microstrip" and geom.pair == "edge" and coated and not geom.above:
        return MASKED
    return ",".join(_identity_causes(geom))


def _offset_coords(geom: LineGeometry) -> dict[str, float]:
    s = sides(geom)
    near, far = sorted(((s.h_above, s.er_above), (s.h_below, s.er_below)))     # by height
    b = s.h_below + s.h_above + geom.t
    out = {"a": near[0] / (near[0] + far[0]), "w_over_b": geom.width / b, "t_over_b": geom.t / b,
           "t_over_w": geom.t / geom.width, "er_near_over_far": near[1] / far[1],
           "side_contrast": max(substrate(geom.below).contrast, substrate(geom.above).contrast)}
    if geom.pair == "edge":
        out.update(s_over_b=geom.pair_gap / b, t_over_s=geom.t / geom.pair_gap)
    return out


def _mask_coords(geom: LineGeometry) -> dict[str, float]:
    sub, m, w, s, t = substrate(geom.below), geom.mask, geom.width, geom.pair_gap, geom.t
    c1 = m.over_substrate or math.inf          # no coating over the substrate: outside
    return {"w_over_h": w / sub.h, "s_over_w": s / w, "t_over_h": t / sub.h, "t_over_w": t / w,
            "t_over_s": t / s, "er": sub.er, "contrast": sub.contrast, "mask_er": m.er,
            "mask_c1_over_h": m.over_substrate / sub.h,
            "mask_c2_over_c1": m.over_trace / c1, "mask_c3_over_c1": m.between / c1,
            "s_over_h": s / sub.h,
            "mask_c2_over_h": m.over_trace / sub.h, "mask_c3_over_s": m.between / s,
            "mask_gap_fill": (m.between + t) / s}


def domain_values(geom: LineGeometry) -> dict[str, float]:
    """The calibrated-domain coordinates of a corrected cause ({} for identity causes)."""
    cause = cause_of(geom)
    if cause == OFFSET:
        return _offset_coords(geom)
    return _mask_coords(geom) if cause == MASKED else {}


def feature_value(name: str, coords: Mapping[str, float]) -> float:
    """A named feature: 'one', or the product of its '*'-joined factors (coordinate or ln_)."""
    out = 1.0
    for factor in ([] if name == "one" else name.split("*")):
        value = coords[factor.removeprefix("ln_")]
        out *= math.log(value) if factor.startswith("ln_") else value
    return out


def _clamped(domain: Mapping[str, tuple[float, float]], values: Mapping[str, float]
             ) -> dict[str, float]:
    """Coordinates moved to the nearest calibrated value (no extrapolation of the forms)."""
    return {k: min(max(v, domain[k][0]), domain[k][1]) if k in domain else v
            for k, v in values.items()}


def mask_ratio(geom: LineGeometry) -> float:
    """Z_coated / Z_bare of the single-ended Wan-Hoorfar microstrip of the pair's width."""
    single = LineGeometry(kind="microstrip", pair="single", width=geom.width, t=geom.t,
                          below=geom.below, above=())
    return (evaluate_microstrip(replace(single, mask=geom.mask), 0).z0
            / evaluate_microstrip(single, 0).z0)


def _interp(knots: tuple[float, ...], values: tuple[float, ...], x: float) -> float:
    """Piecewise-linear in x through (knots, values), constant beyond the end knots."""
    x = min(max(x, knots[0]), knots[-1])
    for (x0, y0), (x1, y1) in pairwise(zip(knots, values, strict=True)):
        if x <= x1:
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return values[-1]


def _notes(cause: str, domain: Mapping[str, tuple[float, float]], geom: LineGeometry
           ) -> list[str]:
    """One note per coordinate outside the calibrated domain and per uncalibrated input."""
    out = []
    for name, value in domain_values(geom).items():
        lo, hi = domain.get(name, (value, value))
        if not lo <= value <= hi:
            label = LABELS.get(name, name.replace("_over_", "/").replace("_", " "))
            out.append(f"fallback {cause}: {label} {value:.4g} is {OUTSIDE} "
                       f"[{lo:.4g}, {hi:.4g}]: corrected at the nearest calibrated value")
    extra = ((f"coplanar grounds (ground_gap {geom.ground_gap} mm)", geom.ground_gap is not None),
             (f"etch {geom.etch} mm", geom.etch > 0))
    return out + [f"fallback {cause}: {what} is {OUTSIDE} (none)" for what, on in extra if on]


def outside_notes(geom: LineGeometry, *, table: FallbackTable | None = None) -> list[str]:
    """Why `geom` lies outside its cause's FD-calibrated domain ([] inside or for identity)."""
    tab, cause = table or default_table(), cause_of(geom)
    if cause not in (OFFSET, MASKED):
        return []
    return _notes(cause, (tab.offset if cause == OFFSET else tab.masked).domain, geom)


def _finish(geom: LineGeometry, result: LineResult, cause: str, z: tuple[float, ...],
            eeff_scale: float, est: float, notes: list[str]) -> LineResult:
    """The corrected LineResult: est raised to the closed form's when outside the domain."""
    outside = any(OUTSIDE in n for n in notes)
    verdict = Verdict("C", max(est, result.est_error_pct) if outside else est,
                      (*result.warnings, *notes))
    model = f"{result.model}+fallback:{cause}"
    eeff, eeff_f = result.eeff * eeff_scale, result.eeff_f * eeff_scale
    if geom.pair == "single":
        return single_line_result(z[0], eeff, eeff_f, model=model, accuracy_class="C",
                                  est_error_pct=verdict.est_error_pct, warnings=verdict.warnings)
    return pair_line_result(z[0], z[1], eeff, model=model, verdict=verdict, eeff_odd_f=eeff_f)


def _dot(terms: Mapping[str, float], x: Mapping[str, float]) -> float:
    return sum(c * feature_value(f, x) for f, c in terms.items())


def _offset(geom: LineGeometry, result: LineResult, fit: OffsetFit) -> LineResult:
    values = domain_values(geom)
    a, x = values["a"], _clamped(fit.domain, values)
    modes = ("single",) if geom.pair == "single" else ("even", "odd")
    es = [_dot({f: _interp(fit.knots, c, x["a"]) for f, c in fit.terms[m].items()}, x)
          for m in modes]
    zs = (result.z0,) if geom.pair == "single" else (result.z_even, result.z_odd)
    note = (f"fallback {OFFSET}: image method / (1 + e) at asymmetry {a:.3f}, e = "
            + " / ".join(f"{v:+.2%}" for v in es) + " (FD-calibrated)")
    return _finish(geom, result, OFFSET, tuple(z / (1 + e) for z, e in zip(zs, es, strict=True)),
                   1, fit.est["single" if geom.pair == "single" else "pair"],
                   [note, *_notes(OFFSET, fit.domain, geom)])


def _masked_pair(geom: LineGeometry, result: LineResult, fit: MaskFit) -> LineResult:
    if result.z_even is None or result.z_odd is None:
        raise ValueError(f"{MASKED}: the closed-form result of an edge pair has no z_even/z_odd")
    x, r = _clamped(fit.domain, domain_values(geom)), mask_ratio(geom)
    k, d = ({m: _dot(fit.terms[f"{p}_{m}"], x) for m in ("even", "odd")} for p in ("k", "d"))
    z_even, z_odd = (getattr(result, f"z_{m}") * r ** k[m] * math.exp(d[m]) for m in k)
    note = (f"fallback {MASKED}: bare pair x r^k e^d with the single-ended Wan-Hoorfar mask "
            f"ratio r {r:.4f}, k_even {k['even']:.3f}, k_odd {k['odd']:.3f} (FD-calibrated)")
    return _finish(geom, result, MASKED, (z_even, z_odd), (result.z_odd / z_odd) ** 2, fit.est,
                   [note, *_notes(MASKED, fit.domain, geom)])


def _identity(geom: LineGeometry, result: LineResult, table: FallbackTable) -> LineResult:
    causes = _identity_causes(geom)
    est = max([table.identity_est[c] for c in causes] + [result.est_error_pct])
    note = (f"fallback {','.join(causes)}: closed-form value kept (no correction model), "
            f"error estimate {est:g} %")
    return replace(result, model=f"{result.model}+fallback:{','.join(causes)}",
                   est_error_pct=est, warnings=(*result.warnings, note))


def correct(geom: LineGeometry, result: LineResult, *,
            table: FallbackTable | None = None) -> LineResult:
    """The fallback model of the geometry's cause applied to its closed-form `result`, whatever
    the result's class (calibration and FD seeding); a result already corrected is returned."""
    if "+fallback:" in result.model:
        return result
    tab, cause = table or default_table(), cause_of(geom)
    if cause == OFFSET:
        return _offset(geom, result, tab.offset)
    if cause == MASKED:
        return _masked_pair(geom, result, tab.masked)
    return _identity(geom, result, tab)


def apply(geom: LineGeometry, result: LineResult, *,
          table: FallbackTable | None = None) -> LineResult:
    """Class A / B results pass through unchanged; class C results are corrected (correct)."""
    return result if result.accuracy_class != "C" else correct(geom, result, table=table)


def z_even_error_pct(geom: LineGeometry, closed: LineResult | None = None, *,
                     table: FallbackTable | None = None) -> float | None:
    """The calibrated even-mode error bound (%) of a corrected pair; outside the calibrated
    domain raised to the closed form's estimate (`closed`, evaluated when not given). None for
    other causes."""
    tab, cause = table or default_table(), cause_of(geom)
    if geom.pair != "edge" or cause not in (OFFSET, MASKED):
        return None
    est = (tab.masked if cause == MASKED else tab.offset).even_est
    if not outside_notes(geom, table=tab):
        return est
    from . import evaluate       # the package registry (tline/__init__ imports this module)
    return max(est, (closed or evaluate(geom)).est_error_pct)
