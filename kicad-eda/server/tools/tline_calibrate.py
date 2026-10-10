"""Calibrate the class C fallback models of tline/fallback.py against the FD tier (D7-05c).

Developer CLI, not run in CI: FD solves need numpy + scipy (fieldsolve2d), the fits numpy.
    .venv/Scripts/python.exe server/tools/tline_calibrate.py            # FD runs + fit (~5 min)
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
Grids (ADR D7.6 / D7.14; FD medium grid, mm, ohm):
- masked edge-coupled microstrip, JLC mask (Er 3.8, c1 0.0305, c2 0.0152, c3 0.0305), 1 oz:
  fit = h 0.0994 / 0.2104 / 0.508 (Dk 4.1 / 4.4 / 3.66) x S/W 0.5 / 1 / 2 near Zdiff 90 (the
  width puts the bare closed form 7 % above the target, on the wide branch, gap >= 0.1 mm; S/W
  0.5 cannot reach 90 ohm above that gap: the floor is used); held out = S/W 0.75 / 1.5 near 100,
  S/W 3 near 90, the [R-imp] USB pair (W 0.259, S 0.15: FD 86.80), DMS90_7628_mask, other
  copper, masks, Dk and the JLC06161H-2116 two-ply outer stack;
- offset / dual stripline: fit = asymmetry 0.1-0.45 on b 0.6 Dk 4.4 0.5 oz and b 0.4 Dk 4.4
  1 oz (singles over W, pairs over W x S); held out = b 0.8 Dk 3.66 1 oz and b 0.3 Dk 4.2 at
  off-knot asymmetries, RO4350B / FR-4 contrast builds, SL50_off, DSL100_off and the
  JLC06161H-2116 In2 dual stripline (singles and pairs);
- measured identity causes: two-ply microstrip / centred stripline with Er contrast, CPW
  without a back plane with finite grounds.
Fits (numpy least squares): offset, per mode, e = Z_image / Z_FD - 1 on hat_k(a) x feature
(linear, 0 at a = 0.5); masked, per mode, ln(Z_FD / Z_bare) on ln(r) x feature (linear).
est_error_pct = the worst |error| over fit + held-out points (offset: per asymmetry interval,
single z0 / pair z_diff), rounded up to the next 0.1 %; identity causes: the larger of the ADR
placeholder and the measured worst rounded up.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
import time
from collections.abc import Mapping, Sequence
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
FIXTURE_SCHEMA = "kicad-eda/tline-fallback-calibration@1"
GENERATED_BY = "server/tools/tline_calibrate.py"
GRID = "medium"
T_1OZ, T_HALF = 0.035, 0.0152
JLC_MASK = MaskModel(er=3.8, over_trace=0.0152, over_substrate=0.0305, between=0.0305)
KNOTS = (0.1, 0.15, 0.2, 0.25, 0.3, 0.35, 0.4, 0.45, 0.5)
MASK_K = {"k_even": ("one", "ln_s_over_h"), "k_odd": ("one", "ln_w_over_h", "mask_gap_fill")}
PLACEHOLDER_PCT = {fb.MULTI_PLY: 3.0, fb.CPW_FINITE: 5.0, fb.NEAREST: 5.0}   # ADR D7.6 table
MASK_TARGET_SCALE = 1.07     # bare closed form 7 % above the masked target (mask ~ -7 %)
GAP_FLOOR_MM = 0.1           # masked grid: smallest pair gap
WIDTH_START_MM, WIDTH_WALK = 3.0, 0.9
FD_DIGITS = 10               # significant digits kept of each FD impedance
COEF_DIGITS = 9
CHECK_TOL = 1e-8              # --check float tolerance (Windows vs Linux libm: 6.5e-12 rel)
FORMS = {
    fb.OFFSET: "Z_mode = Z_image_mode / (1 + e_mode), e_mode = sum_f c_f(a) x_f, x = 1, "
               "ln(W/b), t/b (+ ln(S/b) for pairs), c_f piecewise-linear in the asymmetry a "
               "over `knots` (0 at a = 0.5); est_error_pct per knot interval (single z0, pair "
               "z_diff)",
    fb.MASKED: "Z_even = Z_even_bare r^k_even, Z_odd = Z_odd_bare r^k_odd, r = Z_coated / Z_bare "
               "of the single-ended Wan-Hoorfar microstrip of the same width, k = sum_f k_f x_f, "
               "x = 1, ln(W/h), ln(S/h), (c3 + t)/S; est_error_pct refers to z_diff",
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
    """The fixture's record of a calibration geometry (plies as [thickness, er])."""
    m = geom.mask
    return {"kind": geom.kind, "pair": geom.pair, "width": geom.width, "t": geom.t,
            "below": [[p.thickness, p.er] for p in geom.below],
            "above": [[p.thickness, p.er] for p in geom.above], "pair_gap": geom.pair_gap,
            "ground_gap": geom.ground_gap, "ground_width": geom.ground_width,
            "mask": None if m is None else {"er": m.er, "over_trace": m.over_trace,
                                            "over_substrate": m.over_substrate,
                                            "between": m.between},
            "etch": geom.etch, "dielectric_copper": [list(x) for x in geom.dielectric_copper]}


def geometry_from_dict(d: Mapping[str, Any]) -> LineGeometry:
    mask = d.get("mask")
    return LineGeometry(kind=d["kind"], pair=d["pair"], width=d["width"], t=d["t"],
                        below=tuple(Ply(*p) for p in d["below"]),
                        above=tuple(Ply(*p) for p in d["above"]), pair_gap=d.get("pair_gap"),
                        ground_gap=d.get("ground_gap"), ground_width=d.get("ground_width"),
                        mask=None if mask is None else MaskModel(**mask),
                        etch=d.get("etch", 0.0),
                        dielectric_copper=tuple(tuple(x) for x in d.get("dielectric_copper", ())))


# ---------------------------------------------------------------- grids
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
        below = (Ply(h, er),)
        for part, ratio, target in (("fit", 0.5, 90.0), ("fit", 1.0, 90.0), ("fit", 2.0, 90.0),
                                    ("held_out", 0.75, 100.0), ("held_out", 1.5, 100.0),
                                    ("held_out", 3.0, 90.0)):
            out.append(_masked_cell(f"MSK_{part}_h{h}_sw{ratio:g}_z{target:g}", part, ratio,
                                    target, below))
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


def _osl(w: float, a: float, b0: float, er_near: float, t: float, s: float | None = None,
         er_far: float | None = None) -> LineGeometry:
    """Offset stripline: the near reference above at a (b0 - t), the far one below."""
    h = b0 - t
    return LineGeometry(kind="stripline", pair="single" if s is None else "edge", width=w, t=t,
                        below=(Ply((1 - a) * h, er_far or er_near),), above=(Ply(a * h, er_near),),
                        pair_gap=s)


def _offset_build(tag: str, part: str, asym: Sequence[float], b0: float, er: float, t: float,
                  widths: Sequence[float], pairs: Sequence[tuple[float, float]]) -> list[Spec]:
    out = []
    for a in asym:
        out += [Spec(f"OSL_{tag}_a{a:g}_w{w:g}", fb.OFFSET, part, _osl(w, a, b0, er, t))
                for w in widths]
        out += [Spec(f"OSL_{tag}_pair_a{a:g}_w{w:g}_s{s:g}", fb.OFFSET, part,
                     _osl(w, a, b0, er, t, s)) for w, s in pairs]
    return out


def _named_offset_specs() -> list[Spec]:
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

    held = "held_out"
    out = [Spec("SL50_off@W_FD", fb.OFFSET, held, off(0.13474641701088028), 50.0),
           Spec("SL50_off@W_cf", fb.OFFSET, held, off(0.15364761993288997)),
           Spec("DSL100_off@W_FD", fb.OFFSET, held, off(0.08943079170736003, 0.15), 100.0),
           Spec("DSL100_off@W_cf", fb.OFFSET, held, off(0.1003147764503956, 0.15))]
    out += [Spec(f"JLC6_2116_In2_dual@{w:g}", fb.OFFSET, held, in2(w)) for w in (0.1, 0.15, 0.25)]
    out += [Spec(f"JLC6_2116_In2_dual_pair@{w:g}", fb.OFFSET, held, in2(w, 0.15))
            for w in (0.1, 0.15)]
    for a in (0.2, 0.3):
        for w in (0.1, 0.2):
            out += [Spec(f"OSL_ro_fr_a{a:g}_w{w:g}", fb.OFFSET, held,
                         _osl(w, a, 0.6, 3.66, T_HALF, er_far=4.6)),
                    Spec(f"OSL_fr_ro_a{a:g}_w{w:g}", fb.OFFSET, held,
                         _osl(w, a, 0.6, 4.6, T_HALF, er_far=3.66)),
                    Spec(f"OSL_ro_fr_pair_a{a:g}_w{w:g}", fb.OFFSET, held,
                         _osl(w, a, 0.6, 3.66, T_HALF, 0.15, er_far=4.6))]
    return out


def offset_specs() -> list[Spec]:
    fit_a, off_knot = KNOTS[:-1], (0.125, 0.175, 0.275, 0.325, 0.425)
    return [*_offset_build("fitA", "fit", fit_a, 0.6, 4.4, T_HALF, (0.06, 0.1, 0.15, 0.2, 0.3, 0.4),
                           [(w, s) for w in (0.08, 0.15) for s in (0.1, 0.15, 0.3)]),
            *_offset_build("fitB", "fit", fit_a, 0.4, 4.4, T_1OZ, (0.06, 0.1, 0.15, 0.25),
                           [(w, s) for w in (0.08, 0.15) for s in (0.1, 0.2)]),
            *_offset_build("heldC", "held_out", off_knot, 0.8, 3.66, T_1OZ, (0.1, 0.3),
                           [(0.1, 0.2), (0.3, 0.2)]),
            *_offset_build("heldD", "held_out", off_knot, 0.3, 4.2, T_HALF, (0.12,),
                           [(0.1, 0.12)]),
            *_named_offset_specs()]


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
    specs = [*masked_specs(), *offset_specs(), *measured_specs()]
    for spec in specs:
        if fb.cause_of(spec.geom) != spec.cause:
            raise ValueError(f"{spec.name}: geometry has cause {fb.cause_of(spec.geom)!r}, "
                             f"the grid says {spec.cause!r}")
    if len({s.name for s in specs}) != len(specs):
        raise ValueError("calibration point names are not unique")
    return specs


# ---------------------------------------------------------------- FD runs
def _sig(value: float | None) -> float | None:
    return None if value is None else float(f"{value:.{FD_DIGITS}g}")


def run_fd(specs: Sequence[Spec], grid: str = GRID) -> dict[str, Any]:
    """Solve every point with fieldsolve2d; the calibration record {fd, points}."""
    import fieldsolve2d as fd

    backend = fd.available()
    if backend == "none":
        raise SystemExit("tline_calibrate: no FD backend; pip install -r server/requirements.txt")
    points, start = [], time.perf_counter()
    for i, spec in enumerate(specs, 1):
        res = fd.solve(spec.geom, grid=grid)
        points.append({"name": spec.name, "cause": spec.cause, "set": spec.set, "ref": spec.ref,
                       "geometry": geometry_to_dict(spec.geom),
                       "fd": {k: _sig(getattr(res, k)) for k in ("z0", "z_even", "z_odd",
                                                                  "z_diff")}})
        print(f"[{i}/{len(specs)}] {spec.name}: {res.seconds:.1f} s", flush=True)
    print(f"FD: {len(specs)} points in {time.perf_counter() - start:.1f} s ({backend}, {grid})")
    return {"fd": {"solver": "fieldsolve2d", "version": fd.FD_VERSION, "grid": grid},
            "points": points}


def fd_points_from_fixture(doc: Mapping[str, Any]) -> dict[str, Any]:
    """The calibration record (FD inputs only) of a committed fixture."""
    keys = ("name", "cause", "set", "ref", "geometry", "fd")
    return {"fd": dict(doc["fd"]), "points": [{k: p[k] for k in keys} for p in doc["points"]]}


# ---------------------------------------------------------------- fits
def _hat(a: float) -> list[float]:
    """Piecewise-linear basis over KNOTS without the last knot (pinned to 0)."""
    out = [0.0] * (len(KNOTS) - 1)
    a = min(max(a, KNOTS[0]), KNOTS[-1])
    for i, (a0, a1) in enumerate(pairwise(KNOTS)):
        if a0 <= a <= a1:
            f = (a - a0) / (a1 - a0)
            out[i] += 1.0 - f
            if i + 1 < len(out):
                out[i + 1] += f
            return out
    return out


def _round(value: float) -> float:
    return float(f"{value:.{COEF_DIGITS}g}")


def _modes(point: Mapping[str, Any], closed: LineResult) -> dict[str, tuple[float, float]]:
    """(closed form, FD) impedance per mode of a point."""
    fd = point["fd"]
    if fd["z_diff"] is None:
        return {"single": (closed.z0, fd["z0"])}
    return {"even": (closed.z_even, fd["z_even"]), "odd": (closed.z_odd, fd["z_odd"])}


def fit_offset(points: Sequence[Mapping[str, Any]], geoms: Sequence[LineGeometry],
               closed: Sequence[LineResult]) -> dict[str, dict[str, list[float]]]:
    """Per mode: e = Z_image / Z_FD - 1 regressed on hat(a) x feature (numpy lstsq)."""
    import numpy as np

    rows: dict[str, list] = {m: [] for m in fb.OFFSET_MODES}
    ys: dict[str, list] = {m: [] for m in fb.OFFSET_MODES}
    for p, g, c in zip(points, geoms, closed, strict=True):
        if p["cause"] != fb.OFFSET or p["set"] != "fit":
            continue
        a, x = fb.domain_values(g)["a"], fb.features(g)
        for mode, (z, z_fd) in _modes(p, c).items():
            rows[mode].append([h * x[f] for f in fb.OFFSET_MODES[mode] for h in _hat(a)])
            ys[mode].append(z / z_fd - 1.0)
    terms = {}
    for mode, feats in fb.OFFSET_MODES.items():
        coef = np.linalg.lstsq(np.array(rows[mode]), np.array(ys[mode]), rcond=None)[0]
        n = len(KNOTS) - 1
        terms[mode] = {f: [_round(float(v)) for v in coef[i * n:(i + 1) * n]] + [0.0]
                       for i, f in enumerate(feats)}
    return terms


def fit_masked(points: Sequence[Mapping[str, Any]], geoms: Sequence[LineGeometry],
               closed: Sequence[LineResult]) -> dict[str, dict[str, float]]:
    """Per mode: ln(Z_FD / Z_bare) regressed on ln(r) x feature (numpy lstsq)."""
    import numpy as np

    data: dict[str, tuple[list, list]] = {k: ([], []) for k in MASK_K}
    for p, g, c in zip(points, geoms, closed, strict=True):
        if p["cause"] != fb.MASKED or p["set"] != "fit":
            continue
        x, lr = fb.features(g), math.log(fb.mask_ratio(g))
        for key, z, z_fd in (("k_even", c.z_even, p["fd"]["z_even"]),
                             ("k_odd", c.z_odd, p["fd"]["z_odd"])):
            data[key][0].append([lr * x[f] for f in MASK_K[key]])
            data[key][1].append(math.log(z_fd / z))
    out = {}
    for key, (a_rows, y) in data.items():
        coef = np.linalg.lstsq(np.array(a_rows), np.array(y), rcond=None)[0]
        out[key] = {f: _round(float(v)) for f, v in zip(MASK_K[key], coef, strict=True)}
    return out


# ---------------------------------------------------------------- errors and estimates
def ceil_tenth(value: float) -> float:
    """The next 0.1 % strictly above value."""
    return math.floor(value * 10.0 + 1.0) / 10.0


def _zq(res: Any) -> float:
    return res.z0 if res.z_diff is None else res.z_diff


def _errors(points: Sequence[Mapping[str, Any]], results: Sequence[LineResult]) -> list[dict]:
    """Per point: error (%) of the est quantity (z0 / z_diff) and of z_even vs FD."""
    out = []
    for p, r in zip(points, results, strict=True):
        fd = p["fd"]
        ref = fd["z0"] if fd["z_diff"] is None else fd["z_diff"]
        even = None if fd["z_even"] is None else 100.0 * (r.z_even / fd["z_even"] - 1.0)
        out.append({"z": 100.0 * (_zq(r) / ref - 1.0), "z_even": even})
    return out


def _interval_est(knots: Sequence[float], a_values: Sequence[float],
                  errs: Sequence[float]) -> list[float]:
    """Per knot interval [lo, hi]: the next 0.1 % above the worst |error| of its points."""
    out = []
    for lo, hi in pairwise(knots):
        inside = [abs(e) for a, e in zip(a_values, errs, strict=True) if lo <= a <= hi]
        if not inside:
            raise ValueError(f"asymmetry interval [{lo}, {hi}] has no calibration point")
        out.append(ceil_tenth(max(inside)))
    return out


def _hull(values: Sequence[Mapping[str, float]], fixed: Mapping[str, list] | None = None
          ) -> dict[str, list[float]]:
    names = sorted({k for v in values for k in v})
    out = {k: [min(v[k] for v in values if k in v), max(v[k] for v in values if k in v)]
           for k in names}
    return {**out, **(fixed or {})}


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


# ---------------------------------------------------------------- documents
def _offset_entry(sel: Sequence[tuple[Mapping[str, Any], dict, LineGeometry]],
                  terms: Mapping[str, Any]) -> dict[str, Any]:
    a = [fb.domain_values(g)["a"] for _, _, g in sel]
    single = [(ai, e["z"]) for ai, (p, e, _) in zip(a, sel, strict=True)
              if p["fd"]["z_diff"] is None]
    pair = [(ai, e["z"], e["z_even"]) for ai, (p, e, _) in zip(a, sel, strict=True)
            if p["fd"]["z_diff"] is not None]
    keys = {"single": lambda p, e: e["z"] if p["fd"]["z_diff"] is None else None,
            "pair_z_diff": lambda p, e: e["z"] if p["fd"]["z_diff"] is not None else None,
            "pair_z_even": lambda p, e: e["z_even"]}
    pairs = [(p, e) for p, e, _ in sel]
    return {"form": FORMS[fb.OFFSET], "knots": list(KNOTS), "terms": terms,
            "est_error_pct": {"single": _interval_est(KNOTS, [x[0] for x in single],
                                                      [x[1] for x in single]),
                              "pair": _interval_est(KNOTS, [x[0] for x in pair],
                                                    [x[1] for x in pair])},
            "z_even_error_pct": _interval_est(KNOTS, [x[0] for x in pair], [x[2] for x in pair]),
            "domain": _hull([fb.domain_values(g) for _, _, g in sel], {"a": [min(a), KNOTS[-1]]}),
            "fit": _part_stats(pairs, "fit", keys),
            "held_out": _part_stats(pairs, "held_out", keys)}


def _masked_entry(sel: Sequence[tuple[Mapping[str, Any], dict, LineGeometry]],
                  k: Mapping[str, Any]) -> dict[str, Any]:
    keys = {"z_diff": lambda p, e: e["z"], "z_even": lambda p, e: e["z_even"]}
    pairs = [(p, e) for p, e, _ in sel]
    return {"form": FORMS[fb.MASKED], **k,
            "est_error_pct": ceil_tenth(max(abs(e["z"]) for _, e, _ in sel)),
            "z_even_error_pct": ceil_tenth(max(abs(e["z_even"]) for _, e, _ in sel)),
            "domain": _hull([fb.domain_values(g) for _, _, g in sel]),
            "fit": _part_stats(pairs, "fit", keys),
            "held_out": _part_stats(pairs, "held_out", keys)}


def _identity_entry(cause: str, errs: Sequence[float]) -> dict[str, Any]:
    worst = max((abs(e) for e in errs), default=None)
    est = PLACEHOLDER_PCT[cause] if worst is None else max(PLACEHOLDER_PCT[cause],
                                                           ceil_tenth(worst))
    return {"form": FORMS[cause], "est_error_pct": est, "placeholder_pct": PLACEHOLDER_PCT[cause],
            "measured": None if worst is None else {"points": len(errs), "worst_pct": worst}}


def calibration_digest(points: Sequence[Mapping[str, Any]]) -> str:
    """sha256 of the canonical JSON of the FD inputs (name, cause, set, geometry, fd)."""
    canonical = json.dumps([{k: p[k] for k in ("name", "cause", "set", "geometry", "fd")}
                            for p in points], sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _data_doc(cal: Mapping[str, Any], geoms: Sequence[LineGeometry], errs: Sequence[dict],
              terms: Mapping[str, Any], k: Mapping[str, Any]) -> dict[str, Any]:
    points = cal["points"]

    def sel(cause: str) -> list:
        return [(p, e, g) for p, e, g in zip(points, errs, geoms, strict=True)
                if p["cause"] == cause]

    causes = {fb.OFFSET: _offset_entry(sel(fb.OFFSET), terms),
              fb.MASKED: _masked_entry(sel(fb.MASKED), k)}
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
    return {**p, "closed_form": {"class": c.accuracy_class, "model": c.model, "z": _zq(c),
                                 "est_error_pct": c.est_error_pct},
            "fallback": {"model": f.model, "z": _zq(f), "z_even": f.z_even,
                         "est_error_pct": f.est_error_pct},
            "error_pct": e["z"], "z_even_error_pct": e["z_even"]}


def build_outputs(cal: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """(data document, fixture document) from a calibration record {fd, points}."""
    points = cal["points"]
    geoms = [geometry_from_dict(p["geometry"]) for p in points]
    closed = [tline.evaluate(g) for g in geoms]
    terms, k = fit_offset(points, geoms, closed), fit_masked(points, geoms, closed)
    draft = _data_doc(cal, geoms, [{"z": 1.0, "z_even": 1.0}] * len(points), terms, k)
    draft_table = fb.parse_table(draft, source="draft")
    errs = _errors(points, [fb.correct(g, c, table=draft_table)
                            for g, c in zip(geoms, closed, strict=True)])
    data = _data_doc(cal, geoms, errs, terms, k)
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
        out.append(f"{cause}: est_error_pct {entry['est_error_pct']}")
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
    ap.add_argument("--data", type=Path, default=DATA_OUT)
    ap.add_argument("--fixture", type=Path, default=FIXTURE_OUT)
    ap.add_argument("--fd-source", type=Path, default=None,
                    help="fixture whose FD values --refit/--check reuse (default: --fixture)")
    args = ap.parse_args(argv)
    start = time.perf_counter()
    if args.refit or args.check:
        source = args.fd_source or args.fixture
        cal = fd_points_from_fixture(json.loads(source.read_text(encoding="utf-8")))
    else:
        cal = run_fd(all_specs(), args.grid)
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
