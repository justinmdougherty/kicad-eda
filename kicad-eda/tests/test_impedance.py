"""impedance — Hammerstad microstrip / edge-coupled pair solver."""
from __future__ import annotations

import math

import pytest

import impedance

H, ER = impedance.DEFAULT_STACKUP["h"], impedance.DEFAULT_STACKUP["er"]


def _hammerstad_z0(w: float, h: float, er: float) -> float:
    u = w / h
    eeff = (er + 1) / 2 + (er - 1) / 2 / math.sqrt(1 + 12 / u)
    if u <= 1:
        return 60 / math.sqrt(eeff) * math.log(8 / u + u / 4)
    return 120 * math.pi / math.sqrt(eeff) / (u + 1.393 + 0.667 * math.log(u + 1.444))


@pytest.mark.parametrize("w", [0.05, 0.1, 0.2, 0.3, 0.4, 1.0, 3.0])
def test_microstrip_z0_matches_hand_computed_hammerstad(w):
    assert impedance.microstrip_z0(w, H, ER) == pytest.approx(_hammerstad_z0(w, H, ER), rel=1e-9)


def test_microstrip_z0_continuous_across_wh_equals_one():
    assert impedance.microstrip_z0(H * 0.9999, H, ER) == \
        pytest.approx(impedance.microstrip_z0(H * 1.0001, H, ER), rel=5e-3)


def test_microstrip_z0_depends_only_on_w_over_h():
    # scaling geometry uniformly leaves the impedance unchanged
    assert impedance.microstrip_z0(0.3, 0.2, ER) == \
        pytest.approx(impedance.microstrip_z0(0.6, 0.4, ER), rel=1e-12)


def test_microstrip_z0_decreases_with_width_and_er():
    z = [impedance.microstrip_z0(w / 100, H, ER) for w in range(5, 300, 5)]
    assert all(a > b for a, b in zip(z, z[1:]))
    assert impedance.microstrip_z0(0.3, H, 3.0) > impedance.microstrip_z0(0.3, H, 4.5)


def test_microstrip_50_ohm_default_stackup_hand_value():
    # bisection of the hand formula, independent of the module's solver
    lo, hi = 0.05, 5.0
    for _ in range(60):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if _hammerstad_z0(mid, H, ER) > 50 else (lo, mid)
    w = impedance.microstrip_width(50, H, ER)
    assert w == pytest.approx((lo + hi) / 2, abs=2e-4)
    assert w == pytest.approx(0.399, abs=0.004)          # ~0.4 mm on 0.2 mm FR4
    assert impedance.microstrip_z0(w, H, ER) == pytest.approx(50, abs=0.05)


@pytest.mark.parametrize("z0", [30, 50, 75, 90, 110])
def test_microstrip_width_round_trips(z0):
    w = impedance.microstrip_width(z0, H, ER)
    assert impedance.microstrip_z0(w, H, ER) == pytest.approx(z0, abs=0.1)


def test_microstrip_width_monotonic_in_target():
    assert (impedance.microstrip_width(50, H, ER) > impedance.microstrip_width(75, H, ER)
            > impedance.microstrip_width(90, H, ER))


def test_thinner_dielectric_needs_narrower_trace():
    assert impedance.microstrip_width(50, 0.1, ER) < impedance.microstrip_width(50, 0.2, ER)


def test_coupled_zdiff_limits_and_monotonic_in_gap():
    z0 = impedance.microstrip_z0(0.3, H, ER)
    assert impedance.coupled_zdiff(0.3, 0.0, H, ER) == pytest.approx(2 * z0 * 0.52)
    assert impedance.coupled_zdiff(0.3, 50 * H, H, ER) == pytest.approx(2 * z0, rel=1e-6)
    zd = [impedance.coupled_zdiff(0.3, s / 100, H, ER) for s in range(0, 200, 10)]
    assert all(a < b for a, b in zip(zd, zd[1:]))


@pytest.mark.parametrize("zdiff", [90, 100])
def test_diff_pair_hits_target_with_gap_equal_width(zdiff):
    dp = impedance.diff_pair(zdiff, H, ER)
    assert dp["gap"] == dp["width"]                       # industry default S = W
    assert dp["zdiff_target"] == zdiff
    assert dp["zdiff_achieved"] == pytest.approx(zdiff, abs=0.2)
    assert impedance.coupled_zdiff(dp["width"], dp["gap"], H, ER) == \
        pytest.approx(zdiff, abs=0.1)
    assert dp["z0_se"] == pytest.approx(impedance.microstrip_z0(dp["width"], H, ER), abs=0.01)
    # coupling lowers Zdiff below 2*Z0, so each single trace runs above Zdiff/2
    assert dp["z0_se"] > zdiff / 2


def test_diff_pair_explicit_gap_is_honoured_and_tighter_gap_is_narrower():
    loose = impedance.diff_pair(100, H, ER)
    tight = impedance.diff_pair(100, H, ER, gap=0.15)
    assert tight["gap"] == 0.15
    assert tight["width"] < loose["width"]
    assert tight["zdiff_achieved"] == pytest.approx(100, abs=0.2)


def test_diff_pair_lower_target_is_wider():
    assert impedance.diff_pair(90, H, ER)["width"] > impedance.diff_pair(100, H, ER)["width"]


# --- invalid inputs ----------------------------------------------------------

@pytest.mark.parametrize("w,h", [(0, 0.2), (-0.1, 0.2), (0.3, 0), (0.3, -1)])
def test_microstrip_z0_non_positive_geometry_is_infinite(w, h):
    assert math.isinf(impedance.microstrip_z0(w, h, ER))


def test_microstrip_z0_unphysical_er_raises():
    with pytest.raises(ValueError):
        impedance.microstrip_z0(0.3, H, -2.0)


def test_microstrip_width_zero_height_returns_search_bound():
    # BUG: with h <= 0 every Z0 is +inf so bisection walks to the upper search
    # bound and returns 5.0 mm with no error — a bogus stackup should raise,
    # not size a 5 mm trace. Documenting current behaviour (module not owned).
    assert impedance.microstrip_width(50, 0.0, ER) == 5.0
    assert impedance.microstrip_width(50, -0.2, ER) == 5.0


def test_diff_pair_zero_height_divides_by_zero():
    # BUG: the same bogus stackup makes diff_pair / coupled_zdiff fail with a
    # raw ZeroDivisionError (s / h in the coupling term) instead of a clear
    # ValueError. Documenting current behaviour (module not owned).
    with pytest.raises(ZeroDivisionError):
        impedance.diff_pair(100, 0.0, ER)
    with pytest.raises(ZeroDivisionError):
        impedance.coupled_zdiff(0.3, 0.3, 0.0, ER)


def test_resolve_impedance_non_numeric_spec_raises():
    with pytest.raises(ValueError):
        impedance.resolve_impedance({"net_specs": {"RF": {"impedance": "fifty"}}})
    with pytest.raises(ValueError):
        impedance.resolve_impedance({"net_specs": {"D": {"diff": "x"}}})


# --- resolve_impedance -------------------------------------------------------

def test_resolve_impedance_only_opted_in_nets():
    design = {"nets": {"RF": [], "USB_DP": [], "GND": []},
              "net_specs": {"RF": {"impedance": 50}, "GND": {"current": 3.0},
                            "USB_DP": {"diff": 90, "gap": 0.2}}}
    out = impedance.resolve_impedance(design)
    assert set(out) == {"RF", "USB_DP"}
    rf = out["RF"]
    assert rf["mode"] == "single" and rf["z0"] == 50.0
    assert rf["track_width"] == impedance.microstrip_width(50, H, ER)
    assert rf["stackup"] == {"h": H, "er": ER}
    usb = out["USB_DP"]
    dp = impedance.diff_pair(90, H, ER, 0.2)
    assert usb["mode"] == "diff" and usb["zdiff"] == 90.0
    assert usb["diff_pair_gap"] == 0.2
    assert usb["diff_pair_width"] == usb["track_width"] == dp["width"]
    assert usb["zdiff_achieved"] == dp["zdiff_achieved"]
    assert usb["z0_se"] == dp["z0_se"]


def test_resolve_impedance_stackup_precedence():
    design = {"net_specs": {"RF": {"impedance": 50}}, "stackup": {"h": 0.1}}
    from_design = impedance.resolve_impedance(design)["RF"]
    assert from_design["stackup"] == {"h": 0.1, "er": ER}
    assert from_design["track_width"] == impedance.microstrip_width(50, 0.1, ER)
    explicit = impedance.resolve_impedance(design, {"h": 0.3, "er": 3.5})["RF"]
    assert explicit["stackup"] == {"h": 0.3, "er": 3.5}   # argument beats design


def test_resolve_impedance_empty_and_null_specs():
    assert impedance.resolve_impedance({}) == {}
    assert impedance.resolve_impedance({"net_specs": None, "stackup": None}) == {}


def test_resolve_impedance_on_examples(load_design):
    coupon = impedance.resolve_impedance(load_design("rf_coupon_50r"))
    assert coupon, "the 50 ohm coupon must declare at least one impedance net"
    assert all(v["mode"] == "single" and v["z0"] == 50.0 for v in coupon.values())
    assert impedance.resolve_impedance(load_design("buck_5v3a")) == {}
