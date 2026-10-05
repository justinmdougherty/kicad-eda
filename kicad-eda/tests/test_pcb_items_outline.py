"""backends.pcb_items.outline_geometry — the exact Edge.Cuts outline of a kipy board.

Fakes follow tests/test_pcb_api.py: SimpleNamespace objects carrying the
attributes of the vendored kipy wrappers (start / mid / end, center /
radius_point, top_left / bottom_right, polygons, control1 / control2) in
nanometres; a second tier builds the same shapes from the real vendored kipy
classes so the attribute names are checked against the installed kipy. No
KiCad is needed.
"""
from __future__ import annotations

import math
from types import SimpleNamespace

import pytest

import outline as ol
import route_outline as ro
from backends import kipy_bridge as kb
from backends import pcb_items as items
from test_route_outline import T10_BOARD, assert_same_outline

pytestmark = pytest.mark.skipif(bool(kb.IMPORT_ERROR), reason=kb.IMPORT_ERROR or "")

EDGE = "Edge.Cuts"
DX, DY = 100.5, 115.0                    # where the T10 outline sits on the KiCad sheet
T10_AREA = 2400 - 4 * (16 - 4 * math.pi) - (24 + 9 * math.pi) - 6.25 * math.pi


# ============================================================================
# fakes (attributes mirror the kipy wrappers outline_geometry reads)
# ============================================================================
def vec(x_mm: float, y_mm: float) -> SimpleNamespace:
    return SimpleNamespace(x=int(round(x_mm * 1e6)), y=int(round(y_mm * 1e6)))


def kiid(value: str) -> SimpleNamespace:
    return SimpleNamespace(value=value)


def seg(x0: float, y0: float, x1: float, y1: float, layer: str = EDGE,
        ident: str = "") -> SimpleNamespace:
    return SimpleNamespace(id=kiid(ident), layer=items.layer_value(layer),
                           start=vec(x0, y0), end=vec(x1, y1))


def arc(start: tuple, mid: tuple, end: tuple, layer: str = EDGE) -> SimpleNamespace:
    return SimpleNamespace(id=kiid(""), layer=items.layer_value(layer), start=vec(*start),
                           mid=vec(*mid), end=vec(*end))


def circle(cx: float, cy: float, r: float, layer: str = EDGE) -> SimpleNamespace:
    return SimpleNamespace(id=kiid(""), layer=items.layer_value(layer), center=vec(cx, cy),
                           radius_point=vec(cx + r, cy))


def rect(x0: float, y0: float, x1: float, y1: float, radius_nm: int = 0) -> SimpleNamespace:
    return SimpleNamespace(id=kiid(""), layer=items.layer_value(EDGE), top_left=vec(x0, y0),
                           bottom_right=vec(x1, y1), corner_radius=radius_nm)


def node(x: float, y: float) -> SimpleNamespace:
    return SimpleNamespace(has_point=True, has_arc=False, point=vec(x, y))


def arc_node(start: tuple, mid: tuple, end: tuple) -> SimpleNamespace:
    return SimpleNamespace(has_point=False, has_arc=True,
                           arc=SimpleNamespace(start=vec(*start), mid=vec(*mid), end=vec(*end)))


def polygon(outline: list, *holes: list) -> SimpleNamespace:
    ring = lambda nodes: SimpleNamespace(nodes=list(nodes), closed=True)  # noqa: E731
    poly = SimpleNamespace(outline=ring(outline), holes=[ring(h) for h in holes])
    return SimpleNamespace(id=kiid(""), layer=items.layer_value(EDGE), polygons=[poly])


def footprint(*shapes) -> SimpleNamespace:
    return SimpleNamespace(definition=SimpleNamespace(shapes=list(shapes)))


class FakeBoard:
    """get_shapes / get_footprints / get_origin as kipy's Board answers them."""

    def __init__(self, shapes=(), footprints=(), origin=None) -> None:
        self.shapes, self.footprints, self.origin = list(shapes), list(footprints), origin
        self.origin_types: list[int] = []

    def get_shapes(self):
        return list(self.shapes)

    def get_footprints(self):
        return list(self.footprints)

    def get_origin(self, origin_type):
        self.origin_types.append(origin_type)
        if isinstance(self.origin, BaseException):
            raise self.origin
        return self.origin if self.origin is not None else vec(0, 0)


def fake_shape(prim: dict, dx: float, dy: float):
    """A fake kipy shape for one outline.to_kicad_primitives entry, moved by (dx, dy)."""
    at = lambda p: (p[0] + dx, p[1] + dy)  # noqa: E731
    if prim["kind"] == "line":
        return seg(*at(prim["start"]), *at(prim["end"]))
    if prim["kind"] == "arc":
        return arc(at(prim["start"]), at(prim["mid"]), at(prim["end"]))
    return circle(*at(prim["center"]), prim["radius"])


def t10_board(origin=None) -> FakeBoard:
    """T10 at (DX, DY): outer ring and the round hole drawn on the board, the
    stadium slot (arcs) carried inside a footprint, plus non-Edge.Cuts noise."""
    prims = ol.to_kicad_primitives(ol.from_board_block(T10_BOARD))
    board = [fake_shape(p, DX, DY) for p in prims if p["ring"] != 1]
    slot = [fake_shape(p, DX, DY) for p in prims if p["ring"] == 1]
    noise = [seg(0, 0, 500, 500, layer="F.SilkS"), circle(130, 135, 9, layer="F.Fab")]
    return FakeBoard(shapes=board + noise[:1], footprints=[footprint(*slot, noise[1])],
                     origin=origin)


# ============================================================================
# outline_geometry
# ============================================================================
def test_t10_geometry_carries_board_and_footprint_edges_and_the_aux_origin() -> None:
    board = t10_board(origin=vec(DX + 10, DY + 30))
    geom = items.outline_geometry(board)
    assert board.origin_types == [items.DRILL_ORIGIN]
    assert set(geom) == {"prims", "origin", "datum"}
    assert geom["origin"] == [DX, DY]
    assert geom["datum"] == [DX + 10, DY + 30]
    kinds = [p["kind"] for p in geom["prims"]]
    assert kinds.count("circle") == 1 and kinds.count("arc") == 6 and kinds.count("line") == 6
    footprint_arcs = [p for p in geom["prims"] if p["kind"] == "arc" and p["radius"] == 3.0]
    assert len(footprint_arcs) == 2                        # the slot came from the footprint
    assert footprint_arcs[0]["center"] in ([DX + 30, DY + 7], [DX + 30, DY + 11])
    model, origin = ro.outline_from_geometry(geom)
    assert origin == (DX, DY)
    assert ol.area(model) == pytest.approx(T10_AREA, abs=5e-3)     # 0.1 um rounding of mm
    assert_same_outline(model, ol.from_board_block(T10_BOARD), 1e-4)


def test_prim_dicts_are_from_kicad_primitives_input() -> None:
    geom = items.outline_geometry(t10_board())
    for prim in geom["prims"]:
        assert set(prim) == {"kind", "start", "mid", "end", "center", "radius"}
        if prim["kind"] == "line":
            assert prim["mid"] is None and prim["center"] is None
        if prim["kind"] == "arc":
            assert None not in (prim["start"], prim["mid"], prim["end"], prim["center"])
    assert geom["datum"] is None                           # (0, 0) = KiCad's unset default


def test_no_edge_cuts_gives_no_prims_and_no_origin() -> None:
    board = FakeBoard(shapes=[seg(0, 0, 10, 0, layer="F.Cu")])
    assert items.outline_geometry(board) == {"prims": [], "origin": None, "datum": None}


def test_rectangle_and_rounded_rectangle() -> None:
    plain = items.outline_geometry(FakeBoard(shapes=[rect(30, 25, 10, 5)]))
    assert [p["kind"] for p in plain["prims"]] == ["line"] * 4
    assert plain["origin"] == [10.0, 5.0]
    rounded = items.outline_geometry(FakeBoard(shapes=[rect(10, 5, 30, 25, radius_nm=2_000_000)]))
    kinds = [p["kind"] for p in rounded["prims"]]
    assert kinds.count("arc") == 4 and kinds.count("line") == 4
    model, _origin = ro.outline_from_geometry(rounded)
    assert ol.area(model) == pytest.approx(400 - 4 * (4 - math.pi), abs=1e-3)


def test_polygon_with_an_arc_node_and_a_hole() -> None:
    shape = polygon([node(0, 0), node(20, 0), arc_node((20, 0), (25, 5), (20, 10)), node(0, 10)],
                    [node(5, 3), node(8, 3), node(8, 6), node(5, 6)])
    geom = items.outline_geometry(FakeBoard(shapes=[shape]))
    kinds = [p["kind"] for p in geom["prims"]]
    assert kinds.count("arc") == 1 and kinds.count("line") == 7
    model, origin = ro.outline_from_geometry(geom)
    assert origin == (0.0, 0.0) and len(model.cutouts) == 1
    assert ol.area(model) == pytest.approx(200 + math.pi * 25 / 2 - 9, abs=1e-3)


def test_bezier_is_flattened_with_exact_end_points_and_an_ellipse_is_skipped() -> None:
    curve = SimpleNamespace(id=kiid(""), layer=items.layer_value(EDGE), start=vec(0, 0),
                            control1=vec(3, -6), control2=vec(7, -6), end=vec(10, 0))
    ellipse = SimpleNamespace(id=kiid(""), layer=items.layer_value(EDGE))   # a bare BoardShape
    geom = items.outline_geometry(FakeBoard(shapes=[curve, seg(10, 0, 0, 0), ellipse]))
    assert geom["skipped"] == 1
    lines = geom["prims"]
    assert all(p["kind"] == "line" for p in lines) and len(lines) > 8
    assert lines[0]["start"] == [0.0, 0.0] and lines[-2]["end"] == [10.0, 0.0]
    for p in lines[:-1]:                    # every vertex on the curve, every chord within 0.05
        t0, t1 = _bezier_param(p["start"][0]), _bezier_param(p["end"][0])
        assert p["end"][1] == pytest.approx(-18 * t1 * (1 - t1), abs=1e-3)
        tm = (t0 + t1) / 2
        apex = (9 * tm + 3 * tm ** 2 - 2 * tm ** 3, -18 * tm * (1 - tm))
        assert _distance_to_chord(apex, p["start"], p["end"]) <= ol.CHORD_ERROR_MM + 1e-4
    assert geom["origin"][1] == pytest.approx(-4.5, abs=0.05)
    model, _origin = ro.outline_from_geometry(geom)
    assert 30.6 - 0.4 < ol.area(model) < 30.6     # 18 * int t(1-t) x'(t) dt = 30.6, chords inside


def _distance_to_chord(p: tuple, a: list, b: list) -> float:
    (ax, ay), (bx, by) = a, b
    return abs((bx - ax) * (ay - p[1]) - (ax - p[0]) * (by - ay)) / math.hypot(bx - ax, by - ay)


def _bezier_param(x: float) -> float:
    """t of the test curve at x (x(t) = 9t + 3t^2 - 2t^3 is monotone on [0, 1])."""
    lo, hi = 0.0, 1.0
    for _ in range(60):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if 9 * mid + 3 * mid ** 2 - 2 * mid ** 3 < x else (lo, mid)
    return lo


def test_degenerate_arc_becomes_a_line_and_arc_bbox_is_exact() -> None:
    flat = items.outline_geometry(FakeBoard(shapes=[arc((0, 0), (5, 0), (10, 0))]))
    assert flat["prims"][0]["kind"] == "line"
    bulge = items.outline_geometry(FakeBoard(shapes=[arc((0, 0), (5, -5), (10, 0)),
                                                     seg(10, 0, 0, 0)]))
    assert bulge["origin"] == [0.0, -5.0]                  # the arc's apex, not its end points


def test_a_footprint_shape_already_listed_on_the_board_counts_once() -> None:
    shared = seg(0, 0, 10, 0, ident="kiid-1")
    board = FakeBoard(shapes=[shared, seg(10, 0, 10, 10), seg(10, 10, 0, 0)],
                      footprints=[footprint(shared)])
    assert len(items.outline_geometry(board)["prims"]) == 3


def test_origin_unreadable_on_an_old_kicad_is_a_note_other_errors_raise() -> None:
    unhandled = kb.ApiError("no handler available for request of type GetBoardOrigin",
                            code=kb.ApiStatusCode.AS_UNHANDLED)
    geom = items.outline_geometry(FakeBoard(shapes=[rect(0, 0, 5, 5)], origin=unhandled))
    assert geom["datum"] is None and "aux origin" in geom["notes"][0]
    busy = kb.ApiError("editor busy", code=kb.ApiStatusCode.AS_BUSY)
    with pytest.raises(kb.ApiError, match="busy"):
        items.outline_geometry(FakeBoard(shapes=[rect(0, 0, 5, 5)], origin=busy))
    no_getter = SimpleNamespace(get_shapes=lambda: [rect(0, 0, 5, 5)], get_footprints=lambda: [])
    assert items.outline_geometry(no_getter)["datum"] is None


def test_bbox_outline_is_unchanged() -> None:
    shapes = [seg(0, 0, 30, 0), seg(30, 0, 30, 25), seg(30, 25, 0, 25), seg(0, 25, 0, 0)]
    shapes = [SimpleNamespace(**vars(s), bounding_box=_box_of(s)) for s in shapes]
    assert items.outline(shapes) == {"bbox": [0.0, 0.0, 30.0, 25.0], "width": 30.0,
                                     "height": 25.0, "segments": 4}


def _box_of(shape):
    x0, x1 = sorted((shape.start.x, shape.end.x))
    y0, y1 = sorted((shape.start.y, shape.end.y))
    return lambda: SimpleNamespace(pos=SimpleNamespace(x=x0, y=y0),
                                   size=SimpleNamespace(x=x1 - x0, y=y1 - y0))


# ============================================================================
# the real vendored kipy wrappers
# ============================================================================
def test_real_kipy_shapes_and_a_footprint_internal_edge_arc() -> None:
    from kipy.board_types import (BoardArc, BoardCircle, BoardPolygon, BoardRectangle,
                                  BoardSegment, FootprintInstance)
    from kipy.geometry import PolygonWithHoles, PolyLine, PolyLineNode
    from kipy.proto.board.board_commands_pb2 import BoardOriginType

    assert items.DRILL_ORIGIN == BoardOriginType.BOT_DRILL

    def edge(shape):
        shape.layer = kb.BoardLayer.BL_Edge_Cuts
        return shape

    def line(x0, y0, x1, y1):
        shape = edge(BoardSegment())
        shape.start, shape.end = kb.vec(x0, y0), kb.vec(x1, y1)
        return shape

    right = edge(BoardArc())                               # (20, 0) -> (20, 10), bowing to x = 25
    right.start, right.mid, right.end = kb.vec(20, 0), kb.vec(25, 5), kb.vec(20, 10)
    outer = [line(0, 0, 20, 0), right, line(20, 10, 0, 10), line(0, 10, 0, 0)]
    triangle = edge(BoardPolygon())                        # a polygon is a closed ring itself
    poly, ring = PolygonWithHoles(), PolyLine()
    for x, y in ((14, 2), (18, 2), (18, 6)):
        ring.append(PolyLineNode.from_point(kb.vec(x, y)))
    poly.outline = ring
    triangle.polygons.append(poly)
    hole = edge(BoardCircle())
    hole.center, hole.radius_point = kb.vec(5, 5), kb.vec(6.5, 5)
    pocket = edge(BoardRectangle())                        # inside a footprint, rounded r = 1
    pocket.top_left, pocket.bottom_right = kb.vec(8, 3), kb.vec(12, 8)
    pocket.proto.shape.rectangle.corner_radius.value_nm = 1_000_000
    fp = FootprintInstance()
    fp.definition.add_item(pocket)
    board = SimpleNamespace(get_shapes=lambda: [*outer, triangle, hole],
                            get_footprints=lambda: [fp],
                            get_origin=lambda _t: kb.vec(2, 3))
    geom = items.outline_geometry(board)
    assert geom["origin"] == [0.0, 0.0] and geom["datum"] == [2.0, 3.0]
    assert sorted({p["kind"] for p in geom["prims"]}) == ["arc", "circle", "line"]
    model, _origin = ro.outline_from_geometry(geom)
    assert len(model.cutouts) == 3 and model.datum == (2.0, 3.0)
    area = 200 + math.pi * 25 / 2 - math.pi * 1.5 ** 2 - (16 + math.pi) - 8
    assert ol.area(model) == pytest.approx(area, abs=1e-3)
