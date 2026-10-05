"""Coplanar lines of the tline package (ADR D7.6 / D7.14, task D7-04).

CPWG: Ghione-Naldi conductor-backed CPW (t = 0) evaluated at the EMPIRICAL, FD-calibrated
thickness mapping W + du1*h, S - du1*h/2 [R-imp]; with solder mask a crude gap-fill term
(class B). CPW without a back plane: the same conformal mapping on a finite substrate; finite
coplanar grounds make it class C (D7.6 fallback table).

Vectors: the D7.14 rows (closed form at W_FD within 0.5 / 1.5 ohm), the research closed
forms recorded with the FD runs (prototype pins to 1e-9), the D7.14 e2e bare width, and
conformal-mapping limits that hold exactly.
"""
from __future__ import annotations

import math
from itertools import pairwise

import pytest

import tline
from tline.elliptic import k_ratio, solve_monotone
from tline.types import C0, ETA0, LineGeometry, MaskModel, Ply

T_1OZ = 0.035
JLC_MASK = MaskModel(er=3.8, over_trace=0.0152, over_substrate=0.0305, between=0.0305)  # D7.14
RESEARCH_MASK = MaskModel(er=3.8, over_trace=0.01524, over_substrate=0.03048, between=0.03048)


def cp(width: float, h: float, er: float, gap: float | None, *, t: float = T_1OZ,
       mask: MaskModel | None = None, kind: str = "cpwg", ground_width: float | None = None,
       below: tuple[Ply, ...] | None = None, above: tuple[Ply, ...] = (),
       etch: float = 0.0) -> LineGeometry:
    return LineGeometry(kind=kind, pair="single", width=width, t=t,
                        below=below if below is not None else (Ply(h, er),), above=above,
                        ground_gap=gap, ground_width=ground_width, mask=mask, etch=etch)


def z_at(geom: LineGeometry, freq_hz: float = 0.0) -> float:
    return tline.evaluate(geom, freq_hz).z0


# ---------------------------------------------------------------- D7.14 rows
@pytest.mark.parametrize("name, w_fd, h, er, gap, mask, tol, cls", [
    ("CPWG50_2L", 1.0124, 1.53, 4.5, 0.2, None, 0.5, "A"),
    ("CPWG50_7628", 0.2957, 0.2104, 4.4, 0.15, None, 0.5, "A"),
    ("CPWG50_7628_mask", 0.2567, 0.2104, 4.4, 0.15, JLC_MASK, 1.5, "B"),
])
def test_d714_cpwg_rows(name, w_fd, h, er, gap, mask, tol, cls):
    out = tline.evaluate(cp(w_fd, h, er, gap, mask=mask))
    assert out.z0 == pytest.approx(50.0, abs=tol), name
    assert out.accuracy_class == cls
    assert "empirical" in out.model and "FD-calibrated" in out.model
    assert ("mask" in out.model) == (mask is not None)
    assert out.delay_ps_per_mm == pytest.approx(math.sqrt(out.eeff_f) * 1e9 / C0, rel=1e-12)


@pytest.mark.parametrize("w, h, er, gap, mask, z_rec, z_fd", [
    (1.0313586343824857, 1.53, 4.5, 0.2, None, 49.74396337212964, 49.7337237908919),
    (0.3087405438721179, 0.2104, 4.4, 0.15, None, 48.91568097432342, 48.99797391853099),
    (0.3087405438721179, 0.2104, 4.4, 0.15, RESEARCH_MASK, 46.83977917692572,
     46.17306031611892),
])
def test_port_reproduces_the_research_closed_forms(w, h, er, gap, mask, z_rec, z_fd):
    out = tline.evaluate(cp(w, h, er, gap, mask=mask))
    assert out.z0 == pytest.approx(z_rec, rel=1e-9)
    # and the error vs FD is inside the D7.6 table figure (+/-0.9 % A; +1.0..+1.9 % B)
    assert abs(out.z0 / z_fd - 1.0) * 100 <= out.est_error_pct + 1e-6


def test_e2e_bare_cpwg_width_on_ro4350b():
    """D7.14 e2e table: RO4350B 0.254 mm, Dk 3.66, gap 0.2, 1 oz: bare closed form 0.442."""
    w = solve_monotone(lambda x: z_at(cp(x, 0.254, 3.66, 0.2)), 50.0, 0.05, 3.0)
    assert w == pytest.approx(0.442, abs=0.002)


# ---------------------------------------------------------------- conformal identities
def test_cpw_in_air_with_k_one_over_root_two_is_eta0_over_4():
    """K(k')/K(k) = 1 at k = 1/sqrt(2): an air CPW (t = 0) is exactly eta0/4."""
    w = 1.0
    gap = w / 2 * (math.sqrt(2.0) - 1.0)
    out = tline.evaluate(cp(w, 1000.0, 1.0, gap, t=0.0, kind="cpw"))
    assert out.z0 == pytest.approx(ETA0 / 4, rel=1e-12)
    assert out.eeff == pytest.approx(1.0, rel=1e-12)


def test_thick_substrate_limit_cpwg_equals_cpw_equals_half_space():
    w, gap, er, h = 0.4, 0.2, 4.4, 1e4
    a, b = w / 2, w / 2 + gap
    q0 = k_ratio(a / b)
    z_half = ETA0 / (2 * q0 * math.sqrt(2 * (er + 1)))
    for kind in ("cpwg", "cpw"):
        out = tline.evaluate(cp(w, h, er, gap, t=0.0, kind=kind))
        assert out.eeff == pytest.approx((er + 1) / 2, rel=1e-9), kind
        assert out.z0 == pytest.approx(z_half, rel=1e-9), kind


def test_back_plane_lowers_impedance():
    thin = z_at(cp(0.3, 0.2104, 4.4, 0.15))
    no_back = z_at(cp(0.3, 0.2104, 4.4, 0.15, kind="cpw"))
    assert thin < no_back


@pytest.mark.parametrize("kind", ["cpwg", "cpw"])
@pytest.mark.parametrize("h", [0.0994, 0.2104, 1.53])
def test_impedance_falls_monotonically_over_the_synthesis_width_range(kind, h):
    """Synthesis bisects width between the fab minimum and 5 mm: Z(w) must be strictly
    monotone and finite there, bare and masked (wide lines on thin dielectric included)."""
    widths = [0.05 + 0.005 * i for i in range(991)]                 # 0.05 .. 5.0 mm
    for mask in (None, JLC_MASK):
        zs = [z_at(cp(w, h, 4.4, 0.1, kind=kind, mask=mask)) for w in widths]
        assert all(math.isfinite(z) for z in zs)
        assert all(b < a for a, b in pairwise(zs)), (kind, h, mask)


def test_impedance_is_monotone_in_width_and_gap():
    zs_w = [z_at(cp(w, 0.2104, 4.4, 0.15)) for w in (0.1, 0.2, 0.3, 0.5, 1.0)]
    zs_g = [z_at(cp(0.3, 0.2104, 4.4, g)) for g in (0.1, 0.15, 0.2, 0.4, 1.0)]
    assert all(a > b for a, b in pairwise(zs_w))
    assert all(a < b for a, b in pairwise(zs_g))


# ---------------------------------------------------------------- classes and warnings
def test_cpw_without_back_plane_classes():
    infinite = tline.evaluate(cp(0.3, 1.5, 4.4, 0.15, kind="cpw"))
    assert infinite.accuracy_class == "B"
    assert any("not FD-calibrated" in w for w in infinite.warnings)
    finite = tline.evaluate(cp(0.3, 1.5, 4.4, 0.15, kind="cpw", ground_width=1.0))
    assert finite.accuracy_class == "C"
    assert any("finite" in w for w in finite.warnings)
    assert finite.z0 == infinite.z0                  # the class C seed is the infinite-ground form


def test_cpwg_ground_width_does_not_change_the_closed_form():
    assert z_at(cp(0.3, 0.2104, 4.4, 0.15, ground_width=1.0)) == z_at(cp(0.3, 0.2104, 4.4, 0.15))
    assert tline.evaluate(cp(0.3, 0.2104, 4.4, 0.15, ground_width=1.0)).accuracy_class == "A"


def test_covered_coplanar_line_is_class_c():
    out = tline.evaluate(cp(0.3, 0.2104, 4.4, 0.15, above=(Ply(0.1, 4.4),)))
    assert out.accuracy_class == "C" and any("covered" in w for w in out.warnings)


def test_high_contrast_substrate_is_class_c():
    out = tline.evaluate(cp(0.3, 0.2, 4.4, 0.15, below=(Ply(0.1, 3.0), Ply(0.1, 4.6))))
    assert out.accuracy_class == "C" and any("contrast" in w for w in out.warnings)


def test_dispersion_is_not_modelled_and_says_so_above_2_ghz_mm():
    geom = cp(1.0124, 1.53, 4.5, 0.2)
    low = tline.evaluate(geom, 0.915e9)                 # f*h = 1.40 GHz*mm
    assert low.eeff_f == low.eeff and low.warnings == ()
    high = tline.evaluate(geom, 2.45e9)                 # f*h = 3.75 GHz*mm
    assert high.eeff_f == high.eeff and high.z0 == low.z0
    assert any("dispersion" in w for w in high.warnings)


def test_masked_cpw_is_lower_than_bare():
    bare = tline.evaluate(cp(0.3, 1.5, 4.4, 0.15, kind="cpw"))
    coated = tline.evaluate(cp(0.3, 1.5, 4.4, 0.15, kind="cpw", mask=JLC_MASK))
    assert coated.z0 < bare.z0 and coated.eeff > bare.eeff


def test_etch_is_reported():
    out = tline.evaluate(cp(0.3, 0.2104, 4.4, 0.15, etch=0.01))
    assert any("etch" in w for w in out.warnings)


# ---------------------------------------------------------------- errors
@pytest.mark.parametrize("gap", [None, 0.0])
def test_missing_ground_gap_is_an_error(gap):
    with pytest.raises(ValueError, match="ground_gap"):
        tline.evaluate(cp(0.3, 0.2104, 4.4, gap))


def test_gap_too_small_for_the_thickness_mapping_is_an_error():
    with pytest.raises(ValueError, match="gap"):
        tline.evaluate(cp(0.3, 0.2104, 4.4, 0.01))


def test_zero_width_is_an_error():
    with pytest.raises(ValueError, match="width"):
        tline.evaluate(cp(0.0, 0.2104, 4.4, 0.15))
