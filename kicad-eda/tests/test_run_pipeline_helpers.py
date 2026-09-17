"""run_pipeline pure helpers: _route_hash (stage-cache key) and _sourcing (G5)."""
from __future__ import annotations

import copy
import re

import pytest

import run_pipeline as rp

HEX64 = re.compile(r"^[0-9a-f]{64}$")


def _hash_after(design: dict, mutate) -> str:
    d = copy.deepcopy(design)
    mutate(d)
    return rp._route_hash(d)


# --- _route_hash ---------------------------------------------------------------

def test_route_hash_is_sha256_hex_and_deterministic(hat_design):
    h = rp._route_hash(hat_design)
    assert HEX64.match(h)
    assert rp._route_hash(copy.deepcopy(hat_design)) == h


def test_route_hash_does_not_mutate_design(hat_design):
    snapshot = copy.deepcopy(hat_design)
    rp._route_hash(hat_design)
    assert hat_design == snapshot


@pytest.mark.parametrize("mutate", [
    lambda d: d["components"][0].__setitem__("value", "CHANGED"),
    lambda d: d["components"][3].__setitem__("lcsc", "C12345"),
    lambda d: d["components"][3].__setitem__("mpn", "ABC-123"),
    lambda d: d["components"][3].__setitem__("thermal", {"p_diss": 9.0}),
    lambda d: d["components"][3].__setitem__("rds_mohm", 5),
    lambda d: d.__setitem__("thermal", {"t_ambient": 85}),
    lambda d: d.__setitem__("title", "renamed"),
    lambda d: d.__setitem__("require_sourcing", True),
    lambda d: d.__setitem__("height_zones", []),
], ids=["value", "lcsc", "mpn", "part-thermal", "rds", "design-thermal", "title",
        "require_sourcing", "height_zones"])
def test_route_hash_stable_under_non_geometric_edits(hat_design, mutate):
    assert _hash_after(hat_design, mutate) == rp._route_hash(hat_design)


@pytest.mark.parametrize("mutate", [
    lambda d: d["components"][0].__setitem__("x", d["components"][0]["x"] + 0.1),
    lambda d: d["components"][0].__setitem__("y", 0.0),
    lambda d: d["components"][0].__setitem__("rot", 0),
    lambda d: d["components"][1].pop("side"),
    lambda d: d["components"][0].__setitem__("footprint", "Other:FP"),
    lambda d: d["components"][3].__setitem__("interior", True),
    lambda d: d["nets"]["GND"].append(["TP1", "1"]),
    lambda d: d["nets"].pop("CC1"),
    lambda d: d["net_specs"]["5V_PI"].__setitem__("current", 5.0),
    lambda d: d["preroutes"][0].__setitem__("width", 2.0),
    lambda d: d["preroutes"].pop(),
    lambda d: d["board"].__setitem__("w", 70.0),
    lambda d: d["mounting_holes"]["positions"].pop(),
    lambda d: d.__setitem__("class", 3),
    lambda d: d.__setitem__("emc", {"max_freq_hz": 1e8}),
    lambda d: d.__setitem__("antenna_keepout", {"x": 0, "y": 0, "w": 5, "h": 5}),
    lambda d: d["components"].pop(),
], ids=["x", "y", "rot", "side", "footprint", "interior", "net-add-pad", "net-drop",
        "net_spec-current", "preroute-width", "preroute-drop", "board", "holes",
        "class", "emc", "antenna_keepout", "component-drop"])
def test_route_hash_changes_under_geometric_edits(hat_design, mutate):
    assert _hash_after(hat_design, mutate) != rp._route_hash(hat_design)


def test_route_hash_ignores_component_order(hat_design):
    shuffled = copy.deepcopy(hat_design)
    shuffled["components"].reverse()
    assert rp._route_hash(shuffled) == rp._route_hash(hat_design)


def test_route_hash_ignores_net_key_order(buck_design):
    reordered = copy.deepcopy(buck_design)
    reordered["nets"] = dict(reversed(list(buck_design["nets"].items())))
    assert rp._route_hash(reordered) == rp._route_hash(buck_design)


def test_route_hash_differs_between_designs(buck_design, hat_design):
    assert rp._route_hash(buck_design) != rp._route_hash(hat_design)


def test_route_hash_of_empty_design_is_well_defined():
    assert HEX64.match(rp._route_hash({}))
    assert rp._route_hash({}) == rp._route_hash({"components": []})


# --- _sourcing -------------------------------------------------------------------

def test_sourcing_examples_carry_no_part_numbers(hat_design):
    rep = rp._sourcing(hat_design)
    tps = {c["ref"] for c in hat_design["components"] if c["ref"].startswith("TP")}
    assert len(tps) == 5
    assert set(rep["missing"]) == {c["ref"] for c in hat_design["components"]} - tps
    assert rep["bad"] == [] and rep["required"] is False


def test_sourcing_lcsc_or_mpn_satisfies():
    d = {"components": [{"ref": "R1", "lcsc": "C12345"},
                        {"ref": "R2", "mpn": "RC0603FR-0710KL"},
                        {"ref": "R3", "lcsc": "C1234567", "mpn": "x"},
                        {"ref": "R4"}]}
    rep = rp._sourcing(d)
    assert rep == {"missing": ["R4"], "bad": [], "required": False}


@pytest.mark.parametrize("lcsc", ["12345", "c12345", "C12", "C-12345", "LCSC C12345", "C12345 "])
def test_sourcing_flags_malformed_lcsc(lcsc):
    rep = rp._sourcing({"components": [{"ref": "U1", "lcsc": lcsc}]})
    assert rep["missing"] == []
    assert rep["bad"] == [f"U1: lcsc '{lcsc}' is not Cxxxxx format"]


@pytest.mark.parametrize("lcsc", ["C123", "C12345", "C1234567890"])
def test_sourcing_accepts_well_formed_lcsc(lcsc):
    assert rp._sourcing({"components": [{"ref": "U1", "lcsc": lcsc}]})["bad"] == []


def test_sourcing_non_string_and_null_values():
    d = {"components": [{"ref": "U1", "lcsc": 12345}, {"ref": "U2", "lcsc": None, "mpn": None},
                        {"ref": "U3", "lcsc": "", "mpn": ""}]}
    rep = rp._sourcing(d)
    assert rep["bad"] == ["U1: lcsc '12345' is not Cxxxxx format"]
    assert rep["missing"] == ["U2", "U3"]


def test_sourcing_exempts_testpoints_holes_fiducials():
    d = {"components": [{"ref": "TP1"}, {"ref": "H3"}, {"ref": "FID2"},
                        {"ref": "TP2", "lcsc": "bad"}, {"ref": "R1"}]}
    rep = rp._sourcing(d)
    assert rep["missing"] == ["R1"] and rep["bad"] == []


def test_sourcing_required_flag_from_design():
    assert rp._sourcing({"components": [], "require_sourcing": True})["required"] is True
    assert rp._sourcing({"components": [], "require_sourcing": 0})["required"] is False
    assert rp._sourcing({})["required"] is False
