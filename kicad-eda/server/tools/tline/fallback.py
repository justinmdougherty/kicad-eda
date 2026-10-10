"""Class C fallback models (ADR D7.6 'Class C fallback models', critique M11; task D7-05c).

A class C closed-form result is the FD seed and, without FD (or over budget), the answer; this
module corrects it per cause. It holds the functional forms only: every coefficient, calibrated
range and est_error_pct comes from server/data/tline_fallback.json, written by
tools/tline_calibrate.py from FD runs with tests/fixtures/tline/fallback_calibration.json.
- offset_stripline (stripline / dual_stripline, single or edge, not centred): each mode's image
  result / (1 + e), e = sum_f c_f(a) x_f, x = 1, ln(W/b), t/b (+ ln(S/b) for pairs), c_f
  piecewise-linear in the asymmetry a = h_near / (h_near + h_far); eeff kept (homogeneous
  builds have eeff = Er exactly: the correction is geometric).
- masked_coupled_microstrip (edge pair with solder mask, no covering plies): the bare
  KJ84-mapped pair x r^k_even / r^k_odd, r = Z_coated / Z_bare of the single-ended Wan-Hoorfar
  microstrip of the same width, k = sum_f k_f x_f, x = 1, ln(W/h), ln(S/h), (c3 + t)/S (mask in
  the gap, as coplanar.mask_in_gaps); the mask adds dielectric only, so the air capacitance is
  kept and eeff_odd scales as (Zodd_bare / Zodd)^2.
- multi_ply_er_contrast, cpw_finite_ground, nearest_closed_form: identity (series-mean Er,
  infinite-ground CPW, the nearest closed form); est = max(tabled, the closed form's own).
Outside a cause's calibrated domain (or with coplanar grounds / etch, never calibrated) the
correction still applies but est is never below the closed form's. Results stay class C, model
'<model>+fallback:<cause>'. Stdlib only; registers no model (tline/__init__ lists the module).
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

SCHEMA = "kicad-eda/tline-fallback@1"
DATA_PATH = Path(__file__).resolve().parents[2] / "data" / "tline_fallback.json"
OFFSET, MASKED = "offset_stripline", "masked_coupled_microstrip"
MULTI_PLY, CPW_FINITE, NEAREST = "multi_ply_er_contrast", "cpw_finite_ground", "nearest_closed_form"
CAUSES = (OFFSET, MASKED, MULTI_PLY, CPW_FINITE, NEAREST)
IDENTITY_CAUSES = (MULTI_PLY, CPW_FINITE, NEAREST)
OFFSET_KINDS = ("stripline", "dual_stripline")
OFFSET_MODES = {"single": ("one", "ln_w_over_b", "t_over_b"),
                "even": ("one", "ln_w_over_b", "t_over_b", "ln_s_over_b"),
                "odd": ("one", "ln_w_over_b", "t_over_b", "ln_s_over_b")}
OFFSET_DOMAIN = ("a", "w_over_b", "t_over_b", "s_over_b", "contrast")
MASK_FEATURES = ("one", "ln_w_over_h", "ln_s_over_h", "mask_gap_fill")
MASK_DOMAIN = ("w_over_h", "s_over_h", "t_over_h", "mask_gap_fill", "er", "contrast",
               "mask_er", "mask_c1_over_h", "mask_c2_over_h")
LABELS = {"a": "asymmetry a", "mask_er": "mask Er", "mask_c1_over_h": "mask c1/h",
          "mask_c2_over_h": "mask c2/h", "mask_gap_fill": "mask gap fill (c3 + t)/S"}
OUTSIDE = "outside the FD-calibrated range"


@dataclass(frozen=True)
class OffsetFit:
    knots: tuple[float, ...]
    terms: Mapping[str, Mapping[str, tuple[float, ...]]]   # mode -> feature -> per knot
    est: Mapping[str, tuple[float, ...]]                   # 'single' | 'pair' -> per interval
    even_est: tuple[float, ...]
    domain: Mapping[str, tuple[float, float]]


@dataclass(frozen=True)
class MaskFit:
    k_even: Mapping[str, float]
    k_odd: Mapping[str, float]
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


# ---------------------------------------------------------------- data file
def _number(value: Any, where: str, *, positive: bool = False) -> float:
    ok = isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    if not ok or (positive and not value > 0):
        raise ValueError(f"{where}: expected a finite {'positive ' if positive else ''}number, "
                         f"got {value!r}")
    return float(value)


def _numbers(values: Any, where: str, count: int, *, positive: bool = False) -> tuple[float, ...]:
    if not isinstance(values, list) or len(values) != count:
        raise ValueError(f"{where}: expected a list of {count} numbers, got {values!r}")
    return tuple(_number(v, f"{where}[{i}]", positive=positive) for i, v in enumerate(values))


def _mapping(value: Any, where: str, allowed: tuple[str, ...]) -> Mapping[str, Any]:
    if not isinstance(value, dict) or not value:
        raise ValueError(f"{where}: expected a non-empty object, got {value!r}")
    unknown = sorted(set(value) - set(allowed))
    if unknown:
        raise ValueError(f"{where}: unknown names {unknown} (allowed: {list(allowed)})")
    return value


def _domain(value: Any, where: str, allowed: tuple[str, ...]) -> Mapping[str, tuple[float, float]]:
    out = {}
    for name, pair in _mapping(value, f"{where}.domain", allowed).items():
        lo, hi = _numbers(pair, f"{where}.domain.{name}", 2)
        if lo > hi:
            raise ValueError(f"{where}.domain.{name}: lower bound {lo} above upper bound {hi}")
        out[name] = (lo, hi)
    return MappingProxyType(out)


def _parse_offset(doc: Mapping[str, Any], where: str) -> OffsetFit:
    knots = _numbers(doc.get("knots"), f"{where}.knots", len(doc.get("knots") or ()))
    if len(knots) < 2 or any(b <= a for a, b in pairwise(knots)):
        raise ValueError(f"{where}.knots: expected at least two increasing values, got {knots}")
    terms = _mapping(doc.get("terms"), f"{where}.terms", tuple(OFFSET_MODES))
    parsed = {}
    for mode, allowed in OFFSET_MODES.items():
        given = _mapping(terms.get(mode), f"{where}.terms.{mode}", allowed)
        parsed[mode] = MappingProxyType({f: _numbers(v, f"{where}.terms.{mode}.{f}", len(knots))
                                         for f, v in given.items()})
    est = _mapping(doc.get("est_error_pct"), f"{where}.est_error_pct", ("single", "pair"))
    intervals = len(knots) - 1
    return OffsetFit(knots, MappingProxyType(parsed), MappingProxyType(
        {k: _numbers(est.get(k), f"{where}.est_error_pct.{k}", intervals, positive=True)
         for k in ("single", "pair")}),
        _numbers(doc.get("z_even_error_pct"), f"{where}.z_even_error_pct", intervals,
                 positive=True), _domain(doc.get("domain"), where, OFFSET_DOMAIN))


def _parse_masked(doc: Mapping[str, Any], where: str) -> MaskFit:
    def k(name: str) -> Mapping[str, float]:
        given = _mapping(doc.get(name), f"{where}.{name}", MASK_FEATURES)
        return MappingProxyType({f: _number(v, f"{where}.{name}.{f}") for f, v in given.items()})

    return MaskFit(k("k_even"), k("k_odd"),
                   _number(doc.get("est_error_pct"), f"{where}.est_error_pct", positive=True),
                   _number(doc.get("z_even_error_pct"), f"{where}.z_even_error_pct",
                           positive=True), _domain(doc.get("domain"), where, MASK_DOMAIN))


def parse_table(doc: Mapping[str, Any], *, source: str = "<dict>") -> FallbackTable:
    """Validate a tline_fallback document (ValueError naming the bad entry)."""
    if doc.get("schema") != SCHEMA:
        raise ValueError(f"{source}: schema must be {SCHEMA!r}, got {doc.get('schema')!r}")
    causes = doc.get("causes")
    if not isinstance(causes, dict) or set(causes) != set(CAUSES):
        raise ValueError(f"{source}: causes must be exactly {list(CAUSES)}, "
                         f"got {sorted(causes) if isinstance(causes, dict) else causes!r}")
    identity = {c: _number(causes[c].get("est_error_pct"), f"{source}: {c}.est_error_pct",
                           positive=True) for c in IDENTITY_CAUSES}
    text = json.dumps(doc, sort_keys=True, separators=(",", ":"))
    return FallbackTable(_parse_offset(causes[OFFSET], f"{source}: {OFFSET}"),
                         _parse_masked(causes[MASKED], f"{source}: {MASKED}"),
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


# ---------------------------------------------------------------- causes
def _masked(geom: LineGeometry) -> bool:
    m = geom.mask
    return m is not None and max(m.over_trace, m.over_substrate, m.between) > 0


def _identity_causes(geom: LineGeometry) -> tuple[str, ...]:
    closed = geom.kind in OFFSET_KINDS or geom.pair == "broadside"
    ers = [p.er for p in (geom.below + geom.above if closed else geom.below)]
    found = [MULTI_PLY] if ers and (max(ers) - min(ers)) / min(ers) > CONTRAST_LIMIT else []
    if geom.kind == "cpw" and geom.ground_width is not None:
        found.append(CPW_FINITE)
    return tuple(found) or (NEAREST,)


def cause_of(geom: LineGeometry) -> str:
    """The fallback cause a class C result of `geom` is corrected for (identity causes that
    apply together are joined by ',')."""
    if geom.kind in OFFSET_KINDS and geom.pair in ("single", "edge") and geom.below and geom.above:
        s = sides(geom)
        if not math.isclose(s.h_below, s.h_above, rel_tol=ASYM_EPS):
            return OFFSET
    if geom.kind == "microstrip" and geom.pair == "edge" and _masked(geom) and not geom.above:
        return MASKED
    return ",".join(_identity_causes(geom))


# ---------------------------------------------------------------- features and domains
def _features(values: Mapping[str, float]) -> dict[str, float]:
    """The fitted features from a cause's domain coordinates (offset: w/b, t/b, s/b; masked:
    w/h, s/h, (c3 + t)/S)."""
    if "a" in values:
        x = {"one": 1, "ln_w_over_b": math.log(values["w_over_b"]),
             "t_over_b": values["t_over_b"]}
        if "s_over_b" in values:
            x["ln_s_over_b"] = math.log(values["s_over_b"])
        return x
    return {"one": 1, "ln_w_over_h": math.log(values["w_over_h"]),
            "ln_s_over_h": math.log(values["s_over_h"]), "mask_gap_fill": values["mask_gap_fill"]}


def _clamped(domain: Mapping[str, tuple[float, float]], values: Mapping[str, float]
             ) -> dict[str, float]:
    """Coordinates moved to the nearest calibrated value (no extrapolation of the forms)."""
    return {k: min(max(v, domain[k][0]), domain[k][1]) if k in domain else v
            for k, v in values.items()}


def features(geom: LineGeometry) -> dict[str, float]:
    """The fitted features of an offset line or a masked pair (unclamped)."""
    return _features(domain_values(geom))


def mask_ratio(geom: LineGeometry) -> float:
    """Z_coated / Z_bare of the single-ended Wan-Hoorfar microstrip of the pair's width."""
    single = LineGeometry(kind="microstrip", pair="single", width=geom.width, t=geom.t,
                          below=geom.below, above=())
    return (evaluate_microstrip(replace(single, mask=geom.mask), 0).z0
            / evaluate_microstrip(single, 0).z0)


def domain_values(geom: LineGeometry) -> dict[str, float]:
    """The calibrated-domain coordinates of a corrected cause ({} for identity causes)."""
    cause = cause_of(geom)
    if cause == OFFSET:
        s = sides(geom)
        a, b = min(s.h_below, s.h_above) / (s.h_below + s.h_above), s.h_below + s.h_above + geom.t
        out = {"a": a, "w_over_b": geom.width / b, "t_over_b": geom.t / b, "contrast": s.contrast}
        if geom.pair == "edge":
            out["s_over_b"] = geom.pair_gap / b
        return out
    if cause == MASKED:
        sub, m = substrate(geom.below), geom.mask
        return {"w_over_h": geom.width / sub.h, "s_over_h": geom.pair_gap / sub.h,
                "t_over_h": geom.t / sub.h, "mask_gap_fill": (m.between + geom.t) / geom.pair_gap,
                "er": sub.er, "contrast": sub.contrast, "mask_er": m.er,
                "mask_c1_over_h": m.over_substrate / sub.h, "mask_c2_over_h": m.over_trace / sub.h}
    return {}


# ---------------------------------------------------------------- shared helpers
def _interp(knots: tuple[float, ...], values: tuple[float, ...], x: float) -> float:
    """Piecewise-linear in x through (knots, values), constant beyond the end knots."""
    if x <= knots[0]:
        return values[0]
    for (x0, y0), (x1, y1) in pairwise(zip(knots, values, strict=True)):
        if x <= x1:
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return values[-1]


def _interval_est(knots: tuple[float, ...], est: tuple[float, ...], x: float) -> float:
    """The estimate of the knot interval holding x (the larger one on an interior knot)."""
    hits = [e for (lo, hi), e in zip(pairwise(knots), est, strict=True) if lo <= x <= hi]
    if hits:
        return max(hits)
    return est[0] if x < knots[0] else est[-1]


def _notes(cause: str, domain: Mapping[str, tuple[float, float]], geom: LineGeometry
           ) -> list[str]:
    """One note per coordinate outside the calibrated domain and per uncalibrated input."""
    out = []
    for name, value in domain_values(geom).items():
        lo, hi = domain.get(name, (value, value))
        if not lo <= value <= hi:
            out.append(f"fallback {cause}: {LABELS.get(name, name)} {value:.4g} is {OUTSIDE} "
                       f"[{lo:.4g}, {hi:.4g}]: corrected at the nearest calibrated value")
    if geom.ground_gap is not None:
        out.append(f"fallback {cause}: coplanar grounds (ground_gap {geom.ground_gap} mm) are "
                   f"{OUTSIDE} (none)")
    if geom.etch > 0:
        out.append(f"fallback {cause}: etch {geom.etch} mm is {OUTSIDE} (none)")
    return out


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


# ---------------------------------------------------------------- the corrections
def _e(fit: OffsetFit, mode: str, a: float, x: Mapping[str, float]) -> float:
    """e of one mode: sum over the tabled features of c_f(a) x_f."""
    return sum(_interp(fit.knots, c, a) * x[f] for f, c in fit.terms[mode].items())


def _offset(geom: LineGeometry, result: LineResult, fit: OffsetFit) -> LineResult:
    values = domain_values(geom)
    a, x = values["a"], _features(_clamped(fit.domain, values))
    if geom.pair == "single":
        es = (_e(fit, "single", a, x),)
        z, key = (result.z0 / (1 + es[0]),), "single"
    else:
        es = (_e(fit, "even", a, x), _e(fit, "odd", a, x))
        z, key = (result.z_even / (1 + es[0]), result.z_odd / (1 + es[1])), "pair"
    note = (f"fallback {OFFSET}: image method / (1 + e) at asymmetry {a:.3f}, e = "
            + " / ".join(f"{v:+.2%}" for v in es) + " (FD-calibrated)")
    return _finish(geom, result, OFFSET, z, 1, _interval_est(fit.knots, fit.est[key], a),
                   [note, *_notes(OFFSET, fit.domain, geom)])


def _masked_pair(geom: LineGeometry, result: LineResult, fit: MaskFit) -> LineResult:
    if result.z_even is None or result.z_odd is None:
        raise ValueError(f"{MASKED}: the closed-form result of an edge pair has no z_even/z_odd")
    x, r = _features(_clamped(fit.domain, domain_values(geom))), mask_ratio(geom)
    k_even = sum(c * x[f] for f, c in fit.k_even.items())
    k_odd = sum(c * x[f] for f, c in fit.k_odd.items())
    z_odd = result.z_odd * r ** k_odd
    note = (f"fallback {MASKED}: bare pair x r^k with the single-ended Wan-Hoorfar mask ratio "
            f"r {r:.4f}, k_even {k_even:.3f}, k_odd {k_odd:.3f} (FD-calibrated)")
    return _finish(geom, result, MASKED, (result.z_even * r ** k_even, z_odd),
                   (result.z_odd / z_odd) ** 2, fit.est, [note, *_notes(MASKED, fit.domain, geom)])


def _identity(geom: LineGeometry, result: LineResult, table: FallbackTable) -> LineResult:
    causes = _identity_causes(geom)
    est = max([table.identity_est[c] for c in causes] + [result.est_error_pct])
    note = (f"fallback {','.join(causes)}: closed-form value kept (no correction model), "
            f"error estimate {est:g} %")
    return replace(result, model=f"{result.model}+fallback:{','.join(causes)}",
                   est_error_pct=est, warnings=(*result.warnings, note))


# ---------------------------------------------------------------- public API
def correct(geom: LineGeometry, result: LineResult, *,
            table: FallbackTable | None = None) -> LineResult:
    """The fallback model of the geometry's cause applied to its closed-form `result`, whatever
    the result's class (calibration and FD seeding); a result already corrected is returned."""
    if "+fallback:" in result.model:
        return result
    tab = table or default_table()
    cause = cause_of(geom)
    if cause == OFFSET:
        return _offset(geom, result, tab.offset)
    if cause == MASKED:
        return _masked_pair(geom, result, tab.masked)
    return _identity(geom, result, tab)


def apply(geom: LineGeometry, result: LineResult, *,
          table: FallbackTable | None = None) -> LineResult:
    """Class A / B results pass through unchanged; class C results are corrected (correct)."""
    if result.accuracy_class != "C":
        return result
    return correct(geom, result, table=table)


def z_even_error_pct(geom: LineGeometry, *, table: FallbackTable | None = None) -> float | None:
    """The calibrated even-mode error bound (%) of a corrected pair; None for other causes."""
    tab = table or default_table()
    cause = cause_of(geom)
    if geom.pair != "edge" or cause not in (OFFSET, MASKED):
        return None
    if cause == MASKED:
        return tab.masked.even_est
    return _interval_est(tab.offset.knots, tab.offset.even_est, domain_values(geom)["a"])
