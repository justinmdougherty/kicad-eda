"""Unit tests for server/tools/board_field.py: placement fields on arbitrary
board outlines (ROADMAP_DECISIONS D2, task F3-07).

* legacy_fields reproduces swig_build_board's MIXED-placement fields bit for bit
  (the numbers below are computed by hand with the builder's own formulas and
  compared with ==, never approx);
* auto_fields: legacy rect -> legacy_fields; explicit auto_* on an outline ->
  checked with contains_rect and clipped (with a warning) when they leave the
  board; otherwise the largest inscribed rectangle minus mounting-hole keepouts;
* snap_to_edge casts a ray along a connector's mouth and parks its courtyard
  edge on the board edge (minus clearance, or past it by the overhang).
"""
from __future__ import annotations

import ast
import dataclasses
import json
import math
import subprocess
import sys
from pathlib import Path

import pytest

import board_field as bf
import outline as ol
from conftest import load_example

TOOLS = Path(__file__).resolve().parents[1] / "server" / "tools"
MODULE = TOOLS / "board_field.py"
L_LOOP = [[0, 0], [32, 0], [32, 20], [60, 20], [60, 45], [0, 45]]
L_BOARD = {"outline": {"loop": L_LOOP}}
# T10 outline spike (the spec's board; the same block as tests/fixtures/swig_python.py's,
# kept here so this file depends on no other wave-2 task): 60 x 40, r 4, stadium, hole.
_R4 = 0.41421356237309503                        # bulge tan(90 deg / 4)
T10_BOARD = {"outline": {"loop": [[4, 0], [56, 0, _R4], [60, 4], [60, 36, _R4], [56, 40],
                                  [4, 40, _R4], [0, 36], [0, 4, _R4]]},
             "cutouts": [{"loop": [[27, 7], [27, 11, -1], [33, 11], [33, 7, -1]]},
                         {"circle": [30, 20, 2.5]}],
             "datum": [10, 30]}
U_BOARD = {"outline": {"loop": [[0, 0], [20, 0], [20, 25], [40, 25], [40, 0], [60, 0],
                                [60, 45], [0, 45]]}}
ROUND_BOARD = {"w": 50, "h": 30, "corner_radius": 6.0, "cutouts": [{"circle": [25, 15, 4]}]}
DISC_BOARD = {"outline": {"loop": [[0, 20, 1], [40, 20, 1]]}}     # r 20: two half arcs
EDGE_CLEARANCE = 0.3
INSCRIBED_CLEARANCE = EDGE_CLEARANCE + 0.5      # auto_fields case (3)


def _rect_board(w: float = 60.0, h: float = 40.0) -> ol.Outline:
    return ol.from_board_block({"w": w, "h": h})


def _legacy_by_hand(board: dict, board_w: float, board_h: float, scale: float,
                    has_conn: bool) -> dict[str, tuple]:
    """swig_build_board.py's mixed branch (HEAD lines 154-189), verbatim."""
    board_spec = board
    edge = 2.0 * scale
    edge_top = 1.2 if has_conn else edge
    auto_y = float(board_spec.get("auto_y", 0.0))
    y0 = edge_top + auto_y
    x0_spec = board_spec.get("auto_x")
    avail_w = float(board_spec.get("auto_w", board_w - 2 * edge))
    avail_h = float(board_spec.get("auto_h", board_h - y0 - edge))
    ab = board_spec.get("auto_b") or {}
    return {
        "F": (x0_spec, y0, avail_w, avail_h),
        "B": (ab.get("x", x0_spec), float(ab.get("y", y0)),
              float(ab.get("w", avail_w)), float(ab.get("h", avail_h))),
    }


def _assert_bit_identical(field: bf.Field, raw: tuple, board_w: float, side: str) -> None:
    sx0, sy0, s_w, s_h = raw
    assert field.side == side and field.source == "legacy"
    assert field.legacy == bf.LegacyRect(sx0, sy0, s_w, s_h, board_w)
    x0 = float(sx0) if sx0 is not None else (board_w - s_w) / 2.0
    assert (field.x0, field.y0, field.x1, field.y1) == (x0, sy0, x0 + s_w, sy0 + s_h)
    assert (field.w, field.h) == (s_w, s_h)


# --- legacy fields: bit-identical to the builder ------------------------------------------
def test_legacy_fields_rpi5_battery_hat_bit_identical() -> None:
    design = load_example("rpi5_battery_hat")
    board = design["board"]
    fixed = {c["ref"] for c in design["components"] if "x" in c and "y" in c}
    auto = [c for c in design["components"] if c["ref"] not in fixed]
    has_conn = any(c["ref"][:1] == "J" and not c.get("interior") for c in auto)
    board_w, board_h = float(board["w"]), float(board["h"])
    assert (board_w, board_h, has_conn) == (65.0, 56.5, False)
    raw = _legacy_by_hand(board, board_w, board_h, 1.0, has_conn)
    fields = bf.legacy_fields(board, board_w, board_h, scale=1.0, has_conn=has_conn)
    assert [f.side for f in fields] == ["F", "B"]
    _assert_bit_identical(fields[0], raw["F"], board_w, "F")
    _assert_bit_identical(fields[1], raw["B"], board_w, "B")
    # and the literal numbers (auto_x 8.5, auto_y 21 under a 2.0 top margin, ...)
    assert fields[0].rect == (8.5, 23.0, 31.5, 48.0)
    assert fields[1].rect == (30.0, 26.0, 55.0, 48.0)


@pytest.mark.parametrize("scale, has_conn", [(1.0, False), (1.0, True), (1.3, True),
                                             (1.15, False), (1.7, False)])
def test_legacy_fields_wh_only_block_bit_identical(scale: float, has_conn: bool) -> None:
    board = {"w": 50, "h": 30.7}
    board_w, board_h = float(board["w"]), float(board["h"])
    raw = _legacy_by_hand(board, board_w, board_h, scale, has_conn)
    fields = bf.legacy_fields(board, board_w, board_h, scale=scale, has_conn=has_conn)
    _assert_bit_identical(fields[0], raw["F"], board_w, "F")
    _assert_bit_identical(fields[1], raw["B"], board_w, "B")
    assert fields[0].rect == fields[1].rect          # no auto_b: B shares the F field


def test_legacy_fields_partial_auto_b_and_null_x() -> None:
    board = {"w": 40, "h": 30, "auto_x": None, "auto_y": 1.5,
             "auto_b": {"x": 3, "h": 9.25}}
    raw = _legacy_by_hand(board, 40.0, 30.0, 1.0, False)
    fields = bf.legacy_fields(board, 40, 30)
    _assert_bit_identical(fields[0], raw["F"], 40.0, "F")
    _assert_bit_identical(fields[1], raw["B"], 40.0, "B")
    assert fields[0].legacy.x0 is None and fields[1].legacy.x0 == 3


def test_cluster_origin_reproduces_the_builder_centring() -> None:
    board = load_example("rpi5_battery_hat")["board"]
    f_side, b_side = bf.legacy_fields(board, 65.0, 56.5)
    used_w = 17.3
    assert bf.cluster_origin(f_side, used_w) == (float(8.5) + (23.0 - used_w) / 2.0, 23.0)
    assert bf.cluster_origin(b_side, used_w) == (float(30.0) + (25.0 - used_w) / 2.0, 26.0)
    centred = bf.legacy_fields({"w": 50, "h": 30}, 50.0, 30.0, scale=1.3)[0]
    assert bf.cluster_origin(centred, used_w) == ((50.0 - used_w) / 2.0, 2.6)
    free = bf.Field(1.0, 2.0, 11.0, 7.0, "inscribed")
    assert bf.cluster_origin(free, 4.0) == (1.0 + (10.0 - 4.0) / 2.0, 2.0)


def test_fits_matches_the_builder_fit_check() -> None:
    f = bf.legacy_fields(load_example("rpi5_battery_hat")["board"], 65.0, 56.5)[0]
    assert bf.fits(f, 23.0, 25.0)                 # exactly the field: fits
    assert not bf.fits(f, 23.000001, 1.0)
    assert not bf.fits(f, 1.0, 25.000001)
    assert bf.fits(bf.Field(0, 0, 10, 5, "inscribed"), 10, 5)
    assert not bf.fits(bf.Field(0, 0, 10, 5, "inscribed"), 5, 10)


def test_field_is_frozen_validated_and_measured() -> None:
    f = bf.Field(1, 2, 11, 7, "inscribed", "B")
    assert (f.w, f.h, f.area, f.rect) == (10.0, 5.0, 50.0, (1.0, 2.0, 11.0, 7.0))
    assert isinstance(f.x0, float)
    with pytest.raises(dataclasses.FrozenInstanceError):
        f.x0 = 3.0  # type: ignore[misc]
    with pytest.raises(ValueError, match="side"):
        bf.Field(0, 0, 1, 1, "inscribed", "X")
    with pytest.raises(ValueError, match="finite"):
        bf.Field(0, math.nan, 1, 1, "inscribed")
    with pytest.raises(ValueError, match="finite"):
        bf.Field(0, True, 1, 1, "inscribed")
    assert bf.Field(0, 0, -1, 1, "legacy").area == 0.0      # degenerate: no area


@pytest.mark.parametrize("block, key", [({"auto_w": "23"}, "auto_w"),
                                        ({"auto_x": "8.5"}, "auto_x"),
                                        ({"auto_b": [1, 2]}, "auto_b"),
                                        ({"auto_b": {"y": None}}, "auto_b.y")])
def test_legacy_fields_reject_non_numbers(block: dict, key: str) -> None:
    with pytest.raises(ValueError, match=key.replace(".", r"\.")):
        bf.legacy_fields({"w": 50, "h": 30, **block}, 50.0, 30.0)


def test_bad_board_block_and_scale_are_refused() -> None:
    with pytest.raises(ValueError, match="board block must be an object"):
        bf.auto_fields([60, 40], None, EDGE_CLEARANCE)
    for scale in (0.0, -1.0):
        with pytest.raises(ValueError, match="scale must be > 0"):
            bf.legacy_fields({"w": 50, "h": 30}, 50.0, 30.0, scale=scale)


# --- auto_fields (1): legacy boards ---------------------------------------------------------
def test_auto_fields_delegates_legacy_rects_to_legacy_fields() -> None:
    board = load_example("rpi5_battery_hat")["board"]
    want = bf.legacy_fields(board, 65.0, 56.5, scale=1.2, has_conn=True)
    for outline in (None, ol.from_board_block(board)):
        fields, warnings = bf.auto_fields(board, outline, EDGE_CLEARANCE,
                                          [(3.5, 3.5, 2.7)], scale=1.2, has_conn=True)
        assert fields == want and warnings == []


def test_auto_fields_auto_sized_board_has_no_field() -> None:
    assert bf.auto_fields({}, None, EDGE_CLEARANCE) == ([], [])
    assert bf.auto_fields(None, None, EDGE_CLEARANCE) == ([], [])
    assert bf.auto_fields({"auto_y": 3.0}, None, EDGE_CLEARANCE) == ([], [])


def test_auto_fields_outline_none_builds_the_outline_from_the_block() -> None:
    fields, warnings = bf.auto_fields(L_BOARD, None, EDGE_CLEARANCE)
    assert warnings == [] and fields[0].source == "inscribed"
    assert fields[0].rect == bf.auto_fields(L_BOARD, ol.from_board_block(L_BOARD),
                                            EDGE_CLEARANCE)[0][0].rect


@pytest.mark.parametrize("block, legacy", [({}, True), ({"w": 60, "h": 40}, True),
                                           ({"w": 60, "h": 40, "auto_y": 3}, True),
                                           ({"w": 60, "h": 40, "cutouts": []}, True),
                                           ({"w": 60, "h": 40, "corner_radius": 3}, False),
                                           ({"w": 60, "h": 40, "datum": [1, 2]}, False),
                                           (L_BOARD, False), (T10_BOARD, False)])
def test_is_legacy_block(block: dict, legacy: bool) -> None:
    assert bf.is_legacy_block(block) is legacy


@pytest.mark.parametrize("block", [{"w": 60, "h": 40}, {"w": 60, "h": 40, "auto_y": 3},
                                   {"w": 60, "h": 40, "cutouts": []},
                                   {"w": 60, "h": 40, "corner_radius": 3},
                                   {"w": 60, "h": 40, "datum": [1, 2]}, L_BOARD, T10_BOARD])
def test_is_legacy_block_agrees_with_outline_resolve(block: dict) -> None:
    """Cross-check with F3-06's resolver when it is present (a peer wave-2a module,
    not a dependency of this task: the test skips cleanly without it)."""
    orz = pytest.importorskip("outline_resolve")
    assert orz.resolve_board({"board": block}, None).is_legacy is bf.is_legacy_block(block)


def test_t10_board_matches_the_shared_fixture_when_present() -> None:
    shared = pytest.importorskip("fixtures.swig_python")     # F3-01's live-test fixture
    assert shared.T10_BOARD == T10_BOARD


def test_auto_fields_never_mutates_its_inputs() -> None:
    blocks = [load_example("rpi5_battery_hat")["board"], dict(T10_BOARD),
              {**L_BOARD, "auto_x": 30.0, "auto_y": 3.0, "auto_w": 20.0, "auto_h": 20.0}]
    holes = [[4.0, 4.0, 3.2]]
    for block in blocks:
        before = json.dumps(block, sort_keys=True)
        first = bf.auto_fields(block, None, EDGE_CLEARANCE, holes)
        assert json.dumps(block, sort_keys=True) == before and holes == [[4.0, 4.0, 3.2]]
        assert bf.auto_fields(block, None, EDGE_CLEARANCE, holes) == first   # deterministic


@pytest.mark.parametrize("clearance", [-0.1, math.inf, math.nan, True])
def test_auto_fields_rejects_a_bad_clearance(clearance: float) -> None:
    with pytest.raises(ValueError, match="clearance"):
        bf.auto_fields(L_BOARD, None, clearance)


# --- auto_fields (3): largest inscribed rectangle --------------------------------------------
def _largest_block(free: list[list[bool]]) -> int:
    """Brute force: the most cells in a rectangular block of free cells, over every
    pair of rows."""
    best, nx = 0, len(free[0]) if free else 0
    for top in range(len(free)):
        cols = [True] * nx
        for bottom in range(top, len(free)):
            cols = [c and f for c, f in zip(cols, free[bottom], strict=True)]
            run = longest = 0
            for c in cols:
                run = run + 1 if c else 0
                longest = max(longest, run)
            best = max(best, longest * (bottom - top + 1))
    return best


def _grid_optimum(o: ol.Outline, clearance: float, step: float = 0.5) -> float:
    """Brute force: the largest union of 0.5 mm grid cells, each on the board
    with `clearance` (exact contains_rect; interior cells decided by the
    centre's distance)."""
    x0, y0, x1, y1 = ol.bbox(o)
    nx, ny = round((x1 - x0) / step), round((y1 - y0) / step)
    half_diag = step * math.sqrt(2.0) / 2.0
    free = []
    for j in range(ny):
        row = []
        for i in range(nx):
            cx, cy = x0 + (i + 0.5) * step, y0 + (j + 0.5) * step
            if ol.contains(o, (cx, cy), clearance + half_diag):
                row.append(True)
            elif not ol.contains(o, (cx, cy), clearance):
                row.append(False)
            else:
                row.append(ol.contains_rect(o, (cx - step / 2, cy - step / 2,
                                                cx + step / 2, cy + step / 2), clearance))
        free.append(row)
    return _largest_block(free) * step * step


def _grid_best_inside(o: ol.Outline, req: tuple, clearance: float,
                      step: float = 0.5) -> float:
    """Brute force for a clip: the largest block of `step` cells anchored at req's
    top-left corner, each inside req and on the board (exact contains_rect)."""
    x0, y0, x1, y1 = req
    nx, ny = int((x1 - x0) / step), int((y1 - y0) / step)
    free = [[ol.contains_rect(o, (x0 + i * step, y0 + j * step, x0 + (i + 1) * step,
                                  y0 + (j + 1) * step), clearance) for i in range(nx)]
            for j in range(ny)]
    return _largest_block(free) * step * step


@pytest.mark.parametrize("block", [T10_BOARD, L_BOARD], ids=["T10", "L"])
def test_inscribed_field_is_contained_and_near_the_grid_optimum(block: dict) -> None:
    o = ol.from_board_block(block)
    fields, warnings = bf.auto_fields(block, o, EDGE_CLEARANCE)
    assert warnings == []
    assert [(f.side, f.source) for f in fields] == [("F", "inscribed"), ("B", "inscribed")]
    assert fields[0].rect == fields[1].rect
    assert fields[0].rect == ol.largest_inscribed_rect(o, clearance=INSCRIBED_CLEARANCE)
    assert ol.contains_rect(o, fields[0].rect, INSCRIBED_CLEARANCE)
    assert fields[0].area >= 0.9 * _grid_optimum(o, INSCRIBED_CLEARANCE)


def test_inscribed_field_on_the_l_shape_is_the_bottom_band() -> None:
    fields, _ = bf.auto_fields(L_BOARD, ol.from_board_block(L_BOARD), EDGE_CLEARANCE)
    assert fields[0].rect == pytest.approx((0.8, 20.8, 59.2, 44.2), abs=1e-6)


def _boxes_overlap(a: tuple, b: tuple) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


@pytest.mark.parametrize("holes", [[(4, 4, 3.2), (56, 4, 3.2), (56, 36, 3.2), (4, 36, 3.2)],
                                   [(4, 4), (56, 4), (56, 36), (4, 36)]])
def test_mounting_hole_keepouts_are_subtracted(holes: list) -> None:
    block = {"w": 60, "h": 40, "corner_radius": 3.0}
    o = ol.from_board_block(block)
    rx0, ry0, rx1, ry1 = ol.largest_inscribed_rect(o, clearance=INSCRIBED_CLEARANCE)
    k = 3.2 / 2 + 1.0
    tall, wide = (4 + k, ry0, 56 - k, ry1), (rx0, 4 + k, rx1, 36 - k)
    want = max((tall, wide), key=lambda r: (r[2] - r[0]) * (r[3] - r[1]))
    fields, warnings = bf.auto_fields(block, o, EDGE_CLEARANCE, holes)
    assert warnings == [] and len(fields) == 2
    assert fields[0].rect == pytest.approx(want, abs=1e-9)
    for hx, hy, *_ in holes:
        assert not _boxes_overlap(fields[0].rect, (hx - k, hy - k, hx + k, hy + k))
    assert ol.contains_rect(o, fields[0].rect, INSCRIBED_CLEARANCE)


def test_a_hole_inside_the_field_keeps_the_largest_remaining_piece() -> None:
    block = {"w": 40, "h": 20, "corner_radius": 1.0}
    o = ol.from_board_block(block)
    fields, _ = bf.auto_fields(block, o, EDGE_CLEARANCE, [(12.0, 10.0, 2.0)])
    rx0, ry0, rx1, ry1 = ol.largest_inscribed_rect(o, clearance=INSCRIBED_CLEARANCE)
    assert fields[0].rect == pytest.approx((14.0, ry0, rx1, ry1), abs=1e-9)


@pytest.mark.parametrize("holes", [[(1, 2, 3, 4)], [("a", 1)], [(1.0, 2.0, -3.0)], [3.0]])
def test_bad_mounting_holes_are_refused(holes: list) -> None:
    with pytest.raises(ValueError, match="mounting hole"):
        bf.auto_fields(L_BOARD, None, EDGE_CLEARANCE, holes)


def test_no_field_fits_inside_a_tiny_outline() -> None:
    block = {"w": 1.5, "h": 1.5, "corner_radius": 0.1}
    fields, warnings = bf.auto_fields(block, ol.from_board_block(block), EDGE_CLEARANCE)
    assert fields == [] and len(warnings) == 1
    assert warnings[0].startswith("no placement field fits inside the outline")


def test_no_field_fits_once_the_holes_take_everything() -> None:
    block = {"w": 20, "h": 20, "corner_radius": 1.0}
    fields, warnings = bf.auto_fields(block, None, EDGE_CLEARANCE, [(10, 10, 20)])
    assert fields == [] and len(warnings) == 1
    assert warnings[0].startswith(bf.NO_FIELD) and "mounting" in warnings[0]


# --- auto_fields (2): explicit auto_* on an outline board -------------------------------------
def test_explicit_fields_inside_the_outline_are_kept_bit_identical() -> None:
    block = {**T10_BOARD, "auto_x": 3.0, "auto_y": 12.0, "auto_w": 20.0, "auto_h": 20.0}
    fields, warnings = bf.auto_fields(block, ol.from_board_block(block), EDGE_CLEARANCE)
    raw = _legacy_by_hand(block, 60.0, 40.0, 1.0, False)
    assert warnings == [] and [(f.side, f.source) for f in fields] == [("F", "explicit"),
                                                                       ("B", "explicit")]
    for f, side in zip(fields, "FB", strict=True):
        assert f.legacy == bf.LegacyRect(*raw[side], 60.0)
        assert f.rect == (3.0, 14.0, 23.0, 34.0)


def test_explicit_field_crossing_the_bay_is_warned_and_clipped() -> None:
    block = {**L_BOARD, "auto_x": 30.0, "auto_y": 3.0, "auto_w": 20.0, "auto_h": 20.0,
             "auto_b": {"x": 2.0, "y": 25.0, "w": 20.0, "h": 15.0}}
    o = ol.from_board_block(block)
    fields, warnings = bf.auto_fields(block, o, EDGE_CLEARANCE)
    by_side = {f.side: f for f in fields}
    assert by_side["F"].source == "clipped" and by_side["F"].legacy is None
    assert by_side["B"].source == "explicit" and by_side["B"].rect == (2.0, 25.0, 22.0, 40.0)
    assert len(warnings) == 1 and "F" in warnings[0] and "clipped" in warnings[0]
    clipped = by_side["F"].rect
    assert ol.contains_rect(o, clipped, EDGE_CLEARANCE)
    assert 30.0 <= clipped[0] < clipped[2] <= 50.0 and 5.0 <= clipped[1] < clipped[3] <= 25.0
    # the best piece of (30..50) x (5..25) on the L is the strip under the bay
    assert clipped == pytest.approx((30.0, 20.3, 50.0, 25.0), abs=1e-3)


def _explicit(block: dict, req: tuple) -> dict:
    """`block` with explicit auto_* asking for the F field `req` (scale 1, no flush
    J*: the field top is the 2.0 mm legacy margin + auto_y). No auto_b: B = F."""
    x0, y0, x1, y1 = req
    return {**block, "auto_x": x0, "auto_y": y0 - 2.0, "auto_w": x1 - x0, "auto_h": y1 - y0}


# Requests whose on-board part is awkward to find (from the F3-07 review fuzz): a strip
# beside an r4 corner, sub-millimetre slivers along a straight edge, a notch, a hole.
CLIP_CASES = {
    "T10 r4 corner strip": (T10_BOARD, (57.17, 17.13, 85.47, 41.86)),
    "U floor sliver": (U_BOARD, (31.37, 44.16, 57.05, 50.18)),
    "round floor sliver": (ROUND_BOARD, (3.22, 29.17, 21.62, 47.58)),
    "U notch": (U_BOARD, (10.0, 10.0, 50.0, 40.0)),
    "round hole": (ROUND_BOARD, (15.0, 5.0, 35.0, 25.0)),
    "L bay corner": (L_BOARD, (25.0, 10.0, 45.0, 30.0)),
    "T10 stadium": (T10_BOARD, (24.0, 4.0, 36.0, 14.0)),
}


@pytest.mark.parametrize("block, req", list(CLIP_CASES.values()), ids=list(CLIP_CASES))
def test_clipped_explicit_field_is_near_the_best_on_board_part(block: dict, req: tuple) -> None:
    o = ol.from_board_block(block)
    fields, warnings = bf.auto_fields(_explicit(block, req), o, EDGE_CLEARANCE)
    assert [(f.side, f.source) for f in fields] == [("F", "clipped"), ("B", "clipped")]
    assert len(warnings) == 2 and all("clipped to" in w for w in warnings)
    got = fields[0]
    assert ol.contains_rect(o, got.rect, EDGE_CLEARANCE)
    assert req[0] - 1e-9 <= got.x0 < got.x1 <= req[2] + 1e-9
    assert req[1] - 1e-9 <= got.y0 < got.y1 <= req[3] + 1e-9
    assert got.area >= 0.9 * _grid_best_inside(o, req, EDGE_CLEARANCE)


def test_clip_is_exact_along_straight_edges() -> None:
    """Grid lines through every straight-edge vertex (+- clearance) make the clip of a
    rectilinear board exact: the 0.54 mm floor sliver under the U is found whole."""
    req = CLIP_CASES["U floor sliver"][1]
    fields, _ = bf.auto_fields(_explicit(U_BOARD, req), None, EDGE_CLEARANCE)
    assert fields[0].rect == pytest.approx((req[0], req[1], req[2], 45.0 - EDGE_CLEARANCE),
                                           abs=1e-4)


@pytest.mark.parametrize("scale", [3.0, 6.0])
@pytest.mark.parametrize("block", [ROUND_BOARD, DISC_BOARD, T10_BOARD],
                         ids=["round", "disc", "T10"])
def test_clip_of_a_request_larger_than_the_board_ignores_the_overhang(block: dict,
                                                                     scale: float) -> None:
    """The clip grid covers only the part of the request the board can reach, so a request
    `scale` x the board clips exactly like one the size of the board (review round 2)."""
    o = ol.from_board_block(block)
    x0, y0, x1, y1 = ol.bbox(o)
    cx, cy, hw, hh = (x0 + x1) / 2, (y0 + y1) / 2, (x1 - x0) / 2, (y1 - y0) / 2
    big = (cx - scale * hw, cy - scale * hh, cx + scale * hw, cy + scale * hh)
    fields, warnings = bf.auto_fields(_explicit(block, big), o, EDGE_CLEARANCE)
    same, _ = bf.auto_fields(_explicit(block, (x0, y0, x1, y1)), o, EDGE_CLEARANCE)
    assert [f.source for f in fields] == ["clipped", "clipped"] and len(warnings) == 2
    assert fields[0].rect == pytest.approx(same[0].rect, abs=1e-9)
    assert ol.contains_rect(o, fields[0].rect, EDGE_CLEARANCE)
    assert fields[0].area >= 0.9 * _grid_optimum(o, EDGE_CLEARANCE)


@pytest.mark.parametrize("auto_b", [{"x": 40.0, "y": 2.0, "w": 10.0, "h": 10.0},    # the bay
                                    {"x": 70.0, "y": 50.0, "w": 10.0, "h": 10.0}],  # past bbox
                         ids=["in the bay", "outside the bbox"])
def test_explicit_field_fully_off_the_board_is_dropped_with_a_warning(auto_b: dict) -> None:
    block = {**L_BOARD, "auto_b": auto_b}
    o = ol.from_board_block(block)
    fields, warnings = bf.auto_fields(block, o, EDGE_CLEARANCE)
    assert [f.side for f in fields] == ["F"] and fields[0].source == "clipped"
    assert ol.contains_rect(o, fields[0].rect, EDGE_CLEARANCE)
    assert len(warnings) == 2 and "B" in warnings[1] and "no usable part" in warnings[1]


def test_explicit_field_without_area_is_dropped_with_a_warning() -> None:
    block = {**T10_BOARD, "auto_w": 0.0, "auto_b": {"x": 3.0, "w": 10.0}}
    fields, warnings = bf.auto_fields(block, ol.from_board_block(block), EDGE_CLEARANCE)
    assert [f.side for f in fields] == ["B"]
    assert len(warnings) == 1 and "F" in warnings[0] and "no area" in warnings[0]


# --- snap_to_edge ----------------------------------------------------------------------------
@pytest.mark.parametrize("mouth, want", [("right", (57.75, 20.0)), ((1, 0), (57.75, 20.0)),
                                         ("left", (2.25, 20.0)), ((-2.0, 0.0), (2.25, 20.0)),
                                         ("top", (30.0, 2.25)), ((0, -3), (30.0, 2.25)),
                                         ("bottom", (30.0, 37.75)), ((0, 1), (30.0, 37.75))])
def test_snap_to_edge_on_a_rect_in_four_directions(mouth: object, want: tuple) -> None:
    got = bf.snap_to_edge(_rect_board(), (30.0, 20.0), mouth, 2.0, clearance=0.25)
    assert got == pytest.approx(want, abs=1e-9)


def test_snap_to_edge_overhang_puts_the_courtyard_past_the_edge() -> None:
    got = bf.snap_to_edge(_rect_board(), (30.0, 20.0), "right", 2.0, overhang=1.5,
                          clearance=0.25)
    assert got == pytest.approx((59.5, 20.0), abs=1e-9)
    got = bf.snap_to_edge(_rect_board(), (30.0, 20.0), "top", 3.0, overhang=4.0)
    assert got == pytest.approx((30.0, -1.0), abs=1e-9)     # courtyard top at y = -4


def test_snap_to_edge_on_the_l_bay_edge() -> None:
    o = ol.from_board_block(L_BOARD)
    assert bf.snap_to_edge(o, (20.0, 10.0), "right", 3.0, clearance=0.5) == \
        pytest.approx((28.5, 10.0), abs=1e-9)                     # the bay's x = 32 edge
    assert bf.snap_to_edge(o, (20.0, 30.0), "right", 3.0, clearance=0.5) == \
        pytest.approx((56.5, 30.0), abs=1e-9)                     # below the bay: x = 60
    assert bf.snap_to_edge(o, (45.0, 35.0), "top", 2.0) == \
        pytest.approx((45.0, 22.0), abs=1e-9)                     # the bay floor y = 20


def test_snap_to_edge_on_a_rounded_corner() -> None:
    o = ol.from_board_block(T10_BOARD)           # r = 4 corners, arc centre (4, 4)
    on_arc = 4.0 - math.sqrt(16.0 - 4.0)         # where x = 2 (or y = 2) meets the arc
    assert bf.snap_to_edge(o, (2.0, 15.0), "top", 1.0, clearance=0.2) == \
        pytest.approx((2.0, on_arc + 1.2), abs=1e-9)
    assert bf.snap_to_edge(o, (15.0, 2.0), "left", 1.0, clearance=0.2) == \
        pytest.approx((on_arc + 1.2, 2.0), abs=1e-9)


def test_snap_to_edge_returns_none_without_an_edge_ahead() -> None:
    rect = _rect_board()
    assert bf.snap_to_edge(rect, (70.0, 20.0), "right", 2.0) is None     # the ray misses
    assert bf.snap_to_edge(rect, (-10.0, 20.0), "right", 2.0) is None    # enters the board
    t10 = ol.from_board_block(T10_BOARD)
    assert bf.snap_to_edge(t10, (30.0, 20.0), "right", 1.0) is None      # inside the hole


@pytest.mark.parametrize("kwargs, match", [({"mouth_dir": (0.0, 0.0)}, "mouth"),
                                           ({"mouth_dir": "up"}, "mouth"),
                                           ({"mouth_dir": (1, 2, 3)}, "mouth"),
                                           ({"half_extent_along_dir": -1.0}, "half_extent"),
                                           ({"overhang": -0.5}, "overhang"),
                                           ({"clearance": math.nan}, "clearance"),
                                           ({"center": (1.0,)}, "center")])
def test_snap_to_edge_rejects_bad_input(kwargs: dict, match: str) -> None:
    args = {"outline": _rect_board(), "center": (30.0, 20.0), "mouth_dir": "right",
            "half_extent_along_dir": 2.0, **kwargs}
    with pytest.raises(ValueError, match=match):
        bf.snap_to_edge(**args)


# --- module constraints -----------------------------------------------------------------------
def test_module_is_small_stdlib_only_and_python311() -> None:
    src = MODULE.read_text(encoding="utf-8")
    assert len(src.splitlines()) < 350
    tree = ast.parse(src, feature_version=(3, 11))
    imported = {alias.name.split(".")[0] for node in ast.walk(tree)
                if isinstance(node, ast.Import) for alias in node.names}
    imported |= {node.module.split(".")[0] for node in ast.walk(tree)
                 if isinstance(node, ast.ImportFrom) and node.module}
    assert "pcbnew" not in imported
    assert imported <= set(sys.stdlib_module_names) | {"__future__", "outline"}, imported


_LIVE = """import sys
sys.path.insert(0, sys.argv[1])
import kicad_quiet  # noqa: F401  (KiCad-python rule; no pcbnew is imported here)
import json
import board_field as bf
import outline as ol
t10, hat = json.loads(sys.argv[2]), json.loads(sys.argv[3])
fields, warnings = bf.auto_fields(t10, ol.from_board_block(t10), 0.3)
legacy = bf.legacy_fields(hat, 65.0, 56.5, scale=1.3, has_conn=True)
snap = bf.snap_to_edge(ol.from_board_block(t10), (2.0, 15.0), "top", 1.0, clearance=0.2)
print(json.dumps({"py": list(sys.version_info[:2]), "fields": [f.rect for f in fields],
                  "warnings": warnings, "legacy": [f.rect for f in legacy], "snap": snap,
                  "pcbnew": "pcbnew" in sys.modules}))
"""


def test_live_imports_under_the_oldest_kicad_python() -> None:
    """swig_build_board (KiCad 9.0.4's bundled Python 3.11) imports board_field:
    same numbers as the venv, no pcbnew pulled in."""
    import kicad_env as env
    found = [i for i in env.discover() if i.python is not None]
    if not found:
        pytest.skip(f"no KiCad python with pcbnew: {env.probe_errors()}")
    kpy = str(min(found, key=lambda i: i.version_tuple).python)
    hat = load_example("rpi5_battery_hat")["board"]
    proc = subprocess.run([kpy, "-c", _LIVE, str(TOOLS), json.dumps(T10_BOARD), json.dumps(hat)],
                          capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=180, stdin=subprocess.DEVNULL)
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-2000:]
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    fields, warnings = bf.auto_fields(T10_BOARD, ol.from_board_block(T10_BOARD), 0.3)
    assert out["pcbnew"] is False and out["warnings"] == warnings
    assert [tuple(r) for r in out["fields"]] == pytest.approx([f.rect for f in fields], abs=1e-9)
    legacy = bf.legacy_fields(hat, 65.0, 56.5, scale=1.3, has_conn=True)
    assert [tuple(r) for r in out["legacy"]] == [f.rect for f in legacy]
    assert tuple(out["snap"]) == pytest.approx(
        bf.snap_to_edge(ol.from_board_block(T10_BOARD), (2.0, 15.0), "top", 1.0,
                        clearance=0.2), abs=1e-12)
