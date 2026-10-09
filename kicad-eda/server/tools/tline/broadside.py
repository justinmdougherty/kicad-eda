"""Broadside-coupled stripline (ADR D7.6): two strips stacked across a face gap between planes.

Written from the published equations (sources: ADR D7.16), never from KiCad code: Cohn 1960
(IRE MTT-8(6):633, doi 10.1109/TMTT.1960.1124809) via Rogers design note 3.2.1, zero thickness,
one dielectric:
    Z0e = (eta0 / 2) / sqrt(er) * K(k') / K(k),   Z0o = (eta0 pi / 4) (s/b) / (sqrt(er) atanh k),
    w/b = (2 / pi) [atanh(R) - (s/b) atanh(R / k)],  R = sqrt(k (k - s/b) / (1 - k s/b))
(the corrected width term of [R-imp]; the note prints the constants rounded as 188.3 and
296.1). The width equation is inverted for k = tanh z by bisection on z, with every
complementary quantity formed analytically (1 - R^2 = sech^2 z / (1 - k s/b)) so wide strips
keep their precision.
Validity. Cohn builds the formulas from the mapping of one strip edge, so they hold while the
two edges do not interact: his abstract restricts them to close coupling, and his wide-strip
condition (W/(b - t) >= 0.35 for thick strips; here the face gap s takes the place of t) reads
w >= 0.7 h_out. They are exact as s -> 0 (Cohn 1954 for the merged strip) and as w -> inf, and
read low for narrow strips across a wide gap - Zeven first, down to the unphysical Zeven < Zodd.
An FD sweep of this task (research solver, medium grid, 168 points: x = w/h_out 0.15-2.5,
y = g/h_out 0.25-3, tau = t/h_out 0-0.35) sets the envelope (ADR D7.6 accuracy table):
- class A (t = 0, one Er, centred): x >= 0.7 (Cohn), w >= 0.8 g (Zeven within 1 %: its 1 %
  crossing lies at w = 0.62-0.75 g), y <= 3; Zdiff within 0.3 %;
- class B: x >= 0.15, y <= 3, tau <= 0.35, t <= 0.35 g, and x >= 0.5 y - c(tau), c 0.05 at
  t = 0 rising linearly to 0.17 at tau 0.076 and constant beyond (Zeven within 3 %: its 3 %
  crossing is x = 0.484 y - 0.065 at t = 0, 0.514 y - 0.238 at tau 0.076; copper lowers it);
- class C otherwise - nearest closed form: Zdiff up to 11.2 % and Zeven 24.7 % low (y 3,
  x 0.15, t 0); 1 oz on a 0.05 mm gap Zdiff -2.3 %, Zeven +4.9 % - and whenever Zeven <= Zodd.
Copper thickness enters through an EMPIRICAL mapping calibrated against the [R-imp] FD solver
(not from the literature): the face-to-face gap g, planes b' = g + 2 h_out apart; the even mode
widens each strip by Wheeler's dW for a strip in 2 h_out + t ([R-imp]); the odd mode, whose
mid-gap plane is an electric wall, by that dW and the one of the inner half (spacing g + t)
weighted by the parallel-plate admittances 1/h_out and 2/g (the [R-imp] single dW left Zodd
0.8-6 % low on thin gaps). FD (homogeneous, 0.5 / 1 oz, g 0.1-0.2, h_out 0.2-0.4, W 0.08-0.6;
28 points, fine grid): Zodd -0.40 % mean, -1.16 % worst (the single dW: -2.8 % / -8.4 %), Zeven
+0.79 % mean, +2.72 % worst (1 oz on a 0.1 mm gap) - class B.
Inhomogeneous builds (another Er in the face gap than outside) weight each region's Er by its
parallel-plate capacitance per mode - the even mode charges only the outer plies, the odd mode
also the half gap to its virtual ground (exact for one dielectric; FD-checked on JLC 4.6/4.4
and RO4350B/RO4450F builds); Er contrast over 10 % is class C, as are unequal outer heights
(Cohn's pair is centred: evaluated at the mean, nearest closed form).
Geometry: `above` = plies from the upper strip to its plane (outward); `below` = the face-gap
plies (summing to `pair_gap`), then the plies from the lower strip to its plane; both strips
are `width` x `t`. Pair results as in stripline.pair_line_result (z0 = sqrt(z_even z_odd),
as fieldsolve2d.FdResult; est_error_pct refers to z_diff). Quasi-TEM: no dispersion.
Registers ('stripline', 'broadside').
"""
from __future__ import annotations

import math
from typing import NamedTuple

from . import register
from .elliptic import k_ratio, solve_monotone
from .microstrip import EST_OUTSIDE_PCT, EST_SERIES_PCT, require_width, substrate
from .stripline import (C_REQUIRED, CONTRAST_LIMIT, SYMMETRY_TOL, ignored_verdict,
                        pair_line_result, wheeler_dw)
from .types import ETA0, LineGeometry, LineResult, Verdict, combine

EST_BROADSIDE_PCT = 1.5        # class B z_diff vs FD: Zodd -1.16 % worst on 28 points (fine grid)
EST_COHN60_PCT = 0.3           # class A z_diff vs FD: <= +0.22 % in the class A region (sweep)
EST_WEAK_PCT = 11.5            # weak coupling: Zdiff up to 11.2 % low in the sweep
EST_THICK_PCT = 5.0            # t > 0.35 g: Zdiff up to -4.8 % (t = 1.4 g) in the sweep
COHN_WIDE_X = 0.7              # Cohn's wide-strip condition w/(b - s) >= 0.35: w >= 0.7 h_out
CLASS_A_W_OVER_G = 0.8         # t = 0: Zeven within 1 % when w >= 0.8 g (FD sweep)
SWEPT_X_MIN, SWEPT_Y_MAX, SWEPT_TAU_MAX = 0.15, 3.0, 0.35   # the FD sweep's range
T_OVER_G_MAX = 0.35            # thickness mapping calibrated up to 1 oz on a 0.1 mm face gap
WEAK_SLOPE = 0.5               # class B needs x >= WEAK_SLOPE * y - c(tau) (Zeven within 3 %)
WEAK_OFFSET = ((0.0, 0.05), (0.076, 0.17))   # c(tau): linear between, constant beyond
ENVELOPE_EPS = 1e-9            # geometry rounding at the envelope edges
GAP_TOL_MM = 1e-6              # face-gap plies must add up to pair_gap within this
MODEL = "cohn60@face-gap+dW-map(empirical, FD-calibrated)"
LINE = "broadside pair"


class Broadside(NamedTuple):
    """The three dielectric regions of a broadside pair (height mm, series-mean Er)."""

    h_above: float
    er_above: float
    gap: float
    er_gap: float
    h_below: float
    er_below: float
    contrast: float
    multi_ply: bool


class Ratios(NamedTuple):
    """The dimensionless broadside geometry the validity envelope is drawn in."""

    x: float           # w / h_out
    y: float           # g / h_out
    tau: float         # t / h_out
    t_over_g: float    # t / g
    w_over_g: float    # w / g


# ---------------------------------------------------------------- Cohn 1960
def _tanh_ln_sech2(z: float) -> tuple[float, float]:
    """(tanh z, ln sech^2 z) for z > 0; the log form never underflows for wide strips."""
    e = math.exp(-2.0 * z)
    return (1.0 - e) / (1.0 + e), math.log(4.0) - 2.0 * z - 2.0 * math.log1p(e)


def width_ratio(z: float, s_b: float) -> float:
    """w/b of the strips for modulus k = tanh z at spacing ratio s_b (note eq. 3, corrected R),
    with atanh x = ln(1 + x) - ln(1 - x^2) / 2, 1 - R^2 = sech^2 z / (1 - k s/b) and
    1 - (R/k)^2 = (s/b) sech^2 z / (k (1 - k s/b))."""
    k, ln_sech2 = _tanh_ln_sech2(z)
    one_minus_ks = 1.0 - k * s_b
    r2 = k * (k - s_b) / one_minus_ks
    if r2 <= 0.0:
        return 0.0
    r = math.sqrt(r2)
    atanh_r = math.log1p(r) - 0.5 * (ln_sech2 - math.log(one_minus_ks))
    atanh_rk = math.log1p(r / k) - 0.5 * (math.log(s_b) + ln_sech2 - math.log(k * one_minus_ks))
    return 2.0 / math.pi * (atanh_r - s_b * atanh_rk)


def solve_z(w_b: float, s_b: float) -> float:
    """z = atanh(k) with width_ratio(z, s_b) = w_b (bisection; w/b grows with z)."""
    if not 0.0 < s_b < 1.0 or not w_b > 0.0:
        raise ValueError(f"broadside: need 0 < s/b < 1 and w/b > 0, got s/b {s_b!r}, "
                         f"w/b {w_b!r}")
    lo = math.atanh(s_b)
    step = 1.0
    while width_ratio(lo + step, s_b) < w_b:
        step *= 2.0
    return solve_monotone(lambda z: width_ratio(z, s_b), w_b, lo, lo + step, rel_tol=1e-14)


def cohn_broadside(w: float, s: float, b: float, er: float) -> tuple[float, float]:
    """Cohn 1960 (Zeven, Zodd) of zero-thickness strips w wide, s apart, planes b apart."""
    s_b = s / b
    z = solve_z(w / b, s_b)
    k, ln_sech2 = _tanh_ln_sech2(z)
    root = math.sqrt(er)
    z_even = ETA0 / 2.0 / root / k_ratio(k, math.exp(0.5 * ln_sech2))
    z_odd = ETA0 * math.pi / 4.0 * s_b / (root * z)
    return z_even, z_odd


# ---------------------------------------------------------------- geometry
def regions(geom: LineGeometry) -> Broadside:
    """Split the geometry into the outer plies above, the face gap and the outer plies below."""
    gap = geom.pair_gap
    if gap is None or not gap > 0.0:
        raise ValueError(f"broadside pair: pair_gap (face-to-face gap between the strips) must "
                         f"be > 0 mm, got {gap!r}")
    if not geom.above:
        raise ValueError("broadside pair: no plies above the upper strip (its plane reference)")
    total, count = 0.0, 0
    while count < len(geom.below) and total < gap - GAP_TOL_MM:
        total += geom.below[count].thickness
        count += 1
    if abs(total - gap) > GAP_TOL_MM:
        raise ValueError(f"broadside pair: the first plies of `below` must make up the face gap "
                         f"pair_gap {gap} mm, got {total:.6g} mm")
    lower = geom.below[count:]
    if not lower:
        raise ValueError("broadside pair: no plies below the lower strip (its plane reference)")
    up, mid, down = substrate(geom.above), substrate(geom.below[:count]), substrate(lower)
    ers = [p.er for p in (*geom.above, *geom.below)]
    return Broadside(up.h, up.er, mid.h, mid.er, down.h, down.er,
                     (max(ers) - min(ers)) / min(ers),
                     up.plies > 1 or mid.plies > 1 or down.plies > 1)


def ratios(width: float, t: float, h_out: float, gap: float) -> Ratios:
    """The envelope coordinates of a pair: w/h_out, g/h_out, t/h_out, t/g and w/g."""
    return Ratios(width / h_out, gap / h_out, t / h_out, t / gap, width / gap)


def mode_permittivity(r: Broadside) -> tuple[float, float]:
    """(eeff_even, eeff_odd): region Er weighted by parallel-plate capacitance per mode."""
    y_up, y_down, y_gap = 1.0 / r.h_above, 1.0 / r.h_below, 4.0 / r.gap
    outer = r.er_above * y_up + r.er_below * y_down
    return outer / (y_up + y_down), (outer + r.er_gap * y_gap) / (y_up + y_down + y_gap)


def mode_widths(w: float, t: float, h_out: float, gap: float) -> tuple[float, float]:
    """(even, odd) mapped strip widths: w + dW of the outer half structure (plane spacing
    2 h_out + t) for the even mode; for the odd mode, whose mid-gap plane is an electric wall,
    that dW and the one of the inner half (spacing gap + t) weighted by their parallel-plate
    admittances 1/h_out and 2/gap."""
    dw_out = wheeler_dw(w, 2.0 * h_out + t, t)
    dw_in = wheeler_dw(w, gap + t, t)
    y_out, y_in = 1.0 / h_out, 2.0 / gap
    return w + dw_out, w + (dw_out * y_out + dw_in * y_in) / (y_out + y_in)


def _homogeneous(r: Broadside) -> bool:
    return r.contrast == 0.0


# ---------------------------------------------------------------- validity envelope
def weak_coupling_min_x(y: float, tau: float) -> float:
    """Smallest w/h_out at which the closed form's Zeven stays within 3 % of FD (class B), at
    face gap y = g/h_out and copper tau = t/h_out: 0.5 y - c(tau) (FD sweep, with margin)."""
    (tau0, c0), (tau1, c1) = WEAK_OFFSET
    c = c0 + (c1 - c0) * min(max(tau - tau0, 0.0) / (tau1 - tau0), 1.0)
    return WEAK_SLOPE * y - c


def _cohn_verdict(geom: LineGeometry, r: Broadside, q: Ratios) -> Verdict:
    """Class A only for bare, centred, homogeneous strips inside Cohn's wide-strip condition
    and the FD-measured 1 % envelope; the thickness mapping and everything else is class B."""
    if geom.t > 0.0:
        return Verdict("B", EST_BROADSIDE_PCT)
    wide = (q.x >= COHN_WIDE_X - ENVELOPE_EPS and q.w_over_g >= CLASS_A_W_OVER_G - ENVELOPE_EPS
            and q.y <= SWEPT_Y_MAX + ENVELOPE_EPS)
    if not wide:
        return Verdict("B", EST_BROADSIDE_PCT, (
            f"{LINE}: zero-thickness strips outside Cohn's wide-strip condition (w/h_out "
            f"{q.x:.3g} >= {COHN_WIDE_X:g} and w/g {q.w_over_g:.3g} >= {CLASS_A_W_OVER_G:g}): "
            "FD-calibrated, class B",))
    centred = math.isclose(r.h_above, r.h_below, rel_tol=ENVELOPE_EPS)
    if _homogeneous(r) and centred:
        return Verdict("A", EST_COHN60_PCT)
    return Verdict("B", EST_BROADSIDE_PCT)


def _range_verdict(q: Ratios) -> Verdict:
    """The FD sweep's range and the thickness mapping's calibration (t <= 0.35 g)."""
    parts = []
    if (q.x < SWEPT_X_MIN - ENVELOPE_EPS or q.y > SWEPT_Y_MAX + ENVELOPE_EPS
            or q.tau > SWEPT_TAU_MAX + ENVELOPE_EPS):
        parts.append(Verdict("C", EST_OUTSIDE_PCT, (
            f"{LINE}: w/h_out {q.x:.3g}, g/h_out {q.y:.3g}, t/h_out {q.tau:.3g} outside the "
            f"FD-validated range (w/h_out >= {SWEPT_X_MIN:g}, g/h_out <= {SWEPT_Y_MAX:g}, "
            f"t/h_out <= {SWEPT_TAU_MAX:g}): nearest closed form, error extrapolated - "
            f"{C_REQUIRED}",)))
    if q.t_over_g > T_OVER_G_MAX + ENVELOPE_EPS:
        parts.append(Verdict("C", EST_THICK_PCT, (
            f"{LINE}: copper t/g {q.t_over_g:.2f} > {T_OVER_G_MAX:g} of the face gap is outside "
            "the thickness mapping's calibration (FD: Zdiff up to -4.8 %, Zeven +7.6 % at "
            f"t/g 1.4) - {C_REQUIRED}",)))
    return combine(Verdict("A", 0.0), *parts)


def _coupling_verdict(q: Ratios) -> Verdict:
    """Weak coupling (narrow strips across a wide face gap) is outside Cohn 1960's validity."""
    x_min = weak_coupling_min_x(q.y, q.tau)
    if q.x >= x_min - ENVELOPE_EPS:
        return Verdict("A", 0.0)
    return Verdict("C", EST_WEAK_PCT, (
        f"{LINE}: weak coupling (w/h_out {q.x:.3g} < {x_min:.3g} at g/h_out {q.y:.3g}): Cohn "
        "1960 holds for close coupling only and reads low here (FD: Zdiff up to 11 %, Zeven up "
        f"to 25 %) - {C_REQUIRED}",))


def _physical_verdict(z_even: float, z_odd: float) -> Verdict:
    """A broadside pair always has Zeven > Zodd; anything else is the closed form failing."""
    if z_even > z_odd:
        return Verdict("A", 0.0)
    return Verdict("C", EST_WEAK_PCT, (
        f"{LINE}: the closed form gives Zeven {z_even:.2f} <= Zodd {z_odd:.2f} ohm, which is "
        f"unphysical (outside Cohn 1960's close coupling) - {C_REQUIRED}",))


def _build_verdict(geom: LineGeometry, r: Broadside) -> Verdict:
    """Dielectric contrast and centring (Cohn's pair is homogeneous and centred)."""
    parts = [ignored_verdict(geom)]
    if r.contrast > CONTRAST_LIMIT:
        parts.append(Verdict("C", EST_SERIES_PCT, (
            f"{LINE}: dielectrics with Er contrast {r.contrast:.0%} > {CONTRAST_LIMIT:.0%} "
            f"(per-mode parallel-plate weighting) - {C_REQUIRED}",)))
    if abs(r.h_above - r.h_below) > SYMMETRY_TOL * max(r.h_above, r.h_below):
        parts.append(Verdict("C", EST_OUTSIDE_PCT, (
            f"{LINE}: outer heights {r.h_above:.4g} / {r.h_below:.4g} mm differ by more than "
            f"{SYMMETRY_TOL:.0%}: Cohn 1960 is a centred pair, evaluated at the mean (nearest "
            f"closed form) - {C_REQUIRED}",)))
    return combine(*parts)


def _model_name(geom: LineGeometry, r: Broadside) -> str:
    return (("cohn60" if geom.t <= 0.0 else MODEL)
            + ("" if _homogeneous(r) else "+mode-er") + ("+series-er" if r.multi_ply else ""))


# ---------------------------------------------------------------- model
def evaluate_broadside(geom: LineGeometry, freq_hz: float) -> LineResult:
    """Broadside-coupled stripline pair (quasi-TEM, no dispersion modelled)."""
    require_width(geom)
    r = regions(geom)
    h_out = 0.5 * (r.h_above + r.h_below)
    q = ratios(geom.width, geom.t, h_out, r.gap)
    w_even, w_odd = mode_widths(geom.width, geom.t, h_out, r.gap)
    ze_air, _ = cohn_broadside(w_even, r.gap, r.gap + 2.0 * h_out, 1.0)
    _, zo_air = cohn_broadside(w_odd, r.gap, r.gap + 2.0 * h_out, 1.0)
    eeff_even, eeff_odd = mode_permittivity(r)
    z_even, z_odd = ze_air / math.sqrt(eeff_even), zo_air / math.sqrt(eeff_odd)
    verdict = combine(_cohn_verdict(geom, r, q), _build_verdict(geom, r), _range_verdict(q),
                      _coupling_verdict(q), _physical_verdict(z_even, z_odd))
    return pair_line_result(z_even, z_odd, eeff_odd, model=_model_name(geom, r),
                            verdict=verdict)


register("stripline", "broadside", evaluate_broadside)
