"""Striplines (ADR D7.6): symmetric, offset and dual stripline, single and edge-coupled.

Written from the published equations (sources: ADR D7.16), never from KiCad or scikit-rf code:
- Cohn 1954 exact zero-thickness centred strip: Z0 = eta0/(4 sqrt(er)) K(k)/K(k'),
  k = sech(pi w / 2b) (the ratio falls as the strip widens; ADR D7.14's 97.963 ohm 'Cohn exact'
  row is the reciprocal ratio - correct value 90.611 ohm at w 0.3, b 0.5, Er 1);
- Wheeler 1978 (IEEE MTT-26(11):866, Wadell 3.5) centred strip with thickness, and its width
  increment dW;
- offset stripline: Wadell 3.6.3 image method - the line is two centred half structures of
  plane spacing 2 h_i + t, each holding half the capacitance and seeing its own side's Er
  (capacitance split); centred it is exactly Wheeler (or Cohn at t = 0);
- Cohn 1955 (IRE MTT-3(5):29) exact zero-thickness edge-coupled strips, evaluated at the
  EMPIRICAL mapping (w + dW, s - dW, b - t) calibrated against the [R-imp] FD solver (not from
  the literature): Zeven -0.73 % mean, Zodd +0.84 % mean / +1.84 % worst; offset pairs by the
  same image split per mode.
Geometry: `below` / `above` are the plies between the trace's bottom / top surface and its
reference plane, ordered outward; several plies on one side are collapsed to their series mean
Er. GND and power references are alike (AC ground). Classes: A for centred lines; offset by
asymmetry a = h_near/(h_near + h_far): A >= 0.45, B >= 0.35, else C; dielectric Er contrast
over 10 % is C; edge pairs with copper thickness are B (exact Cohn at t = 0 is A).
Dual stripline (critique M1): a signal layer between the trace and a plane is dielectric of its
adjacent prepreg's Er (dielectric_copper_ply; listed by name in geom.dielectric_copper) under
the orthogonal-routing assumption; never better than B, and C unless symmetric within 5 %.
Pair results (as fieldsolve2d.FdResult): z0 = sqrt(z_even z_odd), z_diff = 2 z_odd,
z_comm = z_even / 2, eeff / delay of the odd mode; est_error_pct refers to z_diff.
Registers ('stripline' | 'dual_stripline', 'single' | 'edge').
"""
from __future__ import annotations

import math
from itertools import pairwise
from typing import NamedTuple

from . import register
from .elliptic import coupled_moduli, k_ratio
from .microstrip import EST_OUTSIDE_PCT, EST_SERIES_PCT, etch_verdict, require_width, substrate
from .types import (C0, ETA0, LineGeometry, LineResult, Ply, Verdict, combine, delay_ps_per_mm,
                    single_line_result)

CONTRAST_LIMIT = 0.10          # dielectric Er contrast that makes a line class C (D7.6)
SYMMETRY_TOL = 0.05            # 'symmetric' = heights (and side Er) within 5 % (D7.6 step 4)
CLASS_A_ASYM, CLASS_B_ASYM = 0.45, 0.35
ASYM_EPS = 1e-9                # geometry rounding at the class thresholds
# worst image-method error vs FD over W 0.06/0.15/0.3 mm (b 0.6, Er 4.4, 0.5 oz; research FD
# solver, D7-05 run), rounded up to 0.1 %: measured 7.71 / 3.61 / 1.25 / 0.20 / 0.07 %
OFFSET_ERROR_PCT = ((0.15, 7.8), (0.25, 3.7), (0.35, 1.3), (0.45, 0.3), (0.5, 0.1))
EST_WHEELER_PCT = 0.2          # Wheeler vs FD: -0.16..+0.04 % for W/b 0.16-0.8 [R-imp]
WHEELER_MEASURED_WB = (0.16, 0.8)
EST_WHEELER_PUBLISHED_PCT = 0.5   # Wheeler's own claim for W/(b - t) < 10 (Wadell 3.5)
WHEELER_MAX_W_OVER_B = 10.0
EST_EXACT_PCT = 0.1            # Cohn exact at t = 0 (research FD agrees to 0.02-0.13 %)
EST_EDGE_PCT = 2.0             # mapped Cohn 1955: Zodd +1.84 %, Zeven -1.44 % worst [R-imp]
MODEL_EDGE = "cohn55@dW-map(empirical, FD-calibrated)"
C_REQUIRED = "class C fallback/FD required"


class Sides(NamedTuple):
    """Both sides of a buried trace, each collapsed to height (mm) and series-mean Er."""

    h_below: float
    er_below: float
    h_above: float
    er_above: float
    contrast: float     # (max Er - min Er) / min Er over every ply on both sides
    multi_ply: bool


# ---------------------------------------------------------------- closed forms
def _sech_tanh(x: float) -> tuple[float, float]:
    """(sech x, tanh x) for x > 0 without overflow or loss of the small one."""
    e = math.exp(-2.0 * x)
    return 2.0 * math.exp(-x) / (1.0 + e), (1.0 - e) / (1.0 + e)


def cohn_z0(w: float, b: float, er: float) -> float:
    """Cohn 1954: exact zero-thickness strip of width w centred between planes b apart."""
    k, kp = _sech_tanh(math.pi * w / (2.0 * b))
    return ETA0 / (4.0 * math.sqrt(er)) * k_ratio(k, kp)


def cohn_coupled(w: float, s: float, b: float, er: float) -> tuple[float, float]:
    """Cohn 1955: exact (Zeven, Zodd) of two zero-thickness strips, gap s, centred."""
    (ke, kep), (ko, kop) = coupled_moduli(math.pi * w / (2.0 * b), math.pi * (w + s) / (2.0 * b))
    f = ETA0 / (4.0 * math.sqrt(er))
    return f / k_ratio(ke, kep), f / k_ratio(ko, kop)


def wheeler_dw(w: float, b: float, t: float) -> float:
    """Wheeler 1978 thickness width increment (absolute, mm) of a strip in spacing b."""
    if t <= 0.0:
        return 0.0
    x = t / b
    m = 2.0 / (1.0 + 2.0 / 3.0 * x / (1.0 - x))
    dwn = (x / (math.pi * (1.0 - x))) * (1.0 - 0.5 * math.log(
        (x / (2.0 - x)) ** 2 + (0.0796 * x / (w / b + 1.1 * x)) ** m))
    return dwn * (b - t)


def wheeler_z0(w: float, b: float, t: float, er: float) -> float:
    """Wheeler 1978 centred stripline of thickness t >= 0 (dW = 0 at t = 0)."""
    wp = (w + wheeler_dw(w, b, t)) / (b - t)
    a = 4.0 / (math.pi * wp)
    return 30.0 / math.sqrt(er) * math.log(1.0 + a * (2.0 * a + math.sqrt((2.0 * a) ** 2 + 6.27)))


def _centred_air(w: float, b: float, t: float) -> float:
    return cohn_z0(w, b, 1.0) if t <= 0.0 else wheeler_z0(w, b, t, 1.0)


def _split(z_air1: float, z_air2: float, er1: float, er2: float) -> tuple[float, float]:
    """Image method: two halves, each half the capacitance, each in its own Er -> (Z, eeff)."""
    c1, c2 = 1.0 / (C0 * z_air1), 1.0 / (C0 * z_air2)
    c_air, c = 0.5 * (c1 + c2), 0.5 * (c1 * er1 + c2 * er2)
    return 1.0 / (C0 * math.sqrt(c * c_air)), c / c_air


def image_single(w: float, h1: float, h2: float, t: float, er1: float,
                 er2: float) -> tuple[float, float]:
    """(Z0, eeff) of a strip h1 / h2 (surface to plane) from its planes, by the image method."""
    return _split(_centred_air(w, 2.0 * h1 + t, t), _centred_air(w, 2.0 * h2 + t, t), er1, er2)


def mapped_coupled_air(w: float, s: float, b: float, t: float) -> tuple[float, float]:
    """Air (Zeven, Zodd) of a centred pair at the empirical mapping (w + dW, s - dW, b - t)."""
    dw = wheeler_dw(w, b, t)
    if s - dw <= 0.0:
        raise ValueError(f"stripline pair gap {s} mm is too small for the thickness mapping: "
                         f"s - dW = {s - dw:.4f} mm at t {t} mm")
    return cohn_coupled(w + dw, s - dw, b - t, 1.0)


def image_coupled(w: float, s: float, h1: float, h2: float, t: float, er1: float,
                  er2: float) -> tuple[float, float, float, float]:
    """(Zeven, Zodd, eeff_even, eeff_odd) of an edge pair by the per-mode image split."""
    ze1, zo1 = mapped_coupled_air(w, s, 2.0 * h1 + t, t)
    ze2, zo2 = mapped_coupled_air(w, s, 2.0 * h2 + t, t)
    ze, ee = _split(ze1, ze2, er1, er2)
    zo, eo = _split(zo1, zo2, er1, er2)
    return ze, zo, ee, eo


def offset_error_pct(a: float) -> float:
    """Image-method error estimate (%) at asymmetry a: linear through OFFSET_ERROR_PCT, and
    growing as (0.15 / a)^2 below the measured range."""
    a_min, e_min = OFFSET_ERROR_PCT[0]
    if a < a_min:
        return e_min * (a_min / a) ** 2
    for (a0, e0), (a1, e1) in pairwise(OFFSET_ERROR_PCT):
        if a <= a1:
            return e0 + (e1 - e0) * (a - a0) / (a1 - a0)
    return OFFSET_ERROR_PCT[-1][1]


# ---------------------------------------------------------------- geometry helpers
def dielectric_copper_ply(thickness: float, adjacent: Ply) -> Ply:
    """The ply that stands for a signal layer's copper in a dual stripline: its thickness,
    filled with the adjacent prepreg (Er, Df) - critique M1."""
    return Ply(thickness, adjacent.er, adjacent.df, adjacent.f_spec_hz)


def sides(geom: LineGeometry) -> Sides:
    """Heights and Er of both sides of a buried trace (both references must exist)."""
    if not geom.below:
        raise ValueError(f"{geom.kind}: no plies below the trace: a stripline needs a reference "
                         "plane below")
    if not geom.above:
        raise ValueError(f"{geom.kind}: no plies above the trace: a stripline needs a reference "
                         "plane above (one reference = embedded microstrip)")
    lo, hi = substrate(geom.below), substrate(geom.above)
    ers = [p.er for p in (*geom.below, *geom.above)]
    return Sides(lo.h, lo.er, hi.h, hi.er, (max(ers) - min(ers)) / min(ers),
                 lo.plies > 1 or hi.plies > 1)


def asymmetry(geom: LineGeometry) -> float:
    """a = h_near / (h_near + h_far) of a buried trace (0.5 = centred)."""
    s = sides(geom)
    return min(s.h_below, s.h_above) / (s.h_below + s.h_above)


def _exactly_centred(s: Sides) -> bool:
    return (math.isclose(s.h_below, s.h_above, rel_tol=1e-9)
            and math.isclose(s.er_below, s.er_above, rel_tol=1e-9))


def _symmetric(s: Sides) -> bool:
    return (abs(s.h_below - s.h_above) <= SYMMETRY_TOL * max(s.h_below, s.h_above)
            and abs(s.er_below - s.er_above) <= SYMMETRY_TOL * min(s.er_below, s.er_above))


def _require_gap(geom: LineGeometry) -> float:
    if geom.pair_gap is None or not geom.pair_gap > 0.0:
        raise ValueError(f"{geom.kind} edge pair: pair_gap (edge-to-edge gap) must be > 0 mm, "
                         f"got {geom.pair_gap!r}")
    return geom.pair_gap


# ---------------------------------------------------------------- verdicts
def _asym_verdict(geom: LineGeometry, s: Sides) -> Verdict:
    a = min(s.h_below, s.h_above) / (s.h_below + s.h_above)
    est = offset_error_pct(a)
    if a >= CLASS_A_ASYM - ASYM_EPS:
        return Verdict("A", est)
    if a >= CLASS_B_ASYM - ASYM_EPS:
        return Verdict("B", est)
    notes = [f"{geom.kind}: offset trace, asymmetry {a:.3f} (h {s.h_above:.4g} / "
             f"{s.h_below:.4g} mm): the image method reads up to {est:.1f} % high - {C_REQUIRED}"]
    if a < OFFSET_ERROR_PCT[0][0]:
        notes.append(f"{geom.kind}: asymmetry {a:.3f} is below the measured range "
                     f"({OFFSET_ERROR_PCT[0][0]}): error estimate extrapolated")
    return Verdict("C", est, tuple(notes))


def _contrast_verdict(geom: LineGeometry, s: Sides) -> Verdict:
    if s.contrast <= CONTRAST_LIMIT:
        return Verdict("A", 0.0)
    return Verdict("C", EST_SERIES_PCT, (
        f"{geom.kind}: dielectrics around the trace have Er contrast {s.contrast:.0%} > "
        f"{CONTRAST_LIMIT:.0%} (per-side series mean, image split) - {C_REQUIRED}",))


def _wheeler_verdict(geom: LineGeometry, s: Sides) -> Verdict:
    """Thickness-model accuracy: Cohn exact at t = 0, else Wheeler (measured / published)."""
    if geom.t <= 0.0:
        return Verdict("A", EST_EXACT_PCT)
    b = s.h_below + s.h_above + geom.t
    if geom.width / (b - geom.t) > WHEELER_MAX_W_OVER_B:
        return Verdict("C", EST_OUTSIDE_PCT, (
            f"{geom.kind}: outside the published validity of wheeler78 (W/(b - t) "
            f"{geom.width / (b - geom.t):.3g} > {WHEELER_MAX_W_OVER_B:g}): nearest closed form "
            f"- {C_REQUIRED}",))
    lo, hi = WHEELER_MEASURED_WB
    inside = lo <= geom.width / b <= hi
    return Verdict("A", EST_WHEELER_PCT if inside else EST_WHEELER_PUBLISHED_PCT)


def ignored_verdict(geom: LineGeometry) -> Verdict:
    """Inputs a buried closed form does not model: etch and mask (reported), coplanar grounds
    (class C)."""
    notes = [etch_verdict(geom)]
    if geom.mask is not None:
        notes.append(Verdict("A", 0.0, (f"{geom.kind}: solder mask ignored on a buried line",)))
    if geom.ground_gap is not None:
        notes.append(Verdict("C", EST_OUTSIDE_PCT, (
            f"{geom.kind}: coplanar grounds beside a stripline are not modelled (ground_gap "
            f"{geom.ground_gap} mm ignored) - {C_REQUIRED}",)))
    return combine(Verdict("A", 0.0), *notes)


def _check_dielectric_copper(geom: LineGeometry) -> None:
    ers = [p.er for p in (*geom.below, *geom.above)]
    for entry in geom.dielectric_copper:
        if len(entry) != 2 or not isinstance(entry[0], str) or not entry[0]:
            raise ValueError(f"dielectric_copper entries are (layer, er), got {entry!r}")
        name, er = entry
        if not any(math.isclose(er, x, rel_tol=1e-9) for x in ers):
            raise ValueError(f"dual stripline: dielectric_copper {name} has Er {er} but no ply "
                             "does: build its ply with dielectric_copper_ply(thickness, "
                             "adjacent prepreg)")


def _dual_verdict(geom: LineGeometry, s: Sides) -> Verdict:
    _check_dielectric_copper(geom)
    names = ", ".join(name for name, _ in geom.dielectric_copper)
    notes = [f"dual stripline: {names} treated as dielectric of the adjacent prepreg's Er "
             "(orthogonal-routing assumption: its tracks must not run parallel over this line, "
             "G4 R28)" if names else
             "dual stripline: no dielectric_copper listed - evaluated as an offset stripline"]
    if _symmetric(s):
        return Verdict("B", 0.0, tuple(notes))
    notes.append(f"dual stripline: references not symmetric within {SYMMETRY_TOL:.0%} "
                 f"(h {s.h_above:.4g} / {s.h_below:.4g} mm) - {C_REQUIRED}")
    return Verdict("C", 0.0, tuple(notes))


def _model_name(base: str, s: Sides, dual: bool) -> str:
    return (base + ("" if _exactly_centred(s) else "+image(wadell3.6.3)")
            + ("+series-er" if s.multi_ply else "") + ("+dual(orthogonal)" if dual else ""))


# ---------------------------------------------------------------- models
def pair_line_result(z_even: float, z_odd: float, eeff_odd: float, *, model: str,
                     verdict: Verdict, eeff_odd_f: float | None = None) -> LineResult:
    """LineResult of a coupled pair, in the FD tier's convention (fieldsolve2d.FdResult):
    z0 = sqrt(z_even z_odd), z_diff = 2 z_odd, z_comm = z_even / 2, eeff / eeff_f / delay of
    the odd (differential) mode."""
    eeff_f = eeff_odd if eeff_odd_f is None else eeff_odd_f
    return LineResult(z0=math.sqrt(z_even * z_odd), eeff=eeff_odd, eeff_f=eeff_f,
                      z_even=z_even, z_odd=z_odd,
                      z_diff=2.0 * z_odd, z_comm=0.5 * z_even,
                      delay_ps_per_mm=delay_ps_per_mm(eeff_f), model=model,
                      accuracy_class=verdict.accuracy_class,
                      est_error_pct=verdict.est_error_pct, warnings=verdict.warnings)


def _evaluate_single(geom: LineGeometry, dual: bool) -> LineResult:
    require_width(geom)
    s = sides(geom)
    z0, eeff = image_single(geom.width, s.h_above, s.h_below, geom.t, s.er_above, s.er_below)
    parts = [_asym_verdict(geom, s), _contrast_verdict(geom, s), _wheeler_verdict(geom, s),
             ignored_verdict(geom)]
    if dual:
        parts.append(_dual_verdict(geom, s))
    verdict = combine(*parts)
    model = _model_name("cohn54" if geom.t <= 0.0 else "wheeler78", s, dual)
    return single_line_result(z0, eeff, eeff, model=model, accuracy_class=verdict.accuracy_class,
                              est_error_pct=verdict.est_error_pct, warnings=verdict.warnings)


def _evaluate_edge(geom: LineGeometry, dual: bool) -> LineResult:
    require_width(geom)
    gap = _require_gap(geom)
    s = sides(geom)
    ze, zo, _, eo = image_coupled(geom.width, gap, s.h_above, s.h_below, geom.t, s.er_above,
                                  s.er_below)
    exact = geom.t <= 0.0
    parts = [_asym_verdict(geom, s), _contrast_verdict(geom, s), _wheeler_verdict(geom, s),
             ignored_verdict(geom),
             Verdict("A", EST_EXACT_PCT) if exact else Verdict("B", EST_EDGE_PCT)]
    if dual:
        parts.append(_dual_verdict(geom, s))
    return pair_line_result(ze, zo, eo, model=_model_name("cohn55" if exact else MODEL_EDGE, s,
                                                          dual),
                            verdict=combine(*parts))


def evaluate_stripline(geom: LineGeometry, freq_hz: float) -> LineResult:
    """Symmetric or offset stripline (TEM: no dispersion, eeff_f = eeff)."""
    return _evaluate_single(geom, dual=False)


def evaluate_stripline_edge(geom: LineGeometry, freq_hz: float) -> LineResult:
    """Edge-coupled stripline pair, centred or offset."""
    return _evaluate_edge(geom, dual=False)


def evaluate_dual_stripline(geom: LineGeometry, freq_hz: float) -> LineResult:
    """Dual stripline (critique M1): offset stripline with dielectric copper."""
    return _evaluate_single(geom, dual=True)


def evaluate_dual_stripline_edge(geom: LineGeometry, freq_hz: float) -> LineResult:
    """Edge-coupled pair in a dual stripline."""
    return _evaluate_edge(geom, dual=True)


register("stripline", "single", evaluate_stripline)
register("stripline", "edge", evaluate_stripline_edge)
register("dual_stripline", "single", evaluate_dual_stripline)
register("dual_stripline", "edge", evaluate_dual_stripline_edge)
