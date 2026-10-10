"""tline.geometry: reference resolution and line classification (ADR D7.6 steps 1-6, D7-07)."""
from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any

import pytest

import stackup_model as sm
import tline
from tline import geometry as geo
from tline.types import LineGeometry, MaskModel, Ply

JLC_MASK = MaskModel(er=3.8, over_trace=0.0152, over_substrate=0.0305, between=0.0305)
RAIL_NETS = {"+3V3": [], "GND": []}


def _rs(design: Mapping[str, Any]) -> sm.ResolvedStackup:
    rs, issues = sm.resolve(design)
    assert rs is not None, [str(i) for i in issues]
    return rs


def _preset(name: str, **extra: Any) -> sm.ResolvedStackup:
    return _rs({"stackup": {"preset": f"jlcpcb:{name}", **extra}, "nets": RAIL_NETS})


def _layers(*entries: Any, **extra: Any) -> sm.ResolvedStackup:
    """A model stackup from ('S'|'G'|'P'|'M:<net>', ...) coppers and (thickness, dk) dielectrics."""
    layers = []
    for e in entries:
        if isinstance(e, tuple):
            layers.append({"type": "dielectric", "kind": "prepreg", "thickness": e[0],
                           "dk": e[1], "df": 0.02})
            continue
        role = {"S": ("signal", None), "G": ("plane", "GND"), "P": ("plane", "+3V3")}.get(e)
        role = role or ("mixed", e.split(":", 1)[1])
        layers.append({"type": "copper", "oz": 1, "role": role[0], "net": role[1]})
    return _rs({"stackup": {"layers": layers, "fab": None, **extra}, "nets": RAIL_NETS})


def _ids(setup: geo.LineSetup) -> list[str]:
    return [i.id for i in setup.issues]


# ---------------------------------------------------------------- outer layers
def test_outer_microstrip_on_the_default_stackup() -> None:
    setup = geo.resolve_line(_rs({}), "F.Cu", {"impedance": 50})
    g = setup.geom
    assert (g.kind, g.pair, g.t, g.width) == ("microstrip", "single", 0.035, 0.0)
    assert [(p.thickness, p.er) for p in g.below] == [(0.2104, 4.4)] and g.above == ()
    assert g.below[0].df > 0 and isinstance(g.below[0], Ply)
    assert g.mask == JLC_MASK and g.layers == ("F.Cu",)
    assert (setup.ref_below, setup.ref_above, setup.ref_nets) == (("In1.Cu",), (), ("GND",))
    assert setup.ok and setup.issues == () and setup.line == "microstrip"


def test_bottom_layer_plies_are_ordered_outward_and_a_rail_reference_warns() -> None:
    setup = geo.resolve_line(_preset("JLC06161H-2116"), "B.Cu", {"impedance": 50})
    assert [(p.thickness, p.er) for p in setup.geom.below] == [(0.127, 4.16), (0.0964, 4.31)]
    assert setup.ref_above == ("In4.Cu",) and setup.ref_below == ()
    assert setup.ref_nets == ("+3V3",)
    assert _ids(setup) == ["ST-W10"] and setup.ok
    assert "+3V3" in setup.issues[0].message and setup.issues[0].severity == "warning"


def test_legacy_shorthand_is_one_unmasked_ply() -> None:
    g = geo.resolve_line(_rs({"stackup": {"h": 0.2, "er": 4.2}}), "F.Cu", {}).geom
    assert [(p.thickness, p.er) for p in g.below] == [(0.2, 4.2)]
    assert g.mask is None and g.t == 0.035


def test_mask_off_gives_no_mask_model() -> None:
    rs = _rs({"stackup": {"preset": "jlcpcb:JLC04161H-3313", "mask": None}})
    assert geo.resolve_line(rs, "F.Cu", {}).geom.mask is None


def test_two_layer_board_microstrip_over_the_mixed_gnd_and_no_reference_on_b() -> None:
    rs = _rs({"copper_layers": 2})
    top = geo.resolve_line(rs, "F.Cu", {"impedance": 50})
    assert top.geom.kind == "microstrip" and top.ref_below == ("B.Cu",)
    assert [(p.thickness, p.er) for p in top.geom.below] == [(1.51, 4.5)]
    bottom = geo.resolve_line(rs, "B.Cu", {"impedance": 50})
    assert bottom.geom is None and _ids(bottom) == ["ST-E9"] and not bottom.ok


def test_single_sided_lines_have_no_reference() -> None:
    setup = geo.resolve_line(_rs({"copper_layers": 1}), "F.Cu", {"impedance": 50})
    assert setup.geom is None and _ids(setup) == ["ST-E9"]
    assert "no reference" in setup.issues[0].message


# ---------------------------------------------------------------- inner layers
def test_symmetric_stripline_between_two_planes() -> None:
    rs = _layers("S", (0.2, 4.4), "G", (0.2, 4.4), "S", (0.2, 4.4), "G", (0.2, 4.4), "S",
                 (0.2, 4.4), "S")
    setup = geo.resolve_line(rs, "In2.Cu", {"impedance": 50})
    g = setup.geom
    assert (g.kind, g.mask) == ("stripline", None)
    assert [p.thickness for p in g.below] == [0.2] and [p.thickness for p in g.above] == [0.2]
    assert (setup.ref_above, setup.ref_below) == (("In1.Cu",), ("In3.Cu",))
    assert setup.issues == ()


def test_jlc06161h_2116_in2_is_a_dual_stripline_with_a_rail_reference() -> None:
    setup = geo.resolve_line(_preset("JLC06161H-2116"), "In2.Cu", {"impedance": 50})
    g = setup.geom
    assert g.kind == "dual_stripline" and g.t == 0.0152
    assert [(p.thickness, p.er) for p in g.above] == [(0.3, 4.41)]
    assert [(p.thickness, p.er) for p in g.below] == [(0.2084, 4.4), (0.2084, 4.4),
                                                       (0.0152, 4.4), (0.3, 4.41)]
    assert g.dielectric_copper == (("In3.Cu", 4.4),)
    assert setup.dielectric_layers == ("In3.Cu",)
    assert (setup.ref_above, setup.ref_below, setup.ref_nets) == (
        ("In1.Cu",), ("In4.Cu",), ("GND", "+3V3"))
    assert _ids(setup) == ["ST-W10"]
    assert tline.evaluate(LineGeometry(**{**_fields(g), "width": 0.12})).accuracy_class == "C"


def _fields(g: LineGeometry) -> dict[str, Any]:
    return {f: getattr(g, f) for f in g.__dataclass_fields__}


def test_rf_net_cannot_pass_a_routed_layer() -> None:
    setup = geo.resolve_line(_preset("JLC06161H-2116"), "In2.Cu",
                             {"impedance": 50, "freq_hz": 9.15e8})
    assert setup.geom is None and "ST-E9" in _ids(setup)
    assert "In3.Cu" in next(i.message for i in setup.issues if i.id == "ST-E9")


def test_rf_void_layers_turn_the_layer_into_dielectric() -> None:
    spec = {"impedance": 50, "freq_hz": 9.15e8, "line": "stripline",
            "void_layers": {"In2.Cu": ["In3.Cu"]}, "ref_nets": ["GND", "+3V3"]}
    setup = geo.resolve_line(_preset("JLC06161H-2116"), "In2.Cu", spec)
    assert setup.geom.kind == "stripline" and setup.void_layers == ("In3.Cu",)
    assert setup.geom.dielectric_copper == () and len(setup.geom.below) == 4
    assert _ids(setup) == ["ST-W10"]


def test_rf_net_on_a_power_plane_needs_it_in_ref_nets() -> None:
    rs = _rs({"nets": RAIL_NETS})                        # 4L default: In2 = +3V3 plane
    bad = geo.resolve_line(rs, "B.Cu", {"impedance": 50, "freq_hz": 9.15e8, "line": "microstrip"})
    assert "ST-E10" in _ids(bad) and not bad.ok
    ok = geo.resolve_line(rs, "B.Cu", {"impedance": 50, "freq_hz": 9.15e8, "line": "microstrip",
                                       "ref_nets": ["GND", "+3V3"]})
    assert _ids(ok) == ["ST-W10"] and ok.ok


def test_embedded_microstrip_warns_about_the_routed_layer_above() -> None:
    rs = _layers("S", (0.1, 4.2), "S", (0.3, 4.4), "G", (1.0, 4.5), "S")
    setup = geo.resolve_line(rs, "In1.Cu", {"impedance": 50})
    g = setup.geom
    assert g.kind == "embedded_microstrip"
    assert [p.thickness for p in g.below] == [0.3] and [p.thickness for p in g.above] == [0.1]
    assert setup.ref_below == ("In2.Cu",) and setup.ok
    assert any(w.startswith("In1.Cu: ") and "F.Cu" in w and "routed" in w
               for w in setup.warnings)
    quiet = geo.resolve_line(_rs({}), "F.Cu", {"impedance": 50})
    assert quiet.geom.kind == "microstrip" and quiet.warnings == ()


def test_plane_and_unknown_layers_are_not_line_layers() -> None:
    rs = _rs({})
    plane = geo.resolve_line(rs, "In1.Cu", {"impedance": 50})
    assert _ids(plane) == ["ST-E9"] and "plane" in plane.issues[0].message
    missing = geo.resolve_line(rs, "In7.Cu", {"impedance": 50})
    assert _ids(missing) == ["ST-E9"] and "In7.Cu" in missing.issues[0].message


def test_mixed_layer_reference_and_ref_layers_override() -> None:
    rs = _layers("S", (0.1, 4.2), "M:+3V3", (1.2, 4.5), "G", (0.1, 4.2), "S")
    rf = {"impedance": 50, "freq_hz": 9.15e8, "line": "microstrip"}
    blocked = geo.resolve_line(rs, "F.Cu", rf)
    assert "ST-E9" in _ids(blocked)                     # mixed +3V3 is routed, RF cannot pass
    plain = geo.resolve_line(rs, "F.Cu", {"impedance": 50})
    assert plain.ref_below == ("In1.Cu",) and _ids(plain) == ["ST-W10"]
    forced = geo.resolve_line(rs, "F.Cu", {**rf, "ref_layers": {"F.Cu": ["In1.Cu"]},
                                           "ref_nets": ["GND", "+3V3"]})
    assert forced.ref_below == ("In1.Cu",) and forced.ok


def test_ref_layers_override_naming_a_signal_layer_is_an_error() -> None:
    rs = _preset("JLC06161H-2116")
    setup = geo.resolve_line(rs, "In2.Cu", {"impedance": 50, "ref_layers": {"In2.Cu": ["In3.Cu"]}})
    assert "ST-E9" in _ids(setup) and not setup.ok


# ---------------------------------------------------------------- line requests
@pytest.mark.parametrize("value,expected", [(9.15e8, 9.15e8), (1, 1.0), ("x", None),
                                            ("9e8", None), (True, None), (0, None),
                                            (-1e9, None), (float("nan"), None),
                                            (float("inf"), None), (None, None)])
def test_rf_freq_counts_only_a_finite_positive_number(value: Any, expected: Any) -> None:
    assert geo.rf_freq({"freq_hz": value}) == expected
    assert geo.rf_freq({}) is None


def test_a_non_numeric_frequency_does_not_make_an_rf_line() -> None:
    setup = geo.resolve_line(_rs({}), "F.Cu", {"impedance": 50, "freq_hz": "x"})
    assert setup.geom.kind == "microstrip" and not setup.rf and not setup.coplanar


def test_auto_line_with_frequency_on_an_outer_layer_is_cpwg() -> None:
    setup = geo.resolve_line(_rs({}), "F.Cu", {"impedance": 50, "freq_hz": 9.15e8})
    assert setup.geom.kind == "cpwg" and setup.coplanar and setup.geom.ground_gap is None
    explicit = geo.resolve_line(_rs({}), "F.Cu", {"impedance": 50, "line": "cpwg",
                                                  "ground_gap": 0.2})
    assert explicit.geom.ground_gap == 0.2 and explicit.geom.mask == JLC_MASK


def test_cpwg_on_an_inner_layer_adds_same_layer_grounds() -> None:
    setup = geo.resolve_line(_preset("JLC06161H-2116"), "In2.Cu",
                             {"impedance": 50, "line": "cpwg", "ground_gap": 0.2})
    assert setup.geom.kind == "dual_stripline" and setup.geom.ground_gap == 0.2
    assert setup.coplanar and any("same-layer" in w for w in setup.warnings)


def test_ground_gap_on_a_non_coplanar_line_is_ignored_with_a_warning() -> None:
    setup = geo.resolve_line(_rs({}), "F.Cu", {"impedance": 50, "ground_gap": 0.2})
    assert setup.geom.ground_gap is None and any("ground_gap" in w for w in setup.warnings)
    assert setup.ok and setup.issues == ()


@pytest.mark.parametrize("layer,line", [("F.Cu", "stripline"), ("In2.Cu", "microstrip"),
                                        ("In2.Cu", "embedded_microstrip"), ("In2.Cu", "cpw"),
                                        ("F.Cu", "coax")])
def test_line_request_contradicting_the_stackup_is_an_error(layer: str, line: str) -> None:
    setup = geo.resolve_line(_preset("JLC06161H-2116"), layer, {"impedance": 50, "line": line})
    assert setup.geom is None and "ST-E9" in _ids(setup)


def test_stripline_request_on_a_dual_stripline_layer_is_honoured_as_dual() -> None:
    setup = geo.resolve_line(_preset("JLC06161H-2116"), "In2.Cu",
                             {"impedance": 50, "line": "stripline"})
    assert setup.geom.kind == "dual_stripline" and any("dual" in w for w in setup.warnings)


def test_cpw_without_a_back_plane_on_an_outer_layer() -> None:
    rs = _layers("S", (0.8, 4.4), "S")
    setup = geo.resolve_line(rs, "F.Cu", {"impedance": 50, "line": "cpw", "ground_gap": 0.2})
    assert setup.geom.kind == "cpw" and [p.thickness for p in setup.geom.below] == [0.8]
    assert setup.ok and setup.ref_below == () and setup.warnings == ()
    over_plane = geo.resolve_line(_rs({}), "F.Cu", {"impedance": 50, "line": "cpw"})
    assert any(w.startswith("F.Cu: cpw") and "cpwg" in w for w in over_plane.warnings)


# ---------------------------------------------------------------- pairs
def test_edge_pair_takes_the_netlist_gap_and_an_explicit_width() -> None:
    g = geo.resolve_line(_rs({}), "F.Cu", {"diff": 90, "gap": 0.15, "width": 0.25}).geom
    assert (g.pair, g.pair_gap, g.width) == ("edge", 0.15, 0.25)
    assert geo.resolve_line(_rs({}), "F.Cu", {"diff": 90}).geom.pair_gap is None


def test_broadside_pair_on_adjacent_inner_layers() -> None:
    spec = {"diff": 100, "pair": "broadside", "layers": ["In2.Cu", "In3.Cu"]}
    rs = _preset("JLC06161H-2116")
    upper = geo.resolve_line(rs, "In2.Cu", spec)
    g = upper.geom
    assert (g.kind, g.pair, g.layers) == ("stripline", "broadside", ("In2.Cu", "In3.Cu"))
    assert g.pair_gap == pytest.approx(0.4168)
    assert [p.thickness for p in g.above] == [0.3]
    assert [p.thickness for p in g.below] == [0.2084, 0.2084, 0.3]
    assert (upper.ref_above, upper.ref_below) == (("In1.Cu",), ("In4.Cu",))
    assert geo.resolve_line(rs, "In3.Cu", spec).geom == g
    assert tline.evaluate(LineGeometry(**{**_fields(g), "width": 0.2})).z_diff > 0


def test_broadside_pair_needs_two_adjacent_layers() -> None:
    spec = {"diff": 100, "pair": "broadside", "layers": ["In2.Cu", "In4.Cu"]}
    setup = geo.resolve_line(_preset("JLC06161H-2116"), "In2.Cu", spec)
    assert setup.geom is None and "ST-E9" in _ids(setup)


# ---------------------------------------------------------------- net level helpers
def test_eligible_layers_per_net_kind() -> None:
    assert geo.eligible_layers(_rs({}), {"impedance": 50}) == ("F.Cu", "B.Cu")
    rs6 = _preset("JLC06161H-2116")
    assert geo.eligible_layers(rs6, {"impedance": 50}) == ("F.Cu", "In2.Cu", "In3.Cu", "B.Cu")
    # RF: In2/In3 would need the dual-stripline assumption (ST-E9), B.Cu references +3V3 (ST-E10)
    assert geo.eligible_layers(rs6, {"impedance": 50, "freq_hz": 1e9}) == ("F.Cu",)
    assert geo.eligible_layers(_rs({"copper_layers": 1}), {"impedance": 50}) == ()


def test_transition_issues_flag_layers_with_different_reference_nets() -> None:
    rs = _rs({"nets": RAIL_NETS})
    setups = [geo.resolve_line(rs, layer, {"impedance": 50}) for layer in ("F.Cu", "B.Cu")]
    issues = geo.transition_issues(setups, path="$.net_specs.RF")
    assert [i.id for i in issues] == ["ST-W11"] and issues[0].path == "$.net_specs.RF"
    assert "GND" in issues[0].message and "+3V3" in issues[0].message
    assert geo.transition_issues(setups[:1]) == ()


def test_line_geometry_wrapper_and_issue_paths() -> None:
    geom, issues = geo.line_geometry(_rs({"copper_layers": 1}), "F.Cu", {"impedance": 50},
                                     path="$.net_specs.RF")
    assert geom is None and issues[0].path == "$.net_specs.RF"
    geom, issues = geo.line_geometry(_rs({}), "F.Cu", {"impedance": 50})
    assert isinstance(geom, LineGeometry) and issues == ()


def test_the_artifact_dict_form_gives_the_same_geometry() -> None:
    rs = _preset("JLC06161H-2116")
    for layer in ("F.Cu", "In2.Cu", "B.Cu"):
        assert (geo.resolve_line(rs.to_dict(), layer, {"impedance": 50})
                == geo.resolve_line(rs, layer, {"impedance": 50}))


def test_inputs_are_not_mutated_and_results_are_frozen() -> None:
    spec = {"impedance": 50, "void_layers": {"In2.Cu": ["In3.Cu"]}, "layers": ["In2.Cu"]}
    before = copy.deepcopy(spec)
    setup = geo.resolve_line(_preset("JLC06161H-2116"), "In2.Cu", spec)
    assert spec == before
    with pytest.raises(AttributeError):
        setup.layer = "F.Cu"                                     # type: ignore[misc]


@pytest.mark.parametrize("net,ground", [("GND", True), ("AGND", True), ("GND_RF", True),
                                        ("VSS", True), ("+3V3", False), ("VBUS", False),
                                        (None, False)])
def test_is_ground(net: str | None, ground: bool) -> None:
    assert geo.is_ground(net) is ground


def test_ref_dict_is_json_ready() -> None:
    setup = geo.resolve_line(_preset("JLC06161H-2116"), "In2.Cu", {"impedance": 50})
    assert setup.ref_dict() == {"below": ["In4.Cu"], "above": ["In1.Cu"], "nets": ["GND", "+3V3"]}
