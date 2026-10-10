"""netclass_writer — IPC net classes into a .kicad_pro."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

import impedance
import ipc_rules
import netclass_writer as nw
import physics_resolve

DEFAULT_CLASS = {"name": "Default", "track_width": 0.2, "clearance": 0.2,
                 "via_diameter": 0.6, "via_drill": 0.3, "diff_pair_width": 0.2}
HAND_CLASS = {"name": "HV_CUSTOM", "track_width": 0.5, "clearance": 1.0}


def _write_project(path: Path, classes=None, patterns=None, rules=None) -> Path:
    proj = {"board": {"design_settings": {"rules": rules or {"min_track_width": 0.1,
                                                              "min_clearance": 0.1}}},
            "net_settings": {"classes": classes if classes is not None else [dict(DEFAULT_CLASS)],
                             "netclass_patterns": patterns or []}}
    path.write_text(json.dumps(proj), encoding="utf-8")
    return path


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# --- _class_name / _via_for -----------------------------------------------------

@pytest.mark.parametrize("net,name", [
    ("+3V3", "NC_P3V3"), ("VBUS_5V", "NC_VBUS_5V"), ("-12V", "NC_N12V"),
    ("A/B C", "NC_A_B_C"), ("GND", "NC_GND"),
])
def test_class_name_is_kicad_safe(net, name):
    assert nw._class_name(net) == name


def test_via_for_floor_when_narrow_and_unloaded():
    assert nw._via_for(0.2, 0.0, 0.6, 0.3) == (0.6, 0.3, ipc_rules.via_ampacity_a(0.3), 1)


def test_via_for_grows_geometrically_with_wide_trace():
    via_d, drill, amp, needed = nw._via_for(0.5, 1.0, 0.6, 0.3)
    assert (via_d, drill) == (0.8, 0.5)            # width + 0.30, drill = d - 0.30
    assert amp == ipc_rules.via_ampacity_a(0.5) and amp > 1.0
    assert needed == 1


def test_via_for_grows_thermally_until_08mm_then_reports_cluster():
    via_d, drill, amp, needed = nw._via_for(0.25, 1.4, 0.6, 0.3)
    assert (via_d, drill) == (0.9, 0.6)                  # grew 0.1 mm steps to carry 1.4 A
    assert via_d == pytest.approx(drill + 0.30)
    assert amp == ipc_rules.via_ampacity_a(0.6) >= 1.4 and needed == 1
    via_d, drill, amp, needed = nw._via_for(1.375, 3.0, 0.6, 0.3)
    assert (via_d, drill) == (1.2, 0.9)                  # capped at 1.2 mm diameter
    assert needed == ipc_rules.vias_for_current(3.0, 0.9) == 2


# --- build_classes ---------------------------------------------------------------

def test_build_classes_buck_every_power_net_gets_a_class(buck_design):
    built = nw.build_classes(buck_design, cls=3)
    names = [c["name"] for c in built["classes"]]
    assert names == ["NC_VIN_RAW", "NC_VIN", "NC_SW", "NC_V5", "NC_GND"]
    assert [c["priority"] for c in built["classes"]] == [0, 1, 2, 3, 4]
    assert built["patterns"] == [{"netclass": n, "pattern": n[3:]} for n in names]
    assert built["default"] == {"track_width": 0.2, "clearance": 0.2}
    assert built["class_name"] == "Class 3 (high reliability)"
    sw = next(c for c in built["classes"] if c["name"] == "NC_SW")
    assert sw["track_width"] == pytest.approx(1.375, abs=0.001)
    assert sw["clearance"] == 0.2
    assert (sw["via_diameter"], sw["via_drill"]) == (1.2, 0.9)
    assert sw["diff_pair_width"] == sw["track_width"]
    assert "diff_pair_gap" not in sw
    assert built["summary"]["SW"]["via"]["vias_needed"] == 2
    assert any(w.startswith("SW: 3.0 A exceeds one 0.9 mm via barrel") for w in built["warnings"])


def test_build_classes_plane_nets_are_exempt_from_width(buck_design):
    built = nw.build_classes(buck_design, cls=3, plane_nets=("GND",))
    assert "NC_GND" not in [c["name"] for c in built["classes"]]
    assert built["summary"]["GND"] == {"plane": True, "voltage": 0.0, "current": 3.0}
    assert not any(w.startswith("GND:") for w in built["warnings"])
    assert not any(p["pattern"] == "GND" for p in built["patterns"])


def test_build_classes_floor_nets_stay_on_default():
    design = {"nets": {"SDA": [], "SCL": []}, "net_specs": {"SDA": {"voltage": 3.3}}}
    built = nw.build_classes(design, cls=2)
    assert built["classes"] == [] and built["patterns"] == []
    assert set(built["summary"]) == {"SDA", "SCL"}
    assert built["summary"]["SDA"] == {"track_width": 0.15, "clearance": 0.13,
                                       "voltage": 3.3, "current": 0.0}


def test_build_classes_template_supplies_via_floor_and_extra_keys():
    design = {"nets": {"HV": []}, "net_specs": {"HV": {"voltage": 250}}}
    tmpl = {"via_diameter": 0.8, "via_drill": 0.4, "microvia_diameter": 0.2}
    c = nw.build_classes(design, cls=1, template=tmpl)["classes"][0]
    assert c["clearance"] == 1.25 and c["track_width"] == 0.15
    assert (c["via_diameter"], c["via_drill"]) == (0.8, 0.4)
    assert c["microvia_diameter"] == 0.2                  # template keys carried


def test_build_classes_impedance_net_is_forced_and_warns_below_floor():
    design = {"nets": {"RF": [], "USB": []},
              "net_specs": {"RF": {"impedance": 90}, "USB": {"diff": 90}}}
    built = nw.build_classes(design, cls=3)
    names = {c["name"]: c for c in built["classes"]}
    assert set(names) == {"NC_RF", "NC_USB"}
    rf_w = impedance.microstrip_width(90, 0.146, 4.20)
    assert names["NC_RF"]["track_width"] == round(rf_w, 3) < 0.2
    assert built["summary"]["RF"]["impedance"]["mode"] == "single"
    assert any(w.startswith("RF: 90.0-ohm width") and "below the Class floor 0.2mm" in w
               for w in built["warnings"])
    dp = impedance.diff_pair(90, 0.1616, 4.20)
    usb = names["NC_USB"]
    assert usb["diff_pair_width"] == round(dp["width"], 3)
    assert usb["diff_pair_gap"] == round(dp["gap"], 3)
    assert usb["track_width"] == usb["diff_pair_width"]


def test_build_classes_impedance_overrides_current_width():
    design = {"nets": {"RF": []}, "net_specs": {"RF": {"impedance": 50, "current": 3.0}}}
    c = nw.build_classes(design, cls=3)["classes"][0]
    assert c["track_width"] == round(impedance.microstrip_width(50, 0.173, 4.20), 3)


def test_build_classes_copper_and_temp_rise_flow_through(buck_design):
    thin = nw.build_classes(buck_design, cls=3)["summary"]["V5"]["track_width"]
    thick = nw.build_classes(buck_design, cls=3, copper_oz=2.0)["summary"]["V5"]["track_width"]
    hot = nw.build_classes(buck_design, cls=3, temp_rise_c=30)["summary"]["V5"]["track_width"]
    assert thick < thin and hot < thin


# --- apply_to_project ------------------------------------------------------------

def test_apply_to_project_writes_classes_and_patterns(tmp_path, buck_design):
    pro = _write_project(tmp_path / "buck.kicad_pro")
    rep = nw.apply_to_project(str(pro), buck_design, cls=3)
    assert rep["ok"] is True and rep["project"] == str(pro)
    assert rep["classes_written"] == ["NC_VIN_RAW", "NC_VIN", "NC_SW", "NC_V5", "NC_GND"]
    assert rep["class_profile"] == "Class 3 (high reliability)"
    assert rep["default"] == {"track_width": 0.2, "clearance": 0.2}
    proj = _read(pro)
    ns = proj["net_settings"]
    assert [c["name"] for c in ns["classes"]] == ["Default"] + rep["classes_written"]
    assert ns["netclass_patterns"] == [{"netclass": n, "pattern": n[3:]}
                                       for n in rep["classes_written"]]
    sw = next(c for c in ns["classes"] if c["name"] == "NC_SW")
    assert sw["via_drill"] == 0.9 and sw["diff_pair_width"] == sw["track_width"]
    assert "SW" in rep["summary"] and rep["warnings"]


def test_apply_to_project_default_is_raised_to_floor_never_lowered(tmp_path, buck_design):
    low = _write_project(tmp_path / "low.kicad_pro",
                         classes=[{**DEFAULT_CLASS, "track_width": 0.1, "clearance": 0.1}])
    nw.apply_to_project(str(low), buck_design, cls=3)
    dflt = _read(low)["net_settings"]["classes"][0]
    assert (dflt["track_width"], dflt["clearance"]) == (0.2, 0.2)
    high = _write_project(tmp_path / "high.kicad_pro",
                          classes=[{**DEFAULT_CLASS, "track_width": 0.3, "clearance": 0.25}])
    nw.apply_to_project(str(high), buck_design, cls=3)
    dflt = _read(high)["net_settings"]["classes"][0]
    assert (dflt["track_width"], dflt["clearance"]) == (0.3, 0.25)


def test_apply_to_project_rule_floor_lowered_to_class_never_raised(tmp_path, buck_design):
    pro = _write_project(tmp_path / "r.kicad_pro",
                         rules={"min_track_width": 0.5, "min_clearance": 0.05,
                                "min_via_diameter": 0.4})
    nw.apply_to_project(str(pro), buck_design, cls=3)
    rules = _read(pro)["board"]["design_settings"]["rules"]
    assert rules["min_track_width"] == 0.2          # lowered to the class floor
    assert rules["min_clearance"] == 0.05           # fab floor kept
    assert rules["min_via_diameter"] == 0.4         # untouched


def test_apply_to_project_is_idempotent_and_keeps_hand_classes(tmp_path, buck_design):
    pro = _write_project(tmp_path / "i.kicad_pro",
                         classes=[dict(DEFAULT_CLASS), dict(HAND_CLASS),
                                  {"name": "NC_STALE", "track_width": 9.9}],
                         patterns=[{"netclass": "HV_CUSTOM", "pattern": "HV*"},
                                   {"netclass": "NC_STALE", "pattern": "OLD"}])
    nw.apply_to_project(str(pro), buck_design, cls=3)
    nw.apply_to_project(str(pro), buck_design, cls=3)
    ns = _read(pro)["net_settings"]
    names = [c["name"] for c in ns["classes"]]
    assert names == ["Default", "HV_CUSTOM", "NC_VIN_RAW", "NC_VIN", "NC_SW", "NC_V5", "NC_GND"]
    assert next(c for c in ns["classes"] if c["name"] == "HV_CUSTOM") == HAND_CLASS
    assert ns["netclass_patterns"][0] == {"netclass": "HV_CUSTOM", "pattern": "HV*"}
    assert len(ns["netclass_patterns"]) == 6
    assert not any(p["netclass"] == "NC_STALE" for p in ns["netclass_patterns"])


def test_apply_to_project_plane_nets_exempt(tmp_path, buck_design):
    pro = _write_project(tmp_path / "p.kicad_pro")
    rep = nw.apply_to_project(str(pro), buck_design, cls=3, plane_nets=("GND", "V5"))
    assert rep["classes_written"] == ["NC_VIN_RAW", "NC_VIN", "NC_SW"]
    assert rep["summary"]["GND"]["plane"] is True and rep["summary"]["V5"]["plane"] is True
    assert not any(p["pattern"] in ("GND", "V5")
                   for p in _read(pro)["net_settings"]["netclass_patterns"])


def test_apply_to_project_minimal_project_file(tmp_path, buck_design):
    pro = tmp_path / "bare.kicad_pro"
    pro.write_text("{}", encoding="utf-8")
    rep = nw.apply_to_project(str(pro), buck_design, cls=2)
    assert rep["ok"] is True
    proj = _read(pro)
    assert proj["net_settings"]["classes"][0]["name"] == "NC_VIN_RAW"   # no Default to bump
    assert proj["board"]["design_settings"]["rules"] == {"min_track_width": 0.15,
                                                          "min_clearance": 0.13}


def test_apply_to_project_missing_file_returns_not_ok(tmp_path, buck_design):
    rep = nw.apply_to_project(str(tmp_path / "missing.kicad_pro"), buck_design)
    assert rep["ok"] is False
    assert rep["reason"].startswith("project file not found:")
    assert not (tmp_path / "missing.kicad_pro").exists()


def test_apply_to_project_hat_writes_class_per_specced_net(tmp_path, hat_design):
    pro = _write_project(tmp_path / "hat.kicad_pro")
    rep = nw.apply_to_project(str(pro), hat_design, cls=hat_design["class"],
                              plane_nets=("GND",))
    assert rep["ok"]
    written = set(rep["classes_written"])
    assert {"NC_VBUS_PD", "NC_SW1", "NC_SW2", "NC_5V_PI"} <= written
    assert "NC_GND" not in written and "NC_3V3" not in written   # 0.1 A sits on the floor


# --- D7-21: classes from the physical-design artifact (ADR D7.4 / D7.6) ----------

STACKUPS = Path(__file__).parent / "fixtures" / "stackups"
CPWG_915 = {"impedance": 50, "freq_hz": 9.15e8}


def _stackup_design(name: str, net_specs: dict, nets: tuple[str, ...] = ()) -> dict:
    """A tests/fixtures/stackups design with `net_specs` (and extra empty nets)."""
    design = json.loads((STACKUPS / f"{name}.json").read_text(encoding="utf-8"))["design"]
    return {**design, "nets": {**design["nets"], **{n: [] for n in nets}},
            "net_specs": net_specs}


def _artifact(design: dict, tmp_path: Path) -> dict:
    """The real physics_resolve artifact (stdlib tier, deterministic)."""
    return physics_resolve.resolve_design(design, tmp_path / "netlist.json", fd="off")


def _line(artifact: dict, net: str) -> dict:
    rec = artifact["lines"][net]
    return rec["layers"][rec["primary_layer"]]


def _classes(built: dict) -> dict:
    return {c["name"]: c for c in built["classes"]}


def _patched(artifact: dict, net: str, **layer_keys) -> dict:
    """A deep copy of `artifact` with the primary-layer record of `net` changed."""
    out = copy.deepcopy(artifact)
    _line(out, net).update(layer_keys)
    return out


def test_plane_nets_from_artifact_follow_plane_and_mixed_roles(tmp_path, load_design):
    two = _artifact(_stackup_design("d722_2l_mixed", {}), tmp_path)
    six = _artifact(_stackup_design("d722_6l_2116", {}), tmp_path)
    mixed = _artifact(_stackup_design("d722_jlc3313_in2_mixed", {}), tmp_path)
    legacy = _artifact(load_design("rf_lna_gainblock"), tmp_path)
    assert nw.plane_nets_from_artifact(two) == ("GND",)           # B.Cu mixed GND
    assert nw.plane_nets_from_artifact(six) == ("GND", "+3V3")
    assert nw.plane_nets_from_artifact(mixed) == ("GND", "+3V3")  # In2.Cu mixed +3V3
    assert nw.plane_nets_from_artifact(legacy) == ("GND", "VBIAS")
    assert nw.plane_nets_from_artifact({"stackup": None}) == ()
    assert nw.plane_nets_from_artifact(None) == ()


def test_roles_on_2l_b_mixed_exempt_the_pour_net(tmp_path):
    design = _stackup_design("d722_2l_mixed", {"GND": {"current": 3.0},
                                               "+3V3": {"current": 1.0}})
    built = nw.build_classes(design, cls=3, artifact=_artifact(design, tmp_path))
    assert built["source"] == "artifact" and built["plane_nets"] == ["GND"]
    assert list(_classes(built)) == ["NC_P3V3"]
    assert built["summary"]["GND"] == {"plane": True, "voltage": 3.3, "current": 3.0}
    assert not any(w.startswith("GND:") for w in built["warnings"])
    today = nw.build_classes(design, cls=3)                       # no artifact: no roles
    assert today["source"] == "design" and today["plane_nets"] == []
    assert list(_classes(today)) == ["NC_GND", "NC_P3V3"]


def test_explicit_plane_nets_add_to_the_roles(tmp_path):
    design = _stackup_design("d722_2l_mixed", {"+3V3": {"current": 1.0}})
    built = nw.build_classes(design, cls=3, artifact=_artifact(design, tmp_path),
                             plane_nets=("+3V3",))               # e.g. a prerouted net
    assert built["classes"] == [] and built["plane_nets"] == ["GND", "+3V3"]


def test_cpwg_class_is_the_primary_layer_width_and_the_ground_gap(tmp_path):
    design = _stackup_design("d722_2l_mixed", {"SIG": CPWG_915})
    art = _artifact(design, tmp_path)
    line = _line(art, "SIG")
    assert art["lines"]["SIG"]["line"] == "cpwg" and line["ground_gap"] == 0.15
    built = nw.build_classes(design, cls=3, artifact=art)
    sig = _classes(built)["NC_SIG"]
    assert sig["track_width"] == line["width"] == pytest.approx(0.63)
    assert sig["clearance"] == 0.15                     # ground_gap, below the 0.2 class floor
    assert sig["diff_pair_width"] == sig["track_width"] and "diff_pair_gap" not in sig
    imp = built["summary"]["SIG"]["impedance"]
    assert imp["line"] == "cpwg" and imp["layer"] == "F.Cu"
    assert imp["clearance_from"] == "ground_gap" and imp["status"] == "closed_form"
    assert built["rule_floor"]["min_clearance"] == 0.15
    assert built["warnings"] == []


def test_cpwg_reference_net_gets_the_gap_as_its_clearance(tmp_path):
    # KiCad 9.0.4 pours at max(track class, pour-net class) clearance (test_..._live below)
    design = _stackup_design("d722_2l_mixed", {"SIG": CPWG_915})
    built = nw.build_classes(design, cls=3, template=DEFAULT_CLASS,
                             artifact=_artifact(design, tmp_path))
    assert [c["name"] for c in built["classes"]] == ["NC_SIG", "NC_GND"]
    gnd = _classes(built)["NC_GND"]
    assert gnd == {**DEFAULT_CLASS, "name": "NC_GND", "track_width": 0.2, "clearance": 0.15,
                   "via_diameter": 0.6, "via_drill": 0.3, "diff_pair_width": 0.2, "priority": 1}
    assert built["patterns"][-1] == {"netclass": "NC_GND", "pattern": "GND"}
    assert built["summary"]["GND"] == {"plane": True, "voltage": 3.3, "current": 0.0,
                                       "coplanar_clearance": 0.15}


def test_cpwg_reference_class_only_ever_lowers_a_clearance(tmp_path):
    wide = _stackup_design("d722_2l_mixed", {"SIG": {**CPWG_915, "ground_gap": 0.25}})
    built = nw.build_classes(wide, cls=3, artifact=_artifact(wide, tmp_path))
    assert list(_classes(built)) == ["NC_SIG"]                      # 0.25 >= Default's 0.2
    # a reference net with its own (3 A) class keeps its width; only its clearance drops
    hot = _stackup_design("d722_2l_mixed", {"SIG": CPWG_915, "GND": {"current": 3.0}})
    art = copy.deepcopy(_artifact(hot, tmp_path))
    art["stackup"]["copper"][1]["role"] = "signal"                  # B.Cu no longer a GND pour
    gnd = _classes(nw.build_classes(hot, cls=3, artifact=art))["NC_GND"]
    assert gnd["track_width"] == pytest.approx(1.375, abs=0.001) and gnd["clearance"] == 0.15


def test_cpwg_reference_net_with_a_leading_underscore_is_never_classed(tmp_path):
    # build_classes leaves '_' nets out entirely (no class, no summary row): so does the
    # coplanar reference-class step, instead of failing on the missing summary row
    design = _stackup_design("d722_2l_mixed", {"SIG": CPWG_915}, nets=("_GND",))
    art = _artifact(design, tmp_path)
    ref = {**_line(art, "SIG")["ref"], "nets": ["_GND"]}
    built = nw.build_classes(design, cls=3, artifact=_patched(art, "SIG", ref=ref))
    assert list(_classes(built)) == ["NC_SIG"] and "_GND" not in built["summary"]
    assert _classes(built)["NC_SIG"]["clearance"] == 0.15


def test_microstrip_keeps_the_net_clearance_and_warns_about_the_pour_gap(tmp_path, load_design):
    design = load_design("rf_coupon_50r")
    art = _artifact(design, tmp_path)
    assert _line(art, "RF_IN")["pour_clearance"] == pytest.approx(0.415)
    built = nw.build_classes(design, cls=3, artifact=art)
    for net in ("RF_IN", "RF_MID", "RF_OUT"):
        c = _classes(built)["NC_" + net]
        assert c["track_width"] == _line(art, net)["width"] == pytest.approx(0.365)
        assert c["clearance"] == 0.2              # 0.415 would fail DRC on C1's 0.40 mm pads
        imp = built["summary"][net]["impedance"]
        assert imp["clearance_from"] == "floor" and imp["pour_clearance"] == pytest.approx(0.415)
    assert "NC_GND" not in _classes(built)        # no coplanar line: GND stays on Default
    assert built["warnings"][0] == (
        "RF_IN: microstrip pour clearance 0.415 mm on F.Cu is not the class clearance (0.2 mm): "
        "a KiCad class clearance also binds the pads of the parts on the line (an 0402 DC "
        "block's pads are 0.40 mm apart), so a GND pour may come within 0.2 mm of the line "
        "until the RF<->GND rule (ADR D7.8 R10) sets the gap")
    assert len(built["warnings"]) == 3
    near = nw.build_classes(design, cls=3, artifact=_patched(art, "RF_IN", pour_clearance=0.12))
    assert _classes(near)["NC_RF_IN"]["clearance"] == 0.2
    assert not any(w.startswith("RF_IN:") for w in near["warnings"])


def test_stripline_clearance_is_the_net_rule(tmp_path):
    design = _stackup_design("d722_6l_2116", {"SIG": {"impedance": 50, "layers": ["In2.Cu"]}})
    art = _artifact(design, tmp_path)
    assert _line(art, "SIG")["ground_gap"] is None and _line(art, "SIG")["pour_clearance"] is None
    c = _classes(nw.build_classes(design, cls=2, artifact=art))["NC_SIG"]
    assert (c["track_width"], c["clearance"]) == (_line(art, "SIG")["width"], 0.13)


def test_diff_pair_takes_the_pair_geometry_and_its_partner_the_same_class(tmp_path):
    design = json.loads((Path(__file__).parent / "fixtures" / "dru" / "jlc4l_usb.json")
                        .read_text(encoding="utf-8"))["design"]
    design = {**design, "nets": {**design["nets"], "USB_DP": [], "USB_DM": []}}
    art = _artifact(design, tmp_path)
    line = _line(art, "USB_DP")
    built = nw.build_classes(design, cls=3, artifact=art)
    for name in ("NC_USB_DP", "NC_USB_DM"):
        c = _classes(built)[name]
        assert c["track_width"] == c["diff_pair_width"] == line["width"] == 0.135
        assert c["diff_pair_gap"] == line["pair_gap"] == 0.15
        assert c["clearance"] == 0.2                     # pour clearance 0.18 < floor 0.2
    assert built["summary"]["USB_DM"]["impedance"]["partner_of"] == "USB_DP"
    assert any(w.startswith("USB_DP: 90.0-ohm width 0.135mm is below the Class floor 0.2mm")
               for w in built["warnings"])
    assert built["rule_floor"]["min_track_width"] == 0.135   # >= the jlcpcb 4L 0.09 mm


def test_ground_gap_below_the_fab_minimum_warns(tmp_path):
    design = _stackup_design("d722_2l_mixed", {"SIG": {**CPWG_915, "ground_gap": 0.08}})
    built = nw.build_classes(design, cls=3, artifact=_artifact(design, tmp_path))
    assert _classes(built)["NC_SIG"]["clearance"] == 0.08
    assert built["warnings"] == [
        "SIG: CPWG ground gap 0.08 mm on F.Cu is below the jlcpcb minimum clearance 0.1 mm "
        "(2 copper layers): the fab cannot etch it - widen net_specs.SIG.ground_gap or drop it "
        "so the solver picks one"]
    assert built["rule_floor"]["min_clearance"] == 0.1        # never below the fab minimum


def test_ground_gap_without_a_fab_uses_the_generic_minimum(tmp_path, load_design):
    design = load_design("rf_coupon_50r")            # legacy shorthand: no fab
    art = _patched(_artifact(design, tmp_path), "RF_IN", ground_gap=0.05, pour_clearance=None)
    art["lines"]["RF_IN"]["line"] = "cpwg"
    built = nw.build_classes(design, cls=3, artifact=art)
    assert any(w.startswith("RF_IN: CPWG ground gap 0.05 mm on F.Cu is below the generic "
                            "minimum clearance 0.1 mm") for w in built["warnings"])


def test_cpwg_gap_below_the_ipc_voltage_spacing_warns(tmp_path):
    # the class clearance stays the gap (the line's impedance needs it), but never silently
    design = _stackup_design("d722_2l_mixed", {"SIG": {**CPWG_915, "voltage": 100}})
    built = nw.build_classes(design, cls=3, artifact=_artifact(design, tmp_path))
    assert _classes(built)["NC_SIG"]["clearance"] == 0.15
    assert _classes(built)["NC_GND"]["clearance"] == 0.15
    assert built["warnings"] == [
        "SIG: CPWG ground gap 0.15 mm on F.Cu is below the IPC-2221 spacing 0.6 mm for 100 V "
        "(Table 6-1 B2): the class clearance stays at the gap so the line keeps its impedance - "
        "declare net_specs.SIG.ground_gap of at least 0.6 mm (the width is re-solved for it) or "
        "lower the voltage"]
    wide = _stackup_design("d722_2l_mixed", {"SIG": {**CPWG_915, "voltage": 100,
                                                     "ground_gap": 0.6}})
    built = nw.build_classes(wide, cls=3, artifact=_artifact(wide, tmp_path))
    assert _classes(built)["NC_SIG"]["clearance"] == 0.6 and built["warnings"] == []


@pytest.mark.parametrize("count", ["four", True, 0, -2, 2.5, float("nan"), float("inf")])
def test_malformed_copper_count_is_an_artifact_error(tmp_path, count):
    design = _stackup_design("d722_2l_mixed", {"SIG": CPWG_915})
    art = _artifact(design, tmp_path)
    bad = {**art, "stackup": {**art["stackup"], "copper_layers": count,
                              "kicad_copper_count": count}}
    pro = _write_project(tmp_path / "count.kicad_pro")
    before = pro.read_bytes()
    rep = nw.apply_to_project(str(pro), design, cls=3, artifact=bad)
    assert rep["ok"] is False and "copper layer count" in rep["reason"]
    assert pro.read_bytes() == before


def test_artifact_modules_load_only_on_the_artifact_path():
    # the no-artifact path (every board_build.apply_netclasses call today) must not need the
    # artifact-only modules physics_resolve (D7-13a) and dru_writer (D7-12a)
    import subprocess
    import sys
    tools = Path(__file__).resolve().parents[1] / "server" / "tools"
    code = ("import json, sys; sys.path.insert(0, sys.argv[1]); import netclass_writer as nw; "
            "nw.build_classes({'nets': {'RF': [], 'V': []}, 'net_specs': {'RF': {'impedance': 50},"
            " 'V': {'current': 2.0}}}, 3); "
            "print(json.dumps(sorted(m for m in ('physics_resolve', 'dru_writer') "
            "if m in sys.modules)))")
    out = subprocess.run([sys.executable, "-c", code, str(tools)], stdin=subprocess.DEVNULL,
                         capture_output=True, encoding="utf-8", errors="replace", timeout=120,
                         check=True)
    assert json.loads(out.stdout.strip().splitlines()[-1]) == []
    assert nw.ARTIFACT_SCHEMA == physics_resolve.SCHEMA


def test_artifact_two_amp_net_on_six_layers_warns_about_inner_copper(tmp_path):
    design = _stackup_design("d722_6l_2116", {"SIG": {"current": 2.0}})
    built = nw.build_classes(design, cls=3, artifact=_artifact(design, tmp_path))
    assert _classes(built)["NC_SIG"]["track_width"] == pytest.approx(0.786, abs=0.001)
    (warning,) = [w for w in built["warnings"] if "IPC-2221 internal" in w]
    assert warning.startswith("SIG: 2 A needs 0.786 mm on the outer 1 oz copper but 4.119 mm "
                              "on In2.Cu/In3.Cu (0.5 oz)")
    assert not any("IPC-2221 internal" in w for w in nw.build_classes(design, cls=3)["warnings"])


def test_artifact_outer_copper_weight_sizes_current_nets(tmp_path):
    heavy = _stackup_design("d722_2l_mixed", {"SIG": {"current": 3.0}})
    layers = heavy["stackup"]["layers"]
    heavy = {**heavy, "stackup": {**heavy["stackup"], "layers": [
        {**layers[0], "oz": 2}, layers[1], {**layers[2], "oz": 2}]}}
    art = _artifact(heavy, tmp_path)
    two_oz = _classes(nw.build_classes(heavy, cls=3, artifact=art))["NC_SIG"]["track_width"]
    assert two_oz == round(ipc_rules.trace_width_mm(3.0, 10.0, 2.0), 3)
    forced = nw.build_classes(heavy, cls=3, copper_oz=1.0, artifact=art)   # explicit wins
    assert _classes(forced)["NC_SIG"]["track_width"] == pytest.approx(1.375, abs=0.001)


def _error_lines(art: dict) -> dict:
    issue = {"id": "ST-E9", "severity": "error", "message": "no reference", "path": "$"}
    return {**art, "lines": {"SIG": {"status": "error", "primary_layer": None, "layers": {},
                                     "issues": [issue]}}}


@pytest.mark.parametrize("breakage,reason", [
    (lambda a: ["not", "an", "artifact"], "must be a kicad-eda/physical@1 artifact"),
    (lambda a: {**a, "schema": "kicad-eda/physical@0"}, "must be a kicad-eda/physical@1"),
    (lambda a: {**a, "stackup": None, "errors": ["ST-E1 at $.stackup: bad"]},
     "has no resolved stackup (ST-E1 at $.stackup: bad)"),
    (lambda a: {**a, "design_digest": "0" * 64}, "is stale"),
    (lambda a: {**a, "lines": {}}, "SIG: controlled impedance but the artifact has no line"),
    (_error_lines, "SIG: the artifact could not size the line (ST-E9 no reference)"),
])
def test_bad_artifact_fails_and_leaves_the_project_untouched(tmp_path, breakage, reason):
    design = _stackup_design("d722_2l_mixed", {"SIG": CPWG_915})
    bad = breakage(_artifact(design, tmp_path))
    pro = _write_project(tmp_path / "bad.kicad_pro")
    before = pro.read_bytes()
    rep = nw.apply_to_project(str(pro), design, cls=3, artifact=bad)
    assert rep["ok"] is False and reason in rep["reason"]
    assert pro.read_bytes() == before
    with pytest.raises(ValueError, match="artifact"):
        nw.build_classes(design, cls=3, artifact=bad)


def test_artifact_given_as_a_path_is_read(tmp_path):
    design = _stackup_design("d722_2l_mixed", {"SIG": CPWG_915})
    art = _artifact(design, tmp_path)
    path = tmp_path / "netlist.stackup.json"
    path.write_text(physics_resolve.artifact_text(art), encoding="utf-8")
    assert nw.build_classes(design, artifact=str(path)) == nw.build_classes(design, artifact=art)
    missing = nw.apply_to_project(str(_write_project(tmp_path / "m.kicad_pro")), design,
                                  artifact=str(tmp_path / "nope.stackup.json"))
    assert missing["ok"] is False and "cannot read the artifact" in missing["reason"]


def test_apply_with_artifact_lowers_the_rule_floor_and_reports(tmp_path):
    design = _stackup_design("d722_2l_mixed", {"SIG": CPWG_915, "GND": {"current": 2.0}})
    pro = _write_project(tmp_path / "a.kicad_pro", rules={"min_track_width": 0.2,
                                                           "min_clearance": 0.2})
    rep = nw.apply_to_project(str(pro), design, cls=3, artifact=_artifact(design, tmp_path))
    assert rep["ok"] is True and rep["source"] == "artifact" and rep["plane_nets"] == ["GND"]
    assert rep["classes_written"] == ["NC_SIG", "NC_GND"]       # GND: the coplanar ground
    rules = _read(pro)["board"]["design_settings"]["rules"]
    assert rules == {"min_track_width": 0.2, "min_clearance": 0.15}
    assert rep["rule_floor"] == rules


def test_apply_with_artifact_is_idempotent_lf_and_keeps_hand_classes(tmp_path):
    design = _stackup_design("d722_2l_mixed", {"SIG": CPWG_915, "+3V3": {"current": 1.0}})
    art = _artifact(design, tmp_path)
    pro = _write_project(tmp_path / "i.kicad_pro",
                         classes=[dict(DEFAULT_CLASS), dict(HAND_CLASS),
                                  {"name": "NC_OLD", "clearance": 9}],
                         patterns=[{"netclass": "HV_CUSTOM", "pattern": "HV*"},
                                   {"netclass": "NC_OLD", "pattern": "OLD"}])
    nw.apply_to_project(str(pro), design, cls=3, artifact=art)
    first = pro.read_bytes()
    nw.apply_to_project(str(pro), design, cls=3, artifact=art)
    assert pro.read_bytes() == first
    assert first.endswith(b"}\n") and b"\r\n" not in first
    ns = _read(pro)["net_settings"]
    # design net order (GND is the B.Cu pour, exempt), then SIG's coplanar ground GND
    assert [c["name"] for c in ns["classes"]] == ["Default", "HV_CUSTOM", "NC_P3V3", "NC_SIG",
                                                  "NC_GND"]
    assert [p["netclass"] for p in ns["netclass_patterns"]] == ["HV_CUSTOM", "NC_P3V3", "NC_SIG",
                                                                "NC_GND"]


def test_apply_without_artifact_writes_lf_and_a_trailing_newline(tmp_path, buck_design):
    pro = _write_project(tmp_path / "lf.kicad_pro")
    rep = nw.apply_to_project(str(pro), buck_design, cls=3)
    data = pro.read_bytes()
    assert data.endswith(b"}\n") and not data.endswith(b"\n\n") and b"\r\n" not in data
    assert rep["source"] == "design" and rep["plane_nets"] == []


def test_no_artifact_keeps_todays_classes(buck_design, hat_design):
    for design, cls in ((buck_design, 3), (hat_design, hat_design["class"])):
        assert nw.build_classes(design, cls) == nw.build_classes(design, cls, artifact=None)
    built = nw.build_classes(buck_design, 3)
    assert built["rule_floor"] is None
    assert [c["name"] for c in built["classes"]] == ["NC_VIN_RAW", "NC_VIN", "NC_SW", "NC_V5",
                                                     "NC_GND"]


_PROBE_BOARD = """
out, width = sys.argv[1], float(sys.argv[2])
mm = pcbnew.FromMM
b = pcbnew.CreateEmptyBoard()
for x0, y0, x1, y1 in ((0, 0, 20, 0), (20, 0, 20, 10), (20, 10, 0, 10), (0, 10, 0, 0)):
    s = pcbnew.PCB_SHAPE(b)
    s.SetShape(pcbnew.SHAPE_T_SEGMENT)
    s.SetStart(pcbnew.VECTOR2I(mm(x0), mm(y0)))
    s.SetEnd(pcbnew.VECTOR2I(mm(x1), mm(y1)))
    s.SetLayer(pcbnew.Edge_Cuts)
    b.Add(s)
nets = {n: pcbnew.NETINFO_ITEM(b, n) for n in ("GND", "+3V3", "SIG")}
for n in nets.values():
    b.Add(n)
t = pcbnew.PCB_TRACK(b)
t.SetStart(pcbnew.VECTOR2I(mm(2), mm(5)))
t.SetEnd(pcbnew.VECTOR2I(mm(18), mm(5)))
t.SetWidth(mm(width))
t.SetLayer(pcbnew.F_Cu)
t.SetNet(nets["SIG"])
b.Add(t)
z = pcbnew.ZONE(b)
z.SetLayer(pcbnew.F_Cu)
z.SetNet(nets["GND"])
z.SetLocalClearance(mm(0.05))
z.SetMinThickness(mm(0.1))
ol = z.Outline()
ol.NewOutline()
for x, y in ((0.5, 0.5), (19.5, 0.5), (19.5, 9.5), (0.5, 9.5)):
    ol.Append(mm(x), mm(y))
b.Add(z)
print(json.dumps({"saved": bool(pcbnew.SaveBoard(out, b))}))
"""
_PROBE_FILL = """
b = pcbnew.LoadBoard(sys.argv[1])
assert b is not None, "LoadBoard returned None"
pcbnew.ZONE_FILLER(b).Fill(b.Zones())
print(json.dumps({"saved": bool(pcbnew.SaveBoard(sys.argv[1], b))}))
"""


def _pour_gap(pcb: Path, width: float) -> float:
    """F.Cu GND fill edge to the SIG track edge (track (2,5)-(18,5)), from the saved file."""
    import math
    import re
    text = pcb.read_text(encoding="utf-8")
    body = re.search(r"\(filled_polygon\s+\(layer \"F\.Cu\"\)\s+\(pts(.*?)\)\s*\)\s*\)",
                     text, re.S).group(1)
    pts = [(float(x), float(y)) for x, y in re.findall(r"\(xy ([-\d.]+) ([-\d.]+)\)", body)]

    def to_track(x: float, y: float) -> float:
        return math.hypot(x - min(18.0, max(2.0, x)), y - 5.0)

    best = min(to_track(a[0] + (b[0] - a[0]) * k / 40, a[1] + (b[1] - a[1]) * k / 40)
               for a, b in zip(pts, pts[1:] + pts[:1], strict=True) for k in range(41))
    return best - width / 2


def test_live_kicad_pours_the_cpwg_gap_only_with_the_reference_class(tmp_path):
    """KiCad 9.0.4 fills at max(track class, pour-net class) clearance: the coplanar gap is
    realised exactly with the NC_GND class netclass_writer adds, and not without it."""
    from fixtures.swig_python import PREAMBLE, run_child
    design = _stackup_design("d722_2l_mixed", {"SIG": CPWG_915})
    art = _artifact(design, tmp_path)
    line = _line(art, "SIG")
    pcb = tmp_path / "probe.kicad_pcb"
    assert run_child(PREAMBLE + _PROBE_BOARD, [pcb, line["width"]], tmp_path)["saved"]
    rep = nw.apply_to_project(str(pcb.with_suffix(".kicad_pro")), design, cls=3, artifact=art)
    assert rep["ok"] and rep["classes_written"] == ["NC_SIG", "NC_GND"]
    pristine = pcb.read_bytes()
    assert run_child(PREAMBLE + _PROBE_FILL, [pcb], tmp_path)["saved"]
    assert _pour_gap(pcb, line["width"]) == pytest.approx(line["ground_gap"], abs=0.002)
    pro = _read(pcb.with_suffix(".kicad_pro"))                    # control: drop NC_GND
    ns = pro["net_settings"]
    ns["classes"] = [c for c in ns["classes"] if c["name"] != "NC_GND"]
    ns["netclass_patterns"] = [p for p in ns["netclass_patterns"] if p["netclass"] != "NC_GND"]
    pcb.with_suffix(".kicad_pro").write_text(json.dumps(pro, indent=2) + "\n", encoding="utf-8")
    pcb.write_bytes(pristine)
    assert run_child(PREAMBLE + _PROBE_FILL, [pcb], tmp_path)["saved"]
    assert _pour_gap(pcb, line["width"]) == pytest.approx(0.2, abs=0.002)   # GND on Default


def test_build_classes_never_mutates_its_inputs(tmp_path):
    design = _stackup_design("d722_2l_mixed", {"SIG": CPWG_915, "+3V3": {"current": 1.0}})
    art = _artifact(design, tmp_path)
    tmpl = dict(DEFAULT_CLASS)
    snap = copy.deepcopy((design, art, tmpl))
    nw.build_classes(design, cls=3, template=tmpl, artifact=art)
    assert (design, art, tmpl) == snap
