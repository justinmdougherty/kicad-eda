"""route_outline — the board outline on the agent-routing path (pure, no KiCad).

Everything runs on the T10 outline (60 x 40 mm, r = 4 corners; cutout 1 = the
stadium slot x 27..33 / y 4..14, cutout 2 = the 5 mm round hole at (30, 20))
drawn at (100.5, 115) on the KiCad sheet, so every test also proves the
KiCad-frame <-> board-frame translation. Points handed to route_outline are
KiCad-frame mm, as the agent and backends.pcb_items use them.

T10_BOARD and assert_same_outline are defined here (test_pcb_items_outline
imports them) so these tests need nothing outside the wave-1 tree.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

import outline as ol
import route_outline as ro
import route_planner as rp

# the spec's T10 outline (60 x 40 r4, stadium slot, 5 mm hole, datum (10, 30))
T10_BOARD = {"outline": {"loop": [[4, 0], [56, 0, 0.41421356237309503], [60, 4],
                                  [60, 36, 0.41421356237309503], [56, 40],
                                  [4, 40, 0.41421356237309503], [0, 36],
                                  [0, 4, 0.41421356237309503]]},
             "cutouts": [{"loop": [[27, 7], [27, 11, -1], [33, 11], [33, 7, -1]]},
                         {"circle": [30, 20, 2.5]}],
             "datum": [10, 30]}
OX, OY = 100.5, 115.0
T10 = ol.from_board_block(T10_BOARD)
Board = tuple[ol.Outline, tuple[float, float]]     # outline_from_geometry's answer
SERVER = Path(__file__).resolve().parents[1] / "server"


def _close(a: tuple, b: tuple, tol: float) -> bool:
    return all(abs(x - y) <= tol for x, y in zip(a, b, strict=True))


def _same_ring(ra: ol.Loop | ol.Circle, rb: ol.Loop | ol.Circle, tol: float) -> bool:
    """Same ring within `tol` from any start vertex (bulges included)."""
    if type(ra) is not type(rb):
        return False
    if isinstance(ra, ol.Circle):
        return _close((ra.cx, ra.cy, ra.r), (rb.cx, rb.cy, rb.r), tol)
    va, vb = tuple(ra.vertices), tuple(rb.vertices)
    return len(va) == len(vb) and any(
        all(_close(p, q, tol) for p, q in zip(va, vb[k:] + vb[:k], strict=True))
        for k in range(len(vb)))


def assert_same_outline(a: ol.Outline, b: ol.Outline, tol: float) -> None:
    """The outer ring, the cutouts in any order (each from any start vertex)
    and the datum match within `tol`."""
    assert _same_ring(a.outer, b.outer, tol), (a.outer, b.outer)
    assert len(a.cutouts) == len(b.cutouts), (a.cutouts, b.cutouts)
    unmatched = list(b.cutouts)
    for ring in a.cutouts:
        hit = next((i for i, other in enumerate(unmatched) if _same_ring(ring, other, tol)), None)
        assert hit is not None, (ring, unmatched)
        unmatched = unmatched[:hit] + unmatched[hit + 1:]
    assert (a.datum is None) == (b.datum is None), (a.datum, b.datum)
    assert a.datum is None or _close(a.datum, b.datum, tol), (a.datum, b.datum)


def k(x: float, y: float) -> list[float]:
    """A board-frame point on the KiCad sheet."""
    return [OX + x, OY + y]


def geometry(datum: tuple | None = (OX + 10, OY + 30)) -> dict:
    """What pcb_items.outline_geometry answers for T10 at (OX, OY)."""
    prims = []
    for p in ol.to_kicad_primitives(T10):
        moved = {key: None if p[key] is None else k(*p[key])
                 for key in ("start", "mid", "end", "center")}
        prims.append({"kind": p["kind"], **moved, "radius": p["radius"]})
    return {"prims": prims, "origin": [OX, OY], "datum": None if datum is None else list(datum)}


@pytest.fixture(scope="module")
def board() -> Board:
    return ro.outline_from_geometry(geometry())


# ============================================================================
# outline_from_geometry
# ============================================================================
def test_outline_from_geometry_returns_the_board_frame_outline_and_origin(board: Board) -> None:
    model, origin = board
    assert origin == (OX, OY)
    assert_same_outline(model, T10, 1e-9)
    assert isinstance(model.cutouts[0], ol.Loop) and isinstance(model.cutouts[1], ol.Circle)
    no_datum, _origin = ro.outline_from_geometry(geometry(datum=None))
    assert no_datum.datum is None


@pytest.mark.parametrize("change", [
    {"cutouts": [T10_BOARD["cutouts"][0], {"circle": [30, 20.01, 2.5]}]},
    {"cutouts": T10_BOARD["cutouts"][:1]},
    {"datum": [10, 31]},
    {"datum": None},
])
def test_assert_same_outline_tells_a_different_board(change: dict) -> None:
    with pytest.raises(AssertionError):
        assert_same_outline(ol.from_board_block({**T10_BOARD, **change}), T10, 1e-3)
    assert_same_outline(T10, ol.from_board_block(T10_BOARD), 0.0)


def test_outline_from_geometry_does_not_touch_its_input() -> None:
    geom = geometry()
    before = repr(geom)
    ro.outline_from_geometry(geom)
    assert repr(geom) == before


@pytest.mark.parametrize("geom, match", [
    ([], "must be an object"),
    ({"prims": [], "origin": None, "datum": None}, "no Edge.Cuts outline"),
    ({"prims": "x", "origin": [0, 0]}, "prims"),
    ({"prims": [{"kind": "line", "start": [0, 0], "end": [1, 0]}], "origin": [0]}, "origin"),
    ({"prims": [{"kind": "line", "start": [0, 0], "end": "far"}], "origin": [0, 0]},
     "prims\\[0\\].end"),
    ({"prims": [7], "origin": [0, 0]}, "prims\\[0\\]"),
])
def test_outline_from_geometry_refuses_bad_input(geom: object, match: str) -> None:
    with pytest.raises(ol.OutlineError, match=match):
        ro.outline_from_geometry(geom)


def test_an_open_outline_names_the_shapes_outline_geometry_skipped() -> None:
    geom = {"prims": [{"kind": "line", "start": [0, 0], "end": [10, 0], "mid": None,
                       "center": None, "radius": None}],
            "origin": [0, 0], "datum": None, "skipped": 1}
    with pytest.raises(ol.OutlineError, match="open .*1 Edge.Cuts shape"):
        ro.outline_from_geometry(geom)


# ============================================================================
# path_outline_errors
# ============================================================================
def test_a_path_through_the_5mm_hole_is_flagged(board: Board) -> None:
    errors = ro.path_outline_errors(*board, [k(20, 20), k(40, 20)], 0.25, 0.3)
    assert len(errors) == 1
    assert "crosses cutout 2" in errors[0] and "[128.0, 135.0]" in errors[0]


def test_a_path_through_the_slot_and_one_inside_the_hole_are_flagged(board: Board) -> None:
    slot = ro.path_outline_errors(*board, [k(20, 9), k(40, 9)], 0.25, 0.3)
    assert len(slot) == 1 and "crosses cutout 1" in slot[0]
    inside = ro.path_outline_errors(*board, [k(29.5, 20), k(30.5, 20)], 0.25, 0.3)
    assert len(inside) == 1 and "inside cutout 2" in inside[0]


def test_a_path_0_25mm_from_the_outer_edge_is_flagged(board: Board) -> None:
    errors = ro.path_outline_errors(*board, [k(10, 0.25), k(50, 0.25)], 0.2, 0.3)
    assert len(errors) == 1
    assert "outline edge" in errors[0] and "0.15 mm" in errors[0] and "0.3 mm" in errors[0]


@pytest.mark.parametrize("y, flagged", [(0.35, True), (0.399, True), (0.401, False),
                                        (0.45, False)])
def test_edge_clearance_counts_from_the_copper_edge(board: Board, y: float, flagged: bool) -> None:
    """width 0.2: the copper edge is 0.1 mm off the centreline, so 0.3 mm of
    edge clearance needs the centreline >= 0.4 mm from the edge."""
    errors = ro.path_outline_errors(*board, [k(10, y), k(50, y)], 0.2, 0.3)
    assert bool(errors) is flagged


def test_a_clean_path_passes(board: Board) -> None:
    assert ro.path_outline_errors(*board, [k(5, 30), k(55, 30)], 0.25, 0.3) == []
    around = [k(5, 18), k(22, 18), k(26, 24), k(34, 24), k(38, 18), k(55, 18)]  # under the hole
    assert ro.path_outline_errors(*board, around, 0.25, 0.3) == []
    through = [k(5, 18), k(22, 18), k(26, 22), k(34, 22), k(38, 18), k(55, 18)]
    errors = ro.path_outline_errors(*board, through, 0.25, 0.3)
    assert len(errors) == 1 and errors[0].startswith("segment [126.5, 137.0] -> [134.5, 137.0]")


def test_copper_outside_a_rounded_corner_is_off_the_board(board: Board) -> None:
    errors = ro.path_outline_errors(*board, [k(0.3, 0.3), k(0.6, 0.3)], 0.2, 0.3)
    assert len(errors) == 1 and "outside the board" in errors[0]


def test_a_single_point_is_judged_as_a_round_pad(board: Board) -> None:
    near = ro.path_outline_errors(*board, [k(30, 23.0)], 0.6, 0.3)
    assert len(near) == 1 and near[0].startswith("copper at [130.5, 138.0]")
    assert "cutout 2" in near[0] and "0.2 mm" in near[0]
    assert ro.path_outline_errors(*board, [k(30, 24.0)], 0.6, 0.3) == []
    same = ro.path_outline_errors(*board, [k(30, 23.0), k(30, 23.0)], 0.6, 0.3)
    assert same == near


@pytest.mark.parametrize("args, match", [
    (([[1, 2]], -0.1, 0.3), "width"),
    (([[1, 2]], 0.2, -1), "edge_clearance"),
    (([], 0.2, 0.3), "at least one"),
    (([[1]], 0.2, 0.3), "points\\[0\\]"),
    (([[1, float("nan")]], 0.2, 0.3), "points\\[0\\]"),
])
def test_path_outline_errors_refuses_bad_input(board: Board, args: tuple, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        ro.path_outline_errors(*board, *args)


def test_bad_outline_or_origin_is_refused(board: Board) -> None:
    with pytest.raises(ValueError, match="outline_from_geometry"):
        ro.path_outline_errors({"prims": []}, (0, 0), [[1, 1]], 0.2, 0.3)
    with pytest.raises(ValueError, match="origin"):
        ro.edge_obstacles(board[0], (0,), 0.3, ["F.Cu"], [0, 0], [1, 1])


# ============================================================================
# edge_obstacles
# ============================================================================
def test_edge_obstacles_from_the_cutouts_block_the_corridor(board: Board) -> None:
    found = ro.edge_obstacles(*board, 0.3, ["F.Cu", "B.Cu"], k(20, 9), k(40, 9), 0.25)
    assert list(found) == ["F.Cu", "B.Cu"]
    assert found["F.Cu"] == [{"kind": "edge", "id": "cutout 1", "net": "", "distance": -0.3,
                              "t": 0.35}]
    assert found["B.Cu"] == found["F.Cu"] and found["B.Cu"] is not found["F.Cu"]


def test_edge_obstacles_grow_each_edge_by_the_edge_clearance(board: Board) -> None:
    a, b = k(20, 17), k(40, 17)                  # 3 mm under the slot, 0.5 mm over the hole
    assert ro.edge_obstacles(*board, 0.3, ["F.Cu"], a, b, 0.25) == {"F.Cu": []}
    wide = ro.edge_obstacles(*board, 0.3, ["F.Cu"], a, b, 0.5)["F.Cu"]
    assert wide == [{"kind": "edge", "id": "cutout 2", "net": "", "distance": 0.2, "t": 0.5}]


def test_edge_obstacles_on_the_outer_edge_and_inside_a_hole(board: Board) -> None:
    rim = ro.edge_obstacles(*board, 0.3, ["F.Cu"], k(10, 0.25), k(50, 0.25), 0.2)["F.Cu"]
    assert [(o["id"], o["distance"]) for o in rim] == [("outline", -0.05)]
    hole = ro.edge_obstacles(*board, 0.3, ["F.Cu"], k(29.5, 20), k(30.5, 20), 0.2)["F.Cu"]
    assert [o["id"] for o in hole] == ["cutout 2"] and hole[0]["distance"] < -0.3


# ============================================================================
# route_planner hooks
# ============================================================================
def pad(pid: str, net: str, at: list[float]) -> dict:
    return {"id": pid, "ref": "U1", "pad": pid, "net": net, "x": at[0], "y": at[1],
            "layers": ["F.Cu"], "through_hole": False, "shape": "rect", "w": 1.0, "h": 1.0,
            "rotation": 0.0, "drill": None}


CONN = {"net": "SIG", "from": pad("a", "SIG", k(20, 9)), "to": pad("b", "SIG", k(40, 9))}
FOREIGN = [pad("x", "GND", k(36, 9.5))]
CLASS = {"name": "Default", "track_width": 0.25}
RULES = {"min_track_width": 0.15, "copper_edge_clearance": 0.3}


def test_corridor_without_an_outline_is_unchanged() -> None:
    plain = rp.corridor(CONN, pads=FOREIGN, clearance=0.2, width=0.25)
    assert rp.corridor(CONN, pads=FOREIGN, clearance=0.2, width=0.25, outline=None) == plain
    assert [o["kind"] for o in plain["F.Cu"]] == ["pad"] and plain["B.Cu"] == []


def test_corridor_with_an_outline_adds_the_edges_in_order_of_t(board: Board) -> None:
    model, origin = board
    found = rp.corridor(CONN, pads=FOREIGN, clearance=0.2, width=0.25, outline=model,
                        origin=origin, edge_clearance=0.3)
    assert [(o["kind"], o["id"]) for o in found["F.Cu"]] == [("edge", "cutout 1"), ("pad", "x")]
    assert [(o["kind"], o["id"]) for o in found["B.Cu"]] == [("edge", "cutout 1")]


def test_validate_path_refuses_a_path_through_the_hole(board: Board) -> None:
    model, origin = board
    path = [{"layer": "F.Cu", "points": [k(20, 20), k(40, 20)]}]
    with pytest.raises(ValueError, match=r"path\[0\] on F.Cu: segment .* crosses cutout 2"):
        rp.validate_path(path, CLASS, RULES, outline=model, origin=origin)


def test_validate_path_with_a_clean_path_is_unchanged(board: Board) -> None:
    model, origin = board
    path = [{"layer": "F.Cu", "points": [k(5, 30), k(25, 30)]}, {"via": k(25, 30)},
            {"layer": "B.Cu", "points": [k(25, 30), k(55, 30)]}]
    plain = rp.validate_path(path, CLASS, RULES, via_diameter=0.6, via_drill=0.3)
    checked = rp.validate_path(path, CLASS, RULES, via_diameter=0.6, via_drill=0.3,
                               outline=model, origin=origin)
    assert checked == plain


def test_validate_path_judges_a_via_by_its_diameter(board: Board) -> None:
    model, origin = board
    path = [{"layer": "F.Cu", "points": [k(20, 23), k(30, 23)]}, {"via": k(30, 23)}]
    with pytest.raises(ValueError, match=r"path\[1\] via: copper at \[130.5, 138.0\]") as err:
        rp.validate_path(path, CLASS, RULES, via_diameter=0.6, via_drill=0.3, outline=model,
                         origin=origin)
    assert "path[0]" not in str(err.value)


def test_validate_path_judges_each_polyline_at_its_own_width(board: Board) -> None:
    """A 2-segment F.Cu run at the class width, a via, then a B.Cu run 0.45 mm
    from the bottom edge: at 0.4 mm wide its copper is 0.25 mm from the edge
    (flagged, and only path[2]); at the 0.25 mm class width it is 0.325 mm."""
    model, origin = board
    tail = [k(20, 35), k(24.55, 39.55), k(50, 39.55)]
    path = [{"layer": "F.Cu", "points": [k(5, 30), k(15, 30), k(20, 35)]}, {"via": k(20, 35)},
            {"layer": "B.Cu", "points": tail, "width": 0.4}]
    kw = {"via_diameter": 0.6, "via_drill": 0.3, "outline": model, "origin": origin}
    with pytest.raises(ValueError, match=r"path\[2\] on B.Cu: segment") as err:
        rp.validate_path(path, CLASS, RULES, **kw)
    assert "0.25 mm from the outline edge" in str(err.value)
    assert "path[0]" not in str(err.value) and "path[1]" not in str(err.value)
    at_class_width = [*path[:2], {"layer": "B.Cu", "points": tail}]
    assert len(rp.validate_path(at_class_width, CLASS, RULES, **kw)["segments"]) == 4


def test_validate_path_edge_clearance_defaults_to_the_board_rule(board: Board) -> None:
    model, origin = board
    rim = [{"layer": "F.Cu", "points": [k(10, 0.35), k(50, 0.35)], "width": 0.2}]
    with pytest.raises(ValueError, match="outline edge"):
        rp.validate_path(rim, CLASS, RULES, allow_narrow=True, outline=model, origin=origin)
    assert rp.validate_path(rim, CLASS, RULES, allow_narrow=True, outline=model, origin=origin,
                            edge_clearance=0.2)["segments"]
    no_rule = {"min_track_width": 0.15}
    assert rp.validate_path(rim, CLASS, no_rule, allow_narrow=True, outline=model,
                            origin=origin)["segments"]


# ============================================================================
# module contract
# ============================================================================
def test_route_outline_never_imports_kipy_or_pcbnew() -> None:
    tree = ast.parse((SERVER / "route_outline.py").read_text(encoding="utf-8"))
    names = {alias.name.split(".")[0] for node in ast.walk(tree)
             if isinstance(node, ast.Import) for alias in node.names}
    names |= {(node.module or "").split(".")[0] for node in ast.walk(tree)
              if isinstance(node, ast.ImportFrom)}
    assert not names & {"kipy", "pcbnew", "backends", "wx"}
