"""Microstrip family of the tline package (ADR D7.6 / D7.14, task D7-04).

Vectors (mm, ohm):
- D7.14 rows: the closed form evaluated at the FD-synthesised width W_FD of [R-imp] must hit
  the target within the class tolerance (A 0.5 ohm, B 1.5 ohm);
- independent Hammerstad-Jensen 50 ohm widths from the D7 critique [C];
- prototype pins: the research closed forms (scratch tlcalc.py, recorded with the FD runs) to
  1e-9, so the port reproduces exactly the numbers the D7.6 accuracy table was measured with;
- Z0(f) dispersion row (critique disp.py, scikit-rf 2.x MLine) to +/- 0.15 points;
- KJ 1982 eeff(f) as recorded from scikit-rf 2.x (research skrf.json).
The scikit-rf comparison itself is opt-in; from the plugin root run, as ONE command line,
    uv run --no-project --with scikit-rf --with pytest pytest tests/test_tline_microstrip.py
    -m oracle
"""
from __future__ import annotations

import math
from itertools import pairwise

import pytest

import tline
from tline.elliptic import solve_monotone
from tline.types import C0, LineGeometry, MaskModel, Ply

T_1OZ = 0.035
JLC_MASK = MaskModel(er=3.8, over_trace=0.0152, over_substrate=0.0305, between=0.0305)  # D7.14
RESEARCH_MASK = MaskModel(er=3.8, over_trace=0.01524, over_substrate=0.03048, between=0.03048)
ORACLE_FREQS = (0.868e9, 0.915e9, 1.575e9, 2.45e9, 5.8e9)


def ms(width: float, h: float = 0.2104, er: float = 4.4, *, t: float = T_1OZ,
       mask: MaskModel | None = None, below: tuple[Ply, ...] | None = None,
       above: tuple[Ply, ...] = (), kind: str = "microstrip", etch: float = 0.0) -> LineGeometry:
    return LineGeometry(kind=kind, pair="single", width=width, t=t,
                        below=below if below is not None else (Ply(h, er),), above=above,
                        mask=mask, etch=etch)


def z_at(geom: LineGeometry, freq_hz: float = 0.0) -> float:
    return tline.evaluate(geom, freq_hz).z0


def width_for(z_target: float, h: float, er: float) -> float:
    return solve_monotone(lambda w: z_at(ms(w, h, er)), z_target, 0.02, 5.0)


def dz_pct(geom: LineGeometry, freq_hz: float) -> float:
    return 100.0 * (z_at(geom, freq_hz) / z_at(geom) - 1.0)


# ---------------------------------------------------------------- D7.14 rows
@pytest.mark.parametrize("name, w_fd, h, er, mask, tol, cls", [
    ("MS50_7628", 0.3721, 0.2104, 4.4, None, 0.5, "A"),
    ("MS50_7628_mask", 0.3480, 0.2104, 4.4, JLC_MASK, 1.5, "B"),
    ("MS50_RO4350B", 1.0724, 0.508, 3.66, None, 0.5, "A"),
    ("MS50_RO4350B_mask", 1.0398, 0.508, 3.66, JLC_MASK, 1.5, "B"),
])
def test_d714_microstrip_rows(name, w_fd, h, er, mask, tol, cls):
    out = tline.evaluate(ms(w_fd, h, er, mask=mask))
    assert out.z0 == pytest.approx(50.0, abs=tol), name
    assert out.accuracy_class == cls
    assert "hj80" in out.model
    assert ("wh00" in out.model) == (mask is not None)
    assert out.z_even is None and out.z_odd is None and out.z_diff is None and out.z_comm is None
    assert out.est_error_pct <= (1.0 if cls == "A" else 3.0)


@pytest.mark.parametrize("h, er, w50", [
    (0.2104, 4.4, 0.3726),      # MS50_7628
    (0.0994, 4.1, 0.175),       # MS50_3313 (JLC04161H-3313; not 0.20, critique E9)
    (0.254, 3.66, 0.5231),      # MS50_RO4350B_0254
    (0.508, 3.66, 1.0746),      # RO4350B 0.020"
])
def test_independent_hammerstad_jensen_widths(h, er, w50):
    assert width_for(50.0, h, er) == pytest.approx(w50, abs=0.002)


def test_todays_0405_width_on_7628_is_documented_as_47_6_ohm():
    """impedance.py's 0.405 mm 50-ohm width on JLC 7628 really is ~47.6 ohm bare [R-imp]."""
    assert z_at(ms(0.405)) == pytest.approx(47.6, abs=1.0)


# ---------------------------------------------------------------- prototype pins
@pytest.mark.parametrize("w, h, er, mask, z, eeff", [
    (0.37211773470044124, 0.2104, 4.4, None, 50.000000569308206, 3.1875284897241927),
    (0.37211773470044124, 0.2104, 4.4, RESEARCH_MASK, 47.95659507791031, 3.46495351451199),
    (1.0733579231798647, 0.508, 3.66, None, 49.99999939200222, 2.806200771635137),
    (1.0733579231798647, 0.508, 3.66, RESEARCH_MASK, 48.47691738889029, 2.9853052634723514),
])
def test_port_reproduces_the_research_closed_forms(w, h, er, mask, z, eeff):
    out = tline.evaluate(ms(w, h, er, mask=mask))
    assert out.z0 == pytest.approx(z, rel=1e-9)
    assert out.eeff == pytest.approx(eeff, rel=1e-9)


def test_coated_microstrip_error_vs_fd_matches_the_adr_table():
    """D7.6: coated microstrip -0.66 % (7628) / -1.22 % (RO4350B) vs FD (class B)."""
    for w, h, er, fd in [(0.37211773470044124, 0.2104, 4.4, 48.27632959654639),
                         (1.0733579231798647, 0.508, 3.66, 49.07793610360239)]:
        out = tline.evaluate(ms(w, h, er, mask=RESEARCH_MASK))
        assert abs(out.z0 / fd - 1.0) * 100 <= out.est_error_pct + 1e-6


@pytest.mark.parametrize("h, er, u, fd", [
    (0.2104, 4.4, 0.4, 86.0255), (0.2104, 4.4, 0.7, 72.5162), (0.2104, 4.4, 0.95, 64.606),
    (0.508, 3.66, 0.4, 100.5548), (0.508, 3.66, 0.7, 83.4093), (0.508, 3.66, 0.95, 73.723),
])
def test_coated_narrow_strips_stay_class_b_against_fd(h, er, u, fd):
    """D7-04 FD check (research solver, medium grid, research mask 1.2/0.6/1.2 mil): below
    u = 1 the coated line is 0.9-1.9 % low; the Wan-Hoorfar narrow-strip branch would be
    2.1-3.2 % low and is not used."""
    out = tline.evaluate(ms(u * h, h, er, mask=RESEARCH_MASK))
    assert out.accuracy_class == "B"
    assert abs(out.z0 / fd - 1.0) * 100 <= out.est_error_pct


@pytest.mark.parametrize("h, cover, u, fd", [
    (0.2, 0.1, 0.4, 83.5078), (0.2, 0.1, 0.7, 70.2627), (0.2, 0.1, 0.95, 62.6773),
    (0.2104, 0.2, 0.6, 72.0288),
])
def test_embedded_narrow_strips_stay_class_b_against_fd(h, cover, u, fd):
    """D7-04 FD check (research solver, medium grid; 0.0152 mm copper, cover measured from the
    substrate surface): -0.5..-1.0 % below u = 1."""
    geom = ms(u * h, h, 4.4, t=0.0152, above=(Ply(cover - 0.0152, 4.4),),
              kind="embedded_microstrip")
    out = tline.evaluate(geom)
    assert out.accuracy_class == "B"
    assert abs(out.z0 / fd - 1.0) * 100 <= out.est_error_pct


@pytest.mark.parametrize("h", [0.0994, 0.2104, 0.508, 1.51])
def test_coated_and_embedded_impedance_fall_monotonically_through_u_1(h):
    """Synthesis bisects Z(w): it must not step up where the filling-factor form changes."""
    widths = [h * (0.05 + 0.002 * i) for i in range(1500)]
    for kind, above, mask in [("microstrip", (), JLC_MASK),
                              ("embedded_microstrip", (Ply(0.1, 4.4),), None)]:
        zs = [z_at(ms(w, h, 4.4, t=0.0152, above=above, mask=mask, kind=kind)) for w in widths]
        assert all(b < a for a, b in pairwise(zs)), (kind, h)


def test_embedded_microstrip_matches_the_research_case():
    """[R-imp] T3: 0.3 mm trace (0.0152 copper) on 0.2 mm Er 4.4, 0.1 mm cover from the
    substrate surface, no top plane: Wan-Hoorfar 50.824 vs FD 51.198 (-0.73 %, class B)."""
    geom = ms(0.3, 0.2, 4.4, t=0.0152, above=(Ply(0.1 - 0.0152, 4.4),),
              kind="embedded_microstrip")
    out = tline.evaluate(geom)
    assert out.z0 == pytest.approx(50.824402005856946, rel=1e-9)
    assert out.accuracy_class == "B" and "embedded" in out.model
    assert abs(out.z0 / 51.19757697343644 - 1.0) * 100 <= out.est_error_pct + 1e-6
    bare = tline.evaluate(ms(0.3, 0.2, 4.4, t=0.0152))
    assert bare.z0 == pytest.approx(55.939334606265625, rel=1e-9)
    assert out.eeff > bare.eeff


def test_multi_cover_plies_add_up_like_one_cover():
    one = ms(0.3, 0.2, 4.4, t=0.0152, above=(Ply(0.0848, 4.4),), kind="embedded_microstrip")
    two = ms(0.3, 0.2, 4.4, t=0.0152, above=(Ply(0.05, 4.4), Ply(0.0348, 4.4)),
             kind="embedded_microstrip")
    assert z_at(two) == pytest.approx(z_at(one), rel=1e-12)
    air = ms(0.3, 0.2, 4.4, t=0.0152, above=(Ply(0.0848, 1.0),), kind="embedded_microstrip")
    assert z_at(air) == pytest.approx(z_at(ms(0.3, 0.2, 4.4, t=0.0152)), rel=1e-12)


def test_high_contrast_reference_stack_is_class_c_with_the_series_mean():
    """[R-imp] T1: plies 3.0 / 4.6 under a 0.3 mm line: series-mean H&J 59.199 vs FD 60.370
    and 57.664 (-1.9 / +2.7 %); contrast > 10 % -> class C (D7.6 fallback table)."""
    for below in [(Ply(0.1, 3.0), Ply(0.1, 4.6)), (Ply(0.1, 4.6), Ply(0.1, 3.0))]:
        out = tline.evaluate(ms(0.3, below=below))
        assert out.z0 == pytest.approx(59.19873665206249, rel=1e-9)
        assert out.accuracy_class == "C"
        assert any("contrast" in w for w in out.warnings)
        for fd in (60.37027781348869, 57.66361786382162):
            assert abs(out.z0 / fd - 1.0) * 100 <= 3.0


def test_low_contrast_reference_stack_uses_the_series_mean_and_keeps_its_class():
    """JLC06161H-2116 outer layer: 2116 0.127 @ 4.16 + 2313 0.0964 @ 4.31 (contrast 3.6 %)."""
    below = (Ply(0.127, 4.16), Ply(0.0964, 4.31))
    h = 0.127 + 0.0964
    er = h / (0.127 / 4.16 + 0.0964 / 4.31)
    out = tline.evaluate(ms(0.4, below=below))
    assert out.z0 == pytest.approx(z_at(ms(0.4, h, er)), rel=1e-12)
    assert out.accuracy_class == "A" and "series" in out.model
    assert out.est_error_pct <= 1.0


# ---------------------------------------------------------------- dispersion
GEOMETRIES = [ms(0.3726), ms(0.348, mask=JLC_MASK), ms(2.80, 1.51, 4.5), ms(4.85, 1.575, 2.2),
              ms(1.0724, 0.508, 3.66), ms(0.3, 0.2, 4.4, t=0.0152, above=(Ply(0.0848, 4.4),),
                                          kind="embedded_microstrip")]


@pytest.mark.parametrize("geom", GEOMETRIES)
def test_eeff_of_f_is_monotone_and_bounded(geom):
    static = tline.evaluate(geom)
    assert static.eeff_f == static.eeff
    er_max = max(p.er for p in geom.below)
    freqs = [0.0] + [k * 0.5e9 for k in range(1, 41)]
    eeffs = [tline.evaluate(geom, f).eeff_f for f in freqs]
    assert all(b >= a for a, b in pairwise(eeffs))
    assert eeffs[-1] > eeffs[0] and eeffs[-1] < max(er_max, static.eeff) + 1e-12


def test_static_result_has_no_dispersion_tags():
    out = tline.evaluate(ms(0.3721))
    assert out.model == "hj80"
    assert out.delay_ps_per_mm == pytest.approx(math.sqrt(out.eeff) * 1e9 / C0, rel=1e-12)


def test_z0_of_f_only_above_2_ghz_mm():
    """JK 1983 Z0(f) is applied only when f*h > 2 GHz*mm (critique E7); KJ eeff(f) always."""
    thin = ms(0.3721)                                     # h 0.2104: f*h = 1.22 at 5.8 GHz
    out = tline.evaluate(thin, 5.8e9)
    assert out.z0 == tline.evaluate(thin).z0
    assert "kj82" in out.model and "jk83" not in out.model
    thick = ms(2.80, 1.51, 4.5)
    below, above = tline.evaluate(thick, 1.3e9), tline.evaluate(thick, 1.4e9)   # 1.96 / 2.11
    assert below.z0 == tline.evaluate(thick).z0 and "jk83" not in below.model
    assert above.z0 != tline.evaluate(thick).z0 and "jk83" in above.model
    assert above.delay_ps_per_mm == pytest.approx(math.sqrt(above.eeff_f) * 1e9 / C0)


@pytest.mark.parametrize("w, h, er, d245, d58", [
    (0.3726, 0.2104, 4.4, -0.01, -0.04),     # JLC 7628
    (2.80, 1.51, 4.5, 0.03, 1.28),           # 2L FR-4
    (4.85, 1.575, 2.2, 0.04, 0.90),          # RT/duroid 5880
])
def test_d714_z0_dispersion_row(w, h, er, d245, d58):
    """ADR D7.14: Z0(f)/Z0(0) - 1 <= 0.04 % at 2.45 GHz; +1.28 / +0.90 / -0.04 % at 5.8 GHz
    (scikit-rf MLine, t 0.035), each within 0.15 percentage points. The ADR prints two
    decimals (RT/duroid is +0.044 % at 2.45 GHz), hence the 0.045 bound."""
    geom = ms(w, h, er)
    assert abs(dz_pct(geom, 2.45e9)) < 0.045
    assert dz_pct(geom, 2.45e9) == pytest.approx(d245, abs=0.15)
    assert dz_pct(geom, 5.8e9) == pytest.approx(d58, abs=0.15)


@pytest.mark.parametrize("w, h, er, eeffs, z58", [
    (0.37211773470044124, 0.2104, 4.4,
     (3.1881482875242924, 3.1882011740484604, 3.1890848173101265, 3.190582243399386,
      3.1984914982556476), None),
    (1.0733579231798647, 0.508, 3.66,
     (2.807872909960007, 2.8080124884760465, 2.8102966760949055, 2.8140168141721955,
      2.832122470287182), 49.99192090161042),
])
def test_kj82_eeff_matches_recorded_scikit_rf(w, h, er, eeffs, z58):
    """scikit-rf 2.x MLine (qucs mode) values recorded in the [R-imp] run."""
    for f, expected in zip(ORACLE_FREQS, eeffs, strict=True):
        assert tline.evaluate(ms(w, h, er), f).eeff_f == pytest.approx(expected, rel=1e-9)
    if z58 is not None:                                  # f*h = 2.95 GHz*mm: JK83 applies
        assert z_at(ms(w, h, er), 5.8e9) == pytest.approx(z58, rel=1e-8)


def test_coated_line_disperses_and_stays_class_b():
    out = tline.evaluate(ms(0.348, mask=JLC_MASK), 2.45e9)
    assert out.accuracy_class == "B"
    assert out.eeff_f > out.eeff


# ---------------------------------------------------------------- validity and errors
def test_geometry_errors_are_loud():
    with pytest.raises(ValueError, match="width"):
        tline.evaluate(ms(0.0))
    with pytest.raises(ValueError, match="below"):
        tline.evaluate(ms(0.3, below=()))


def test_etch_is_reported_not_silently_modelled():
    out = tline.evaluate(ms(0.3721, etch=0.0125))
    assert out.z0 == z_at(ms(0.3721))
    assert any("etch" in w for w in out.warnings)


def test_outside_published_validity_is_class_c():
    wide = tline.evaluate(ms(30.0, 0.2, 4.4))                 # u = 150 > 100 (H&J)
    assert wide.accuracy_class == "C" and any("validity" in w for w in wide.warnings)
    hi_er = tline.evaluate(ms(0.1, 0.2, 25.0), 1e9)            # KJ dispersion: Er <= 20
    assert hi_er.accuracy_class == "C" and any("kj82" in w for w in hi_er.warnings)
    ok = tline.evaluate(ms(0.3721), 5.8e9)
    assert ok.accuracy_class == "A" and ok.warnings == ()


# ---------------------------------------------------------------- opt-in oracle
ORACLE_CASES = [(0.3721, 0.2104, 4.4), (1.0724, 0.508, 3.66), (2.80, 1.51, 4.5),
                (4.85, 1.575, 2.2), (0.175, 0.0994, 4.1)]


@pytest.mark.oracle
@pytest.mark.parametrize("w, h, er", ORACLE_CASES)
def test_oracle_scikit_rf_mline(request, w, h, er):
    """D7.14 'scikit-rf MLine' row: Z0 and eeff(f) agree to 0.1 % (opt-in, -m oracle)."""
    if "oracle" not in (request.config.option.markexpr or ""):
        pytest.skip("opt-in: run with -m oracle (needs scikit-rf, see the module docstring)")
    skrf = pytest.importorskip("skrf")
    import numpy as np
    from skrf.media import MLine

    freqs = [1e6, *ORACLE_FREQS]
    line = MLine(frequency=skrf.Frequency.from_f(freqs, unit="Hz"), w=w * 1e-3, h=h * 1e-3,
                 t=T_1OZ * 1e-3, ep_r=er, rho=1e-12, tand=0.0, rough=0.0,
                 model="hammerstadjensen", disp="kirschningjansen", diel="frequencyinvariant",
                 compatibility_mode="qucs")
    z_ref = np.real(np.atleast_1d(line.z0))
    e_ref = np.real(np.atleast_1d(line.ep_reff_f))
    static = tline.evaluate(ms(w, h, er))
    assert static.eeff == pytest.approx(float(np.real(np.atleast_1d(line.ep_reff)[0])), rel=1e-3)
    assert static.z0 == pytest.approx(float(z_ref[0]), rel=1e-3)
    for i, f in enumerate(ORACLE_FREQS, start=1):
        ours = tline.evaluate(ms(w, h, er), f)
        assert ours.z0 == pytest.approx(float(z_ref[i]), rel=1e-3), f
        assert ours.eeff_f == pytest.approx(float(e_ref[i]), rel=1e-3), f
