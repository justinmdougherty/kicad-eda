"""fp_author / fp_author_templates: footprints authored in the D0 dialect (F2, D1).

Unit tier (venv only, no KiCad):
  * Quectel LG290P(03): 79 pads at the datasheet coordinates and sizes. The
    expected values are a literal table typed from Hardware Design V1.0
    Fig. 18 / Fig. 3, not the template's helpers. Pad 25 is round and
    pad = 25 + 11*col + row. The header is in the D0 dialect and the courtyard
    is the max-material body + 0.25 mm; the pin-1 marker fits inside it;
  * the courtyard rule (IPC-7351B nominal 0.25 mm excess around the max body
    and all copper, grown to cover the pin-1 marker when it has no room inside,
    on a 0.01 mm grid) and the silk rule (>= 0.2 mm from copper, checked here
    by an independent point sampler) on every template;
  * QFN with exposed pad, thermal vias and paste windows; DFN; SOP; LCC; pin header;
  * spec and template inputs fail loudly; the reader names what it cannot read;
    validate() reports static problems and kicad-cli failures (fake runner);
  * pcbnew_load's KiCad-python child imports kicad_quiet before pcbnew (run
    with the venv Python and a stub pcbnew).
  compare_footprints and the reader split are tested in test_fp_compare.py.

KiCad tier (skipped without the files or the tools; never needed by CI):
  * the SOP / QFN templates reproduce KiCad's own stock SOIC-8 and
    QFN-16 thermal-via pads (and paste windows) exactly;
  * the oldest installed KiCad (9 here, 10 in Docker) parses every generated
    footprint (kicad-cli fp upgrade) and loads it through pcbnew.FootprintLoad
    in a child (stdin DEVNULL, kicad_quiet first). That includes the LG290P
    with 79 pads.
"""
from __future__ import annotations

import json
import math
import re
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Callable

import pytest

import fp_author as fa
import fp_author_templates as ft
import kicad_env as env
from fixtures.kicad_install import KICAD

# --- LG290P(03) Hardware Design V1.0, Fig. 18 (mm, KiCad frame: +Y down) ----
LG_EDGE_Y = [-7.0, -5.9, -4.8, -3.7, -2.6, -1.5, -0.4, 2.6, 3.7, 4.8, 5.9, 7.0]   # pins 1-12
LG_INNER_X = [-2.8, -1.4, 0.0, 1.4, 2.8]
LG_INNER_Y = [-7.0, -5.6, -4.2, -2.8, -1.4, 0.0, 1.4, 2.8, 4.2, 5.6, 7.0]


def lg_expected() -> dict[str, tuple[float, float, float, float, str]]:
    """number -> (x, y, w, h, shape), typed from the datasheet figure."""
    exp = {}
    for i, y in enumerate(LG_EDGE_Y):
        exp[str(i + 1)] = (-5.05, y, 1.5, 0.8, "rect")             # left, top to bottom
        exp[str(24 - i)] = (5.05, y, 1.5, 0.8, "rect")             # right: 24 top .. 13 bottom
    exp.update({"25": (-2.8, -7.0, 1.0, 1.0, "circle"), "36": (-1.4, -7.0, 1.0, 1.0, "rect"),
                "69": (2.8, -7.0, 1.0, 1.0, "rect"), "35": (-2.8, 7.0, 1.0, 1.0, "rect"),
                "79": (2.8, 7.0, 1.0, 1.0, "rect"), "30": (-2.8, 0.0, 1.0, 1.0, "rect"),
                "52": (0.0, 0.0, 1.0, 1.0, "rect")})
    return exp


@pytest.fixture(scope="module")
def lg_file(tmp_path_factory) -> Path:
    return fa.write_footprint(ft.lg290p(), tmp_path_factory.mktemp("lg") / "SPS_parts.pretty")


def pads_by_number(path: Path) -> dict[str, fa.ParsedPad]:
    pads = [p for p in fa.read_footprint(path).pads if p.number]
    out = {p.number: p for p in pads}
    assert len(out) == len(pads), "duplicate pad numbers"
    return out


def crtyd_rect(path: Path) -> tuple[float, float, float, float]:
    pts = [pt for a, b, _ in fa.read_footprint(path).strokes["F.CrtYd"] for pt in (a, b)]
    return (min(x for x, _ in pts), min(y for _, y in pts),
            max(x for x, _ in pts), max(y for _, y in pts))


# ---------------------------------------------------------------------------
# LG290P
# ---------------------------------------------------------------------------
def test_lg290p_has_79_uniquely_numbered_smd_pads(lg_file):
    pads = pads_by_number(lg_file)
    assert sorted(pads, key=int) == [str(n) for n in range(1, 80)]
    assert all(p.type == "smd" and p.pad_layers == ("F.Cu", "F.Mask", "F.Paste")
               for p in pads.values())


def test_lg290p_edge_pads_match_the_datasheet(lg_file):
    pads = pads_by_number(lg_file)
    for n, (x, y, w, h, shape) in lg_expected().items():
        p = pads[n]
        assert (p.x, p.y, p.w, p.h) == pytest.approx((x, y, w, h)), n
        assert p.shape == shape and p.rotation == 0, n


def test_lg290p_inner_grid_and_numbering_rule(lg_file):
    pads = pads_by_number(lg_file)
    for col, x in enumerate(LG_INNER_X):
        for row, y in enumerate(LG_INNER_Y):
            p = pads[str(25 + 11 * col + row)]
            assert (p.x, p.y, p.w, p.h) == pytest.approx((x, y, 1.0, 1.0)), (col, row)
            assert p.shape == ("circle" if (col, row) == (0, 0) else "rect")
    # pitch 1.4 in both axes and the first column 3.3 mm from the body's left edge
    assert pads["36"].x - pads["25"].x == pytest.approx(1.4)
    assert pads["26"].y - pads["25"].y == pytest.approx(1.4)
    assert pads["25"].x - (-12.2 / 2) == pytest.approx(3.3)
    assert pads["1"].x - (-12.2 / 2) == pytest.approx(1.05)
    assert pads["8"].y - pads["7"].y == pytest.approx(3.0)                    # the 3 mm gap


def test_lg290p_header_is_the_d0_dialect(lg_file):
    text = lg_file.read_text(encoding="utf-8")
    assert text.startswith('(footprint "Quectel_LG290P"\n\t(version 20241229)\n'
                           '\t(generator "kicad-eda")\n')
    fp = fa.read_footprint(lg_file)
    assert fp.version == fa.FORMAT_VERSION == 20241229 and fp.attr == ("smd",)
    assert fa.static_problems(fp, lg_file, expected_pads=79) == []


def test_lg290p_courtyard_fab_and_pin1(lg_file):
    # IPC-7351B: max-material body (12.2+0.3) x (16.0+0.3) + 0.25 mm excess, unchanged
    # by the pin-1 marker, which fits inside it
    assert crtyd_rect(lg_file) == pytest.approx((-6.5, -8.4, 6.5, 8.4))
    fab = [pt for a, b, _ in fa.read_footprint(lg_file).strokes["F.Fab"] for pt in (a, b)]
    assert (min(x for x, _ in fab), max(x for x, _ in fab)) == pytest.approx((-6.1, 6.1))
    assert (min(y for _, y in fab), max(y for _, y in fab)) == pytest.approx((-8.0, 8.0))
    assert (-6.1, -8.0) not in fab and (-5.1, -8.0) in fab and (-6.1, -7.0) in fab  # chamfer
    # above pin 1 (x = -5.05), pointing down at it, 0.27 mm (0.2 + half a stroke) off its copper
    placed = fa.place_pin1_marker(ft.lg290p())
    assert placed.side == "T" and placed.inside and placed.notch
    assert list(placed.points) == pytest.approx([(-5.05, -7.67), (-5.3, -8.07), (-4.8, -8.07)])
    # the top silk line yields to it (a notch), SILK_WIDTH + SILK_GAP clear on either side
    top = sorted(s for s in fa.silk_segments(ft.lg290p()) if s[0][1] == s[1][1] == -8.11)
    assert top == [((-6.21, -8.11), (-5.47, -8.11)), ((-4.63, -8.11), (6.21, -8.11))]


def test_lg290p_pin1_side_auto_explicit_and_none():
    assert fa.pin1_marker(ft.lg290p(pin1_side="auto")) == fa.pin1_marker(ft.lg290p())
    # left of pin 1 there is no room inside the courtyard: an explicit "L" grows it
    left = ft.lg290p(pin1_side="L")
    assert fa.pin1_marker(left)[0] == pytest.approx((-6.42, -7.0))
    assert not fa.place_pin1_marker(left).inside
    assert fa.courtyard_rect(left) == pytest.approx((-6.88, -8.4, 6.5, 8.4))
    for spec in (ft.lg290p(pin1_side="none"), ft.lg290p(pin1=None)):
        assert fa.pin1_marker(spec) is None and len(fa.silk_segments(spec)) == 4
        assert fa.courtyard_rect(spec) == pytest.approx((-6.5, -8.4, 6.5, 8.4))
        assert fa.render(spec).count("(fp_poly") == 1                       # the fab outline
    assert len(fa.fab_outline(ft.lg290p(pin1=None))) == 4                   # no chamfer


def test_lg290p_forwards_spec_keywords():
    spec = ft.lg290p(descr="custom", courtyard_excess=0.5, pin1=None)
    assert spec.descr == "custom" and spec.pin1 is None
    assert spec.datasheet == ft.LG290P_DATASHEET and spec.name == ft.LG290P_NAME
    assert fa.courtyard_rect(spec) == pytest.approx((-6.75, -8.65, 6.75, 8.65))
    with pytest.raises(TypeError):
        ft.lg290p(fa.Model("x.step"))                                       # keywords only


def test_lg290p_summary_and_pin_groups():
    s = fa.summary(ft.lg290p())
    assert s["pads"] == 79 and s["height"] == pytest.approx(2.8)
    groups = ft.pad_groups(ft.LG290P_PAD_FUNCTIONS)
    assert sorted(ft.LG290P_PAD_FUNCTIONS, key=int) == [str(n) for n in range(1, 80)]
    assert groups["GND"] == ["10", "12", "13"] + [str(n) for n in range(24, 80)]
    assert groups["RESERVED"] == ["1", "2", "5", "17"]
    assert (groups["VCC"], groups["RF_IN"], groups["1PPS"]) == (["23"], ["11"], ["3"])


def test_pad_groups_sort_pad_names_naturally():
    functions = {"B2": "X", "A10": "X", "A2": "X", "10": "Y", "9": "Y"}
    assert ft.pad_groups(functions) == {"X": ["A2", "A10", "B2"], "Y": ["9", "10"]}


# ---------------------------------------------------------------------------
# Courtyard and silk rules on every template
# ---------------------------------------------------------------------------
def soic8() -> fa.FootprintSpec:
    return ft.sop("SOIC-8_3.9x4.9mm_P1.27mm", body=fa.Body(3.9, 4.9), pins=8, pitch=1.27,
                  span=4.95, pad_w=0.6, pad_l=1.95)


def qfn16_vias() -> fa.FootprintSpec:
    ep = ft.ExposedPad(1.7, 1.7, paste_grid=(2, 2), paste_coverage=(0.69 / 0.85) ** 2,
                       vias=ft.ThermalVias(2, 2, pitch=1.2, drill=0.2, size=0.5))
    return ft.qfn("QFN-16-1EP_3x3mm_P0.5mm_EP1.7x1.7mm_ThermalVias", body=fa.Body(3.0, 3.0),
                  pitch=0.5, pins_x=4, pins_y=4, span_x=2.925, span_y=2.925, pad_w=0.25,
                  pad_l=0.825, ep=ep)


def all_templates() -> list[fa.FootprintSpec]:
    return [ft.lg290p(), soic8(), qfn16_vias(),
            ft.dfn("DFN-8_2x2mm_P0.5mm", body=fa.Body(2.0, 2.0), pitch=0.5, pins_per_side=4,
                   span=1.9, pad_w=0.25, pad_l=0.6, ep=ft.ExposedPad(0.9, 1.6)),
            ft.lcc_castellated("ESP32-like_LCC", body=fa.Body(18.0, 25.5), pitch=1.27,
                               pins=(14, 10, 14, 0), pad_w=0.9, pad_l=1.5, edge_offset=0.25),
            ft.pin_header("PinHeader_2x03_P2.54mm", rows=3, cols=2)]


def sampled_silk_gap(path: Path, step: float = 0.01) -> float:
    """Independent check: the smallest gap from any sampled silk point (minus
    half the stroke) to any front-copper pad (circles exact, rects axis-aligned)."""
    fp = fa.read_footprint(path)
    pads = [p for p in fp.pads if {"F.Cu", "*.Cu"} & set(p.pad_layers)]
    assert all(p.rotation % 180 == 0 for p in pads)
    worst = math.inf
    for (ax, ay), (bx, by), width in fp.strokes["F.SilkS"]:
        n = max(1, math.ceil(math.hypot(bx - ax, by - ay) / step))
        for k in range(n + 1):
            x, y = ax + (bx - ax) * k / n, ay + (by - ay) * k / n
            for p in pads:
                if p.shape == "circle":
                    d = math.hypot(x - p.x, y - p.y) - p.w / 2
                else:
                    d = math.hypot(max(abs(x - p.x) - p.w / 2, 0), max(abs(y - p.y) - p.h / 2, 0))
                worst = min(worst, d - width / 2)
    return worst


@pytest.mark.parametrize("spec", all_templates(), ids=lambda s: s.name)
def test_every_template_obeys_the_silk_and_courtyard_rules(spec, tmp_path):
    path = fa.write_footprint(spec, tmp_path / "t.pretty")
    fp = fa.read_footprint(path)
    assert fa.static_problems(fp, path) == []
    assert fp.strokes["F.SilkS"], "silk outline / pin-1 marker missing"
    assert sampled_silk_gap(path) >= fa.SILK_PAD_CLEARANCE_MM - 0.005
    # courtyard: union(max body, pad copper) + 0.25 mm, grown to cover every silk
    # stroke (the pin-1 marker), rounded OUT to 0.01 mm
    boxes = [fa.pad_bbox(p) for p in fp.pads if any(x.endswith(".Cu") for x in p.pad_layers)]
    boxes.append(spec.body.max_rect())
    silk = [(x, y, w / 2) for a, b, w in fp.strokes["F.SilkS"] for x, y in (a, b)]
    raw = (min(min(b[0] for b in boxes) - 0.25, *(x - h for x, _, h in silk)),
           min(min(b[1] for b in boxes) - 0.25, *(y - h for _, y, h in silk)),
           max(max(b[2] for b in boxes) + 0.25, *(x + h for x, _, h in silk)),
           max(max(b[3] for b in boxes) + 0.25, *(y + h for _, y, h in silk)))
    got = crtyd_rect(path)
    for g, r, sign in zip(got, raw, (-1, -1, 1, 1), strict=True):
        assert -1e-9 <= (g - r) * sign < 0.01 + 1e-9 and abs(g * 100 - round(g * 100)) < 1e-6


def test_courtyard_follows_pads_the_max_body_and_the_pin1_marker():
    # pads set x, the body sets y: KiCad's stock SOIC-8 courtyard ...
    assert fa.body_courtyard(soic8()) == pytest.approx((-3.7, -2.7, 3.7, 2.7))
    # ... has no room for the marker above pin 1 (stock draws it outside): it grows to cover it
    assert not fa.place_pin1_marker(soic8()).inside
    assert fa.courtyard_rect(soic8()) == pytest.approx((-3.7, -2.94, 3.7, 2.7))
    big = ft.sop("S", body=fa.Body(3.9, 4.9, w_max=4.0, h_max=5.0), pins=8, pitch=1.27,
                 span=4.95, pad_w=0.6, pad_l=1.95, courtyard_excess=0.5)
    placed = fa.place_pin1_marker(big)                 # the same spot, now inside, no notch
    assert placed.inside and not placed.notch and list(placed.points) == fa.pin1_marker(soic8())
    assert fa.courtyard_rect(big) == fa.body_courtyard(big)
    assert fa.body_courtyard(big) == pytest.approx((-3.95, -3.0, 3.95, 3.0))


def test_the_marker_walks_past_neighbouring_copper():
    pads = (fa.Pad("1", -2.0, 0.0, 0.5, 0.5), fa.Pad("2", -2.6, 0.0, 0.3, 0.3))
    spec = fa.FootprintSpec("W", pads, fa.Body(6.0, 6.0), pin1_side="L")
    marker = fa.pin1_marker(spec)            # pad 2 sits where the first spots would be
    assert marker[0][0] <= -2.75 - fa.SILK_PAD_CLEARANCE_MM - fa.SILK_WIDTH_MM / 2
    assert fa.static_problems(fa.parse_footprint(fa.render(spec))) == []


def test_a_marker_with_no_room_anywhere_fails_loudly(monkeypatch):
    monkeypatch.setattr(fa, "PIN1_MAX_STEPS", 0)
    with pytest.raises(ValueError, match="no room for a pin-1 marker on side T"):
        fa.render(soic8())


def test_silk_is_cut_where_it_would_cross_pads():
    segs = fa.silk_segments(soic8())
    # the long sides run between the pad rows: cut away entirely; top/bottom survive whole
    assert sorted(segs) == [((-2.06, -2.56), (2.06, -2.56)), ((-2.06, 2.56), (2.06, 2.56))]
    lcc = all_templates()[4]
    assert all(b[0] - a[0] + b[1] - a[1] >= fa.MIN_SILK_SEGMENT_MM
               for a, b in fa.silk_segments(lcc))


def test_geometry_checks_flag_bad_silk_courtyard_and_overlaps(tmp_path):
    bad = tmp_path / "Bad.pretty" / "Bad.kicad_mod"
    bad.parent.mkdir()
    bad.write_text("""(footprint "Bad" (version 20241229) (generator "x") (layer "F.Cu") (attr smd)
  (fp_line (start -1 -0.5) (end 1 -0.5) (stroke (width 0.12) (type solid)) (layer "F.SilkS"))
  (fp_line (start 0.4 0) (end 0.4 2) (stroke (width 0.12) (type solid)) (layer "F.SilkS"))
  (fp_rect (start -1.1 -0.6) (end 1.1 0.6) (stroke (width 0.05) (type solid)) (fill no)
    (layer "F.CrtYd"))
  (pad "1" smd rect (at -0.5 0) (size 1 0.8) (layers "F.Cu" "F.Mask"))
  (pad "2" smd rect (at 0.4 0) (size 1 0.8) (layers "F.Cu" "F.Mask")))""", encoding="utf-8")
    problems = fa.static_problems(fa.read_footprint(bad), bad)
    joined = "\n".join(problems)
    assert "courtyard leaves 0.100 mm" in joined
    assert "no F.Fab" in joined and "silk within 0.2 mm of pad(s) ['1', '2']" in joined
    assert "(closest -0.060 mm)" in joined                  # the line starting inside pad 2
    assert "overlaps: [('1', '2')]" in joined


def test_static_checks_name_every_missing_piece():
    fp = fa.parse_footprint('(footprint "E" (layer "F.Cu") (fp_rect (start -1 -1) (end 1 1) '
                            '(layer "F.CrtYd")) (pad "" smd rect (at 0 0) (size 1 1) '
                            '(layers "F.Paste")))')
    assert fa.static_problems(fp) == ["no (version ...) header", "no (attr smd|through_hole)",
                                      "no numbered copper pads", "no F.Fab body outline"]


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------
def test_qfn_with_exposed_pad_thermal_vias_and_paste():
    spec = qfn16_vias()
    numbered = [p for p in spec.pads if p.number and p.number != "17"]
    assert [p.number for p in numbered] == [str(n) for n in range(1, 17)]
    pos = {p.number: (p.x, p.y) for p in numbered}
    # counter-clockwise from the top of the left side
    assert pos["1"] == pytest.approx((-1.4625, -0.75))
    assert pos["4"] == pytest.approx((-1.4625, 0.75))
    assert pos["5"] == pytest.approx((-0.75, 1.4625)) and pos["9"] == pytest.approx((1.4625, 0.75))
    assert pos["13"] == pytest.approx((0.75, -1.4625))
    assert pos["16"] == pytest.approx((-0.75, -1.4625))
    ep = [p for p in spec.pads if p.number == "17"]
    top = [p for p in ep if p.type == "smd" and p.pad_layers == ("F.Cu", "F.Mask")]
    vias = [p for p in ep if p.type == "thru_hole"]
    assert len(top) == 1 and (top[0].w, top[0].h) == (1.7, 1.7) and top[0].heatsink
    assert sorted((v.x, v.y) for v in vias) == pytest.approx([(-0.6, -0.6), (-0.6, 0.6),
                                                              (0.6, -0.6), (0.6, 0.6)])
    assert all(v.drill == 0.2 and v.w == 0.5 and v.pad_layers == ("*.Cu",) for v in vias)
    assert any(p.pad_layers == ("B.Cu",) for p in ep)
    paste = [p for p in spec.pads if p.pad_layers == ("F.Paste",)]
    assert len(paste) == 4 and all(p.number == "" for p in paste)
    assert sum(p.w * p.h for p in paste) / (1.7 * 1.7) == pytest.approx((0.69 / 0.85) ** 2)
    assert sorted((p.x, p.y, p.w) for p in paste) == pytest.approx(
        [(-0.425, -0.425, 0.69), (-0.425, 0.425, 0.69),
         (0.425, -0.425, 0.69), (0.425, 0.425, 0.69)])


def test_qfn_solid_exposed_pad_keeps_its_paste():
    spec = ft.qfn("Q", body=fa.Body(4, 4), pitch=0.5, pins_x=5, pins_y=5, span_x=3.9, span_y=3.9,
                  pad_w=0.25, pad_l=0.7, ep=ft.ExposedPad(2.6, 2.6))
    (ep,) = [p for p in spec.pads if p.number == "21"]
    assert ep.pad_layers == ("F.Cu", "F.Mask", "F.Paste") and len(spec.pads) == 21


def test_thermal_vias_without_a_bottom_pad_and_an_lga_without_a_grid():
    vias = ft.ThermalVias(2, 2, pitch=1.0, bottom_pad=False)
    pads = ft.exposed_pads(ft.ExposedPad(2, 2, vias=vias), "9")
    assert [p.pad_layers for p in pads] == [("F.Cu", "F.Mask", "F.Paste")] + [("*.Cu",)] * 4
    edge = ft.lga_grid("E", body=fa.Body(4, 4), edge_rows=(ft.EdgeRow("L", -1.5, (0.0,), 1,
                                                                      0.8, 0.5),))
    assert [p.number for p in edge.pads] == ["1"]


def test_dfn_has_two_rows():
    spec = all_templates()[3]
    rows = {p.x for p in spec.pads if p.number not in ("", "9")}
    assert rows == {-0.95, 0.95} and [p.number for p in spec.pads][:8] == list("12345678")


def test_sop_numbering_and_rows():
    pos = {p.number: (p.x, p.y) for p in soic8().pads}
    assert pos["1"] == pytest.approx((-2.475, -1.905))
    assert pos["4"] == pytest.approx((-2.475, 1.905))
    assert pos["5"] == pytest.approx((2.475, 1.905)) and pos["8"] == pytest.approx((2.475, -1.905))
    assert all(p.shape == "roundrect" and (p.w, p.h) == (1.95, 0.6) for p in soic8().pads)
    with pytest.raises(ValueError, match="even"):
        ft.sop("S", body=fa.Body(3, 3), pins=7, pitch=1, span=4, pad_w=0.5, pad_l=1)


def test_lcc_castellated_pads_straddle_the_body_edge():
    spec = all_templates()[4]
    assert len(spec.pads) == 38
    left = [p for p in spec.pads if p.x < -8]
    assert {p.x for p in left} == {-9.25} and min(p.x - p.w / 2 for p in left) < -9.0 < max(
        p.x + p.w / 2 for p in left)
    # pin 14 ends the left side; pin 15 starts the bottom row
    assert spec.pads[13].number == "14" and spec.pads[14].y == pytest.approx(13.0)


def test_pin_header_through_hole_numbering():
    spec = all_templates()[5]
    pos = {p.number: (p.x, p.y, p.shape) for p in spec.pads}
    assert pos["1"] == (0.0, 0.0, "rect") and pos["2"] == (2.54, 0.0, "circle")
    assert pos["6"][:2] == pytest.approx((2.54, 5.08)) and pos["6"][2] == "circle"
    assert pos["5"][:2] == pytest.approx((0.0, 5.08))
    assert spec.attr == "through_hole" and all(p.drill == 1.0 and p.type == "thru_hole"
                                               for p in spec.pads)


def test_lga_grid_numbering_callable_can_depopulate():
    spec = ft.lga_grid("G", body=fa.Body(5, 5), grid=ft.PadGrid(ft.centred_run(3, 1.2),
                       ft.centred_run(3, 1.2), 0.6, 0.6),
                       numbering=lambda c, r: None if (c, r) == (1, 1) else f"{'ABC'[r]}{c + 1}",
                       pin1="A1")
    assert sorted(p.number for p in spec.pads) == ["A1", "A2", "A3", "B1", "B3", "C1", "C2", "C3"]
    with pytest.raises(ValueError, match="duplicate"):
        ft.lga_grid("G", body=fa.Body(5, 5), grid=ft.PadGrid((0.0, 1.0), (0.0,), 0.6, 0.6),
                    numbering=lambda c, r: "1")


def test_model_and_datasheet_are_written(tmp_path):
    spec = ft.lg290p(model=fa.Model("${KIPRJMOD}/libs/3d/Quectel_LG290P.step"))
    text = fa.render(spec)
    assert '(model "${KIPRJMOD}/libs/3d/Quectel_LG290P.step"' in text
    assert '(property "Datasheet" "https://' in text and "(embedded_fonts no)" in text
    assert fa.render(spec) == text                                            # deterministic


# ---------------------------------------------------------------------------
# Spec / template validation and the reader
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(("kw", "msg"), [
    ({"type": "smt"}, "type 'smt'"), ({"shape": "star"}, "shape 'star'"),
    ({"w": 0}, "must be positive"), ({"shape": "circle", "w": 1, "h": 2}, "w == h"),
    ({"drill": 0.3}, "smd pad has no drill"), ({"type": "thru_hole"}, "positive drill"),
    ({"type": "thru_hole", "drill": 1.0}, "annular ring"),
    ({"shape": "roundrect", "roundrect_ratio": 0.7}, "roundrect_ratio"), ({"layers": ()}, "empty"),
    ({"number": 1}, "number must be a str"),
])
def test_bad_pads_fail_loudly(kw, msg):
    with pytest.raises(ValueError, match=msg):
        fa.Pad(**{"number": "1", "x": 0, "y": 0, "w": 1, "h": 1, **kw})


def test_bad_specs_fail_loudly():
    pad = fa.Pad("1", 0, 0, 1, 1)
    for kw, msg in (({"name": "a/b"}, "file name"), ({"pads": ()}, "no pads"),
                    ({"attr": "virtual"}, "attr 'virtual'"), ({"pin1": "A1"}, "pin1 'A1'"),
                    ({"attr_flags": ("bogus",)}, "attr_flags"), ({"pin1_side": "X"}, "pin1_side"),
                    ({"courtyard_excess": -0.1}, "courtyard_excess")):
        with pytest.raises(ValueError, match=msg):
            fa.FootprintSpec(**{"name": "N", "pads": (pad,), "body": fa.Body(2, 2), **kw})
    for kw, msg in (({"w_max": 1.5}, "below nominal"), ({"w": 0}, "must be positive"),
                    ({"height": 0}, "height 0")):
        with pytest.raises(ValueError, match=msg):
            fa.Body(**{"w": 2, "h": 2, **kw})
    with pytest.raises(ValueError, match="pretty"):
        fa.write_footprint(fa.FootprintSpec("N", (pad,), fa.Body(2, 2)), "somewhere/else")


@pytest.mark.parametrize(("build", "msg"), [
    (lambda: ft.EdgeRow("X", 0.0, (0.0,), 1, 1.0, 0.5).pads(), "edge row side 'X'"),
    (lambda: ft.exposed_pads(ft.ExposedPad(1, 1, paste_grid=(0, 1)), "9"), "paste grid"),
    (lambda: ft.exposed_pads(ft.ExposedPad(1, 1, vias=ft.ThermalVias(3, 3, 1.0)), "9"), "fit"),
    (lambda: ft.lga_grid("G", body=fa.Body(5, 5), grid=ft.PadGrid((0.0,), (0.0,), 1, 1)),
     "numbering"),
    (lambda: ft.pin_header("H", rows=0), "rows and cols"),
])
def test_template_inputs_fail_loudly(build, msg):
    with pytest.raises(ValueError, match=msg):
        build()


@pytest.mark.parametrize("mid_y", [1.0, -1.0])
def test_reader_flattens_arcs_and_circles(mid_y):
    fp = fa.parse_footprint(f"""(footprint "A" (version 20241229) (layer "F.Cu") (attr smd)
      (fp_arc (start 1 0) (mid 0 {mid_y}) (end -1 0) (stroke (width 0.1) (type solid))
        (layer "F.Fab"))
      (fp_arc (start 0 0) (mid 1 1) (end 2 2) (stroke (width 0.1) (type solid)) (layer "F.Fab"))
      (fp_circle (center 0 0) (end 2 0) (stroke (width 0.05) (type solid)) (fill no)
        (layer "F.CrtYd"))
      (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu")))""")
    *arc, line = fp.strokes["F.Fab"]
    pts = [pt for a, b, _ in arc for pt in (a, b)]
    assert pts[0] == pytest.approx((1, 0)) and pts[-1] == pytest.approx((-1, 0))
    assert all(math.hypot(x, y) == pytest.approx(1) and y * mid_y >= -1e-9 for x, y in pts)
    assert line[:2] == ((0.0, 0.0), (2.0, 2.0))                      # a degenerate arc: a chord
    ring = [pt for a, b, _ in fp.strokes["F.CrtYd"] for pt in (a, b)]
    assert all(math.hypot(x, y) == pytest.approx(2) for x, y in ring)


def test_overlap_check_respects_layers_and_unnumbered_copper():
    fp = fa.parse_footprint("""(footprint "O" (version 20241229) (layer "F.Cu") (attr smd)
      (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu"))
      (pad "2" smd rect (at 0.2 0) (size 1 1) (layers "B.Cu"))
      (pad "" smd rect (at 0 0) (size 2 2) (layers "F.Cu"))
      (pad "3" thru_hole circle (at 0.4 0) (size 1 1) (drill 0.5) (layers "*.Cu")))""")
    overlaps = [p for p in fa.geometry_problems(fp) if "overlaps" in p]
    assert overlaps == ["copper of differently numbered pads overlaps: [('1', '3'), ('2', '3')]"]


@pytest.mark.parametrize(("text", "msg"), [
    ("(footprint", "balanced"), ("(footprint \"x\"))", "unbalanced"),
    ("(kicad_pcb (version 1))", "not a footprint"), ('(footprint "x" "open)', "unreadable text"),
    ('(footprint "x" (pad "1" smd))', "too few fields"),
    ('(footprint "x" (version abc))', "unreadable (version"),
])
def test_parser_reports_garbage(text, msg):
    with pytest.raises(fa.FootprintParseError, match=re.escape(msg)):
        fa.parse_footprint(text)


def test_parser_atoms_and_unreadable_files(tmp_path):
    assert fa.parse_sexpr('(a "q \\"x\\"" (b 1))') == ["a", 'q "x"', ["b", "1"]]
    with pytest.raises(fa.FootprintParseError, match="cannot read"):
        fa.read_footprint(tmp_path / "missing.kicad_mod")


# ---------------------------------------------------------------------------
# validate(): static problems + the kicad-cli parse check (fake runner)
# ---------------------------------------------------------------------------
Runner = Callable[..., subprocess.CompletedProcess]


def fake_upgrade(rc: int = 0, drop_last_pad: bool = False, console: str = "",
                 write: bool = True) -> Runner:
    def run(args, **kw):
        assert kw.get("stdin") is subprocess.DEVNULL and args[1:4] == ["fp", "upgrade", "--force"]
        assert (kw.get("encoding"), kw.get("errors")) == ("utf-8", "replace") and kw["timeout"]
        out, src = Path(args[5]), Path(args[6])
        if rc == 0 and write:
            shutil.copytree(src, out)
            if drop_last_pad:
                f = next(out.glob("*.kicad_mod"))
                text = f.read_text(encoding="utf-8")
                cut = text.rindex("\t(pad ")
                f.write_text(text[:cut] + text[text.index("\t(embedded_fonts", cut):],
                             encoding="utf-8")
        return subprocess.CompletedProcess(args, rc, console, "")
    return run


def raising(exc: Exception) -> Runner:
    def run(args, **kw):
        raise exc
    return run


def test_validate_passes_a_good_file_with_a_working_kicad_cli(lg_file, monkeypatch):
    monkeypatch.setattr(fa.subprocess, "run", fake_upgrade())
    assert fa.validate(lg_file, expected_pads=79, kicad_cli="kicad-cli") == []
    # D0: only "Error:" lines count, not a path that happens to contain "error"
    monkeypatch.setattr(fa.subprocess, "run", fake_upgrade(console="Saving C:/error_logs/x\n"))
    assert fa.validate(lg_file, kicad_cli="kicad-cli") == []


@pytest.mark.parametrize(("runner", "msg"), [
    (fake_upgrade(rc=2, console="Unable to load library"), "could not load Quectel_LG290P"),
    (fake_upgrade(drop_last_pad=True), "re-serialised the pads differently"),
    (fake_upgrade(console="Error: something odd"), "reported: Error: something odd"),
    (fake_upgrade(console="12:00:01: Error: stamped"), "reported: 12:00:01: Error: stamped"),
    (fake_upgrade(write=False), "fp upgrade wrote no Quectel_LG290P.kicad_mod"),
    (raising(OSError("denied")), "fp upgrade could not run: denied"),
])
def test_validate_reports_kicad_cli_failures(lg_file, monkeypatch, runner, msg):
    monkeypatch.setattr(fa.subprocess, "run", runner)
    problems = fa.validate(lg_file, kicad_cli="kicad-cli")
    assert len(problems) == 1 and msg in problems[0], problems


def test_validate_without_kicad_is_a_problem_not_a_pass(lg_file, monkeypatch):
    monkeypatch.setattr(fa, "oldest_kicad_cli", lambda: (None, ""))
    assert fa.validate(lg_file) == ["no kicad-cli found (install KiCad 9 or set KICAD_PATH): "
                                    "the KiCad parse check did not run"]


def test_validate_static_failures(tmp_path):
    assert fa.validate(tmp_path / "none.kicad_mod")[0].endswith("no such file")
    newer = tmp_path / "N.pretty" / "Other.kicad_mod"
    newer.parent.mkdir()
    newer.write_text(fa.render(ft.lg290p()).replace("20241229", "20260901"), encoding="utf-8")
    problems = fa.static_problems(fa.read_footprint(newer), newer, expected_pads=80)
    assert any("newer than 20241229" in p for p in problems)
    assert any("differs from the file name 'Other'" in p for p in problems)
    assert any("79 distinct pad numbers, expected 80" in p for p in problems)
    (tmp_path / "N.pretty" / "Junk.kicad_mod").write_text("(footprint", encoding="utf-8")
    assert "Junk.kicad_mod:" in fa.validate(tmp_path / "N.pretty" / "Junk.kicad_mod")[0]


def test_the_oldest_install_is_the_d0_floor(monkeypatch):
    def inst(v: tuple, python: str | None) -> SimpleNamespace:
        return SimpleNamespace(version=".".join(map(str, v)), version_tuple=v,
                               kicad_cli=Path(f"cli{v[0]}"), python=python and Path(python))
    found = [inst((10, 99, 0), None), inst((9, 0, 4), "py9"), inst((10, 0, 5), "py10")]
    monkeypatch.setattr(env, "discover", lambda: found)
    monkeypatch.setattr(sys, "path", [p for p in sys.path if p != str(fa.SERVER_DIR)])
    assert fa.oldest_kicad_cli() == (str(Path("cli9")), "kicad-cli 9.0.4")
    assert fa.oldest_kicad_python() == str(Path("py9"))
    monkeypatch.setattr(env, "discover", lambda: [])
    assert fa.oldest_kicad_cli() == (None, "") and fa.oldest_kicad_python() is None


# ---------------------------------------------------------------------------
# pcbnew_load: the KiCad-python child (a stub pcbnew stands in for KiCad's)
# ---------------------------------------------------------------------------
STUB_PCBNEW = '''\
import os
import sys
QUIET_FIRST = "kicad_quiet" in sys.modules    # imported (from argv[1]) before pcbnew?
F_CrtYd = 49


class _Item:
    def __init__(self, n):
        self.n = n

    def GetNumber(self):
        return self.n

    def OutlineCount(self):
        return self.n


class _Footprint:
    built = False

    def Pads(self):
        return [_Item("1"), _Item("2")]

    def BuildCourtyardCaches(self):       # 9.0.4: GetCourtyard is empty until this ran
        self.built = True

    def GetCourtyard(self, layer):
        return _Item(1 if self.built and layer == F_CrtYd else 0)


def Version():
    return "9.0.4-stub"


def FootprintLoad(lib, name):
    if not QUIET_FIRST:
        raise RuntimeError("pcbnew imported before kicad_quiet")
    return _Footprint() if os.path.isfile(os.path.join(lib, name + ".kicad_mod")) else None
'''


def test_pcbnew_load_child_imports_kicad_quiet_first(lg_file, tmp_path, monkeypatch):
    stub = tmp_path / "stub"
    stub.mkdir()
    (stub / "pcbnew.py").write_text(STUB_PCBNEW, encoding="utf-8")
    monkeypatch.setenv("PYTHONPATH", str(stub))
    calls, real_run = [], subprocess.run

    def run(args, **kw):
        calls.append((list(args), kw))
        return real_run(args, **kw)
    monkeypatch.setattr(fa.subprocess, "run", run)
    res = fa.pcbnew_load(lg_file, python=sys.executable)
    assert res == {"ok": True, "kicad_version": "9.0.4-stub", "loaded": True, "pads": 2,
                   "numbers": ["1", "2"], "courtyard_outlines": 1}
    ((argv, kw),) = calls
    assert argv[1] == "-c" and (Path(argv[3]) / "kicad_quiet.py").is_file()
    assert kw["stdin"] is subprocess.DEVNULL and (kw["encoding"], kw["errors"]) == ("utf-8",
                                                                                 "replace")
    lines = fa.LOAD_CHILD.splitlines()
    at = [next(i for i, ln in enumerate(lines) if ln.startswith(s))
          for s in ("sys.path.insert(0, sys.argv[1])", "import kicad_quiet", "import pcbnew")]
    assert at == sorted(at) and "import wx" not in fa.LOAD_CHILD


@pytest.mark.parametrize(("runner", "error"), [
    (raising(OSError("gone")), "KiCad python could not run: gone"),
    (lambda args, **kw: subprocess.CompletedProcess(args, 1, "", "Traceback: boom"),
     "rc=1: Traceback: boom"),
    (lambda args, **kw: subprocess.CompletedProcess(
        args, 0, json.dumps({"loaded": False, "kicad_version": "9"}), ""),
     "pcbnew.FootprintLoad returned None for Quectel_LG290P.kicad_mod"),
    (lambda args, **kw: subprocess.CompletedProcess(args, 0, "{not json", ""), "rc=0: {not json"),
])
def test_pcbnew_load_reports_a_child_that_fails(lg_file, monkeypatch, runner, error):
    monkeypatch.setattr(fa.subprocess, "run", runner)
    res = fa.pcbnew_load(lg_file, python="kicad-python")
    assert res["ok"] is False and res["error"] == error, res


def test_pcbnew_load_without_a_file_or_kicad(tmp_path, monkeypatch):
    res = fa.pcbnew_load(tmp_path / "Nope.kicad_mod", python="python")
    assert res == {"ok": False, "error": f"{tmp_path / 'Nope.kicad_mod'}: no such file"}
    monkeypatch.setattr(fa, "oldest_kicad_python", lambda: None)
    (tmp_path / "Y.kicad_mod").write_text("(footprint \"Y\")", encoding="utf-8")
    assert fa.pcbnew_load(tmp_path / "Y.kicad_mod")["error"].startswith("no KiCad bundled Python")


# ---------------------------------------------------------------------------
# KiCad tier: stock libraries, kicad-cli parse, pcbnew FootprintLoad
# ---------------------------------------------------------------------------
STOCK_FP = KICAD.symbols.parent / "footprints" if KICAD else None


@pytest.mark.skipif(STOCK_FP is None, reason="KiCad stock footprint libraries not installed")
@pytest.mark.parametrize(("spec_fn", "stock"), [
    (soic8, "Package_SO.pretty/SOIC-8_3.9x4.9mm_P1.27mm.kicad_mod"),
    (qfn16_vias,
     "Package_DFN_QFN.pretty/QFN-16-1EP_3x3mm_P0.5mm_EP1.7x1.7mm_ThermalVias.kicad_mod"),
])
def test_templates_reproduce_kicad_stock_pads(spec_fn, stock, tmp_path):
    stock_path = STOCK_FP / stock
    if not stock_path.is_file():
        pytest.skip(f"{stock_path} not in this KiCad install")
    path = fa.write_footprint(spec_fn(), tmp_path / "t.pretty")
    diff = fa.compare_footprints(path, stock_path)           # copper and paste windows
    assert diff["identical"], diff["summary"]
    assert diff["apertures"]["matched"] == (4 if spec_fn is qfn16_vias else 0)


def _stable_install() -> env.KiCadInstall | None:
    """The oldest install, if it is a stable release (<= 10): the D0 dialect floor."""
    found = env.discover()
    oldest = min(found, key=lambda i: i.version_tuple) if found else None
    return oldest if oldest and oldest.version_tuple[0] <= 10 else None


@pytest.fixture(scope="module")
def stable() -> env.KiCadInstall:
    inst = _stable_install()
    if inst is None:
        pytest.skip("no KiCad 9/10 install (the D0 dialect floor) to parse / load footprints")
    return inst


@pytest.mark.parametrize("spec", all_templates(), ids=lambda s: s.name)
def test_live_kicad_cli_parses_every_template(stable, spec, tmp_path):
    path = fa.write_footprint(spec, tmp_path / "SPS_parts.pretty")
    assert fa.validate(path, kicad_cli=str(stable.kicad_cli)) == [], stable.version


def test_live_kicad_cli_rejects_a_malformed_footprint(stable, tmp_path):
    bad = tmp_path / "B.pretty" / "Bad.kicad_mod"
    bad.parent.mkdir()
    bad.write_text('(footprint "Bad"\n (version 20241229)\n (pad "1" smd bogus (at 0 0) '
                   '(size 1 1) (layers "F.Cu"))\n)\n', encoding="utf-8")
    problems = fa.kicad_parse_problems(bad, str(stable.kicad_cli))
    assert len(problems) == 1 and "could not load Bad.kicad_mod" in problems[0], problems


@pytest.fixture(scope="module")
def stable_python(stable) -> str:
    if stable.python is None:
        pytest.skip(f"KiCad {stable.version} has no bundled Python with pcbnew")
    return str(stable.python)


def test_live_pcbnew_loads_the_lg290p_with_79_pads(stable, stable_python, lg_file):
    res = fa.pcbnew_load(lg_file, python=stable_python)
    assert res["ok"] and res["loaded"], res
    assert res["pads"] == 79 and sorted(res["numbers"], key=int) == [str(n) for n in range(1, 80)]
    assert res["courtyard_outlines"] == 1
    assert res["kicad_version"].startswith(f"{stable.version_tuple[0]}."), res


@pytest.mark.parametrize("spec", all_templates()[1:], ids=lambda s: s.name)
def test_live_pcbnew_loads_every_template(stable_python, spec, tmp_path):
    path = fa.write_footprint(spec, tmp_path / "SPS_parts.pretty")
    res = fa.pcbnew_load(path, python=stable_python)
    assert res["ok"] and res["pads"] == len(spec.pads) and res["courtyard_outlines"] == 1, res


def test_live_pcbnew_load_reports_a_malformed_footprint(stable_python, tmp_path):
    bad = tmp_path / "Bad.kicad_mod"                 # not even in a .pretty: copied into one
    bad.write_text('(footprint "Bad"\n (version 20241229)\n (pad "1" smd bogus (at 0 0) '
                   '(size 1 1) (layers "F.Cu"))\n)\n', encoding="utf-8")
    res = fa.pcbnew_load(bad, python=stable_python)
    assert res["ok"] is False and res.get("error"), res
