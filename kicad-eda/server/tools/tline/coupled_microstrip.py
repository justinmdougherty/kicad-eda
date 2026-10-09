"""Edge-coupled microstrip (ADR D7.6): a differential pair on an outer layer.

Written from the published equations (sources: ADR D7.16), never from KiCad or scikit-rf code:
Kirschning & Jansen 1984 (IEEE MTT-32(1):83, as summarised in the Qucs technical papers,
node77) static even / odd eeff and impedances at t = 0, referenced to the Hammerstad-Jensen
single line. Copper thickness enters through the EMPIRICAL mapping of [R-imp], calibrated
against its FD solver (not from the literature): KJ84 at t = 0 on w' = w + d, s' = s - d,
d = du_r + 0.2 (du1 - du_r), du1 / du_r the H&J air / dielectric width increments. Error vs FD:
Zdiff +0.19 % mean, 1.66 % worst (27 fit points, t/h <= 0.35), 1.62 % held out up to t/h 0.46,
3.27 % at t/h 0.7 (outside the calibration: class C). Mode permittivities follow H&J's own
thickness treatment: eeff = (Z_air / Z)^2 with Z from the d mapping and Z_air at the air
mapping (w + du1, s - du1) - within 4 % of FD for du1/s < 0.7 (the d-mapped KJ84 eeff is up to
+27 % off); identical to KJ84's eeff at t = 0. Quasi-static: dispersion is flagged above
f*h = 2 GHz*mm, not modelled.
No closed form covers a coated pair: with solder mask the BARE result is returned, class C
('mask ignored - class C fallback/FD required'; D7-05c fallback.apply adds the mask, FD
synthesises); covering plies likewise. Pair results as in stripline.pair_line_result
(z0 = sqrt(z_even z_odd), as fieldsolve2d.FdResult; est_error_pct refers to z_diff; z_even
is up to 2.8 % / 4.4 % off fit / held out). Dispersion of the pair is NOT modelled (eeff_f = eeff;
flagged above f*h = 2 GHz*mm).
Numeric domain: KJ84's odd-mode term exp(Q6 ln u u^-Q9) diverges once Q6 turns negative
(mapped g < 0.0015), so a mapped gap under KJ84_MIN_G h is a ValueError, like a gap the
thickness mapping closes; the air mapping then falls back to the closed-gap eeff.
Registers ('microstrip', 'edge').
"""
from __future__ import annotations

import math

from . import register
from .microstrip import (EST_OUTSIDE_PCT, etch_verdict, hj_eeff, hj_width_increments, hj_z01,
                         require_width, substrate, substrate_verdict)
from .stripline import C_REQUIRED, pair_line_result
from .types import ETA0, LineGeometry, LineResult, MaskModel, Verdict, combine

__all__ = ["evaluate_coupled_microstrip", "hj_width_increments", "kj84_domain_error",
           "kj84_static", "mapped_increments"]

MAPPING_WEIGHT = 0.2           # d = du_r + 0.2 (du1 - du_r) [R-imp]
EST_KJ84_PCT = 0.3             # KJ84 at t = 0 vs FD: Zodd 0.04 % mean, 0.22 % worst [R-imp]
EST_MAPPED_PCT = 2.0           # mapped, t/h <= 0.5: Zdiff 1.66 % fit / 1.62 % held-out worst
EST_THICK_PCT = 3.5            # t/h > 0.5: 3.27 % held out at t/h 0.7 [R-imp]
THICK_LIMIT = 0.5              # t/h covered by the calibration (fit 0.35, held out 0.46)
# the bare result vs the masked FD on JLC 7628 (S 0.15): +7.9 % at W 0.259 (93.64 / 86.80),
# +8.2 % at the D7.14 W_FD 0.2349 (97.42 / 90.0) [R-imp]; D7-05c's fallback replaces it
EST_MASK_IGNORED_PCT = 8.5
EST_COVER_IGNORED_PCT = 10.0   # a cover shifts Z ~9 % (embedded microstrip study [R-imp])
KJ84_RANGE = {"u": (0.1, 10.0), "g": (0.1, 10.0), "er": 18.0}
# numeric floor of the mapped g = s'/h: KJ84's Q6 changes sign at g 0.00152, below which its
# odd-mode term exp(Q6 ln u u^-Q9) diverges for u < 1 (OverflowError; D7-05 review 2)
KJ84_MIN_G = 0.002
EXP_LIMIT = 700.0              # math.exp overflows just above 709.78
DISPERSION_NOTE_GHZ_MM = 2.0
MODEL = "kj84@d-map(empirical, FD-calibrated)"
MASK_IGNORED = "mask ignored - class C fallback/FD required"
LINE = "microstrip edge pair"


# ---------------------------------------------------------------- Kirschning-Jansen 1984
def _eeff_even(u: float, g: float, er: float) -> float:
    v = u * (20.0 + g * g) / (10.0 + g * g) + g * math.exp(-g)
    a_e = (1.0 + math.log((v ** 4 + (v / 52.0) ** 2) / (v ** 4 + 0.432)) / 49.0
           + math.log(1.0 + (v / 18.1) ** 3) / 18.7)
    b_e = 0.564 * ((er - 0.9) / (er + 3.0)) ** 0.053
    return (er + 1.0) / 2.0 + (er - 1.0) / 2.0 * (1.0 + 10.0 / v) ** (-a_e * b_e)


def _eeff_odd(u: float, g: float, er: float, eeff_single: float) -> float:
    a_o = 0.7287 * (eeff_single - (er + 1.0) / 2.0) * (1.0 - math.exp(-0.179 * u))
    b_o = 0.747 * er / (0.15 + er)
    c_o = b_o - (b_o - 0.207) * math.exp(-0.414 * u)
    d_o = 0.593 + 0.694 * math.exp(-0.562 * u)
    return ((er + 1.0) / 2.0 + a_o - eeff_single) * math.exp(-c_o * g ** d_o) + eeff_single


def _q2(g: float) -> float:
    return 1.0 + 0.7519 * g + 0.189 * g ** 2.31


def _q4(u: float, g: float) -> float:
    q1 = 0.8695 * u ** 0.194
    q3 = (0.1975 + (16.6 + (8.4 / g) ** 6) ** -0.387
          + math.log(g ** 10 / (1.0 + (g / 3.4) ** 10)) / 241.0)
    return 2.0 * q1 / _q2(g) / (math.exp(-g) * u ** q3 + (2.0 - math.exp(-g)) * u ** -q3)


def _q6_q9(g: float) -> tuple[float, float]:
    q6 = (0.2305 + math.log(g ** 10 / (1.0 + (g / 5.8) ** 10)) / 281.3
          + math.log(1.0 + 0.598 * g ** 1.154) / 5.1)
    q7 = (10.0 + 190.0 * g * g) / (1.0 + 82.3 * g ** 3)
    q8 = math.exp(-6.5 - 0.95 * math.log(g) - (g / 0.15) ** 5)
    return q6, math.log(q7) * (q8 + 1.0 / 16.5)


def _q10_exponent(u: float, g: float) -> float:
    """Q6 ln(u) u^-Q9, the exponent of KJ84's odd-mode term, formed in log space so its own
    power cannot overflow; +-inf once its magnitude passes EXP_LIMIT."""
    q6, q9 = _q6_q9(g)
    lead = q6 * math.log(u)
    if lead == 0.0:
        return 0.0
    log_mag = math.log(abs(lead)) - q9 * math.log(u)
    if log_mag > math.log(EXP_LIMIT):
        return math.copysign(math.inf, lead)
    return math.copysign(math.exp(log_mag), lead)


def _q10(u: float, g: float, q4: float) -> float:
    q5 = 1.794 + 1.14 * math.log(1.0 + 0.638 / (g + 0.517 * g ** 2.43))
    return q4 - q5 / _q2(g) * math.exp(_q10_exponent(u, g))


def kj84_domain_error(u: float, g: float) -> str:
    """Why KJ84 cannot be evaluated at (u, g), '' when it can: below KJ84_MIN_G its Q6 turns
    negative and the odd-mode term diverges for u < 1; with u and g in the thousands (far
    outside its validity) the term leaves the float range too."""
    if not (u > 0.0 and g >= KJ84_MIN_G):
        return (f"kj84: mapped u {u:.3g}, g {g:.3g}: needs u > 0 and g >= {KJ84_MIN_G:g} "
                "(below it the odd-mode term diverges)")
    if _q10_exponent(u, g) > EXP_LIMIT:
        return (f"kj84: the odd-mode term overflows at mapped u {u:.3g}, g {g:.3g} (published "
                f"validity u <= {KJ84_RANGE['u'][1]:g}, g <= {KJ84_RANGE['g'][1]:g})")
    return ""


def kj84_static(u: float, g: float, er: float) -> tuple[float, float, float, float]:
    """(Zeven, Zodd, eeff_even, eeff_odd) of a zero-thickness pair, u = w/h, g = s/h.
    ValueError outside KJ84's numeric domain (kj84_domain_error)."""
    reason = kj84_domain_error(u, g)
    if reason:
        raise ValueError(reason)
    eeff = hj_eeff(u, er)
    z_single = hj_z01(u) / math.sqrt(eeff)
    load = z_single / ETA0 * math.sqrt(eeff)
    eeff_e, eeff_o = _eeff_even(u, g, er), _eeff_odd(u, g, er, eeff)
    q4 = _q4(u, g)
    z_even = z_single * math.sqrt(eeff / eeff_e) / (1.0 - load * q4)
    z_odd = z_single * math.sqrt(eeff / eeff_o) / (1.0 - load * _q10(u, g, q4))
    return z_even, z_odd, eeff_e, eeff_o


def mapped_increments(w: float, h: float, t: float, er: float) -> tuple[float, float]:
    """(d, du1*h) in mm: the empirical impedance mapping and the H&J air increment."""
    d1, dr = hj_width_increments(w, h, t, er)
    return dr + MAPPING_WEIGHT * (d1 - dr), d1


# ---------------------------------------------------------------- verdicts
def _require_gap(geom: LineGeometry) -> float:
    if geom.pair_gap is None or not geom.pair_gap > 0.0:
        raise ValueError(f"{LINE}: pair_gap (edge-to-edge gap) must be > 0 mm, "
                         f"got {geom.pair_gap!r}")
    return geom.pair_gap


def _masked(mask: MaskModel | None) -> bool:
    return mask is not None and max(mask.over_trace, mask.over_substrate, mask.between) > 0.0


def _thickness_verdict(t: float, h: float) -> Verdict:
    if t <= 0.0:
        return Verdict("A", EST_KJ84_PCT)
    if t / h <= THICK_LIMIT:
        return Verdict("B", EST_MAPPED_PCT)
    return Verdict("C", EST_THICK_PCT, (
        f"{LINE}: t/h {t / h:.2f} > {THICK_LIMIT} is outside the calibrated range of the "
        f"thickness mapping (3.3 % at t/h 0.7 held out) - {C_REQUIRED}",))


def _validity_verdict(u: float, g: float, er: float) -> Verdict:
    (u_lo, u_hi), (g_lo, g_hi) = KJ84_RANGE["u"], KJ84_RANGE["g"]
    if u_lo <= u <= u_hi and g_lo <= g <= g_hi and er <= KJ84_RANGE["er"]:
        return Verdict("A", 0.0)
    return Verdict("C", EST_OUTSIDE_PCT, (
        f"{LINE}: outside the published validity of kj84 (mapped u {u:.3g} in [{u_lo:g}, "
        f"{u_hi:g}], g {g:.3g} in [{g_lo:g}, {g_hi:g}], Er {er:.3g} <= {KJ84_RANGE['er']:g}): "
        f"nearest closed form - {C_REQUIRED}",))


def _context_verdict(geom: LineGeometry, h: float, freq_hz: float) -> Verdict:
    notes = [etch_verdict(geom)]
    if _masked(geom.mask):
        notes.append(Verdict("C", EST_MASK_IGNORED_PCT, (
            f"{LINE}: no closed form for a coated pair, the bare KJ84 result is returned: "
            f"{MASK_IGNORED}",)))
    if geom.above:
        notes.append(Verdict("C", EST_COVER_IGNORED_PCT, (
            f"{LINE}: {len(geom.above)} covering plies have no coupled closed form, the bare "
            f"result is returned - {C_REQUIRED}",)))
    fh = freq_hz * 1e-9 * h
    if fh > DISPERSION_NOTE_GHZ_MM:
        notes.append(Verdict("A", 0.0, (
            f"{LINE}: coupled-microstrip dispersion is not modelled: quasi-static eeff at "
            f"f*h = {fh:.2f} GHz*mm > {DISPERSION_NOTE_GHZ_MM:g}",)))
    return combine(Verdict("A", 0.0), *notes)


def _odd_eeff(w: float, s: float, h: float, d1: float, z_odd: float, kj_eeff_odd: float,
              er: float) -> tuple[float, Verdict]:
    """Odd-mode eeff from the air mapping (H&J's thickness treatment); KJ84's own eeff at the
    d mapping when the air-mapped gap closes (or leaves KJ84's numeric domain)."""
    u_air, g_air = (w + d1) / h, (s - d1) / h
    reason = kj84_domain_error(u_air, g_air)
    if reason:
        return min(max(kj_eeff_odd, 1.0), er), Verdict("A", 0.0, (
            f"{LINE}: no KJ84 air mapping at gap {s} mm (s - du1 = {s - d1:.4g} mm; {reason}): "
            "odd-mode eeff and delay from the impedance mapping, up to ~25 % high",))
    _, zo_air, _, _ = kj84_static(u_air, g_air, 1.0)
    return min(max((zo_air / z_odd) ** 2, 1.0), er), Verdict("A", 0.0)


# ---------------------------------------------------------------- model
def evaluate_coupled_microstrip(geom: LineGeometry, freq_hz: float) -> LineResult:
    """Edge-coupled microstrip pair (bare; see the module docstring for mask and covers)."""
    require_width(geom)
    s = _require_gap(geom)
    sub = substrate(geom.below)
    w, h, t = geom.width, sub.h, geom.t
    d, d1 = mapped_increments(w, h, t, sub.er)
    if (s - d) / h < KJ84_MIN_G:
        raise ValueError(f"{LINE}: gap {s} mm is too small: the mapped gap s - d = "
                         f"{s - d:.4g} mm (d {d:.4g} mm at t {t} mm) is below KJ84's numeric "
                         f"floor {KJ84_MIN_G:g} h = {KJ84_MIN_G * h:.4g} mm")
    u, g = (w + d) / h, (s - d) / h
    z_even, z_odd, _, kj_eeff_odd = kj84_static(u, g, sub.er)
    eeff_odd, eeff_note = _odd_eeff(w, s, h, d1, z_odd, kj_eeff_odd, sub.er)
    verdict = combine(_thickness_verdict(t, h), substrate_verdict(sub, LINE),
                      _validity_verdict(u, g, sub.er), _context_verdict(geom, h, freq_hz),
                      eeff_note)
    model = ("kj84" if t <= 0.0 else MODEL) + ("+series-er" if sub.plies > 1 else "")
    return pair_line_result(z_even, z_odd, eeff_odd, model=model, verdict=verdict)


register("microstrip", "edge", evaluate_coupled_microstrip)
