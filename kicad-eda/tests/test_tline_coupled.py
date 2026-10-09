"""Coupled lines of the tline package (ADR D7.6 / D7.14, task D7-05).

Kinds: ('stripline', 'edge') / ('dual_stripline', 'edge') - Cohn 1955 exact at t = 0, at the
EMPIRICAL Wheeler-dW mapping (w + dW, s - dW, b - t), offset lines by the per-mode image split;
('stripline', 'broadside') - Cohn 1960 with the face gap and dW; ('microstrip', 'edge') -
Kirschning-Jansen 1984 at t = 0 on the EMPIRICAL mapping w + d, s - d,
d = du_r + 0.2 (du1 - du_r) [R-imp].

Pair results (stripline.pair_line_result; fieldsolve2d.FdResult uses the same, D7-06):
z0 = sqrt(z_even z_odd) (the coupler Z0 of Rogers note 3.2.1), z_diff = 2 z_odd,
z_comm = z_even / 2, eeff / eeff_f / delay of the odd mode. est_error_pct refers to z_diff.

Broadside validity envelope (D7-05 rework): Cohn 1960 holds for close coupling only; an FD
sweep over w/h_out, g/h_out and t/h_out (BSL_SWEEP) pins where class A / B hold and that every
geometry outside is class C.

Broadside geometry convention (for D7-07 geometry / D7-06 fd_sections): `above` = plies from
the upper strip to its plane (outward), `below` = the face-gap plies (summing to `pair_gap`)
followed by the plies from the lower strip to its plane; both strips are `width` x `t`.

Vectors: the D7.14 rows at W_FD (FD = target), FD grids of the research run (grid_csl,
grid_cms_*, grid_val, cpwg_bsl; scratch d7/impedance) and of this task (fd_refs.log,
fd_bsl.log: the research solver run from scratch wave2/D7-05; BSL_SWEEP), the D7-05 review's
weak-coupling FD points (BSL_WEAK_FD), the 18 Rogers design note 3.2.1 examples
(tests/fixtures/tline), and prototype pins of the research closed forms (tlcalc.py).
"""
from __future__ import annotations

import json
import math
from collections import Counter
from collections.abc import Callable
from itertools import pairwise, product
from pathlib import Path

import pytest

import tline
from tline import broadside as bs
from tline import coupled_microstrip as cm
from tline import stripline as sl
from tline.types import C0, CLASS_BOUND_PCT, ETA0, LineGeometry, MaskModel, Ply

ROOT = Path(__file__).resolve().parents[1]
ROGERS = ROOT / "tests" / "fixtures" / "tline" / "rogers_note_321.json"
INCH = 25.4
T_1OZ, T_HALF_OZ = 0.035, 0.0152
JLC_MASK = MaskModel(er=3.8, over_trace=0.0152, over_substrate=0.0305, between=0.0305)  # D7.14
D_NEPER_DB = 20.0 / math.log(10.0)
PairOf = Callable[[float, float], LineGeometry]   # (width, gap) -> geometry


def edge_sl(width: float, gap: float, h_below: float, h_above: float, er_below: float,
            er_above: float | None = None, *, t: float = T_HALF_OZ,
            kind: str = "stripline") -> LineGeometry:
    return LineGeometry(kind=kind, pair="edge", width=width, t=t,
                        below=(Ply(h_below, er_below),),
                        above=(Ply(h_above, er_below if er_above is None else er_above),),
                        pair_gap=gap)


def centred_edge(width: float, gap: float, b: float, er: float,
                 t: float = T_HALF_OZ) -> LineGeometry:
    h = (b - t) / 2
    return edge_sl(width, gap, h, h, er, t=t)


def bsl(width: float, core: float, er_core: float, gap: float, er_gap: float, *,
        t: float = T_HALF_OZ, core_below: float | None = None) -> LineGeometry:
    lower = core if core_below is None else core_below
    return LineGeometry(kind="stripline", pair="broadside", width=width, t=t,
                        above=(Ply(core, er_core),),
                        below=(Ply(gap, er_gap), Ply(lower, er_core)), pair_gap=gap,
                        layers=("In2.Cu", "In3.Cu"))


def cms(width: float, gap: float, h: float, er: float, *, t: float = T_1OZ,
        mask: MaskModel | None = None, below: tuple[Ply, ...] | None = None,
        above: tuple[Ply, ...] = ()) -> LineGeometry:
    return LineGeometry(kind="microstrip", pair="edge", width=width, t=t,
                        below=below if below is not None else (Ply(h, er),), above=above,
                        pair_gap=gap, mask=mask)


def coupling_db(z_even: float, z_odd: float) -> float:
    return -D_NEPER_DB * math.log((z_even - z_odd) / (z_even + z_odd))


def pct(value: float, ref: float) -> float:
    return 100.0 * (value / ref - 1.0)


# ---------------------------------------------------------------- D7.14 rows
@pytest.mark.parametrize("name, geom, target, tol", [
    ("DSL100_sym", centred_edge(0.12702164431735333, 0.15, 0.5, 4.2), 100.0, 3.0),
    ("DMS90_7628", cms(0.28161273198623493, 0.15, 0.2104, 4.4), 90.0, 2.7),
    ("DMS90_3313", cms(0.1573530246154307, 0.127, 0.0994, 4.1), 90.0, 2.7),
    ("BSL100", bsl(0.1156763825052941, 0.4, 4.6, 0.2028, 4.4), 100.0, 3.0),
])
def test_d714_coupled_rows_at_the_fd_width(name: str, geom: LineGeometry, target: float,
                                           tol: float) -> None:
    """Closed form at the FD-synthesised width W_FD (FD Zdiff = target) [R-imp]: class B."""
    out = tline.evaluate(geom)
    assert out.z_diff == pytest.approx(target, abs=tol), name
    assert out.accuracy_class == "B"
    assert abs(pct(out.z_diff, target)) <= out.est_error_pct
    assert out.z0 == pytest.approx(math.sqrt(out.z_even * out.z_odd), rel=1e-12)
    assert out.z_diff == pytest.approx(2 * out.z_odd, rel=1e-12)
    assert out.z_comm == pytest.approx(out.z_even / 2, rel=1e-12)
    assert out.delay_ps_per_mm == pytest.approx(math.sqrt(out.eeff_f) * 1e9 / C0, rel=1e-12)
    assert out.warnings == ()


@pytest.mark.parametrize("w, s", [(0.08943079170736003, 0.15), (0.1003147764503956, 0.15)])
def test_dsl100_off_row_is_class_c(w: float, s: float) -> None:
    """D7.14 DSL100_off (h1 0.1088 @ 4.16, h2 0.55 @ 4.6, S 0.15): class C; the per-mode
    image method reads high (+4.2 % at W_cf [R-imp]), inside its estimate."""
    out = tline.evaluate(edge_sl(w, s, 0.55, 0.1088, 4.6, 4.16))
    assert out.accuracy_class == "C"
    assert any("class C fallback/FD required" in m for m in out.warnings)
    fd = 100.0 if w < 0.09 else 95.98552794733985              # W_FD target / FD at W_cf
    assert out.z_diff > fd and pct(out.z_diff, fd) <= out.est_error_pct


def test_masked_coupled_microstrip_returns_the_bare_result_as_class_c() -> None:
    """D7.14 DMS90_7628_mask: no closed form for mask on coupled lines; the bare KJ84 result
    with class C (D7-05c fallback.apply adds the mask, FD synthesises)."""
    bare = tline.evaluate(cms(0.23492859579490222, 0.15, 0.2104, 4.4))
    out = tline.evaluate(cms(0.23492859579490222, 0.15, 0.2104, 4.4, mask=JLC_MASK))
    assert out.accuracy_class == "C"
    assert any("mask ignored - class C fallback/FD required" in m for m in out.warnings)
    assert (out.z_even, out.z_odd, out.z_diff) == (bare.z_even, bare.z_odd, bare.z_diff)
    assert out.eeff == bare.eeff
    # the bare value is the seed: FD with the mask is 90 ohm at this width, 86.80 at W 0.259
    assert out.est_error_pct >= pct(out.z_diff, 90.0)
    usb = tline.evaluate(cms(0.25898442342877387, 0.15, 0.2104, 4.4, mask=JLC_MASK))
    assert usb.est_error_pct >= pct(usb.z_diff, 86.80381239663738)


# ---------------------------------------------------------------- edge-coupled stripline
def test_cohn_coupled_exact_at_t0_matches_the_research_closed_form() -> None:
    ze, zo = sl.cohn_coupled(0.15, 0.2, 0.5, 4.2)
    assert ze == pytest.approx(70.19425692130028, rel=1e-9)
    assert zo == pytest.approx(55.582102856009996, rel=1e-9)
    far_ze, far_zo = sl.cohn_coupled(0.15, 5.0, 0.5, 4.2)    # decoupled: both -> single line
    single = sl.cohn_z0(0.15, 0.5, 4.2)
    assert far_ze == pytest.approx(single, rel=1e-6) and far_zo == pytest.approx(single, rel=1e-6)


def test_centred_edge_stripline_reproduces_the_research_mapping() -> None:
    """tlcalc.coupled_stripline_rec: Cohn exact at (w + dW, s - dW, b - t)."""
    out = tline.evaluate(centred_edge(0.13522555992007254, 0.15, 0.5, 4.2))
    assert out.z_even == pytest.approx(70.68639849863312, rel=1e-9)
    assert out.z_odd == pytest.approx(49.16503618279904, rel=1e-9)
    assert pct(out.z_diff, 97.93197602745663) == pytest.approx(0.4065029158911715, abs=1e-6)
    assert "cohn55" in out.model and "empirical" in out.model
    assert out.eeff == pytest.approx(4.2, rel=1e-12)


# research grid_csl.log (medium grid, Er 4.2, centred): b, w, s, t, Zeven, Zodd
CSL_FD = [
    (0.3, 0.06, 0.09, 0.0152, 77.053, 50.725), (0.3, 0.06, 0.09, 0.035, 68.798, 42.916),
    (0.3, 0.06, 0.18, 0.0152, 69.343, 59.292), (0.3, 0.06, 0.18, 0.035, 61.370, 51.473),
    (0.3, 0.12, 0.09, 0.0152, 56.605, 40.178), (0.3, 0.12, 0.09, 0.035, 51.194, 34.852),
    (0.3, 0.12, 0.18, 0.0152, 52.064, 45.788), (0.3, 0.12, 0.18, 0.035, 46.802, 40.565),
    (0.5, 0.1, 0.15, 0.0152, 80.351, 54.014), (0.5, 0.1, 0.15, 0.035, 74.357, 48.103),
    (0.5, 0.1, 0.3, 0.0152, 72.585, 62.528), (0.5, 0.1, 0.3, 0.035, 66.715, 56.689),
    (0.5, 0.2, 0.15, 0.0152, 58.628, 42.287), (0.5, 0.2, 0.15, 0.035, 54.886, 38.437),
    (0.5, 0.2, 0.3, 0.0152, 54.066, 47.816), (0.5, 0.2, 0.3, 0.035, 50.378, 44.097),
]


def test_edge_stripline_error_vs_fd_is_inside_the_class_b_estimate() -> None:
    """ADR D7.6: Zeven -0.73 % mean, Zodd +0.84 % mean (+1.84 % worst) on 16 points."""
    errs_e, errs_o = [], []
    for b, w, s, t, fd_e, fd_o in CSL_FD:
        out = tline.evaluate(centred_edge(w, s, b, 4.2, t=t))
        assert out.accuracy_class == "B"
        errs_e.append(pct(out.z_even, fd_e))
        errs_o.append(pct(out.z_odd, fd_o))
        assert abs(errs_o[-1]) <= out.est_error_pct and abs(errs_e[-1]) <= out.est_error_pct
    assert sum(errs_e) / 16 == pytest.approx(-0.73, abs=0.01)
    assert sum(errs_o) / 16 == pytest.approx(0.84, abs=0.01)
    assert max(errs_o, key=abs) == pytest.approx(1.84, abs=0.01)


def test_centred_edge_stripline_with_two_dielectrics_vs_fd() -> None:
    """Centred pair, 0.2424 mm @ 4.41 above / @ 4.4 below (FD medium grid, fd_refs.log)."""
    out = tline.evaluate(edge_sl(0.12, 0.15, 0.2424, 0.2424, 4.4, 4.41))
    assert out.accuracy_class == "B" and 4.4 < out.eeff < 4.41
    assert abs(pct(out.z_odd, 49.7548728731596)) <= out.est_error_pct
    assert abs(pct(out.z_even, 72.9442972998008)) <= out.est_error_pct


def test_zero_thickness_centred_edge_stripline_is_exact_class_a() -> None:
    out = tline.evaluate(centred_edge(0.15, 0.2, 0.5, 4.2, t=0.0))
    assert (out.z_even, out.z_odd) == pytest.approx(sl.cohn_coupled(0.15, 0.2, 0.5, 4.2))
    assert out.accuracy_class == "A"


def test_mapping_needs_a_gap_wider_than_dw() -> None:
    with pytest.raises(ValueError, match="gap"):
        tline.evaluate(centred_edge(0.15, 0.02, 0.3, 4.2, t=0.035))
    with pytest.raises(ValueError, match="pair_gap"):
        tline.evaluate(LineGeometry(kind="stripline", pair="edge", width=0.1, t=0.0152,
                                    below=(Ply(0.2, 4.4),), above=(Ply(0.2, 4.4),)))


def test_dual_stripline_edge_pair_is_class_c_and_names_the_layer() -> None:
    adjacent = Ply(0.2084, 4.4)
    geom = LineGeometry(kind="dual_stripline", pair="edge", width=0.1, t=T_HALF_OZ,
                        below=(adjacent, adjacent, sl.dielectric_copper_ply(T_HALF_OZ, adjacent),
                               Ply(0.3, 4.41)),
                        above=(Ply(0.3, 4.41),), pair_gap=0.15,
                        dielectric_copper=(("In3.Cu", 4.4),))
    out = tline.evaluate(geom)
    assert out.accuracy_class == "C" and "dual" in out.model
    assert any("In3.Cu" in m for m in out.warnings)
    assert out.z_diff == pytest.approx(2 * out.z_odd)
    assert out.z0 == pytest.approx(math.sqrt(out.z_even * out.z_odd))


# ---------------------------------------------------------------- broadside
def _rogers() -> dict:
    return json.loads(ROGERS.read_text(encoding="utf-8"))


def test_rogers_fixture_holds_all_18_examples() -> None:
    data = _rogers()
    assert len(data["broadside"]) == 9 and len(data["edge"]) == 9
    names = [row["substrate"] for row in data["broadside"]]
    assert names == [row["substrate"] for row in data["edge"]]
    assert set(names) <= set(data["er"])


def _rogers_broadside(row: dict, er: float) -> LineGeometry:
    outer, centre = row["outer_board_in"] * INCH, row["center_board_in"] * INCH
    return bsl(row["line_width_in"] * INCH, outer, er, centre, er, t=0.0)


def _rogers_edge(row: dict, er: float) -> LineGeometry:
    board = row["board_in"] * INCH
    return edge_sl(row["width_in"] * INCH, row["spacing_in"] * INCH, board, board, er, t=0.0)


@pytest.mark.parametrize("index", range(9))
def test_rogers_note_321_broadside_examples_exact(index: int) -> None:
    """With the note's printed constants (188.3, 296.1) Cohn 1960 reproduces Z0 and coupling
    to the printed digits; with exact eta0/2, eta0*pi/4 the model stays within 0.05 %."""
    data = _rogers()
    row = data["broadside"][index]
    out = tline.evaluate(_rogers_broadside(row, data["er"][row["substrate"]]))
    ze_r = out.z_even * 188.3 / (ETA0 / 2)
    zo_r = out.z_odd * 296.1 / (ETA0 * math.pi / 4)
    assert math.sqrt(ze_r * zo_r) == pytest.approx(row["z0_ohm"], abs=0.0055)
    assert coupling_db(ze_r, zo_r) == pytest.approx(row["coupling_db"], abs=0.0055)
    assert abs(pct(out.z0, row["z0_ohm"])) <= 0.05 + 100 * 0.005 / row["z0_ohm"]
    assert coupling_db(out.z_even, out.z_odd) == pytest.approx(row["coupling_db"], abs=0.01)
    # wide, closely coupled strips: inside Cohn's validity, the closed form is final
    assert out.accuracy_class == "A" and out.model == "cohn60"


@pytest.mark.parametrize("index", range(9))
def test_rogers_note_321_edge_examples_within_0_07_pct(index: int) -> None:
    """Cohn 1955 with eta0/4 is within 0.07 % (+ the printed digit) of the note, whose 30 pi
    prefactor is 0.069 % above eta0/4; with 30 pi it reproduces the printed digits."""
    data = _rogers()
    row = data["edge"][index]
    out = tline.evaluate(_rogers_edge(row, data["er"][row["substrate"]]))
    z0 = math.sqrt(out.z_even * out.z_odd)
    assert abs(pct(z0, row["z0_ohm"])) <= 0.07 + 100 * 0.005 / row["z0_ohm"]
    assert z0 * 30 * math.pi / (ETA0 / 4) == pytest.approx(row["z0_ohm"], abs=0.0055)
    assert coupling_db(out.z_even, out.z_odd) == pytest.approx(row["coupling_db"], abs=0.0055)


def test_broadside_width_equation_uses_the_corrected_r() -> None:
    """[R-imp]: R = sqrt(k (k - s/b) / (1 - k s/b)) (the note's eq. 3 with R as printed);
    w/b rises from 0 at k = s/b, grows without bound as k -> 1 and is inverted exactly."""
    s_b = 0.2
    z_lo = math.atanh(s_b)
    assert bs.width_ratio(z_lo, s_b) == pytest.approx(0.0, abs=1e-12)
    zs = [z_lo + 0.05 * i for i in range(1, 400)]
    widths = [bs.width_ratio(z, s_b) for z in zs]
    assert all(b > a for a, b in pairwise(widths)) and widths[-1] > 10
    for w_b in (0.05, 0.5, 2.0, 12.0):
        assert bs.width_ratio(bs.solve_z(w_b, s_b), s_b) == pytest.approx(w_b, rel=1e-9)
    k = math.tanh(0.9)
    r = math.sqrt(k * (k - s_b) / (1 - k * s_b))
    direct = 2 / math.pi * (math.atanh(r) - s_b * math.atanh(r / k))
    assert bs.width_ratio(0.9, s_b) == pytest.approx(direct, rel=1e-12)


def test_broadside_wide_strip_limits() -> None:
    """Parallel plates: Zodd -> eta0 s_b (1 - s_b) / (2 w_b), Zeven -> eta0 (1 - s_b)/(2 w_b);
    fringing only adds capacitance, so the exact values sit just below."""
    ze, zo = bs.cohn_broadside(20.0, 0.2, 1.0, 1.0)
    plate_o, plate_e = ETA0 * 0.2 * 0.8 / (2 * 20.0), ETA0 * 0.8 / (2 * 20.0)
    assert 0.95 * plate_o < zo < plate_o
    assert 0.95 * plate_e < ze < plate_e


@pytest.mark.parametrize("w, gap, core, er, ze, zo", [
    (0.3, 0.2028, 0.4, 4.55, 84.3235251808066, 29.424593628308244),
    (0.1434, 0.2028, 0.4, 4.55, 105.20122851650693, 44.200623686508116),
    (0.3, 0.1, 0.25, 3.66, 77.96534961083056, 19.98741928826643)])
def test_broadside_even_mode_is_the_research_mapping(w: float, gap: float, core: float, er: float,
                                                     ze: float, zo: float) -> None:
    """tlcalc.broadside_stripline_rec (homogeneous; constants 188.3 / 296.1): the even mode is
    that mapping; in the odd mode the near virtual ground shields the strip sidewalls, the
    weighted dW is smaller and Zodd reads higher (the research mapping was 0.7-4 % low)."""
    out = tline.evaluate(bsl(w, core, er, gap, er))
    assert out.z_even * 188.3 / (ETA0 / 2) == pytest.approx(ze, rel=1e-9)
    assert out.z_odd * 296.1 / (ETA0 * math.pi / 4) > zo
    assert out.eeff == pytest.approx(er, rel=1e-12)


def test_odd_mode_width_weights_the_outer_and_the_virtual_ground_half() -> None:
    w_even, w_odd = bs.mode_widths(0.1, 0.0152, 0.4, 0.2)
    d_out, d_in = sl.wheeler_dw(0.1, 0.8152, 0.0152), sl.wheeler_dw(0.1, 0.2152, 0.0152)
    assert w_even == pytest.approx(0.1 + d_out, rel=1e-12)
    assert w_odd == pytest.approx(0.1 + (d_out / 0.4 + d_in * 2 / 0.2) / (1 / 0.4 + 2 / 0.2))
    assert d_in < d_out and w_odd < w_even
    assert bs.mode_widths(0.1, 0.0, 0.4, 0.2) == (0.1, 0.1)


# research cpwg_bsl.log (fine grid, homogeneous, 0.5 oz): w, core, gap, er, FD Zeven, Zodd
BSL_FD = [(0.1434, 0.4, 0.2028, 4.55, 105.126, 44.884), (0.3, 0.4, 0.2028, 4.55, 83.94, 29.755),
          (0.6, 0.4, 0.2028, 4.55, 61.692, 18.273), (0.3, 0.25, 0.1, 3.66, 77.353, 20.332)]


@pytest.mark.parametrize("w, core, gap, er, fd_e, fd_o", BSL_FD)
def test_broadside_error_vs_fd_is_inside_the_class_b_estimate(w: float, core: float, gap: float,
                                                              er: float, fd_e: float,
                                                              fd_o: float) -> None:
    """ADR D7.6 (research mapping): Zeven <= 0.8 %, Zodd <= 1.7 %; with the odd-mode
    virtual-ground dW the odd mode is within 0.3 % on these four points."""
    out = tline.evaluate(bsl(w, core, er, gap, er))
    assert out.accuracy_class == "B"
    assert abs(pct(out.z_odd, fd_o)) <= 0.3 and abs(pct(out.z_even, fd_e)) <= 0.85
    assert abs(pct(out.z_odd, fd_o)) <= out.est_error_pct


# D7-05 FD grid (research solver, fine grid; fd_bsl.log), homogeneous Er 4.0:
# t, face gap, outer height, w, FD Zeven, FD Zodd
BSL_GRID_FD = [
    (0.0152, 0.1, 0.2, 0.08, 105.1991, 43.0683), (0.0152, 0.1, 0.2, 0.15, 87.0617, 30.5286),
    (0.0152, 0.1, 0.2, 0.3, 64.4732, 18.9522), (0.0152, 0.1, 0.4, 0.08, 139.5199, 44.5462),
    (0.0152, 0.1, 0.4, 0.15, 120.8082, 31.9109), (0.0152, 0.1, 0.4, 0.3, 96.1619, 20.1389),
    (0.0152, 0.2, 0.2, 0.08, 96.4484, 57.2709), (0.0152, 0.2, 0.2, 0.15, 80.1793, 42.9920),
    (0.0152, 0.2, 0.2, 0.3, 60.4029, 28.3394), (0.0152, 0.2, 0.4, 0.08, 127.7480, 60.9589),
    (0.0152, 0.2, 0.4, 0.15, 111.0495, 46.5304), (0.0152, 0.2, 0.4, 0.3, 89.6701, 31.4824),
    (0.035, 0.1, 0.2, 0.08, 97.5608, 39.6103), (0.035, 0.1, 0.2, 0.15, 81.8989, 28.7933),
    (0.035, 0.1, 0.2, 0.3, 61.6193, 18.2756), (0.035, 0.1, 0.4, 0.08, 130.3388, 41.2047),
    (0.035, 0.1, 0.4, 0.15, 114.1729, 30.2470), (0.035, 0.1, 0.4, 0.3, 92.0296, 19.4890),
    (0.035, 0.2, 0.2, 0.08, 90.2118, 52.5270), (0.035, 0.2, 0.2, 0.15, 76.0945, 40.3285),
    (0.035, 0.2, 0.2, 0.3, 58.1191, 27.1678), (0.035, 0.2, 0.4, 0.08, 120.3034, 56.2533),
    (0.035, 0.2, 0.4, 0.15, 105.8105, 43.8735), (0.035, 0.2, 0.4, 0.3, 86.3694, 30.2866),
]


@pytest.mark.parametrize("t, gap, h, w, fd_e, fd_o", BSL_GRID_FD)
def test_broadside_grid_error_vs_fd(t: float, gap: float, h: float, w: float, fd_e: float,
                                    fd_o: float) -> None:
    """Zodd (z_diff) inside the class B estimate; Zeven up to +2.72 % (1 oz on 0.1 mm)."""
    out = tline.evaluate(bsl(w, h, 4.0, gap, 4.0, t=t))
    assert out.accuracy_class == "B"
    assert abs(pct(out.z_odd, fd_o)) <= out.est_error_pct
    assert abs(pct(out.z_even, fd_e)) <= 2.75


def test_odd_mode_mapping_beats_the_single_dw_on_the_grid() -> None:
    """The [R-imp] single dW (outer half only) leaves Zodd up to 8.4 % low on this grid."""
    ours, single = [], []
    for t, gap, h, w, _, fd_o in BSL_GRID_FD:
        ours.append(pct(tline.evaluate(bsl(w, h, 4.0, gap, 4.0, t=t)).z_odd, fd_o))
        w_even, _ = bs.mode_widths(w, t, h, gap)
        single.append(pct(bs.cohn_broadside(w_even, gap, gap + 2 * h, 4.0)[1], fd_o))
    assert max(map(abs, ours)) <= 1.2 and abs(sum(ours) / len(ours)) <= 0.5
    assert min(single) < -8.0


# FD of inhomogeneous broadside pairs (research solver, fine grid, 0.5 oz; D7-05 fd_refs.log):
# w, core, Er core, face gap, Er gap, FD Zeven, Zodd
BSL_MIXED_FD = [
    (0.1157, 0.4, 4.6, 0.2028, 4.4, 110.67183654437444, 49.995538414968635),
    (0.3, 0.4, 4.6, 0.2028, 4.4, 83.83022517995539, 30.06265375025559),
    (0.1157, 0.254, 3.66, 0.101, 3.52, 111.34659914384912, 38.61332080714051),
    (0.3, 0.254, 3.66, 0.101, 3.52, 78.2483345728036, 20.813186790260602),
]


@pytest.mark.parametrize("w, core, er_c, gap, er_g, fd_e, fd_o", BSL_MIXED_FD)
def test_inhomogeneous_broadside_error_vs_fd(w: float, core: float, er_c: float, gap: float,
                                             er_g: float, fd_e: float, fd_o: float) -> None:
    """JLC06161H-7628 L3/L4 (4.6 cores, 4.4 prepreg) and RO4350B cores on RO4450F bondply:
    contrast <= 10 %, class B, Zdiff within the estimate."""
    out = tline.evaluate(bsl(w, core, er_c, gap, er_g))
    assert out.accuracy_class == "B"
    assert abs(pct(out.z_odd, fd_o)) <= out.est_error_pct
    assert abs(pct(out.z_even, fd_e)) <= out.est_error_pct


def test_zero_thickness_homogeneous_broadside_is_exact_class_a() -> None:
    out = tline.evaluate(bsl(0.3, 0.4, 4.55, 0.2028, 4.55, t=0.0))
    assert out.accuracy_class == "A" and "cohn60" in out.model


def test_inhomogeneous_broadside_uses_per_mode_permittivity() -> None:
    """Odd-mode field crowds into the face gap, even-mode into the outer plies: the closed
    form weights the plies by their parallel-plate capacitance per mode."""
    out = tline.evaluate(bsl(0.1157, 0.4, 4.6, 0.2028, 4.4))
    assert 4.4 < out.eeff < 4.5                                   # odd mode: gap-weighted
    homog = tline.evaluate(bsl(0.1157, 0.4, 4.5, 0.2028, 4.5))
    assert homog.eeff == pytest.approx(4.5, rel=1e-12)
    high = tline.evaluate(bsl(0.1157, 0.2, 4.4, 0.2, 3.0))       # 47 % contrast
    assert high.accuracy_class == "C" and any("contrast" in m for m in high.warnings)


def test_offset_broadside_is_class_c() -> None:
    out = tline.evaluate(bsl(0.15, 0.4, 4.4, 0.2, 4.4, core_below=0.3))
    assert out.accuracy_class == "C"
    assert any("outer heights" in m for m in out.warnings)


# ---------------------------------------------------------------- broadside validity envelope
# D7-05 FD sweep (research solver, medium grid; pair in air, outer height BSL_H): per
# (t, y = g/h_out) the FD (Zeven, Zodd) at each x = w/h_out of BSL_X. The medium grid itself
# reads up to 0.4 % low on bare narrow strips (vs Cohn 1954 exact), 0.03-0.05 % on 0.5 oz.
BSL_H = 0.2
BSL_X = (0.15, 0.25, 0.4, 0.7, 1.0, 1.5, 2.5)
BSL_SWEEP = [
    (0.0, 0.25, (319.143, 114.221, 285.389, 87.400, 251.369, 65.523, 206.784, 44.191,
                 176.875, 33.483, 143.034, 23.898, 103.645, 15.211)),
    (0.0, 0.5, (290.678, 151.163, 259.428, 121.964, 229.443, 96.332, 191.212, 68.881,
                165.333, 53.899, 135.400, 39.658, 99.579, 25.978)),
    (0.0, 1.0, (267.635, 184.813, 237.168, 154.830, 208.812, 127.596, 174.365, 96.482,
                151.869, 78.141, 125.979, 59.527, 94.351, 40.376)),
    (0.0, 1.5, (257.598, 201.059, 227.275, 170.926, 199.250, 143.349, 165.753, 111.261,
                144.369, 91.786, 120.210, 71.361, 90.897, 49.476)),
    (0.0, 2.0, (252.065, 210.557, 221.788, 180.374, 193.873, 152.680, 160.713, 120.233,
                139.769, 100.295, 116.404, 79.023, 88.405, 55.647)),
    (0.0, 3.0, (246.306, 220.906, 216.059, 190.690, 188.213, 162.921, 155.276, 130.233,
                134.640, 109.960, 111.892, 88.018, 85.121, 63.260)),
    (0.0152, 0.25, (277.028, 93.831, 253.316, 75.529, 227.124, 58.877, 190.565, 41.166,
                    164.988, 31.737, 135.196, 23.000, 99.471, 14.843)),
    (0.0152, 0.5, (255.300, 125.161, 233.554, 105.371, 210.308, 86.085, 178.319, 63.647,
                   155.718, 50.675, 128.913, 37.894, 96.029, 25.210)),
    (0.0152, 1.0, (235.488, 155.367, 214.555, 135.004, 192.814, 114.474, 164.054, 89.057,
                   144.246, 73.253, 120.776, 56.662, 91.414, 39.039)),
    (0.0152, 1.5, (226.270, 170.544, 205.516, 150.051, 184.137, 129.217, 156.311, 102.937,
                   137.529, 86.127, 115.604, 67.914, 88.283, 47.797)),
    (0.0152, 2.0, (221.028, 179.617, 200.338, 159.074, 179.090, 138.126, 151.623, 111.504,
                   133.277, 94.259, 112.103, 75.262, 85.986, 53.759)),
    (0.0152, 3.0, (215.439, 189.699, 194.794, 169.119, 173.636, 148.087, 146.424, 121.215,
                   128.402, 103.637, 107.845, 83.988, 82.901, 61.166)),
    (0.035, 0.25, (247.744, 83.191, 229.319, 68.721, 208.035, 54.780, 177.159, 39.173,
                   154.900, 30.551, 128.366, 22.373, 95.726, 14.579)),
    (0.035, 0.5, (231.186, 110.233, 214.212, 94.939, 195.050, 79.180, 167.492, 59.879,
                  147.450, 48.277, 123.211, 36.543, 92.831, 24.606)),
    (0.035, 1.0, (214.452, 137.275, 198.221, 121.646, 180.358, 105.001, 155.473, 83.347,
                  137.714, 69.374, 116.212, 54.321, 88.783, 37.914)),
    (0.035, 1.5, (206.123, 151.345, 190.085, 135.624, 172.585, 118.738, 148.575, 96.358,
                  131.739, 81.513, 111.599, 65.024, 85.959, 46.348)),
    (0.035, 2.0, (201.220, 159.947, 185.259, 144.185, 167.901, 127.197, 144.255, 104.509,
                  127.837, 89.270, 108.396, 72.065, 83.849, 52.110)),
    (0.035, 3.0, (195.843, 169.716, 179.939, 153.915, 162.688, 136.842, 139.318, 113.908,
                  123.234, 98.348, 104.397, 80.521, 80.961, 59.315)),
    (0.07, 0.25, (216.428, 73.350, 202.657, 62.025, 186.111, 50.528, 161.162, 36.991,
                  142.572, 29.216, 119.797, 21.651, 90.881, 14.270)),
    (0.07, 0.5, (205.021, 95.834, 192.188, 84.289, 176.993, 71.765, 154.189, 55.607,
                 137.083, 45.477, 115.899, 34.920, 88.620, 23.860)),
    (0.07, 1.0, (192.017, 119.049, 179.795, 107.440, 165.611, 94.436, 144.815, 76.649,
                 129.408, 64.693, 110.284, 51.415, 85.287, 36.476)),
    (0.07, 1.5, (184.932, 131.627, 172.905, 119.982, 159.061, 106.827, 139.032, 88.500,
                 124.399, 75.851, 106.396, 61.379, 82.868, 44.467)),
    (0.07, 2.0, (180.545, 139.551, 168.604, 127.879, 154.910, 114.646, 135.233, 96.068,
                 120.984, 83.085, 103.598, 67.995, 81.014, 49.950)),
    (0.07, 3.0, (175.518, 148.833, 163.650, 137.124, 150.080, 123.811, 130.699, 105.002,
                 116.783, 91.722, 99.973, 76.061, 78.404, 56.867)),
]


def _sweep_points() -> list[tuple[float, float, float, float, float]]:
    """(t, x, y, FD Zeven, FD Zodd) of every BSL_SWEEP point."""
    return [(t, x, y, flat[2 * i], flat[2 * i + 1])
            for t, y, flat in BSL_SWEEP for i, x in enumerate(BSL_X)]


def test_broadside_classes_hold_over_the_fd_sweep() -> None:
    """ADR D7.6 accuracy table: class A within its estimate (Zdiff) and Zeven within 1 %,
    class B within its estimate and Zeven within 3 %; everything else is class C - whose
    estimate still bounds the Zdiff error (Cohn reads up to 11.2 % low at g/h 3, w/h 0.15)."""
    points = _sweep_points()
    assert len(points) == 168
    classes = Counter()
    for t, x, y, fd_e, fd_o in points:
        out = tline.evaluate(bsl(x * BSL_H, BSL_H, 1.0, y * BSL_H, 1.0, t=t))
        classes[out.accuracy_class] += 1
        assert abs(pct(out.z_odd, fd_o)) <= out.est_error_pct, (t, x, y)
        if out.accuracy_class == "C":
            assert any("class C fallback/FD required" in m for m in out.warnings), (t, x, y)
        else:
            assert abs(pct(out.z_even, fd_e)) <= CLASS_BOUND_PCT[out.accuracy_class], (t, x, y)
            assert out.z_even > out.z_odd
    # the envelope as calibrated (10 of the 79 class C points would have met class B bounds)
    assert classes == {"A": 15, "B": 74, "C": 79}


@pytest.mark.parametrize("x, y, cls", [
    (0.7, 0.5, "A"), (0.6, 0.5, "B"),        # Cohn's wide-strip condition w/(b - s) >= 0.35
    (1.0, 1.0, "A"), (0.7, 1.0, "B"),        # ... and w >= 0.8 g (Zeven within 1 %)
    (2.5, 3.0, "A"), (1.5, 3.0, "B"), (1.0, 3.0, "C"),
    (0.2, 0.5, "B"), (0.15, 0.5, "C"),       # weak coupling: w/h >= 0.5 g/h - 0.05 at t = 0
    (2.5, 3.5, "C"), (0.1, 0.25, "C"),       # outside the swept range
])
def test_zero_thickness_broadside_class_follows_cohns_validity(x: float, y: float,
                                                               cls: str) -> None:
    out = tline.evaluate(bsl(x * BSL_H, BSL_H, 4.4, y * BSL_H, 4.4, t=0.0))
    assert out.accuracy_class == cls
    if cls == "A":
        assert out.est_error_pct == bs.EST_COHN60_PCT and out.warnings == ()
    if cls == "B":
        assert out.est_error_pct == bs.EST_BROADSIDE_PCT
        assert any("zero-thickness" in m for m in out.warnings)


# reviewer FD (research solver, medium grid): w, h_out, Er out, face gap, Er gap, t,
# FD Zeven, FD Zodd - weak coupling (g/h_out 1.5-5, narrow strips) the closed form reads low
BSL_WEAK_FD = [
    (0.1, 0.2, 4.4, 0.8, 4.4, 0.0, 82.10845034537456, 73.92492833970431),        # t 0: was A
    (0.1, 0.2, 4.4, 0.8, 4.4, 0.0152, 76.39522413537374, 68.05602022872598),
    (0.1, 0.2, 4.4, 0.4, 4.4, 0.0152, 80.27202240402231, 60.86115131459655),
    (0.05, 0.2, 4.4, 0.4, 4.4, 0.0152, 95.50750525467936, 75.83565836159688),
    (0.25, 0.2104, 4.4, 1.065, 4.4, 0.0152, 55.1760290911715, 49.32629738584792),  # 7628 L2/L3
    (0.1, 0.2, 4.4, 0.3, 4.4, 0.0152, 82.62673667531831, 56.66150388174327),     # Zeven -3.2 %
]


@pytest.mark.parametrize("w, h, er_o, gap, er_g, t, fd_e, fd_o", BSL_WEAK_FD)
def test_weakly_coupled_broadside_is_class_c(w: float, h: float, er_o: float, gap: float,
                                             er_g: float, t: float, fd_e: float,
                                             fd_o: float) -> None:
    out = tline.evaluate(bsl(w, h, er_o, gap, er_g, t=t))
    assert out.accuracy_class == "C"
    assert any("class C fallback/FD required" in m for m in out.warnings)
    assert abs(pct(out.z_odd, fd_o)) <= out.est_error_pct
    assert pct(out.z_even, fd_e) < -3.0                          # why it is not class B


def test_jlc6_2116_broadside_in2_in3_stays_class_b() -> None:
    """JLC06161H-2116 In2/In3 (0.3 mm cores at 4.41, 0.4168 mm face gap at 4.4), w 0.2: g/h_out
    1.39 is inside the envelope; reviewer FD (medium grid) Zodd -0.09 %, Zeven -1.86 %."""
    out = tline.evaluate(bsl(0.2, 0.3, 4.41, 0.4168, 4.4))
    assert out.accuracy_class == "B" and out.warnings == ()
    assert abs(pct(out.z_odd, 50.04885513684426)) <= out.est_error_pct
    assert abs(pct(out.z_even, 77.60337455483612)) <= 3.0


def test_broadside_never_reports_an_unphysical_pair_as_class_a_or_b() -> None:
    """Zeven < Zodd is impossible for a broadside pair; the reviewer found it as class A at
    w 0.03, h 0.05, g 0.5, t 0. Every A / B result over a coarse grid has Zeven > Zodd."""
    out = tline.evaluate(bsl(0.03, 0.05, 4.4, 0.5, 4.4, t=0.0))
    assert out.z_even < out.z_odd and out.accuracy_class == "C"
    assert any("unphysical" in m for m in out.warnings)
    for w, h, g, t in product((0.03, 0.06, 0.1, 0.2, 0.4, 0.8, 1.6), (0.05, 0.1, 0.2, 0.4, 0.8),
                              (0.05, 0.1, 0.2, 0.5, 1.0, 2.0), (0.0, 0.0152, 0.035)):
        res = tline.evaluate(bsl(w, h, 4.4, g, 4.4, t=t))
        assert res.accuracy_class == "C" or res.z_even > res.z_odd, (w, h, g, t)


def test_thick_copper_on_a_thin_face_gap_is_class_c() -> None:
    """t/g > 0.35 is outside the thickness mapping's calibration (FD sweep: 1 oz on a 0.05 mm
    gap reads Zodd -2.3 %, Zeven +4.9 %); 1 oz on 0.1 mm (t/g 0.35) is the calibrated edge."""
    out = tline.evaluate(bsl(0.14, 0.2, 4.0, 0.05, 4.0, t=0.035))
    assert out.accuracy_class == "C"
    assert any("t/g" in m and "class C fallback/FD required" in m for m in out.warnings)
    assert tline.evaluate(bsl(0.14, 0.2, 4.0, 0.1, 4.0, t=0.035)).accuracy_class == "B"
    thick = tline.evaluate(bsl(0.3, 0.1, 4.0, 0.2, 4.0, t=0.07))           # t/h_out 0.7
    assert thick.accuracy_class == "C" and any("t/h_out" in m for m in thick.warnings)


def test_broadside_geometry_errors_are_loud() -> None:
    with pytest.raises(ValueError, match="pair_gap"):
        tline.evaluate(LineGeometry(kind="stripline", pair="broadside", width=0.1, t=0.0152,
                                    above=(Ply(0.4, 4.4),),
                                    below=(Ply(0.2, 4.4), Ply(0.4, 4.4))))
    with pytest.raises(ValueError, match="face gap"):
        tline.evaluate(LineGeometry(kind="stripline", pair="broadside", width=0.1, t=0.0152,
                                    above=(Ply(0.4, 4.4),),
                                    below=(Ply(0.2, 4.4), Ply(0.4, 4.4)), pair_gap=0.15))
    with pytest.raises(ValueError, match="lower"):
        tline.evaluate(LineGeometry(kind="stripline", pair="broadside", width=0.1, t=0.0152,
                                    above=(Ply(0.4, 4.4),), below=(Ply(0.2, 4.4),),
                                    pair_gap=0.2))


# ---------------------------------------------------------------- coupled microstrip
def test_coupled_microstrip_reproduces_the_research_mapping() -> None:
    """tlcalc.coupled_microstrip_rec at the research W_cf of DMS90_7628 / DMS90_3313."""
    out = tline.evaluate(cms(0.25898442342877387, 0.15, 0.2104, 4.4))
    assert out.z_even == pytest.approx(71.22608235815179, rel=1e-9)
    assert out.z_odd == pytest.approx(46.818135759964285, rel=1e-9)
    assert pct(out.z_diff, 93.27261158810246) == pytest.approx(0.3898892993712266, abs=1e-6)
    thin = tline.evaluate(cms(0.12667508706450462, 0.127, 0.0994, 4.1))
    assert thin.z_diff == pytest.approx(100.45379103171913, rel=1e-9)
    assert "kj84" in out.model and "empirical" in out.model


# research grid_cms_*.json (t 0.035, 27 fit points) + grid_val.log (9 held-out points),
# medium grid: h, er, w, s, t, Zeven, Zodd, eeff_even, eeff_odd
CMS_FD = [
    (0.0994, 4.1, 0.0497, 0.0497, 0.035, 111.132, 51.074, 2.8276, 2.0479),
    (0.0994, 4.1, 0.0497, 0.0994, 0.035, 100.728, 64.367, 2.8308, 2.1998),
    (0.0994, 4.1, 0.0497, 0.1988, 0.035, 91.324, 74.963, 2.7742, 2.3477),
    (0.0994, 4.1, 0.0994, 0.0497, 0.035, 83.165, 42.304, 3.0233, 2.1859),
    (0.0994, 4.1, 0.0994, 0.0994, 0.035, 77.085, 51.691, 3.0203, 2.3500),
    (0.0994, 4.1, 0.0994, 0.1988, 0.035, 71.122, 59.060, 2.9611, 2.5069),
    (0.0994, 4.1, 0.1988, 0.0497, 0.035, 56.098, 32.787, 3.2538, 2.3852),
    (0.0994, 4.1, 0.1988, 0.0994, 0.035, 53.271, 38.412, 3.2432, 2.5608),
    (0.0994, 4.1, 0.1988, 0.1988, 0.035, 50.244, 42.682, 3.1847, 2.7212),
    (0.2104, 4.4, 0.1052, 0.1052, 0.035, 112.285, 57.440, 3.0852, 2.3788),
    (0.2104, 4.4, 0.1052, 0.2104, 0.035, 102.046, 69.747, 3.0873, 2.5020),
    (0.2104, 4.4, 0.1052, 0.4208, 0.035, 93.246, 79.303, 3.0311, 2.6307),
    (0.2104, 4.4, 0.2104, 0.1052, 0.035, 82.876, 46.217, 3.2827, 2.5123),
    (0.2104, 4.4, 0.2104, 0.2104, 0.035, 76.948, 54.614, 3.2785, 2.6457),
    (0.2104, 4.4, 0.2104, 0.4208, 0.035, 71.396, 61.116, 3.2179, 2.7842),
    (0.2104, 4.4, 0.4208, 0.1052, 0.035, 55.261, 34.797, 3.5172, 2.7099),
    (0.2104, 4.4, 0.4208, 0.2104, 0.035, 52.536, 39.613, 3.5052, 2.8526),
    (0.2104, 4.4, 0.4208, 0.4208, 0.035, 49.732, 43.277, 3.4441, 2.9948),
    (0.508, 3.66, 0.254, 0.254, 0.035, 125.301, 67.636, 2.6905, 2.2197),
    (0.508, 3.66, 0.254, 0.508, 0.035, 114.210, 80.476, 2.6955, 2.2876),
    (0.508, 3.66, 0.254, 1.016, 0.035, 104.834, 90.438, 2.6587, 2.3705),
    (0.508, 3.66, 0.508, 0.254, 0.035, 91.749, 53.409, 2.8309, 2.3043),
    (0.508, 3.66, 0.508, 0.508, 0.035, 85.335, 62.019, 2.8299, 2.3809),
    (0.508, 3.66, 0.508, 1.016, 0.035, 79.409, 68.741, 2.7882, 2.4724),
    (0.508, 3.66, 1.016, 0.254, 0.035, 60.858, 39.575, 3.0010, 2.4404),
    (0.508, 3.66, 1.016, 0.508, 0.035, 57.911, 44.414, 2.9933, 2.5248),
    (0.508, 3.66, 1.016, 1.016, 0.035, 54.910, 48.163, 2.9499, 2.6201),
    (0.0994, 4.1, 0.12624, 0.12723, 0.0175, 68.500, 52.843, 3.1335, 2.5970),
    (0.0994, 4.1, 0.07952, 0.1491, 0.07, 77.147, 54.688, 2.8468, 2.2008),
    (0.2104, 4.4, 0.25248, 0.14728, 0.0175, 74.052, 49.189, 3.3816, 2.7294),
    (0.2104, 4.4, 0.14728, 0.12624, 0.07, 93.206, 49.347, 3.1077, 2.2797),
    (0.508, 3.66, 0.762, 0.2032, 0.07, 73.310, 40.984, 2.9033, 2.2628),
    (0.508, 3.66, 0.3048, 0.6096, 0.0175, 105.810, 80.602, 2.7481, 2.3750),
    (0.1164, 4.16, 0.1164, 0.1164, 0.035, 77.139, 52.515, 3.0734, 2.4141),
    (0.0764, 3.91, 0.10696, 0.12224, 0.035, 62.761, 48.932, 2.9446, 2.4060),
    (0.2104, 4.4, 0.33664, 0.08416, 0.035, 64.612, 36.462, 3.4373, 2.5897),
]


@pytest.mark.parametrize("h, er, w, s, t, fd_e, fd_o, fd_ee, fd_eo", CMS_FD)
def test_coupled_microstrip_error_vs_fd_is_inside_its_estimate(h: float, er: float, w: float,
                                                               s: float, t: float, fd_e: float,
                                                               fd_o: float, fd_ee: float,
                                                               fd_eo: float) -> None:
    """ADR D7.6: Zdiff +0.19 % mean / 1.66 % worst (fit), 3.27 % held-out at t/h 0.7."""
    out = tline.evaluate(cms(w, s, h, er, t=t))
    assert abs(pct(out.z_diff, 2 * fd_o)) <= out.est_error_pct
    assert out.accuracy_class == ("B" if t / h <= 0.5 else "C")
    # odd-mode eeff from the H&J air mapping (w + du1, s - du1): within 4 % of FD on the
    # class B points with du1/s < 0.7 (-9.5 % worst beyond; the d-mapped KJ84 eeff is +27 %)
    du1, _ = cm.hj_width_increments(w, h, t, er)
    if t / h <= 0.5 and du1 / s < 0.7:
        assert abs(pct(out.eeff, fd_eo)) <= 4.0


def test_coupled_microstrip_fit_statistics_match_the_adr() -> None:
    errs = [pct(tline.evaluate(cms(w, s, h, er, t=t)).z_diff, 2 * fd_o)
            for h, er, w, s, t, _, fd_o, _, _ in CMS_FD[:27]]
    assert sum(errs) / 27 == pytest.approx(0.19, abs=0.01)
    assert max(errs, key=abs) == pytest.approx(1.66, abs=0.01)


# research grid_cms_*.json at t = 0 (medium grid): h, er, w, s, Zeven, Zodd
CMS_T0_FD = [(0.0994, 4.1, 0.0497, 0.0497, 123.495, 70.780),
             (0.2104, 4.4, 0.2104, 0.2104, 80.611, 60.543),
             (0.508, 3.66, 1.016, 0.254, 61.718, 41.624),
             (0.508, 3.66, 0.254, 1.016, 109.821, 96.252)]


@pytest.mark.parametrize("h, er, w, s, fd_e, fd_o", CMS_T0_FD)
def test_zero_thickness_coupled_microstrip_is_kj84_class_a(h: float, er: float, w: float, s: float,
                                                           fd_e: float, fd_o: float) -> None:
    """KJ84 static at t = 0: Zodd 0.04 % mean / 0.22 % worst, Zeven 0.6 % worst [R-imp]."""
    out = tline.evaluate(cms(w, s, h, er, t=0.0))
    assert out.accuracy_class == "A"
    assert abs(pct(out.z_odd, fd_o)) <= out.est_error_pct
    assert abs(pct(out.z_even, fd_e)) <= 0.6


def test_kj84_eeff_from_the_air_mapping_equals_kj84_at_t0() -> None:
    ze, zo, ee, eo = cm.kj84_static(1.2, 0.8, 4.4)
    ze_air, zo_air, _, _ = cm.kj84_static(1.2, 0.8, 1.0)
    assert (ze_air / ze) ** 2 == pytest.approx(ee, rel=1e-12)
    assert (zo_air / zo) ** 2 == pytest.approx(eo, rel=1e-12)


def test_covered_coupled_microstrip_is_class_c() -> None:
    out = tline.evaluate(cms(0.2, 0.15, 0.2104, 4.4, above=(Ply(0.1, 4.4),)))
    assert out.accuracy_class == "C"
    assert any("class C fallback/FD required" in m for m in out.warnings)


def test_coupled_microstrip_outside_kj84_validity_is_class_c() -> None:
    out = tline.evaluate(cms(3.0, 0.15, 0.2104, 4.4))             # u' ~ 14 > 10
    assert out.accuracy_class == "C" and any("kj84" in m for m in out.warnings)


def test_coupled_microstrip_dispersion_is_flagged_not_modelled() -> None:
    geom = cms(2.0, 0.5, 1.51, 4.5)
    low, high = tline.evaluate(geom, 1e9), tline.evaluate(geom, 2.45e9)
    assert low.warnings == () and high.eeff_f == high.eeff
    assert any("dispersion" in m for m in high.warnings)


def test_coupled_microstrip_geometry_errors_are_loud() -> None:
    with pytest.raises(ValueError, match="pair_gap"):
        tline.evaluate(cms(0.2, None, 0.2104, 4.4))
    with pytest.raises(ValueError, match="gap"):
        tline.evaluate(cms(0.3, 0.005, 0.2104, 4.4))


@pytest.mark.parametrize("w, h, er, t", [(0.1, 0.2104, 4.4, T_1OZ), (0.3, 1.51, 4.5, T_1OZ),
                                         (0.5, 1.51, 4.5, 0.07), (0.03, 1.51, 4.5, 0.0)])
def test_coupled_microstrip_at_the_mapping_edge_never_overflows(w: float, h: float, er: float,
                                                                t: float) -> None:
    """D7-05 review 2: micron gaps at the edge of the d / du1 mappings drove KJ84's odd-mode
    term exp(Q6 ln u u^-Q9) out of the float range (OverflowError; Q6 < 0 below g 0.0015).
    A mapped gap under KJ84_MIN_G h is a loud ValueError; an air-mapped one takes the
    closed-gap eeff fallback."""
    d, d1 = cm.mapped_increments(w, h, t, er)
    outcomes = Counter()
    for i in range(500):
        s = d + h * 10 ** (-7 + 6.5 * i / 500)               # (s - d)/h from 1e-7 to 0.3
        if (s - d) / h < cm.KJ84_MIN_G:
            with pytest.raises(ValueError, match="gap .* too small"):
                tline.evaluate(cms(w, s, h, er, t=t))
            outcomes["error"] += 1
            continue
        out = tline.evaluate(cms(w, s, h, er, t=t))
        assert min(out.z_odd, out.z_even) > 0 and 1.0 <= out.eeff <= er, s
        assert out.accuracy_class == "C" or out.z_odd < out.z_even, s   # g < 0.1: class C
        closed = (s - d1) / h < cm.KJ84_MIN_G
        assert closed == any("air mapping" in m for m in out.warnings), s
        outcomes["fallback" if closed else "ok"] += 1
    assert outcomes["error"] and outcomes["ok"] and (outcomes["fallback"] or t == 0.0)


def test_kj84_static_outside_its_numeric_domain_is_a_value_error() -> None:
    for u, g in [(0.02, 1e-3), (0.5, 0.0), (2000.0, 9000.0)]:   # tiny gap; huge u and g
        with pytest.raises(ValueError, match="kj84"):
            cm.kj84_static(u, g, 4.4)
    edge = cm.kj84_static(0.02, cm.KJ84_MIN_G, 4.4)          # just inside: finite, positive
    assert all(math.isfinite(v) and v > 0 for v in edge)


# ---------------------------------------------------------------- monotonicity
@pytest.mark.parametrize("geom_of", [
    lambda w, s: centred_edge(w, s, 0.5, 4.2),
    lambda w, s: edge_sl(w, s, 0.55, 0.1088, 4.6, 4.16),
    lambda w, s: cms(w, s, 0.2104, 4.4),
    lambda w, s: cms(w, s, 0.0994, 4.1, mask=JLC_MASK),
])
def test_zdiff_falls_with_width_and_rises_with_gap(geom_of: PairOf) -> None:
    widths = [0.06 + 0.002 * i for i in range(300)]
    zw = [tline.evaluate(geom_of(w, 0.15)).z_diff for w in widths]
    assert all(b < a for a, b in pairwise(zw))
    gaps = [0.1 + 0.005 * i for i in range(200)]
    zg = [tline.evaluate(geom_of(0.12, s)).z_diff for s in gaps]
    assert all(b > a for a, b in pairwise(zg))


def test_broadside_zdiff_falls_with_width() -> None:
    widths = [0.04 + 0.002 * i for i in range(400)]
    zs = [tline.evaluate(bsl(w, 0.4, 4.6, 0.2028, 4.4)).z_diff for w in widths]
    assert all(b < a for a, b in pairwise(zs))
