"""Single-ended striplines of the tline package (ADR D7.6 / D7.14, task D7-05).

Kinds: ('stripline', 'single') - symmetric (Wheeler 1978, Cohn 1954 exact at t = 0) and offset
(Wadell 3.6.3 image method with a per-side capacitance split, class by asymmetry); and
('dual_stripline', 'single') - the offset stripline between the two planes with the other
signal layer's copper as dielectric of the adjacent prepreg's Er (critique M1).

Geometry convention (consumed by D7-07 geometry): `below` / `above` hold the plies between the
trace's bottom / top surface and its reference plane, ordered outward; b = h_below + t + h_above.

Vectors (mm, ohm): the Cohn exact value (see the erratum test), Wheeler vs Cohn, the D7.14
SL50_off row, the JLC06161H-2116 In2 dual stripline, the research FD widths (scratch
vectors.json, recommended_vs_fd.json) and prototype pins of the research closed forms.
"""
from __future__ import annotations

import math
import subprocess
from collections.abc import Callable
from itertools import pairwise
from pathlib import Path

import pytest

import tline
import toolchain as tc
from tline import stripline as sl
from tline.elliptic import ellipk
from tline.types import C0, ETA0, LineGeometry, MaskModel, Ply, Verdict

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "server" / "tools"
TLINE = TOOLS / "tline"
T_HALF_OZ = 0.0152
KICAD_PY_TIMEOUT_S = 120
LineOf = Callable[[float], LineGeometry]          # width -> geometry


def strip(width: float, below: tuple[Ply, ...], above: tuple[Ply, ...], *, t: float = T_HALF_OZ,
          kind: str = "stripline", pair: str = "single", gap: float | None = None,
          dielectric_copper: tuple[tuple[str, float], ...] = (), **kw: object) -> LineGeometry:
    return LineGeometry(kind=kind, pair=pair, width=width, t=t, below=below, above=above,
                        pair_gap=gap, dielectric_copper=dielectric_copper, **kw)


def centred(width: float, b: float, er: float, *, t: float = T_HALF_OZ) -> LineGeometry:
    h = (b - t) / 2
    return strip(width, (Ply(h, er),), (Ply(h, er),), t=t)


def offset(width: float, h1: float, er1: float, h2: float, er2: float,
           t: float = T_HALF_OZ) -> LineGeometry:
    return strip(width, (Ply(h2, er2),), (Ply(h1, er1),), t=t)


def jlc6_2116_in2(width: float) -> LineGeometry:
    """JLC06161H-2116 In2 (D7.1 snapshot): In1 GND above through the 0.3 mm core (Dk 4.41);
    In4 +3V3 below through 2 x 7628 0.2084 (Dk 4.4), In3's copper as Dk 4.4 and the 0.3 core."""
    adjacent = Ply(0.2084, 4.4)
    below = (adjacent, adjacent, sl.dielectric_copper_ply(T_HALF_OZ, adjacent), Ply(0.3, 4.41))
    return strip(width, below, (Ply(0.3, 4.41),), kind="dual_stripline",
                 dielectric_copper=(("In3.Cu", 4.4),), layers=("In2.Cu",))


# ---------------------------------------------------------------- Cohn exact and Wheeler
def test_cohn_exact_zero_thickness_stripline() -> None:
    """Cohn 1954: Z0 = eta0/(4 sqrt(er)) K(k)/K(k'), k = sech(pi w / 2b), at w 0.3, b 0.5,
    Er 1 is 90.611 ohm. Three independent checks pin the orientation of the K ratio."""
    z = sl.cohn_z0(0.3, 0.5, 1.0)
    x = math.pi * 0.3 / (2 * 0.5)
    direct = ETA0 / 4 * ellipk(1 / math.cosh(x)) / ellipk(math.tanh(x))
    assert z == pytest.approx(direct, rel=1e-12)
    assert z == pytest.approx(90.6108, rel=1e-4)                 # +/- 0.01 %
    assert sl.wheeler_z0(0.3, 0.5, 0.0, 1.0) == pytest.approx(z, rel=2e-3)
    # research FD solver (t_fd_exact, er 4.2): -0.02 % of this value at the fine grid
    assert sl.cohn_z0(0.3, 0.5, 4.2) == pytest.approx(z / math.sqrt(4.2), rel=1e-12)
    # parallel-plate limit of a wide strip: Z -> eta0 b / (4 w) minus fringing
    wide = sl.cohn_z0(5.0, 0.5, 1.0)
    assert wide < ETA0 * 0.5 / (4 * 5.0) and wide > 0.9 * ETA0 * 0.5 / (4 * 5.0)


def test_adr_d714_cohn_value_97_963_is_the_inverted_k_ratio() -> None:
    """ERRATUM (reported for X-06): ADR D7.14 'Cohn exact ... 97.963 ohm' is the critique's
    30 pi K(k')/K(k) - the reciprocal ratio, which grows with width. The correct value is
    90.611 ohm (above); the stripline model must never produce 97.963 here."""
    x = math.pi * 0.3 / (2 * 0.5)
    inverted = 30 * math.pi * ellipk(math.tanh(x)) / ellipk(1 / math.cosh(x))
    assert inverted == pytest.approx(97.963, abs=0.001)
    assert sl.cohn_z0(0.6, 0.5, 1.0) < sl.cohn_z0(0.3, 0.5, 1.0)  # wider strip, lower Z
    assert tline.evaluate(centred(0.3, 0.5, 1.0, t=0.0)).z0 == pytest.approx(90.6108, rel=1e-4)


@pytest.mark.parametrize("w_over_b", [0.16, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8])
def test_wheeler_within_0_2_pct_of_cohn_at_t0(w_over_b: float) -> None:
    for er in (1.0, 4.2):
        cohn = sl.cohn_z0(w_over_b * 0.5, 0.5, er)
        assert sl.wheeler_z0(w_over_b * 0.5, 0.5, 0.0, er) == pytest.approx(cohn, rel=2e-3)


def test_wheeler_port_reproduces_the_research_closed_form() -> None:
    """tlcalc.stripline_wheeler / wheeler_dw values recorded in the research run."""
    assert sl.wheeler_z0(0.15, 0.5, 0.0152, 4.2) == pytest.approx(58.010083530181234, rel=1e-9)
    assert sl.wheeler_z0(0.4, 0.5, 0.035, 4.2) == pytest.approx(32.660323236688534, rel=1e-9)
    assert sl.wheeler_dw(0.15, 0.5, 0.0152) == pytest.approx(0.0244404506408684, rel=1e-9)
    assert sl.wheeler_dw(0.15, 0.5, 0.0) == 0.0


def test_symmetric_stripline_error_vs_fd_is_inside_the_class_a_estimate() -> None:
    """FD (research solver, fine grid; D7-05 fd_refs.log): 0.5 oz, b 0.5, Er 4.2 - Wheeler
    +0.01 / +0.04 / -0.16 % at W/b 0.16 / 0.3 / 0.8 (ADR D7.6: -0.16..+0.04 %)."""
    for w, fd in [(0.08, 72.79612582493277), (0.15, 57.987886260401346),
                  (0.40, 34.87742242936471)]:
        out = tline.evaluate(centred(w, 0.5, 4.2))
        assert out.accuracy_class == "A"
        assert abs(out.z0 / fd - 1) * 100 <= out.est_error_pct <= 1.0


# ---------------------------------------------------------------- symmetric stripline model
def test_symmetric_stripline_is_wheeler_class_a() -> None:
    out = tline.evaluate(centred(0.15, 0.5, 4.2))
    assert out.z0 == pytest.approx(sl.wheeler_z0(0.15, 0.5, T_HALF_OZ, 4.2), rel=1e-12)
    assert out.eeff == pytest.approx(4.2, rel=1e-12) and out.eeff_f == out.eeff
    assert out.model == "wheeler78" and out.accuracy_class == "A"
    assert out.est_error_pct <= 0.5 and out.warnings == ()
    assert out.z_even is None and out.z_diff is None
    assert out.delay_ps_per_mm == pytest.approx(math.sqrt(4.2) * 1e9 / C0, rel=1e-12)


def test_zero_thickness_symmetric_stripline_is_cohn_exact() -> None:
    out = tline.evaluate(centred(0.3, 0.5, 4.2, t=0.0))
    assert out.z0 == pytest.approx(sl.cohn_z0(0.3, 0.5, 4.2), rel=1e-12)
    assert out.model == "cohn54" and out.accuracy_class == "A"


def test_multi_ply_side_uses_the_series_mean() -> None:
    one = tline.evaluate(offset(0.15, 0.3, 4.4, 0.3, 4.4))
    two = tline.evaluate(strip(0.15, (Ply(0.1, 4.4), Ply(0.2, 4.4)), (Ply(0.3, 4.4),)))
    assert two.z0 == pytest.approx(one.z0, rel=1e-12)


# ---------------------------------------------------------------- offset stripline
@pytest.mark.parametrize("h_near, h_far, cls", [
    (0.30, 0.30, "A"), (0.28, 0.32, "A"), (0.272, 0.328, "A"),  # a 0.5, 0.467, 0.453
    (0.26, 0.34, "B"), (0.212, 0.388, "B"),                     # a 0.433, 0.353
    (0.20, 0.40, "C"), (0.09, 0.51, "C"),                       # a 0.333, 0.15
])
def test_offset_class_follows_the_asymmetry(h_near: float, h_far: float, cls: str) -> None:
    geom = offset(0.15, h_near, 4.4, h_far, 4.4)
    assert sl.asymmetry(geom) == pytest.approx(h_near / (h_near + h_far))
    out = tline.evaluate(geom)
    assert out.accuracy_class == cls
    if cls == "C":
        assert any("class C fallback/FD required" in w for w in out.warnings)
    if h_near != h_far:
        assert "image" in out.model


@pytest.mark.parametrize("a, pct", [(0.5, 0.1), (0.45, 0.3), (0.35, 1.3), (0.25, 3.7),
                                    (0.15, 7.8), (0.3, 2.5), (0.4, 0.8), (0.475, 0.2)])
def test_offset_error_estimate_bounds_the_measured_fd_error(a: float, pct: float) -> None:
    """Worst image-method error over W 0.06 / 0.15 / 0.3 (b 0.6, Er 4.4; FD below), rounded
    up to 0.1 %: 0.07 / 0.20 / 1.25 / 3.61 / 7.71 % at a 0.5 / 0.45 / 0.35 / 0.25 / 0.15
    (ADR D7.6 rounds them to 0.03 / - / ~1 / ~3 / 4.8..7.7 %); linear in between."""
    assert sl.offset_error_pct(a) == pytest.approx(pct, abs=1e-9)


def test_offset_error_estimate_grows_quadratically_below_the_measured_range() -> None:
    assert sl.offset_error_pct(0.10) == pytest.approx(7.8 * (0.15 / 0.10) ** 2)
    out = tline.evaluate(offset(0.15, 0.05, 4.4, 0.55, 4.4))     # a 0.083
    assert out.accuracy_class == "C" and out.est_error_pct > 7.8
    assert any("measured" in w for w in out.warnings)


# FD (research solver, medium grid; D7-05 fd_refs.log): b0 0.6, Er 4.4, 0.5 oz, the grid_osl
# geometry: a, W, h_near, h_far, Z
OSL_FD = [
    (0.15, 0.06, 0.08772, 0.49708, 61.65458797567724),
    (0.15, 0.15, 0.08772, 0.49708, 42.08107133102427),
    (0.15, 0.3, 0.08772, 0.49708, 28.22903825893105),
    (0.25, 0.06, 0.1462, 0.4386, 73.2203061892625),
    (0.25, 0.15, 0.1462, 0.4386, 52.76803926065494),
    (0.25, 0.3, 0.1462, 0.4386, 37.161614210750336),
    (0.35, 0.06, 0.20468, 0.38012, 79.39905277096005),
    (0.35, 0.15, 0.20468, 0.38012, 58.66672687619781),
    (0.35, 0.3, 0.20468, 0.38012, 42.35752381009173),
    (0.45, 0.06, 0.26316, 0.32164, 82.17532847926377),
    (0.45, 0.15, 0.26316, 0.32164, 61.34785074613457),
    (0.45, 0.3, 0.26316, 0.32164, 44.775336006671914),
    (0.5, 0.06, 0.2924, 0.2924, 82.51041228687723),
    (0.5, 0.15, 0.2924, 0.2924, 61.67246849617861),
    (0.5, 0.3, 0.2924, 0.2924, 45.07018599598265),
]


@pytest.mark.parametrize("a, w, h_near, h_far, fd", OSL_FD)
def test_offset_stripline_error_vs_fd_is_inside_its_estimate(a: float, w: float, h_near: float,
                                                             h_far: float, fd: float) -> None:
    out = tline.evaluate(offset(w, h_near, 4.4, h_far, 4.4))
    assert out.accuracy_class == ("A" if a >= 0.45 else "B" if a >= 0.35 else "C")
    assert 0.0 <= (out.z0 / fd - 1) * 100 <= out.est_error_pct   # the image method reads high


def test_image_method_reduces_to_wheeler_when_centred() -> None:
    h = (0.5 - T_HALF_OZ) / 2
    z, eeff = sl.image_single(0.15, h, h, T_HALF_OZ, 4.2, 4.2)
    assert z == pytest.approx(sl.wheeler_z0(0.15, 0.5, T_HALF_OZ, 4.2), rel=1e-12)
    assert eeff == pytest.approx(4.2, rel=1e-12)


def test_sl50_off_row_is_class_c_with_the_image_method_seed() -> None:
    """D7.14 SL50_off: h1 0.1088 @ 4.16, h2 0.55 @ 4.6, W_FD 0.1347 (FD 50 ohm): class C.
    The image method reads high (+6.2 % on this geometry [R-imp]); est_error covers it."""
    out = tline.evaluate(offset(0.13474641701088028, 0.1088, 4.16, 0.55, 4.6))
    assert out.accuracy_class == "C"
    assert any("class C fallback/FD required" in w for w in out.warnings)
    assert 50.0 < out.z0 < 54.0
    assert abs(out.z0 / 50.0 - 1) * 100 <= out.est_error_pct
    # research prototype (tlcalc.offset_stripline, capacitance split) at W_cf
    proto = tline.evaluate(offset(0.15364761993288997, 0.1088, 4.16, 0.55, 4.6))
    assert proto.z0 == pytest.approx(50.000002084593554, rel=1e-9)
    assert proto.eeff == pytest.approx(4.298410478877474, rel=1e-9)
    assert abs(proto.z0 / 47.0796208044725 - 1) * 100 <= proto.est_error_pct   # FD at W_cf


def test_er_contrast_above_10_pct_is_class_c_even_when_centred() -> None:
    out = tline.evaluate(offset(0.15, 0.3, 3.0, 0.3, 4.6))
    assert out.accuracy_class == "C"
    assert any("contrast" in w for w in out.warnings)
    ok = tline.evaluate(offset(0.15, 0.3, 4.41, 0.3, 4.4))
    assert ok.accuracy_class == "A"
    assert 4.4 < ok.eeff < 4.41


# ---------------------------------------------------------------- dual stripline (M1)
def test_jlc6_2116_in2_dual_stripline_is_class_c() -> None:
    geom = jlc6_2116_in2(0.15)
    assert sl.asymmetry(geom) == pytest.approx(0.3 / (0.3 + 0.4168 + T_HALF_OZ + 0.3))
    assert 0.28 < sl.asymmetry(geom) < 0.30
    out = tline.evaluate(geom)
    assert out.accuracy_class == "C"
    assert "dual" in out.model and "image" in out.model
    assert any("In3.Cu" in w and "orthogonal" in w for w in out.warnings)
    assert any("class C fallback/FD required" in w for w in out.warnings)
    same_as_offset = tline.evaluate(LineGeometry(
        kind="stripline", pair="single", width=0.15, t=T_HALF_OZ,
        below=geom.below, above=geom.above))
    assert out.z0 == pytest.approx(same_as_offset.z0, rel=1e-12)


@pytest.mark.parametrize("w, fd", [(0.10, 80.49664920649484), (0.15, 70.89700606345086),
                                   (0.25, 58.39026031398051)])
def test_jlc6_2116_in2_dual_stripline_error_vs_fd(w: float, fd: float) -> None:
    """FD of the In2 cross-section (research solver, medium grid; D7-05 fd_refs.log, In3's
    copper as 7628 resin): the image method reads +2.4..2.5 % high, inside its estimate."""
    out = tline.evaluate(jlc6_2116_in2(w))
    assert 0.0 < (out.z0 / fd - 1) * 100 <= out.est_error_pct


def test_dielectric_copper_ply_takes_the_adjacent_prepreg() -> None:
    prepreg = Ply(0.2084, 4.4, 0.02, 1e9)
    ply = sl.dielectric_copper_ply(0.0152, prepreg)
    assert ply == Ply(0.0152, 4.4, 0.02, 1e9)


def test_symmetric_dual_stripline_is_never_better_than_b() -> None:
    adjacent = Ply(0.15, 4.4)
    geom = strip(0.12, (adjacent, sl.dielectric_copper_ply(T_HALF_OZ, adjacent), Ply(0.1, 4.4)),
                 (Ply(0.2652, 4.4),), kind="dual_stripline", dielectric_copper=(("In3.Cu", 4.4),))
    assert sl.asymmetry(geom) == pytest.approx(0.5, abs=1e-9)
    out = tline.evaluate(geom)
    assert out.accuracy_class == "B"
    nearly = strip(0.12, geom.below, (Ply(0.255, 4.4),), kind="dual_stripline",
                   dielectric_copper=(("In3.Cu", 4.4),))        # heights 3.9 % apart
    assert tline.evaluate(nearly).accuracy_class == "B"
    lopsided = strip(0.12, geom.below, (Ply(0.22, 4.4),), kind="dual_stripline",
                     dielectric_copper=(("In3.Cu", 4.4),))      # a 0.454: offset A, dual C
    assert tline.evaluate(lopsided).accuracy_class == "C"


def test_dual_stripline_rejects_dielectric_copper_with_a_foreign_er() -> None:
    adjacent = Ply(0.2, 4.4)
    geom = strip(0.12, (adjacent, sl.dielectric_copper_ply(T_HALF_OZ, adjacent)),
                 (Ply(0.3, 4.4),), kind="dual_stripline", dielectric_copper=(("In3.Cu", 3.0),))
    with pytest.raises(ValueError, match="In3.Cu"):
        tline.evaluate(geom)


def test_dual_stripline_without_dielectric_copper_says_so() -> None:
    out = tline.evaluate(strip(0.12, (Ply(0.4, 4.4),), (Ply(0.3, 4.4),), kind="dual_stripline"))
    assert any("dielectric_copper" in w for w in out.warnings)


# ---------------------------------------------------------------- monotonicity and errors
@pytest.mark.parametrize("geom_of", [
    lambda w: centred(w, 0.5, 4.2),
    lambda w: offset(w, 0.1088, 4.16, 0.55, 4.6),
    jlc6_2116_in2,
])
def test_impedance_falls_monotonically_with_width(geom_of: LineOf) -> None:
    widths = [0.03 + 0.002 * i for i in range(600)]
    zs = [tline.evaluate(geom_of(w)).z0 for w in widths]
    assert all(b < a for a, b in pairwise(zs))


def test_geometry_errors_are_loud() -> None:
    with pytest.raises(ValueError, match="above"):
        tline.evaluate(strip(0.15, (Ply(0.3, 4.4),), ()))
    with pytest.raises(ValueError, match="below"):
        tline.evaluate(strip(0.15, (), (Ply(0.3, 4.4),)))
    with pytest.raises(ValueError, match="width"):
        tline.evaluate(centred(0.0, 0.5, 4.2))


def test_ignored_inputs_are_reported() -> None:
    out = tline.evaluate(strip(0.15, (Ply(0.3, 4.4),), (Ply(0.3, 4.4),), etch=0.01,
                               mask=MaskModel(3.8, 0.0152, 0.0305, 0.0305)))
    assert any("etch" in w for w in out.warnings)
    assert any("mask" in w for w in out.warnings)
    cop = tline.evaluate(strip(0.15, (Ply(0.3, 4.4),), (Ply(0.3, 4.4),), ground_gap=0.2))
    assert cop.accuracy_class == "C" and any("coplanar" in w for w in cop.warnings)


def test_outside_wheeler_validity_is_class_c() -> None:
    out = tline.evaluate(centred(6.0, 0.5, 4.2))                  # W/(b-t) = 12.4 > 10
    assert out.accuracy_class == "C" and any("validity" in w for w in out.warnings)


def test_pair_z0_is_one_convention_in_the_closed_form_and_the_fd_tier() -> None:
    """One pair convention for D7-07's TARGET_ATTR comparisons (D7-05 review 2: the tiers once
    differed by 20-40 %): z0 = sqrt(z_even z_odd) in pair_line_result and in
    fieldsolve2d.FdResult (D7-06), so the tiers' z0 and z_diff agree within the closed form's
    estimate (DSL100_sym at W_FD: FD Zdiff 100 ohm)."""
    h = (0.5 - T_HALF_OZ) / 2
    geom = strip(0.12702164431735333, (Ply(h, 4.2),), (Ply(h, 4.2),), pair="edge", gap=0.15)
    out = tline.evaluate(geom)
    assert sl.pair_line_result(72.0, 50.0, 3.0, model="m", verdict=Verdict("B", 1.0)).z0 == 60.0
    fd2d = pytest.importorskip("fieldsolve2d")
    if fd2d.available() == "none":
        pytest.skip("fieldsolve2d: no numpy backend")
    res = fd2d.solve(geom, grid="medium")
    for r in (out, res):
        assert r.z0 == pytest.approx(math.sqrt(r.z_even * r.z_odd), rel=1e-12)
        assert r.z_diff == pytest.approx(2 * r.z_odd, rel=1e-12)
    assert res.z_diff == pytest.approx(100.0, rel=0.005)
    assert abs(out.z0 / res.z0 - 1) * 100 <= out.est_error_pct
    assert abs(out.z_diff / res.z_diff - 1) * 100 <= out.est_error_pct


def test_registered_kinds() -> None:
    keys = tline.registered()
    for key in [("stripline", "single"), ("stripline", "edge"), ("dual_stripline", "single"),
                ("dual_stripline", "edge"), ("stripline", "broadside"), ("microstrip", "edge")]:
        assert key in keys
    assert {"stripline", "broadside", "coupled_microstrip"} <= set(tline.LOADED)


def test_models_import_and_evaluate_under_kicad_python() -> None:
    """tline (with the D7-05 models) runs in KiCad's bundled Python (9.0.4 ships 3.11.5)."""
    py = tc.kicad_python()
    if py is None:
        pytest.skip(f"no KiCad bundled Python: {tc.kicad_python_error()}")
    code = ("import sys; sys.path.insert(0, sys.argv[1]); import kicad_quiet; import tline\n"
            "from tline.types import LineGeometry, Ply\n"
            "def g(kind, pair, w, below, above, gap=None, t=0.0152):\n"
            "    return LineGeometry(kind=kind, pair=pair, width=w, t=t, below=below,\n"
            "                        above=above, pair_gap=gap)\n"
            "h = (0.5 - 0.0152) / 2\n"
            "print(tline.LOADED)\n"
            "print(round(tline.evaluate(g('stripline', 'single', 0.15, (Ply(h, 4.2),),\n"
            "                             (Ply(h, 4.2),))).z0, 6),\n"
            "      round(tline.evaluate(g('stripline', 'edge', 0.127, (Ply(h, 4.2),),\n"
            "                             (Ply(h, 4.2),), 0.15)).z_diff, 6),\n"
            "      round(tline.evaluate(g('stripline', 'broadside', 0.1157, (Ply(0.2028, 4.4),\n"
            "            Ply(0.4, 4.6)), (Ply(0.4, 4.6),), 0.2028)).z_diff, 6),\n"
            "      round(tline.evaluate(g('microstrip', 'edge', 0.2816, (Ply(0.2104, 4.4),),\n"
            "                             (), 0.15, 0.035)).z_diff, 6))\n")
    proc = subprocess.run([py, "-c", code, str(TOOLS)], stdin=subprocess.DEVNULL,
                          capture_output=True, encoding="utf-8", errors="replace",
                          timeout=KICAD_PY_TIMEOUT_S, check=False)
    assert proc.returncode == 0, proc.stderr
    loaded, values = proc.stdout.strip().splitlines()[-2:]
    assert "'stripline', 'broadside', 'coupled_microstrip'" in loaded
    z_sl, zd_sl, zd_bsl, zd_ms = (float(x) for x in values.split())
    h = (0.5 - T_HALF_OZ) / 2
    assert z_sl == pytest.approx(tline.evaluate(centred(0.15, 0.5, 4.2)).z0, abs=2e-6)
    edge = strip(0.127, (Ply(h, 4.2),), (Ply(h, 4.2),), pair="edge", gap=0.15)
    assert zd_sl == pytest.approx(tline.evaluate(edge).z_diff, abs=2e-6)
    assert 97.0 < zd_bsl < 103.0 and 87.3 < zd_ms < 92.7
