"""layer_role_checks (ADR D7.8 R8 / R27 / R28; task D7-18a): the pure G4 layer-role checks.

Plain dicts in, plain dicts out ({issues, warnings, metrics}); no pcbnew, no kipy. The board
dict is what swig_layer_role_checks extracts (tracks / vias / pads / zones in mm), the roles
come from the resolved stackup and the lines are the artifact's `lines` shape (or
line_layers(stackup, design), which builds the same shape from tline.geometry).
"""
from __future__ import annotations

import ast
import copy
import json
import math
from collections.abc import Sequence
from pathlib import Path

import pytest

import layer_role_checks as lrc
import stackup_model as sm

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "server" / "tools" / "layer_role_checks.py"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "boards" / "role_checks"

ROLES_4L = {"F.Cu": {"role": "signal", "net": None}, "In1.Cu": {"role": "plane", "net": "GND"},
            "In2.Cu": {"role": "plane", "net": "+3V3"}, "B.Cu": {"role": "signal", "net": None}}


def _design(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def _rs(design: dict) -> sm.ResolvedStackup:
    rs, issues = sm.resolve(design)
    assert rs is not None, issues
    return rs


def _track(net: str, layer: str, start: tuple, end: tuple, width: float = 0.2,
           **extra: object) -> dict:
    return {"net": net, "layer": layer, "start": list(start), "end": list(end), "width": width,
            **extra}


def _via(net: str, at: tuple, diameter: float = 0.6) -> dict:
    return {"net": net, "at": list(at), "layers": ["F.Cu", "B.Cu"], "diameter": diameter}


def _pad(ref: str, pad: str, net: str, at: tuple) -> dict:
    return {"ref": ref, "pad": pad, "net": net, "at": list(at), "layers": ["F.Cu"]}


def _board(tracks: Sequence[dict] = (), vias: Sequence[dict] = (),
           pads: Sequence[dict] = (), zones: Sequence[dict] = ()) -> dict:
    return {"tracks": list(tracks), "vias": list(vias), "pads": list(pads), "zones": list(zones)}


def _line(nets: list[str], dielectric: list[str] | None = None, line: str = "microstrip") -> dict:
    return {"line": line, "ref": {"below": [], "above": [], "nets": nets},
            "dielectric_layers": dielectric or []}


LINES_4L = {"Z50": {"layers": {"F.Cu": _line(["GND"]), "B.Cu": _line(["+3V3"])}}}


# ---------------------------------------------------------------- purity
def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    return names


def test_the_module_imports_neither_pcbnew_nor_kipy() -> None:
    imported = _imported_modules(MODULE)
    assert not imported & {"pcbnew", "kipy", "wx", "kicad_quiet"}, imported


# ---------------------------------------------------------------- R8 plane layers
def _plane_board(foreign_layer: str) -> dict:
    return _board(tracks=[
        _track("SIG_A", "F.Cu", (110, 110), (130, 110)),
        _track("SIG_A", foreign_layer, (130, 110), (130, 120)),
        _track("GND", "In1.Cu", (112, 125), (128, 125), 0.5),
        _track("+3V3", "In2.Cu", (112, 127), (128, 127), 0.5)])


def test_one_foreign_track_on_the_gnd_plane_is_one_blocker() -> None:
    res = lrc.check_plane_layers(_plane_board("In1.Cu"), ROLES_4L)
    assert res["metrics"]["foreign_tracks"] == 1
    assert res["metrics"]["foreign_zones"] == 0
    assert res["metrics"]["by_layer"]["In1.Cu"]["nets"] == {"SIG_A": 1}
    assert res["metrics"]["by_layer"]["In1.Cu"]["foreign_length_mm"] == pytest.approx(10.0)
    assert res["metrics"]["plane_layers"] == {"In1.Cu": "GND", "In2.Cu": "+3V3"}
    assert len(res["issues"]) == 1 and res["warnings"] == []
    assert res["issues"][0].startswith("R8: In1.Cu") and "SIG_A" in res["issues"][0]


def test_the_clean_twin_has_no_foreign_copper() -> None:
    res = lrc.check_plane_layers(_plane_board("B.Cu"), ROLES_4L)
    assert res["metrics"]["foreign_tracks"] == 0
    assert res["issues"] == [] and res["warnings"] == []


def test_no_net_tracks_and_arcs_on_a_plane_are_foreign() -> None:
    board = _board(tracks=[_track("", "In2.Cu", (0, 0), (1, 0)),
                           _track("SIG_A", "In2.Cu", (0, 0), (2, 0), mid=[1, 1])])
    res = lrc.check_plane_layers(board, ROLES_4L)
    assert res["metrics"]["foreign_tracks"] == 2
    assert res["metrics"]["by_layer"]["In2.Cu"]["nets"] == {"(no net)": 1, "SIG_A": 1}
    arc = math.pi * 1.0                           # the half circle through (1, 1)
    assert res["metrics"]["foreign_length_mm"] == pytest.approx(1.0 + arc, abs=1e-3)


def test_mixed_and_signal_layers_route_any_net() -> None:
    roles = {**ROLES_4L, "In2.Cu": {"role": "mixed", "net": "+3V3"}}
    board = _board(tracks=[_track("SIG_A", "In2.Cu", (0, 0), (5, 0)),
                           _track("SIG_A", "F.Cu", (0, 0), (5, 0))])
    res = lrc.check_plane_layers(board, roles)
    assert res["metrics"]["foreign_tracks"] == 0 and res["issues"] == []
    assert res["metrics"]["plane_layers"] == {"In1.Cu": "GND"}


def test_a_foreign_pour_on_a_plane_is_a_blocker_rule_areas_are_not() -> None:
    zones = [{"name": "", "net": "GND", "layer": "In1.Cu", "layers": ["In1.Cu"],
              "rule_area": False},
             {"name": "stray", "net": "GND", "layer": "In2.Cu", "layers": ["In2.Cu"],
              "rule_area": False},
             {"name": "keepout", "net": "", "layer": None, "layers": ["In1.Cu", "In2.Cu"],
              "rule_area": True}]
    res = lrc.check_plane_layers(_board(zones=zones), ROLES_4L)
    assert res["metrics"]["foreign_zones"] == 1 and res["metrics"]["foreign_tracks"] == 0
    assert len(res["issues"]) == 1 and "'stray'" in res["issues"][0]
    assert "In2.Cu" in res["issues"][0]


def test_issues_are_grouped_per_plane_layer_and_sorted() -> None:
    board = _board(tracks=[_track("B", "In2.Cu", (0, 0), (1, 0)),
                           _track("A", "In1.Cu", (0, 0), (1, 0)),
                           _track("C", "In1.Cu", (0, 0), (1, 0)),
                           _track("A", "In1.Cu", (2, 0), (3, 0))])
    res = lrc.check_plane_layers(board, ROLES_4L)
    assert res["metrics"]["foreign_tracks"] == 4
    assert [i.split(" ")[1] for i in res["issues"]] == ["In1.Cu", "In2.Cu"]
    assert res["metrics"]["by_layer"]["In1.Cu"]["nets"] == {"A": 2, "C": 1}


def test_a_plane_without_a_net_counts_every_track() -> None:
    roles = {**ROLES_4L, "In1.Cu": {"role": "plane", "net": None}}
    res = lrc.check_plane_layers(_board(tracks=[_track("GND", "In1.Cu", (0, 0), (1, 0))]),
                                 roles)
    assert res["metrics"]["foreign_tracks"] == 1


def test_malformed_board_input_is_refused() -> None:
    with pytest.raises(ValueError, match="track"):
        lrc.check_plane_layers({"tracks": [{"net": "A"}]}, ROLES_4L)
    with pytest.raises(ValueError, match="board"):
        lrc.check_plane_layers(["not", "a", "board"], ROLES_4L)
    with pytest.raises(ValueError, match="list of dicts"):
        lrc.check_plane_layers({"tracks": 5}, ROLES_4L)
    with pytest.raises(ValueError, match="start"):
        lrc.check_ref_transitions({"tracks": [{"net": "Z50", "layer": "F.Cu", "end": [0, 0]}]},
                                  LINES_4L)


def test_an_unnamed_foreign_pour_is_named_as_such() -> None:
    zone = {"name": "", "net": "+3V3", "layers": ["In1.Cu"], "rule_area": False}
    res = lrc.check_plane_layers(_board(zones=[zone]), ROLES_4L)
    assert res["issues"] == ["R8: In1.Cu is the GND plane but an unnamed zone pours +3V3: "
                             "remove the pour or declare In1.Cu a mixed layer"]


# ---------------------------------------------------------------- R27 reference transitions
def _transition(*extra_vias: dict, pads: tuple = ()) -> dict:
    return _board(tracks=[_track("Z50", "F.Cu", (100, 100), (110, 100)),
                          _track("Z50", "B.Cu", (110, 100), (120, 100))],
                  vias=[_via("Z50", (110, 100)), *extra_vias], pads=pads)


def _cap(dist: float, nets: tuple = ("+3V3", "GND"), ref: str = "C1") -> tuple:
    return (_pad(ref, "1", nets[0], (110, 100 + dist)),
            _pad(ref, "2", nets[1], (110 + 1.6, 100 + dist)))


def test_a_gnd_to_3v3_transition_without_a_capacitor_warns() -> None:
    res = lrc.check_ref_transitions(_transition(), LINES_4L)
    assert res["issues"] == [] and len(res["warnings"]) == 1
    w = res["warnings"][0]
    assert w.startswith("R27: Z50") and "F.Cu -> B.Cu" in w and "GND -> +3V3" in w
    assert res["metrics"] == {"impedance_vias": 1, "reference_changes": 1, "unsupported": 1,
                              "unjudged_vias": 0}


def test_a_decoupling_capacitor_within_2mm_supports_the_transition() -> None:
    res = lrc.check_ref_transitions(_transition(pads=_cap(1.5)), LINES_4L)
    assert res["warnings"] == []
    assert res["metrics"]["reference_changes"] == 1 and res["metrics"]["unsupported"] == 0


def test_a_capacitor_too_far_or_on_one_net_does_not() -> None:
    for pads in (_cap(2.5), _cap(1.0, ("GND", "GND")), _cap(1.0, ref="R1")):
        res = lrc.check_ref_transitions(_transition(pads=pads), LINES_4L)
        assert len(res["warnings"]) == 1, pads


def test_max_dist_is_a_parameter() -> None:
    res = lrc.check_ref_transitions(_transition(pads=_cap(2.5)), LINES_4L, max_dist_mm=3.0)
    assert res["warnings"] == []


def test_a_gnd_via_does_not_join_disjoint_reference_nets() -> None:
    res = lrc.check_ref_transitions(_transition(_via("GND", (111, 100))), LINES_4L)
    assert len(res["warnings"]) == 1


def test_a_via_of_the_shared_reference_net_supports_the_transition() -> None:
    lines = {"Z50": {"layers": {"F.Cu": _line(["GND"]),
                                "B.Cu": _line(["GND", "+3V3"], line="stripline")}}}
    alone = lrc.check_ref_transitions(_transition(), lines)
    assert len(alone["warnings"]) == 1 and "GND" in alone["warnings"][0]
    near = lrc.check_ref_transitions(_transition(_via("GND", (111.5, 100))), lines)
    assert near["warnings"] == []
    far = lrc.check_ref_transitions(_transition(_via("GND", (113, 100))), lines)
    assert len(far["warnings"]) == 1


def test_same_reference_nets_or_one_layer_are_no_change() -> None:
    same = {"Z50": {"layers": {"F.Cu": _line(["GND"]), "B.Cu": _line(["GND"])}}}
    res = lrc.check_ref_transitions(_transition(), same)
    assert res["warnings"] == [] and res["metrics"]["reference_changes"] == 0
    one_layer = _board(tracks=[_track("Z50", "F.Cu", (100, 100), (110, 100))],
                       vias=[_via("Z50", (110, 100))])
    res = lrc.check_ref_transitions(one_layer, LINES_4L)
    assert res["metrics"] == {"impedance_vias": 1, "reference_changes": 0, "unsupported": 0,
                              "unjudged_vias": 0}


def test_a_layer_without_a_resolved_reference_is_unjudged() -> None:
    lines = {"Z50": {"layers": {"F.Cu": _line(["GND"])}}}
    res = lrc.check_ref_transitions(_transition(), lines)
    assert res["warnings"] == [] and res["metrics"]["unjudged_vias"] == 1


def test_non_impedance_nets_are_ignored() -> None:
    board = _board(tracks=[_track("SIG_A", "F.Cu", (0, 0), (1, 0)),
                           _track("SIG_A", "B.Cu", (1, 0), (2, 0))], vias=[_via("SIG_A", (1, 0))])
    res = lrc.check_ref_transitions(board, LINES_4L)
    assert res["metrics"]["impedance_vias"] == 0 and res["warnings"] == []


# ---------------------------------------------------------------- R28 dual stripline
LINES_6L = {"ZS": {"layers": {"In2.Cu": _line(["GND", "+3V3"], ["In3.Cu"], "dual_stripline")}}}
ZS = _track("ZS", "In2.Cu", (110, 115), (130, 115), 0.15)


def _dual(*others: dict) -> dict:
    return _board(tracks=[ZS, *others])


def test_an_8mm_parallel_run_over_the_line_warns_once() -> None:
    res = lrc.check_dual_stripline_parallel(
        _dual(_track("AUX", "In3.Cu", (115, 115), (123, 115), 0.15)), LINES_6L)
    assert res["issues"] == [] and len(res["warnings"]) == 1
    w = res["warnings"][0]
    assert w.startswith("R28: ZS") and "AUX" in w and "In3.Cu" in w and "8.0 mm" in w
    runs = res["metrics"]["parallel_runs"]
    assert len(runs) == 1 and runs[0]["length_mm"] == pytest.approx(8.0)
    assert res["metrics"]["dual_stripline_lines"] == 1


def test_short_crossing_offset_and_steep_runs_do_not() -> None:
    others = [_track("SHORT", "In3.Cu", (124.5, 115), (128.5, 115), 0.15),
              _track("XING", "In3.Cu", (126, 105), (126, 125), 0.15),
              _track("OFFSIDE", "In3.Cu", (112, 117), (120, 117), 0.15),
              _track("STEEP", "In3.Cu", (112, 115), (112 + 8 * math.cos(math.radians(35)),
                                                     115 + 8 * math.sin(math.radians(35))))]
    res = lrc.check_dual_stripline_parallel(_dual(*others), LINES_6L)
    assert res["warnings"] == [] and res["metrics"]["parallel_runs"] == []


def test_a_shallow_angle_still_counts_and_thresholds_are_parameters() -> None:
    ang = math.radians(1)                # drifts off the 0.15 mm overlap band after ~8.6 mm
    shallow = _track("AUX", "In3.Cu", (112, 115), (112 + 9 * math.cos(ang),
                                                  115 + 9 * math.sin(ang)), 0.15)
    res = lrc.check_dual_stripline_parallel(_dual(shallow), LINES_6L)
    assert len(res["warnings"]) == 1
    run = res["metrics"]["parallel_runs"][0]["length_mm"]
    assert 4.0 < run < 9.0
    res = lrc.check_dual_stripline_parallel(_dual(shallow), LINES_6L, min_len_mm=run + 0.1)
    assert res["warnings"] == []
    steep = _track("AUX", "In3.Cu", (112, 115), (112 + 8 * math.cos(math.radians(25)),
                                                115 + 8 * math.sin(math.radians(25))))
    loose = lrc.check_dual_stripline_parallel(_dual(steep), LINES_6L, max_offset_mm=10.0)
    tight = lrc.check_dual_stripline_parallel(_dual(steep), LINES_6L, max_offset_mm=10.0,
                                              max_angle_deg=20.0)
    assert len(loose["warnings"]) == 1 and tight["warnings"] == []


def test_runs_add_up_per_foreign_net_and_ignore_the_own_net() -> None:
    others = [_track("AUX", "In3.Cu", (112, 115), (115, 115)),
              _track("AUX", "In3.Cu", (116, 115), (119, 115)),
              _track("ZS", "In3.Cu", (112, 115), (128, 115))]
    res = lrc.check_dual_stripline_parallel(_dual(*others), LINES_6L)
    assert len(res["warnings"]) == 1 and "AUX" in res["warnings"][0]
    assert res["metrics"]["parallel_runs"][0]["length_mm"] == pytest.approx(6.0)


def test_only_the_layers_taken_as_dielectric_are_checked() -> None:
    on_b = _track("AUX", "B.Cu", (112, 115), (128, 115))
    res = lrc.check_dual_stripline_parallel(_dual(on_b), LINES_6L)
    assert res["warnings"] == []
    plain = {"ZS": {"layers": {"In2.Cu": _line(["GND"], [], "stripline")}}}
    res = lrc.check_dual_stripline_parallel(
        _dual(_track("AUX", "In3.Cu", (112, 115), (128, 115))), plain)
    assert res["warnings"] == [] and res["metrics"]["dual_stripline_lines"] == 0


# ---------------------------------------------------------------- stackup helpers
def test_roles_of_reads_a_resolved_stackup_and_its_dict() -> None:
    rs = _rs(_design("role4l"))
    assert lrc.roles_of(rs) == ROLES_4L
    assert lrc.roles_of(rs.to_dict()) == ROLES_4L


def test_line_layers_resolve_references_and_dual_striplines() -> None:
    lines4 = lrc.line_layers(_rs(_design("role4l")), _design("role4l"))
    assert set(lines4) == {"Z50"}
    assert lines4["Z50"]["layers"]["F.Cu"]["ref"]["nets"] == ["GND"]
    assert lines4["Z50"]["layers"]["B.Cu"]["ref"]["nets"] == ["+3V3"]
    lines6 = lrc.line_layers(_rs(_design("dual6l")), _design("dual6l"))
    in2 = lines6["ZS"]["layers"]["In2.Cu"]
    assert in2["line"] == "dual_stripline" and in2["dielectric_layers"] == ["In3.Cu"]
    assert in2["ref"]["nets"] == ["GND", "+3V3"]


def test_line_layers_give_a_diff_partner_the_lines_of_its_pair() -> None:
    design = {**_design("role4l"),
              "net_specs": {"DP": {"diff": 90, "diff_pair": "DM", "gap": 0.15}}}
    lines = lrc.line_layers(_rs(design), design)
    assert set(lines) == {"DP", "DM"} and lines["DM"] == lines["DP"]


def test_line_layers_skip_a_design_without_impedance_nets() -> None:
    design = {**_design("role4l"), "net_specs": {"USB_DP": {"diff_pair": "USB_DM"}}}
    assert lrc.line_layers(_rs(design), design) == {}


# ---------------------------------------------------------------- run_checks
def test_run_checks_merges_the_three_checks_without_mutating_inputs() -> None:
    board = _board(tracks=[*_plane_board("In1.Cu")["tracks"],
                           *_transition()["tracks"]], vias=_transition()["vias"])
    roles, lines = copy.deepcopy(ROLES_4L), copy.deepcopy(LINES_4L)
    before = copy.deepcopy((board, roles, lines))
    res = lrc.run_checks(board, roles, lines)
    assert (board, roles, lines) == before
    assert len(res["issues"]) == 1 and res["issues"][0].startswith("R8")
    assert len(res["warnings"]) == 1 and res["warnings"][0].startswith("R27")
    assert set(res["metrics"]) == {"plane_layers", "ref_transitions", "dual_stripline"}
    assert res["metrics"]["plane_layers"]["foreign_tracks"] == 1
    assert json.loads(json.dumps(res)) == res


def test_run_checks_is_deterministic() -> None:
    board = _dual(_track("AUX", "In3.Cu", (115, 115), (123, 115), 0.15),
                  _track("GND", "In3.Cu", (115, 115.05), (124, 115.05), 0.15))
    roles = lrc.roles_of(_rs(_design("dual6l")))
    a = lrc.run_checks(board, roles, LINES_6L)
    b = lrc.run_checks(copy.deepcopy(board), roles, LINES_6L)
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
    assert [r["other_net"] for r in a["metrics"]["dual_stripline"]["parallel_runs"]] == \
        ["AUX", "GND"]
