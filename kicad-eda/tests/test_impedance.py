"""impedance: the legacy Hammerstad helpers and the ADR D7.6 resolve_impedance facade."""
from __future__ import annotations

import copy
import json
import math
import subprocess
import sys
from dataclasses import replace
from itertools import pairwise
from pathlib import Path
from typing import Any

import pytest

import decisions
import fieldsolve2d as fs
import impedance
from tline import geometry as geo
from tline import synth

H, ER = impedance.DEFAULT_STACKUP["h"], impedance.DEFAULT_STACKUP["er"]
FD = pytest.mark.skipif(fs.available() == "none", reason="numpy/scipy not installed")
TOOLS = Path(impedance.__file__).resolve().parent
LEGACY_SINGLE = {"mode", "z0", "track_width", "stackup"}
LEGACY_DIFF = {"mode", "zdiff", "zdiff_achieved", "diff_pair_width", "diff_pair_gap",
               "track_width", "z0_se", "stackup"}
D76_KEYS = {"kind", "pair", "z_target", "line", "freq_hz", "primary_layer", "layers",
            "eligible_layers", "model", "accuracy_class", "fd", "status", "width_window",
            "z_achieved", "issues", "warnings", "decision_drafts"}
MALFORMED = [({"diff": 90, "gap": "x"}, "gap"), ({"impedance": 50, "width": "wide"}, "width"),
             ({"impedance": 50, "line": "cpwg", "ground_gap": -0.1}, "ground_gap"),
             ({"impedance": 50, "freq_hz": "x"}, "freq_hz"),
             ({"impedance": 50, "width": 0}, "width"),
             ({"impedance": 50, "z_tol_pct": True}, "z_tol_pct"),
             ({"impedance": 50, "layers": "F.Cu"}, "layers"),
             ({"impedance": 50, "layers": []}, "layers"),
             ({"impedance": 50, "ref_nets": "GND"}, "ref_nets"),
             ({"impedance": 50, "ref_layers": {"F.Cu": "In1.Cu"}}, "ref_layers"),
             ({"impedance": 50, "void_layers": ["In1.Cu"]}, "void_layers"),
             ({"diff": 90, "pair": "stacked"}, "pair"), ({"impedance": "fifty"}, "impedance"),
             ({"diff": float("inf")}, "diff")]
EMBEDDED = {"fab": None, "layers": [
    {"type": "copper", "oz": 1, "role": "signal"},
    {"type": "dielectric", "kind": "prepreg", "thickness": 0.1, "dk": 4.2, "df": 0.02},
    {"type": "copper", "oz": 1, "role": "signal"},
    {"type": "dielectric", "kind": "core", "thickness": 0.3, "dk": 4.4, "df": 0.02},
    {"type": "copper", "oz": 1, "role": "plane", "net": "GND"},
    {"type": "dielectric", "kind": "prepreg", "thickness": 1.0, "dk": 4.5, "df": 0.02},
    {"type": "copper", "oz": 1, "role": "signal"}]}
HYBRID = {"fab": "pcbway", "dk_policy": "design", "design_freq_hz": 1.575e9, "finish": "ENIG",
          "impedance_control": {"required": True, "tolerance_pct": 10},
          "layers": [
              {"type": "copper", "oz": 1, "role": "signal"},
              {"type": "dielectric", "kind": "core", "material": "rogers.ro4350b",
               "thickness": 0.254, "locked": True},
              {"type": "copper", "oz": 1, "role": "plane", "net": "GND"},
              {"type": "dielectric", "kind": "prepreg", "plies": [
                  {"material": "rogers.ro4450f", "thickness": 0.101},
                  {"material": "isola.370hr", "construction": "2116", "thickness": 0.12}]},
              {"type": "copper", "oz": 0.5, "role": "plane", "net": "+3V3"},
              {"type": "dielectric", "kind": "core", "material": "isola.370hr", "thickness": 0.71},
              {"type": "copper", "oz": 1, "role": "signal"}]}


def _hammerstad_z0(w: float, h: float, er: float) -> float:
    u = w / h
    eeff = (er + 1) / 2 + (er - 1) / 2 / math.sqrt(1 + 12 / u)
    if u <= 1:
        return 60 / math.sqrt(eeff) * math.log(8 / u + u / 4)
    return 120 * math.pi / math.sqrt(eeff) / (u + 1.393 + 0.667 * math.log(u + 1.444))


def _design(specs: dict[str, Any], stackup: Any = None, **extra: Any) -> dict[str, Any]:
    design = {"nets": {n: [] for n in [*specs, "GND", "+3V3"]}, "net_specs": specs, **extra}
    return design if stackup is None else {**design, "stackup": stackup}


def _ids(rec: dict[str, Any]) -> list[str]:
    return [i["id"] for i in rec["issues"]]


@pytest.fixture
def no_solves(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail the test if the FD tier runs a single solve."""
    def boom(*_a: Any, **_k: Any) -> None:
        raise AssertionError("an FD solve ran")
    monkeypatch.setattr(fs, "solve", boom)
    monkeypatch.setattr(fs, "synthesize_fd", boom)


# --- legacy helpers (kept) -------------------------------------------------------

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
    assert all(a > b for a, b in pairwise(z))
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
    assert all(a < b for a, b in pairwise(zd))


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


@pytest.mark.parametrize("w,h", [(0, 0.2), (-0.1, 0.2), (0.3, 0), (0.3, -1)])
def test_microstrip_z0_non_positive_geometry_is_infinite(w, h):
    assert math.isinf(impedance.microstrip_z0(w, h, ER))


def test_microstrip_z0_unphysical_er_raises():
    with pytest.raises(ValueError):
        impedance.microstrip_z0(0.3, H, -2.0)


def test_microstrip_width_zero_height_returns_search_bound():
    # the legacy helper keeps its behaviour: every Z0 is +inf, bisection walks to 5.0 mm
    assert impedance.microstrip_width(50, 0.0, ER) == 5.0
    assert impedance.microstrip_width(50, -0.2, ER) == 5.0


def test_diff_pair_zero_height_divides_by_zero():
    with pytest.raises(ZeroDivisionError):
        impedance.diff_pair(100, 0.0, ER)
    with pytest.raises(ZeroDivisionError):
        impedance.coupled_zdiff(0.3, 0.3, 0.0, ER)


# --- resolve_impedance: inputs ---------------------------------------------------

@pytest.mark.parametrize("spec", [{"impedance": "fifty"}, {"diff": "x"}, {"impedance": None},
                                  {"impedance": -50}, {"diff": float("nan")}])
def test_resolve_impedance_non_numeric_spec_raises(spec):
    with pytest.raises(ValueError):
        impedance.resolve_impedance({"net_specs": {"N": spec}})


def test_resolve_impedance_empty_and_null_specs():
    assert impedance.resolve_impedance({}) == {}
    assert impedance.resolve_impedance({"net_specs": None, "stackup": None}) == {}
    assert impedance.resolve_impedance({"net_specs": {"GND": {"current": 3.0}}}) == {}


def test_resolve_impedance_bad_options_raise():
    design = _design({"RF": {"impedance": 50}})
    for kwargs in ({"fd": "sometimes"}, {"errors": "ignore"}):
        with pytest.raises(ValueError):
            impedance.resolve_impedance(design, **kwargs)
    with pytest.raises(ValueError):
        impedance.resolve_impedance(_design({"RF": {"impedance": 50, "layers": "F.Cu"}}))


# --- resolve_impedance: legacy keys ---------------------------------------------

def test_resolve_impedance_only_opted_in_nets_with_the_legacy_keys():
    design = {"nets": {"RF": [], "USB_DP": [], "GND": []},
              "net_specs": {"RF": {"impedance": 50}, "GND": {"current": 3.0},
                            "USB_DP": {"diff": 90, "gap": 0.2}}}
    out = impedance.resolve_impedance(design)
    assert set(out) == {"RF", "USB_DP"}
    rf, usb = out["RF"], out["USB_DP"]
    assert LEGACY_SINGLE | D76_KEYS <= set(rf) and LEGACY_DIFF | D76_KEYS <= set(usb)
    assert rf["mode"] == rf["kind"] == "single" and rf["z0"] == rf["z_target"] == 50.0
    assert rf["stackup"] == {"h": 0.2104, "er": 4.4}          # JLC04161H-7628 under F.Cu
    assert rf["primary_layer"] == "F.Cu"
    assert rf["track_width"] == rf["layers"]["F.Cu"]["width"]
    assert usb["mode"] == "diff" and usb["zdiff"] == 90.0
    assert usb["diff_pair_gap"] == 0.2
    assert usb["diff_pair_width"] == usb["track_width"] == usb["layers"]["F.Cu"]["width"]
    assert usb["zdiff_achieved"] == round(usb["layers"]["F.Cu"]["z_achieved"], 1)
    assert usb["zdiff_achieved"] == pytest.approx(90, rel=0.03)
    assert 45 < usb["z0_se"] < 90


def test_resolve_impedance_legacy_stackup_precedence():
    design = _design({"RF": {"impedance": 50}}, {"h": 0.1})
    from_design = impedance.resolve_impedance(design)["RF"]
    assert from_design["stackup"] == {"h": 0.1, "er": ER}
    explicit = impedance.resolve_impedance(design, {"h": 0.3, "er": 3.5})["RF"]
    assert explicit["stackup"] == {"h": 0.3, "er": 3.5}       # argument beats design
    base = impedance.resolve_impedance(_design({"RF": {"impedance": 50}}, {"h": 0.2}))["RF"]
    assert from_design["track_width"] < base["track_width"] < explicit["track_width"]


def test_pair_without_a_gap_ties_the_gap_to_the_width():
    usb = impedance.resolve_impedance(_design({"USB": {"diff": 90}}))["USB"]
    assert usb["diff_pair_gap"] == usb["diff_pair_width"]
    assert usb["zdiff_achieved"] == pytest.approx(90, rel=0.03)


# --- resolve_impedance: the fixture cases (ADR D7.6 / D7.14) ---------------------

def test_rf_coupon_is_an_unmasked_class_a_microstrip(load_design):
    coupon = impedance.resolve_impedance(load_design("rf_coupon_50r"))
    assert set(coupon) == {"RF_IN", "RF_MID", "RF_OUT"}
    for rec in coupon.values():
        assert rec["mode"] == "single" and rec["z0"] == 50.0
        assert rec["track_width"] == pytest.approx(0.37, abs=0.006)     # was 0.399 (no t)
        assert rec["track_width"] == pytest.approx(0.365)
        assert (rec["accuracy_class"], rec["status"], rec["fd"]) == ("A", "closed_form", None)
        assert rec["stackup"] == {"h": 0.2, "er": 4.2} and rec["decision_drafts"] == []
        f_cu = rec["layers"]["F.Cu"]
        assert f_cu["line"] == "microstrip" and f_cu["ref"]["below"] == ["In1.Cu"]
        assert "coated" not in f_cu["model"] and abs(f_cu["error_pct"]) < 2
    assert impedance.resolve_impedance(load_design("buck_5v3a")) == {}


def test_jlc3313_masked_microstrip_is_class_b_without_fd(tmp_path):
    rec = impedance.resolve_impedance(
        _design({"RF": {"impedance": 50}}, {"preset": "jlcpcb:JLC04161H-3313"}))["RF"]
    assert rec["accuracy_class"] == "B" and rec["status"] == "closed_form"
    assert rec["track_width"] < 0.175
    draft = rec["decision_drafts"][0]
    assert draft["stage"] == "G3" and draft["override"] == "net_specs.RF.width"
    entry = decisions.append_decision(tmp_path / "n.json", **draft)
    assert decisions.validate_decision(entry) == []


@FD
def test_jlc3313_masked_microstrip_is_fd_verified():
    rec = impedance.resolve_impedance(
        _design({"RF": {"impedance": 50}}, {"preset": "jlcpcb:JLC04161H-3313"}), fd="auto")["RF"]
    assert rec["accuracy_class"] == "B" and rec["status"] == "fd" and rec["fd"]["verified"]
    assert rec["track_width"] < 0.175 and rec["decision_drafts"] == []


@FD
def test_masked_usb_pair_on_7628_is_class_c_fd_within_1pct():
    rec = impedance.resolve_impedance(_design({"USB_DP": {"diff": 90, "gap": 0.15}}),
                                      fd="auto")["USB_DP"]
    assert rec["accuracy_class"] == "C" and rec["status"] == "fd"
    assert rec["fd"]["value"] == pytest.approx(0.2349, rel=0.01)
    assert rec["diff_pair_width"] == pytest.approx(0.235) and rec["diff_pair_gap"] == 0.15
    assert rec["decision_drafts"] == []
    again = impedance.resolve_impedance(_design({"USB_DP": {"diff": 90, "gap": 0.15}}),
                                        fd="auto", cache=impedance.fd_cache({"USB_DP": rec}))
    assert again["USB_DP"] == rec                                 # cache hit, same record


def test_masked_usb_pair_without_fd_is_the_fallback_with_a_draft():
    rec = impedance.resolve_impedance(_design({"USB_DP": {"diff": 90, "gap": 0.15}}))["USB_DP"]
    assert (rec["accuracy_class"], rec["status"], rec["fd"]) == ("C", "fallback_unverified", None)
    assert "+fallback:" in rec["model"] and rec["diff_pair_width"] == pytest.approx(0.235)
    assert len(rec["decision_drafts"]) == len(rec["layers"]) == 2      # one per allowed layer
    assert "USB_DP on F.Cu" in rec["decision_drafts"][0]["question"]
    assert rec["decision_drafts"][0]["override"] == "net_specs.USB_DP.width"


def test_fd_budget_zero_runs_no_solve(no_solves):
    rec = impedance.resolve_impedance(_design({"USB_DP": {"diff": 90, "gap": 0.15}}),
                                      fd="auto", budget=fs.FdBudget(0))["USB_DP"]
    assert rec["status"] == "fallback_unverified" and rec["fd"] is None
    assert "budget" in rec["decision_drafts"][0]["rationale"]


def test_jlc2116_in2_is_a_dual_stripline_on_a_rail_reference():
    rec = impedance.resolve_impedance(
        _design({"DDR": {"impedance": 50, "layers": ["In2.Cu"]}},
                {"preset": "jlcpcb:JLC06161H-2116"}))["DDR"]
    assert rec["line"] == "dual_stripline" and rec["primary_layer"] == "In2.Cu"
    in2 = rec["layers"]["In2.Cu"]
    assert in2["ref"] == {"below": ["In4.Cu"], "above": ["In1.Cu"], "nets": ["GND", "+3V3"]}
    assert in2["dielectric_layers"] == ["In3.Cu"]
    assert "ST-W10" in _ids(rec) and rec["accuracy_class"] == "C"
    assert rec["status"] == "fallback_unverified" and set(rec["layers"]) == {"In2.Cu"}
    assert {"In2.Cu", "In3.Cu"} <= set(rec["eligible_layers"])


@FD
def test_rogers_hybrid_matches_the_fd_references():
    out = impedance.resolve_impedance(
        _design({"GNSS_MS": {"impedance": 50, "freq_hz": 1.575e9, "line": "microstrip"},
                 "GNSS_CPWG": {"impedance": 50, "freq_hz": 1.575e9, "ground_gap": 0.2}},
                HYBRID), fd="auto")
    ms, cpwg = out["GNSS_MS"], out["GNSS_CPWG"]
    assert set(ms["layers"]) == set(cpwg["layers"]) == {"F.Cu"}    # RF default: primary outer
    assert ms["line"] == "microstrip" and cpwg["line"] == "cpwg"
    assert ms["layers"]["F.Cu"]["ref"]["nets"] == ["GND"]
    assert ms["status"] == cpwg["status"] == "fd"
    assert ms["fd"]["value"] == pytest.approx(0.4942, rel=0.02)
    assert cpwg["fd"]["value"] == pytest.approx(0.3975, rel=0.02)
    assert cpwg["layers"]["F.Cu"]["ground_gap"] == 0.2 and ms["track_width"] > cpwg["track_width"]


@FD
def test_two_layer_cpwg_matches_the_fd_reference_within_3pct():
    rec = impedance.resolve_impedance(
        _design({"ANT": {"impedance": 50, "freq_hz": 9.15e8, "ground_gap": 0.2}},
                copper_layers=2), fd="auto")["ANT"]
    assert rec["line"] == "cpwg" and rec["status"] == "fd"
    assert rec["track_width"] == pytest.approx(0.8664, rel=0.03)


def test_rf_net_defaults_to_the_primary_outer_layer_and_needs_a_gnd_reference():
    out = impedance.resolve_impedance(_design({"ANT": {"impedance": 50, "freq_hz": 9.15e8}}))
    rec = out["ANT"]
    assert rec["line"] == "cpwg" and list(rec["layers"]) == ["F.Cu"]
    assert rec["eligible_layers"] == ["F.Cu"]          # B.Cu rests on the +3V3 plane: ST-E10
    assert rec["layers"]["F.Cu"]["ground_gap"] == 0.15  # CPWG default gap


def test_layers_on_different_reference_nets_warn_st_w10_and_w11():
    rec = impedance.resolve_impedance(_design({"CLK": {"impedance": 50}}))["CLK"]
    assert set(rec["layers"]) == {"F.Cu", "B.Cu"} and rec["primary_layer"] == "F.Cu"
    assert {"ST-W10", "ST-W11"} <= set(_ids(rec))
    assert rec["layers"]["B.Cu"]["ref"]["nets"] == ["+3V3"]


def test_embedded_microstrip_under_a_routed_layer_is_warned_in_the_record():
    rec = impedance.resolve_impedance(
        _design({"CLK": {"impedance": 50, "layers": ["In1.Cu"]}}, EMBEDDED))["CLK"]
    assert rec["line"] == "embedded_microstrip" and rec["pair"] == "single"
    layer = rec["layers"]["In1.Cu"]
    assert any(w.startswith("In1.Cu: ") and "F.Cu" in w and "routed" in w
               for w in layer["warnings"])
    assert set(layer["warnings"]) <= set(rec["warnings"]) and "notes" not in layer


def test_broadside_pair_warns_that_the_legacy_gap_is_the_face_gap():
    rec = impedance.resolve_impedance(_design(
        {"BS": {"diff": 100, "pair": "broadside", "layers": ["In2.Cu", "In3.Cu"]}},
        {"preset": "jlcpcb:JLC06161H-2116"}))["BS"]
    assert rec["pair"] == "broadside" and rec["line"] == "stripline"
    assert rec["diff_pair_gap"] == pytest.approx(0.4168)
    assert any("face" in w and "diff_pair_gap" in w for w in rec["warnings"])
    usb = impedance.resolve_impedance(_design({"USB": {"diff": 90, "gap": 0.15}}))["USB"]
    assert usb["pair"] == "edge" and not any("face" in w for w in usb["warnings"])


def test_unreachable_target_is_clamped_to_the_fab_minimum_track():
    rec = impedance.resolve_impedance(_design({"RF": {"impedance": 90}}))["RF"]
    assert rec["track_width"] == pytest.approx(0.09)        # JLC multilayer min track
    assert any("bound" in w for w in rec["warnings"])


def test_a_cpwg_wider_than_1_2x_the_chain_pad_shrinks_its_default_gap():
    """ADR D7.6 CPWG default: a width > 1.2 x the narrowest chain pad shrinks the gap."""
    design = _design({"ANT": {"impedance": 50, "freq_hz": 9.15e8}}, copper_layers=2)
    free = impedance.resolve_impedance(design)["ANT"]
    pad = round(free["track_width"] / 1.2 - 0.02, 3)
    tight = impedance.resolve_impedance(design, chain_pads={"ANT": pad})["ANT"]
    assert tight["layers"]["F.Cu"]["ground_gap"] < free["layers"]["F.Cu"]["ground_gap"] == 0.15
    assert tight["track_width"] <= 1.2 * pad < free["track_width"]
    assert tight["z_achieved"] == pytest.approx(50, rel=0.02)
    assert impedance.resolve_impedance(design, chain_pads={"OTHER": pad}) == {"ANT": free}
    ms = _design({"RF": {"impedance": 50}})                   # microstrip: no coplanar gap
    assert impedance.resolve_impedance(ms, chain_pads={"RF": 0.1}) == \
        impedance.resolve_impedance(ms)


def test_a_declared_ground_gap_is_kept_and_warned_against_the_chain_pad():
    design = _design({"ANT": {"impedance": 50, "freq_hz": 9.15e8, "ground_gap": 0.2}},
                     copper_layers=2)
    rec = impedance.resolve_impedance(design, chain_pads={"ANT": 0.5})["ANT"]
    assert rec["layers"]["F.Cu"]["ground_gap"] == 0.2 and rec["track_width"] == 0.895
    hits = [w for w in rec["warnings"] if "chain pad" in w]
    assert len(hits) == 1 and hits[0].startswith("ANT on F.Cu: ") and "ground_gap" in hits[0]
    assert hits[0] in rec["layers"]["F.Cu"]["warnings"]
    ms = _design({"RF": {"impedance": 50, "line": "microstrip", "ground_gap": 0.2}})
    rec = impedance.resolve_impedance(ms, chain_pads={"RF": 0.1})["RF"]   # gap ignored: no rule
    assert rec["line"] == "microstrip" and rec["layers"]["F.Cu"]["ground_gap"] is None
    assert not any("chain pad" in w for w in rec["warnings"])


@pytest.mark.parametrize("pads", [{"ANT": 0}, {"ANT": "wide"}, {"ANT": True},
                                  {"ANT": float("nan")}, ["ANT"]])
def test_malformed_chain_pads_raise(pads):
    with pytest.raises(ValueError, match="chain_pads"):
        impedance.resolve_impedance(_design({"ANT": {"impedance": 50}}), chain_pads=pads,
                                    errors="record")


def test_a_single_ended_z0_the_closed_forms_cannot_give_is_warned(monkeypatch):
    real = synth.evaluate

    def no_single(geom: Any, freq_hz: float = 0.0) -> Any:
        if geom.pair == "single":
            raise ValueError("outside the published validity")
        return real(geom, freq_hz)

    monkeypatch.setattr(synth, "evaluate", no_single)
    rec = impedance.resolve_impedance(_design({"USB": {"diff": 90, "gap": 0.15}}))["USB"]
    assert rec["z0_se"] is None and rec["status"] == "fallback_unverified"
    hits = [w for w in rec["warnings"] if "z0_se" in w]
    assert len(hits) == 1 and hits[0].startswith("USB: ")
    assert "outside the published validity" in hits[0]


def test_explicit_width_is_checked_not_solved():
    """The old 0.405 mm 50-ohm width on bare 7628 evaluates to 47.6 +/- 1 ohm [R-imp]."""
    rec = impedance.resolve_impedance(
        _design({"RF": {"impedance": 50, "width": 0.405}}, {"h": 0.2104, "er": 4.4}))["RF"]
    assert rec["track_width"] == 0.405
    assert rec["z_achieved"] == pytest.approx(47.6, abs=1.0)


def test_as_built_against_a_facade_record(load_design):
    design = load_design("rf_coupon_50r")
    line = impedance.resolve_impedance(design)["RF_IN"]["layers"]["F.Cu"]
    import stackup_model
    rs, _ = stackup_model.resolve(design)
    geom, _ = geo.line_geometry(rs, "F.Cu", {"impedance": 50})
    assert synth.as_built(replace(geom, width=line["width"]), line, width=line["width"]).ok
    assert not synth.as_built(geom, line, width=0.25).ok


# --- resolve_impedance: errors and hygiene --------------------------------------

def test_unresolvable_layer_raises_or_is_recorded():
    design = _design({"RF": {"impedance": 50, "layers": ["In1.Cu"]}})
    with pytest.raises(ValueError, match="In1.Cu"):
        impedance.resolve_impedance(design)
    rec = impedance.resolve_impedance(design, errors="record")["RF"]
    assert rec["status"] == "error" and rec["track_width"] is None
    assert rec["issues"] and rec["issues"][0]["severity"] == "error"


def test_a_line_the_closed_forms_cannot_size_is_an_error(monkeypatch):
    def cannot(*_a: Any, **_k: Any) -> None:
        raise ValueError("no width in [0.09, 5] mm gives a line the closed forms can evaluate")
    monkeypatch.setattr(synth, "synthesize", cannot)
    design = _design({"RF": {"impedance": 50}})
    with pytest.raises(ValueError, match="RF: IMP-E1 F.Cu: no width"):
        impedance.resolve_impedance(design)
    rec = impedance.resolve_impedance(design, errors="record")["RF"]
    assert rec["status"] == "error" and _ids(rec) == ["IMP-E1"]


@pytest.mark.parametrize("spec,key", MALFORMED)
def test_malformed_spec_values_raise_naming_the_net_or_are_recorded(spec, key, no_solves):
    design = _design({"N": spec})
    with pytest.raises(ValueError, match=rf"N: IMP-E2 net_specs\.N\.{key} "):
        impedance.resolve_impedance(design)
    rec = impedance.resolve_impedance(design, errors="record", fd="auto")["N"]
    assert rec["status"] == "error" and _ids(rec) == ["IMP-E2"] and rec["layers"] == {}
    assert rec["issues"][0]["path"] == f"$.net_specs.N.{key}"
    assert LEGACY_SINGLE - {"z0"} <= set(rec) and D76_KEYS <= set(rec)


def test_record_mode_keeps_resolving_the_well_formed_nets():
    out = impedance.resolve_impedance(
        _design({"BAD": {"impedance": 50, "width": "wide"}, "RF": {"impedance": 50}}),
        errors="record")
    assert out["BAD"]["status"] == "error" and out["RF"]["status"] == "closed_form"
    assert out["RF"]["track_width"] > 0


def test_broken_stackup_raises_or_is_recorded():
    design = _design({"RF": {"impedance": 50}}, {"preset": "jlcpcb:NOPE"})
    with pytest.raises(ValueError, match="ST-E4"):
        impedance.resolve_impedance(design)
    rec = impedance.resolve_impedance(design, errors="record")["RF"]
    assert rec["status"] == "error" and "ST-E4" in _ids(rec)


def test_output_is_json_ready_deterministic_and_the_design_is_untouched():
    design = _design({"RF": {"impedance": 50}, "USB": {"diff": 90, "gap": 0.15}})
    before = copy.deepcopy(design)
    a, b = impedance.resolve_impedance(design), impedance.resolve_impedance(design)
    assert design == before
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
    assert json.loads(json.dumps(a)) == a


def test_fd_cache_collects_every_entry():
    assert impedance.fd_cache({}) == {}
    entry = {"hash": "h1", "z": 50.0}
    resolved = {"N": {"layers": {"F.Cu": {"fd": {"cache": {"h1": entry}}},
                                 "B.Cu": {"fd": None}}}}
    assert impedance.fd_cache(resolved) == {"h1": entry}


def test_cli_resolves_a_netlist_and_prints_the_legacy_table(tmp_path):
    netlist = tmp_path / "n.json"
    netlist.write_text(json.dumps(_design({"RF": {"impedance": 50}}, {"h": 0.2, "er": 4.2})),
                       encoding="utf-8")
    run = subprocess.run([sys.executable, str(TOOLS / "impedance.py"), str(netlist), "--fd", "off"],
                         capture_output=True, text=True, check=True, stdin=subprocess.DEVNULL)
    assert json.loads(run.stdout)["RF"]["track_width"] == pytest.approx(0.365)
    table = subprocess.run([sys.executable, str(TOOLS / "impedance.py")], capture_output=True,
                           text=True, check=True, stdin=subprocess.DEVNULL)
    assert "50 ohm single-ended" in table.stdout
