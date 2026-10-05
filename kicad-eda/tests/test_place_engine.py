"""place_engine — the pure geometry half (shelf_pack / functional_place), the
connector edges of place_conn (F3-08), and fp_extent / fp_mouth on stub
footprints (no KiCad).

place_engine imports pcbnew at module level for fp_extent/fp_mouth. When pcbnew
is not importable (the dev venv, CI) a stub module is registered for the
duration of the place_engine import ONLY and is removed again afterwards, so it
never leaks into other test modules that expect `import pcbnew` to fail
(board_shapes, emc_stitch, silk_place, _kicad_python). The fp_extent/fp_mouth
tests swap place_engine's own pcbnew reference for a namespace via monkeypatch.
"""
from __future__ import annotations

import ast
import importlib
import math
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

import place_conn as pconn


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


def test_functional_place_pack_debug_env_writes_cluster_sizes(monkeypatch, capsys):
    # PACK_DEBUG=1 is a diagnostic only: it prints one line per cluster to
    # stderr and must not change the placement (it used to raise NameError,
    # place_engine never imported sys)
    design, extents, pincount = _mcu_design()
    monkeypatch.delenv("PACK_DEBUG", raising=False)
    quiet = pe.functional_place(design, extents, pincount)
    monkeypatch.setenv("PACK_DEBUG", "1")
    assert pe.functional_place(design, extents, pincount) == quiet
    err = capsys.readouterr().err.splitlines()
    assert [line.split(":")[0] for line in err] == ["cluster U1", "cluster J1"]
    assert err[0].startswith("cluster U1: ") and "(5 sats)" in err[0]


def _passives_design(n: int) -> tuple[dict, dict, dict]:
    """n two-pin parts and no anchor IC or connector — an all-passive auto
    field (e.g. the back side of a mixed board whose ICs are fixed)."""
    refs = [f"R{i}" for i in range(1, n + 1)]
    design = {"components": [{"ref": r} for r in refs],
              "nets": {"A": [[r, "1"] for r in refs], "B": [[r, "2"] for r in refs]}}
    extents = {r: (1.6 + 0.4 * (i % 3), 0.8 + 0.2 * (i % 2), 0, 0) for i, r in enumerate(refs)}
    return design, extents, {r: 2 for r in refs}


@pytest.mark.parametrize("n", [1, 2, 9])
def test_functional_place_without_anchor_or_connector_places_every_part(n):
    # used to raise IndexError: satellites fell back to connectors[0]
    design, extents, pincount = _passives_design(n)
    pos, rots, w, h = pe.functional_place(design, extents, pincount)
    refs = {c["ref"] for c in design["components"]}
    assert set(pos) == refs and rots == {}
    sizes = {r: (extents[r][0], extents[r][1]) for r in refs}
    _assert_no_overlap(pos, sizes)
    boxes = _boxes(pos, sizes)
    assert min(b[0] for b in boxes.values()) == pytest.approx(0.0, abs=1e-3)
    assert min(b[1] for b in boxes.values()) == pytest.approx(0.0, abs=1e-3)
    assert w == pytest.approx(max(b[2] for b in boxes.values()), abs=0.01)
    assert h == pytest.approx(max(b[3] for b in boxes.values()), abs=0.01)


def test_functional_place_all_passive_field_honours_width_limit():
    design, extents, pincount = _passives_design(12)
    _, _, w, h = pe.functional_place(design, extents, pincount, width_limit=8.0)
    _, _, w_free, h_free = pe.functional_place(design, extents, pincount)
    assert w <= 8.0 and h > h_free


# --- F3-08: default placement is bit-identical to the pre-F3-08 engine ----------
#
# GOLDEN_DEFAULT was captured by running the HEAD (pre-F3-08) functional_place on
# the conftest examples (buck_design, hat_design and the hat's F/B auto halves as
# swig_build_board splits them) and on _mcu_design, with _stub_inputs standing in
# for fp_extent / GetPadCount / fp_mouth. Every conn_edges form that leaves all
# connectors on the default (top) edge must reproduce it exactly: repr equality,
# i.e. the same float bits, ints and dict order. The full-hat case breaks
# net-fanout ties in str-set order, which follows PYTHONHASHSEED, so it is
# compared in a child process pinned to the seed it was captured under.

GOLDEN_SEED = "1"
_MOUTHS = ((0.0, 1.0), (1.0, 0.0), (0.0, -1.0), (-1.0, 0.0), (0.0, 0.0))
EDGE_MODES = ("none", "empty", "all_top", "foreign")


def _stub_inputs(design: dict) -> tuple[dict, dict, dict]:
    """Deterministic stand-ins for fp_extent / GetPadCount / fp_mouth on an
    example netlist: pad count from the nets, a size per reference class, and
    the J* mouths cycling through every direction (and mouthless)."""
    pads: dict[str, set] = {c["ref"]: set() for c in design["components"]}
    for conns in design["nets"].values():
        for ref, pad in conns:
            pads.setdefault(ref, set()).add(pad)
    extents, pincount = {}, {}
    for i, c in enumerate(design["components"]):
        ref, n = c["ref"], len(pads[c["ref"]])
        kind = ref.rstrip("0123456789")
        if kind == "U":
            w, h = 2.0 + 0.25 * n, 2.4 + 0.25 * n
        elif kind == "J":
            w, h = 1.5 + 1.27 * n, 5.0 + i % 3
        elif kind == "L":
            w, h = 7.0, 6.6
        else:
            w, h = 1.6 + 0.4 * (i % 3), 0.8 + 0.2 * (i % 2)
        extents[ref] = (round(w, 3), round(h, 3), 0.0, 0.0)
        pincount[ref] = n
    jrefs = [c["ref"] for c in design["components"] if c["ref"][:1] == "J"]
    mouth = {r: _MOUTHS[k % len(_MOUTHS)] for k, r in enumerate(jrefs)}
    return extents, pincount, mouth


def _connectors(design: dict) -> list[str]:
    return [c["ref"] for c in design["components"]
            if (c["ref"][:1] == "J" or c.get("edge_row")) and not c.get("interior")]


def _edges_for(mode: str, design: dict, universe: list[str]) -> dict | None:
    """The conn_edges forms that keep every connector on the default top edge."""
    if mode == "none":
        return None
    if mode == "empty":
        return {}
    if mode == "all_top":
        return {r: "top" for r in _connectors(design)}
    # one mapping for a whole board: entries naming parts outside this
    # (sub-)design -- fixed parts, the other side's half -- are ignored
    refs = {c["ref"] for c in design["components"]}
    return {**{r: "left" for r in universe if r not in refs}, "NOT_A_PART": "right"}


def golden_outputs(buck: dict, hat: dict, mode: str) -> dict:
    """functional_place on every golden case with the `mode` conn_edges form."""
    auto = [c for c in hat["components"] if not ("x" in c and "y" in c)]
    cases = {"buck": (buck, buck, {}), "buck_scaled": (buck, buck, {"margin": 2.25}),
             "hat": (hat, hat, {}),
             "hat_f": (hat, {**hat, "components": [c for c in auto if c.get("side", "F") != "B"]},
                       {"margin": 1.5, "width_limit": 23.0}),
             "hat_b": (hat, {**hat, "components": [c for c in auto if c.get("side", "F") == "B"]},
                       {"margin": 1.5, "width_limit": 25.0})}
    out = {}
    for name, (board, design, kw) in cases.items():
        ext, pins, mouth = _stub_inputs(board)
        edges = _edges_for(mode, design, [c["ref"] for c in board["components"]])
        out[name] = pe.functional_place(design, ext, pins, mouth, conn_edges=edges, **kw)
    mcu, ext, pins = _mcu_design()
    universe = [c["ref"] for c in mcu["components"]]
    row = {**mcu, "components": [dict(c, edge_row=True) if c["ref"] == "Y1" else c
                                 for c in mcu["components"]]}
    for name, m in (("up", (0.0, -1.0)), ("right", (1.0, 0.0)), ("down", (0.0, 1.0)),
                    ("left", (-1.0, 0.0)), ("edge_row", (0.0, 1.0))):
        design = row if name == "edge_row" else mcu
        out[f"mcu_{name}"] = pe.functional_place(
            design, ext, pins, mouth={"J1": m}, conn_edges=_edges_for(mode, design, universe))
    return out


def _golden_hat_in_pinned_child() -> dict:
    """{mode: golden_outputs(...)["hat"]} from a child pinned to GOLDEN_SEED."""
    here = Path(__file__).resolve().parent
    code = ("import test_place_engine as t; from conftest import load_example as ld; "
            "print(repr({m: t.golden_outputs(ld('buck_5v3a'), ld('rpi5_battery_hat'), m)"
            "['hat'] for m in t.EDGE_MODES}))")
    path = os.pathsep.join(str(p) for p in (here, here.parent / "server" / "tools",
                                            here.parent / "server"))
    proc = subprocess.run([sys.executable, "-c", code], cwd=str(here), timeout=120,
                          env={**os.environ, "PYTHONHASHSEED": GOLDEN_SEED, "PYTHONPATH": path},
                          capture_output=True, encoding="utf-8", errors="replace",
                          stdin=subprocess.DEVNULL)
    assert proc.returncode == 0, proc.stderr[-3000:]
    return ast.literal_eval(proc.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize("mode", EDGE_MODES)
def test_default_placement_is_bit_identical_to_the_captured_literals(
        mode: str, buck_design: dict, hat_design: dict) -> None:
    got = golden_outputs(buck_design, hat_design, mode)
    assert set(got) == set(GOLDEN_DEFAULT)
    for name, golden in GOLDEN_DEFAULT.items():
        if name != "hat":            # hash-seed dependent: the pinned child below
            assert repr(got[name]) == repr(golden), name


def test_default_full_hat_placement_is_bit_identical_under_the_capture_seed() -> None:
    got = _golden_hat_in_pinned_child()
    assert set(got) == set(EDGE_MODES)
    for mode, result in got.items():
        assert repr(result) == repr(GOLDEN_DEFAULT["hat"]), mode


# functional_place outputs of the pre-F3-08 engine (see the section comment)
GOLDEN_DEFAULT = {
    "buck": ({
        "J1": (2.02, 2.5), "J2": (8.04, 2.02), "U1": (6.02, 8.325), "C1": (2.295, 7.0),
        "C3": (2.495, 8.5), "F1": (9.545, 7.0), "C4": (9.745, 8.4), "L1": (4.77, 14.35),
        "D1": (9.77, 11.45), "C2": (2.07, 18.55)}, {"J1": 180, "J2": 90}, 10.94, 18.95),
    "buck_scaled": ({
        "J1": (2.02, 2.5), "J2": (8.79, 2.02), "U1": (6.77, 9.075), "C1": (2.595, 7.75),
        "C3": (2.795, 9.25), "F1": (10.745, 7.75), "C4": (10.945, 9.15), "L1": (5.52, 15.55),
        "D1": (10.52, 12.65), "C2": (2.82, 19.75)}, {"J1": 180, "J2": 90}, 12.14, 20.15),
    "hat": ({
        "J1": (11.035, 2.5), "C3": (1.2, 0.5), "C5": (1.4, 2.0), "C6": (1.2, 3.4),
        "C15": (20.87, 0.5), "TP5": (20.67, 2.0), "C7": (20.47, 3.5), "TP1": (7.685, 6.4),
        "C14": (9.985, 6.3), "C4": (12.285, 6.3), "C22": (14.385, 6.3), "J5": (29.87, 11.545),
        "R12": (24.77, 0.5), "TP3": (24.77, 2.0), "R14": (24.97, 3.5), "R15": (24.77, 4.9),
        "C24": (24.77, 6.2), "C19": (25.17, 7.6), "TP4": (25.17, 9.0), "C21": (34.97, 0.5),
        "D3": (34.77, 2.0), "C23": (34.77, 3.5), "C18": (34.97, 4.9), "Q7": (34.57, 6.3),
        "Q2": (34.57, 7.7), "J2": (39.69, 3.5), "J3": (45.71, 3.29), "U1": (8.73, 41.315),
        "C1": (3.905, 38.49), "C2": (13.755, 38.39), "U2": (8.405, 26.79), "C9": (4.305, 25.09),
        "C8": (12.305, 24.99), "R1": (12.105, 26.29), "L1": (8.205, 33.19), "U3": (19.705, 26.79),
        "C17": (15.805, 25.09), "C20": (15.805, 26.49), "R9": (23.805, 24.99),
        "R10": (23.405, 26.39), "L2": (19.905, 33.19), "U4": (21.78, 40.815),
        "R2": (17.255, 38.49), "U5": (30.93, 40.315), "R8": (26.905, 38.49), "R7": (34.755, 38.39),
        "C16": (34.755, 39.69), "U6": (31.405, 26.79), "R11": (27.505, 24.99),
        "J4": (29.955, 32.89), "Q1": (37.355, 25.09), "C11": (35.905, 26.99),
        "R4": (38.205, 26.99), "C13": (35.705, 28.49), "R3": (38.205, 28.39),
        "C12": (36.105, 29.89), "D1": (38.805, 29.89), "RS1": (35.905, 31.19),
        "C10": (38.205, 31.19), "Q3": (43.955, 25.09), "D2": (42.505, 26.99),
        "RS2": (45.405, 26.99), "R6": (42.505, 28.49), "Q4": (45.405, 28.39),
        "R5": (42.305, 29.89), "TP2": (44.805, 29.89), "D4": (42.105, 31.19),
        "R13": (44.205, 31.19)}, {"J1": 180, "J5": 90, "J2": 0, "J3": 270}, 48.21, 44.64),
    "hat_f": ({
        "U1": (8.065, 3.325), "J4": (2.02, 3.0), "R12": (13.29, 0.5), "C3": (13.29, 2.0),
        "D3": (13.09, 3.5), "TP5": (13.09, 5.0), "C1": (1.595, 8.05), "TP1": (3.695, 8.05),
        "C2": (5.995, 7.95), "C14": (8.495, 7.95), "C4": (10.795, 7.95), "TP4": (12.895, 7.95)
        }, {}, 14.49, 8.55),
    "hat_b": ({
        "U4": (5.925, 2.825), "D2": (1.2, 0.5), "R2": (1.4, 2.0), "R3": (1.2, 3.4),
        "D1": (1.4, 4.7), "R6": (10.65, 0.5), "C23": (10.45, 2.0), "C24": (10.65, 3.4),
        "R5": (10.45, 4.7), "C22": (5.925, 6.95), "U6": (18.25, 2.2), "R14": (14.35, 0.5),
        "Q7": (14.55, 2.0), "R11": (14.35, 3.4), "Q4": (22.35, 0.4), "R15": (22.35, 1.7),
        "R4": (21.95, 3.1), "Q2": (16.35, 5.7), "D4": (18.45, 5.7), "R13": (20.55, 5.7)
        }, {}, 23.55, 7.35),
    "mcu_up": ({
        "J1": (4.0, 2.0), "U1": (6.6, 8.0), "Y1": (1.6, 6.75), "C3": (2.4, 8.9), "C1": (10.8, 5.9),
        "C2": (10.8, 7.2), "R1": (10.8, 8.5)}, {"J1": 0}, 11.6, 10.5),
    "mcu_right": ({
        "J1": (2.0, 4.0), "U1": (6.6, 12.0), "Y1": (1.6, 10.75), "C3": (2.4, 12.9),
        "C1": (10.8, 9.9), "C2": (10.8, 11.2), "R1": (10.8, 12.5)}, {"J1": 90}, 11.6, 14.5),
    "mcu_down": ({
        "J1": (4.0, 2.0), "U1": (6.6, 8.0), "Y1": (1.6, 6.75), "C3": (2.4, 8.9), "C1": (10.8, 5.9),
        "C2": (10.8, 7.2), "R1": (10.8, 8.5)}, {"J1": 180}, 11.6, 10.5),
    "mcu_left": ({
        "J1": (2.0, 4.0), "U1": (6.6, 12.0), "Y1": (1.6, 10.75), "C3": (2.4, 12.9),
        "C1": (10.8, 9.9), "C2": (10.8, 11.2), "R1": (10.8, 12.5)}, {"J1": 270}, 11.6, 14.5),
    "mcu_edge_row": ({
        "J1": (4.0, 2.0), "Y1": (11.1, 1.25), "U1": (7.1, 8.0), "C1": (2.9, 5.9), "R1": (2.9, 7.2),
        "C2": (11.3, 5.9), "C3": (11.3, 7.2)}, {"J1": 180, "Y1": 0}, 12.7, 10.5),
}


# --- F3-08: connectors on any board edge (conn_edges) ----------------------------

NORMAL = {"top": (0.0, -1.0), "bottom": (0.0, 1.0), "left": (-1.0, 0.0), "right": (1.0, 0.0)}
AXES = ((0.0, -1.0), (1.0, 0.0), (0.0, 1.0), (-1.0, 0.0))
FOUR_EDGES = {"J1": "top", "J2": "bottom", "J3": "left", "J4": "right"}


def _rot_cw(vec: tuple, rot: int) -> tuple[float, float]:
    """KiCad's clockwise-positive rotation in the Y-down frame (the NOTE in
    swig_build_board's placement loop), written independently of place_conn."""
    a = math.radians(rot)
    x, y = vec
    return (round(x * math.cos(a) + y * math.sin(a), 9) + 0.0,
            round(-x * math.sin(a) + y * math.cos(a), 9) + 0.0)


def _rot_sizes(extents: dict, rots: dict) -> dict:
    return {r: (e[1], e[0]) if rots.get(r, 0) in (90, 270) else (e[0], e[1])
            for r, e in extents.items()}


def _edge_gap(result: tuple, sizes: dict, ref: str, edge: str) -> float:
    """Distance from ref's (rotated) bbox to `edge` of the placement extent."""
    pos, _, w, h = result
    cx, cy = pos[ref]
    sw, sh = sizes[ref]
    return {"top": cy - sh / 2, "bottom": h - (cy + sh / 2),
            "left": cx - sw / 2, "right": w - (cx + sw / 2)}[edge]


def _check_on_edge(result: tuple, extents: dict, ref: str, edge: str, mouth: tuple) -> None:
    """ref's rotated mouth is `edge`'s outward normal, its courtyard bbox is
    flush with that edge of the placement, and nothing overlaps."""
    pos, rots, _, _ = result
    sizes = _rot_sizes(extents, rots)
    assert _rot_cw(mouth, rots[ref]) == NORMAL[edge], (ref, edge, rots[ref])
    assert _edge_gap(result, sizes, ref, edge) == pytest.approx(0.0, abs=0.01), (ref, edge)
    assert set(pos) == set(extents)
    _assert_no_overlap(pos, sizes)


def _box_gap(pos: dict, sizes: dict, a: str, b: str) -> float:
    """Chebyshev gap between two bboxes (0 when they touch)."""
    gx = abs(pos[a][0] - pos[b][0]) - (sizes[a][0] + sizes[b][0]) / 2
    gy = abs(pos[a][1] - pos[b][1]) - (sizes[a][1] + sizes[b][1]) / 2
    return max(gx, gy, 0.0)


def _four_edge_design() -> tuple[dict, dict, dict]:
    """Two ICs and four connectors; J3 (a 6-pin USB-C-like head) owns its CC
    pulldown R3, which must travel with it to whatever edge J3 is given."""
    refs = ["U1", "U2", "J1", "J2", "J3", "J4", "R3", "C1", "C2"]
    nets = {"GND": [["U1", "1"], ["U2", "1"], ["J1", "1"], ["J2", "1"], ["J3", "1"],
                    ["J4", "1"], ["R3", "2"], ["C1", "2"], ["C2", "2"]],
            "V": [["U1", "2"], ["U2", "2"], ["C1", "1"], ["C2", "1"]],
            "CC": [["J3", "2"], ["R3", "1"]],
            "A": [["U1", "3"], ["J1", "2"]], "B": [["U1", "4"], ["J2", "2"]],
            "D": [["U2", "3"], ["J3", "3"]], "E": [["U2", "4"], ["J4", "2"]]}
    extents = {"U1": (5, 5, 0, 0), "U2": (4, 6, 0, 0), "J1": (8, 4, 0, 0),
               "J2": (6, 3, 0, 0), "J3": (9, 7, 0, 0), "J4": (7, 5, 0, 0),
               "R3": (1.6, 0.8, 0, 0), "C1": (1.6, 0.8, 0, 0), "C2": (1.6, 0.8, 0, 0)}
    pincount = {"U1": 8, "U2": 8, "J1": 2, "J2": 2, "J3": 6, "J4": 2, "R3": 2, "C1": 2,
                "C2": 2}
    return {"components": [{"ref": r} for r in refs], "nets": nets}, extents, pincount


@pytest.mark.parametrize("edge,rot", [("top", 180), ("bottom", 0), ("left", 270),
                                      ("right", 90)])
def test_right_angle_connector_mouth_faces_out_of_each_edge(edge: str, rot: int) -> None:
    # a right-angle stub whose mouth is +y at rot 0, placed on each edge in turn
    design, extents, pincount = _mcu_design()
    result = pe.functional_place(design, extents, pincount, mouth={"J1": (0.0, 1.0)},
                                 conn_edges={"J1": edge})
    assert result[1] == {"J1": rot}
    _check_on_edge(result, extents, "J1", edge, (0.0, 1.0))


@pytest.mark.parametrize("edge", pconn.EDGES)
@pytest.mark.parametrize("mouth", AXES)
def test_rot_face_points_every_mouth_along_every_edge_normal(mouth: tuple, edge: str) -> None:
    assert _rot_cw(mouth, pconn.rot_face(mouth, edge)) == NORMAL[edge]
    assert pconn.rotate_cw(mouth, pconn.rot_face(mouth, edge)) == NORMAL[edge]
    assert pconn.rot_face((0.0, 0.0), edge) == pconn.rot_face((0.3, 0.2), edge) == 0


@pytest.mark.parametrize("rot", (0, 90, 180, 270))
@pytest.mark.parametrize("vec", AXES + ((0.6, -0.8),))
def test_rotate_cw_matches_kicads_clockwise_formula(vec: tuple, rot: int) -> None:
    assert pconn.rotate_cw(vec, rot) == pytest.approx(_rot_cw(vec, rot), abs=1e-9)


def test_mixed_edges_put_each_connector_on_its_edge_without_overlap() -> None:
    design, extents, pincount = _four_edge_design()
    result = pe.functional_place(design, extents, pincount,
                                 {j: (0.0, 1.0) for j in FOUR_EDGES}, conn_edges=FOUR_EDGES)
    for ref, edge in FOUR_EDGES.items():
        _check_on_edge(result, extents, ref, edge, (0.0, 1.0))
    sizes = _rot_sizes(extents, result[1])
    assert _box_gap(result[0], sizes, "R3", "J3") < 1.0     # CC pulldown at its connector


@pytest.mark.parametrize("edge", pconn.EDGES)
def test_connectors_sharing_an_edge_form_a_row_or_column_along_it(edge: str) -> None:
    design, extents, pincount = _four_edge_design()
    result = pe.functional_place(design, extents, pincount,
                                 {j: (0.0, 1.0) for j in FOUR_EDGES},
                                 conn_edges={j: edge for j in FOUR_EDGES})
    for ref in FOUR_EDGES:
        _check_on_edge(result, extents, ref, edge, (0.0, 1.0))
    along = 0 if edge in ("top", "bottom") else 1
    assert len({result[0][j][along] for j in FOUR_EDGES}) == 4   # spread along the edge
    assert _box_gap(result[0], _rot_sizes(extents, result[1]), "R3", "J3") < 1.0


def test_bottom_row_stays_flush_below_a_long_side_column() -> None:
    design, extents, pincount = _four_edge_design()
    edges = {"J1": "left", "J3": "left", "J4": "left", "J2": "bottom"}
    result = pe.functional_place(design, extents, pincount, {j: (0.0, 1.0) for j in edges},
                                 conn_edges=edges)
    for ref, edge in edges.items():
        _check_on_edge(result, extents, ref, edge, (0.0, 1.0))


def test_side_columns_narrow_the_anchor_field_to_width_limit() -> None:
    design = {"components": [{"ref": f"U{i}"} for i in range(6)] + [{"ref": "J1"}],
              "nets": {}}
    extents = {**{f"U{i}": (6, 6, 0, 0) for i in range(6)}, "J1": (8, 4, 0, 0)}
    pincount = {**{f"U{i}": 8 for i in range(6)}, "J1": 3}
    for edge in ("left", "right"):
        result = pe.functional_place(design, extents, pincount, {"J1": (0.0, 1.0)},
                                     width_limit=40.0, conn_edges={"J1": edge})
        assert result[2] <= 40.0                  # 5 anchors per row would need 41.5
        _check_on_edge(result, extents, "J1", edge, (0.0, 1.0))


def _ble_design() -> tuple[dict, dict, dict]:
    """An MCU, a BLE module flagged edge_row + "edge": "right" (antenna toward
    -y in its rot-0 frame, like the Raytac MDBT42Q) with its own decoupling
    cap C2, and a USB connector J1 left on the default edge."""
    design = {"components": [{"ref": "U1"}, {"ref": "U2", "edge_row": True, "edge": "right"},
                             {"ref": "J1"}, {"ref": "C1"}, {"ref": "C2"}],
              "nets": {"GND": [["U1", "1"], ["U2", "1"], ["J1", "1"], ["C1", "2"],
                               ["C2", "2"]],
                       "VBLE": [["U2", "2"], ["C2", "1"]],
                       "VCC": [["U1", "2"], ["C1", "1"], ["J1", "2"]],
                       "TX": [["U1", "3"], ["U2", "3"]]}}
    extents = {"U1": (5, 5, 0, 0), "U2": (10, 16, 0, 0), "J1": (9, 7, 0, 0),
               "C1": (1.6, 0.8, 0, 0), "C2": (1.6, 0.8, 0, 0)}
    return design, extents, {"U1": 8, "U2": 40, "J1": 6, "C1": 2, "C2": 2}


def test_edge_row_module_with_edge_right_lands_on_the_right_edge() -> None:
    design, extents, pincount = _ble_design()
    # what the builder (F3-12) passes: the component key "edge"
    edges = {c["ref"]: c["edge"] for c in design["components"] if "edge" in c}
    mouth = {"U2": (0.0, -1.0), "J1": (0.0, 1.0)}
    result = pe.functional_place(design, extents, pincount, mouth, conn_edges=edges)
    assert result[1] == {"U2": 270, "J1": 180}
    _check_on_edge(result, extents, "U2", "right", (0.0, -1.0))
    _check_on_edge(result, extents, "J1", "top", (0.0, 1.0))
    sizes = _rot_sizes(extents, result[1])
    assert _box_gap(result[0], sizes, "C2", "U2") < 1.0     # module decoupling at the module


def test_conn_edges_rejects_an_unknown_edge_name() -> None:
    design, extents, pincount = _mcu_design()
    with pytest.raises(ValueError, match="'north'"):
        pe.functional_place(design, extents, pincount, conn_edges={"J1": "north"})


@pytest.mark.parametrize("ref,flag", [("U1", None), ("Y1", None), ("J1", "interior")])
def test_conn_edges_rejects_a_part_that_is_not_an_edge_connector(ref: str, flag) -> None:
    design, extents, pincount = _mcu_design()
    if flag:
        design = {**design, "components": [dict(c, **{flag: True}) if c["ref"] == ref else c
                                           for c in design["components"]]}
    with pytest.raises(ValueError, match=f"'{ref}'"):
        pe.functional_place(design, extents, pincount, conn_edges={ref: "left"})


def test_orient_cluster_puts_the_head_on_the_side_facing_its_edge() -> None:
    # laid out for the top edge: head H on y = 0..2, satellite S below it
    top = ({"H": (2.5, 1.0), "S": (2.5, 3.5)}, 5.0, 4.0)
    assert pconn.orient_cluster(top, "top") is top
    assert pconn.orient_cluster(top, "bottom") == ({"H": (2.5, 3.0), "S": (2.5, 0.5)}, 5.0, 4.0)
    # left/right take a cluster laid out on TRANSPOSED sizes and transpose it back
    assert pconn.orient_cluster(top, "left") == ({"H": (1.0, 2.5), "S": (3.5, 2.5)}, 4.0, 5.0)
    assert pconn.orient_cluster(top, "right") == ({"H": (3.0, 2.5), "S": (0.5, 2.5)}, 4.0, 5.0)


# --- fp_extent / fp_mouth: courtyard caches (empty on KiCad 9.0.4) ----------------

NM = 1_000_000


class _Box:
    def __init__(self, x0: float, y0: float, x1: float, y1: float) -> None:
        self.x0, self.y0, self.x1, self.y1 = (round(v * NM) for v in (x0, y0, x1, y1))

    def GetWidth(self) -> int:  # noqa: N802 (pcbnew API)
        return self.x1 - self.x0

    def GetHeight(self) -> int:  # noqa: N802
        return self.y1 - self.y0

    def GetCenter(self) -> types.SimpleNamespace:  # noqa: N802
        return types.SimpleNamespace(x=(self.x0 + self.x1) // 2, y=(self.y0 + self.y1) // 2)


class _Poly:
    def __init__(self, box: _Box | None) -> None:
        self.box = box

    def OutlineCount(self) -> int:  # noqa: N802
        return 1 if self.box else 0

    def BBox(self) -> _Box:  # noqa: N802  (an empty set's bbox: a zero box at the origin)
        return self.box or _Box(0, 0, 0, 0)


_PCB = types.SimpleNamespace(VECTOR2I=lambda x, y: (x, y), F_CrtYd=46, B_CrtYd=47,
                             ToMM=lambda v: v / NM)


class _LazyFootprint:
    """KiCad 10.0-like: GetCourtyard is always current; no BuildCourtyardCaches."""

    def __init__(self, courtyard: _Box | None, bbox: _Box, pads: tuple = ()) -> None:
        self.courtyard, self.bbox, self.pads = courtyard, bbox, list(pads)
        self.calls: list[str] = []
        self.built = True

    def SetPosition(self, _p: tuple) -> None:  # noqa: N802
        self.calls.append("SetPosition")

    def GetCourtyard(self, layer: int) -> _Poly:  # noqa: N802
        self.calls.append("GetCourtyard")
        return _Poly(self.courtyard if self.built and layer == _PCB.F_CrtYd else None)

    def GetBoundingBox(self) -> _Box:  # noqa: N802
        return self.bbox

    def Pads(self) -> list:  # noqa: N802
        return self.pads


class _Footprint904(_LazyFootprint):
    """KiCad 9.0.4-like: the courtyard cache is EMPTY until BuildCourtyardCaches()."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.built = False

    def BuildCourtyardCaches(self) -> None:  # noqa: N802
        self.calls.append("BuildCourtyardCaches")
        self.built = True


def _pad(x: float, y: float) -> types.SimpleNamespace:
    at = types.SimpleNamespace(x=round(x * NM), y=round(y * NM))
    return types.SimpleNamespace(GetPosition=lambda: at)


def _cap(cls: type) -> _LazyFootprint:
    """A 0603 cap: courtyard 3.05 x 1.55 centred at (0, 0.1); silk+text bbox 14.9 x 4.6."""
    return cls(_Box(-1.525, -0.675, 1.525, 0.875), _Box(-7.45, -2.3, 7.45, 2.3))


def _edge_jack(cls: type) -> _LazyFootprint:
    """Pads on y = 0.2, courtyard centred 1.8 mm further toward +y: mouth (0, 1)."""
    return cls(_Box(-3, -1, 3, 5), _Box(-8, -3, 8, 6), pads=(_pad(-1, 0.2), _pad(1, 0.2)))


@pytest.fixture
def stub_pcbnew(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pe, "pcbnew", _PCB)


@pytest.mark.parametrize("cls", [_Footprint904, _LazyFootprint])
def test_fp_extent_builds_courtyard_caches_before_reading_them(stub_pcbnew, cls: type) -> None:
    fp = _cap(cls)
    assert pe.fp_extent(fp) == pytest.approx((3.05, 1.55, 0.0, 0.1))   # not the silk bbox
    built = ["BuildCourtyardCaches"] if cls is _Footprint904 else []  # hasattr-guarded
    assert fp.calls[:2 + len(built)] == ["SetPosition", *built, "GetCourtyard"]


def test_fp_extent_falls_back_to_the_bounding_box_without_a_courtyard(stub_pcbnew) -> None:
    fp = _Footprint904(None, _Box(-2, -1, 2, 1))
    assert pe.fp_extent(fp) == pytest.approx((4.0, 2.0, 0.0, 0.0))


@pytest.mark.parametrize("cls", [_Footprint904, _LazyFootprint])
def test_fp_mouth_reads_the_built_courtyard(stub_pcbnew, cls: type) -> None:
    fp = _edge_jack(cls)
    assert pe.fp_mouth(fp) == (0.0, 1.0)
    assert ("BuildCourtyardCaches" in fp.calls) == (cls is _Footprint904)


def test_fp_mouth_is_indeterminate_without_a_front_courtyard(stub_pcbnew) -> None:
    # no F.CrtYd at all: no mouth, rather than one measured from the origin
    fp = _Footprint904(None, _Box(-8, -3, 8, 6), pads=(_pad(-1, 3.0), _pad(1, 3.0)))
    assert pe.fp_mouth(fp) == (0.0, 0.0)
