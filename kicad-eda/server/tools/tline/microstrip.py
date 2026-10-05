"""Microstrip family (ADR D7.6): bare, coated (solder mask) and embedded microstrip.

Written from the published equations (sources: ADR D7.16), never from KiCad or scikit-rf code:
- static Z0 / eeff: Hammerstad & Jensen 1980 (MTT-S Digest 407-409) with their strip-thickness
  width increments du1 (air) / dur (dielectric), as given in the Qucs technical papers, node75;
- eeff(f): Kirschning & Jansen 1982 (Electron. Lett. 18(6):272);
- Z0(f): Jansen & Kirschning 1983 (AEU 37:108), applied only when f*h > 2 GHz*mm (critique E7:
  below that it moves Z0 by <= 0.04 % on every tested board, above it by up to +1.3 % at 5.8 GHz);
- covers (solder mask, embedding plies): Wan & Hoorfar 2000 (IEEE MGWL 10(6):223) filling factor
  q2 of each cover layer with Bahl-Stuchly air replacement, eeff += sum(dq_i * (er_i - 1)).
Error vs the [R-imp] FD solver (D7.6 table): bare -0.02 / +0.03 % (class A), coated
-0.66 / -1.22 % (B; -0.9..-1.9 % below u = 1, see wan_hoorfar_q2), embedded -0.73 % (B).
A reference stack of several plies uses the series (harmonic) mean Er; above 10 % Er
contrast the line is class C (fallback / FD, D7-05c).
Registers ('microstrip', 'single') and ('embedded_microstrip', 'single').
"""
from __future__ import annotations

import math
from collections.abc import Sequence
from typing import NamedTuple

from . import register
from .types import (CLASS_BOUND_PCT, C0, ETA0, LineGeometry, LineResult, Ply, Verdict, combine,
                    single_line_result)

Z0F_MIN_GHZ_MM = 2.0          # JK83 Z0(f) only when f*h exceeds this (critique E7)
CONTRAST_LIMIT = 0.10         # reference-stack Er contrast that makes a line class C (D7.6)
EST_BARE_PCT = 0.03           # H&J 1980 vs FD: -0.02 / +0.03 % [R-imp]
EST_COATED_PCT = 2.0          # Wan-Hoorfar mask vs FD: -0.66 / -1.22 % at u ~ 2 [R-imp];
#                               -0.9..-1.9 % at 0.4 <= u < 1 (D7-04 FD check, medium grid)
EST_EMBEDDED_PCT = 1.1        # Wan-Hoorfar cover vs FD: -0.73 % at u 1.5 [R-imp];
#                               -0.5..-1.02 % at 0.4 <= u < 1 (D7-04 FD check, medium grid)
EST_SERIES_PCT = 3.0          # series-mean Er at > 10 % contrast: +2.7 / -1.9 % side study (D7.6)
EST_OUTSIDE_PCT = 5.0         # outside published validity: nearest closed form, placeholder (D7.6)
# published validity (H&J 1980; KJ 1982 / JK 1983 as summarised in Qucs node75)
HJ_RANGE = {"u": (0.01, 100.0), "er": 128.0}
KJ_RANGE = {"u": (0.1, 100.0), "er": 20.0, "h_lambda": 0.13}
JK_RANGE = {"u": (0.1, 10.0), "er": 18.0, "h_lambda": 0.13}


class Substrate(NamedTuple):
    """The plies between a trace and its reference, collapsed to one layer."""

    h: float            # total thickness, mm
    er: float           # series (harmonic) mean Er
    contrast: float     # (max Er - min Er) / min Er
    plies: int


def require_width(geom: LineGeometry) -> None:
    if not geom.width > 0.0:
        raise ValueError(f"{geom.kind}: width must be > 0 mm to evaluate, got {geom.width!r}")


def substrate(plies: Sequence[Ply]) -> Substrate:
    """Total height and series-mean Er of a reference stack (the D7.6 multi-ply rule)."""
    if not plies:
        raise ValueError("no dielectric plies below the trace: a line needs its reference stack")
    h = sum(p.thickness for p in plies)
    er = plies[0].er if len(plies) == 1 else h / sum(p.thickness / p.er for p in plies)
    ers = [p.er for p in plies]
    return Substrate(h=h, er=er, contrast=(max(ers) - min(ers)) / min(ers), plies=len(plies))


def substrate_verdict(sub: Substrate, line: str) -> Verdict:
    if sub.plies == 1:
        return Verdict("A", 0.0)
    if sub.contrast <= CONTRAST_LIMIT:
        return Verdict("A", CLASS_BOUND_PCT["A"])
    return Verdict("C", EST_SERIES_PCT, (
        f"{line}: reference stack of {sub.plies} plies with Er contrast {sub.contrast:.0%} "
        f"> {CONTRAST_LIMIT:.0%}: series-mean Er {sub.er:.3f} - class C fallback/FD required",))


def etch_verdict(geom: LineGeometry) -> Verdict:
    if geom.etch <= 0.0:
        return Verdict("A", 0.0)
    return Verdict("A", 0.0, (f"etch {geom.etch} mm ignored by the closed form (rectangular "
                              "trace at the drawn width); the FD tier models the trapezoid",))


# ---------------------------------------------------------------- Hammerstad-Jensen 1980
def hj_z01(u: float) -> float:
    """Air impedance of a zero-thickness microstrip, u = W/h."""
    f = 6.0 + (2.0 * math.pi - 6.0) * math.exp(-((30.666 / u) ** 0.7528))
    return ETA0 / (2.0 * math.pi) * math.log(f / u + math.sqrt(1.0 + (2.0 / u) ** 2))


def hj_eeff(u: float, er: float) -> float:
    """Static effective permittivity of a zero-thickness microstrip."""
    a = (1.0 + math.log((u ** 4 + (u / 52.0) ** 2) / (u ** 4 + 0.432)) / 49.0
         + math.log(1.0 + (u / 18.1) ** 3) / 18.7)
    b = 0.564 * ((er - 0.9) / (er + 3.0)) ** 0.053
    return (er + 1.0) / 2.0 + (er - 1.0) / 2.0 * (1.0 + 10.0 / u) ** (-a * b)


def hj_width_increments(w: float, h: float, t: float, er: float) -> tuple[float, float]:
    """Strip-thickness width increments (du1 * h, dur * h) in mm: air and dielectric."""
    if t <= 0.0:
        return 0.0, 0.0
    u, tn = w / h, t / h
    coth = 1.0 / math.tanh(math.sqrt(6.517 * u))
    du1 = tn / math.pi * math.log(1.0 + 4.0 * math.e / (tn * coth * coth))
    dur = 0.5 * (1.0 + 1.0 / math.cosh(math.sqrt(er - 1.0))) * du1
    return du1 * h, dur * h


def hj_static(w: float, h: float, t: float, er: float) -> tuple[float, float]:
    """Static (Z0, eeff) of a microstrip with strip thickness t."""
    d1, dr = hj_width_increments(w, h, t, er)
    u1, ur = (w + d1) / h, (w + dr) / h
    eeff_r = hj_eeff(ur, er)
    z0 = hj_z01(ur) / math.sqrt(eeff_r)
    return z0, eeff_r * (hj_z01(u1) / hj_z01(ur)) ** 2


# ---------------------------------------------------------------- Wan-Hoorfar 2000 covers
def wan_hoorfar_q2(u: float, hbar: float) -> float:
    """Filling factor of the cover between the substrate surface (hbar = 1) and hbar = h2/h:
    the Wan & Hoorfar 2000 wide-strip form (eqs 4, 5, 12, 13), q2(u, 1) = 0, used for EVERY u.
    Their narrow-strip branch (u < 1) is discontinuous at u = 1 (Z steps about 1 %, so Z(w)
    is not monotone) and the D7-04 FD check (medium grid, 0.4 <= u < 1, h 0.2104 / 0.508)
    put it 2.1-3.2 % low against 0.9-1.9 % low for this form; the u >= 1 values are unchanged."""
    if hbar <= 1.0:
        return 0.0
    weff = u + 2.0 / math.pi * math.log(17.08 * (0.5 * u + 0.92))
    v = 2.0 / math.pi * math.atan(2.0 * math.pi / (weff * math.pi - 4.0) * (hbar - 1.0))
    q1 = 1.0 - math.log(weff * math.pi - 1.0) / (2.0 * weff)
    inner = (2.0 * weff * math.cos(0.5 * math.pi * v) / (2.0 * hbar - 1.0 + v)
             + math.sin(0.5 * math.pi * v))
    return max(0.0, 1.0 - q1 - (1.0 - v) / (2.0 * weff) * math.log(inner))


def cover_layers(geom: LineGeometry) -> tuple[tuple[float, float], ...]:
    """(thickness from the layer below, er) of each cover over the substrate surface, outward:
    the covering plies (the first also fills the trace's own height t), then the solder mask
    over bare substrate (JLC C1)."""
    plies = tuple((p.thickness + (geom.t if i == 0 else 0.0), p.er)
                  for i, p in enumerate(geom.above))
    mask = geom.mask
    if mask is None or mask.over_substrate <= 0.0:
        return plies
    return (*plies, (mask.over_substrate, mask.er))


def cover_delta_eeff(u: float, h: float, covers: Sequence[tuple[float, float]]) -> float:
    """Bahl-Stuchly air replacement: sum of dq_i * (er_i - 1) over the cover layers."""
    total, top, q_below = 0.0, 0.0, 0.0
    for thickness, er in covers:
        top += thickness
        q_top = wan_hoorfar_q2(u, 1.0 + top / h)
        total += max(q_top - q_below, 0.0) * (er - 1.0)
        q_below = q_top
    return total


# ---------------------------------------------------------------- dispersion
def kj_eeff_dispersion(eeff0: float, u: float, er: float, f_hz: float, h_mm: float) -> float:
    """Kirschning & Jansen 1982 eeff(f); fn = f * h in GHz*mm."""
    fn = f_hz * 1e-9 * h_mm
    p1 = (0.27488 + (0.6315 + 0.525 / (1.0 + 0.0157 * fn) ** 20) * u
          - 0.065683 * math.exp(-8.7513 * u))
    p2 = 0.33622 * (1.0 - math.exp(-0.03442 * er))
    p3 = 0.0363 * math.exp(-4.6 * u) * (1.0 - math.exp(-((fn / 38.7) ** 4.97)))
    p4 = 1.0 + 2.751 * (1.0 - math.exp(-((er / 15.916) ** 8)))
    p = p1 * p2 * ((0.1844 + p3 * p4) * fn) ** 1.5763
    return er - (er - eeff0) / (1.0 + p)


def jk_z0_dispersion(z00: float, eeff0: float, eeff_f: float, u: float, er: float,
                     f_hz: float, h_mm: float) -> float:
    """Jansen & Kirschning 1983 Z0(f) (power-current definition)."""
    fn = f_hz * 1e-9 * h_mm
    r1 = 0.03891 * er ** 1.4
    r2 = 0.267 * u ** 7
    r3 = 4.766 * math.exp(-3.228 * u ** 0.641)
    r4 = 0.016 + (0.0514 * er) ** 4.524
    r5 = (fn / 28.843) ** 12
    r6 = 22.2 * u ** 1.92
    r7 = 1.206 - 0.3144 * math.exp(-r1) * (1.0 - math.exp(-r2))
    r8 = 1.0 + 1.275 * (1.0 - math.exp(-0.004625 * r3 * er ** 1.674 * (fn / 18.365) ** 2.745))
    r9 = (5.086 * r4 * r5 / (0.3838 + 0.386 * r4) * math.exp(-r6) / (1.0 + 1.2992 * r5)
          * (er - 1.0) ** 6 / (1.0 + 10.0 * (er - 1.0) ** 6))
    r10 = 0.00044 * er ** 2.136 + 0.0184
    r11 = (fn / 19.47) ** 6 / (1.0 + 0.0962 * (fn / 19.47) ** 6)
    r12 = 1.0 / (1.0 + 0.00245 * u * u)
    r13 = 0.9408 * eeff_f ** r8 - 0.9603
    r14 = (0.9408 - r9) * eeff0 ** r8 - 0.9603
    r15 = 0.707 * r10 * (fn / 12.3) ** 1.097
    r16 = 1.0 + 0.0503 * er * er * r11 * (1.0 - math.exp(-((u / 15.0) ** 6)))
    r17 = r7 * (1.0 - 1.1241 * r12 / r16 * math.exp(-0.026 * fn ** 1.15656 - r15))
    return z00 * (r13 / r14) ** r17


def disperse(z0: float, eeff: float, u: float, er: float, h: float,
             freq_hz: float) -> tuple[float, float, str]:
    """(Z0(f), eeff(f), model tag). eeff(f) by KJ82 whenever f > 0 (no change once the covers
    lift eeff to the substrate Er); Z0(f) by JK83 only above f*h = 2 GHz*mm."""
    if freq_hz <= 0.0:
        return z0, eeff, ""
    eeff_f = kj_eeff_dispersion(eeff, u, er, freq_hz, h) if eeff < er else eeff
    if freq_hz * 1e-9 * h <= Z0F_MIN_GHZ_MM:
        return z0, eeff_f, "+kj82"
    return jk_z0_dispersion(z0, eeff, eeff_f, u, er, freq_hz, h), eeff_f, "+kj82+jk83"


# ---------------------------------------------------------------- assessment and model
def _outside(name: str, u: float, er: float, h_lambda: float | None, rng: dict) -> str | None:
    lo, hi = rng["u"]
    bad_lambda = h_lambda is not None and h_lambda > rng["h_lambda"]
    if lo <= u <= hi and er <= rng["er"] and not bad_lambda:
        return None
    detail = f"u={u:.3g} in [{lo:g}, {hi:g}], Er {er:.3g} <= {rng['er']:g}"
    if h_lambda is not None:
        detail += f", h/lambda0 {h_lambda:.3g} <= {rng['h_lambda']:g}"
    return (f"outside the published validity of {name} (needs {detail}): nearest closed form "
            "- class C fallback/FD required")


def validity_verdict(u: float, er: float, h: float, freq_hz: float) -> Verdict:
    h_lambda = h * freq_hz / (C0 * 1e3) if freq_hz > 0.0 else None
    found = [_outside("hj80", u, er, None, HJ_RANGE)]
    if h_lambda is not None:
        found.append(_outside("kj82", u, er, h_lambda, KJ_RANGE))
        if freq_hz * 1e-9 * h > Z0F_MIN_GHZ_MM:
            found.append(_outside("jk83", u, er, h_lambda, JK_RANGE))
    problems = tuple(p for p in found if p)
    return Verdict("C", EST_OUTSIDE_PCT, problems) if problems else Verdict("A", 0.0)


def _cover_model(geom: LineGeometry) -> tuple[str, Verdict]:
    has_mask = geom.mask is not None and geom.mask.over_substrate > 0.0
    if geom.above:
        tag = "hj80+wh00-embedded" + ("+mask" if has_mask else "")
        return tag, Verdict("B", max(EST_EMBEDDED_PCT, EST_COATED_PCT if has_mask else 0.0))
    if has_mask:
        return "hj80+wh00-mask", Verdict("B", EST_COATED_PCT)
    return "hj80", Verdict("A", EST_BARE_PCT)


def evaluate_microstrip(geom: LineGeometry, freq_hz: float) -> LineResult:
    """Single microstrip (bare, coated or embedded) at freq_hz (0 = static)."""
    require_width(geom)
    sub = substrate(geom.below)
    u = geom.width / sub.h
    z0, eeff = hj_static(geom.width, sub.h, geom.t, sub.er)
    delta = cover_delta_eeff(u, sub.h, cover_layers(geom))
    if delta > 0.0:
        z0, eeff = z0 * math.sqrt(eeff / (eeff + delta)), eeff + delta
    z_f, eeff_f, disp_tag = disperse(z0, eeff, u, sub.er, sub.h, freq_hz)
    base_model, base = _cover_model(geom)
    verdict = combine(base, substrate_verdict(sub, geom.kind), etch_verdict(geom),
                      validity_verdict(u, sub.er, sub.h, freq_hz))
    model = base_model + ("+series-er" if sub.plies > 1 else "") + disp_tag
    return single_line_result(z_f, eeff, eeff_f, model=model,
                              accuracy_class=verdict.accuracy_class,
                              est_error_pct=verdict.est_error_pct, warnings=verdict.warnings)


register("microstrip", "single", evaluate_microstrip)
register("embedded_microstrip", "single", evaluate_microstrip)
