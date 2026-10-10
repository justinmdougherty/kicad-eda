"""Class C fallback models of the tline package (ADR D7.6 'Class C fallback models', critique
M11; task D7-05c).

tline/fallback.py corrects a class C closed-form result per cause: offset / dual stripline
(image method / (1 + e)), solder mask on an edge-coupled microstrip (bare pair x r^k x e^d with
the single-ended Wan-Hoorfar ratio r), and identity causes (multi-ply Er contrast, CPW with
finite grounds, the nearest closed form). Every coefficient, the calibrated domain and every
est_error_pct come from server/data/tline_fallback.json, which tools/tline_calibrate.py writes
from FD runs together with the calibration set tests/fixtures/tline/fallback_calibration.json.

The calibration set holds the task grid, named D7.14 / [R-imp] vectors and seeded random
points drawn uniformly over each cause's declared domain (fit and an independent held-out
set); est_error_pct is the worst error over all of them times a margin. These tests run on the
committed fixture only (no FD solve), except the refit tests (numpy).
"""
from __future__ import annotations

import ast
import copy
import hashlib
import json
import math
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

import tline
import toolchain as tc
from tline import fallback as fb
from tline.types import LineGeometry, LineResult, MaskModel, Ply

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "server" / "tools"
FALLBACK_PY = TOOLS / "tline" / "fallback.py"
DATA = ROOT / "server" / "data" / "tline_fallback.json"
FIXTURE = ROOT / "tests" / "fixtures" / "tline" / "fallback_calibration.json"
JLC_MASK = MaskModel(er=3.8, over_trace=0.0152, over_substrate=0.0305, between=0.0305)  # D7.14
T_1OZ, T_HALF_OZ = 0.035, 0.0152
KICAD_PY_TIMEOUT_S = 120
MIN_RANDOM = {fb.OFFSET: 400, fb.MASKED: 150}      # seeded random points per corrected cause


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


DOC = _load(DATA)
CAL = _load(FIXTURE)
POINTS = CAL["points"]


def geometry(d: dict[str, Any]) -> LineGeometry:
    """A LineGeometry from the fixture's compact geometry record (plies as [thickness, er])."""
    mask = d.get("mask")
    return LineGeometry(kind=d["kind"], pair=d["pair"], width=d["width"], t=d["t"],
                        below=tuple(Ply(*p) for p in d["below"]),
                        above=tuple(Ply(*p) for p in d["above"]), pair_gap=d.get("pair_gap"),
                        ground_gap=d.get("ground_gap"), ground_width=d.get("ground_width"),
                        mask=None if mask is None else MaskModel(**mask),
                        etch=d.get("etch", 0.0),
                        dielectric_copper=tuple(tuple(x) for x in d.get("dielectric_copper", ())))


def zq(res: Any) -> float:
    """The quantity est_error_pct refers to: z0 of a single line, z_diff of a pair."""
    return res.z0 if res.z_diff is None else res.z_diff


def pct(value: float, ref: float) -> float:
    return 100.0 * (value / ref - 1.0)


def fd_z(point: dict[str, Any]) -> float:
    fd = point["fd"]
    return fd["z0"] if "z_odd" not in fd else 2.0 * fd["z_odd"]


def by_name(name: str) -> dict[str, Any]:
    return next(p for p in POINTS if p["name"] == name)


def inside(geom: LineGeometry) -> bool:
    return not fb.outside_notes(geom)


def cms(width: float, gap: float, h: float = 0.2104, er: float = 4.4, *,
        mask: MaskModel | None = JLC_MASK, above: tuple[Ply, ...] = (),
        t: float = T_1OZ) -> LineGeometry:
    return LineGeometry(kind="microstrip", pair="edge", width=width, t=t,
                        below=(Ply(h, er),), above=above, pair_gap=gap, mask=mask)


def sl_off(width: float, gap: float | None = None, **kw: Any) -> LineGeometry:
    """D7.14 SL50_off / DSL100_off: h1 0.1088 @ 4.16, h2 0.55 @ 4.6, 0.5 oz."""
    return LineGeometry(kind="stripline", pair="single" if gap is None else "edge", width=width,
                        t=T_HALF_OZ, below=(Ply(0.55, 4.6),), above=(Ply(0.1088, 4.16),),
                        pair_gap=gap, **kw)


def osl(width: float, a: float, b: float = 0.6, t: float = T_HALF_OZ, gap: float | None = None,
        er_near: float = 4.4, er_far: float = 4.4) -> LineGeometry:
    """Offset stripline, near reference above at asymmetry a, plane spacing b (copper incl.)."""
    h = b - t
    return LineGeometry(kind="stripline", pair="single" if gap is None else "edge", width=width,
                        t=t, below=(Ply((1 - a) * h, er_far),), above=(Ply(a * h, er_near),),
                        pair_gap=gap)


# ---------------------------------------------------------------- data file and fixture
def test_data_file_and_fixture_are_the_calibration_outputs() -> None:
    assert DOC["schema"] == fb.SCHEMA
    assert DOC["generated_by"] == "server/tools/tline_calibrate.py"
    assert CAL["schema"] == "kicad-eda/tline-fallback-calibration@2"
    assert set(DOC["causes"]) == set(fb.CAUSES)
    canonical = json.dumps([{k: p[k] for k in ("name", "cause", "set", "geometry", "fd")}
                            for p in POINTS], sort_keys=True, separators=(",", ":"))
    assert DOC["calibration"]["digest"] == hashlib.sha256(canonical.encode()).hexdigest()
    assert DOC["calibration"]["points"] == len(POINTS)
    assert DOC["fd"]["version"] == CAL["fd"]["version"] and DOC["fd"]["grid"] == "medium"
    names = [p["name"] for p in POINTS]
    assert len(names) == len(set(names))


def test_the_calibration_grid_covers_the_task_grid() -> None:
    """Masked coupled microstrip: h 0.0994 / 0.2104 / 0.508 x S/W 0.5 / 1 / 2 (>= 9 fit points)
    plus held-out points; offset / dual stripline over asymmetry 0.15-0.35 incl. the -2116 In2
    dual stripline, SL50_off and DSL100_off."""
    masked = [p for p in POINTS if p["cause"] == fb.MASKED and p["name"].startswith("MSK_grid")]
    cells = {(p["geometry"]["below"][0][0], round(p["geometry"]["pair_gap"]
                                                   / p["geometry"]["width"], 3)) for p in masked}
    assert {(h, r) for h in (0.0994, 0.2104, 0.508) for r in (0.5, 1.0, 2.0)} <= cells
    assert all(p["set"] == "fit" for p in masked if round(p["geometry"]["pair_gap"]
                                                          / p["geometry"]["width"], 3) in
               (0.5, 1.0, 2.0))
    grid = [p for p in POINTS if p["name"].startswith("OSL_grid")]
    asym = {round(fb.domain_values(geometry(p["geometry"]))["a"], 3) for p in grid}
    assert {0.15, 0.2, 0.25, 0.3, 0.35} <= asym
    held = {p["name"] for p in POINTS if p["cause"] == fb.OFFSET and p["set"] == "held_out"}
    assert {"SL50_off@W_FD", "DSL100_off@W_FD", "JLC6_2116_In2_dual@0.15"} <= held
    assert {p["cause"] for p in POINTS if p["set"] == "measured"} == {fb.MULTI_PLY, fb.CPW_FINITE}


@pytest.mark.parametrize("cause", [fb.OFFSET, fb.MASKED])
def test_seeded_random_points_cover_the_declared_domain(cause: str) -> None:
    """The domain written to the data file is the box the calibration samples uniformly: every
    random point (fit and held out) lies inside it and each coordinate's sampled range spans
    most of its declared interval (no corner of the box is claimed without samples near it)."""
    rand = [p for p in POINTS if p["cause"] == cause and "_rand_" in p["name"]]
    assert len(rand) >= MIN_RANDOM[cause]
    assert {p["set"] for p in rand} == {"fit", "held_out"}
    held = [p for p in rand if p["set"] == "held_out"]
    assert len(held) >= MIN_RANDOM[cause] // 3
    domain = DOC["causes"][cause]["domain"]
    values = [fb.domain_values(geometry(p["geometry"])) for p in rand]
    assert all(inside(geometry(p["geometry"])) for p in rand)
    for name, (lo, hi) in domain.items():
        seen = [v[name] for v in values if name in v]
        span = (max(seen) - min(seen)) / (hi - lo) if hi > lo else 1.0
        assert span > 0.8, (name, min(seen), max(seen), lo, hi)


def test_every_offset_knot_interval_is_sampled_incl_near_centred_lines() -> None:
    """Fit and held-out points in every asymmetry interval for singles and pairs, the last
    (up to centred) included, with and without Er contrast."""
    knots = DOC["causes"][fb.OFFSET]["knots"]
    pts = [(p, fb.domain_values(geometry(p["geometry"]))) for p in POINTS
           if p["cause"] == fb.OFFSET]
    for lo, hi in zip(knots, knots[1:], strict=False):
        for pair in ("single", "edge"):
            for part in ("fit", "held_out"):
                hits = [v for p, v in pts if p["set"] == part and lo <= v["a"] <= hi
                        and p["geometry"]["pair"] == pair]
                assert len(hits) >= 5, (lo, hi, pair, part)
                assert any(abs(math.log(v["er_near_over_far"])) > 0.1 for v in hits)


def test_fit_and_held_out_statistics_are_reported() -> None:
    for cause in (fb.OFFSET, fb.MASKED):
        entry = DOC["causes"][cause]
        for part in ("fit", "held_out"):
            assert entry[part]["points"] >= 50 and entry[part]["worst_pct"]
        assert "form" in entry and entry["domain"] and entry["est_margin"] > 1


# ---------------------------------------------------------------- every point within est
@pytest.mark.parametrize("point", POINTS, ids=[p["name"] for p in POINTS])
def test_every_calibration_point_is_within_its_est_error(point: dict[str, Any]) -> None:
    """The fallback model of the point's cause (forced through fallback.correct, whatever the
    closed form's class) is within the est_error_pct it reports, against the FD value."""
    geom = geometry(point["geometry"])
    closed = tline.evaluate(geom)
    assert fb.cause_of(geom) == point["cause"]
    out = fb.correct(geom, closed)
    err = pct(zq(out), fd_z(point))
    assert abs(err) <= out.est_error_pct, (point["name"], err, out.est_error_pct)
    assert out.est_error_pct == pytest.approx(point["fallback"]["est_error_pct"], abs=1e-12)
    assert zq(out) == pytest.approx(point["fallback"]["z"], rel=1e-9)
    assert err == pytest.approx(point["error_pct"], abs=1e-6)
    if closed.accuracy_class == "C":
        assert fb.apply(geom, closed) == out
    else:
        assert fb.apply(geom, closed) is closed


@pytest.mark.parametrize("cause", [fb.OFFSET, fb.MASKED])
def test_even_mode_of_every_calibrated_pair_is_within_its_bound(cause: str) -> None:
    for point in POINTS:
        if point["cause"] != cause or point["geometry"]["pair"] != "edge":
            continue
        geom = geometry(point["geometry"])
        closed = tline.evaluate(geom)
        out = fb.correct(geom, closed)
        err = abs(pct(out.z_even, point["fd"]["z_even"]))
        assert err <= fb.z_even_error_pct(geom, closed), (point["name"], err)


def _worst(cause: str, pick: Any) -> dict[Any, float]:
    """Worst |error| per estimate group over the cause's inside-domain points."""
    out: dict[Any, float] = {}
    for p in POINTS:
        geom = geometry(p["geometry"])
        if p["cause"] != cause or not inside(geom):
            continue
        key, err = pick(p, geom)
        out[key] = max(out.get(key, 0.0), abs(err))
    return out


def test_estimates_carry_the_margin_over_every_inside_point() -> None:
    """est_error_pct >= margin x the worst inside-domain error of its mode (fit + held out),
    so an estimate is never just the largest sampled error; one estimate per mode (thin
    asymmetry intervals understated independent probes in round 1)."""
    import tline_calibrate as cal

    off = DOC["causes"][fb.OFFSET]
    assert off["est_margin"] == cal.EST_MARGIN >= 1.5
    margin = off["est_margin"]
    kinds = _worst(fb.OFFSET, lambda p, g: ("pair" if g.pair == "edge" else "single",
                                             p["error_pct"]))
    for kind, worst in kinds.items():
        assert off["est_error_pct"][kind] >= margin * worst - 1e-9, (kind, worst)
    even = _worst(fb.OFFSET, lambda p, g: ("e", p.get("z_even_error_pct") or 0.0))["e"]
    assert off["z_even_error_pct"] >= margin * even - 1e-9
    masked = DOC["causes"][fb.MASKED]
    worst = _worst(fb.MASKED, lambda p, g: ("z", p["error_pct"]))["z"]
    assert masked["est_error_pct"] >= masked["est_margin"] * worst - 1e-9
    even = _worst(fb.MASKED, lambda p, g: ("e", p["z_even_error_pct"]))["e"]
    assert masked["z_even_error_pct"] >= masked["est_margin"] * even - 1e-9


def test_the_fallback_improves_on_the_closed_form_where_it_corrects() -> None:
    for cause in (fb.OFFSET, fb.MASKED):
        before = after = 0.0
        for point in (p for p in POINTS if p["cause"] == cause):
            geom = geometry(point["geometry"])
            if not inside(geom):
                continue
            closed = tline.evaluate(geom)
            before = max(before, abs(pct(zq(closed), fd_z(point))))
            after = max(after, abs(pct(zq(fb.correct(geom, closed)), fd_z(point))))
        assert after < before / 2, (cause, before, after)


# ---------------------------------------------------------------- D7.14 / [R-imp] vectors
def test_masked_usb_pair_on_7628_reproduces_86_80_ohm() -> None:
    """[R-imp]: the USB pair on JLC 7628 (W 0.259, S 0.15) with JLC mask is Zdiff 86.80 ohm by
    FD; the bare closed form reads 93.6 (+7.9 %, est 8.5 %). The fallback is class C within
    its calibrated error, which is well below the closed form's."""
    geom = cms(0.25898442342877387, 0.15)
    closed = tline.evaluate(geom)
    assert closed.accuracy_class == "C" and inside(geom)
    out = fb.apply(geom, closed)
    assert out.accuracy_class == "C"
    assert out.model == f"{closed.model}+fallback:{fb.MASKED}"
    assert abs(pct(out.z_diff, 86.80)) <= out.est_error_pct < closed.est_error_pct / 2
    assert pct(closed.z_diff, 86.80) > 7.0
    point = by_name("USB90_7628_mask")
    assert point["ref"] == pytest.approx(86.80)
    assert out.z_diff == pytest.approx(point["fallback"]["z"], rel=1e-9)


def test_dms90_7628_mask_row_is_within_its_calibrated_error() -> None:
    """D7.14 DMS90_7628_mask: W_FD 0.2349 is the masked FD width for Zdiff 90 ohm."""
    geom = cms(0.23492859579490222, 0.15)
    out = fb.apply(geom, tline.evaluate(geom))
    assert abs(pct(out.z_diff, 90.0)) <= out.est_error_pct
    assert out.z_diff == pytest.approx(by_name("DMS90_7628_mask")["fallback"]["z"], rel=1e-9)


@pytest.mark.parametrize("name, geom, ref", [
    ("SL50_off@W_FD", sl_off(0.13474641701088028), 50.0),
    ("DSL100_off@W_FD", sl_off(0.08943079170736003, 0.15), 100.0),
])
def test_offset_rows_sl50_off_and_dsl100_off(name: str, geom: LineGeometry, ref: float) -> None:
    """D7.14 SL50_off / DSL100_off at the FD width: the image method reads +6.4 % / +1.8 %
    (class C); the fallback is within its calibrated error of the FD target."""
    closed = tline.evaluate(geom)
    assert closed.accuracy_class == "C" and inside(geom)
    out = fb.apply(geom, closed)
    assert out.model.endswith(f"+fallback:{fb.OFFSET}")
    assert abs(pct(zq(out), ref)) <= out.est_error_pct < closed.est_error_pct
    assert abs(pct(zq(out), ref)) < abs(pct(zq(closed), ref))
    assert by_name(name)["ref"] == pytest.approx(ref)
    if geom.pair == "edge":
        assert out.eeff == closed.eeff and out.z_diff == pytest.approx(2 * out.z_odd, rel=1e-12)


def test_jlc6_2116_in2_dual_stripline_is_corrected() -> None:
    point = by_name("JLC6_2116_In2_dual@0.15")
    geom = geometry(point["geometry"])
    assert geom.kind == "dual_stripline" and geom.dielectric_copper == (("In3.Cu", 4.4),)
    closed = tline.evaluate(geom)
    assert closed.accuracy_class == "C" and inside(geom)
    out = fb.apply(geom, closed)
    assert abs(pct(out.z0, fd_z(point))) <= out.est_error_pct < abs(pct(closed.z0, fd_z(point)))
    assert any("dual stripline" in w for w in out.warnings)       # the model's notes are kept


# ---------------------------------------------------------------- near-centred lines
def test_the_centred_knot_is_fitted_for_pairs() -> None:
    """The mapped-Cohn pair has its own error at a = 0.5 (stripline EST_EDGE_PCT), so the
    pair modes' coefficients at the last knot are fitted, not pinned to 0."""
    terms = DOC["causes"][fb.OFFSET]["terms"]
    assert DOC["causes"][fb.OFFSET]["knots"][-1] == 0.5
    for mode in ("even", "odd"):
        assert any(abs(c[-1]) > 1e-4 for c in terms[mode].values()), mode


def test_near_centred_contrast_line_is_corrected_with_its_interval_estimate() -> None:
    """A single at a 0.497 with Er 4.05 / 4.62 is class C by contrast: the fallback corrects it
    (e != 0) and reports the calibrated single-line estimate from the data file."""
    geom = osl(0.22, 0.497, b=0.422, t=T_1OZ, er_near=4.05, er_far=4.62)
    closed = tline.evaluate(geom)
    assert closed.accuracy_class == "C" and fb.cause_of(geom) == fb.OFFSET and inside(geom)
    out = fb.apply(geom, closed)
    assert out.z0 != closed.z0
    assert out.est_error_pct == DOC["causes"][fb.OFFSET]["est_error_pct"]["single"]


# ---------------------------------------------------------------- pass-through and identity
@pytest.mark.parametrize("geom", [
    LineGeometry(kind="microstrip", pair="single", width=0.3721, t=T_1OZ,
                 below=(Ply(0.2104, 4.4),), above=()),                         # class A
    LineGeometry(kind="microstrip", pair="single", width=0.348, t=T_1OZ,
                 below=(Ply(0.2104, 4.4),), above=(), mask=JLC_MASK),          # class B
    cms(0.28161273198623493, 0.15, mask=None),                                 # class B pair
    LineGeometry(kind="stripline", pair="single", width=0.15, t=T_HALF_OZ,
                 below=(Ply(0.2424, 4.2),), above=(Ply(0.2424, 4.2),)),        # class A
])
def test_class_a_and_b_results_pass_through_unchanged(geom: LineGeometry) -> None:
    closed = tline.evaluate(geom)
    assert closed.accuracy_class in ("A", "B")
    assert fb.apply(geom, closed) is closed


def test_apply_is_idempotent() -> None:
    geom = cms(0.259, 0.15)
    once = fb.apply(geom, tline.evaluate(geom))
    assert fb.apply(geom, once) is once and fb.correct(geom, once) is once


@pytest.mark.parametrize("geom, cause", [
    (LineGeometry(kind="microstrip", pair="single", width=0.2, t=T_1OZ,
                  below=(Ply(0.1, 4.6), Ply(0.1, 3.0)), above=()), fb.MULTI_PLY),
    (LineGeometry(kind="cpw", pair="single", width=0.3, t=T_1OZ, below=(Ply(0.2104, 4.4),),
                  above=(), ground_gap=0.15, ground_width=0.5), fb.CPW_FINITE),
    (LineGeometry(kind="stripline", pair="broadside", width=0.1, t=0.0,
                  above=(Ply(0.2, 4.4),), below=(Ply(0.8, 4.4), Ply(0.2, 4.4)), pair_gap=0.8),
     fb.NEAREST),
])
def test_identity_causes_keep_the_closed_form_and_take_the_tabled_error(
        geom: LineGeometry, cause: str) -> None:
    closed = tline.evaluate(geom)
    assert closed.accuracy_class == "C"
    out = fb.apply(geom, closed)
    assert (out.z0, out.z_even, out.z_odd, out.eeff) == (closed.z0, closed.z_even, closed.z_odd,
                                                         closed.eeff)
    assert out.model == f"{closed.model}+fallback:{cause}"
    tabled = DOC["causes"][cause]["est_error_pct"]
    assert out.est_error_pct == max(tabled, closed.est_error_pct)
    assert out.warnings[:len(closed.warnings)] == closed.warnings


def test_measured_identity_estimates_carry_the_margin_and_never_undercut_the_adr() -> None:
    """Multi-ply contrast (ADR placeholder 3 %) and CPW with finite grounds (5 %) are measured
    on FD points; the tabled estimate is the larger of the placeholder and margin x worst."""
    for cause in (fb.MULTI_PLY, fb.CPW_FINITE):
        entry = DOC["causes"][cause]
        worst = max(abs(p["error_pct"]) for p in POINTS if p["cause"] == cause)
        assert entry["measured"]["worst_pct"] == pytest.approx(worst, abs=1e-6)
        assert entry["est_error_pct"] >= max(entry["placeholder_pct"],
                                             entry["est_margin"] * worst)
    assert len([p for p in POINTS if p["cause"] == fb.MULTI_PLY]) >= 20
    assert DOC["causes"][fb.NEAREST]["est_error_pct"] == DOC["causes"][fb.NEAREST][
        "placeholder_pct"]


# ---------------------------------------------------------------- calibrated domain
def test_offset_below_the_calibrated_asymmetry_keeps_the_closed_form_estimate() -> None:
    geom = LineGeometry(kind="stripline", pair="single", width=0.15, t=T_HALF_OZ,
                        below=(Ply(0.55, 4.4),), above=(Ply(0.03, 4.4),))      # a 0.052
    closed = tline.evaluate(geom)
    out = fb.apply(geom, closed)
    assert out.z0 < closed.z0                         # still corrected (clamped coordinates)
    assert out.est_error_pct == closed.est_error_pct > 7.8
    assert any("outside the FD-calibrated range" in w and "asymmetry" in w for w in out.warnings)


@pytest.mark.parametrize("geom, word", [
    (replace(sl_off(0.1347), ground_gap=0.1), "ground_gap"),
    (replace(sl_off(0.1347), etch=0.01), "etch"),
    (osl(0.06, 0.25, b=0.4, t=T_1OZ), "t/W"),                   # 1 oz on a 0.06 mm trace
    (osl(0.15, 0.3, b=0.6, t=T_1OZ, gap=0.09), "t/S"),          # gap under 2.9 t
    (cms(0.1, 0.2, t=0.07), "t/W"),
    (cms(0.3, 0.09), "t/S"),
])
def test_unmodelled_inputs_and_joint_limits_are_outside_the_domain(geom: LineGeometry,
                                                                   word: str) -> None:
    """Coplanar grounds and etch were never calibrated; t/W and t/S bound the domain jointly
    (the mapped thickness models break down when the copper is thick against W or S)."""
    closed = tline.evaluate(geom)
    out = fb.correct(geom, closed)
    assert out.est_error_pct >= closed.est_error_pct
    assert any(word in w and "outside" in w for w in out.warnings), out.warnings
    if geom.pair == "edge":
        assert fb.z_even_error_pct(geom, closed) >= closed.est_error_pct


def test_masked_pair_outside_the_calibrated_mask_keeps_the_closed_form_estimate() -> None:
    thick = MaskModel(er=4.5, over_trace=0.05, over_substrate=0.08, between=0.08)
    geom = cms(0.259, 0.15, mask=thick)
    closed = tline.evaluate(geom)
    out = fb.apply(geom, closed)
    assert out.z_diff < closed.z_diff
    assert out.est_error_pct == closed.est_error_pct
    assert any("mask" in w and "outside the FD-calibrated range" in w for w in out.warnings)


def test_a_mask_without_substrate_coating_does_not_crash() -> None:
    geom = cms(0.259, 0.15, mask=MaskModel(er=3.8, over_trace=0.015, over_substrate=0.0,
                                           between=0.0))
    out = fb.apply(geom, tline.evaluate(geom))
    assert any("outside" in w for w in out.warnings)


@pytest.mark.parametrize("cause, make, key", [
    (fb.OFFSET, lambda w: sl_off(w), "w_over_b"),
    (fb.OFFSET, lambda w: sl_off(0.1, gap=w), "s_over_b"),
    (fb.MASKED, lambda w: cms(w, 0.6 * w), "w_over_h"),
    (fb.MASKED, lambda w: cms(0.259, w), "s_over_w"),
])
def test_coordinates_outside_the_domain_are_evaluated_at_the_nearest_calibrated_value(
        cause: str, make: Any, key: str) -> None:
    """No extrapolation of the fitted forms: beyond the calibrated range the correction factor
    is the one at the range's end (and the estimate falls back to the closed form's)."""
    hi = DOC["causes"][cause]["domain"][key][1]
    ref = make(1.0)
    scale = fb.domain_values(ref)[key]                 # the coordinate per mm of `make`'s arg
    far = make(3.0 * hi / scale)

    def factor(g: LineGeometry) -> float:
        closed = tline.evaluate(g)
        return zq(fb.correct(g, closed)) / zq(closed)

    assert fb.domain_values(far)[key] > hi
    assert 0.7 < factor(far) < 1.1
    out = fb.correct(far, tline.evaluate(far))
    assert any("nearest calibrated value" in w for w in out.warnings)


def test_masked_pair_with_covering_plies_falls_back_to_the_nearest_closed_form() -> None:
    geom = cms(0.259, 0.15, above=(Ply(0.1, 4.2),))
    closed = tline.evaluate(geom)
    out = fb.apply(geom, closed)
    assert fb.cause_of(geom) == fb.NEAREST and out.z_diff == closed.z_diff
    assert out.est_error_pct >= closed.est_error_pct


# ---------------------------------------------------------------- result conventions
def test_masked_correction_keeps_the_air_capacitance() -> None:
    """The mask adds dielectric only: eeff_odd scales as (Zodd_bare / Zodd)^2 and the delay
    follows; pair fields keep the tline convention (z0 = sqrt(ze zo), z_diff = 2 zo)."""
    geom = cms(0.259, 0.15)
    closed = tline.evaluate(geom)
    out = fb.apply(geom, closed)
    assert out.eeff == pytest.approx(closed.eeff * (closed.z_odd / out.z_odd) ** 2, rel=1e-12)
    assert out.eeff_f == pytest.approx(closed.eeff_f * (closed.z_odd / out.z_odd) ** 2,
                                       rel=1e-12)
    assert out.delay_ps_per_mm > closed.delay_ps_per_mm
    assert out.z0 == pytest.approx(math.sqrt(out.z_even * out.z_odd), rel=1e-12)
    assert out.z_comm == pytest.approx(out.z_even / 2, rel=1e-12)
    assert out.z_even < closed.z_even and out.z_odd < closed.z_odd


def test_offset_correction_keeps_eeff_and_reads_lower() -> None:
    geom = sl_off(0.13474641701088028)
    closed = tline.evaluate(geom)
    out = fb.apply(geom, closed)
    assert out.eeff == closed.eeff and out.eeff_f == closed.eeff_f
    assert out.delay_ps_per_mm == closed.delay_ps_per_mm
    assert out.z0 < closed.z0 and out.accuracy_class == "C"


# ---------------------------------------------------------------- features and data file
def test_features_are_products_of_domain_coordinates() -> None:
    coords = {"w_over_h": 2.0, "t_over_s": 0.25}
    assert fb.feature_value("one", coords) == 1
    assert fb.feature_value("t_over_s", coords) == 0.25
    assert fb.feature_value("ln_w_over_h*t_over_s", coords) == pytest.approx(
        0.25 * math.log(2.0), rel=1e-15)
    assert fb.feature_value("t_over_s*t_over_s", coords) == pytest.approx(0.0625, rel=1e-15)


def test_estimates_and_coefficients_come_from_the_data_file() -> None:
    doc = copy.deepcopy(DOC)
    entry = doc["causes"][fb.MASKED]
    entry["est_error_pct"] = 12.5
    for key in ("k_odd", "d_odd"):
        entry[key] = {name: 0.0 for name in entry[key]}
    off = doc["causes"][fb.OFFSET]
    off["est_error_pct"]["single"] = 9.25
    doc["causes"][fb.MULTI_PLY]["est_error_pct"] = 7.75
    table = fb.parse_table(doc, source="edited copy")
    masked = cms(0.259, 0.15)
    out = fb.apply(masked, tline.evaluate(masked), table=table)
    assert out.est_error_pct == 12.5
    assert out.z_odd == pytest.approx(tline.evaluate(masked).z_odd, rel=1e-12)   # r^0 e^0
    off_geom = sl_off(0.13474641701088028)
    assert fb.apply(off_geom, tline.evaluate(off_geom), table=table).est_error_pct == 9.25
    multi = LineGeometry(kind="microstrip", pair="single", width=0.2, t=T_1OZ,
                         below=(Ply(0.1, 4.6), Ply(0.1, 3.0)), above=())
    assert fb.apply(multi, tline.evaluate(multi), table=table).est_error_pct == 7.75
    assert table.digest != fb.default_table().digest


def test_fallback_py_has_no_error_estimate_literals() -> None:
    """ADR D7.6: est_error_pct of every fallback comes from the calibration, never from a
    literal in code. fallback.py may hold no number but 0, 1 and 2 (exponent of the
    capacitance ratio), and passes est_error_pct only as computed values."""
    tree = ast.parse(FALLBACK_PY.read_text(encoding="utf-8"))
    numbers = {node.value for node in ast.walk(tree) if isinstance(node, ast.Constant)
               and isinstance(node.value, (int, float)) and not isinstance(node.value, bool)}
    assert numbers <= {0, 1, 2}, numbers
    for node in ast.walk(tree):
        if isinstance(node, ast.keyword) and node.arg == "est_error_pct":
            assert not isinstance(node.value, ast.Constant)


def test_fallback_registers_no_model() -> None:
    tree = ast.parse(FALLBACK_PY.read_text(encoding="utf-8"))
    calls = {getattr(n.func, "id", getattr(n.func, "attr", None)) for n in ast.walk(tree)
             if isinstance(n, ast.Call)}
    assert "register" not in calls
    assert "fallback" in tline.LOADED
    assert all(fn.__module__ != fb.__name__ for fn in tline._REGISTRY.values())


@pytest.mark.parametrize("edit, match", [
    (lambda d: d.update(schema="kicad-eda/other@1"), "schema"),
    (lambda d: d["causes"].pop(fb.NEAREST), "causes"),
    (lambda d: d["causes"].update({fb.NEAREST: 5.0}), "nearest_closed_form"),
    (lambda d: d["causes"][fb.MASKED].update(est_error_pct=-1.0), "est_error_pct"),
    (lambda d: d["causes"][fb.MASKED]["k_odd"].update(bogus_feature=1.0), "bogus_feature"),
    (lambda d: d["causes"][fb.MASKED]["d_even"].update({"ln_t_over_s": 1.0}), "ln_t_over_s"),
    (lambda d: d["causes"][fb.OFFSET].update(knots=[0.3, 0.2, 0.5]), "knots"),
    (lambda d: d["causes"][fb.OFFSET].update(knots=5), "knots"),
    (lambda d: d["causes"][fb.OFFSET]["terms"]["single"].update(one=[0.0]), "one"),
    (lambda d: d["causes"][fb.OFFSET]["terms"]["single"].update(t_over_s=[0.0] * len(
        d["causes"][fb.OFFSET]["knots"])), "t_over_s"),
    (lambda d: d["causes"][fb.OFFSET]["domain"].update(a=[0.5, 0.1]), "domain"),
    (lambda d: d["causes"][fb.OFFSET]["domain"].pop("t_over_w"), "t_over_w"),
    (lambda d: d["causes"][fb.CPW_FINITE].update(est_error_pct=math.nan), "est_error_pct"),
    (lambda d: d["causes"][fb.OFFSET].update(terms=[1.0]), "terms"),
])
def test_a_malformed_data_file_is_refused_loudly(edit: Any, match: str) -> None:
    doc = copy.deepcopy(DOC)
    edit(doc)
    with pytest.raises(ValueError, match=match):
        fb.parse_table(doc, source="test")


def test_missing_data_file_names_the_calibration_tool(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="tline_calibrate"):
        fb.load_table(tmp_path / "absent.json")


def test_default_table_is_the_committed_file() -> None:
    table = fb.default_table()
    assert table is fb.default_table()                     # loaded once
    assert table.digest == fb.load_table(DATA).digest
    assert fb.DATA_PATH == DATA


# ---------------------------------------------------------------- calibration tool
def test_declared_domains_are_the_tools_sampling_boxes() -> None:
    import tline_calibrate as cal

    for cause, box in ((fb.OFFSET, cal.OFFSET_BOX), (fb.MASKED, cal.MASK_BOX)):
        domain = DOC["causes"][cause]["domain"]
        for name, (lo, hi) in box.items():
            assert domain[name] == [lo, hi], name


def test_refit_from_the_fixture_reproduces_the_data_file() -> None:
    """tline_calibrate.build_outputs on the fixture's FD values gives the committed data file
    and fixture (coefficients to 1e-6: BLAS may differ in the last bits)."""
    pytest.importorskip("numpy")
    import tline_calibrate as cal

    data, fixture = cal.build_outputs(cal.fd_points_from_fixture(CAL))
    _assert_close(data, DOC, "data")
    _assert_close(fixture, CAL, "fixture")


def _assert_close(got: Any, want: Any, where: str) -> None:
    if isinstance(want, dict):
        assert isinstance(got, dict) and set(got) == set(want), where
        for key in want:
            _assert_close(got[key], want[key], f"{where}.{key}")
    elif isinstance(want, list):
        assert isinstance(got, list) and len(got) == len(want), where
        for i, (g, w) in enumerate(zip(got, want, strict=True)):
            _assert_close(g, w, f"{where}[{i}]")
    elif isinstance(want, float) and not isinstance(want, bool):
        assert got == pytest.approx(want, rel=1e-6, abs=1e-9), where
    else:
        assert got == want, where


def test_the_random_grid_is_reproducible_from_its_seeds() -> None:
    """The seeded sampler regenerates the fixture's random geometries exactly (stdlib random:
    platform-independent), so the calibration set is a documented sample, not a hand pick."""
    import tline_calibrate as cal

    specs = {s.name: s for s in cal.all_specs()}
    assert set(specs) == {p["name"] for p in POINTS}
    for p in POINTS:
        if "_rand_" in p["name"]:
            assert cal.geometry_to_dict(specs[p["name"]].geom) == p["geometry"], p["name"]


def test_same_doc_ignores_platform_float_noise_only() -> None:
    """libm ulps differ across platforms (6.5e-12 rel measured Windows vs Linux on the
    masked closed forms); --check must not call that a change, but must catch real ones."""
    import tline_calibrate as cal

    doc = {"a": [1.0, 2, "x", None, True], "b": {"z": 86.80, "e": 0.01}}
    noisy = {"a": [1.0 + 1e-11, 2, "x", None, True], "b": {"z": 86.80 * (1 + 7e-12),
                                                         "e": 0.01 + 1e-9}}
    assert cal.same_doc(doc, copy.deepcopy(doc)) and cal.same_doc(doc, noisy)
    for changed in ({"a": doc["a"], "b": {"z": 86.80 * (1 + 1e-6), "e": 0.01}},
                    {"a": doc["a"], "b": {"z": 86.80, "e": 0.02}},
                    {"a": [1.0, 2, "y", None, True], "b": doc["b"]},
                    {"a": doc["a"][:-1], "b": doc["b"]},
                    {"a": doc["a"], "b": {"z": 86.80}},
                    {"a": [1.0, 2, "x", None, False], "b": doc["b"]},
                    {"a": [1.0, 2, "x", 0.0, True], "b": doc["b"]}):
        assert not cal.same_doc(doc, changed), changed


def test_refit_and_check_cli(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """--refit --fd-source writes elsewhere from the committed FD values (no FD run);
    --check accepts float noise and rejects a real change."""
    pytest.importorskip("numpy")
    import tline_calibrate as cal

    data, fixture = tmp_path / "out" / "d.json", tmp_path / "out" / "f.json"
    paths = ["--data", str(data), "--fixture", str(fixture)]
    assert cal.main(["--refit", "--fd-source", str(FIXTURE), *paths]) == 0
    assert cal.same_doc(_load(data), DOC) and cal.same_doc(_load(fixture), CAL)
    noisy = _load(fixture)
    noisy["points"][0]["closed_form"]["z"] *= 1 + 5e-12
    fixture.write_text(cal.dumps(noisy), encoding="utf-8", newline="\n")
    capsys.readouterr()
    assert cal.main(["--check", *paths]) == 0
    assert "check: up to date" in capsys.readouterr().out
    changed = _load(data)
    changed["causes"][fb.MASKED]["est_error_pct"] += 0.1
    data.write_text(cal.dumps(changed), encoding="utf-8", newline="\n")
    assert cal.main(["--check", *paths]) == 1
    assert "DIFFERS" in capsys.readouterr().out


def test_calibration_geometry_records_round_trip() -> None:
    import tline_calibrate as cal

    for point in POINTS[::17]:
        geom = geometry(point["geometry"])
        assert cal.geometry_from_dict(cal.geometry_to_dict(geom)) == geom
        assert cal.geometry_to_dict(geom) == point["geometry"]


# ---------------------------------------------------------------- KiCad bundled Python
def test_fallback_runs_under_kicad_python() -> None:
    """tline (fallback included) runs in KiCad's bundled Python 3.11 with stdlib only."""
    py = tc.kicad_python()
    if py is None:
        pytest.skip(f"no KiCad bundled Python: {tc.kicad_python_error()}")
    code = ("import sys; sys.path.insert(0, sys.argv[1]); import kicad_quiet; import tline\n"
            "from tline import fallback as fb\n"
            "from tline.types import LineGeometry, MaskModel, Ply\n"
            "m = MaskModel(er=3.8, over_trace=0.0152, over_substrate=0.0305, between=0.0305)\n"
            "g = LineGeometry(kind='microstrip', pair='edge', width=0.25898442342877387,\n"
            "                 t=0.035, below=(Ply(0.2104, 4.4),), above=(), pair_gap=0.15,\n"
            "                 mask=m)\n"
            "out = fb.apply(g, tline.evaluate(g))\n"
            "print(sys.version_info[:2], repr(out.z_diff), out.est_error_pct)\n")
    proc = subprocess.run([py, "-c", code, str(TOOLS)], stdin=subprocess.DEVNULL,
                          capture_output=True, encoding="utf-8", errors="replace",
                          timeout=KICAD_PY_TIMEOUT_S, check=False)
    assert proc.returncode == 0, proc.stderr
    z_diff, est = (float(x) for x in proc.stdout.split(")")[-1].split())
    point = by_name("USB90_7628_mask")
    assert z_diff == pytest.approx(point["fallback"]["z"], rel=1e-11)   # stored to 12 digits
    assert est == point["fallback"]["est_error_pct"]


def test_result_types_are_line_results() -> None:
    geom = sl_off(0.08943079170736003, 0.15)
    out = fb.apply(geom, tline.evaluate(geom))
    assert isinstance(out, LineResult) and out.accuracy_class == "C"
