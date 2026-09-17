"""place_engine — the pure geometry half (shelf_pack / functional_place).

place_engine imports pcbnew at module level for fp_extent/fp_mouth, which are
not exercised here. When pcbnew is not importable (the dev venv, CI) a stub
module is registered for the duration of the place_engine import ONLY and is
removed again afterwards, so it never leaks into other test modules that expect
`import pcbnew` to fail (board_shapes, emc_stitch, silk_place, _kicad_python).
"""
from __future__ import annotations

import importlib
import sys
import types

import pytest


def _import_place_engine() -> types.ModuleType:
    """Import place_engine; when pcbnew is absent, stub it for this import only."""
    try:
        import pcbnew  # noqa: F401  (KiCad's bundled python only)
    except ImportError:
        pass
    else:
        return importlib.import_module("place_engine")
    stub = types.ModuleType("pcbnew")
    sys.modules["pcbnew"] = stub
    try:
        return importlib.import_module("place_engine")
    finally:
        if sys.modules.get("pcbnew") is stub:
            del sys.modules["pcbnew"]


pe = _import_place_engine()


def test_pcbnew_stub_does_not_leak_into_sys_modules():
    # a real pcbnew has __file__; the bare ModuleType stub used above has none
    cached = sys.modules.get("pcbnew")
    assert cached is None or getattr(cached, "__file__", None), (
        "bare pcbnew stub leaked into sys.modules")


def _boxes(pos: dict, sizes: dict) -> dict:
    """{ref: (x0, y0, x1, y1)} from bbox-centre positions and (w, h) sizes."""
    return {r: (cx - sizes[r][0] / 2, cy - sizes[r][1] / 2,
                cx + sizes[r][0] / 2, cy + sizes[r][1] / 2) for r, (cx, cy) in pos.items()}


def _overlaps(a, b, eps=1e-6) -> bool:
    return not (a[2] <= b[0] + eps or b[2] <= a[0] + eps
                or a[3] <= b[1] + eps or b[3] <= a[1] + eps)


def _assert_no_overlap(pos: dict, sizes: dict) -> None:
    boxes = _boxes(pos, sizes)
    refs = list(boxes)
    for i, a in enumerate(refs):
        for b in refs[i + 1:]:
            assert not _overlaps(boxes[a], boxes[b]), f"{a} overlaps {b}"


# --- shelf_pack ------------------------------------------------------------------

def test_shelf_pack_known_layout_with_backfill():
    pos, w, h = pe.shelf_pack([("A", 4, 2), ("B", 2, 2), ("C", 6, 3)],
                              margin=1.0, row_limit=8.0)
    # C (tallest) opens row 0; A does not fit beside it (6+1+4 > 8) -> row 1 at
    # y = 3 + 1; B backfills row 1 (5 + 2 <= 8, h 2 <= 2) instead of a new row
    assert pos == {"C": (3.0, 1.5), "A": (2.0, 5.0), "B": (6.0, 5.0)}
    assert (w, h) == (7.0, 6.0)


def test_shelf_pack_empty():
    assert pe.shelf_pack([]) == ({}, 0.0, 0.0)


def test_shelf_pack_single_item_sits_at_origin_corner():
    pos, w, h = pe.shelf_pack([("U1", 5.0, 3.0)])
    assert pos == {"U1": (2.5, 1.5)} and (w, h) == (5.0, 3.0)


def test_shelf_pack_rows_respect_limit_and_never_overlap():
    sizes = [(f"P{i}", 1 + (i * 7) % 5, 1 + (i * 3) % 4) for i in range(40)]
    pos, w, h = pe.shelf_pack(sizes, margin=0.5, row_limit=12.0)
    assert set(pos) == {r for r, _, _ in sizes}
    assert w <= 12.0 and h > 0
    _assert_no_overlap(pos, {r: (sw, sh) for r, sw, sh in sizes})
    assert all(cx - sw / 2 >= -1e-9 and cy - sh / 2 >= -1e-9
               for (r, sw, sh) in sizes for cx, cy in [pos[r]])


def test_shelf_pack_extent_is_content_true():
    sizes = [("A", 3, 2), ("B", 3, 2)]
    pos, w, h = pe.shelf_pack(sizes, margin=1.0, row_limit=10.0)
    boxes = _boxes(pos, {r: (sw, sh) for r, sw, sh in sizes})
    assert w == pytest.approx(max(b[2] for b in boxes.values()))
    assert h == pytest.approx(max(b[3] for b in boxes.values()))
    assert min(b[0] for b in boxes.values()) == 0.0
    assert w == 7.0 and h == 2.0                       # 3 + 1 + 3 wide, one row


def test_shelf_pack_tallest_first_orders_rows():
    pos, _, _ = pe.shelf_pack([("short", 2, 1), ("tall", 2, 5)], margin=0.0, row_limit=2.0)
    assert pos["tall"][1] < pos["short"][1]


def test_shelf_pack_aspect_derives_row_limit_when_unspecified():
    sizes = [(f"P{i}", 2.0, 2.0) for i in range(16)]
    _, w_wide, h_wide = pe.shelf_pack(sizes, margin=0.0, aspect=4.0)
    _, w_sq, h_sq = pe.shelf_pack(sizes, margin=0.0, aspect=1.0)
    assert w_wide > w_sq and h_wide < h_sq
    assert w_sq == h_sq == 8.0                         # 4 x 4 grid of 2 mm parts


# --- functional_place --------------------------------------------------------

def _mcu_design():
    design = {"components": [{"ref": "U1"}, {"ref": "C1"}, {"ref": "C2"},
                             {"ref": "J1"}, {"ref": "R1"}, {"ref": "Y1"}, {"ref": "C3"}],
              "nets": {"VCC": [["U1", "1"], ["C1", "1"], ["J1", "1"]],
                       "GND": [["U1", "2"], ["C1", "2"], ["C2", "2"], ["J1", "2"],
                               ["C3", "2"]],
                       "NRST": [["U1", "3"], ["C2", "1"]],
                       "TX": [["U1", "4"], ["R1", "1"]],
                       "TXO": [["R1", "2"], ["J1", "3"]],
                       "OSC": [["U1", "5"], ["Y1", "1"], ["C3", "1"]]}}
    extents = {"U1": (5, 5, 0, 0), "C1": (1.6, 0.8, 0, 0), "C2": (1.6, 0.8, 0, 0),
               "J1": (8, 4, 0, 0), "R1": (1.6, 0.8, 0, 0), "Y1": (3.2, 2.5, 0, 0),
               "C3": (1.6, 0.8, 0, 0)}
    pincount = {"U1": 8, "C1": 2, "C2": 2, "J1": 3, "R1": 2, "Y1": 2, "C3": 2}
    return design, extents, pincount


def test_functional_place_places_every_part_without_overlap():
    design, extents, pincount = _mcu_design()
    pos, rots, w, h = pe.functional_place(design, extents, pincount)
    refs = {c["ref"] for c in design["components"]}
    assert set(pos) == refs
    sizes = {r: (extents[r][0], extents[r][1]) for r in refs}
    _assert_no_overlap(pos, sizes)
    boxes = _boxes(pos, sizes)
    assert min(b[0] for b in boxes.values()) == pytest.approx(0.0, abs=1e-3)
    assert min(b[1] for b in boxes.values()) == pytest.approx(0.0, abs=1e-3)
    assert w == pytest.approx(max(b[2] for b in boxes.values()), abs=0.01)
    assert h == pytest.approx(max(b[3] for b in boxes.values()), abs=0.01)


def test_functional_place_connector_row_on_top_and_satellites_cluster_at_ic():
    design, extents, pincount = _mcu_design()
    pos, rots, _, _ = pe.functional_place(design, extents, pincount)
    assert pos["J1"][1] < pos["U1"][1]                  # connector row above the IC
    assert set(rots) == {"J1"} and rots["J1"] == 0       # mouthless header: no turn
    for sat in ("C1", "C2", "R1", "Y1", "C3"):
        dx = abs(pos[sat][0] - pos["U1"][0])
        dy = abs(pos[sat][1] - pos["U1"][1])
        assert max(dx, dy) < 8.0, f"{sat} far from its IC: {pos[sat]} vs {pos['U1']}"


@pytest.mark.parametrize("mouth,rot", [
    ((0.0, -1.0), 0), ((1.0, 0.0), 90), ((0.0, 1.0), 180), ((-1.0, 0.0), 270),
    ((0.0, 0.0), 0), ((0.3, 0.2), 0),
])
def test_functional_place_rotates_connector_mouth_off_top_edge(mouth, rot):
    design, extents, pincount = _mcu_design()
    _, rots, _, _ = pe.functional_place(design, extents, pincount, mouth={"J1": mouth})
    assert rots["J1"] == rot


def test_functional_place_rotated_connector_swaps_extent():
    design, extents, pincount = _mcu_design()
    _, _, w0, h0 = pe.functional_place(design, extents, pincount, mouth={"J1": (0, -1)})
    _, _, w90, h90 = pe.functional_place(design, extents, pincount, mouth={"J1": (1, 0)})
    # J1 is 8 x 4: turned 90 it is 4 wide and 8 tall -> connector row grows taller
    assert h90 > h0


def test_functional_place_interior_and_edge_row_flags():
    design, extents, pincount = _mcu_design()
    design["components"][3]["interior"] = True           # J1 is a config jumper
    _, rots, _, _ = pe.functional_place(design, extents, pincount)
    assert rots == {}                                    # no connector -> no rotations
    design, extents, pincount = _mcu_design()
    design["components"][5]["edge_row"] = True           # Y1 pinned to the edge row
    pos, rots, _, _ = pe.functional_place(design, extents, pincount)
    assert set(rots) == {"J1", "Y1"}
    assert pos["Y1"][1] < pos["U1"][1]


def test_functional_place_only_satellites_and_connectors():
    design = {"components": [{"ref": "J1"}, {"ref": "C1"}],
              "nets": {"A": [["J1", "1"], ["C1", "1"]], "B": [["J1", "2"], ["C1", "2"]]}}
    extents = {"J1": (6, 3, 0, 0), "C1": (1.6, 0.8, 0, 0)}
    pos, rots, w, h = pe.functional_place(design, extents, {"J1": 2, "C1": 2})
    assert set(pos) == {"J1", "C1"} and rots == {"J1": 0}
    _assert_no_overlap(pos, {"J1": (6, 3), "C1": (1.6, 0.8)})


def test_functional_place_rail_attach_prefers_first_anchor_on_the_rail():
    # a rail (fanout > 10) is never "specific": every cap on it lands on the
    # FIRST anchor the rail names, so decoupling piles on U1 by design here
    design = {"components": [{"ref": "U1"}, {"ref": "U2"}, {"ref": "C1"}, {"ref": "C2"}],
              "nets": {"GND": [["U1", "1"], ["U2", "1"], ["C1", "2"], ["C2", "2"]]
                       + [[f"X{i}", "1"] for i in range(12)],
                       "V": [["U1", "2"], ["U2", "2"], ["C1", "1"], ["C2", "1"]]
                       + [[f"X{i}", "2"] for i in range(12)]}}
    extents = {"U1": (5, 5, 0, 0), "U2": (5, 5, 0, 0), "C1": (1.6, 0.8, 0, 0),
               "C2": (1.6, 0.8, 0, 0)}
    pos, _, _, _ = pe.functional_place(design, extents, {"U1": 8, "U2": 8, "C1": 2, "C2": 2})
    assert {_nearest(pos, "C1"), _nearest(pos, "C2")} == {"U1"}


def test_functional_place_unassociated_caps_round_robin_over_anchors():
    # caps that share NO net with any anchor are spread across the anchors
    design = {"components": [{"ref": "U1"}, {"ref": "U2"}, {"ref": "C1"}, {"ref": "C2"}],
              "nets": {"GND": [["C1", "2"], ["C2", "2"]] + [[f"X{i}", "1"] for i in range(12)],
                       "V": [["C1", "1"], ["C2", "1"]],
                       "A": [["U1", "1"], ["U1", "2"]], "B": [["U2", "1"], ["U2", "2"]]}}
    extents = {"U1": (5, 5, 0, 0), "U2": (5, 5, 0, 0), "C1": (1.6, 0.8, 0, 0),
               "C2": (1.6, 0.8, 0, 0)}
    pos, _, _, _ = pe.functional_place(design, extents, {"U1": 8, "U2": 8, "C1": 2, "C2": 2})
    assert {_nearest(pos, "C1"), _nearest(pos, "C2")} == {"U1", "U2"}


def _nearest(pos: dict, sat: str) -> str:
    return min(("U1", "U2"), key=lambda a: abs(pos[sat][0] - pos[a][0])
               + abs(pos[sat][1] - pos[a][1]))


def test_functional_place_width_limit_widens_the_anchor_field():
    design = {"components": [{"ref": f"U{i}"} for i in range(6)], "nets": {}}
    extents = {f"U{i}": (6, 6, 0, 0) for i in range(6)}
    pincount = {f"U{i}": 8 for i in range(6)}
    _, _, w_narrow, h_narrow = pe.functional_place(design, extents, pincount)
    _, _, w_wide, h_wide = pe.functional_place(design, extents, pincount, width_limit=40.0)
    assert w_wide > w_narrow and h_wide < h_narrow


def test_functional_place_pack_debug_env_raises_name_error(monkeypatch):
    # BUG: the PACK_DEBUG diagnostic path writes to sys.stderr but place_engine
    # never imports sys, so setting PACK_DEBUG=1 crashes functional_place with
    # NameError instead of printing cluster sizes. Documented, not fixed here.
    monkeypatch.setenv("PACK_DEBUG", "1")
    design, extents, pincount = _mcu_design()
    with pytest.raises(NameError, match="sys"):
        pe.functional_place(design, extents, pincount)
