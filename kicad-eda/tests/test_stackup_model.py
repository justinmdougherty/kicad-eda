"""stackup_model (ADR D7.2, D7.4 'stackup'): netlist stackup -> ResolvedStackup.

Unit tier:
  * the committed fixtures tests/fixtures/stackups/*.json: the D7.2.2 examples,
    the D7.2.4 defaults (2/8/10L), single-sided, legacy, policy origin and the K10
    spec s6L_hybrid resolve to their pinned source, roles, mask source, issue IDs
    and thickness (1.6284 / 1.3685 / 1.6554 mm, D7.1) with copper + plies + 2 masks
    gbrjob rows; one fixture per error ID returns that ID at its JSON path;
  * D7.2.2 rules: oz defaults, per-ply Dk via the materials library, df default,
    mask default (declared / fab / generic / none), legacy, defaults, single-sided;
  * lint IDs ST-E1..E8, E12 and ST-W1..W9 with JSON paths;
  * kicad_layer_id, walk_outward, accessors, to_dict / digest determinism, no input
    mutation, frozen results.

Live tier (skipped without KiCad 9): the module imports and resolves every fixture
under KiCad 9's bundled Python (stdlib only) to the venv's digests.
"""
from __future__ import annotations

import ast
import copy
import dataclasses
import json
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import load_example

import fab_presets as fp
import materials as mat
import stackup_model as sm

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "server" / "tools"
FIXTURES = ROOT / "tests" / "fixtures" / "stackups"
VALID = sorted(p.stem for p in FIXTURES.glob("*.json") if not p.stem.startswith("st_"))
ERRORS = sorted(p.stem for p in FIXTURES.glob("st_*.json"))
LIVE_TIMEOUT_S = 60
COMP = [{"ref": "J1", "lib_id": "Connector_Generic:Conn_01x02",
         "footprint": "Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical"}]
NETS = {"GND": [["J1", "1"]], "+3V3": [["J1", "2"]]}


@pytest.fixture(scope="module")
def lib() -> mat.MaterialLibrary:
    return mat.load_library()


@pytest.fixture(scope="module")
def fabs() -> fp.FabSet:
    return fp.load_fabs()


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def design(**keys) -> dict:
    return {"name": "t", "components": copy.deepcopy(COMP), "nets": copy.deepcopy(NETS), **keys}


def two_layer(**stackup) -> dict:
    layers = [{"type": "copper", "oz": 1, "role": "signal"},
              {"type": "dielectric", "kind": "core", "material": "generic.fr4", "thickness": 1.51},
              {"type": "copper", "oz": 1, "role": "mixed", "net": "GND"}]
    return {"layers": layers, **stackup}


def run(lib, fabs, d: dict):
    return sm.resolve(d, materials=lib, fabs=fabs)


def ids(issues) -> list[str]:
    return sorted(i.id for i in issues)


def only(issues, ident: str) -> list:
    return [i for i in issues if i.id == ident]


# ---------------------------------------------------------------------------
# 1. committed fixtures
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name", VALID)
def test_fixture_resolves_as_pinned(name, lib, fabs):
    fx = fixture(name)
    exp = fx["expect"]
    rs, issues = run(lib, fabs, fx["design"])
    assert rs is not None, [str(i) for i in issues]
    assert rs.source == exp["source"] and rs.fab == exp["fab"]
    assert rs.copper_layers == exp["copper_layers"]
    assert rs.kicad_copper_count == exp["kicad_copper_count"] == len(rs.copper)
    assert rs.board_thickness_computed == pytest.approx(exp["thickness"], abs=1e-4)
    rows = len(rs.copper) + sum(len(d.plies) for d in rs.dielectrics) + 2
    assert rows == exp["gbrjob_rows"]
    assert [[c.name, c.role, c.net] for c in rs.copper] == exp["roles"]
    assert rs.mask.source == exp["mask_source"]
    assert ids(issues) == sorted(exp["issues"])
    assert len(rs.decision_drafts) == exp["drafts"]
    assert all(i.severity in ("warning", "info") for i in issues)


@pytest.mark.parametrize("name", ERRORS)
def test_error_fixture_returns_its_id_and_path(name, lib, fabs):
    fx = fixture(name)
    rs, issues = run(lib, fabs, fx["design"])
    want = (fx["expect"]["error"], "error", fx["expect"]["path"])
    assert want in [(i.id, i.severity, i.path) for i in issues], [str(i) for i in issues]
    if want[0] == "ST-E12":         # a design violation: the stackup itself still resolves
        assert rs is not None and rs.single_sided
    else:
        assert rs is None


def test_every_error_id_has_a_fixture():
    assert sorted(fixture(n)["expect"]["error"] for n in ERRORS) == sorted(
        [f"ST-E{k}" for k in range(1, 9)] + ["ST-E12"])


def test_thickness_pins_from_d71(lib, fabs):
    got = {n: run(lib, fabs, fixture(n)["design"])[0].board_thickness_computed
           for n in ("s6l_hybrid", "d722_4l_hybrid", "d722_6l_2116")}
    assert got == pytest.approx({"s6l_hybrid": 1.6284, "d722_4l_hybrid": 1.3685,
                                 "d722_6l_2116": 1.6554}, abs=1e-4)


# ---------------------------------------------------------------------------
# 2. D7.2.2 rules
# ---------------------------------------------------------------------------
def test_oz_defaults_outer_1_inner_half_and_explicit_thickness_wins(lib, fabs):
    pp = {"type": "dielectric", "kind": "prepreg", "material": "generic.fr4", "thickness": 0.2}
    layers = [{"type": "copper"}, pp,
              {"type": "copper", "role": "plane", "net": "GND"},
              {"type": "dielectric", "kind": "core", "material": "generic.fr4", "thickness": 1.0},
              {"type": "copper", "role": "plane", "net": "+3V3", "thickness": 0.0152},
              pp, {"type": "copper", "oz": 2}]
    rs, issues = run(lib, fabs, design(stackup={"layers": layers}))
    assert ids(issues) == ["ST-W2"]                 # 1 oz top vs 2 oz bottom
    assert [(c.oz, c.thickness) for c in rs.copper] == [
        (1, 0.035), (0.5, 0.0175), (0.434, 0.0152), (2, 0.07)]
    assert [c.role for c in rs.copper] == ["signal", "plane", "plane", "signal"]
    assert [c.kicad_type for c in rs.copper] == ["signal", "power", "power", "signal"]
    assert [c.kicad_id for c in rs.copper] == [0, 4, 6, 2]


def test_per_ply_dk_comes_from_the_library_with_provenance(lib, fabs):
    rs, issues = run(lib, fabs, fixture("d722_4l_hybrid")["design"])
    ro4350b = rs.dielectrics[0].plies[0]
    assert (ro4350b.er, ro4350b.er_basis, ro4350b.material_id) == (3.66, "design", "rogers.ro4350b")
    assert ro4350b.er_source.startswith("https://") and ro4350b.df == pytest.approx(0.0031)
    assert rs.dielectrics[0].locked and rs.dielectrics[0].name == "dielectric 1"
    assert [p.material_id for p in rs.dielectrics[1].plies] == ["rogers.ro4450f", "isola.370hr"]
    assert rs.dielectrics[1].plies[0].er == 3.52 and rs.dielectrics[1].kind == "prepreg"
    w5 = only(issues, "ST-W5")
    assert [i.path for i in w5] == ["$.stackup.layers[1]"] and "0.254" in w5[0].message
    assert sorted(i.path for i in only(issues, "ST-W6")) == [
        "$.stackup.layers[1]", "$.stackup.layers[3].plies[0]"]
    assert all(i.severity == "info" for i in only(issues, "ST-W6"))


def test_jlc_preset_plies_take_the_fab_dk(lib, fabs):
    rs, _ = run(lib, fabs, design(stackup={"preset": "jlcpcb:JLC04161H-7628"}))
    pp = rs.dielectrics[0].plies[0]
    assert (pp.er, pp.er_basis, pp.construction) == (4.4, "fab", "7628")
    assert rs.dielectrics[1].plies[0].er == 4.43 and rs.dielectrics[1].kind == "core"


def test_explicit_dk_wins_and_unknown_material_is_a_label(lib, fabs):
    rs, _ = run(lib, fabs, fixture("s6l_hybrid")["design"])
    core = rs.dielectrics[0].plies[0]
    assert (core.er, core.er_basis, core.er_freq_hz, core.material_id) == (
        3.66, "explicit", 1e10, "rogers.ro4350b")
    fr4 = rs.dielectrics[2].plies[0]
    assert (fr4.material_id, fr4.label, fr4.er, fr4.df) == (None, "FR4 core", 4.6, 0.02)


def test_missing_df_defaults_to_0_02_with_an_info(lib, fabs):
    st = two_layer()
    st["layers"][1] = {"type": "dielectric", "kind": "core", "material": "my-laminate",
                       "dk": 3.9, "thickness": 1.5}
    rs, issues = run(lib, fabs, design(stackup=st))
    assert rs.dielectrics[0].plies[0].df == 0.02
    info = [i for i in issues if "df" in i.message]
    assert [(i.id, i.severity, i.path) for i in info] == [("ST-W6", "info", "$.stackup.layers[1]")]


@pytest.mark.parametrize("stackup, source, dk", [
    ({}, "fab", 3.8),
    ({"mask": None}, "none", None),
    ({"fab": None}, "generic", 3.8),
    ({"fab": "pcbway"}, "generic", 3.8),
    ({"mask": {"over_substrate": 0.02, "dk": 3.5}}, "declared", 3.5),
])
def test_mask_sources(lib, fabs, stackup, source, dk):
    st = two_layer(fab="jlcpcb", **stackup) if "fab" not in stackup else two_layer(**stackup)
    rs, issues = run(lib, fabs, design(stackup=st))
    assert rs.mask.source == source and rs.mask.dk == dk
    masks = {"fab": 0.0305, "generic": 0.0305, "declared": 0.02, "none": 0.0}[source]
    assert rs.board_thickness_computed == pytest.approx(1.58 + 2 * masks, abs=1e-6)
    w9 = ["$.stackup.mask"] if source == "none" else []
    assert [i.path for i in only(issues, "ST-W9")] == w9
    if source == "declared":       # missing keys come from the fab / generic mask
        assert (rs.mask.over_trace, rs.mask.between_traces) == (0.0152, 0.0305)


def test_legacy_shorthand(lib, fabs):
    rs, issues = run(lib, fabs, load_example("rf_coupon_50r"))
    assert rs.source == "legacy" and rs.legacy == {"h": 0.2, "er": 4.2, "t": 0.035}
    assert rs.copper_names() == ("F.Cu", "In1.Cu", "In2.Cu", "B.Cu")
    assert rs.planes() == {"In1.Cu": "GND", "In2.Cu": "GND"}
    top, bottom = rs.dielectrics[0].plies, rs.dielectrics[-1].plies
    assert top == bottom and len(top) == 1 and (top[0].thickness, top[0].er) == (0.2, 4.2)
    assert rs.mask.source == "none" and not rs.impedance_controlled
    assert [(i.id, i.path) for i in issues] == [("ST-W7", "$.stackup")]


def test_legacy_t_sets_the_outer_copper_and_copper_layers_is_honoured(lib, fabs):
    rs, _ = run(lib, fabs, design(stackup={"h": 1.4, "er": 4.5, "t": 0.07}, copper_layers=2))
    assert rs.kicad_copper_count == 2 and [c.thickness for c in rs.copper] == [0.07, 0.07]
    assert rs.dielectrics[0].plies[0].thickness == 1.4


def test_rf_lna_and_buck_keep_todays_in2_rail(lib, fabs):
    lna, _ = run(lib, fabs, load_example("rf_lna_gainblock"))
    buck, _ = run(lib, fabs, load_example("buck_5v3a"))
    assert lna.planes() == {"In1.Cu": "GND", "In2.Cu": "VBIAS"}
    assert buck.planes() == {"In1.Cu": "GND", "In2.Cu": "VIN_RAW"} and buck.source == "default"


def test_nothing_declared_is_the_4l_jlc04161h_7628_default(lib, fabs):
    rs, issues = run(lib, fabs, load_example("cp2102n_uart"))
    assert (rs.source, rs.preset, rs.fab, rs.kicad_copper_count) == (
        "default", "jlcpcb:JLC04161H-7628", "jlcpcb", 4)
    assert rs.planes() == {"In1.Cu": "GND", "In2.Cu": "+3V3"} and issues == ()


def test_a_model_without_preset_or_layers_keeps_the_default_geometry(lib, fabs):
    rs, issues = run(lib, fabs, design(copper_layers=4, stackup={
        "rationale": "4L: GND under the feeds", "finish": "ENIG", "fab": "pcbway"}))
    assert (rs.source, rs.preset, rs.fab, rs.rationale) == (
        "default", "jlcpcb:JLC04161H-7628", "pcbway", "4L: GND under the feeds")
    assert rs.mask.source == "generic" and rs.finish == "ENIG" and issues == ()


def test_origin_layer_policy_reports_source_policy_for_every_shape(lib, fabs):
    for st in ({"preset": "jlcpcb:JLC04161H-3313", "origin": "layer_policy"},
               two_layer(origin="layer_policy"), {"origin": "layer_policy"}):
        rs, _ = run(lib, fabs, design(stackup=st))
        assert rs.source == "policy" and rs.origin == "layer_policy"
    rs, _ = run(lib, fabs, design(stackup=two_layer(origin="designer")))
    assert rs.source == "layers" and rs.origin == "designer"


def test_design_freq_defaults_to_the_highest_net_freq(lib, fabs):
    d = design(stackup=two_layer(), net_specs={"A": {"freq_hz": 9.15e8}, "B": {"freq_hz": 1.575e9}})
    assert run(lib, fabs, d)[0].design_freq_hz == 1.575e9
    assert run(lib, fabs, design(stackup=two_layer()))[0].design_freq_hz == 1e9
    rs, _ = run(lib, fabs, design(stackup=two_layer(design_freq_hz=2.4e9, dk_policy="process")))
    assert (rs.design_freq_hz, rs.dk_policy) == (2.4e9, "process")


def test_defaulted_plane_nets_become_g1_decision_drafts(lib, fabs, tmp_path):
    import decisions
    rs, _ = run(lib, fabs, fixture("d722_6l_2116")["design"])
    drafts = rs.draft_dicts()
    assert [d["decision"] for d in drafts] == ["In1.Cu carries GND", "In4.Cu carries +3V3"]
    for draft in drafts:
        decisions.append_decision(tmp_path / "n.json", **draft)
    assert len(decisions.read_decisions(tmp_path / "n.json")) == 2
    assert [c.net_source for c in rs.copper] == [None, "default", None, None, "default", None]
    assert "decision_drafts" not in rs.to_dict()


def test_impedance_control_and_tolerance(lib, fabs):
    rs, _ = run(lib, fabs, fixture("d722_jlc3313_in2_mixed")["design"])
    assert rs.impedance_controlled and rs.impedance_tolerance_pct == 10
    rs, _ = run(lib, fabs, design(stackup=two_layer()))
    assert not rs.impedance_controlled


def test_single_sided_layers_form_and_preset_form(lib, fabs):
    st = two_layer()
    st["layers"][2] = {"type": "copper", "role": "unused"}
    rs, issues = run(lib, fabs, design(stackup=st))
    assert (rs.copper_layers, rs.single_sided, issues) == (1, True, ())
    assert rs.routable_layers() == ("F.Cu",) and rs.planes() == {}
    rs, issues = run(lib, fabs, design(copper_layers=1, stackup={"preset": "generic:2L-1.6"}))
    assert rs.single_sided and rs.copper[1].role == "unused" and issues == ()


# ---------------------------------------------------------------------------
# 3. errors (beyond the fixtures) with JSON paths
# ---------------------------------------------------------------------------
def _err(lib, fabs, d: dict) -> list[tuple]:
    rs, issues = run(lib, fabs, d)
    errs = [(i.id, i.path) for i in issues if i.severity == "error"]
    assert errs and (rs is None) == any(e[0] != "ST-E12" for e in errs)
    return errs


@pytest.mark.parametrize("st, want", [
    ({"layers": []}, ("ST-E1", "$.stackup.layers")),
    ({"layers": [{"type": "dielectric", "thickness": 1}]}, ("ST-E1", "$.stackup.layers[0]")),
    ({"layers": [{"type": "copper"}, {"type": "core", "thickness": 1}, {"type": "copper"}]},
     ("ST-E1", "$.stackup.layers[1].type")),
    ({"layers": [{"type": "copper"}, {"type": "dielectric", "kind": "prepreg"},
                 {"type": "copper"}]}, ("ST-E1", "$.stackup.layers[1]")),
    ({"layers": [{"type": "copper"}, {"type": "dielectric", "plies": []}, {"type": "copper"}]},
     ("ST-E1", "$.stackup.layers[1].plies")),
])
def test_structure_errors_are_st_e1(lib, fabs, st, want):
    assert want in _err(lib, fabs, design(stackup=st))


def test_odd_and_huge_counts_are_st_e2(lib, fabs):
    cu = {"type": "copper"}
    die = {"type": "dielectric", "material": "generic.fr4", "thickness": 0.5}
    assert ("ST-E2", "$.stackup.layers") in _err(lib, fabs, design(stackup={
        "layers": [cu, die, cu, die, cu]}))
    assert ("ST-E2", "$.stackup.layers") in _err(lib, fabs, design(stackup={
        "layers": [cu] + [die, cu] * 33}))
    for bad in (3, 0, 34, "4", 4.0, True):
        assert ("ST-E2", "$.copper_layers") in _err(lib, fabs, design(copper_layers=bad))


@pytest.mark.parametrize("st, path", [
    ({"h": 0.2, "preset": "jlcpcb:JLC04161H-7628"}, "$.stackup"),
    ({"layers": two_layer()["layers"], "roles": {"B.Cu": {"role": "signal"}}}, "$.stackup.roles"),
    ({"preset": "jlcpcb:JLC04161H-7628", "colour": "red"}, "$.stackup.colour"),
    ({"preset": "jlcpcb:JLC04161H-7628", "dk_policy": "best"}, "$.stackup.dk_policy"),
    ({"preset": "jlcpcb:JLC04161H-7628", "origin": "napkin"}, "$.stackup.origin"),
    ({"preset": "jlcpcb:JLC04161H-7628", "finish": "gold"}, "$.stackup.finish"),
    ({"preset": "jlcpcb:JLC04161H-7628", "mask": {"thickness": 0.01}}, "$.stackup.mask.thickness"),
    ({"preset": "jlcpcb:JLC04161H-7628", "impedance_control": {"required": "yes"}},
     "$.stackup.impedance_control.required"),
    ({"layers": [{"type": "copper", "weight": 1}, {"type": "dielectric", "thickness": 1.5,
                                                   "dk": 4}, {"type": "copper"}]},
     "$.stackup.layers[0].weight"),
    ("4L", "$.stackup"),
])
def test_shape_errors_are_st_e3(lib, fabs, st, path):
    assert ("ST-E3", path) in _err(lib, fabs, design(stackup=st))


@pytest.mark.parametrize("st, path", [
    ({"preset": "nofab:X"}, "$.stackup.preset"),
    ({"preset": "JLC04161H-7628"}, "$.stackup.preset"),
    ({"preset": "jlcpcb:JLC04161H-7628", "fab": "acme"}, "$.stackup.fab"),
    ({"layers": [{"type": "copper"}, {"type": "dielectric", "material": "acme.x", "thickness": 1},
                 {"type": "copper"}]}, "$.stackup.layers[1].material"),
    ({"layers": [{"type": "copper"}, {"type": "dielectric", "thickness": 1}, {"type": "copper"}]},
     "$.stackup.layers[1]"),
    ({"preset": "jlcpcb:JLC04161H-7628", "mask": {"material": "acme ink"}},
     "$.stackup.mask.material"),
])
def test_unknown_presets_fabs_and_materials_are_st_e4(lib, fabs, st, path):
    assert ("ST-E4", path) in _err(lib, fabs, design(stackup=st))


def test_no_template_for_the_count_is_st_e4(lib, tmp_path):
    (tmp_path / "generic.json").write_bytes((fp.DEFAULT_DIR / "generic.json").read_bytes())
    only_generic = fp.load_fabs(tmp_path)
    assert ("ST-E4", "$.copper_layers") in _err(lib, only_generic, design(copper_layers=4))


def test_an_ordered_thickness_picks_the_nearest_default_template(lib, fabs):
    rs, _ = run(lib, fabs, design(copper_layers=4, stackup={"board_thickness": 1.0}))
    assert rs.preset == "jlcpcb:NOREQ-4L-1mm-1oz-0.5oz" and rs.board_thickness_ordered == 1.0
    rs, issues = run(lib, fabs, design(copper_layers=2, stackup={"board_thickness": 0.8}))
    assert rs.preset == "generic:2L-1.6" and ids(issues) == ["ST-W1"]


@pytest.mark.parametrize("field, value, path", [
    ("dk", 20, "$.stackup.layers[1].dk"), ("dk", 0.5, "$.stackup.layers[1].dk"),
    ("df", 0.5, "$.stackup.layers[1].df"), ("df", -0.01, "$.stackup.layers[1].df"),
    ("thickness", 0, "$.stackup.layers[1].thickness"),
    ("thickness", "1.5", "$.stackup.layers[1].thickness"),
    ("dk_freq_hz", 0, "$.stackup.layers[1].dk_freq_hz"),
])
def test_numeric_ranges_are_st_e5(lib, fabs, field, value, path):
    st = two_layer()
    st["layers"][1] = {**st["layers"][1], "dk": 4.5, field: value}
    assert ("ST-E5", path) in _err(lib, fabs, design(stackup=st))


@pytest.mark.parametrize("extra, path", [
    ({"board_thickness": -1}, "$.stackup.board_thickness"),
    ({"design_freq_hz": 0}, "$.stackup.design_freq_hz"),
    ({"mask": {"over_substrate": -0.01}}, "$.stackup.mask.over_substrate"),
    ({"mask": {"dk": 30}}, "$.stackup.mask.dk"),
    ({"impedance_control": {"required": True, "tolerance_pct": 0}},
     "$.stackup.impedance_control.tolerance_pct"),
])
def test_stackup_level_numbers_are_st_e5(lib, fabs, extra, path):
    assert ("ST-E5", path) in _err(lib, fabs, design(stackup=two_layer(**extra)))


def test_copper_oz_and_thickness_are_st_e5(lib, fabs):
    st = two_layer()
    st["layers"][0] = {"type": "copper", "oz": 0}
    st["layers"][2] = {"type": "copper", "thickness": -0.035}
    errs = _err(lib, fabs, design(stackup=st))
    assert ("ST-E5", "$.stackup.layers[0].oz") in errs
    assert ("ST-E5", "$.stackup.layers[2].thickness") in errs


def test_role_problems_in_the_layers_form_are_st_e6(lib, fabs):
    st = two_layer()
    st["layers"][0] = {"type": "copper", "role": "unused"}
    assert ("ST-E6", "$.stackup.layers[0].role") in _err(lib, fabs, design(stackup=st))
    st["layers"][0] = {"type": "copper", "role": "signal", "net": "GND"}
    assert ("ST-E6", "$.stackup.layers[0].net") in _err(lib, fabs, design(stackup=st))
    st["layers"][0] = {"type": "copper", "role": "planar"}
    assert ("ST-E6", "$.stackup.layers[0].role") in _err(lib, fabs, design(stackup=st))
    errs = _err(lib, fabs, design(stackup=two_layer(), planes={"B.Cu": "+3V3"}))
    assert ("ST-E6", "$.planes['B.Cu']") in errs


def test_copper_layers_disagreeing_with_layers_is_st_e7(lib, fabs):
    assert ("ST-E7", "$.copper_layers") in _err(lib, fabs, design(copper_layers=4,
                                                                    stackup=two_layer()))
    st = two_layer()
    st["layers"][2] = {"type": "copper", "role": "unused"}
    assert ("ST-E7", "$.copper_layers") in _err(lib, fabs, design(copper_layers=2, stackup=st))


def test_copper_layers_1_with_a_two_copper_stack_is_single_sided(lib, fabs):
    st = two_layer()
    st["layers"][2] = {"type": "copper"}
    rs, issues = run(lib, fabs, design(copper_layers=1, stackup=st))
    assert rs.single_sided and issues == ()


@pytest.mark.parametrize("spans, path", [
    ([["F.Cu", "In1.Cu"]], "$.stackup.via_spans[0]"),
    ([["F.Cu", "B.Cu"], ["In1.Cu", "In2.Cu"]], "$.stackup.via_spans[1]"),
    ([["F.Cu"]], "$.stackup.via_spans[0]"),
    ("through", "$.stackup.via_spans"),
])
def test_non_through_via_spans_are_st_e8(lib, fabs, spans, path):
    st = {"preset": "jlcpcb:JLC04161H-7628", "via_spans": spans}
    assert ("ST-E8", path) in _err(lib, fabs, design(stackup=st))


def test_through_spans_either_way_round_are_fine(lib, fabs):
    st = {"preset": "jlcpcb:JLC04161H-7628", "via_spans": [["B.Cu", "F.Cu"]]}
    rs, issues = run(lib, fabs, design(stackup=st))
    assert issues == () and rs.via_spans == (("F.Cu", "B.Cu"),)


@pytest.mark.parametrize("keys, path", [
    ({"preroutes": [{"net": "GND", "width": 0.5, "layer": "B.Cu", "path": []}]},
     "$.preroutes[0].layer"),
    ({"preroutes": [{"net": "GND", "width": 0.5, "path": [], "vias": [[1, 1]]}]},
     "$.preroutes[0].vias"),
    ({"emc": {"stitch": True}}, "$.emc.stitch"),
    ({"net_specs": {"RF_IN": {"impedance": 50}}}, "$.net_specs.RF_IN"),
    ({"net_specs": {"+USB": {"diff": 90}}}, "$.net_specs['+USB']"),
    ({"net_specs": {"ANT": {"freq_hz": 9.15e8}}}, "$.net_specs.ANT"),
])
def test_single_sided_violations_are_st_e12(lib, fabs, keys, path):
    assert ("ST-E12", path) in _err(lib, fabs, design(copper_layers=1, **keys))


def test_single_sided_tht_on_b_and_power_specs_are_fine(lib, fabs):
    rs, issues = run(lib, fabs, fixture("single_sided")["design"])
    assert issues == ()
    d = design(copper_layers=1, net_specs={"GND": {"voltage": 0, "current": 1}})
    assert run(lib, fabs, d)[1] == ()


# ---------------------------------------------------------------------------
# 4. warnings
# ---------------------------------------------------------------------------
def test_w1_ordered_vs_computed_thickness(lib, fabs):
    rs, issues = run(lib, fabs, design(stackup=two_layer(fab="jlcpcb", board_thickness=2.0)))
    assert rs.board_thickness_ordered == 2.0
    assert [i.path for i in only(issues, "ST-W1")] == ["$.stackup.board_thickness"]
    _, issues = run(lib, fabs, design(stackup=two_layer(fab="jlcpcb", board_thickness=1.6)))
    assert only(issues, "ST-W1") == []
    _, issues = run(lib, fabs, design(stackup=two_layer(board_thickness=1.9)))     # no fab
    assert [i.id for i in issues] == ["ST-W1"]


def test_w2_marks_asymmetric_builds_only(lib, fabs):
    _, issues = run(lib, fabs, fixture("d722_4l_hybrid")["design"])
    assert [i.path for i in only(issues, "ST-W2")] == ["$.stackup.layers"]
    st = two_layer()
    st["layers"][2] = {"type": "copper", "oz": 2, "role": "mixed", "net": "GND"}
    assert ids(run(lib, fabs, design(stackup=st))[1]) == ["ST-W2"]


def test_w3_fab_capability(lib, fabs):
    _, issues = run(lib, fabs, design(stackup=two_layer(
        fab="jlcpcb", impedance_control={"required": True})))
    assert [(i.id, i.path) for i in issues] == [("ST-W3", "$.stackup.fab")]
    assert "impedance" in issues[0].message
    hybrid = fixture("d722_4l_hybrid")["design"]
    hybrid = {**hybrid, "stackup": {**hybrid["stackup"], "fab": "jlcpcb"}}
    w3 = [i.message for i in only(run(lib, fabs, hybrid)[1], "ST-W3")]
    assert any("hybrid" in m for m in w3)


def test_w4_ro4450f_copper_fill_per_ply(lib, fabs):
    layers = [{"type": "copper", "oz": 1},
              {"type": "dielectric", "kind": "prepreg", "material": "rogers.ro4450f",
               "thickness": 0.101},
              {"type": "copper", "oz": 1, "role": "mixed", "net": "GND"}]
    _, issues = run(lib, fabs, design(stackup={"layers": layers}))
    assert [i.path for i in only(issues, "ST-W4")] == ["$.stackup.layers[1]"]
    _, issues = run(lib, fabs, fixture("d722_4l_hybrid")["design"])
    assert only(issues, "ST-W4") == []         # its RO4450F ply faces 35 um only


def test_w8_only_on_reference_dielectrics_of_impedance_designs(lib, fabs):
    rs, issues = run(lib, fabs, fixture("d722_jlc3313_in2_mixed")["design"])
    assert [i.path for i in only(issues, "ST-W8")] == ["$.stackup.preset"]
    _, issues = run(lib, fabs, design(stackup={"preset": "jlcpcb:JLC04161H-3313"}))
    assert only(issues, "ST-W8") == []          # no impedance net: Dk does not matter


def test_warnings_are_listed_on_the_stackup(lib, fabs):
    rs, issues = run(lib, fabs, fixture("d722_4l_hybrid")["design"])
    assert len(rs.warnings) == len(issues)
    assert all(w.startswith("ST-W") for w in rs.warnings)


# ---------------------------------------------------------------------------
# 5. layer ids, walking, accessors, canonical form
# ---------------------------------------------------------------------------
def test_kicad_layer_id():
    assert [sm.kicad_layer_id(n) for n in ("F.Cu", "B.Cu", "In1.Cu", "In2.Cu", "In30.Cu")] == [
        0, 2, 4, 6, 62]
    for bad in ("In0.Cu", "In31.Cu", "F.Mask", "in1.cu", ""):
        with pytest.raises(ValueError):
            sm.kicad_layer_id(bad)


def test_walk_outward_orders_plies_away_from_the_layer(lib, fabs):
    rs, _ = run(lib, fabs, fixture("d722_6l_2116")["design"])
    up = sm.walk_outward(rs, "In2.Cu", "up")
    assert [x.name for x in up] == ["dielectric 2", "In1.Cu", "dielectric 1", "F.Cu"]
    assert [p.construction for p in up[2].plies] == ["2313", "2116"]       # nearest In1 first
    down = sm.walk_outward(rs, "In2.Cu", "down")
    assert [x.name for x in down][:3] == ["dielectric 3", "In3.Cu", "dielectric 4"]
    assert sm.walk_outward(rs, "F.Cu", "up") == ()
    with pytest.raises(ValueError):
        sm.walk_outward(rs, "In2.Cu", "sideways")
    with pytest.raises(KeyError):
        sm.walk_outward(rs, "In9.Cu", "up")


def test_accessors(lib, fabs):
    rs, _ = run(lib, fabs, fixture("d722_jlc3313_in2_mixed")["design"])
    assert rs.copper_names() == ("F.Cu", "In1.Cu", "In2.Cu", "B.Cu")
    assert rs.layer("In2.Cu").net == "+3V3" and rs.layer("In2.Cu").kicad_type == "mixed"
    assert rs.plane_layers() == ("In1.Cu",) and rs.signal_layers() == ("F.Cu", "B.Cu")
    assert rs.routable_layers() == ("F.Cu", "In2.Cu", "B.Cu")
    assert rs.planes() == {"In1.Cu": "GND", "In2.Cu": "+3V3"}
    with pytest.raises(KeyError):
        rs.layer("In3.Cu")
    assert rs.dielectrics[0].thickness == pytest.approx(0.0994)


def test_to_dict_has_the_d74_fields_and_is_json(lib, fabs):
    rs, _ = run(lib, fabs, fixture("d722_4l_hybrid")["design"])
    d = rs.to_dict()
    assert {"source", "origin", "fab", "kicad_copper_count", "copper", "dielectrics", "mask",
            "finish", "impedance_controlled", "board_thickness_computed", "design_freq_hz",
            "dk_policy", "rationale", "warnings"} <= set(d)
    assert set(d["copper"][0]) >= {"name", "kicad_id", "thickness", "oz", "role", "net",
                                   "kicad_type"}
    assert set(d["dielectrics"][1]) >= {"name", "kind", "locked", "plies"}
    assert set(d["dielectrics"][1]["plies"][0]) >= {
        "thickness", "er", "df", "er_basis", "er_freq_hz", "er_source", "material_id", "label",
        "construction"}
    assert d["mask"]["source"] == "generic"
    assert json.loads(json.dumps(d)) == d


@pytest.mark.parametrize("name", VALID)
def test_resolution_is_deterministic_and_never_mutates_the_design(name, lib, fabs):
    d = fixture(name)["design"]
    before = copy.deepcopy(d)
    a, ia = run(lib, fabs, d)
    b, ib = run(lib, fabs, d)
    assert d == before and a == b and ia == ib
    assert a.digest() == b.digest() and len(a.digest()) == 64
    assert json.dumps(a.to_dict(), sort_keys=True) == json.dumps(b.to_dict(), sort_keys=True)


def test_digest_changes_with_the_stackup(lib, fabs):
    a, _ = run(lib, fabs, design(stackup=two_layer()))
    b, _ = run(lib, fabs, design(stackup=two_layer(finish="ENIG")))
    assert a.digest() != b.digest()


def test_results_are_frozen(lib, fabs):
    rs, issues = run(lib, fabs, fixture("d722_4l_hybrid")["design"])
    for obj, field in ((rs, "source"), (rs.copper[0], "net"), (rs.dielectrics[0], "kind"),
                       (rs.dielectrics[0].plies[0], "er"), (rs.mask, "dk"), (issues[0], "id")):
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(obj, field, None)


def test_default_libraries_load_when_not_given():
    rs, issues = sm.resolve(fixture("d722_6l_2116")["design"])
    assert rs.board_thickness_computed == pytest.approx(1.6554, abs=1e-4)


# ---------------------------------------------------------------------------
# 6. stdlib only; KiCad 9's bundled Python
# ---------------------------------------------------------------------------
def test_module_is_stdlib_only_and_short():
    text = (TOOLS / "stackup_model.py").read_text(encoding="utf-8")
    names: set[str] = set()
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add((node.module or "").split(".")[0])
    ours = {"materials", "fab_presets", "stackup_roles"}
    assert not {n for n in names if n not in sys.stdlib_module_names and n not in ours}
    assert len(text.splitlines()) < 800


CHILD = (
    "import json, sys\n"
    "sys.path.insert(0, sys.argv[1])\n"
    "{quiet}"
    "before = set(sys.modules)\n"
    "import stackup_model as sm\n"
    "out = {{}}\n"
    "for path in sys.argv[2:]:\n"
    "    rs, issues = sm.resolve(json.load(open(path, encoding='utf-8'))['design'])\n"
    "    out[path] = [rs.digest() if rs else None, sorted(i.id for i in issues)]\n"
    "ours = ('stackup_model', 'stackup_roles', 'fab_presets', 'materials')\n"
    "new = sorted(x for x in set(sys.modules) - before\n"
    "             if x.split('.')[0] not in sys.stdlib_module_names and x not in ours)\n"
    "print(json.dumps({{'py': list(sys.version_info[:2]), 'out': out, 'new': new}}))\n")


def _expected_child_output() -> dict:
    out = {}
    for path in sorted(FIXTURES.glob("*.json")):
        rs, issues = sm.resolve(json.loads(path.read_text(encoding="utf-8"))["design"])
        out[str(path)] = [rs.digest() if rs else None, sorted(i.id for i in issues)]
    return out


def _run_child(python: str, quiet: str, tmp_path: Path, *flags: str) -> dict:
    child = tmp_path / "child.py"
    child.write_text(CHILD.format(quiet=quiet), encoding="utf-8")
    paths = [str(p) for p in sorted(FIXTURES.glob("*.json"))]
    proc = subprocess.run([python, *flags, str(child), str(TOOLS), *paths], capture_output=True,
                          text=True, stdin=subprocess.DEVNULL, timeout=LIVE_TIMEOUT_S,
                          encoding="utf-8", errors="replace")
    assert proc.returncode == 0, proc.stderr
    got = json.loads(proc.stdout.strip().splitlines()[-1])
    assert got["new"] == [] and got["out"] == _expected_child_output()
    return got


def test_runs_with_no_site_packages(tmp_path):
    _run_child(sys.executable, "", tmp_path, "-I", "-S")


@pytest.fixture(scope="module")
def kicad9_python() -> Path:
    import kicad_env as env
    for inst in env.discover():
        if inst.version_tuple[0] == 9 and inst.python is not None:
            return Path(inst.python)
    pytest.skip("no KiCad 9 bundled Python")


def test_live_imports_and_resolves_under_kicad9_python(kicad9_python, tmp_path):
    got = _run_child(str(kicad9_python), "import kicad_quiet\n", tmp_path)
    assert got["py"] == [3, 11]
