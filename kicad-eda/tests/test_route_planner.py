"""route_planner — pure geometry for agent-directed routing (no KiCad needed).

Every test hands the planner hand-built pad/track/via dicts in the shapes
``backends.pcb_items`` produces (mm floats) and checks one rule each:
connections (Prim over clusters), obstacles (foreign copper near a segment),
validate_path (every rule of the path contract), width_compliance (the SWIG
open-field / entry-neck rule) and via_ampacity (IPC-2221 barrel ampacity).
"""
from __future__ import annotations

import copy
import math

import pytest

import ipc_rules
import route_planner as rp


# ----------------------------------------------------------------------------
# builders (the pcb_items dict shapes)
# ----------------------------------------------------------------------------
def pad(pid: str, net: str, x: float, y: float, *, ref: str = "U1", num: str = "1",
        layers: tuple[str, ...] = ("F.Cu",), w: float = 1.0, h: float = 1.0,
        drill: float | None = None) -> dict:
    return {"id": pid, "ref": ref, "pad": num, "net": net, "x": x, "y": y,
            "layers": list(layers), "through_hole": drill is not None,
            "shape": "rect", "w": w, "h": h, "rotation": 0.0, "drill": drill}


def track(tid: str, net: str, layer: str, start: list, end: list,
          width: float = 0.25, **extra) -> dict:
    d = {"id": tid, "net": net, "layer": layer, "start": list(start), "end": list(end),
         "width": width, "length": round(math.dist(start, end), 4)}
    return {**d, **extra}


def via(vid: str, net: str, x: float, y: float, diameter: float = 0.6,
        drill: float = 0.3) -> dict:
    return {"id": vid, "net": net, "x": x, "y": y, "diameter": diameter, "drill": drill}


NET_CLASS = {"name": "Power", "clearance": 0.2, "track_width": 0.5,
             "via_diameter": 0.8, "via_drill": 0.4}
RULES = {"min_clearance": 0.1, "min_track_width": 0.15, "min_via_diameter": 0.5,
         "min_via_drill": 0.3, "min_via_annular": 0.1, "copper_edge_clearance": 0.3,
         "hole_clearance": 0.25, "hole_to_hole": 0.5}
GOOD_PATH = [
    {"layer": "F.Cu", "points": [[0, 0], [5, 0], [8, 3]]},
    {"via": [8, 3]},
    {"layer": "B.Cu", "points": [[8, 3], [8, 10]]},
]


# ============================================================================
# connections
# ============================================================================
def test_connections_three_clusters_give_two_links_at_closest_pairs():
    pads = [pad("a", "SIG", 0, 0), pad("b", "SIG", 10, 0), pad("c", "SIG", 11, 5),
            pad("d", "SIG", 12, 5)]
    links = rp.connections(pads, {"SIG": [["a"], ["b", "c"], ["d"]]})
    assert [(c["from"]["id"], c["to"]["id"], c["length"]) for c in links] == [
        ("c", "d", 1.0), ("a", "b", 10.0)]
    assert all(c["net"] == "SIG" for c in links)


def test_connections_picks_the_closest_pad_pair_between_clusters():
    pads = [pad("a", "SIG", 0, 0), pad("a2", "SIG", 9, 0), pad("b", "SIG", 10, 0)]
    links = rp.connections(pads, {"SIG": [["a", "a2"], ["b"]]})
    assert len(links) == 1
    assert (links[0]["from"]["id"], links[0]["to"]["id"]) == ("a2", "b")
    assert links[0]["length"] == 1.0


def test_connections_single_cluster_or_single_pad_gives_nothing():
    pads = [pad("a", "SIG", 0, 0), pad("b", "SIG", 5, 0), pad("c", "LONE", 9, 9)]
    assert rp.connections(pads, {"SIG": [["a", "b"]], "LONE": [["c"]]}) == []


def test_connections_net_absent_from_clusters_is_fully_unrouted():
    pads = [pad("a", "SIG", 0, 0), pad("b", "SIG", 5, 0), pad("c", "SIG", 5, 5)]
    links = rp.connections(pads, {})
    assert len(links) == 2
    assert {c["length"] for c in links} == {5.0}


def test_connections_pad_missing_from_every_cluster_becomes_its_own_cluster():
    pads = [pad("a", "SIG", 0, 0), pad("b", "SIG", 5, 0), pad("c", "SIG", 5, 5)]
    links = rp.connections(pads, {"SIG": [["a", "b"]]})
    assert [(c["from"]["id"], c["to"]["id"]) for c in links] == [("b", "c")]


def test_connections_ordered_by_net_then_length_and_skips_unconnected_pads():
    pads = [pad("z1", "Z", 0, 0), pad("z2", "Z", 1, 0),
            pad("a1", "A", 0, 0), pad("a2", "A", 20, 0), pad("a3", "A", 20, 2),
            pad("n1", "", 0, 0), pad("n2", "", 3, 0)]
    links = rp.connections(pads, {})
    assert [(c["net"], c["length"]) for c in links] == [("A", 2.0), ("A", 20.0), ("Z", 1.0)]


def test_connections_never_mutates_inputs_and_returns_pad_copies():
    pads = [pad("a", "SIG", 0, 0), pad("b", "SIG", 5, 0)]
    clusters = {"SIG": [["a"], ["b"]]}
    before = copy.deepcopy((pads, clusters))
    links = rp.connections(pads, clusters)
    assert (pads, clusters) == before
    links[0]["from"]["x"] = 99
    assert pads[0]["x"] == 0


# ============================================================================
# obstacles / corridor
# ============================================================================
A, B = [0.0, 0.0], [10.0, 0.0]
ARGS = dict(net="SIG", clearance=0.2, width=0.25)      # limit = 0.325 mm


def test_obstacles_finds_foreign_pad_on_the_line():
    pads = [pad("p", "GND", 5, 0, ref="C1", num="2")]
    out = rp.obstacles(A, B, "F.Cu", pads, [], [], **ARGS)
    assert len(out) == 1
    o = out[0]
    assert (o["kind"], o["id"], o["ref"], o["pad"], o["net"]) == ("pad", "p", "C1", "2", "GND")
    assert o["t"] == 0.5
    assert o["distance"] == pytest.approx(-0.5, abs=1e-4)     # the line runs 0.5 mm into a 1 x 1 pad


def test_obstacles_ignores_same_net_pad_and_pad_on_the_other_layer():
    pads = [pad("mine", "SIG", 5, 0), pad("back", "GND", 5, 0, layers=("B.Cu",))]
    assert rp.obstacles(A, B, "F.Cu", pads, [], [], **ARGS) == []
    assert [o["id"] for o in rp.obstacles(A, B, "B.Cu", pads, [], [], **ARGS)] == ["back"]


def test_obstacles_through_hole_pad_counts_on_each_of_its_layers():
    pads = [pad("th", "GND", 5, 0, layers=("F.Cu", "In1.Cu", "In2.Cu", "B.Cu"), drill=0.8)]
    for layer in ("F.Cu", "In1.Cu", "B.Cu"):
        assert [o["id"] for o in rp.obstacles(A, B, layer, pads, [], [], **ARGS)] == ["th"]


def test_obstacles_pad_reach_is_clearance_plus_half_width_from_the_pads_edge():
    near = pad("near", "GND", 5, 0.8)        # 1 x 1 rect: edge at 0.3 < 0.325
    far = pad("far", "GND", 5, 0.85)         # edge at 0.35 > 0.325
    out = rp.obstacles(A, B, "F.Cu", [near, far], [], [], **ARGS)
    assert [o["id"] for o in out] == ["near"]
    assert out[0]["distance"] == pytest.approx(0.3, abs=1e-4)


def test_obstacles_rect_pads_are_exact_so_a_track_fits_between_header_pins():
    # two 1.7 mm square pins 2.54 mm apart; a 0.25 mm track at 0.2 mm clearance between them
    # (the agent-routing skill's rule 1: 0.84 mm gap, 0.65 mm needed, 0.19 mm to spare)
    pins = [pad("j1", "GND", 5, 6.15, w=1.7, h=1.7, drill=1.0, layers=("F.Cu", "B.Cu")),
            pad("j2", "VCC", 5, 8.69, w=1.7, h=1.7, drill=1.0, layers=("F.Cu", "B.Cu"))]
    a, b = [0.0, 7.42], [10.0, 7.42]
    assert rp.obstacles(a, b, "F.Cu", pins, [], [], net="SIG", clearance=0.2, width=0.25) == []
    tight = rp.obstacles(a, b, "F.Cu", pins, [], [], net="SIG", clearance=0.3, width=0.25)
    assert [o["id"] for o in tight] == ["j1", "j2"]
    assert all(o["distance"] == pytest.approx(0.42, abs=1e-4) for o in tight)   # 1.27 - 0.85


def test_obstacles_pad_edge_follows_the_pad_shape():
    def gap(p: dict) -> tuple[float, float]:
        out = rp.obstacles(A, B, "F.Cu", [p], [], [], net="SIG", clearance=5.0, width=0.0)
        return out[0]["distance"], out[0]["t"]

    circle = {**pad("c", "GND", 5, 1.5, w=1.7, h=1.7), "shape": "circle"}
    assert gap(circle)[0] == pytest.approx(0.65)                       # 1.5 - 0.85
    oval_along = {**pad("o", "GND", 5, 1.2, w=2.0, h=1.0), "shape": "oval"}
    assert gap(oval_along)[0] == pytest.approx(0.7)                    # long axis parallel
    oval_across = {**oval_along, "rotation": 90.0}
    assert gap(oval_across)[0] == pytest.approx(0.2)                   # axis end 0.5 mm nearer
    tall = {**pad("r", "GND", 5, 1.0, w=3.0, h=1.0), "rotation": 90.0}
    assert gap(tall) == (pytest.approx(-0.5), pytest.approx(0.5))      # 3 mm across: inside by 0.5
    beyond = pad("e", "GND", 12, 0, w=3.0, h=1.0)                      # past the end: edge at 10.5
    assert gap(beyond) == (pytest.approx(0.5), 1.0)
    rounded = {**pad("rr", "GND", 5, 0.8), "shape": "roundrect"}
    assert gap(rounded)[0] == pytest.approx(0.3)                       # bounded by its rectangle
    custom = {**pad("x", "GND", 5, 0.8), "shape": "custom"}
    assert gap(custom)[0] == pytest.approx(0.8 - math.hypot(1, 1) / 2, abs=1e-4)   # half-diagonal


def test_obstacles_crossing_track_has_zero_gap_minus_half_width():
    tracks = [track("t", "GND", "F.Cu", [5, -2], [5, 2], width=0.2)]
    out = rp.obstacles(A, B, "F.Cu", [], tracks, [], **ARGS)
    assert len(out) == 1 and out[0]["kind"] == "track"
    assert out[0]["distance"] == pytest.approx(-0.1, abs=1e-6)
    assert out[0]["t"] == pytest.approx(0.5)
    assert "ref" not in out[0]


def test_obstacles_ignores_far_parallel_track_other_layer_track_and_same_net_track():
    tracks = [track("far", "GND", "F.Cu", [0, 5], [10, 5]),
              track("back", "GND", "B.Cu", [5, -2], [5, 2]),
              track("mine", "SIG", "F.Cu", [5, -2], [5, 2])]
    assert rp.obstacles(A, B, "F.Cu", [], tracks, [], **ARGS) == []


def test_obstacles_track_endpoint_distance_uses_segment_to_segment():
    tracks = [track("t", "GND", "F.Cu", [5, 0.5], [5, 3], width=0.2)]   # gap 0.4
    assert rp.obstacles(A, B, "F.Cu", [], tracks, [], **ARGS) == []
    out = rp.obstacles(A, B, "F.Cu", [], tracks, [], net="SIG", clearance=0.3, width=0.25)
    assert [o["id"] for o in out] == ["t"]
    assert out[0]["distance"] == pytest.approx(0.4, abs=1e-6)


def test_obstacles_arc_track_is_judged_by_its_mid_point_chords():
    arc = track("arc", "GND", "F.Cu", [2, -3], [8, -3], width=0.2, arc=True, mid=[5, -0.2])
    chord = track("chord", "GND", "F.Cu", [2, -3], [8, -3], width=0.2)
    assert [o["id"] for o in rp.obstacles(A, B, "F.Cu", [], [arc, chord], [], **ARGS)] == ["arc"]


def test_obstacles_via_counts_on_every_copper_layer_and_output_sorted_by_t():
    vias = [via("v", "GND", 8, 0.3)]                 # gap 0.3 - 0.3 = 0 at t 0.8
    pads = [pad("p", "GND", 5, 0)]                   # t 0.5
    tracks = [track("t", "GND", "In1.Cu", [2, -1], [2, 1])]   # t 0.2, inner layer
    out = rp.obstacles(A, B, "In1.Cu", pads, tracks, vias, **ARGS)
    assert [o["kind"] for o in out] == ["track", "via"]
    assert [o["t"] for o in out] == [pytest.approx(0.2), pytest.approx(0.8)]
    assert rp.obstacles(A, B, "B.Cu", [], [], [via("mine", "SIG", 8, 0.3)], **ARGS) == []


def test_obstacles_never_mutates_inputs():
    pads = [pad("p", "GND", 5, 0)]
    tracks = [track("t", "GND", "F.Cu", [5, -2], [5, 2])]
    vias = [via("v", "GND", 8, 0.3)]
    before = copy.deepcopy((pads, tracks, vias))
    rp.obstacles(A, B, "F.Cu", pads, tracks, vias, **ARGS)
    assert (pads, tracks, vias) == before


def test_corridor_reports_both_default_layers():
    conn = {"net": "SIG", "from": pad("a", "SIG", 0, 0), "to": pad("b", "SIG", 10, 0),
            "length": 10.0}
    pads = [pad("top", "GND", 5, 0), pad("bot", "GND", 7, 0, layers=("B.Cu",))]
    out = rp.corridor(conn, pads=pads, tracks=[], vias=[], clearance=0.2, width=0.25)
    assert set(out) == {"F.Cu", "B.Cu"}
    assert [o["id"] for o in out["F.Cu"]] == ["top"]
    assert [o["id"] for o in out["B.Cu"]] == ["bot"]
    assert list(rp.corridor(conn, ("In1.Cu",), pads=pads, tracks=[], vias=[],
                            clearance=0.2, width=0.25)) == ["In1.Cu"]


# ============================================================================
# validate_path
# ============================================================================
def test_validate_path_good_path_fills_defaults_from_the_class():
    out = rp.validate_path(GOOD_PATH, NET_CLASS, RULES)
    assert out["warnings"] == []
    assert out["segments"] == [
        {"layer": "F.Cu", "start": [0.0, 0.0], "end": [5.0, 0.0], "width": 0.5},
        {"layer": "F.Cu", "start": [5.0, 0.0], "end": [8.0, 3.0], "width": 0.5},
        {"layer": "B.Cu", "start": [8.0, 3.0], "end": [8.0, 10.0], "width": 0.5},
    ]
    assert out["vias"] == [{"x": 8.0, "y": 3.0, "diameter": 0.8, "drill": 0.4}]


def test_validate_path_falls_back_to_rules_minimums_without_class_values():
    no_class = {"name": "Default", "clearance": None, "track_width": None,
                "via_diameter": None, "via_drill": None}
    out = rp.validate_path(GOOD_PATH, no_class, RULES)
    assert {s["width"] for s in out["segments"]} == {0.15}
    assert out["vias"] == [{"x": 8.0, "y": 3.0, "diameter": 0.5, "drill": 0.3}]


def test_validate_path_explicit_width_and_via_size_and_per_element_overrides():
    path = [{"layer": "F.Cu", "points": [[0, 0], [5, 0]], "width": 0.8},
            {"via": [5, 0], "diameter": 1.0, "drill": 0.5},
            {"layer": "B.Cu", "points": [[5, 0], [5, 5]]}]
    out = rp.validate_path(path, NET_CLASS, RULES, width=0.6, via_diameter=0.9, via_drill=0.45)
    assert [s["width"] for s in out["segments"]] == [0.8, 0.6]
    assert out["vias"][0] == {"x": 5.0, "y": 0.0, "diameter": 1.0, "drill": 0.5}


def test_validate_path_requires_a_polyline():
    with pytest.raises(ValueError, match="at least one polyline"):
        rp.validate_path([], NET_CLASS, RULES)
    with pytest.raises(ValueError, match="polyline"):
        rp.validate_path([{"via": [1, 1]}], NET_CLASS, RULES)


def test_validate_path_polyline_needs_two_distinct_points_and_drops_zero_length():
    with pytest.raises(ValueError, match=r"path\[0\].*2 distinct points"):
        rp.validate_path([{"layer": "F.Cu", "points": [[1, 1], [1, 1.0004]]}], NET_CLASS, RULES)
    out = rp.validate_path([{"layer": "F.Cu", "points": [[0, 0], [0, 0], [5, 0], [5, 0.0002]]}],
                           NET_CLASS, RULES)
    assert len(out["segments"]) == 1
    assert out["segments"][0]["end"] == [5.0, 0.0]


def test_validate_path_two_polylines_need_a_via_between_them():
    path = [{"layer": "F.Cu", "points": [[0, 0], [5, 0]]},
            {"layer": "B.Cu", "points": [[5, 0], [5, 5]]}]
    with pytest.raises(ValueError, match=r"path\[1\].*without a via"):
        rp.validate_path(path, NET_CLASS, RULES)


def test_validate_path_via_must_sit_at_the_join_within_a_micron():
    off = [{"layer": "F.Cu", "points": [[0, 0], [5, 0]]}, {"via": [5, 0.01]},
           {"layer": "B.Cu", "points": [[5, 0], [5, 5]]}]
    with pytest.raises(ValueError, match=r"path\[1\].*not at the end of path\[0\]"):
        rp.validate_path(off, NET_CLASS, RULES)
    off_start = [{"layer": "F.Cu", "points": [[0, 0], [5, 0]]}, {"via": [5, 0]},
                 {"layer": "B.Cu", "points": [[5.01, 0], [5, 5]]}]
    with pytest.raises(ValueError, match=r"path\[1\].*not at the start of path\[2\]"):
        rp.validate_path(off_start, NET_CLASS, RULES)
    within = [{"layer": "F.Cu", "points": [[0, 0], [5, 0]]}, {"via": [5.0005, 0]},
              {"layer": "B.Cu", "points": [[5, 0], [5, 5]]}]
    assert len(rp.validate_path(within, NET_CLASS, RULES)["vias"]) == 1


def test_validate_path_allows_a_trailing_or_leading_via_stub_to_a_plane():
    stub = [{"layer": "F.Cu", "points": [[0, 0], [1.5, 0]]}, {"via": [1.5, 0]}]
    out = rp.validate_path(stub, NET_CLASS, RULES)
    assert len(out["segments"]) == 1 and len(out["vias"]) == 1 and out["warnings"] == []
    lead = [{"via": [0, 0]}, {"layer": "B.Cu", "points": [[0, 0], [1.5, 0]]}]
    assert len(rp.validate_path(lead, NET_CLASS, RULES)["vias"]) == 1
    with pytest.raises(ValueError, match="not at the end"):
        rp.validate_path([{"layer": "F.Cu", "points": [[0, 0], [1.5, 0]]}, {"via": [3, 0]}],
                         NET_CLASS, RULES)


def test_validate_path_rejects_two_vias_in_a_row():
    path = [{"layer": "F.Cu", "points": [[0, 0], [5, 0]]}, {"via": [5, 0]}, {"via": [5, 0]},
            {"layer": "B.Cu", "points": [[5, 0], [5, 5]]}]
    with pytest.raises(ValueError, match=r"path\[2\].*two vias in a row"):
        rp.validate_path(path, NET_CLASS, RULES)


def test_validate_path_warns_on_a_same_layer_via():
    path = [{"layer": "F.Cu", "points": [[0, 0], [5, 0]]}, {"via": [5, 0]},
            {"layer": "F.Cu", "points": [[5, 0], [5, 5]]}]
    out = rp.validate_path(path, NET_CLASS, RULES)
    assert len(out["vias"]) == 1
    assert any("same layer" in w and "F.Cu" in w for w in out["warnings"])


def test_validate_path_width_below_class_is_an_error_naming_the_class_width():
    with pytest.raises(ValueError, match=r"0\.3 mm.*Power.*0\.5 mm"):
        rp.validate_path(GOOD_PATH, NET_CLASS, RULES, width=0.3)


def test_validate_path_allow_narrow_permits_a_class_underrun_but_not_a_rules_underrun():
    out = rp.validate_path(GOOD_PATH, NET_CLASS, RULES, width=0.3, allow_narrow=True)
    assert {s["width"] for s in out["segments"]} == {0.3}
    with pytest.raises(ValueError, match=r"0\.1 mm.*minimum track width 0\.15 mm"):
        rp.validate_path(GOOD_PATH, NET_CLASS, RULES, width=0.1, allow_narrow=True)


def test_validate_path_rejects_negative_or_non_numeric_width():
    with pytest.raises(ValueError, match="width"):
        rp.validate_path(GOOD_PATH, NET_CLASS, RULES, width=-0.2)
    bad = [{"layer": "F.Cu", "points": [[0, 0], [5, 0]], "width": "wide"}]
    with pytest.raises(ValueError, match=r"path\[0\].*width"):
        rp.validate_path(bad, NET_CLASS, RULES)


def test_validate_path_no_width_anywhere_is_an_error():
    empty_rules = {k: None for k in RULES}
    no_class = {"name": "x", "clearance": None, "track_width": None,
                "via_diameter": None, "via_drill": None}
    with pytest.raises(ValueError, match="pass width="):
        rp.validate_path([{"layer": "F.Cu", "points": [[0, 0], [5, 0]]}], no_class, empty_rules)


def test_validate_path_via_geometry_rules():
    with pytest.raises(ValueError, match=r"drill 0\.8 mm.*diameter 0\.8 mm"):
        rp.validate_path(GOOD_PATH, NET_CLASS, RULES, via_drill=0.8)
    with pytest.raises(ValueError, match=r"annular ring 0\.05.*0\.1"):
        rp.validate_path(GOOD_PATH, NET_CLASS, RULES, via_diameter=0.6, via_drill=0.5)
    with pytest.raises(ValueError, match=r"diameter 0\.4 mm.*minimum via diameter 0\.5"):
        rp.validate_path(GOOD_PATH, NET_CLASS, RULES, via_diameter=0.4, via_drill=0.2)
    with pytest.raises(ValueError, match=r"drill 0\.2 mm.*minimum via drill 0\.3"):
        rp.validate_path(GOOD_PATH, NET_CLASS, RULES, via_diameter=0.6, via_drill=0.2)


def test_validate_path_non_45_bend_is_a_warning_not_an_error():
    path = [{"layer": "F.Cu", "points": [[0, 0], [5, 0], [8, 2]]}]
    out = rp.validate_path(path, NET_CLASS, RULES)
    assert len(out["segments"]) == 2
    assert len(out["warnings"]) == 1
    assert "33.7" in out["warnings"][0] and "45" in out["warnings"][0]
    assert "[5.0, 0.0]" in out["warnings"][0]


def test_validate_path_45_and_90_bends_are_silent_and_acute_turns_warn():
    ok = [{"layer": "F.Cu", "points": [[0, 0], [5, 0], [8, 3], [8, 8], [3, 8]]}]
    assert rp.validate_path(ok, NET_CLASS, RULES)["warnings"] == []
    acute = [{"layer": "F.Cu", "points": [[0, 0], [5, 0], [0, 5]]}]
    warns = rp.validate_path(acute, NET_CLASS, RULES)["warnings"]
    assert len(warns) == 1 and "acute" in warns[0]


def test_validate_path_rejects_malformed_elements_and_points():
    with pytest.raises(ValueError, match=r"path\[0\].*neither"):
        rp.validate_path([{"width": 0.5}], NET_CLASS, RULES)
    with pytest.raises(ValueError, match=r"path\[0\].*dict"):
        rp.validate_path(["F.Cu"], NET_CLASS, RULES)
    with pytest.raises(ValueError, match=r"path\[0\]\.points\[1\]"):
        rp.validate_path([{"layer": "F.Cu", "points": [[0, 0], [1]]}], NET_CLASS, RULES)
    with pytest.raises(ValueError, match=r"path\[0\]\.via"):
        rp.validate_path([{"via": [0, "x"]}, {"layer": "F.Cu", "points": [[0, 0], [1, 0]]}],
                         NET_CLASS, RULES)
    with pytest.raises(ValueError, match=r"path\[0\].*layer"):
        rp.validate_path([{"layer": "", "points": [[0, 0], [1, 0]]}], NET_CLASS, RULES)
    with pytest.raises(ValueError, match=r"path\[0\].*points"):
        rp.validate_path([{"layer": "F.Cu", "points": "0,0 1,0"}], NET_CLASS, RULES)
    with pytest.raises(ValueError, match="path must be a list"):
        rp.validate_path({"layer": "F.Cu"}, NET_CLASS, RULES)


def test_validate_path_rounds_to_4dp_and_never_mutates_the_input():
    path = [{"layer": "F.Cu", "points": [[0.123456, 0], [5.000049, 0]]}, {"via": [5.000049, 0]}]
    before = copy.deepcopy(path)
    out = rp.validate_path(path, NET_CLASS, RULES)
    assert path == before
    assert out["segments"][0]["start"] == [0.1235, 0.0]
    assert out["vias"][0]["x"] == 5.0


# ============================================================================
# route_length
# ============================================================================
def test_route_length_sums_segment_lengths():
    segs = rp.validate_path(GOOD_PATH, NET_CLASS, RULES)["segments"]
    assert rp.route_length(segs) == pytest.approx(5 + math.hypot(3, 3) + 7, abs=1e-4)
    assert rp.route_length([]) == 0.0
    assert rp.route_length([track("t", "X", "F.Cu", [0, 0], [3, 4])]) == 5.0


# ============================================================================
# width_compliance
# ============================================================================
CLASS_W = {"V5": 1.375}


def test_width_compliance_flags_a_long_open_field_sub_width_run():
    tracks = {"V5": [track("t", "V5", "F.Cu", [20, 20], [40, 20], width=0.25)]}
    entries = {"V5": [[0, 0, 1.0, 1.1]]}                    # narrow pad, 20+ mm away
    issues = rp.width_compliance(tracks, CLASS_W, entries)
    assert issues == ["V5: open-field segment routed at 0.250 mm but the net class "
                      "requires 1.375 mm"]


def test_width_compliance_exempts_an_entry_neck_near_a_narrow_pad():
    # pad bbox 1.0 x 1.1 at (0.5, 0.55): reach = max(3, 1.1 + 1) = 3 mm
    tracks = {"V5": [track("neck", "V5", "F.Cu", [0.5, 0.55], [3.2, 0.55], width=0.25),
                     track("fat", "V5", "F.Cu", [3.2, 0.55], [30, 0.55], width=1.375)]}
    entries = {"V5": [[0, 0, 1.0, 1.1]]}
    assert rp.width_compliance(tracks, CLASS_W, entries) == []
    # both ends > 3 mm from the pad centre (0.5, 0.55): open field, flagged
    too_far = {"V5": [track("neck", "V5", "F.Cu", [3.6, 0.55], [7, 0.55], width=0.25)]}
    assert len(rp.width_compliance(too_far, CLASS_W, entries)) == 1


def test_width_compliance_reach_grows_with_the_entry_long_dimension():
    # 1.0 x 6.0 pad: reach = 7 mm; a neck 6.5 mm from its centre is still exempt
    tracks = {"V5": [track("neck", "V5", "F.Cu", [7.0, 0], [12, 0], width=0.25)]}
    assert rp.width_compliance(tracks, CLASS_W, {"V5": [[0, -3, 1.0, 3]]}) == []
    assert len(rp.width_compliance(tracks, CLASS_W, {"V5": [[0, -0.5, 1.0, 0.5]]})) == 1


def test_width_compliance_wide_entry_needs_no_neck_and_a_via_bbox_exempts():
    tracks = {"V5": [track("neck", "V5", "F.Cu", [1, 0], [2.5, 0], width=0.25)]}
    wide = {"V5": [[-1, -1, 1, 1]]}                        # 2 x 2 >= 1.375: no neck needed
    assert len(rp.width_compliance(tracks, CLASS_W, wide)) == 1
    via_bbox = {"V5": [[-0.5, -0.5, 0.5, 0.5]]}
    assert rp.width_compliance(tracks, CLASS_W, via_bbox) == []


def test_width_compliance_one_issue_per_net_and_only_for_classed_nets():
    tracks = {"V5": [track("a", "V5", "F.Cu", [20, 20], [40, 20], width=0.25),
                     track("b", "V5", "B.Cu", [20, 30], [40, 30], width=0.3)],
              "SIG": [track("c", "SIG", "F.Cu", [20, 40], [40, 40], width=0.1)]}
    issues = rp.width_compliance(tracks, {**CLASS_W, "SIG": 0}, {})
    assert len(issues) == 1 and issues[0].startswith("V5:")
    assert rp.width_compliance(tracks, {}, {}) == []


def test_width_compliance_accepts_width_within_a_micron():
    tracks = {"V5": [track("t", "V5", "F.Cu", [20, 20], [40, 20], width=1.3745)]}
    assert rp.width_compliance(tracks, CLASS_W, {}) == []


def test_width_compliance_prerouted_fat_path_must_survive():
    gone = {"SW1": [track("tap", "SW1", "F.Cu", [0, 0], [0.5, 0], width=0.25)]}
    issues = rp.width_compliance(gone, {}, {}, {"SW1": 1.375})
    assert len(issues) == 1
    assert issues[0].startswith("SW1: prerouted 1.375 mm power path missing")
    assert "0.0 mm of fat copper survives" in issues[0]
    kept = {"SW1": [track("fat", "SW1", "F.Cu", [0, 0], [1.2, 0], width=1.375)]}
    assert rp.width_compliance(kept, {}, {}, {"SW1": 1.375}) == []


def test_preroutes_reads_widths_and_authored_fat_lengths_like_the_swig_gate():
    sw1 = {"net": "SW1", "width": 1.375, "layer": "F.Cu",           # examples/rpi5_battery_hat
           "path": [["pad", "U2", "8", 0.6], ["xy", 44.3, 9.14, 1.375], ["xy", 45.53, 10.0]]}
    widths, authored = rp.preroutes({"preroutes": [sw1]})
    assert widths == {"SW1": 1.375} and authored["SW1"] == pytest.approx(1.5008, abs=1e-3)
    # a point's width applies to the segment it starts: the 0.5 mm run is not fat, the next
    # segment reverts to the preroute width; two identical preroutes add up
    thin = {"net": "V5", "width": 1.0, "path": [["xy", 0, 0, 0.5], ["xy", 3, 0], ["xy", 3, 4]]}
    assert rp.preroutes({"preroutes": [thin, sw1, sw1]}) == \
        ({"SW1": 1.375, "V5": 1.0}, {"V5": 4.0, "SW1": pytest.approx(3.0016, abs=1e-3)})
    wide = {"net": "V5", "width": 1.0, "path": [["xy", 0, 0], ["xy", 3, 0], ["xy", 3, 4, 0.4],
                                                 ["xy", 9, 4]]}
    assert rp.preroutes({"preroutes": [wide]})[1] == {"V5": pytest.approx(7.0)}  # 3 + 4, not the 0.4 run
    no_xy = {"net": "V5", "width": 1.0, "path": [["pad", "U1", "2"], ["xy", 1, 1]]}
    assert rp.preroutes({"preroutes": [no_xy, {"net": "", "width": 1}, "junk"]}) == ({"V5": 1.0}, {})
    assert rp.preroutes({}) == ({}, {})


def test_width_compliance_prerouted_fat_path_needs_half_the_authored_length():
    """SWIG parity: need = min(1 mm, 0.5 x authored); 1 mm when the length is unknown."""
    kept = {"SW1": [track("fat", "SW1", "F.Cu", [0, 0], [0.8, 0], width=1.375)]}
    assert rp.width_compliance(kept, {}, {}, {"SW1": 1.375}, {"SW1": 1.5}) == []   # need 0.75
    unknown = rp.width_compliance(kept, {}, {}, {"SW1": 1.375})                    # need 1.0
    assert len(unknown) == 1 and "authored length not in the netlist" in unknown[0]
    short = {"SW1": [track("fat", "SW1", "F.Cu", [0, 0], [0.7, 0], width=1.375)]}
    assert rp.width_compliance(short, {}, {}, {"SW1": 1.375}, {"SW1": 1.5}) == [
        "SW1: prerouted 1.375 mm power path missing from the routed board (0.7 mm of fat "
        "copper survives, authored 1.5 mm — removed?)"]
    long_run = {"SW1": [track("fat", "SW1", "F.Cu", [0, 0], [1.0, 0], width=1.375)]}
    assert rp.width_compliance(long_run, {}, {}, {"SW1": 1.375}, {"SW1": 6.0}) == []  # capped at 1 mm


def test_width_compliance_never_mutates_inputs():
    tracks = {"V5": [track("t", "V5", "F.Cu", [20, 20], [40, 20], width=0.25)]}
    entries = {"V5": [[0, 0, 1.0, 1.1]]}
    before = copy.deepcopy((tracks, entries))
    rp.width_compliance(tracks, CLASS_W, entries, {"V5": 1.0})
    assert (tracks, entries) == before


# ============================================================================
# via_ampacity
# ============================================================================
def test_via_ampacity_below_spec_names_vias_barrels_and_capacity():
    vias = {"V5": [via("v1", "V5", 0, 0, drill=0.3), via("v2", "V5", 1, 0, drill=0.3)]}
    specs = {"V5": {"voltage": 5.0, "current": 3.0}}
    issues = rp.via_ampacity(vias, {}, specs, [])
    cap = round(2 * ipc_rules.via_ampacity_a(0.3), 2)
    assert issues == [f"V5: 3.0 A crosses layers through 2 via(s) + 0 PTH barrel(s) "
                      f"totaling {cap} A (IPC-2221) — add parallel vias at the transition"]


def test_via_ampacity_enough_vias_pass():
    need = ipc_rules.vias_for_current(3.0, 0.3)
    vias = {"V5": [via(f"v{i}", "V5", i, 0, drill=0.3) for i in range(need)]}
    assert rp.via_ampacity(vias, {}, {"V5": {"current": 3.0}}, []) == []


def test_via_ampacity_pth_barrels_share_the_transition_current():
    vias = {"V5": [via("v1", "V5", 0, 0, drill=0.3)]}
    pth = {"V5": [1.0, 1.0, 0.0]}                          # two real barrels, one bogus
    issues = rp.via_ampacity(vias, pth, {"V5": {"current": 3.0}}, [])
    cap = ipc_rules.via_ampacity_a(0.3) + 2 * ipc_rules.via_ampacity_a(1.0)
    if cap >= 3.0:
        assert issues == []
    else:
        assert "1 via(s) + 2 PTH barrel(s)" in issues[0]


def test_via_ampacity_skips_low_current_nets_and_nets_without_vias():
    vias = {"SIG": [via("v", "SIG", 0, 0, drill=0.2)]}
    specs = {"SIG": {"current": 1.9}, "V5": {"current": 5.0}, "GND": {}}
    assert rp.via_ampacity(vias, {"V5": [1.0]}, specs, []) == []


def test_via_ampacity_prerouted_net_judged_by_its_authored_via_cluster():
    specs = {"SW1": {"current": 3.0}}
    single = [{"net": "SW1", "width": 1.375, "layer": "F.Cu", "path": []}]
    board_vias = {"SW1": [via("tap", "SW1", 0, 0, drill=0.2)]}
    assert rp.via_ampacity(board_vias, {}, specs, single) == []       # single-layer path
    thin = [{"net": "SW1", "width": 1.375, "layer": "F.Cu", "path": [],
             "vias": [[1, 1]], "via_size": [1.2, 0.9]},
            {"net": "SW1", "width": 1.375, "layer": "B.Cu", "path": []}]
    issues = rp.via_ampacity(board_vias, {}, specs, thin)
    cap = round(ipc_rules.via_ampacity_a(0.9), 2)
    assert issues == [f"SW1: prerouted layer transition carries 3.0 A but the authored via "
                      f"cluster (1 via(s)) totals only {cap} A (IPC-2221) — add parallel vias"]
    enough = [{**thin[0], "vias": [[1, 1], [2, 1], [3, 1]]}, thin[1]]
    assert rp.via_ampacity(board_vias, {}, specs, enough) == []


def test_via_ampacity_default_via_size_and_input_immutability():
    specs = {"SW1": {"current": 2.0}}
    pre = [{"net": "SW1", "width": 1.0, "layer": "F.Cu", "vias": [[1, 1], [2, 2], [3, 3]]},
           {"net": "SW1", "width": 1.0, "layer": "B.Cu"}]
    vias = {"SW1": [via("v", "SW1", 0, 0)]}
    before = copy.deepcopy((specs, pre, vias))
    issues = rp.via_ampacity(vias, {}, specs, pre)
    assert (specs, pre, vias) == before
    assert issues == [] if 3 * ipc_rules.via_ampacity_a(0.9) >= 2.0 else len(issues) == 1
