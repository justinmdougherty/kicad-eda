"""netlist_schema_d7 — the D7 fragments of the netlist schema (ADR D7.2, D7.7, D7.11).

Covers: every ADR D7.2.2 / D7.7 example and every valid D7-02 stackup fixture
validates; copper_layers is 1 or an even 2..32; negative cases report their JSON
path; the fragment vocabularies match the modules that consume them; the merge
refuses key collisions and dangling $refs; docs/PIPELINE.md section 2 documents
every D7 key, and its D7 example validates and has no G1 stackup errors.
"""
from __future__ import annotations

import copy
import json
import re
from pathlib import Path

import pytest

import netlist_schema as ns
import netlist_schema_d7 as d7

ROOT = Path(__file__).resolve().parents[1]
STACKUP_FIXTURES = ROOT / "tests" / "fixtures" / "stackups"
PART = {"ref": "J1", "lib_id": "Connector_Generic:Conn_01x03",
        "footprint": "Connector_PinHeader_2.54mm:PinHeader_1x03_P2.54mm_Vertical"}


def _design(**keys) -> dict:
    return {"name": "d7", "components": [dict(PART)],
            "nets": {"GND": [["J1", "1"]], "+3V3": [["J1", "2"]]}, **keys}


# --- the ADR D7.2.2 examples (verbatim; the model reference split into its preset
# and its layers form, since a stackup has at most one of them) ---------------------
COPPER_ENTRY = ('{"type": "copper", "name": "F.Cu", "oz": 1, "thickness": 0.035, '
                '"role": "signal", "net": null}')
DIELECTRIC_ENTRY = ('{"type": "dielectric", "kind": "core", "thickness": 0.254, '
                    '"material": "rogers.ro4350b", "construction": "7628", "dk": 3.66, '
                    '"df": 0.0037, "dk_freq_hz": 1e10, "label": "RO4350B 10 mil", "locked": true}')
MODEL_COMMON = '''"fab": "jlcpcb", "board_thickness": 1.6, "finish": "ENIG",
  "mask": {"material": "lpi", "dk": 3.8, "df": 0.025, "color": "Green",
           "over_substrate": 0.0305, "over_trace": 0.0152, "between_traces": 0.0305},
  "dk_policy": "auto", "design_freq_hz": 9.15e8,
  "impedance_control": {"required": true, "tolerance_pct": 10},
  "via_spans": [["F.Cu", "B.Cu"]],
  "rationale": "4L: solid GND under the LoRa and GNSS feeds", "origin": "designer"'''
D722 = {
    "model_preset_form": '{"preset": "jlcpcb:JLC04161H-3313", '
                         '"roles": {"In2.Cu": {"role": "plane", "net": "+3V3"}}, '
                         + MODEL_COMMON + "}",
    "model_layers_form": '{"layers": [' + COPPER_ENTRY + ", " + DIELECTRIC_ENTRY + ''',
        {"type": "copper", "role": "mixed", "net": "GND"}], ''' + MODEL_COMMON + "}",
    "multi_ply_dielectric": '''{"layers": [{"type": "copper"},
        {"type": "dielectric", "kind": "prepreg", "plies": [
          {"thickness": 0.101, "material": "rogers.ro4450f", "construction": "4450F",
           "dk": 3.52, "df": 0.004, "dk_freq_hz": 1e10, "label": "RO4450F", "locked": true},
          {"material": "isola.370hr", "construction": "2116", "thickness": 0.12}]},
        {"type": "copper"}]}''',
    "2l_mixed_gnd": '''{"fab": "jlcpcb", "finish": "ENIG",
      "rationale": "2L: one RF feed, low density", "layers": [
        {"type": "copper", "oz": 1, "role": "signal"},
        {"type": "dielectric", "kind": "core", "material": "generic.fr4", "thickness": 1.51,
         "dk": 4.5},
        {"type": "copper", "oz": 1, "role": "mixed", "net": "GND"}]}''',
    "jlc_preset_in2_mixed": '''{"preset": "jlcpcb:JLC04161H-3313",
      "roles": {"In2.Cu": {"role": "mixed", "net": "+3V3"}},
      "impedance_control": {"required": true}, "rationale": "4L: USB + 50 ohm feeds"}''',
    "6l_2116": '''{"preset": "jlcpcb:JLC06161H-2116", "impedance_control": {"required": true},
      "rationale": "6L: two inner routing layers for the MCU fan-out"}''',
    "4l_hybrid": '''{"fab": "pcbway", "dk_policy": "design", "design_freq_hz": 1.575e9,
      "finish": "ENIG", "impedance_control": {"required": true, "tolerance_pct": 10},
      "rationale": "Rogers under the RF feeds only",
      "layers": [
        {"type": "copper", "oz": 1, "role": "signal"},
        {"type": "dielectric", "kind": "core", "material": "rogers.ro4350b", "thickness": 0.254,
         "locked": true},
        {"type": "copper", "oz": 1, "role": "plane", "net": "GND"},
        {"type": "dielectric", "kind": "prepreg", "plies": [
           {"material": "rogers.ro4450f", "thickness": 0.101},
           {"material": "isola.370hr", "construction": "2116", "thickness": 0.12}]},
        {"type": "copper", "oz": 0.5, "role": "plane", "net": "+3V3"},
        {"type": "dielectric", "kind": "core", "material": "isola.370hr", "thickness": 0.71},
        {"type": "copper", "oz": 1, "role": "signal"}]}''',
    "mask_off": '{"preset": "jlcpcb:JLC04161H-7628", "mask": null, "fab": null, "finish": null}',
    "legacy_shorthand": '{"h": 0.20, "er": 4.2, "t": 0.035}',
    "legacy_one_key": '{"er": 4.5}',
    "empty_model": "{}",
    "policy_origin": '''{"preset": "jlcpcb:JLC04161H-7628", "origin": "layer_policy",
      "rationale": "napkin default: 4 layers"}''',
}

# --- the ADR D7.7 example (placeholders [x, y, w, h], "a|b" filled in) -------------
D77_NET_SPECS = '''{"LORA_ANT": {"impedance": 50, "freq_hz": 9.15e8, "role": "tx",
  "line": "auto", "layers": ["F.Cu"],
  "ref_layers": {"F.Cu": ["In1.Cu"]}, "void_layers": {"F.Cu": []}, "ref_nets": ["GND"],
  "width": null, "ground_gap": null,
  "z_tol_pct": 5, "max_vias": 0, "max_len_mm": null, "stub_max_mm": null,
  "fence": {"pitch_mm": null, "offset_mm": null, "via": [0.6, 0.3], "rows": 1, "freq_hz": null},
  "isolation_mm": 1.0, "rf_group": "LORA", "ref_plane_margin_h": 5,
  "ref_plane_exempt": [{"rect": [12.0, 3.0, 4.0, 2.5], "layers": ["In1.Cu"],
                        "why": "SMA pin relief"}],
  "allow_branches": [["L1", "1"]], "route": "auto"},
 "USB_DP": {"diff": 90, "gap": 0.15, "diff_pair": "USB_DM", "pair": "edge", "line": "microstrip"},
 "GNSS_RF": {"impedance": 50, "freq_hz": 1.575e9, "role": "rx", "line": "cpwg", "width": 0.3,
  "ground_gap": 0.2, "max_vias": 2, "max_len_mm": 25, "stub_max_mm": 4.5, "fence": false,
  "route": "preroute"}}'''
D77_RF = '''{
  "chains": [{"name": "LORA_TX", "group": "LORA", "from": ["U3", "ANT"], "to": ["J2", "1"],
              "nets": ["LORA_RF1", "LORA_ANT"], "series": ["C10", "L3"], "shunts": ["C11"],
              "match_max_dist_mm": 2.0, "max_len_mm": 25, "tx_power_dbm": 20}],
  "isolation": [{"groups": ["LORA", "GNSS"], "min_mm": 15, "edges_differ": true}],
  "antennas": [{"ref": "AE1", "kind": "pcb_trace", "preset": null,
                "generator": {"family": "ifa", "band_hz": [8.63e8, 9.28e8], "params": {}},
                "feed_net": "ANT_FEED", "ground_net": "GND",
                "keepout": {"layers": "all", "from_footprint": true, "rect": null, "polygon": null},
                "ground_under": false, "edge": "top", "clearance_mm": 5, "license_ack": false,
                "match": {"topology": "pi", "refs": ["C10", "R2", "C11"], "max_dist_mm": 2.0,
                          "fit": {"C10": "NM", "R2": "0R", "C11": "NM"}},
                "sim": "none",
                "human_steps": [
                  "VNA-tune the pi network on the assembled board in its enclosure"]}],
  "coexistence": {
    "check": true,
    "tx": [{"group": "LORA", "band_hz": [9.02e8, 9.28e8], "power_dbm": 20},
           {"group": "BLE", "band_hz": [2.4e9, 2.4835e9], "power_dbm": 8}],
    "rx": [{"group": "GNSS",
            "bands_hz": [[1.559e9, 1.606e9], [1.196e9, 1.284e9], [1.164e9, 1.189e9]],
            "abs_max_dbm": 10, "oob_rejection_db": 30, "lna_gain_db": 43}],
    "isolation_db": [{"from": "LORA", "to": "GNSS", "db": 20}],
    "margin_db": 6}}'''
D77_LAYER_POLICY = '{"max_layers": 8, "fab": "jlcpcb"}'


def _d77_design() -> dict:
    d = _design(net_specs=json.loads(D77_NET_SPECS), rf=json.loads(D77_RF),
                layer_policy=json.loads(D77_LAYER_POLICY), copper_layers=4,
                stackup=json.loads(D722["jlc_preset_in2_mixed"]))
    d["components"][0]["clock_hz"] = [4.8e7, 32768]
    return d


def _full() -> dict:
    """Every D7 key in one valid design (the layers form of the model; the preset
    form, plies and the legacy keys have their own examples)."""
    d = _d77_design()
    d["stackup"] = json.loads(D722["model_layers_form"])
    d["copper_layers"] = 2
    d["stackup"]["layers"][1]["dielectric_model"] = "djordjevic_sarkar"
    d["stackup"]["layers"][2]["name"] = "B.Cu"
    return d


ELSEWHERE = {"preset", "roles", "plies", "h", "er", "t"}   # keys _full() cannot carry


def _errors_at(errors: list[str], path: str) -> list[str]:
    return [e for e in errors if e.startswith(f"{path}: ")]


def _assert_error(errors: list[str], path: str, fragment: str) -> None:
    hits = _errors_at(errors, path)
    assert hits, f"no error at {path}; got {errors}"
    assert any(fragment in e for e in hits), f"no error at {path} mentions {fragment!r}; {hits}"


# --- valid designs ---------------------------------------------------------------
@pytest.mark.parametrize("name", sorted(D722))
def test_every_adr_d722_stackup_example_validates(name):
    assert ns.validate(_design(stackup=json.loads(D722[name]))) == []


def test_adr_d722_single_sided_example_validates():
    assert ns.validate(_design(copper_layers=1)) == []


def test_adr_d77_example_validates():
    assert ns.validate(_d77_design()) == []


def test_a_design_using_every_d7_key_validates():
    d = _full()
    assert ns.validate(d) == []
    used = _keys_of(d)
    assert set(d7.D7_KEYS) - ELSEWHERE <= used, set(d7.D7_KEYS) - ELSEWHERE - used
    assert ELSEWHERE <= set(d7.D7_KEYS)


def _keys_of(node) -> set[str]:
    if isinstance(node, dict):
        return set(node) | {k for v in node.values() for k in _keys_of(v)}
    if isinstance(node, list):
        return {k for v in node for k in _keys_of(v)}
    return set()


def _stackup_fixtures() -> list[Path]:
    return sorted(STACKUP_FIXTURES.glob("*.json"))


@pytest.mark.parametrize("path", _stackup_fixtures(), ids=lambda p: p.stem)
def test_stackup_fixture_designs_validate_unless_the_shape_is_wrong(path):
    """D7-02's fixtures: every valid design validates; of the error fixtures ST-E3
    (preset + layers) and ST-E5 (a value out of range) are schema matters too, at
    the lint's path; the rest are lint semantics."""
    fixture = json.loads(path.read_text(encoding="utf-8"))
    errors = ns.validate(fixture["design"])
    expect = fixture["expect"]
    if expect.get("error") == "ST-E3":
        _assert_error(errors, "$.stackup", "at most one of 'preset' and 'layers'")
    elif expect.get("error") == "ST-E5":
        _assert_error(errors, expect["path"], "must be > 0")
    else:
        assert errors == [], path.stem


@pytest.mark.parametrize("count", d7.COPPER_LAYER_COUNTS)
def test_every_d721_copper_count_is_valid(count):
    assert ns.validate(_design(copper_layers=count)) == []


def test_copper_layer_counts_are_one_or_even_two_to_thirty_two():
    assert d7.COPPER_LAYER_COUNTS == (1,) + tuple(range(2, 33, 2))
    assert ns.COPPER_LAYER_COUNTS == d7.COPPER_LAYER_COUNTS


@pytest.mark.parametrize("bad", [0, 3, 5, 31, 33, 34, -2])
def test_other_copper_counts_are_rejected(bad):
    _assert_error(ns.validate(_design(copper_layers=bad)), "$.copper_layers",
                  "1 (single-sided) or an even number 2..32")


def test_stackup_null_and_legacy_keep_working():
    assert ns.validate(_design(stackup=None)) == []
    assert ns.validate(_design(stackup={"h": 0.2, "er": 4.2})) == []


# --- negative cases --------------------------------------------------------------
def _set(path: str, value):
    def mutate(d: dict) -> None:
        keys = [int(k) if k.lstrip("-").isdigit() else k for k in path.split("/")]
        target = d
        for k in keys[:-1]:
            target = target[k]
        target[keys[-1]] = value
    mutate.__name__ = f"{path}={value!r}"
    return mutate


def _drop(path: str):
    def mutate(d: dict) -> None:
        keys = [int(k) if k.isdigit() else k for k in path.split("/")]
        target = d
        for k in keys[:-1]:
            target = target[k]
        del target[keys[-1]]
    mutate.__name__ = f"drop {path}"
    return mutate


ST, LAY = "stackup", "stackup/layers"
SPEC, RF = "net_specs/LORA_ANT", "rf"
NEGATIVE = [
    # copper_layers
    (_set("copper_layers", 4.0), "$.copper_layers", "expected integer"),
    (_set("copper_layers", "2"), "$.copper_layers", "expected integer"),
    # stackup shape (D7.2.2)
    (_set(ST, "jlcpcb:JLC04161H-7628"), "$.stackup", "legacy {h, er, t} shorthand"),
    (_set(ST, {"h": 0.2, "preset": "jlcpcb:JLC04161H-7628"}), "$.stackup", "cannot be mixed"),
    (_set(ST, {"h": 0}), "$.stackup.h", "must be > 0"),
    (_set(ST, {"er": 20}), "$.stackup.er", "er is a number in [1, 15]"),
    (_set(ST, {"t": "35um"}), "$.stackup.t", "expected number"),
    (_set(f"{ST}/preset", "jlcpcb:JLC04161H-7628"), "$.stackup",
     "at most one of 'preset' and 'layers'"),
    (_set(f"{ST}/roles", {"In1.Cu": {"role": "plane"}}), "$.stackup", "needs 'preset'"),
    (_set(f"{ST}/presets", "x"), "$.stackup", "unknown key 'presets'"),
    (_set(f"{ST}/origin", "napkin"), "$.stackup.origin", "must be one of"),
    (_set(f"{ST}/dk_policy", "best"), "$.stackup.dk_policy", "must be one of"),
    (_set(f"{ST}/finish", "gold"), "$.stackup.finish", "must be one of"),
    (_set(f"{ST}/fab", ""), "$.stackup.fab", "must not be empty"),
    (_set(f"{ST}/board_thickness", 0), "$.stackup.board_thickness", "must be > 0"),
    (_set(f"{ST}/design_freq_hz", -1), "$.stackup.design_freq_hz", "must be > 0"),
    (_set(f"{ST}/rationale", ""), "$.stackup.rationale", "must not be empty"),
    (_set(f"{ST}/impedance_control/required", "yes"), "$.stackup.impedance_control.required",
     "expected boolean"),
    (_set(f"{ST}/impedance_control/tol", 10), "$.stackup.impedance_control", "unknown key 'tol'"),
    (_set(f"{ST}/impedance_control/tolerance_pct", 0),
     "$.stackup.impedance_control.tolerance_pct", "must be > 0"),
    (_set(f"{ST}/via_spans", [["F.Cu"]]), "$.stackup.via_spans[0]", "[top layer, bottom layer]"),
    (_set(f"{ST}/via_spans", [["F.Cu", "Top"]]), "$.stackup.via_spans[0][1]", "copper layer"),
    (_set(f"{ST}/via_spans", []), "$.stackup.via_spans", "at least 1"),
    (_set(f"{ST}/mask/thickness", 0.02), "$.stackup.mask", "unknown key 'thickness'"),
    (_set(f"{ST}/mask/over_trace", 0), "$.stackup.mask.over_trace", "must be > 0"),
    (_set(f"{ST}/mask/dk", 0.5), "$.stackup.mask.dk", "dk is a number in [1, 15]"),
    (_set(f"{ST}/mask", "lpi"), "$.stackup.mask", "expected object or null"),
    # layer entries
    (_set(LAY, []), "$.stackup.layers", "at least 1"),
    (_set(f"{LAY}/0", {"oz": 1}), "$.stackup.layers[0]", "'type' is a required property"),
    (_set(f"{LAY}/0/type", "metal"), "$.stackup.layers[0].type", "must be one of"),
    (_set(f"{LAY}/0/material", "fr4"), "$.stackup.layers[0]", "unknown key 'material'"),
    (_set(f"{LAY}/0/oz", 0), "$.stackup.layers[0].oz", "must be > 0"),
    (_set(f"{LAY}/0/role", "ground"), "$.stackup.layers[0].role", "must be one of"),
    (_set(f"{LAY}/0/name", "Top"), "$.stackup.layers[0].name", "copper layer"),
    (_set(f"{LAY}/0/net", ""), "$.stackup.layers[0].net", "must not be empty"),
    (_set(f"{LAY}/1/plies", [{"thickness": 0.1}]), "$.stackup.layers[1]", "per ply"),
    (_drop(f"{LAY}/1/thickness"), "$.stackup.layers[1]", "needs a 'thickness'"),
    (_set(f"{LAY}/1/kind", "laminate"), "$.stackup.layers[1].kind", "must be one of"),
    (_set(f"{LAY}/1/dk", 0.5), "$.stackup.layers[1].dk", "dk is a number in [1, 15]"),
    (_set(f"{LAY}/1/df", 0.5), "$.stackup.layers[1].df", "df is a number in [0, 0.1]"),
    (_set(f"{LAY}/1/locked", "yes"), "$.stackup.layers[1].locked", "expected boolean"),
    (_set(f"{LAY}/1/role", "signal"), "$.stackup.layers[1]", "unknown key 'role'"),
    (_set(f"{LAY}/1/thickness", -0.1), "$.stackup.layers[1].thickness", "must be > 0"),
    (_set(f"{LAY}/1", {"type": "dielectric", "plies": []}), "$.stackup.layers[1].plies",
     "at least 1"),
    (_set(f"{LAY}/1", {"type": "dielectric", "plies": [{"kind": "core", "thickness": 0.1}]}),
     "$.stackup.layers[1].plies[0]", "unknown key 'kind'"),
    (_set(f"{LAY}/1", {"type": "dielectric", "plies": [{"material": "x"}]}),
     "$.stackup.layers[1].plies[0]", "'thickness' is a required property"),
    (_set(f"{LAY}/1", {"type": "dielectric", "plies": [{"type": "copper", "thickness": 0.1}]}),
     "$.stackup.layers[1].plies[0].type", "dielectric"),
    # net_specs RF keys (D7.7)
    (_set(f"{SPEC}/freq_hz", 0), "$.net_specs.LORA_ANT.freq_hz", "must be > 0"),
    (_set(f"{SPEC}/role", "both"), "$.net_specs.LORA_ANT.role", "must be one of"),
    (_set(f"{SPEC}/line", "coax"), "$.net_specs.LORA_ANT.line", "must be one of"),
    (_set(f"{SPEC}/pair", "side"), "$.net_specs.LORA_ANT.pair", "must be one of"),
    (_set(f"{SPEC}/layers", "F.Cu"), "$.net_specs.LORA_ANT.layers", "expected array"),
    (_set(f"{SPEC}/layers", []), "$.net_specs.LORA_ANT.layers", "at least 1"),
    (_set(f"{SPEC}/layers", ["Top"]), "$.net_specs.LORA_ANT.layers[0]", "copper layer"),
    (_set(f"{SPEC}/ref_layers", {"F.Cu": "In1.Cu"}), "$.net_specs.LORA_ANT.ref_layers['F.Cu']",
     "expected array"),
    (_set(f"{SPEC}/void_layers", {"Top": []}), "$.net_specs.LORA_ANT.void_layers",
     "copper layer"),
    (_set(f"{SPEC}/ref_nets", []), "$.net_specs.LORA_ANT.ref_nets", "at least 1"),
    (_set(f"{SPEC}/width", 0), "$.net_specs.LORA_ANT.width", "must be > 0"),
    (_set(f"{SPEC}/ground_gap", "0.2"), "$.net_specs.LORA_ANT.ground_gap", "expected number"),
    (_set(f"{SPEC}/z_tol_pct", 0), "$.net_specs.LORA_ANT.z_tol_pct", "must be > 0"),
    (_set(f"{SPEC}/max_vias", -1), "$.net_specs.LORA_ANT.max_vias", "must be >= 0"),
    (_set(f"{SPEC}/max_vias", 1.5), "$.net_specs.LORA_ANT.max_vias", "expected integer"),
    (_set(f"{SPEC}/fence", True), "$.net_specs.LORA_ANT.fence", "an object or false"),
    (_set(f"{SPEC}/fence/spacing", 3), "$.net_specs.LORA_ANT.fence", "unknown key 'spacing'"),
    (_set(f"{SPEC}/fence/via", [0.6]), "$.net_specs.LORA_ANT.fence.via", "[diameter, drill]"),
    (_set(f"{SPEC}/fence/rows", 0), "$.net_specs.LORA_ANT.fence.rows", "must be >= 1"),
    (_set(f"{SPEC}/isolation_mm", -1), "$.net_specs.LORA_ANT.isolation_mm", "must be >= 0"),
    (_set(f"{SPEC}/route", "freerouting"), "$.net_specs.LORA_ANT.route", "must be one of"),
    (_set(f"{SPEC}/ref_plane_exempt", [{"rect": [0, 0, 1], "why": "x"}]),
     "$.net_specs.LORA_ANT.ref_plane_exempt[0].rect", "[x, y, w, h]"),
    (_set(f"{SPEC}/ref_plane_exempt", [{"rect": [0, 0, 1, 1]}]),
     "$.net_specs.LORA_ANT.ref_plane_exempt[0]", "'why' is a required property"),
    (_set(f"{SPEC}/allow_branches", [["L1"]]), "$.net_specs.LORA_ANT.allow_branches[0]",
     "[ref, pad]"),
    # rf block
    (_set(f"{RF}/chain", []), "$.rf", "unknown key 'chain'"),
    (_set(RF, []), "$.rf", "expected object or null"),
    (_drop(f"{RF}/chains/0/name"), "$.rf.chains[0]", "'name' is a required property"),
    (_set(f"{RF}/chains/0/from", ["U3"]), "$.rf.chains[0].from", "[ref, pad]"),
    (_set(f"{RF}/chains/0/hops", 2), "$.rf.chains[0]", "unknown key 'hops'"),
    (_set(f"{RF}/isolation/0/groups", ["LORA"]), "$.rf.isolation[0].groups", "at least 2"),
    (_set(f"{RF}/antennas/0/kind", "whip"), "$.rf.antennas[0].kind", "must be one of"),
    (_set(f"{RF}/antennas/0/edge", "north"), "$.rf.antennas[0].edge", "must be one of"),
    (_set(f"{RF}/antennas/0/generator/family", "yagi"), "$.rf.antennas[0].generator.family",
     "must be one of"),
    (_set(f"{RF}/antennas/0/generator/band_hz", [9e8]), "$.rf.antennas[0].generator.band_hz",
     "[low, high]"),
    (_set(f"{RF}/antennas/0/keepout/layers", "some"), "$.rf.antennas[0].keepout.layers",
     '"all" or a list of copper layers'),
    (_set(f"{RF}/antennas/0/keepout/rect", [0, 0, 5]), "$.rf.antennas[0].keepout.rect",
     "[x, y, w, h]"),
    (_set(f"{RF}/antennas/0/match/fit", {"C10": 0}), "$.rf.antennas[0].match.fit.C10",
     "expected string"),
    (_set(f"{RF}/coexistence/tx/0/power_dbm", "20"), "$.rf.coexistence.tx[0].power_dbm",
     "expected number"),
    (_set(f"{RF}/coexistence/rx/0/bands_hz", []), "$.rf.coexistence.rx[0].bands_hz",
     "at least 1"),
    (_drop(f"{RF}/coexistence/isolation_db/0/db"), "$.rf.coexistence.isolation_db[0]",
     "'db' is a required property"),
    # components[].clock_hz and layer_policy
    (_set("components/0/clock_hz", "48MHz"), "$.components[0].clock_hz",
     "expected number or array"),
    (_set("components/0/clock_hz", []), "$.components[0].clock_hz", "clock frequency in Hz"),
    (_set("components/0/clock_hz", -1), "$.components[0].clock_hz", "clock frequency in Hz"),
    (_set("components/0/clock_hz", [0]), "$.components[0].clock_hz[0]", "must be > 0"),
    (_set("layer_policy/max_layers", 7), "$.layer_policy.max_layers", "even number 2..32"),
    (_set("layer_policy/scoring", True), "$.layer_policy", "unknown key 'scoring'"),
    (_set("layer_policy/fab", ""), "$.layer_policy.fab", "must not be empty"),
]


@pytest.mark.parametrize("mutate,path,fragment", NEGATIVE,
                         ids=[m.__name__ for m, _p, _f in NEGATIVE])
def test_negative_cases_report_the_path(mutate, path, fragment):
    d = _full()
    mutate(d)
    _assert_error(ns.validate(d), path, fragment)


def test_preset_form_negative_cases():
    d = _d77_design()
    d["stackup"]["preset"] = "JLC04161H-3313"
    _assert_error(ns.validate(d), "$.stackup.preset", "'<fab>:<template>'")
    d = _d77_design()
    d["stackup"]["roles"] = {"Top": {"role": "plane"}}
    _assert_error(ns.validate(d), "$.stackup.roles", "copper layer")
    d = _d77_design()
    d["stackup"]["roles"]["In2.Cu"] = {"net": "+3V3"}
    _assert_error(ns.validate(d), "$.stackup.roles['In2.Cu']", "'role' is a required property")
    d["stackup"]["roles"]["In2.Cu"] = {"role": "mixed", "net": "+3V3", "oz": 1}
    _assert_error(ns.validate(d), "$.stackup.roles['In2.Cu']", "unknown key 'oz'")


def test_a_wrong_kind_of_stackup_is_one_type_error():
    errs = _errors_at(ns.validate(_design(stackup=[1, 2])), "$.stackup")
    assert len(errs) == 1 and ": expected object or null" in errs[0], errs


def test_validate_never_mutates_a_d7_design():
    d = _full()
    d["stackup"]["layers"][0]["oz"] = -1
    before = copy.deepcopy(d)
    ns.validate(d)
    assert d == before


# --- the vocabularies match their consumers ----------------------------------------
def test_vocabularies_match_the_consuming_modules():
    import fab_presets
    import impedance
    import materials
    import stackup_model
    import stackup_roles
    from tline import geometry
    assert set(d7.STACKUP_MODEL_KEYS) == set(stackup_model.MODEL_KEYS)
    assert set(d7.LEGACY_STACKUP_KEYS) == set(stackup_model.LEGACY_KEYS)
    assert set(d7.MASK_KEYS) == set(stackup_model.MASK_KEYS)
    assert set(d7.COPPER_ENTRY_KEYS) == set(stackup_roles.COPPER_KEYS)
    assert set(d7.PLY_KEYS) == set(stackup_roles.PLY_KEYS)
    assert set(d7.DIELECTRIC_ENTRY_KEYS) == set(stackup_roles.DIELECTRIC_KEYS)
    assert d7.LAYER_ROLES == fab_presets.ROLES == stackup_roles.ROLES
    assert d7.DIELECTRIC_KINDS == stackup_roles.KINDS
    assert d7.FINISHES == stackup_model.FINISHES
    assert d7.ORIGINS == stackup_model.ORIGINS
    assert d7.DK_POLICIES == materials.POLICIES
    assert d7.LINE_KINDS == geometry.LINES
    assert d7.PAIR_KINDS == impedance.PAIRS
    assert (d7.DK_RANGE, d7.DF_RANGE) == (stackup_roles.DK_RANGE, stackup_roles.DF_RANGE)
    assert d7.EDGES == ns.BOARD_EDGES
    assert d7.X_MESSAGE == ns.X_MESSAGE
    for n in range(-2, 40):
        try:
            stackup_roles.check_count(n)
            valid = True
        except ValueError:
            valid = False
        assert valid == (n in d7.COPPER_LAYER_COUNTS), n


def test_schema_properties_come_from_the_fragments():
    props, defs = ns.SCHEMA["properties"], ns.SCHEMA["$defs"]
    for key, frag in d7.TOP_LEVEL_PROPERTIES.items():
        assert props[key] is frag
    for key in d7.NET_SPEC_PROPERTIES:
        assert key in defs["net_spec"]["properties"]
    assert "clock_hz" in defs["component"]["properties"]
    assert set(d7.DEFS) <= set(defs)
    assert {"copper_layers", "stackup", "rf", "layer_policy"} <= set(props)


def test_d7_keys_list_every_property_of_the_fragments():
    for key in ("copper_layers", "stackup", "rf", "layer_policy", "clock_hz", "freq_hz",
                "preset", "roles", "layers", "plies", "over_substrate", "ref_plane_exempt",
                "chains", "antennas", "coexistence", "isolation_db", "max_layers", "origin"):
        assert key in d7.D7_KEYS, key
    assert list(d7.D7_KEYS) == sorted(set(d7.D7_KEYS))


# --- the merge -------------------------------------------------------------------
def test_merge_refuses_a_key_collision_and_never_mutates():
    base = {"net_spec": {"type": "object", "properties": {"freq_hz": {}}},
            "component": {"type": "object", "properties": {}}}
    snapshot = copy.deepcopy(base)
    with pytest.raises(ValueError, match="freq_hz"):
        d7.merge_defs(base)
    assert base == snapshot
    with pytest.raises(ValueError, match="stackup"):
        d7.merge_top_level({"stackup": {}})


def test_merge_refuses_a_dangling_ref():
    base = {"net_spec": {"type": "object", "properties": {}},
            "component": {"type": "object", "properties": {}}}
    with pytest.raises(ValueError, match="copper_layer"):
        d7.merge_defs(base)               # the fragments use the base's copper_layer def


def test_merge_needs_the_component_and_net_spec_defs():
    with pytest.raises(ValueError, match="component"):
        d7.merge_defs({"net_spec": {"properties": {}}})


# --- docs/PIPELINE.md section 2 --------------------------------------------------
def _section_2() -> str:
    text = (ROOT / "docs" / "PIPELINE.md").read_text(encoding="utf-8")
    start = text.index("## 2. Author the netlist")
    return text[start:text.index("\n## 3.", start)]


def _strip_comments(text: str) -> str:
    out, in_str, i = [], False, 0
    while i < len(text):
        ch = text[i]
        if in_str and ch == "\\":
            out.append(text[i:i + 2])
            i += 2
            continue
        if ch == '"':
            in_str = not in_str
        elif not in_str and text.startswith("//", i):
            i = text.find("\n", i) if text.find("\n", i) >= 0 else len(text)
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _section_2_designs() -> list[dict]:
    blocks = re.findall(r"```jsonc\n(.*?)```", _section_2(), re.DOTALL)
    return [json.loads(_strip_comments(b)) for b in blocks]


def test_pipeline_section_2_documents_every_d7_key():
    doc = _section_2()
    missing = sorted(k for k in d7.D7_KEYS if f'"{k}"' not in doc)
    assert not missing, f"PIPELINE.md section 2 does not document {missing}"
    for phrase in ("1 | even 2..32 (default 4)", "lint_rules_stackup", "netlist_schema_d7"):
        assert phrase in doc, phrase


def test_pipeline_section_2_d7_example_validates_and_has_no_stackup_errors():
    import lint_rules_stackup
    designs = [d for d in _section_2_designs() if "rf" in d]
    assert len(designs) == 1, "section 2 has one stackup / RF example"
    d = designs[0]
    assert ns.validate(d) == []
    assert isinstance(d["stackup"].get("layers"), list) and d.get("layer_policy")
    errors, _warnings = lint_rules_stackup.check(d, None)
    assert errors == []
