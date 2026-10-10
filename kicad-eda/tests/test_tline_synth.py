"""tline.synth: closed-form synthesis, the D7.6 accuracy policy, FD cache, as-built (D7-07)."""
from __future__ import annotations

import json
import math
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

import decisions
import fieldsolve2d as fs
import tline
from tline import synth
from tline.types import LineGeometry, MaskModel, Ply

JLC = MaskModel(er=3.8, over_trace=0.0152, over_substrate=0.0305, between=0.0305)
FD = pytest.mark.skipif(fs.available() == "none", reason="numpy/scipy not installed")


def _ms(h: float = 0.2104, er: float = 4.4, mask: MaskModel | None = JLC, **kw: Any
        ) -> LineGeometry:
    return LineGeometry(kind="microstrip", pair="single", width=0.0, t=0.035,
                        below=(Ply(h, er),), above=(), mask=mask, **kw)


def _usb(**kw: Any) -> LineGeometry:
    return replace(_ms(), pair="edge", pair_gap=0.15, **kw)


class CountingFd:
    """The real FD tier with every solve counted (or reporting no backend)."""

    FdBudget = fs.FdBudget

    def __init__(self, backend: str | None = None) -> None:
        self.backend = backend
        self.solves = 0

    def available(self) -> str:
        return self.backend or fs.available()

    def solve(self, geom: LineGeometry, **kw: Any) -> fs.FdResult:
        self.solves += 1
        return fs.solve(geom, **kw)

    def synthesize_fd(self, *args: Any, **kw: Any) -> tuple:
        out = fs.synthesize_fd(*args, **kw)
        self.solves += out[2]["solves"]
        return out

    @staticmethod
    def cross_section_hash(*args: Any, **kw: Any) -> str:
        return fs.cross_section_hash(*args, **kw)


# ---------------------------------------------------------------- evaluate
def test_evaluate_applies_the_fallback_to_class_c_only() -> None:
    usb = replace(_usb(), width=0.235)
    c = synth.evaluate(usb)
    assert c.accuracy_class == "C" and "+fallback:masked_coupled_microstrip" in c.model
    assert c.z_diff == pytest.approx(90.0, rel=0.03)
    bare = replace(_ms(mask=None), width=0.37)
    assert synth.evaluate(bare) == tline.evaluate(bare)


# ---------------------------------------------------------------- accuracy policy
def test_class_a_closed_form_is_final_and_needs_no_fd() -> None:
    fd = CountingFd()
    s = synth.synthesize(_ms(0.2, 4.2, mask=None), 50.0, fd=fd)
    assert (s.accuracy_class, s.status, s.fd, s.decision_draft) == ("A", "closed_form", None, None)
    assert s.value == s.geom.width == pytest.approx(0.365)
    assert abs(s.error_pct) < 2.0 and fd.solves == 0
    assert s.width_window == (0.36, 0.37)


@FD
def test_fd_mode_force_verifies_a_class_a_line() -> None:
    fd = CountingFd()
    s = synth.synthesize(_ms(0.2, 4.2, mask=None), 50.0, fd=fd, fd_mode="force")
    assert s.status == "fd" and s.fd["verified"] and s.fd["mode"] == "verify"
    assert fd.solves == 1 and s.value == pytest.approx(0.365)


def test_class_b_without_fd_keeps_the_closed_form_and_drafts_a_decision(tmp_path: Path) -> None:
    s = synth.synthesize(_ms(0.0994, 4.1), 50.0, label="RF on F.Cu")
    assert s.accuracy_class == "B" and s.status == "closed_form" and s.fd is None
    assert s.value < 0.175
    draft = s.decision_draft
    assert (draft["stage"], draft["confidence"]) == ("G3", "low")
    assert "RF on F.Cu" in draft["question"] and "off" in draft["rationale"]
    assert any("numpy" in a for a in draft["alternatives"])
    assert any("budget" in a for a in draft["alternatives"])
    entry = decisions.append_decision(tmp_path / "n.json", **draft)
    assert decisions.validate_decision(entry) == []


@FD
def test_class_b_is_fd_verified_and_corrected() -> None:
    s = synth.synthesize(_ms(0.0994, 4.1), 50.0, fd=fs)
    assert s.accuracy_class == "B" and s.status == "fd" and s.fd["verified"]
    assert s.value < 0.175 and s.decision_draft is None
    assert s.z_achieved == pytest.approx(50.0, rel=0.02)


@FD
def test_class_c_masked_usb_pair_is_fd_synthesised_within_1pct_of_the_reference() -> None:
    s = synth.synthesize(_usb(), 90.0, target_kind="zdiff", fd=fs)
    assert s.accuracy_class == "C" and s.status == "fd" and s.fd["mode"] == "synthesize"
    assert s.fd["value"] == pytest.approx(0.2349, rel=0.01)
    assert s.value == pytest.approx(0.235) and s.fd["verified"] and s.decision_draft is None


def test_class_c_without_fd_is_the_fallback_unverified_with_a_draft() -> None:
    s = synth.synthesize(_usb(), 90.0, target_kind="zdiff", fd=fs, fd_mode="off")
    assert (s.accuracy_class, s.status, s.fd) == ("C", "fallback_unverified", None)
    assert "+fallback:" in s.model and s.value == pytest.approx(0.235)
    assert s.decision_draft["stage"] == "G3" and "fallback" in s.decision_draft["decision"]


def test_budget_zero_runs_no_solve() -> None:
    fd = CountingFd()
    s = synth.synthesize(_usb(), 90.0, target_kind="zdiff", fd=fd, budget=fs.FdBudget(0))
    assert fd.solves == 0 and s.status == "fallback_unverified" and s.run["solves"] == 0
    assert "budget" in s.decision_draft["rationale"]


def test_no_fd_backend_is_reported() -> None:
    s = synth.synthesize(_usb(), 90.0, target_kind="zdiff", fd=CountingFd("none"))
    assert s.status == "fallback_unverified" and "numpy" in s.decision_draft["rationale"]


@FD
def test_fd_cache_by_cross_section_hash_skips_the_solves() -> None:
    first_fd, again_fd = CountingFd(), CountingFd()
    first = synth.synthesize(_usb(), 90.0, target_kind="zdiff", fd=first_fd)
    assert first_fd.solves >= 1 and first.cache and first.run["cache_hits"] == 0
    again = synth.synthesize(_usb(), 90.0, target_kind="zdiff", fd=again_fd, cache=first.cache)
    assert again_fd.solves == 0 and again.run["cache_hits"] >= 1 and again.cache == {}
    assert again.fd == first.fd and again.record() == first.record()
    assert set(first.fd["cache"]) == set(first.cache)


# ---------------------------------------------------------------- geometry rules
def test_width_window_rule() -> None:
    assert synth.width_window(0.365) == (0.36, 0.37)
    assert synth.width_window(1.0) == (0.985, 1.015)


def test_rounding_to_the_grid_picks_the_closer_impedance() -> None:
    s = synth.synthesize(_ms(0.2104, 4.4, mask=None), 50.0, grid=0.01)
    assert s.value == pytest.approx(round(s.value / 0.01) * 0.01)
    other = s.value + (0.01 if s.z_achieved > 50 else -0.01)
    z_other = synth.evaluate(replace(s.geom, width=other)).z0
    assert abs(s.z_achieved - 50) <= abs(z_other - 50)


def test_microstrip_pour_clearance_is_the_smallest_gap_within_1pct() -> None:
    s = synth.synthesize(_ms(), 50.0)
    pc = s.pour_clearance
    z_ms = synth.evaluate(s.geom).z0

    def z_cpwg(gap: float) -> float:
        return synth.evaluate(replace(s.geom, kind="cpwg", ground_gap=gap)).z0

    assert z_cpwg(pc) >= 0.99 * z_ms and z_cpwg(pc - 0.005) < 0.99 * z_ms
    assert pc == pytest.approx(round(pc / 0.005) * 0.005)


def test_pour_clearance_only_for_microstrip() -> None:
    cpwg = replace(_ms(), kind="cpwg", ground_gap=0.2)
    assert synth.synthesize(cpwg, 50.0).pour_clearance is None


def test_cpwg_default_ground_gap_follows_the_fab_minimum() -> None:
    cpwg = replace(_ms(), kind="cpwg")
    assert synth.synthesize(cpwg, 50.0).geom.ground_gap == 0.15
    wide = synth.Bounds(width=(0.1, 5.0), gap=(0.2, 2.0))
    assert synth.synthesize(cpwg, 50.0, bounds=wide).geom.ground_gap == 0.2


def test_cpwg_wider_than_the_chain_pad_shrinks_the_gap() -> None:
    cpwg = replace(_ms(), kind="cpwg")
    free = synth.synthesize(cpwg, 50.0)
    tight = synth.synthesize(cpwg, 50.0, max_width=free.value - 0.02)
    assert tight.geom.ground_gap < free.geom.ground_gap and tight.value < free.value


def test_unreachable_target_is_clamped_to_the_fab_minimum_with_a_warning() -> None:
    s = synth.synthesize(_ms(), 90.0, bounds=synth.Bounds(width=(0.09, 5.0)))
    assert s.value == pytest.approx(0.09) and s.z_achieved < 90.0
    assert any("bound" in w for w in s.warnings)


def test_pair_without_a_gap_ties_the_gap_to_the_width() -> None:
    s = synth.synthesize(replace(_usb(), pair_gap=None), 90.0, target_kind="zdiff",
                         tie_gap=True)
    assert s.geom.pair_gap == s.value and s.result.z_diff == pytest.approx(90.0, rel=0.02)


def test_explicit_width_is_checked_not_solved() -> None:
    s = synth.synthesize(replace(_ms(), width=0.3), 50.0, fixed=True, z_tol_pct=5.0)
    assert s.value == 0.3 and s.z_achieved > 50.0
    assert not any("tolerance" in w for w in s.warnings)
    off = synth.synthesize(replace(_ms(), width=0.2), 50.0, fixed=True, z_tol_pct=5.0)
    assert any("tolerance" in w for w in off.warnings)


def test_frequency_reaches_the_dispersive_closed_form() -> None:
    thick = _ms(1.51, 4.5, mask=None)
    static = synth.synthesize(thick, 50.0)
    at_58 = synth.synthesize(thick, 50.0, freq_hz=5.8e9)
    assert at_58.result.eeff_f > static.result.eeff_f
    assert at_58.z_achieved == pytest.approx(50.0, rel=0.02)


@pytest.mark.parametrize("kwargs", [{"target_kind": "zbogus"}, {"solve_for": "height"},
                                    {"fd_mode": "sometimes"}, {"target_kind": "zdiff"},
                                    {"solve_for": "pair_gap"}])
def test_invalid_requests_raise(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        synth.synthesize(_ms(), 50.0, **kwargs)
    with pytest.raises(ValueError):
        synth.synthesize(_ms(), -5.0)


def test_record_is_json_ready_and_deterministic() -> None:
    a = synth.synthesize(_usb(), 90.0, target_kind="zdiff").record()
    b = synth.synthesize(_usb(), 90.0, target_kind="zdiff").record()
    assert a == b and json.loads(json.dumps(a)) == a
    for key in ("width", "pair_gap", "ground_gap", "pour_clearance", "z_target", "target_kind",
                "z_achieved", "eeff_f", "delay_ps_per_mm", "model", "accuracy_class",
                "est_error_pct", "width_window", "fd", "status", "warnings"):
        assert key in a


# ---------------------------------------------------------------- as built
def test_as_built_class_a_b_re_solves_the_closed_form() -> None:
    rec = synth.synthesize(_ms(), 50.0).record()
    g = replace(_ms(), width=rec["width"])
    ok = synth.as_built(g, rec, width=rec["width"])
    assert ok.ok and ok.method == "closed_form" and abs(ok.error_pct) < 2
    bad = synth.as_built(g, rec, width=rec["width"] * 0.6, z_tol_pct=5.0)
    assert not bad.ok and bad.error_pct > 5 and bad.messages


def test_as_built_class_c_compares_with_the_synthesised_window() -> None:
    rec = synth.synthesize(_usb(), 90.0, target_kind="zdiff").record()
    lo, hi = rec["width_window"]
    g = replace(_usb(), width=rec["width"])
    inside = synth.as_built(g, rec, width=(lo + hi) / 2, pair_gap=0.15)
    assert inside.ok and inside.method == "window" and inside.z is None
    outside = synth.as_built(g, rec, width=hi + 0.01, pair_gap=0.15)
    assert not outside.ok and any("window" in m for m in outside.messages)
    gap_off = synth.as_built(g, rec, width=rec["width"], pair_gap=0.25)
    assert not gap_off.ok and any("pair_gap" in m for m in gap_off.messages)


def test_as_built_needs_a_known_class() -> None:
    with pytest.raises(ValueError):
        synth.as_built(_ms(), {"accuracy_class": "D"}, width=0.3)


def test_window_fields_are_finite() -> None:
    rec = synth.synthesize(_ms(), 50.0).record()
    assert all(math.isfinite(v) for v in rec["width_window"])
