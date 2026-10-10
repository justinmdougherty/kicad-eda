"""Calibrate the class C fallback models of tline/fallback.py against the FD tier (D7-05c).

Developer CLI, not run in CI: FD solves need numpy + scipy (fieldsolve2d), the fits numpy.
    .venv/Scripts/python.exe server/tools/tline_calibrate.py --jobs 3   # FD + fit (~20 min)
    .venv/Scripts/python.exe server/tools/tline_calibrate.py --refit    # refit committed FD data
    .venv/Scripts/python.exe server/tools/tline_calibrate.py --check    # compare, write nothing
(--refit / --check reuse the FD values of --fd-source, default the --fixture path.)
Outputs, byte-stable on one platform (no timestamps or run times; the run time is printed);
--check also accepts floats within CHECK_TOL, as libm last bits differ across platforms:
- server/data/tline_fallback.json: per cause the form, coefficients, est_error_pct, calibrated
  domain, fit and held-out statistics; read by tline/fallback.py;
- tests/fixtures/tline/fallback_calibration.json: every FD point (name, cause, set, geometry,
  FD values, closed form, fallback value and error); tests/test_tline_fallback.py checks every
  point against its est_error_pct and refits the data file from it (rerun --refit whenever a
  closed form changes).
Calibration set (FD medium grid; mm, ohm):
- the domain of each corrected cause is a declared box (OFFSET_BOX, MASK_BOX; plus the sampled
  range of the masked derived coordinates); seeded random points drawn uniformly over it (log
  scale for ratios) form a fit set and an independent held-out set (stdlib random: the same
  geometries on every platform);
- the task grid: masked edge-coupled microstrip, JLC mask, h 0.0994 / 0.2104 / 0.508 x S/W
  0.5 / 1 / 2 near Zdiff 90 (fit; width with the bare closed form 7 % above the target, gap
  >= 0.11 mm) and S/W 0.75 / 1.5 / 2.5 (held out); offset stripline at asymmetry 0.15-0.35
  (fit); held-out named vectors: the [R-imp] USB pair (FD 86.80), DMS90_7628_mask, mask / copper
  / Dk variants, SL50_off, DSL100_off and the JLC06161H-2116 In2 dual stripline;
- measured identity causes: two-ply microstrips with Er contrast (named and random), centred
  stripline with side contrast, CPW without a back plane with finite grounds.
Fits (numpy least squares) on the fit set: offset, per mode, e = Z_image / Z_FD - 1 on
hat_k(a) x feature over KNOTS (every knot free); masked, per mode, ln(Z_FD / Z_bare) on
ln(r) x feature + feature. est_error_pct = EST_MARGIN x the worst |error| over the fit and
held-out points inside the domain, rounded up to the next 0.1 % (offset: single z0, pair
z_diff and z_even; masked: z_diff and z_even); identity causes: the larger of the ADR
placeholder and that. One estimate per mode, not per asymmetry interval: thin intervals hold
few points and their sample maxima understated independent FD probes (round-1 review).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from typing import Any

import tline
from tline import fallback as fb
from tline.elliptic import solve_monotone
from tline.stripline import dielectric_copper_ply
from tline.types import LineGeometry, LineResult, MaskModel, Ply

ROOT = Path(__file__).resolve().parents[2]
DATA_OUT = ROOT / "server" / "data" / "tline_fallback.json"
FIXTURE_OUT = ROOT / "tests" / "fixtures" / "tline" / "fallback_calibration.json"
FIXTURE_REL = "tests/fixtures/tline/fallback_calibration.json"
FIXTURE_SCHEMA = "kicad-eda/tline-fallback-calibration@2"
GENERATED_BY = "server/tools/tline_calibrate.py"
GRID = "medium"
T_1OZ, T_HALF = 0.035, 0.0152
JLC_MASK = MaskModel(er=3.8, over_trace=0.0152, over_substrate=0.0305, between=0.0305)
KNOTS = (0.1, 0.15, 0.2, 0.3, 0.4, 0.5)
EST_MARGIN = 1.5             # est = margin x worst sampled error (a sample max is no bound):
#                              independent probes reached 1.46 x the calibration worst
PLACEHOLDER_PCT = {fb.MULTI_PLY: 3.0, fb.CPW_FINITE: 5.0, fb.NEAREST: 5.0}   # ADR D7.6 table
# declared domains: sampled uniformly (log scale where the range spans > 2x), t/W and t/S cap
# the copper (the mapped thickness models break down beyond: stripline pairs -4..-9 % at t/S 0.45)
OFFSET_BOX = {"a": (0.1, 0.5), "w_over_b": (0.08, 0.7), "t_over_b": (0.012, 0.14),
              "t_over_w": (0.0, 0.5), "er_near_over_far": (0.8, 1.25),
              "side_contrast": (0.0, 0.1), "s_over_b": (0.1, 0.6), "t_over_s": (0.0, 0.35)}
MASK_BOX = {"w_over_h": (0.5, 2.5), "s_over_w": (0.4, 3.0), "t_over_h": (0.03, 0.4),
            "t_over_w": (0.0, 0.5), "t_over_s": (0.0, 0.35), "er": (3.0, 4.8),
            "contrast": (0.0, 0.1), "mask_er": (3.2, 4.2), "mask_c1_over_h": (0.025, 0.4),
            "mask_c2_over_c1": (0.3, 1.0), "mask_c3_over_c1": (0.8, 1.5)}
ER_RANGE, PLANE_MM, H_MAX_MM = (3.0, 4.8), (0.15, 1.6), 0.6
SEEDS = {"offset_fit": 1101, "offset_held": 2202, "masked_fit": 3303, "masked_held": 4404,
         "multi": 5505}
COUNTS = {"offset_fit": 600, "offset_held": 600, "masked_fit": 180, "masked_held": 160,
          "multi": 24}
MM_DIGITS, ER_DIGITS = 6, 4  # sampled dimensions rounded (compact, exactly reproducible records)
_PAIR = ("one", "ln_w_over_b", "t_over_b", "ln_s_over_b", "t_over_s", "t_over_s*t_over_s",
         "t_over_b*t_over_s", "ln_er_near_over_far", "t_over_s*ln_w_over_b",
         "t_over_s*ln_s_over_b")
OFFSET_FEATURES = {"single": ("one", "ln_w_over_b", "t_over_b", "ln_er_near_over_far"),
                   "even": _PAIR, "odd": _PAIR}
MASK_FEATURES = {
    "k_even": ("one", "ln_mask_c1_over_h", "mask_gap_fill", "ln_s_over_w",
               "ln_mask_c3_over_c1", "ln_mask_c2_over_h", "er", "mask_er"),
    "d_even": ("t_over_s", "t_over_s*ln_s_over_w"),
    "k_odd": ("one", "mask_gap_fill", "ln_mask_c2_over_h", "ln_s_over_h", "mask_c3_over_s",
              "ln_mask_c3_over_c1", "ln_mask_c2_over_c1", "t_over_w", "er"),
    "d_odd": ("t_over_s*ln_w_over_h", "t_over_s*t_over_s")}
MASK_TARGET_SCALE = 1.07     # grid: bare closed form 7 % above the masked target (mask ~ -7 %)
GAP_FLOOR_MM = 0.11          # grid: smallest pair gap (t/S 0.32 at 1 oz)
WIDTH_START_MM, WIDTH_WALK = 3.0, 0.9
FD_DIGITS, OUT_DIGITS, COEF_DIGITS = 10, 12, 9
CHECK_TOL = 1e-8              # --check float tolerance (Windows vs Linux libm: 6.5e-12 rel)
FORMS = {
    fb.OFFSET: "Z_mode = Z_image_mode / (1 + e_mode), e_mode = sum_f c_f(a) x_f, c_f "
               "piecewise-linear in the asymmetry a over `knots` (every knot fitted); "
               "est_error_pct: single z0, pair z_diff",
    fb.MASKED: "Z_mode = Z_bare_mode r^k_mode e^d_mode, r = Z_coated / Z_bare of the "
               "single-ended Wan-Hoorfar microstrip of the same width, k = sum k_f x_f, "
               "d = sum d_f x_f; est_error_pct refers to z_diff",
    fb.MULTI_PLY: "identity: the closed forms' series (harmonic) mean Er of the plies",
    fb.CPW_FINITE: "identity: Ghione-Naldi CPW with infinite grounds",
    fb.NEAREST: "identity: the nearest closed form (outside published validity); est is the "
                "larger of this and the closed form's own"}


@dataclass(frozen=True)
class Spec:
    """One calibration point: name, cause, set (fit | held_out | measured), geometry, the
    published reference impedance when there is one."""

    name: str
    cause: str
    set: str
    geom: LineGeometry
    ref: float | None = None


# ---------------------------------------------------------------- geometry records
def geometry_to_dict(geom: LineGeometry) -> dict[str, Any]:
    """The fixture's compact record of a calibration geometry (plies as [thickness, er];
    None / default fields left out)."""
    out: dict[str, Any] = {"kind": geom.kind, "pair": geom.pair, "width": geom.width,
                           "t": geom.t, "below": [[p.thickness, p.er] for p in geom.below],
                           "above": [[p.thickness, p.er] for p in geom.above]}
    for key in ("pair_gap", "ground_gap", "ground_width"):
        if getattr(geom, key) is not None:
            out[key] = getattr(geom, key)
    m = geom.mask
    if m is not None:
        out["mask"] = {"er": m.er, "over_trace": m.over_trace,
                       "over_substrate": m.over_substrate, "between": m.between}
    if geom.etch:
        out["etch"] = geom.etch
    if geom.dielectric_copper:
        out["dielectric_copper"] = [list(x) for x in geom.dielectric_copper]
    return out


def geometry_from_dict(d: Mapping[str, Any]) -> LineGeometry:
    mask = d.get("mask")
    return LineGeometry(kind=d["kind"], pair=d["pair"], width=d["width"], t=d["t"],
                        below=tuple(Ply(*p) for p in d["below"]),
                        above=tuple(Ply(*p) for p in d["above"]), pair_gap=d.get("pair_gap"),
                        ground_gap=d.get("ground_gap"), ground_width=d.get("ground_width"),
                        mask=None if mask is None else MaskModel(**mask),
                        etch=d.get("etch", 0.0),
                        dielectric_copper=tuple(tuple(x) for x in d.get("dielectric_copper", ())))


# ---------------------------------------------------------------- random grids
def _logu(rng: random.Random, lo: float, hi: float) -> float:
    return math.exp(rng.uniform(math.log(lo), math.log(hi)))


def _mm(value: float) -> float:
    return round(value, MM_DIGITS)


def _ply(thickness: float, er: float) -> Ply:
    return Ply(_mm(thickness), round(er, ER_DIGITS))


def _accept(geom: LineGeometry, cause: str, box: Mapping[str, tuple[float, float]]) -> bool:
    """A sampled geometry the closed forms evaluate, of the cause, inside the declared box."""
    try:
        tline.evaluate(geom)
    except ValueError:
        return False
    if fb.cause_of(geom) != cause:
        return False
    values = fb.domain_values(geom)
    return all(box[k][0] <= v <= box[k][1] for k, v in values.items() if k in box)


def _offset_draw(rng: random.Random, pair: bool) -> LineGeometry | None:
    box = OFFSET_BOX
    a, wb, tb = rng.uniform(*box["a"]), _logu(rng, *box["w_over_b"]), _logu(rng, *box["t_over_b"])
    sb = _logu(rng, *box["s_over_b"])
    q = 1.0 if rng.random() < 0.5 else _logu(rng, *box["er_near_over_far"])
    er_near, split = rng.uniform(*ER_RANGE), rng.uniform(0.0, box["side_contrast"][1])
    two_ply, frac, t = rng.random() < 1 / 3, rng.uniform(0.3, 0.7), rng.choice((T_HALF, T_1OZ))
    b = t / tb
    if not PLANE_MM[0] <= b <= PLANE_MM[1]:
        t = T_1OZ if t == T_HALF else T_HALF
        b = t / tb
    if not PLANE_MM[0] <= b <= PLANE_MM[1]:
        return None
    h, er_far = b - t, er_near / q
    far = ((_ply(frac * (1 - a) * h, er_far), _ply((1 - frac) * (1 - a) * h, er_far * (1 + split)))
           if two_ply else (_ply((1 - a) * h, er_far),))
    return LineGeometry(kind="stripline", pair="edge" if pair else "single", width=_mm(wb * b),
                        t=t, below=far, above=(_ply(a * h, er_near),),
                        pair_gap=_mm(sb * b) if pair else None)


def _masked_draw(rng: random.Random) -> LineGeometry | None:
    box = MASK_BOX
    wh, sw, th = (_logu(rng, *box[k]) for k in ("w_over_h", "s_over_w", "t_over_h"))
    er, ratio = rng.uniform(*box["er"]), _logu(rng, 1 / (1 + box["contrast"][1]),
                                               1 + box["contrast"][1])
    two_ply, frac, mer = rng.random() < 0.5, rng.uniform(0.3, 0.7), rng.uniform(*box["mask_er"])
    c1h = _logu(rng, *box["mask_c1_over_h"])
    c2c1, c3c1 = rng.uniform(*box["mask_c2_over_c1"]), rng.uniform(*box["mask_c3_over_c1"])
    t = T_1OZ if T_1OZ / th <= H_MAX_MM else T_HALF
    h = t / th
    if h > H_MAX_MM:
        return None
    below = ((_ply(frac * h, er), _ply((1 - frac) * h, er * ratio)) if two_ply
             else (_ply(h, er),))
    mask = MaskModel(er=round(mer, ER_DIGITS), over_trace=_mm(c2c1 * c1h * h),
                     over_substrate=_mm(c1h * h), between=_mm(c3c1 * c1h * h))
    return LineGeometry(kind="microstrip", pair="edge", width=_mm(wh * h), t=t, below=below,
                        above=(), pair_gap=_mm(sw * wh * h), mask=mask)


def _multi_draw(rng: random.Random) -> LineGeometry | None:
    er1, er2 = rng.uniform(2.9, 4.8), rng.uniform(2.9, 4.8)
    h1, h2, w = _logu(rng, 0.05, 0.25), _logu(rng, 0.05, 0.25), _logu(rng, 0.1, 0.6)
    if max(er1, er2) / min(er1, er2) - 1 <= 0.12:
        return None
    return LineGeometry(kind="microstrip", pair="single", width=_mm(w), t=T_1OZ,
                        below=(_ply(h1, er1), _ply(h2, er2)), above=())


def random_specs(key: str, cause: str, part: str, prefix: str,
                 draw: Callable[[random.Random, int], LineGeometry | None],
                 box: Mapping[str, tuple[float, float]]) -> list[Spec]:
    """COUNTS[key] seeded uniform draws accepted by _accept (rejection keeps them uniform)."""
    rng, out = random.Random(SEEDS[key]), []
    while len(out) < COUNTS[key]:
        geom = draw(rng, len(out))
        if geom is not None and _accept(geom, cause, box):
            out.append(Spec(f"{prefix}_rand_{key}_{len(out):03d}", cause, part, geom))
    return out


# ---------------------------------------------------------------- named grids
def _cms(w: float, s: float, below: tuple[Ply, ...], t: float = T_1OZ,
         mask: MaskModel | None = JLC_MASK) -> LineGeometry:
    return LineGeometry(kind="microstrip", pair="edge", width=w, t=t, below=below, above=(),
                        pair_gap=s, mask=mask)


def masked_width(target: float, ratio: float, below: tuple[Ply, ...], t: float) -> float:
    """Width (S = ratio W) whose bare closed form reads MASK_TARGET_SCALE x target, on the
    wide branch of Zdiff(W); the gap floor when the target is out of reach above it."""
    def zd(w: float) -> float:
        return tline.evaluate(_cms(w, ratio * w, below, t, None)).z_diff

    goal, lo, w = target * MASK_TARGET_SCALE, GAP_FLOOR_MM / ratio, WIDTH_START_MM
    while w * WIDTH_WALK >= lo and zd(w * WIDTH_WALK) < goal:
        w *= WIDTH_WALK
    if w * WIDTH_WALK < lo:
        return lo
    return solve_monotone(lambda x: -zd(x), -goal, w * WIDTH_WALK, w)


def _masked_cell(name: str, part: str, ratio: float, target: float, below: tuple[Ply, ...],
                 t: float = T_1OZ, mask: MaskModel = JLC_MASK) -> Spec:
    w = masked_width(target, ratio, below, t)
    return Spec(name, fb.MASKED, part, _cms(w, ratio * w, below, t, mask))


def masked_specs() -> list[Spec]:
    out = []
    for h, er in ((0.0994, 4.1), (0.2104, 4.4), (0.508, 3.66)):
        for part, ratio, target in (("fit", 0.5, 90.0), ("fit", 1.0, 90.0), ("fit", 2.0, 90.0),
                                    ("held_out", 0.75, 100.0), ("held_out", 1.5, 100.0),
                                    ("held_out", 2.5, 90.0)):
            out.append(_masked_cell(f"MSK_grid_{part}_h{h}_sw{ratio:g}_z{target:g}", part,
                                    ratio, target, (Ply(h, er),)))
    b7628, held = (Ply(0.2104, 4.4),), "held_out"
    thick = MaskModel(er=3.8, over_trace=0.025, over_substrate=0.04, between=0.04)
    thin = MaskModel(er=3.5, over_trace=0.01, over_substrate=0.02, between=0.02)
    jlc6_outer = (Ply(0.127, 4.16), Ply(0.0964, 4.31))
    return [*out,
            Spec("USB90_7628_mask", fb.MASKED, held, _cms(0.25898442342877387, 0.15, b7628),
                 86.80),
            Spec("DMS90_7628_mask", fb.MASKED, held, _cms(0.23492859579490222, 0.15, b7628),
                 90.0),
            _masked_cell("MSK_var_half_oz", held, 1.0, 90.0, b7628, T_HALF),
            _masked_cell("MSK_var_2oz", held, 1.0, 90.0, b7628, 0.07),
            _masked_cell("MSK_var_thick_mask", held, 1.0, 90.0, b7628, mask=thick),
            _masked_cell("MSK_var_thin_mask", held, 1.0, 90.0, b7628, mask=thin),
            _masked_cell("MSK_var_dk3.0_h0.127", held, 0.75, 100.0, (Ply(0.127, 3.0),)),
            _masked_cell("MSK_var_dk4.6_h0.3", held, 1.5, 90.0, (Ply(0.3, 4.6),)),
            _masked_cell("MSK_var_ro4350b_h0.254", held, 1.0, 90.0, (Ply(0.254, 3.66),)),
            _masked_cell("MSK_var_jlc6_outer_sw1", held, 1.0, 90.0, jlc6_outer),
            _masked_cell("MSK_var_jlc6_outer_sw1.5", held, 1.5, 100.0, jlc6_outer)]


def _osl(w: float, a: float, b0: float, er: float, t: float, s: float | None = None
         ) -> LineGeometry:
    """Offset stripline: the near reference above at a (b0 - t), the far one below."""
    h = b0 - t
    return LineGeometry(kind="stripline", pair="single" if s is None else "edge", width=w, t=t,
                        below=(Ply((1 - a) * h, er),), above=(Ply(a * h, er),), pair_gap=s)


def offset_specs() -> list[Spec]:
    def off(w: float, s: float | None = None) -> LineGeometry:      # D7.14 SL50_off / DSL100_off
        return LineGeometry(kind="stripline", pair="single" if s is None else "edge", width=w,
                            t=T_HALF, below=(Ply(0.55, 4.6),), above=(Ply(0.1088, 4.16),),
                            pair_gap=s)

    def in2(w: float, s: float | None = None) -> LineGeometry:      # JLC06161H-2116 In2
        adj = Ply(0.2084, 4.4)
        return LineGeometry(kind="dual_stripline", pair="single" if s is None else "edge",
                            width=w, t=T_HALF, pair_gap=s, above=(Ply(0.3, 4.41),),
                            below=(adj, adj, dielectric_copper_ply(T_HALF, adj), Ply(0.3, 4.41)),
                            dielectric_copper=(("In3.Cu", 4.4),))

    grid = [Spec(f"OSL_grid_a{a:g}_w{w:g}" + ("" if s is None else f"_s{s:g}"), fb.OFFSET, "fit",
                 _osl(w, a, 0.6, 4.4, T_HALF, s))
            for a in (0.15, 0.2, 0.25, 0.3, 0.35)
            for w, s in ((0.1, None), (0.2, None), (0.3, None), (0.1, 0.15), (0.15, 0.2))]
    held = "held_out"
    named = [Spec("SL50_off@W_FD", fb.OFFSET, held, off(0.13474641701088028), 50.0),
             Spec("SL50_off@W_cf", fb.OFFSET, held, off(0.15364761993288997)),
             Spec("DSL100_off@W_FD", fb.OFFSET, held, off(0.08943079170736003, 0.15), 100.0),
             Spec("DSL100_off@W_cf", fb.OFFSET, held, off(0.1003147764503956, 0.15))]
    named += [Spec(f"JLC6_2116_In2_dual@{w:g}", fb.OFFSET, held, in2(w)) for w in (0.1, 0.15, 0.25)]
    named += [Spec(f"JLC6_2116_In2_dual_pair@{w:g}", fb.OFFSET, held, in2(w, 0.15))
              for w in (0.1, 0.15)]
    return [*grid, *named]


def measured_specs() -> list[Spec]:
    def ms(w: float, below: tuple[Ply, ...], mask: MaskModel | None = None) -> LineGeometry:
        return LineGeometry(kind="microstrip", pair="single", width=w, t=T_1OZ, below=below,
                            above=(), mask=mask)

    out = [Spec(f"MULTI_ms_w{w:g}_{lo:g}_{hi:g}", fb.MULTI_PLY, "measured",
                ms(w, (Ply(0.1, lo), Ply(0.1, hi)))) for w in (0.2, 0.4)
           for lo, hi in ((3.0, 4.6), (4.6, 3.0))]
    out.append(Spec("MULTI_ms_ro4450f_370hr_mask", fb.MULTI_PLY, "measured",
                    ms(0.5, (Ply(0.101, 3.52), Ply(0.12, 4.2)), JLC_MASK)))
    out += [Spec(f"MULTI_sl_centred_w{w:g}", fb.MULTI_PLY, "measured", LineGeometry(
        kind="stripline", pair="single", width=w, t=T_HALF, below=(Ply(0.25, 4.4),),
        above=(Ply(0.25, 3.66),))) for w in (0.1, 0.2)]
    for h, er, w, s, gw in ((0.2104, 4.4, 0.3, 0.15, 0.5), (0.2104, 4.4, 0.3, 0.15, 1.0),
                            (0.508, 3.66, 0.6, 0.2, 0.5), (0.508, 3.66, 0.6, 0.2, 1.0),
                            (1.51, 4.5, 1.0, 0.2, 1.0), (1.51, 4.5, 1.0, 0.2, 2.0)):
        out.append(Spec(f"CPW_h{h:g}_w{w:g}_s{s:g}_gw{gw:g}", fb.CPW_FINITE, "measured",
                        LineGeometry(kind="cpw", pair="single", width=w, t=T_1OZ,
                                     below=(Ply(h, er),), above=(), ground_gap=s,
                                     ground_width=gw)))
    return out


def all_specs() -> list[Spec]:
    o, m = fb.OFFSET, fb.MASKED
    specs = [*masked_specs(),
             *random_specs("masked_fit", m, "fit", "MSK", lambda r, i: _masked_draw(r), MASK_BOX),
             *random_specs("masked_held", m, "held_out", "MSK", lambda r, i: _masked_draw(r),
                           MASK_BOX),
             *offset_specs(),
             *random_specs("offset_fit", o, "fit", "OSL", lambda r, i: _offset_draw(r, i % 2 == 1),
                           OFFSET_BOX),
             *random_specs("offset_held", o, "held_out", "OSL",
                           lambda r, i: _offset_draw(r, i % 2 == 1), OFFSET_BOX),
             *measured_specs(),
             *random_specs("multi", fb.MULTI_PLY, "measured", "MULTI", lambda r, i: _multi_draw(r),
                           {})]
    for spec in specs:
        if fb.cause_of(spec.geom) != spec.cause:
            raise ValueError(f"{spec.name}: geometry has cause {fb.cause_of(spec.geom)!r}, "
                             f"the grid says {spec.cause!r}")
    if len({s.name for s in specs}) != len(specs):
        raise ValueError("calibration point names are not unique")
    return specs


# ---------------------------------------------------------------- FD runs
def _sig(value: float, digits: int = FD_DIGITS) -> float:
    return float(f"{value:.{digits}g}")


def _solve(job: tuple[LineGeometry, str]) -> tuple[dict[str, float], float]:
    import fieldsolve2d as fd

    res = fd.solve(job[0], grid=job[1])
    keys = ("z0",) if res.z_diff is None else ("z_even", "z_odd")
    return {k: _sig(getattr(res, k)) for k in keys}, res.seconds


def run_fd(specs: Sequence[Spec], grid: str = GRID, jobs: int = 1) -> dict[str, Any]:
    """Solve every point with fieldsolve2d (jobs worker processes); the record {fd, points}."""
    import fieldsolve2d as fd

    backend = fd.available()
    if backend == "none":
        raise SystemExit("tline_calibrate: no FD backend; pip install -r server/requirements.txt")
    start, work = time.perf_counter(), [(s.geom, grid) for s in specs]
    with ProcessPoolExecutor(max(1, jobs)) as pool:
        solved = list(pool.map(_solve, work, chunksize=4))
    points = [{"name": s.name, "cause": s.cause, "set": s.set, "ref": s.ref,
               "geometry": geometry_to_dict(s.geom), "fd": values}
              for s, (values, _) in zip(specs, solved, strict=True)]
    print(f"FD: {len(specs)} points, {sum(sec for _, sec in solved):.1f} s solver time, "
          f"{time.perf_counter() - start:.1f} s wall ({backend}, {grid}, {jobs} jobs)")
    return {"fd": {"solver": "fieldsolve2d", "version": fd.FD_VERSION, "grid": grid},
            "points": points}


def fd_points_from_fixture(doc: Mapping[str, Any]) -> dict[str, Any]:
    """The calibration record (FD inputs only) of a committed fixture."""
    keys = ("name", "cause", "set", "ref", "geometry", "fd")
    return {"fd": dict(doc["fd"]), "points": [{k: p[k] for k in keys} for p in doc["points"]]}


# ---------------------------------------------------------------- fits
def _hat(a: float) -> list[float]:
    """Piecewise-linear basis over KNOTS (one function per knot)."""
    out, a = [0.0] * len(KNOTS), min(max(a, KNOTS[0]), KNOTS[-1])
    for i, (a0, a1) in enumerate(pairwise(KNOTS)):
        if a0 <= a <= a1:
            f = (a - a0) / (a1 - a0)
            out[i], out[i + 1] = 1.0 - f, f
            break
    return out


def _round(value: float) -> float:
    return float(f"{value:.{COEF_DIGITS}g}")


def _modes(point: Mapping[str, Any], closed: LineResult) -> dict[str, tuple[float, float]]:
    """(closed form, FD) impedance per mode of a point."""
    fd = point["fd"]
    if "z0" in fd:
        return {"single": (closed.z0, fd["z0"])}
    return {"even": (closed.z_even, fd["z_even"]), "odd": (closed.z_odd, fd["z_odd"])}


def _lstsq(rows: Sequence[Sequence[float]], ys: Sequence[float]) -> list[float]:
    import numpy as np

    return [_round(float(v)) for v in np.linalg.lstsq(np.array(rows), np.array(ys),
                                                       rcond=None)[0]]


def fit_offset(fit: Sequence[tuple[Mapping[str, Any], LineGeometry, LineResult]]
               ) -> dict[str, dict[str, list[float]]]:
    """Per mode: e = Z_image / Z_FD - 1 regressed on hat(a) x feature."""
    rows: dict[str, list] = {m: [] for m in fb.OFFSET_MODES}
    ys: dict[str, list] = {m: [] for m in fb.OFFSET_MODES}
    for p, g, c in fit:
        values = fb.domain_values(g)
        for mode, (z, z_fd) in _modes(p, c).items():
            rows[mode].append([h * fb.feature_value(f, values) for f in OFFSET_FEATURES[mode]
                               for h in _hat(values["a"])])
            ys[mode].append(z / z_fd - 1.0)
    n, terms = len(KNOTS), {}
    for mode, feats in OFFSET_FEATURES.items():
        coef = _lstsq(rows[mode], ys[mode])
        terms[mode] = {f: coef[i * n:(i + 1) * n] for i, f in enumerate(feats)}
    return terms


def fit_masked(fit: Sequence[tuple[Mapping[str, Any], LineGeometry, LineResult]]
               ) -> dict[str, dict[str, float]]:
    """Per mode: ln(Z_FD / Z_bare) regressed on ln(r) x k-features + d-features."""
    out = {}
    for mode in ("even", "odd"):
        k_f, d_f = MASK_FEATURES[f"k_{mode}"], MASK_FEATURES[f"d_{mode}"]
        rows, ys = [], []
        for p, g, c in fit:
            x, lr = fb.domain_values(g), math.log(fb.mask_ratio(g))
            rows.append([lr * fb.feature_value(f, x) for f in k_f]
                        + [fb.feature_value(f, x) for f in d_f])
            ys.append(math.log(p["fd"][f"z_{mode}"] / getattr(c, f"z_{mode}")))
        coef = _lstsq(rows, ys)
        out[f"k_{mode}"] = dict(zip(k_f, coef[:len(k_f)], strict=True))
        out[f"d_{mode}"] = dict(zip(d_f, coef[len(k_f):], strict=True))
    return out


# ---------------------------------------------------------------- errors and estimates
def ceil_tenth(value: float) -> float:
    """The next 0.1 % strictly above value."""
    return math.floor(value * 10.0 + 1.0) / 10.0


def _est(worst: float) -> float:
    return ceil_tenth(EST_MARGIN * worst)


def _zq(res: Any) -> float:
    return res.z0 if res.z_diff is None else res.z_diff


def _errors(points: Sequence[Mapping[str, Any]], results: Sequence[LineResult],
            geoms: Sequence[LineGeometry], table: fb.FallbackTable) -> list[dict]:
    """Per point: error (%) of the est quantity (z0 / z_diff) and of z_even vs FD, and whether
    it lies inside its cause's calibrated domain."""
    out = []
    for p, r, g in zip(points, results, geoms, strict=True):
        fd = p["fd"]
        ref = fd["z0"] if "z0" in fd else 2.0 * fd["z_odd"]
        even = None if "z0" in fd else 100.0 * (r.z_even / fd["z_even"] - 1.0)
        out.append({"z": 100.0 * (_zq(r) / ref - 1.0), "z_even": even,
                    "inside": not fb.outside_notes(g, table=table)})
    return out


def _stats(errs: Sequence[float]) -> dict[str, float]:
    return {"worst_pct": round(max(abs(e) for e in errs), 4),
            "rms_pct": round(math.sqrt(sum(e * e for e in errs) / len(errs)), 4)}


def _part_stats(sel: Sequence[tuple[Mapping[str, Any], dict]], part: str,
                keys: Mapping[str, Any]) -> dict[str, Any]:
    chosen = [(p, e) for p, e in sel if p["set"] == part]
    out: dict[str, Any] = {"points": len(chosen), "worst_pct": {}, "rms_pct": {}}
    for label, pick in keys.items():
        errs = [pick(p, e) for p, e in chosen if pick(p, e) is not None]
        if errs:
            stats = _stats(errs)
            out["worst_pct"][label], out["rms_pct"][label] = stats["worst_pct"], stats["rms_pct"]
    return out


def _domain(box: Mapping[str, tuple[float, float]], coords: Sequence[str],
            sampled: Sequence[Mapping[str, float]]) -> dict[str, list[float]]:
    """The declared box, plus the sampled range of each derived coordinate."""
    return {c: list(box[c]) if c in box else [min(v[c] for v in sampled),
                                              max(v[c] for v in sampled)] for c in coords}


# ---------------------------------------------------------------- documents
def _offset_entry(sel: Sequence[tuple[Mapping[str, Any], dict, LineGeometry]],
                  terms: Mapping[str, Any]) -> dict[str, Any]:
    inner = [(p, e) for p, e, _ in sel if e["inside"]]
    single = [abs(e["z"]) for p, e in inner if "z0" in p["fd"]]
    pair = [(abs(e["z"]), abs(e["z_even"])) for p, e in inner if "z0" not in p["fd"]]
    keys = {"single": lambda p, e: e["z"] if "z0" in p["fd"] else None,
            "pair_z_diff": lambda p, e: None if "z0" in p["fd"] else e["z"],
            "pair_z_even": lambda p, e: e["z_even"]}
    return {"form": FORMS[fb.OFFSET], "features": {m: list(f) for m, f in OFFSET_FEATURES.items()},
            "knots": list(KNOTS), "terms": terms, "est_margin": EST_MARGIN,
            "est_error_pct": {"single": _est(max(single)), "pair": _est(max(x[0] for x in pair))},
            "z_even_error_pct": _est(max(x[1] for x in pair)),
            "domain": _domain(OFFSET_BOX, fb.OFFSET_COORDS, []),
            "fit": _part_stats(inner, "fit", keys),
            "held_out": _part_stats(inner, "held_out", keys)}


def _masked_entry(sel: Sequence[tuple[Mapping[str, Any], dict, LineGeometry]],
                  coef: Mapping[str, Any]) -> dict[str, Any]:
    keys = {"z_diff": lambda p, e: e["z"], "z_even": lambda p, e: e["z_even"]}
    inner = [(p, e) for p, e, _ in sel if e["inside"]]
    sampled = [fb.domain_values(g) for p, _, g in sel if "_rand_" in p["name"]]
    return {"form": FORMS[fb.MASKED], **coef, "est_margin": EST_MARGIN,
            "est_error_pct": _est(max(abs(e["z"]) for _, e in inner)),
            "z_even_error_pct": _est(max(abs(e["z_even"]) for _, e in inner)),
            "domain": _domain(MASK_BOX, fb.MASK_COORDS, sampled),
            "fit": _part_stats(inner, "fit", keys),
            "held_out": _part_stats(inner, "held_out", keys)}


def _identity_entry(cause: str, errs: Sequence[float]) -> dict[str, Any]:
    worst = max((abs(e) for e in errs), default=None)
    est = PLACEHOLDER_PCT[cause] if worst is None else max(PLACEHOLDER_PCT[cause], _est(worst))
    return {"form": FORMS[cause], "est_error_pct": est, "est_margin": EST_MARGIN,
            "placeholder_pct": PLACEHOLDER_PCT[cause],
            "measured": None if worst is None else {"points": len(errs), "worst_pct": worst}}


def calibration_digest(points: Sequence[Mapping[str, Any]]) -> str:
    """sha256 of the canonical JSON of the FD inputs (name, cause, set, geometry, fd)."""
    canonical = json.dumps([{k: p[k] for k in ("name", "cause", "set", "geometry", "fd")}
                            for p in points], sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _data_doc(cal: Mapping[str, Any], geoms: Sequence[LineGeometry], errs: Sequence[dict],
              terms: Mapping[str, Any], coef: Mapping[str, Any]) -> dict[str, Any]:
    points = cal["points"]

    def sel(cause: str) -> list:
        return [(p, e, g) for p, e, g in zip(points, errs, geoms, strict=True)
                if p["cause"] == cause]

    causes = {fb.OFFSET: _offset_entry(sel(fb.OFFSET), terms),
              fb.MASKED: _masked_entry(sel(fb.MASKED), coef)}
    for cause in fb.IDENTITY_CAUSES:
        causes[cause] = _identity_entry(cause, [e["z"] for _, e, _ in sel(cause)])
    return {"schema": fb.SCHEMA, "generated_by": GENERATED_BY,
            "adr": "ADR D7.6 'Class C fallback models' (critique M11), task D7-05c",
            "fd": dict(cal["fd"]),
            "calibration": {"fixture": FIXTURE_REL, "points": len(points),
                            "digest": calibration_digest(points)},
            "causes": causes}


def _fixture_point(p: Mapping[str, Any], c: LineResult, f: LineResult,
                   e: Mapping[str, Any]) -> dict[str, Any]:
    out = {**p, "closed_form": {"class": c.accuracy_class, "z": _sig(_zq(c), OUT_DIGITS),
                                "est_error_pct": _sig(c.est_error_pct, OUT_DIGITS)},
           "fallback": {"z": _sig(_zq(f), OUT_DIGITS), "est_error_pct": f.est_error_pct},
           "error_pct": _sig(e["z"], OUT_DIGITS)}
    if e["z_even"] is not None:
        out["fallback"]["z_even"] = _sig(f.z_even, OUT_DIGITS)
        out["z_even_error_pct"] = _sig(e["z_even"], OUT_DIGITS)
    return out


def _draft_table(cal: Mapping[str, Any], geoms: Sequence[LineGeometry], terms: Mapping[str, Any],
                 coef: Mapping[str, Any]) -> fb.FallbackTable:
    """The fitted forms with placeholder estimates (to measure the errors the estimates are
    derived from); every point counts as inside here."""
    errs = [{"z": 1.0, "z_even": 1.0, "inside": True}] * len(geoms)
    return fb.parse_table(_data_doc(cal, geoms, errs, terms, coef), source="draft")


def build_outputs(cal: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """(data document, fixture document) from a calibration record {fd, points}."""
    points = cal["points"]
    geoms = [geometry_from_dict(p["geometry"]) for p in points]
    closed = [tline.evaluate(g) for g in geoms]
    fit = [(p, g, c) for p, g, c in zip(points, geoms, closed, strict=True) if p["set"] == "fit"]
    terms = fit_offset([x for x in fit if x[0]["cause"] == fb.OFFSET])
    coef = fit_masked([x for x in fit if x[0]["cause"] == fb.MASKED])
    draft = _draft_table(cal, geoms, terms, coef)
    errs = _errors(points, [fb.correct(g, c, table=draft) for g, c in zip(geoms, closed,
                                                                           strict=True)],
                   geoms, draft)
    for p, e in zip(points, errs, strict=True):
        if p["set"] == "fit" and not e["inside"]:
            raise ValueError(f"{p['name']}: a fit point outside the calibrated domain")
    data = _data_doc(cal, geoms, errs, terms, coef)
    table = fb.parse_table(data, source="calibration")
    final = [fb.correct(g, c, table=table) for g, c in zip(geoms, closed, strict=True)]
    for p, f, e in zip(points, final, errs, strict=True):
        if abs(e["z"]) > f.est_error_pct:
            raise ValueError(f"{p['name']}: error {e['z']:.3f} % above est {f.est_error_pct} %")
    fixture = {"schema": FIXTURE_SCHEMA, "generated_by": GENERATED_BY, "fd": dict(cal["fd"]),
               "points": [_fixture_point(p, c, f, e)
                          for p, c, f, e in zip(points, closed, final, errs, strict=True)]}
    return data, fixture


# ---------------------------------------------------------------- output and CLI
def dumps(doc: Mapping[str, Any]) -> str:
    """Stable JSON text: indented, except one line per calibration point."""
    if "points" not in doc:
        return json.dumps(doc, indent=1, ensure_ascii=False) + "\n"
    head = json.dumps({k: v for k, v in doc.items() if k != "points"}, indent=1,
                      ensure_ascii=False)[:-2]
    lines = ",\n  ".join(json.dumps(p, ensure_ascii=False) for p in doc["points"])
    return f'{head},\n "points": [\n  {lines}\n ]\n}}\n'


def same_doc(a: Any, b: Any, tol: float = CHECK_TOL) -> bool:
    """Equal JSON values, floats within tol (relative or absolute); bools and None exact."""
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    if isinstance(a, float) or isinstance(b, float):
        return (isinstance(a, (int, float)) and isinstance(b, (int, float))
                and math.isclose(a, b, rel_tol=tol, abs_tol=tol))
    if isinstance(a, Mapping) and isinstance(b, Mapping):
        return a.keys() == b.keys() and all(same_doc(a[k], b[k], tol) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(same_doc(x, y, tol) for x, y in zip(a, b, strict=True))
    return a == b


def check(data: Mapping[str, Any], fixture: Mapping[str, Any], data_path: Path,
          fixture_path: Path) -> bool:
    """True when the files on disk hold these outputs (bytes, else floats within CHECK_TOL)."""
    pairs = [(dumps(doc), path.read_text(encoding="utf-8"))
             for doc, path in ((data, data_path), (fixture, fixture_path))]
    if all(new == old for new, old in pairs):
        print("check: up to date (byte-identical)")
        return True
    if all(same_doc(json.loads(new), json.loads(old)) for new, old in pairs):
        print(f"check: up to date (floats within {CHECK_TOL:g}; platform libm last bits)")
        return True
    print("check: DIFFERS from the committed files")
    return False


def report(data: Mapping[str, Any]) -> str:
    out = []
    for cause, entry in data["causes"].items():
        out.append(f"{cause}: est_error_pct {entry['est_error_pct']}"
                   + (f", z_even {entry['z_even_error_pct']}" if "z_even_error_pct" in entry
                      else ""))
        for part in ("fit", "held_out"):
            if part in entry:
                out.append(f"  {part}: {entry[part]['points']} points, worst "
                           f"{entry[part]['worst_pct']}, rms {entry[part]['rms_pct']}")
        if entry.get("measured"):
            out.append(f"  measured: {entry['measured']}")
    return "\n".join(out)


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--refit", action="store_true", help="refit the committed FD data (no FD)")
    ap.add_argument("--check", action="store_true", help="refit and compare; write nothing")
    ap.add_argument("--grid", default=GRID, choices=("medium", "fine"))
    ap.add_argument("--jobs", type=int, default=1, help="FD worker processes")
    ap.add_argument("--data", type=Path, default=DATA_OUT)
    ap.add_argument("--fixture", type=Path, default=FIXTURE_OUT)
    ap.add_argument("--fd-source", type=Path, default=None,
                    help="fixture whose FD values --refit/--check reuse (default: --fixture)")
    ap.add_argument("--save-fd", type=Path, default=None,
                    help="also write the raw FD record (an --fd-source) before fitting")
    args = ap.parse_args(argv)
    start = time.perf_counter()
    if args.refit or args.check:
        source = args.fd_source or args.fixture
        cal = fd_points_from_fixture(json.loads(source.read_text(encoding="utf-8")))
    else:
        cal = run_fd(all_specs(), args.grid, args.jobs)
        if args.save_fd:
            args.save_fd.write_text(json.dumps(cal), encoding="utf-8", newline="\n")
    data, fixture = build_outputs(cal)
    print(report(data))
    if args.check:
        return 0 if check(data, fixture, args.data, args.fixture) else 1
    for path, doc in ((args.data, data), (args.fixture, fixture)):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(dumps(doc), encoding="utf-8", newline="\n")
        print(f"wrote {path}")
    print(f"total {time.perf_counter() - start:.1f} s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
