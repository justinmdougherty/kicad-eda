"""Coplanar lines (ADR D7.6): conductor-backed CPW (CPWG) and CPW without a back plane.

Written from the published conformal-mapping equations (Ghione & Naldi 1987 via Simons,
Coplanar Waveguide Circuits, Wiley 2001; Wadell 3.4), strip thickness t = 0, with a = W/2,
b = W/2 + S, k = a/b:
- CPWG: k3 = tanh(pi a / 2h) / tanh(pi b / 2h),
  eeff = (q0 + er q3)/(q0 + q3), Z0 = (eta0 / 2) / sqrt((q0 + er q3)(q0 + q3)),
  q = K(k)/K(k'), the top grounds wide (the FD references use finite grounds; +/-0.9 %).
- CPW: k1 = sinh(pi a / 2h) / sinh(pi b / 2h),
  eeff = 1 + (er - 1)/2 * q1/q0, Z0 = (eta0 / 4) / (q0 sqrt(eeff)).
Thickness enters through an EMPIRICAL mapping calibrated against the [R-imp] FD solver, not
taken from the literature: the t = 0 forms are evaluated at W + du1*h, S - du1*h/2 with du1
the Hammerstad-Jensen air width increment (CPWG +/-0.9 % on five 1 oz geometries, class A).
Solder mask adds the crude gap-fill capacitance (er_mask - 1) eps0 (c3 + t) / S (+1.0..+1.9 %,
class B). CPW without a back plane is not FD-calibrated (class B); with finite coplanar grounds
it is class C (infinite-ground seed, D7-05c fallback / FD). Quasi-static: no dispersion model.
Registers ('cpwg', 'single') and ('cpw', 'single').
"""
from __future__ import annotations

import math

from . import register
from .elliptic import k_ratio, sinh_ratio_modulus, tanh_ratio_modulus
from .microstrip import (EST_OUTSIDE_PCT, etch_verdict, hj_width_increments, require_width,
                         substrate, substrate_verdict)
from .types import (C0, EPS0, ETA0, LineGeometry, LineResult, MaskModel, Verdict, combine,
                    single_line_result)

EST_CPWG_PCT = 0.9            # mapped Ghione-Naldi vs FD, five geometries [R-imp]
EST_CPWG_MASK_PCT = 1.9       # + crude mask-in-gap term: +1.0..+1.9 % [R-imp]
EST_CPW_PCT = 3.0             # CPW without back plane: unmeasured, held to the class B bound
EST_CPW_FINITE_PCT = 5.0      # finite grounds, no back plane: 5 % placeholder (D7.6 fallback)
DISPERSION_NOTE_GHZ_MM = 2.0  # quasi-static eeff is flagged above this f*h
MODEL_CPWG = "gn87-cpwg@du1-map(empirical, FD-calibrated)"
MODEL_CPW = "gn87-cpw@du1-map(empirical, FD-calibrated on CPWG)"
MASK_TAG = "+mask-gap(crude)"


def _moduli(w: float, s: float) -> tuple[float, float]:
    """k = a/b and k' = sqrt((b - a)(b + a))/b for a = w/2, b = w/2 + s."""
    b = 0.5 * w + s
    return 0.5 * w / b, math.sqrt(s * (w + s)) / b


def cpwg_static(w: float, s: float, h: float, er: float) -> tuple[float, float]:
    """(Z0, eeff) of a conductor-backed CPW, t = 0, wide top grounds."""
    k, kp = _moduli(w, s)
    k3, k3p = tanh_ratio_modulus(math.pi * w / (4.0 * h), math.pi * (w + 2.0 * s) / (4.0 * h))
    q0, q3 = k_ratio(k, kp), k_ratio(k3, k3p)
    c_air, c_die = q0 + q3, q0 + er * q3
    return ETA0 / (2.0 * math.sqrt(c_die * c_air)), c_die / c_air


def cpw_static(w: float, s: float, h: float, er: float) -> tuple[float, float]:
    """(Z0, eeff) of a CPW on a substrate of height h without a back plane, t = 0, wide grounds."""
    k, kp = _moduli(w, s)
    k1, k1p = sinh_ratio_modulus(math.pi * w / (4.0 * h), math.pi * (w + 2.0 * s) / (4.0 * h))
    q0, q1 = k_ratio(k, kp), k_ratio(k1, k1p)
    eeff = 1.0 + 0.5 * (er - 1.0) * q1 / q0
    return ETA0 / (4.0 * q0 * math.sqrt(eeff)), eeff


def mapped_geometry(w: float, s: float, h: float, t: float, er: float) -> tuple[float, float]:
    """The empirical thickness mapping (W + du1*h, S - du1*h/2) of [R-imp]."""
    d1, _ = hj_width_increments(w, h, t, er)
    s_mapped = s - 0.5 * d1
    if s_mapped <= 0.0:
        raise ValueError(f"ground_gap {s} mm is too small for the thickness mapping: "
                         f"S - du1*h/2 = {s_mapped:.4f} mm at t {t} mm")
    return w + d1, s_mapped


def mask_in_gaps(z0: float, eeff: float, mask: MaskModel, t: float,
                 s: float) -> tuple[float, float]:
    """Crude solder-mask term: (er_mask - 1) eps0 (c3 + t) / S added to the line capacitance."""
    c = math.sqrt(eeff) / (C0 * z0)
    c_air = 1.0 / (C0 * z0 * math.sqrt(eeff))
    c += (mask.er - 1.0) * EPS0 * (mask.between + t) / s
    return 1.0 / (C0 * math.sqrt(c * c_air)), c / c_air


def _require_gap(geom: LineGeometry) -> float:
    if geom.ground_gap is None or not geom.ground_gap > 0.0:
        raise ValueError(f"{geom.kind}: ground_gap (signal-to-coplanar-ground gap) must be "
                         f"> 0 mm, got {geom.ground_gap!r}")
    return geom.ground_gap


def _base_verdict(geom: LineGeometry, backed: bool) -> Verdict:
    masked = Verdict("B", EST_CPWG_MASK_PCT) if geom.mask is not None else Verdict("A", 0.0)
    if backed:
        return combine(Verdict("A", EST_CPWG_PCT), masked)
    if geom.ground_width is not None:
        return combine(masked, Verdict("C", EST_CPW_FINITE_PCT, (
            f"cpw: finite coplanar grounds ({geom.ground_width} mm) without a back plane: "
            "infinite-ground closed form - class C fallback/FD required",)))
    return combine(masked, Verdict("B", EST_CPW_PCT, (
        "cpw: CPW without a back plane is not FD-calibrated (class B bound); FD verifies it",)))


def _context_verdict(geom: LineGeometry, h: float, freq_hz: float) -> Verdict:
    notes = []
    if geom.above:
        notes.append(Verdict("C", EST_OUTSIDE_PCT, (
            f"{geom.kind}: covered coplanar line ({len(geom.above)} plies above the trace) has "
            "no closed form: the uncovered line is used - class C fallback/FD required",)))
    fh = freq_hz * 1e-9 * h
    if fh > DISPERSION_NOTE_GHZ_MM:
        notes.append(Verdict("A", 0.0, (
            f"{geom.kind}: coplanar dispersion is not modelled: quasi-static eeff at "
            f"f*h = {fh:.2f} GHz*mm > {DISPERSION_NOTE_GHZ_MM:g}",)))
    return combine(Verdict("A", 0.0), *notes)


def _evaluate(geom: LineGeometry, freq_hz: float, backed: bool) -> LineResult:
    require_width(geom)
    gap = _require_gap(geom)
    sub = substrate(geom.below)
    w_m, s_m = mapped_geometry(geom.width, gap, sub.h, geom.t, sub.er)
    z0, eeff = (cpwg_static if backed else cpw_static)(w_m, s_m, sub.h, sub.er)
    model = MODEL_CPWG if backed else MODEL_CPW
    if geom.mask is not None:
        z0, eeff = mask_in_gaps(z0, eeff, geom.mask, geom.t, gap)
        model += MASK_TAG
    if sub.plies > 1:
        model += "+series-er"
    verdict = combine(_base_verdict(geom, backed), substrate_verdict(sub, geom.kind),
                      etch_verdict(geom), _context_verdict(geom, sub.h, freq_hz))
    return single_line_result(z0, eeff, eeff, model=model, accuracy_class=verdict.accuracy_class,
                              est_error_pct=verdict.est_error_pct, warnings=verdict.warnings)


def evaluate_cpwg(geom: LineGeometry, freq_hz: float) -> LineResult:
    """Conductor-backed coplanar waveguide (the back plane is the reference below)."""
    return _evaluate(geom, freq_hz, backed=True)


def evaluate_cpw(geom: LineGeometry, freq_hz: float) -> LineResult:
    """Coplanar waveguide on a substrate without a back plane."""
    return _evaluate(geom, freq_hz, backed=False)


register("cpwg", "single", evaluate_cpwg)
register("cpw", "single", evaluate_cpw)
