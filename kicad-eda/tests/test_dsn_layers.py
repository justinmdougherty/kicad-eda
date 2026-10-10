"""server/tools/dsn_layers.py: the DSN layer policy for Freerouting (D7-19,
ADR D7.9).

postprocess(dsn_text, policy) rewrites KiCad's Specctra export so Freerouting
honours the stackup roles: plane layers typed (type power) become (type
signal); every non-plane class gets (circuit (use_layer <signal + mixed
layers>)); each plane net moves to its own class kicad_eda_plane_<net>,
restricted to the signal layers plus its own plane layer(s) (or unrestricted:
the T10 variant); every impedance net moves to its own class on its
Freerouting layers (the primary layer plus the layers whose solved width lies
in the primary's window; others only with a verified per-class layer_rule);
unused layers are excluded everywhere. derive_policy_from_dsn builds a policy
from the DSN alone: planes come from (type power) layers or inner layers that
carry a (plane ...), never from outer pours (E10).

Unit tier only (dev venv, no KiCad): fixtures under tests/fixtures/dsn_layers/
are real KiCad 9.0.4 exports (cp2102n 4L, a 6L JLC06161H-2116 role build with
LT_POWER planes, a 2L board with a B.Cu GND pour) plus a hand-written file with
quoted names, wrapped class lists, an outer F.Cu GND pour and a B-side SMD part.
The live tier (Freerouting runs) is tests/test_dsn_layers_live.py.
"""
from __future__ import annotations

import ast
import copy
import json
from pathlib import Path

import pytest

import dsn_canon
import dsn_layers as dl
import dsn_sexpr as ds

HERE = Path(__file__).resolve().parent
FIX = HERE / "fixtures" / "dsn_layers"
MODULE = HERE.parent / "server" / "tools" / "dsn_layers.py"

POLICY_4L = {"signal_layers": ["F.Cu", "B.Cu"], "plane_nets": {"GND": ["In1.Cu"],
                                                               "+3V3": ["In2.Cu"]},
             "unused_layers": [], "net_layers": {}}
POLICY_6L = {"signal_layers": ["F.Cu", "In2.Cu", "In3.Cu", "B.Cu"],
             "plane_nets": {"GND": ["In1.Cu"], "+3V3": ["In4.Cu"]},
             "unused_layers": [], "net_layers": {}}
POLICY_2L = {"signal_layers": ["F.Cu", "B.Cu"], "plane_nets": {}, "unused_layers": [],
             "net_layers": {}}
POLICY_1L = {"signal_layers": ["F.Cu"], "plane_nets": {}, "unused_layers": ["B.Cu"],
             "net_layers": {}}


def fixture(name: str, newline: str = "\n") -> str:
    text = (FIX / f"{name}.dsn.txt").read_bytes().decode("utf-8").replace("\r\n", "\n")
    return text.replace("\n", newline)


def classes(text: str) -> dict[str, dict]:
    """{class name: {nets, use_layer, width, layer_rules, tokens}} read back
    through dsn_sexpr (names unquoted)."""
    q = ds.declared_quote(text)
    net = ds.child(ds.parse_nodes(text)[0], "network")
    out = {}
    for c in ds.find_all(net, "class"):
        toks = [x for x in c.items if isinstance(x, ds.Token)]
        circuit = ds.child(c, "circuit")
        use = ds.child(circuit, "use_layer") if circuit else None
        rule = ds.child(c, "rule")
        width = ds.child(rule, "width") if rule else None
        lrules = {}
        for lr in ds.find_all(c, "layer_rule"):
            layers = [t.value for t in lr.items if isinstance(t, ds.Token)]
            w = ds.child(ds.child(lr, "rule"), "width")
            lrules.update({layer: float(w.items[0].value) for layer in layers})
        out[ds.unquote(toks[0].value, q)] = {
            "nets": [ds.unquote(t.value, q) for t in toks[1:]],
            "tokens": [t.value for t in toks[1:]],
            "use_layer": [t.value for t in use.items] if use else None,
            "width": float(width.items[0].value) if width else None,
            "layer_rules": lrules}
    return out


def layer_types(text: str) -> dict[str, str]:
    st = ds.child(ds.parse_nodes(text)[0], "structure")
    return {lay.items[0].value: ds.child(lay, "type").items[0].value
            for lay in ds.find_all(st, "layer")}


def section(text: str, name: str) -> str:
    node = ds.child(ds.parse_nodes(text)[0], name)
    return text[node.start:node.end]


def imp(primary: str, widths: dict, window: list | None = None) -> dict:
    entry = {"primary": primary, "widths": widths}
    return entry if window is None else {**entry, "window": window}


# ----------------------------------------------------------------------------
# the 4L restricted (D7) variant on the real cp2102n export
# ----------------------------------------------------------------------------
def test_cp2102n_restricted_split() -> None:
    raw = fixture("cp2102n")
    text, rep = dl.postprocess(raw, POLICY_4L)
    got = classes(text)
    assert list(got) == ["kicad_default", "kicad_eda_plane_+3V3", "kicad_eda_plane_GND"]
    assert "GND" not in got["kicad_default"]["nets"] and "+3V3" not in got["kicad_default"]["nets"]
    assert len(got["kicad_default"]["nets"]) == 16
    assert got["kicad_default"]["use_layer"] == ["F.Cu", "B.Cu"]
    assert got["kicad_eda_plane_GND"] == {"nets": ["GND"], "tokens": ["GND"],
                                          "use_layer": ["F.Cu", "In1.Cu", "B.Cu"],
                                          "width": 200.0, "layer_rules": {}}
    assert got["kicad_eda_plane_+3V3"]["use_layer"] == ["F.Cu", "In2.Cu", "B.Cu"]
    assert rep["variant"] == dl.RESTRICTED and rep["retyped"] == []
    assert rep["nets_moved"] == {"+3V3": "kicad_eda_plane_+3V3", "GND": "kicad_eda_plane_GND"}
    assert rep["pad_layer_excluded"] == {} and rep["pad_layer_blocked"] == {}
    assert rep["warnings"] == []
    for name in ("placement", "library", "wiring", "structure"):
        assert section(text, name) == section(raw, name), name


def test_report_lists_every_class_with_its_layers() -> None:
    _text, rep = dl.postprocess(fixture("cp2102n"), POLICY_4L)
    by_name = {c["name"]: c for c in rep["classes"]}
    assert by_name["kicad_default"]["from"] == "kicad_default"
    assert by_name["kicad_default"]["use_layer"] == ["F.Cu", "B.Cu"]
    assert by_name["kicad_eda_plane_GND"] == {"name": "kicad_eda_plane_GND",
                                              "from": "kicad_default", "nets": ["GND"],
                                              "use_layer": ["F.Cu", "In1.Cu", "B.Cu"],
                                              "width_mm": None, "layer_rules": {}}
    json.dumps(rep)                               # JSON-able as one result line


def test_unrestricted_variant_leaves_plane_classes_without_use_layer() -> None:
    raw = fixture("cp2102n")
    for text, rep in (dl.postprocess(raw, POLICY_4L, variant=dl.UNRESTRICTED),
                      dl.postprocess(raw, {**POLICY_4L, "plane_classes": "unrestricted"})):
        got = classes(text)
        assert rep["variant"] == dl.UNRESTRICTED
        assert got["kicad_default"]["use_layer"] == ["F.Cu", "B.Cu"]
        assert got["kicad_eda_plane_GND"]["use_layer"] is None
        assert got["kicad_eda_plane_+3V3"]["use_layer"] is None


def test_output_parses_and_survives_dsn_canon() -> None:
    text, _rep = dl.postprocess(fixture("cp2102n"), POLICY_4L)
    canon = dsn_canon.canonicalize(text)
    assert classes(canon)["kicad_eda_plane_GND"]["use_layer"] == ["F.Cu", "In1.Cu", "B.Cu"]
    assert ds.parse(canon)[0] == "pcb"


def test_deterministic_and_inputs_untouched() -> None:
    raw, policy = fixture("cp2102n"), copy.deepcopy(POLICY_4L)
    first = dl.postprocess(raw, policy)
    assert dl.postprocess(raw, policy) == first and policy == POLICY_4L


# ----------------------------------------------------------------------------
# 6L: (type power) planes are retyped, inner signal layers routable
# ----------------------------------------------------------------------------
def test_six_layer_retypes_power_planes_and_routes_inner_signals() -> None:
    raw = fixture("six_layer")
    assert layer_types(raw) == {"F.Cu": "signal", "In1.Cu": "power", "In2.Cu": "signal",
                                "In3.Cu": "signal", "In4.Cu": "power", "B.Cu": "signal"}
    text, rep = dl.postprocess(raw, POLICY_6L)
    assert set(layer_types(text).values()) == {"signal"}
    assert rep["retyped"] == ["In1.Cu", "In4.Cu"]
    got = classes(text)
    assert got["kicad_default"]["use_layer"] == ["F.Cu", "In2.Cu", "In3.Cu", "B.Cu"]
    assert got["kicad_eda_plane_GND"]["use_layer"] == ["F.Cu", "In1.Cu", "In2.Cu", "In3.Cu",
                                                       "B.Cu"]
    assert got["kicad_eda_plane_+3V3"]["use_layer"] == ["F.Cu", "In2.Cu", "In3.Cu", "In4.Cu",
                                                        "B.Cu"]
    before, after = ds.tokenize(section(raw, "structure")), ds.tokenize(section(text, "structure"))
    assert len(before) == len(after)
    changed = [(a.value, b.value) for a, b in zip(before, after, strict=True) if a.value != b.value]
    assert changed == [("power", "signal"), ("power", "signal")]


# ----------------------------------------------------------------------------
# quoted names, wrapped lists, token reuse, newlines
# ----------------------------------------------------------------------------
@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_quoted_names_and_wrapped_lists_survive(newline: str) -> None:
    raw = fixture("quoted_wrapped", newline)
    text, rep = dl.postprocess(raw, POLICY_4L)
    got = classes(text)
    assert got["kicad_default"]["tokens"] == classes(raw)["kicad_default"]["tokens"]
    assert '"Net-(D1-A)"' in got["kicad_default"]["tokens"]
    assert '""' in got["kicad_default"]["tokens"]                  # the empty name kept
    assert got["Power"]["tokens"] == ['"#RESET"', '/ANT_2"']      # KiCad's spelling reused
    assert got["Power"]["use_layer"] == ["F.Cu", "B.Cu"]
    assert got["kicad_eda_plane_GND"]["width"] == 400.0           # copied from Power
    assert rep["nets_moved"] == {"+3V3": "kicad_eda_plane_+3V3", "GND": "kicad_eda_plane_GND"}
    if newline == "\r\n":
        assert "\n" not in text.replace("\r\n", "")
    else:
        assert "\r" not in text


def test_a_class_of_plane_nets_only_is_replaced_by_the_plane_classes() -> None:
    raw = fixture("quoted_wrapped").replace(
        '(class Power "#RESET" +3V3 /ANT_2" GND', "(class Power +3V3 GND").replace(
        '"unconnected-(U1-PA0-Pad13)"\n', '"unconnected-(U1-PA0-Pad13)" "#RESET" /ANT_2"\n')
    text, rep = dl.postprocess(raw, POLICY_4L)
    got = classes(text)
    assert "Power" not in got and [c["from"] for c in rep["classes"]] == [
        "kicad_default", "Power", "Power"]
    assert got["kicad_eda_plane_+3V3"]["width"] == got["kicad_eda_plane_GND"]["width"] == 400.0
    assert got["kicad_default"]["tokens"][-2:] == ['"#RESET"', '/ANT_2"']


def test_long_member_lists_are_wrapped() -> None:
    raw = fixture("cp2102n")
    names = [f"N{k:03d}_{'x' * 20}" for k in range(12)]
    widened = raw.replace("(class kicad_default +3V3", "(class kicad_default " +
                          " ".join(names) + " +3V3")
    for n in names:
        widened = widened.replace("  (class kicad_default", f"  (net {n}\n      (pins R1-1)\n"
                                  "    )\n    (class kicad_default", 1)
    text, _rep = dl.postprocess(widened, POLICY_4L)
    node = ds.find_all(ds.child(ds.parse_nodes(text)[0], "network"), "class")[0]
    body = text[node.start:ds.child(node, "circuit").start]
    assert body.count("\n") >= 3 and max(len(x) for x in body.splitlines()) <= dl.WRAP_COLUMNS
    assert classes(text)["kicad_default"]["nets"][:12] == names


# ----------------------------------------------------------------------------
# impedance nets: narrowing, widths, layer rules, pad layers
# ----------------------------------------------------------------------------
def test_impedance_net_moves_to_its_own_class_on_its_freerouting_layers() -> None:
    nl = {"USB_DP": imp("F.Cu", {"F.Cu": 0.30, "B.Cu": 0.301, "In2.Cu": 0.20},
                        [0.295, 0.305])}
    text, rep = dl.postprocess(fixture("quoted_wrapped"),
                               {**POLICY_4L, "plane_nets": {"GND": ["In1.Cu"]},
                                "signal_layers": ["F.Cu", "In2.Cu", "B.Cu"], "net_layers": nl},
                               layer_rule_verified=False)
    got = classes(text)["kicad_eda_net_USB_DP"]
    assert got["nets"] == ["USB_DP"] and got["use_layer"] == ["F.Cu", "B.Cu"]
    assert got["width"] == 300.0 and got["layer_rules"] == {}
    assert rep["routed_layers"] == {"USB_DP": ["F.Cu", "B.Cu"]}
    assert rep["excluded_layers"] == {"USB_DP": ["In2.Cu"]} and rep["layer_rules"] == {}
    assert rep["nets_moved"]["USB_DP"] == "kicad_eda_net_USB_DP"
    assert rep["pad_layer_excluded"] == {}                        # R3 (back) pad on B.Cu


def test_verified_layer_rule_adds_the_other_layers_with_their_widths() -> None:
    nl = {"USB_DP": imp("F.Cu", {"F.Cu": 0.30, "B.Cu": 0.30, "In2.Cu": 0.1955})}
    text, rep = dl.postprocess(fixture("quoted_wrapped"),
                               {**POLICY_4L, "plane_nets": {"GND": ["In1.Cu"]},
                                "signal_layers": ["F.Cu", "In2.Cu", "B.Cu"], "net_layers": nl},
                               layer_rule_verified=True)
    got = classes(text)["kicad_eda_net_USB_DP"]
    assert got["use_layer"] == ["F.Cu", "In2.Cu", "B.Cu"]
    assert got["layer_rules"] == {"In2.Cu": 195.5}
    assert rep["layer_rules"] == {"USB_DP": {"In2.Cu": 0.1955}} and rep["excluded_layers"] == {}
    assert rep["layer_rule_verified"] is True


def test_default_window_is_solved_width_plus_minus_max_5um_or_1_5_pct() -> None:
    entry = dl.load_policy({**POLICY_4L, "net_layers": {
        "A": imp("F.Cu", {"F.Cu": 0.2}), "B": imp("F.Cu", {"F.Cu": 1.0})}}).net_layers
    assert entry["A"].window == pytest.approx((0.195, 0.205))
    assert entry["B"].window == pytest.approx((0.985, 1.015))


def test_pad_layer_excluded_when_the_pad_layer_is_not_routed() -> None:
    nl = {"USB_DP": imp("F.Cu", {"F.Cu": 0.30, "B.Cu": 0.25})}      # B.Cu outside the window
    _text, rep = dl.postprocess(fixture("quoted_wrapped"), {**POLICY_4L, "net_layers": nl},
                                layer_rule_verified=False)
    assert rep["routed_layers"] == {"USB_DP": ["F.Cu"]}
    assert rep["pad_layer_excluded"] == {"USB_DP": ["B.Cu"]}
    _text, rep = dl.postprocess(fixture("quoted_wrapped"), {**POLICY_4L, "net_layers": nl},
                                layer_rule_verified=True)
    assert rep["pad_layer_excluded"] == {} and rep["layer_rules"] == {"USB_DP": {"B.Cu": 0.25}}


POLICY_F_ONLY = {**POLICY_4L, "signal_layers": ["F.Cu"], "unused_layers": ["B.Cu"]}


def test_pad_layer_excluded_is_for_impedance_nets_only() -> None:
    """ADR D7.9: pad_layer_excluded (autoroute refuses) names impedance nets;
    another routed net with an SMD pad outside its layers (R3 is on the back,
    B.Cu unused) is pad_layer_blocked plus a warning; a one-pin net is
    neither (nothing to route)."""
    _text, rep = dl.postprocess(fixture("quoted_wrapped"), POLICY_F_ONLY)
    assert rep["pad_layer_excluded"] == {}
    assert rep["pad_layer_blocked"] == {"USB_DP": ["B.Cu"]}       # "unconnected-(R3-Pad2)": 1 pin
    assert any("USB_DP" in w and "B.Cu" in w for w in rep["warnings"]), rep["warnings"]
    nl = {"USB_DP": imp("F.Cu", {"F.Cu": 0.30})}
    _text, rep = dl.postprocess(fixture("quoted_wrapped"), {**POLICY_F_ONLY, "net_layers": nl})
    assert rep["pad_layer_excluded"] == {"USB_DP": ["B.Cu"]} and rep["pad_layer_blocked"] == {}


def test_plane_net_pad_outside_its_class_is_blocked() -> None:
    raw = fixture("quoted_wrapped").replace("(place D1 110000 -85000 front", "(place D1 "
                                            "110000 -85000 back")
    _text, rep = dl.postprocess(raw, POLICY_F_ONLY)                # D1-1 (GND) on B.Cu
    assert rep["pad_layer_blocked"]["GND"] == ["B.Cu"] and "GND" not in rep["pad_layer_excluded"]
    _text, rep = dl.postprocess(raw, POLICY_F_ONLY, variant=dl.UNRESTRICTED)
    assert rep["pad_layer_blocked"]["GND"] == ["B.Cu"]             # B.Cu unused even so
    _text, rep = dl.postprocess(raw, {**POLICY_4L, "plane_classes": "unrestricted"})
    assert "GND" not in rep["pad_layer_blocked"]                   # no use_layer: every layer


def test_smd_pad_layers_mirror_back_side_parts_and_skip_through_holes() -> None:
    pads = dl.smd_pad_layers(fixture("quoted_wrapped"))
    assert pads["USB_DP"] == ("F.Cu", "B.Cu")                     # U1-6 (rotated pin), R3 back
    assert pads["+3V3"] == ("F.Cu",)                              # J1-1 is through-hole
    assert pads["unconnected-(R3-Pad2)"] == ("B.Cu",)
    assert "Net-(D1-A)" in pads and pads["#RESET"] == ("F.Cu",)


@pytest.mark.parametrize("token,want", [
    ("U1-6", ("U1", "6")), ('"R 1"-2', ("R 1", "2")), ('U1-"A-1"', ("U1", "A-1")),
    ('"#P1"-1', ("#P1", "1")), ("J1-A12", ("J1", "A12")), ('K1-7"', ("K1", '7"')),
])
def test_split_pin_ref(token: str, want: tuple[str, str]) -> None:
    assert dl.split_pin_ref(token, '"') == want


@pytest.mark.parametrize("token", ['"U1-6', 'U1-"6', '"R 1"-"2'])
def test_split_pin_ref_refuses_an_unterminated_quote(token: str) -> None:
    with pytest.raises(dl.DsnLayersError, match="pin reference"):
        dl.split_pin_ref(token, '"')


# ----------------------------------------------------------------------------
# single-sided and 2L mixed
# ----------------------------------------------------------------------------
def test_single_sided_routes_f_cu_only() -> None:
    text, rep = dl.postprocess(fixture("two_layer_mixed"), POLICY_1L)
    got = classes(text)
    assert all(c["use_layer"] == ["F.Cu"] for c in got.values()) and got
    assert rep["unused_layers"] == ["B.Cu"] and rep["nets_moved"] == {}


def test_two_layer_mixed_keeps_both_layers_and_the_outer_pour() -> None:
    raw = fixture("two_layer_mixed")
    text, rep = dl.postprocess(raw, POLICY_2L)
    assert classes(text)["kicad_default"]["use_layer"] == ["F.Cu", "B.Cu"]
    assert "GND" in classes(text)["kicad_default"]["nets"]
    assert section(text, "structure") == section(raw, "structure")


# ----------------------------------------------------------------------------
# derive_policy_from_dsn: (type power) or inner planes only, never outer pours
# ----------------------------------------------------------------------------
@pytest.mark.parametrize("name,signal,planes,unused", [
    ("cp2102n", ["F.Cu", "B.Cu"], {"GND": ["In1.Cu"], "+3V3": ["In2.Cu"]}, []),
    ("six_layer", ["F.Cu", "In2.Cu", "In3.Cu", "B.Cu"], {"GND": ["In1.Cu"], "+3V3": ["In4.Cu"]},
     []),
    ("two_layer_mixed", ["F.Cu", "B.Cu"], {}, []),
    ("quoted_wrapped", ["F.Cu", "B.Cu"], {"GND": ["In1.Cu"], "+3V3": ["In2.Cu"]}, []),
])
def test_derive_policy_from_dsn(name: str, signal: list, planes: dict, unused: list) -> None:
    pol = dl.derive_policy_from_dsn(fixture(name))
    assert pol["signal_layers"] == signal and pol["plane_nets"] == planes
    assert pol["unused_layers"] == unused and pol["net_layers"] == {}
    assert pol["source"] == "derived"
    text, rep = dl.postprocess(fixture(name), pol)
    assert rep["source"] == "derived" and classes(text)


def test_derived_power_layer_without_a_pour_is_unused() -> None:
    raw = fixture("six_layer")
    st = ds.child(ds.parse_nodes(raw)[0], "structure")
    plane = next(p for p in ds.find_all(st, "plane") if p.items[0].value == "+3V3")
    pol = dl.derive_policy_from_dsn(raw[:plane.start] + raw[plane.end:])
    assert pol["unused_layers"] == ["In4.Cu"] and pol["plane_nets"] == {"GND": ["In1.Cu"]}
    assert any("In4.Cu" in n for n in pol["notes"])


def test_outer_power_layer_is_a_plane_but_an_outer_pour_is_not() -> None:
    raw = fixture("quoted_wrapped")
    pol = dl.derive_policy_from_dsn(raw)
    assert "F.Cu" in pol["signal_layers"]                         # F.Cu carries a GND pour
    typed = raw.replace("(layer F.Cu\n      (type signal)", "(layer F.Cu\n      (type power)")
    assert dl.derive_policy_from_dsn(typed)["plane_nets"]["GND"] == ["F.Cu", "In1.Cu"]


# ----------------------------------------------------------------------------
# policy files and in-place rewrite
# ----------------------------------------------------------------------------
def test_load_policy_file_reads_an_artifact_or_a_bare_policy(tmp_path: Path) -> None:
    art = tmp_path / "netlist.stackup.json"
    art.write_text(json.dumps({"schema": "kicad-eda/physical@1", "dsn": POLICY_4L}),
                   encoding="utf-8")
    bare = tmp_path / "policy.json"
    bare.write_text(json.dumps({**POLICY_4L, "plane_classes": "unrestricted"}), encoding="utf-8")
    assert dl.load_policy_file(art) == POLICY_4L
    assert dl.load_policy_file(bare)["plane_classes"] == "unrestricted"
    for bad in ("[]", "{\"schema\": \"x\"}", "{not json"):
        (tmp_path / "bad.json").write_text(bad, encoding="utf-8")
        with pytest.raises(dl.DsnLayersError):
            dl.load_policy_file(tmp_path / "bad.json")
    with pytest.raises(dl.DsnLayersError, match="cannot read"):
        dl.load_policy_file(tmp_path / "missing.json")


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_postprocess_file_rewrites_in_place_keeping_newlines(tmp_path: Path,
                                                            newline: str) -> None:
    path = tmp_path / "board.dsn"
    raw = fixture("cp2102n", newline)
    path.write_bytes(raw.encode("utf-8"))
    rep = dl.postprocess_file(path, POLICY_4L)
    want, want_rep = dl.postprocess(raw, POLICY_4L)
    assert path.read_bytes() == want.encode("utf-8") and rep == want_rep
    assert [p.name for p in tmp_path.iterdir()] == ["board.dsn"]


# ----------------------------------------------------------------------------
# refusals
# ----------------------------------------------------------------------------
@pytest.mark.parametrize("policy,needle", [
    ({**POLICY_4L, "signal_layers": ["F.Cu", "B.Cu", "In3.Cu"]}, "In3.Cu"),
    ({**POLICY_4L, "plane_nets": {"GND": ["In1.Cu"]}}, "In2.Cu"),
    ({**POLICY_4L, "unused_layers": ["B.Cu"]}, "B.Cu"),
    ({**POLICY_4L, "net_layers": {"GND": imp("F.Cu", {"F.Cu": 0.2})}}, "GND"),
    ({**POLICY_4L, "net_layers": {"TXD": imp("In1.Cu", {"In1.Cu": 0.2})}}, "In1.Cu"),
    ({**POLICY_4L, "net_layers": {"TXD": imp("F.Cu", {"F.Cu": 0.2}, [0.25, 0.3])}}, "window"),
    ({**POLICY_4L, "net_layers": {"TXD": imp("F.Cu", {"B.Cu": 0.2})}}, "primary"),
    ({**POLICY_4L, "net_layers": {"TXD": imp("F.Cu", {"F.Cu": -1})}}, "width"),
    ({**POLICY_4L, "plane_classes": "sometimes"}, "plane_classes"),
    ({**POLICY_4L, "signal_layers": []}, "signal"),
    ({"plane_nets": {}}, "signal_layers"),
])
def test_bad_policies_are_refused(policy: dict, needle: str) -> None:
    with pytest.raises(dl.DsnLayersError, match=None) as err:
        dl.postprocess(fixture("cp2102n"), policy)
    assert needle in str(err.value)


def test_a_policy_must_be_a_mapping() -> None:
    with pytest.raises(dl.DsnLayersError, match="mapping"):
        dl.postprocess(fixture("cp2102n"), ["F.Cu"])


def test_unknown_policy_keys_are_reported_not_fatal() -> None:
    _text, rep = dl.postprocess(fixture("cp2102n"), {**POLICY_4L, "mixed_layers": []})
    assert any("mixed_layers" in w for w in rep["warnings"])


def test_policy_nets_absent_from_the_dsn_are_warnings() -> None:
    pol = {**POLICY_4L, "net_layers": {"NOPE": imp("F.Cu", {"F.Cu": 0.2})},
           "plane_nets": {**POLICY_4L["plane_nets"], "VBAT": ["In2.Cu"]}}
    _text, rep = dl.postprocess(fixture("cp2102n"), pol)
    assert any("NOPE" in w for w in rep["warnings"]) and any("VBAT" in w for w in rep["warnings"])


def test_an_already_processed_dsn_is_refused() -> None:
    once, _rep = dl.postprocess(fixture("cp2102n"), POLICY_4L)
    with pytest.raises(dl.DsnLayersError, match="already"):
        dl.postprocess(once, POLICY_4L)


def test_a_net_in_no_class_is_refused() -> None:
    raw = fixture("cp2102n").replace("(class kicad_default +3V3 CC1", "(class kicad_default CC1")
    with pytest.raises(dl.DsnLayersError, match=r"\+3V3"):
        dl.postprocess(raw, POLICY_4L)


@pytest.mark.parametrize("text,needle", [
    ("(pcb x (structure (layer F.Cu (type signal))))", "network"),
    ("(pcb x (network (class c A)))", "structure"),
    ("(session x)", "pcb"),
    ("(pcb x (structure (boundary", "DsnError"),
])
def test_malformed_dsn_is_refused(text: str, needle: str) -> None:
    with pytest.raises(dl.DsnLayersError, match=needle):
        dl.postprocess(text, POLICY_2L)


# ----------------------------------------------------------------------------
# the one-time layer_rule check and the source contract
# ----------------------------------------------------------------------------
def test_layer_rule_verdict_is_backed_by_the_recorded_run() -> None:
    evidence = json.loads((FIX / "layer_rule_evidence.json").read_text(encoding="utf-8"))
    assert isinstance(dl.LAYER_RULE_VERIFIED, bool)
    assert evidence["verified"] is dl.LAYER_RULE_VERIFIED
    assert evidence["freerouting"] == "1.9.0" and evidence["fixture"] == "six_layer.dsn.txt"
    assert "layer_rule_evidence.json" in dl.LAYER_RULE_EVIDENCE
    observed = evidence["observed_um"]                 # {layer: {width: wire count}}
    assert set(observed) >= {"F.Cu", "In2.Cu"}
    want = evidence["expected_um"]
    honoured = all(set(observed[layer]) == {str(w)} for layer, w in want.items())
    assert honoured is dl.LAYER_RULE_VERIFIED
    _text, rep = dl.postprocess(fixture("cp2102n"), POLICY_4L)
    assert rep["layer_rule_verified"] is dl.LAYER_RULE_VERIFIED
    assert rep["layer_rule_evidence"] == dl.LAYER_RULE_EVIDENCE


def test_module_is_stdlib_py311_small_and_regex_free() -> None:
    source = MODULE.read_text(encoding="utf-8")
    tree = ast.parse(source, feature_version=(3, 11))
    assert len(source.splitlines()) < 800
    mods = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import)
            for a in n.names}
    mods |= {n.module.split(".")[0] for n in ast.walk(tree)
             if isinstance(n, ast.ImportFrom) and n.module}
    assert mods <= {"__future__", "dsn_sexpr", "json", "math", "os", "tempfile", "collections",
                    "dataclasses", "pathlib", "typing"}, mods
    for fn in (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)):
        assert fn.end_lineno - fn.lineno < 50, fn.name
        assert fn.returns is not None, fn.name
        args = [*fn.args.posonlyargs, *fn.args.args, *fn.args.kwonlyargs]
        assert all(a.annotation is not None for a in args if a.arg != "self"), fn.name
