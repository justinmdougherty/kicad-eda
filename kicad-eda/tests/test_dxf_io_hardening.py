"""Hardening tests for server/tools/dxf_io.py and dxf_loops.py (F3-05): units
from $MEASUREMENT, KiCad's own Edge.Cuts DXF exports (colour / Edge.Cuts layer
names, drill circles), corrupt values, degenerate loops, the gap-message cap,
the frame named in messages and the Fusion datum rule (D2: the sketch origin).

The KiCad fixtures under tests/fixtures/dxf/ are kicad-cli exports of the T10
board (60 x 40 mm, r=4 corners, a stadium and a circle cutout, aux origin at
board (10, 30)), made with `pcb export dxf --layers Edge.Cuts --use-drill-origin
--ou mm --drill-shape-opt 0 --mode-single`: k9 by KiCad 9.0.4 (R12, no
$INSUNITS, layer WHITE), k11 by 10.99 (AC1018, $INSUNITS 4, layer Edge.Cuts).
Every other DXF is generated with ezdxf (tests/fixtures/dxf_builders.py).
"""
from __future__ import annotations

import json
import math
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

import dxf_io
import dxf_loops
import outline as ol
from dxf_io import DxfOutlineError
from dxf_loops import Piece
from fixtures.dxf_builders import (SKETCH_LAYER, fusion_doc, new_doc, rect_lines, rect_poly,
                                   ring_dist, save)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "dxf"
K9 = FIXTURES / "k9_mm_nodrill.dxf"
K11 = FIXTURES / "k11_mm_nodrill.dxf"
T10_BBOX = (-10.0, -10.0, 50.0, 30.0)        # external frame (Y up, origin = the datum)
DRILL_HINT = "looks like a drill hole; export with --drill-shape-opt 0"
CORRUPT_MSG = "with corrupt values"
# a Fusion sketch on the XY plane moved by (1, 2, 0) cm: world = sketch + t
XLATE = (1, 0, 0, 1, 0, 1, 0, 2, 0, 0, 1, 0, 0, 0, 0, 1)


def _outer(res: dxf_io.BoardOutline) -> dxf_loops.Loop:
    """The outer loop in the drawing frame (mm, Y up, the frame's origin)."""
    return max(res.source.loops, key=lambda lp: abs(lp.area))


def _r12_rect(tmp_path: Path, measurement: int | None, name: str = "r12.dxf") -> Path:
    """A 2 x 1.5 drawing-unit rectangle in a minimal R12 file shaped like
    kicad-cli 9's: a HEADER with $MEASUREMENT only (ezdxf writes no
    $MEASUREMENT for R12, so the text is written by hand)."""
    header = ("" if measurement is None
              else f"0\nSECTION\n2\nHEADER\n9\n$MEASUREMENT\n70\n{measurement}\n0\nENDSEC\n")
    lines = "".join(f"0\nLINE\n8\nWHITE\n10\n{a[0]}\n20\n{a[1]}\n11\n{b[0]}\n21\n{b[1]}\n"
                    for a, b in (((0, 0), (2, 0)), ((2, 0), (2, 1.5)), ((2, 1.5), (0, 1.5)),
                                 ((0, 1.5), (0, 0))))
    path = tmp_path / name
    path.write_text(header + "0\nSECTION\n2\nENTITIES\n" + lines + "0\nENDSEC\n0\nEOF\n",
                    encoding="ascii")
    return path


# --- units: override > non-zero $INSUNITS > $MEASUREMENT > mm ------------------------
def test_kicad9_export_has_no_insunits_and_reads_as_mm_via_measurement() -> None:
    res = dxf_io.read_board_outline(K9)
    assert res.source.units == "mm" and res.source.units_source == "$MEASUREMENT"
    assert any("$MEASUREMENT 1" in w and "$INSUNITS" in w for w in res.warnings), res.warnings
    assert _outer(res).bbox() == pytest.approx(T10_BBOX, abs=0.01)


@pytest.mark.parametrize("insunits", [None, 0])
def test_measurement_zero_reads_inches(tmp_path: Path, insunits: int | None) -> None:
    if insunits is None:
        path = _r12_rect(tmp_path, 0)
    else:
        doc, msp = new_doc(insunits=insunits)
        doc.header["$MEASUREMENT"] = 0
        rect_poly(msp, 0, 0, 2, 1.5)
        path = save(doc, tmp_path)
    res = dxf_io.read_board_outline(path)
    assert res.size == pytest.approx((50.8, 38.1))
    assert res.source.units == "in" and res.source.units_source == "$MEASUREMENT"
    assert any("$MEASUREMENT 0" in w and "in" in w for w in res.warnings), res.warnings


def test_measurement_one_reads_mm_with_a_warning(tmp_path: Path) -> None:
    res = dxf_io.read_board_outline(_r12_rect(tmp_path, 1))
    assert res.size == pytest.approx((2, 1.5))
    assert res.source.units_source == "$MEASUREMENT"
    assert any("$MEASUREMENT 1" in w for w in res.warnings)


def test_nonzero_insunits_wins_over_measurement(tmp_path: Path) -> None:
    doc, msp = new_doc(insunits=4)
    doc.header["$MEASUREMENT"] = 0                 # imperial flag, mm units: units win
    rect_poly(msp, 0, 0, 50, 30)
    res = dxf_io.read_board_outline(save(doc, tmp_path))
    assert res.size == pytest.approx((50, 30))
    assert res.source.units_source == "$INSUNITS"
    assert not any("$MEASUREMENT" in w for w in res.warnings)
    fusion, fmsp = fusion_doc()                    # Fusion: $INSUNITS 5 + $MEASUREMENT 0
    rect_poly(fmsp, 0, 0, 5, 3, layer=SKETCH_LAYER)
    cm = dxf_io.read_board_outline(save(fusion, tmp_path, "fusion.dxf"))
    assert cm.source.units == "cm" and cm.size == pytest.approx((50, 30))


def test_units_override_wins_over_measurement(tmp_path: Path) -> None:
    res = dxf_io.read_board_outline(_r12_rect(tmp_path, 0), units="mm")
    assert res.size == pytest.approx((2, 1.5))
    assert res.source.units_source == "override"
    assert not any("$MEASUREMENT" in w for w in res.warnings)


@pytest.mark.parametrize("measurement", [None, 2])
def test_without_a_usable_measurement_the_default_is_mm(tmp_path: Path,
                                                         measurement: int | None) -> None:
    res = dxf_io.read_board_outline(_r12_rect(tmp_path, measurement))
    assert res.source.units == "mm" and res.source.units_source == "default"
    assert any("$INSUNITS" in w and "$MEASUREMENT" in w for w in res.warnings), res.warnings


@pytest.mark.parametrize("blank", ["", "  "])
def test_blank_units_is_no_override(tmp_path: Path, blank: str) -> None:
    res = dxf_io.read_board_outline(_r12_rect(tmp_path, 0), units=blank)
    assert res.source.units_source == "$MEASUREMENT" and res.size == pytest.approx((50.8, 38.1))


# --- KiCad's own Edge.Cuts exports (T10 round trip) ----------------------------------
@pytest.mark.parametrize("path", [K9, K11], ids=["kicad9", "kicad10.99"])
@pytest.mark.parametrize("layer", [None, ""])
def test_kicad_exports_read_back_to_the_t10_outline(path: Path, layer: str | None) -> None:
    assert path.is_file(), f"fixture missing: {path}"
    res = dxf_io.read_board_outline(path, layer=layer)        # no units argument
    assert res.source.units == "mm"
    assert _outer(res).bbox() == pytest.approx(T10_BBOX, abs=0.01)
    assert res.size == pytest.approx((60, 40), abs=0.01)
    assert res.datum == pytest.approx((10, 30), abs=0.01)
    assert len(res.cutouts) == 2
    assert sorted(c.circle is not None for c in res.cutouts) == [False, True]
    assert not any(DRILL_HINT in w for w in res.warnings)
    assert ol.from_board_block(res.to_board_block())         # the next stage accepts it


def test_kicad9_and_kicad11_exports_agree_within_0_01_mm() -> None:
    a, b = dxf_io.read_board_outline(K9), dxf_io.read_board_outline(K11)
    assert len(a.source.loops) == len(b.source.loops) == 3

    def rings(res: dxf_io.BoardOutline) -> list[list[tuple[float, float]]]:
        loops = sorted(res.source.loops, key=lambda lp: lp.bbox())
        return [lp.ring(0.001) for lp in loops]

    worst = 0.0
    for ra, rb in zip(rings(a), rings(b), strict=True):
        worst = max(worst, max(ring_dist(p, rb) for p in ra), max(ring_dist(p, ra) for p in rb))
    assert worst <= 0.01


def test_kicad_layer_names() -> None:
    assert dxf_io.read_board_outline(K9, layer="white").source.layers == ("WHITE",)
    assert dxf_io.read_board_outline(K11, layer="edge.cuts").source.layers == ("Edge.Cuts",)
    with pytest.raises(DxfOutlineError, match=r"(?s)layer 'Edge.Cuts'.*WHITE") as info:
        dxf_io.read_board_outline(K9, layer="Edge.Cuts")
    assert "layer=''" in str(info.value)           # how to read every layer


def test_missing_layer_lists_the_available_layers(tmp_path: Path) -> None:
    doc, msp = new_doc()
    doc.layers.add("OUTLINE")
    doc.layers.add("EMPTY")
    rect_poly(msp, 0, 0, 50, 30, layer="OUTLINE")
    path = save(doc, tmp_path)
    with pytest.raises(DxfOutlineError) as info:
        dxf_io.read_board_outline(path, layer="Edge.Cuts")
    msg = str(info.value)
    assert "not in the DXF" in msg and "OUTLINE (1)" in msg and "EMPTY" in msg
    with pytest.raises(DxfOutlineError, match="no outline geometry on layer 'EMPTY'"):
        dxf_io.read_board_outline(path, layer="EMPTY")


@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_layer_reads_every_layer(tmp_path: Path, blank: str) -> None:
    doc, msp = new_doc()
    rect_poly(msp, 0, 0, 50, 30, layer="A")
    rect_poly(msp, 10, 10, 20, 15, layer="B")
    res = dxf_io.read_board_outline(save(doc, tmp_path), layer=blank)
    assert len(res.cutouts) == 1 and set(res.source.layers) == {"A", "B"}
    spec = dxf_io.outline_from_spec({"dxf": "board.dxf", "layer": blank}, tmp_path)
    assert len(spec.cutouts) == 1


# --- drill circles ---------------------------------------------------------------
def test_small_circle_cutouts_are_reported_as_drill_holes(tmp_path: Path) -> None:
    doc, msp = new_doc()
    rect_poly(msp, 0, 0, 50, 30)
    via = msp.add_circle((5, 5), 0.4)              # 0.8 mm: a via / small drill
    pin = msp.add_circle((10, 5), 0.5)             # 1.0 mm: KiCad 9's pin-header drill
    hole = msp.add_circle((30, 15), 1.6)           # 3.2 mm: a mounting hole, milled
    res = dxf_io.read_board_outline(save(doc, tmp_path))
    assert len(res.cutouts) == 3                   # still cutouts: only a warning
    drills = [w for w in res.warnings if DRILL_HINT in w]
    assert len(drills) == 2
    assert any(f"CIRCLE #{via.dxf.handle}" in w and "0.800 mm" in w for w in drills)
    assert any(f"CIRCLE #{pin.dxf.handle}" in w for w in drills)
    assert not any(f"#{hole.dxf.handle}" in w for w in drills)


# --- corrupt values -----------------------------------------------------------------
def _insert_nan_scale(msp: object) -> object:
    blk = msp.doc.blocks.new("HOLE")
    blk.add_circle((0, 0), 1.0)
    return msp.add_blockref("HOLE", (20, 15), dxfattribs={"xscale": math.nan})


CORRUPT: dict[str, Callable[[object], object]] = {
    "circle radius inf": lambda m: m.add_circle((10, 10), math.inf),
    "circle radius 1e300": lambda m: m.add_circle((10, 10), 1e300),
    "line nan x": lambda m: m.add_line((math.nan, 5), (10, 5)),
    "arc nan angle": lambda m: m.add_arc((10, 10), 2, math.nan, 90),
    "lwpolyline inf vertex": lambda m: m.add_lwpolyline([(5, 5), (math.inf, 5), (8, 8)]),
    "lwpolyline nan bulge": lambda m: m.add_lwpolyline([(5, 5, math.nan), (8, 5)],
                                                        format="xyb"),
    "lwpolyline 1e9 bulge": lambda m: m.add_lwpolyline([(5, 5, 1e9), (6, 5)], format="xyb"),
    "spline nan fit point": lambda m: m.add_spline(fit_points=[(5, 5), (6, math.nan), (7, 5)]),
    "ellipse 1e300 axis": lambda m: m.add_ellipse((10, 10), (1e300, 0, 0), 0.5),
    "line 2e6 mm away": lambda m: m.add_line((2e6, 5), (2e6, 6)),
    "insert nan scale": _insert_nan_scale,
}


@pytest.mark.parametrize("kind", sorted(CORRUPT))
def test_corrupt_values_raise_naming_the_entity(tmp_path: Path, kind: str) -> None:
    doc, msp = new_doc()
    rect_poly(msp, 0, 0, 50, 30)
    bad = CORRUPT[kind](msp)
    path = save(doc, tmp_path)
    with pytest.raises(DxfOutlineError) as info:
        dxf_io.read_board_outline(path)
    msg = str(info.value).replace(str(path), "<path>")     # tmp_path names the test
    assert f"{bad.dxftype()} #{bad.dxf.handle}" in msg, msg
    assert CORRUPT_MSG in msg, msg
    assert info.value.problems and all(f"#{bad.dxf.handle}" in p for p in info.value.problems)


def test_the_size_limit_applies_after_unit_scaling(tmp_path: Path) -> None:
    doc, msp = new_doc(insunits=4)                 # 200 m in mm: implausible, not corrupt
    rect_poly(msp, 0, 0, 2e5, 1e5)
    res = dxf_io.read_board_outline(save(doc, tmp_path, "mm.dxf"))
    assert res.size == pytest.approx((2e5, 1e5))
    assert any("implausible" in w for w in res.warnings)
    doc, msp = new_doc(insunits=5)                 # the same numbers in cm: 2 km
    poly = rect_poly(msp, 0, 0, 2e5, 1e5)
    with pytest.raises(DxfOutlineError, match=rf"LWPOLYLINE #{poly.dxf.handle}.*1e\+06 mm"):
        dxf_io.read_board_outline(save(doc, tmp_path, "cm.dxf"))


def test_a_corrupt_entity_on_another_layer_is_ignored(tmp_path: Path) -> None:
    doc, msp = new_doc()
    rect_poly(msp, 0, 0, 50, 30, layer="OUTLINE")
    msp.add_circle((10, 10), math.inf, dxfattribs={"layer": "NOTES"})
    path = save(doc, tmp_path)
    assert dxf_io.read_board_outline(path, layer="OUTLINE").size == pytest.approx((50, 30))
    with pytest.raises(DxfOutlineError) as info:
        dxf_io.read_board_outline(path)
    assert CORRUPT_MSG in str(info.value).replace(str(path), "<path>")


def test_cli_reports_corrupt_values_as_json_without_a_traceback(tmp_path: Path) -> None:
    doc, msp = new_doc()
    rect_poly(msp, 0, 0, 50, 30)
    bad = msp.add_circle((10, 10), math.inf)
    path = save(doc, tmp_path)
    proc = subprocess.run([sys.executable, str(Path(dxf_io.__file__)), "read", str(path)],
                          stdin=subprocess.DEVNULL, capture_output=True, encoding="utf-8",
                          errors="replace", timeout=120)
    assert proc.returncode == 1, proc.stderr
    assert "Traceback" not in proc.stderr and "Traceback" not in proc.stdout
    out = json.loads(proc.stdout)
    assert out["ok"] is False and f"CIRCLE #{bad.dxf.handle}" in out["error"]


# --- degenerate loops ---------------------------------------------------------------
def test_a_hairline_ellipse_cutout_is_dropped(tmp_path: Path) -> None:
    doc, msp = new_doc()
    rect_poly(msp, 0, 0, 50, 30)
    ell = msp.add_ellipse((10, 10), (5, 0, 0), 1e-10)
    res = dxf_io.read_board_outline(save(doc, tmp_path))
    assert res.cutouts == ()
    assert any("degenerate" in w and f"ELLIPSE #{ell.dxf.handle}" in w for w in res.warnings)
    assert ol.from_board_block(res.to_board_block()).cutouts == ()


@pytest.mark.parametrize(("width", "kept"), [(0.005, False), (0.05, True)])
def test_a_sliver_narrower_than_the_join_tolerance_is_dropped(tmp_path: Path, width: float,
                                                              kept: bool) -> None:
    doc, msp = new_doc()
    rect_poly(msp, 0, 0, 50, 30)
    sliver = rect_poly(msp, 10, 10, 40, 10 + width)   # area 0.15 mm^2 > tol^2 at 0.005
    res = dxf_io.read_board_outline(save(doc, tmp_path))
    assert len(res.cutouts) == int(kept)
    dropped = [w for w in res.warnings if "degenerate" in w]
    assert bool(dropped) != kept
    assert all(f"LWPOLYLINE #{sliver.dxf.handle}" in w for w in dropped)
    ol.from_board_block(res.to_board_block())


def test_only_degenerate_loops_is_an_error(tmp_path: Path) -> None:
    doc, msp = new_doc()
    msp.add_ellipse((10, 10), (5, 0, 0), 1e-10)
    path = save(doc, tmp_path)
    with pytest.raises(DxfOutlineError) as info:
        dxf_io.read_board_outline(path)
    assert "every closed loop is degenerate" in str(info.value).replace(str(path), "<path>")


def test_drop_degenerate_directly() -> None:
    square = dxf_loops.Loop(((0, 0, 0), (10, 0, 0), (10, 10, 0), (0, 10, 0)), ("sq",))
    tiny = dxf_loops.Loop(((0, 0, 0), (0.005, 0, 0), (0.005, 0.005, 0)), ("tiny",))
    sliver = dxf_loops.Loop(((0, 0, 0), (30, 0, 0), (30, 0.004, 0), (0, 0.004, 0)), ("sl",))
    hole = dxf_loops.circle_loop(5, 5, 0.02, "c")
    warnings: list[str] = []
    kept = dxf_loops.drop_degenerate([square, tiny, sliver, hole], 0.01, warnings)
    assert kept == [square, hole]
    assert len(warnings) == 2 and "tiny" in warnings[0] and "sl" in warnings[1]


# --- gap messages: capped, naming the frame -------------------------------------------
def test_1500_gaps_give_a_short_message_and_the_full_list(tmp_path: Path) -> None:
    doc, msp = new_doc()
    rect_poly(msp, 0, 0, 500, 300)
    for i in range(1500):
        msp.add_line((10 + i * 0.3, 10), (10 + i * 0.3, 20))
    with pytest.raises(DxfOutlineError) as info:
        dxf_io.read_board_outline(save(doc, tmp_path))
    err = info.value
    assert len(str(err).encode("utf-8")) < 4096
    assert len(err.gaps) == 1500
    assert "+1480 more" in str(err)
    assert sum(line.startswith("  gap ") for line in str(err).splitlines()) == 20


def _strays(n: int) -> list[list[Piece]]:
    return [[Piece(((3.0 * k, 0.0, 3.0 * k, 10.0, 0.0),), f"L{k}", f"L{k} start", f"L{k} end")]
            for k in range(n)]


@pytest.mark.parametrize(("n", "more"), [(20, None), (21, "+1 more")])
def test_gap_list_cap_boundary(n: int, more: str | None) -> None:
    err = dxf_loops.gap_error("x.dxf", _strays(n), 0.01, 0)
    assert len(err.gaps) == n
    assert ("more" in str(err)) == (more is not None)
    if more:
        assert more in str(err)


def test_pairing_is_closest_first_and_complete() -> None:
    chains = _strays(7)                            # 14 ends, 3 mm apart in two rows
    gaps = dxf_loops.gap_error("x.dxf", chains, 0.01, 0).gaps
    assert len(gaps) == 7
    assert [round(g.distance, 6) for g in gaps] == sorted(round(g.distance, 6) for g in gaps)
    ends = {(g.a, g.a_at) for g in gaps} | {(g.b, g.b_at) for g in gaps}
    assert len(ends) == 14


def test_gap_and_branch_messages_name_the_xy_frame(tmp_path: Path) -> None:
    doc, msp = new_doc()
    rect_lines(msp, gap=0.25)
    with pytest.raises(DxfOutlineError, match="frame: XY") as info:
        dxf_io.read_board_outline(save(doc, tmp_path, "gap.dxf"))
    assert "DXF frame" not in str(info.value)
    doc, msp = new_doc()
    rect_lines(msp)
    msp.add_line((50, 30), (60, 40))
    with pytest.raises(DxfOutlineError, match="frame: XY"):
        dxf_io.read_board_outline(save(doc, tmp_path, "branch.dxf"))
    doc, msp = new_doc()
    rect_poly(msp, 0, 0, 50, 30)
    rect_poly(msp, 60, 0, 65, 5)
    with pytest.raises(DxfOutlineError, match="frame: XY"):
        dxf_io.read_board_outline(save(doc, tmp_path, "stray.dxf"))


def test_gap_in_a_translated_fusion_sketch_is_reported_in_the_sketch_frame(
        tmp_path: Path) -> None:
    doc, msp = fusion_doc(transform=XLATE)
    lay = {"layer": SKETCH_LAYER}
    # world (cm) = sketch + (1, 2); the top edge stops 0.25 mm short of the corner
    for a, b in (((1, 2), (6, 2)), ((6, 2), (6, 5)), ((6, 5), (1.025, 5)), ((1, 5), (1, 2))):
        msp.add_line(a, b, dxfattribs=lay)
    with pytest.raises(DxfOutlineError) as info:
        dxf_io.read_board_outline(save(doc, tmp_path))
    msg = str(info.value)
    assert "frame: Fusion sketch transform" in msg
    assert "(0.250, 30.000)" in msg and "(0.000, 30.000)" in msg   # sketch, not world


# --- Fusion datum = the sketch origin (D2) ----------------------------------------------
def test_translated_fusion_transform_datum_is_the_sketch_origin(tmp_path: Path) -> None:
    doc, msp = fusion_doc(transform=XLATE)
    rect_poly(msp, 1, 2, 6, 5, layer=SKETCH_LAYER)     # world cm = sketch (0,0)-(5,3) cm
    res = dxf_io.read_board_outline(save(doc, tmp_path))
    assert "Fusion" in res.source.plane
    assert res.size == pytest.approx((50, 30))
    # sketch origin = the rectangle's bottom-left: board (0, 30); the world
    # origin would be (-10, 50)
    assert res.datum == pytest.approx((0, 30))
    assert res.to_board_block()["datum"] == pytest.approx([0, 30])
    assert _outer(res).bbox() == pytest.approx((0, 0, 50, 30))


def test_docstrings_state_the_frame_order_and_the_datum_rule() -> None:
    doc = dxf_io.__doc__
    assert "sketch origin" in doc
    assert doc.index("NFusion_SketchTransform") < doc.index("else XY")
    pick = dxf_io._pick_frame.__doc__
    assert pick.index("Fusion sketch transform") < pick.index("XY")
    assert "sketch origin" in (dxf_io.BoardOutline.__doc__ or "")
