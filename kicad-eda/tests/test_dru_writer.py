"""dru_writer: the managed <board stem>.kicad_dru with the ADR D7.8 2a families
(R8 plane-role layers, R6 impedance width windows, R7 allowed layers).

Unit tier (no KiCad): Rule rendering; base_rules from a ResolvedStackup or its
artifact dict plus the impedance lines (both nets of a pair); render's guards
(every function call A./B.-qualified -- critique E1 --, quote / backslash /
wildcard net names, no 'disallow zone', no 'deg', no layer clause on an
assertion, known layer names, balanced text, unique names); byte snapshots
for the JLC 4L preset with a USB pair and the 6L JLC06161H-2116 fixture;
merge keeps user rules byte-identical outside the markers; dru_path_for /
write follow the BOARD stem (critique M13).

Snapshots: tests/fixtures/dru/<case>.json (design + trimmed line records) ->
<case>.kicad_dru. Set KICAD_EDA_UPDATE_DRU_SNAPSHOTS=1 to rewrite them after
a deliberate format change (review the diff).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

import pytest

import drc_guard as dg
import dru_writer as dw
import impedance
import stackup_model as sm

FIX = Path(__file__).resolve().parent / "fixtures" / "dru"
SNAPSHOTS = ("jlc4l_usb", "six_layer")
UPDATE = os.environ.get("KICAD_EDA_UPDATE_DRU_SNAPSHOTS") == "1"
NETS = {"GND": [["J1", "1"]], "+3V3": [["J1", "2"]]}


def _case(name: str) -> dict:
    return json.loads((FIX / f"{name}.json").read_text(encoding="utf-8"))


def _rs(design: dict) -> sm.ResolvedStackup:
    rs, issues = sm.resolve(design)
    assert rs is not None, [str(i) for i in issues]
    return rs


def _rules_of(name: str) -> list[dw.Rule]:
    case = _case(name)
    return dw.base_rules(_rs(case["design"]), case["lines"], pairs=dw.pairs_of(case["design"]))


def _design(**stackup: Any) -> dict:
    st = {"preset": "jlcpcb:JLC04161H-7628", "rationale": "test", **stackup}
    return {"name": "t", "components": [], "nets": NETS, "stackup": st}


def _line(width: float, lo: float, hi: float, *layers: str, kind: str = "single") -> dict:
    return {"kind": kind, "layers": {la: {"width": width, "width_window": [lo, hi]}
                                     for la in layers}}


def _by_name(rules: list[dw.Rule]) -> dict[str, dw.Rule]:
    return {r.name: r for r in rules}


# ---------------------------------------------------------------- Rule
def test_a_rule_renders_its_clauses_on_separate_lines() -> None:
    rule = dw.Rule("n1", "In1.Cu", "A.NetName == 'X'", "disallow track")
    assert rule.render() == ('(rule "n1"\n  (layer "In1.Cu")\n'
                             "  (condition \"A.NetName == 'X'\")\n  (constraint disallow track))")


def test_a_rule_without_layer_or_condition_omits_the_clause() -> None:
    assert dw.Rule("n2", None, None, "track_angle (min 135)").render() == (
        '(rule "n2"\n  (constraint track_angle (min 135)))')


def test_rule_is_frozen() -> None:
    rule = dw.Rule("n", None, None, "disallow track")
    with pytest.raises(AttributeError):
        rule.name = "m"  # type: ignore[misc]


# ---------------------------------------------------------------- base_rules
def test_r8_one_disallow_per_plane_layer_with_the_adr_condition() -> None:
    rules = dw.base_rules(_rs(_design()), {})
    assert [r.render() for r in rules] == [
        '(rule "kicad_eda_plane_In1.Cu"\n  (layer "In1.Cu")\n'
        "  (condition \"A.Type == 'Track' && A.NetName != 'GND'\")\n"
        "  (constraint disallow track))",
        '(rule "kicad_eda_plane_In2.Cu"\n  (layer "In2.Cu")\n'
        "  (condition \"A.Type == 'Track' && A.NetName != '+3V3'\")\n"
        "  (constraint disallow track))"]


def test_r6_window_and_r7_other_routable_layers() -> None:
    lines = {"RF": _line(0.36, 0.3546, 0.3654, "F.Cu")}
    rules = _by_name(dw.base_rules(_rs(_design()), lines))
    width = rules["kicad_eda_width_RF_F.Cu"]
    assert (width.layer, width.condition) == ("F.Cu", "A.NetName == 'RF'")
    assert width.constraint == "track_width (min 0.3546mm) (opt 0.36mm) (max 0.3654mm)"
    off = rules["kicad_eda_offlayer_RF_B.Cu"]
    assert (off.layer, off.condition, off.constraint) == ("B.Cu", "A.NetName == 'RF'",
                                                          "disallow track")
    # plane layers are R8's; no R7 there, no R6 off the allowed layers
    assert sorted(rules) == ["kicad_eda_offlayer_RF_B.Cu", "kicad_eda_plane_In1.Cu",
                             "kicad_eda_plane_In2.Cu", "kicad_eda_width_RF_F.Cu"]


def test_family_of_names_the_adr_rule() -> None:
    families = [dw.family_of(r) for r in _rules_of("jlc4l_usb")]
    assert families == ["R8", "R8", "R7", "R7", "R6", "R6"]
    assert dw.family_of(dw.Rule("rf_len_X", None, None, "length (max 30mm)")) is None


def test_families_are_ordered_r8_r7_r6_and_by_net_then_stackup_layer() -> None:
    lines = {"ZZ": _line(0.2, 0.195, 0.205, "B.Cu", "F.Cu"),
             "AA": _line(0.3, 0.295, 0.305, "F.Cu")}
    names = [r.name for r in dw.base_rules(_rs(_design()), lines)]
    assert names == ["kicad_eda_plane_In1.Cu", "kicad_eda_plane_In2.Cu",
                     "kicad_eda_offlayer_AA_B.Cu",
                     "kicad_eda_width_AA_F.Cu", "kicad_eda_width_ZZ_F.Cu",
                     "kicad_eda_width_ZZ_B.Cu"]


def test_the_lines_dict_order_does_not_change_the_rules() -> None:
    case = _case("six_layer")
    rs, lines = _rs(case["design"]), case["lines"]
    reversed_lines = dict(reversed(list(lines.items())))
    assert dw.base_rules(rs, lines) == dw.base_rules(rs, reversed_lines)


def test_the_artifact_stackup_dict_gives_the_same_rules() -> None:
    case = _case("six_layer")
    rs = _rs(case["design"])
    as_dict = json.loads(json.dumps(rs.to_dict()))
    assert dw.base_rules(as_dict, case["lines"]) == dw.base_rules(rs, case["lines"])


def test_a_mixed_layer_gets_no_plane_rule_but_is_routable() -> None:
    design = _design(roles={"In2.Cu": {"role": "mixed", "net": "+3V3"}})
    rules = _by_name(dw.base_rules(_rs(design), {"RF": _line(0.36, 0.355, 0.365, "F.Cu")}))
    assert "kicad_eda_plane_In2.Cu" not in rules and "kicad_eda_plane_In1.Cu" in rules
    assert {"kicad_eda_offlayer_RF_In2.Cu", "kicad_eda_offlayer_RF_B.Cu"} <= set(rules)


def test_single_sided_unused_b_cu_is_not_a_routable_layer() -> None:
    design = {"name": "t", "components": [], "nets": NETS, "copper_layers": 1}
    rules = dw.base_rules(_rs(design), {"RF": _line(1.0, 0.985, 1.015, "F.Cu")})
    assert [r.name for r in rules] == ["kicad_eda_width_RF_F.Cu"]


def test_a_line_that_could_not_be_resolved_gets_no_rules() -> None:
    lines = {"BAD": {"kind": "single", "status": "error", "layers": {}, "width_window": None},
             "NOLAYERS": {"kind": "single"}}
    names = [r.name for r in dw.base_rules(_rs(_design()), lines)]
    assert names == ["kicad_eda_plane_In1.Cu", "kicad_eda_plane_In2.Cu"]


def test_full_facade_records_are_accepted() -> None:
    design = {**_design(), "net_specs": {"RF": {"impedance": 50, "layers": ["F.Cu"]},
                                         "USB_DP": {"diff": 90, "diff_pair": "USB_DM"}}}
    lines = impedance.resolve_impedance(design)
    rules = _by_name(dw.base_rules(_rs(design), lines, pairs=dw.pairs_of(design)))
    for net, partner in (("RF", None), ("USB_DP", "USB_DM")):
        for layer, rec in lines[net]["layers"].items():
            lo, hi = rec["width_window"]
            for n in filter(None, (net, partner)):
                want = (f"track_width (min {dw.mm(lo)}) (opt {dw.mm(rec['width'])}) "
                        f"(max {dw.mm(hi)})")
                assert rules[f"kicad_eda_width_{n}_{layer}"].constraint == want
    assert "kicad_eda_offlayer_RF_B.Cu" in rules


# ---------------------------------------------------------------- pairs
def test_both_nets_of_a_pair_get_the_rules_via_pairs_of() -> None:
    case = _case("jlc4l_usb")
    rules = _by_name(_rules_of("jlc4l_usb"))
    assert dw.pairs_of(case["design"]) == {"USB_DP": "USB_DM", "USB_DM": "USB_DP"}
    for net in ("USB_DP", "USB_DM"):
        assert f"kicad_eda_width_{net}_F.Cu" in rules
        assert f"kicad_eda_offlayer_{net}_B.Cu" in rules


def test_a_partner_named_in_the_record_gets_the_rules() -> None:
    lines = {"P": {**_line(0.2, 0.195, 0.205, "F.Cu", kind="diff"), "diff_pair": "N"}}
    rules = _by_name(dw.base_rules(_rs(_design()), lines))
    assert rules["kicad_eda_width_N_F.Cu"].condition == "A.NetName == 'N'"


def test_a_partner_with_its_own_record_keeps_it() -> None:
    lines = {"P": _line(0.2, 0.195, 0.205, "F.Cu", kind="diff"),
             "N": _line(0.3, 0.295, 0.305, "B.Cu", kind="diff")}
    rules = _by_name(dw.base_rules(_rs(_design()), lines, pairs={"P": "N", "N": "P"}))
    assert "kicad_eda_width_N_F.Cu" not in rules and "kicad_eda_width_N_B.Cu" in rules
    assert "kicad_eda_offlayer_N_F.Cu" in rules


def test_a_single_ended_record_does_not_extend_to_a_partner() -> None:
    lines = {"P": _line(0.2, 0.195, 0.205, "F.Cu")}
    names = [r.name for r in dw.base_rules(_rs(_design()), lines, pairs={"P": "N"})]
    assert not any("_N_" in n for n in names)


def test_pairs_of_reads_net_specs_both_ways_and_ignores_junk() -> None:
    design = {"net_specs": {"A": {"diff_pair": "B"}, "C": {"diff": 90}, "D": "junk",
                            "E": {"diff_pair": 5}, "F": {"diff_pair": "F"}}}
    assert dw.pairs_of(design) == {"A": "B", "B": "A"}
    assert dw.pairs_of({}) == {}


# ---------------------------------------------------------------- validation
@pytest.mark.parametrize("net", ["X'Y", "X\\Y", 'X"Y', "USB*", "D?", "A\nB", ""])
def test_unsafe_impedance_net_names_are_refused(net: str) -> None:
    with pytest.raises(ValueError, match="net name"):
        dw.base_rules(_rs(_design()), {net: _line(0.2, 0.195, 0.205, "F.Cu")})


def test_an_unsafe_plane_net_name_is_refused() -> None:
    design = {**_design(), "nets": {**NETS, "V'X": [["J1", "3"]]},
              "stackup": {"preset": "jlcpcb:JLC04161H-7628", "rationale": "t",
                          "roles": {"In2.Cu": {"role": "plane", "net": "V'X"}}}}
    with pytest.raises(ValueError, match="V'X"):
        dw.base_rules(_rs(design), {})


@pytest.mark.parametrize("record, match", [
    ({"layers": {"F.Cu": {"width": 0.2}}}, "width_window"),
    ({"layers": {"F.Cu": {"width": 0.2, "width_window": [0.21, 0.19]}}}, "window"),
    ({"layers": {"F.Cu": {"width": 0.3, "width_window": [0.19, 0.21]}}}, "outside"),
    ({"layers": {"F.Cu": {"width": "x", "width_window": [0.19, 0.21]}}}, "width"),
    ({"layers": {"F.Cu": {"width": 0.2, "width_window": [0, 0.21]}}}, "window"),
    ({"layers": {"F.Cu": {"width": 0.2, "width_window": [0.19, float("nan")]}}}, "window"),
    ({"layers": {"In5.Cu": {"width": 0.2, "width_window": [0.19, 0.21]}}}, "In5.Cu"),
    ({"layers": ["F.Cu"]}, "layers"),
])
def test_malformed_line_records_are_refused(record: dict, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        dw.base_rules(_rs(_design()), {"RF": record})


@pytest.mark.parametrize("stackup, match", [
    (None, "no copper"), ({"copper": []}, "no copper"),
    ({"copper": [{"name": "F.Cu"}]}, "name and a role"),
    ({"copper": [{"name": "F.Cu", "role": "signal"}, {"name": "In1.Cu", "role": "plane",
                                                       "net": None}]}, "In1.Cu has no net")])
def test_malformed_stackups_are_refused(stackup: Any, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        dw.base_rules(stackup, {})


@pytest.mark.parametrize("lines, match", [
    (["RF"], "lines must be"), ({"RF": 5}, "lines.RF must be"),
    ({"RF": {"layers": {"F.Cu": 0.2}}}, "lines.RF.layers.F.Cu must be")])
def test_malformed_lines_are_refused(lines: Any, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        dw.base_rules(_rs(_design()), lines)


def test_base_rules_never_mutates_its_inputs() -> None:
    case = _case("six_layer")
    before = json.dumps(case, sort_keys=True)
    dw.base_rules(_rs(case["design"]), case["lines"], pairs=dw.pairs_of(case["design"]))
    assert json.dumps(case, sort_keys=True) == before


# ---------------------------------------------------------------- render guards
@pytest.mark.parametrize("condition", [
    "intersectsArea('X')", "A.NetName == 'X' && hasNetclass('Y')", "C.intersectsArea('X')",
    "A.B.intersectsArea('X')", "!insideCourtyard('U1')", "A.Type == 'Track' && isPlated ()"])
def test_render_refuses_unqualified_calls(condition: str) -> None:
    with pytest.raises(ValueError, match="qualified"):
        dw.render([dw.Rule("r", None, condition, "disallow track")])


@pytest.mark.parametrize("condition", [
    "A.intersectsArea('X')", "B.intersectsArea('X') && A.NetName != 'GND'",
    "A.NetName == 'Net-(D1-A)'", "A.NetName == 'F(X)' && A.Type == 'Track'",
    "(A.NetName == 'X' || A.NetName == 'Y') && !A.isPlated()", "A.hasNetclass ('NC')"])
def test_render_accepts_qualified_calls_and_parentheses_in_strings(condition: str) -> None:
    assert condition in dw.render([dw.Rule("r", None, condition, "disallow track")])


def test_render_checks_assertion_expressions_too() -> None:
    with pytest.raises(ValueError, match="qualified"):
        dw.render([dw.Rule("r", None, "A.NetName == 'X'", 'assertion "isPlated()"')])
    ok = dw.Rule("r", None, "A.NetName == 'X' && A.Layer != 'F.Cu'", 'assertion "A.Width < 0mm"')
    assert 'assertion "A.Width < 0mm"' in dw.render([ok])


@pytest.mark.parametrize("rule, match", [
    (dw.Rule("r", None, "A.NetName == 'X'", "disallow zone"), "zone"),
    (dw.Rule("r", None, "A.NetName == 'X'", "disallow track via zone"), "zone"),
    (dw.Rule("r", None, "A.NetName == 'X'", "track_angle (min 135deg)"), "deg"),
    (dw.Rule("r", "F.Cu", "A.NetName == 'X'", 'assertion "A.Width < 0mm"'), "assertion"),
    (dw.Rule("r", "Bogus.Cu", "A.NetName == 'X'", "disallow track"), "Bogus.Cu"),
    (dw.Rule("r", "In31.Cu", None, "disallow track"), "In31.Cu"),
    (dw.Rule("r", "In0.Cu", None, "disallow track"), "In0.Cu"),
    (dw.Rule('r"x', None, None, "disallow track"), "rule name"),
    (dw.Rule("", None, None, "disallow track"), "rule name"),
    (dw.Rule("r", None, 'A.NetName == "X"', "disallow track"), "condition"),
    (dw.Rule("r", None, "A.NetName == 'X\\Y'", "disallow track"), "condition"),
    (dw.Rule("r", None, "A.NetName == 'X", "disallow track"), "quote"),
    (dw.Rule("r", None, "(A.NetName == 'X'", "disallow track"), "parenthes"),
    (dw.Rule("r", None, "A.NetName == 'X')(", "disallow track"), "parenthes"),
    (dw.Rule("r", None, None, "track_width (min 1mm"), "parenthes"),
    (dw.Rule("r", None, None, 'assertion "A.Width < 0mm'), "double quote"),
    (dw.Rule("r", None, None, ""), "constraint"),
    (dw.Rule("r", None, None, 'assertion "A.Width <\n 0mm"'), "constraint"),
])
def test_render_refuses_rules_kicad_would_drop_or_misread(rule: dw.Rule, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        dw.render([rule])


@pytest.mark.parametrize("layer", ["F.Cu", "B.Cu", "In1.Cu", "In30.Cu", "outer", "inner"])
def test_render_accepts_copper_layer_names(layer: str) -> None:
    assert f'(layer "{layer}")' in dw.render([dw.Rule("r", layer, None, "disallow track")])


@pytest.mark.parametrize("layer", ["F.Courtyard", "Edge.Cuts", "f.cu", "F.Cu ", ""])
def test_render_refuses_layers_outside_the_verified_copper_set(layer: str) -> None:
    with pytest.raises(ValueError, match="layer"):
        dw.render([dw.Rule("r", layer, None, "disallow track")])


def test_render_refuses_anything_but_rules() -> None:
    with pytest.raises(ValueError, match="not a dru_writer.Rule"):
        dw.render([("r", None, None, "disallow track")])  # type: ignore[list-item]


def test_render_refuses_duplicate_names_and_the_canary_name() -> None:
    rule = dw.Rule("r", None, None, "disallow track")
    with pytest.raises(ValueError, match="duplicate"):
        dw.render([rule, rule])
    with pytest.raises(ValueError, match="duplicate"):
        dw.render([dw.Rule("__kicad_eda_canary", None, None, "disallow track")])


def test_render_layout_header_markers_and_canary_first() -> None:
    text = dw.render([dw.Rule("r", None, "A.NetName == 'X'", "disallow track")])
    lines = text.splitlines()
    assert lines[0] == "(version 1)" and lines[1] == dw.BEGIN_MARKER
    first_rule = next(ln for ln in lines if ln.startswith("(rule"))
    assert first_rule == dg.CANARY_RULE
    assert lines[-1] == dw.END_MARKER and text.endswith("\n")
    assert text.count("(version 1)") == 1
    assert dg.with_canary(text) == (text, False)      # rules_compile adds nothing


def test_render_of_no_rules_is_the_header_and_the_canary() -> None:
    text = dw.render([])
    assert [ln for ln in text.splitlines() if not ln.startswith("#")] == ["(version 1)",
                                                                           dg.CANARY_RULE]


def test_mm_has_no_exponent_and_no_trailing_zeros() -> None:
    assert [dw.mm(x) for x in (0.13, 0.3546, 0.349675, 1e-05, 10, 0.1 + 0.2)] == [
        "0.13mm", "0.3546mm", "0.349675mm", "0.00001mm", "10mm", "0.3mm"]


# ---------------------------------------------------------------- snapshots
@pytest.mark.parametrize("name", SNAPSHOTS)
def test_snapshot(name: str) -> None:
    text = dw.render(_rules_of(name))
    path = FIX / f"{name}.kicad_dru"
    if UPDATE:
        path.write_bytes(text.encode("utf-8"))
    assert text == path.read_bytes().decode("utf-8")


def test_snapshots_hold_every_family_and_no_forbidden_text() -> None:
    for name in SNAPSHOTS:
        text = (FIX / f"{name}.kicad_dru").read_text(encoding="utf-8")
        for prefix in ("kicad_eda_plane_", "kicad_eda_offlayer_", "kicad_eda_width_"):
            assert f'(rule "{prefix}' in text, (name, prefix)
        assert "disallow zone" not in text and not re.search(r"\d\s*deg", text)


# ---------------------------------------------------------------- merge
BLOCK = dw.render_block([dw.Rule("r", None, "A.NetName == 'X'", "disallow track")])
USER = ("(version 1)\n# my rules\n(rule \"mine\" (condition \"A.NetName == 'Y'\")\n"
        "  (constraint track_width (min 1mm)))\n")


def test_render_is_the_header_plus_the_block() -> None:
    rules = [dw.Rule("r", None, "A.NetName == 'X'", "disallow track")]
    assert dw.render(rules) == "(version 1)\n" + dw.render_block(rules)
    assert BLOCK.startswith(dw.BEGIN_MARKER + "\n") and BLOCK.endswith(dw.END_MARKER + "\n")


@pytest.mark.parametrize("existing", [None, "", "  \n"])
def test_merge_into_nothing_is_the_rendered_file(existing: str | None) -> None:
    assert dw.merge(existing, BLOCK) == "(version 1)\n" + BLOCK


def test_merge_appends_the_block_after_user_rules_without_markers() -> None:
    merged = dw.merge(USER, BLOCK)
    assert merged == USER + BLOCK


def test_merge_adds_a_missing_newline_before_the_block() -> None:
    assert dw.merge(USER.rstrip("\n"), BLOCK) == USER + BLOCK


def test_merge_replaces_only_the_managed_block() -> None:
    before, after = USER, "# below\n(rule \"late\" (constraint disallow via))\n"
    old = dw.render_block([dw.Rule("old", "F.Cu", "A.NetName == 'Q'", "disallow track")])
    merged = dw.merge(before + old + after, BLOCK)
    assert merged == before + BLOCK + after
    assert dw.merge(merged, BLOCK) == merged                    # idempotent


def test_merge_keeps_crlf_user_text_byte_identical() -> None:
    crlf = USER.replace("\n", "\r\n")
    old = dw.render_block([]).replace("\n", "\r\n")
    tail = "# tail\r\n"
    assert dw.merge(crlf + old + tail, BLOCK) == crlf + BLOCK + tail


def test_merge_accepts_a_begin_marker_with_trailing_text() -> None:
    old = "# kicad-eda managed begin (verified 2026-10-04)\n(rule \"x\" (constraint " \
          "disallow via))\n# kicad-eda managed end\n"
    assert dw.merge(USER + old, BLOCK) == USER + BLOCK


def test_merge_prepends_a_missing_version_header() -> None:
    user = "# no header\n(rule \"mine\" (constraint disallow via))\n"
    assert dw.merge(user, BLOCK) == "(version 1)\n" + user + BLOCK
    assert dw.merge("﻿(version 1)\n" + user, BLOCK) == "﻿(version 1)\n" + user + BLOCK


@pytest.mark.parametrize("existing", [
    USER + dw.BEGIN_MARKER + "\n",
    USER + dw.END_MARKER + "\n",
    USER + BLOCK + BLOCK,
    USER + dw.END_MARKER + "\n" + dw.BEGIN_MARKER + "\n"])
def test_merge_refuses_broken_markers(existing: str) -> None:
    with pytest.raises(ValueError, match="marker"):
        dw.merge(existing, BLOCK)


@pytest.mark.parametrize("block", ["(rule \"x\" (constraint disallow via))\n",
                                   dw.BEGIN_MARKER + "\n", BLOCK + BLOCK])
def test_merge_refuses_a_block_that_is_not_one_managed_block(block: str) -> None:
    with pytest.raises(ValueError, match="block"):
        dw.merge(USER, block)


# ---------------------------------------------------------------- paths and write
@pytest.mark.parametrize("board, expected", [
    ("out/main.kicad_pcb", "out/main.kicad_dru"),
    ("x.k9.kicad_pcb", "x.k9.kicad_dru")])
def test_dru_path_for_is_the_board_stem(board: str, expected: str) -> None:
    assert dw.dru_path_for(board) == Path(expected)


@pytest.mark.parametrize("bad", ["netlist.json", "board.kicad_pro", "board"])
def test_dru_path_for_refuses_anything_but_a_board(bad: str) -> None:
    with pytest.raises(ValueError, match="kicad_pcb"):
        dw.dru_path_for(bad)


def test_write_creates_the_file_at_the_board_stem(tmp_path: Path) -> None:
    (tmp_path / "netlist.json").write_text("{}", encoding="utf-8")
    board = tmp_path / "out" / "main.kicad_pcb"               # stems differ; dir is new
    rules = _rules_of("jlc4l_usb")
    res = dw.write(board, rules)
    path = tmp_path / "out" / "main.kicad_dru"
    data = path.read_bytes()
    assert res == {"path": str(path), "sha256": hashlib.sha256(data).hexdigest(),
                   "rules": len(rules) + 1}
    assert data.decode("utf-8") == dw.render(rules)
    assert not (tmp_path / "netlist.kicad_dru").exists()
    assert sorted(p.name for p in path.parent.iterdir()) == ["main.kicad_dru"]   # no temp left


def test_write_keeps_user_rules_byte_identical(tmp_path: Path) -> None:
    board = tmp_path / "b.kicad_pcb"
    path = dw.dru_path_for(board)
    tail = "# after\r\n(rule \"late\" (constraint disallow via))\r\n"
    path.write_bytes((USER + dw.render_block([]) + tail).encode("utf-8"))
    rules = _rules_of("six_layer")
    res = dw.write(board, rules)
    text = path.read_bytes().decode("utf-8")
    assert text == USER + dw.render_block(rules) + tail
    assert res["sha256"] == hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_write_twice_is_byte_identical_and_does_not_touch_the_file(tmp_path: Path) -> None:
    board = tmp_path / "b.kicad_pcb"
    rules = _rules_of("six_layer")
    first = dw.write(board, rules)
    path = Path(first["path"])
    os.utime(path, ns=(1_000_000_000, 1_000_000_000))
    assert dw.write(board, rules) == first
    assert path.stat().st_mtime_ns == 1_000_000_000


def test_write_refuses_bad_rules_and_leaves_the_file_alone(tmp_path: Path) -> None:
    board = tmp_path / "b.kicad_pcb"
    dw.dru_path_for(board).write_bytes(USER.encode("utf-8"))
    with pytest.raises(ValueError, match="qualified"):
        dw.write(board, [dw.Rule("r", None, "intersectsArea('X')", "disallow track")])
    assert dw.dru_path_for(board).read_bytes() == USER.encode("utf-8")


def test_a_failed_replace_keeps_the_old_file_and_leaves_no_temp(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    board = tmp_path / "b.kicad_pcb"
    dw.dru_path_for(board).write_bytes(USER.encode("utf-8"))

    def refuse(src: str, dst: str) -> None:
        raise PermissionError(f"locked: {dst}")

    monkeypatch.setattr(dw.os, "replace", refuse)
    with pytest.raises(PermissionError, match="locked"):
        dw.write(board, _rules_of("jlc4l_usb"))
    assert sorted(p.name for p in tmp_path.iterdir()) == ["b.kicad_dru"]
    assert dw.dru_path_for(board).read_bytes() == USER.encode("utf-8")


def test_write_refuses_a_file_that_is_not_utf8(tmp_path: Path) -> None:
    board = tmp_path / "b.kicad_pcb"
    raw = b"(version 1)\n# caf\xe9\n"
    dw.dru_path_for(board).write_bytes(raw)
    with pytest.raises(ValueError, match="UTF-8"):
        dw.write(board, [])
    assert dw.dru_path_for(board).read_bytes() == raw
