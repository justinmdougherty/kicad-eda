"""netclass_writer — IPC net classes into a .kicad_pro."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import impedance
import ipc_rules
import netclass_writer as nw

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
