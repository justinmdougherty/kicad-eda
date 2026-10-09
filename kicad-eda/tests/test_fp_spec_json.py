"""fp_spec_json: JSON-shaped params -> fp_author_templates FootprintSpec (F2-12, D1).

Unit tier (venv only, no KiCad):
  * every template (lga_grid, qfn, dfn, lcc_castellated, sop, pin_header,
    lg290p) built from JSON equals the same template called directly, and the
    LG290P re-expressed as a generic lga_grid JSON equals ft.lg290p() pad for pad;
  * the common FootprintSpec keywords (descr, tags, datasheet, model, pin1,
    pin1_side, attr_flags, courtyard_excess) are forwarded; pin1 null = no marker;
  * coordinate runs (lists, start/pitch/count, centred, mixed) and the grid
    numbering rules (linear, grid_ref, table, skip);
  * clear errors: unknown keys (with path, suggestion and allowed keys), missing
    and mistyped keys (bool is not a number), every problem at once, unknown
    templates, template ValueErrors wrapped, bad JSON text;
  * the input is never mutated (deep copy compared; read-only mappings work);
  * stdlib-only imports (KiCad 9's Python 3.11 can import it).
Live tier: KiCad 9's bundled Python imports the module (skipped without it).
"""
from __future__ import annotations

import ast
import copy
import json
import subprocess
import time
from pathlib import Path
from types import MappingProxyType

import pytest

import fp_author as fa
import fp_author_templates as ft
import fp_spec_json as fj
import kicad_env as env

TOOLS = Path(fj.__file__).resolve().parent


def soic8_json() -> dict:
    return {"name": "SOIC-8_3.9x4.9mm_P1.27mm", "body": {"w": 3.9, "h": 4.9}, "pins": 8,
            "pitch": 1.27, "span": 4.95, "pad_w": 0.6, "pad_l": 1.95}


def qfn16_json() -> dict:
    return {"name": "QFN-16-1EP_3x3mm_P0.5mm_EP1.7x1.7mm_ThermalVias", "body": {"w": 3, "h": 3},
            "pitch": 0.5, "pins_x": 4, "pins_y": 4, "span_x": 2.925, "span_y": 2.925,
            "pad_w": 0.25, "pad_l": 0.825,
            "ep": {"w": 1.7, "h": 1.7, "paste_grid": [2, 2], "paste_coverage": (0.69 / 0.85) ** 2,
                   "vias": {"nx": 2, "ny": 2, "pitch": 1.2, "drill": 0.2, "size": 0.5}}}


def lg290p_as_lga_grid_json() -> dict:
    """Quectel LG290P(03) written out as a generic lga_grid (Fig. 18 numbers)."""
    left = [{"start": -7.0, "pitch": 1.1, "count": 7}, {"start": 2.6, "pitch": 1.1, "count": 5}]
    right = [{"start": 7.0, "pitch": -1.1, "count": 5}, {"start": -0.4, "pitch": -1.1, "count": 7}]
    return {
        "name": ft.LG290P_NAME,
        "body": {"w": 12.2, "h": 16.0, "w_max": 12.5, "h_max": 16.3, "height": 2.8},
        "edge_rows": [
            {"side": "L", "at": -5.05, "positions": left, "first": 1, "length": 1.5, "width": 0.8},
            {"side": "R", "at": 5.05, "positions": right, "first": 13, "length": 1.5,
             "width": 0.8}],
        "grid": {"xs": {"start": -2.8, "pitch": 1.4, "count": 5},
                 "ys": {"start": -7.0, "pitch": 1.4, "count": 11}, "w": 1.0, "h": 1.0},
        "numbering": {"rule": "linear", "order": "col_major", "start": 25},
        "shapes": {"25": "circle"},
        "pin1_side": "T",
    }


# ---------------------------------------------------------------------------
# Every template: JSON == the direct call
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(("template", "params", "direct"), [
    ("sop", soic8_json(), lambda: ft.sop("SOIC-8_3.9x4.9mm_P1.27mm", body=ft.Body(3.9, 4.9),
                                         pins=8, pitch=1.27, span=4.95, pad_w=0.6, pad_l=1.95)),
    ("qfn", qfn16_json(), lambda: ft.qfn(
        "QFN-16-1EP_3x3mm_P0.5mm_EP1.7x1.7mm_ThermalVias", body=ft.Body(3.0, 3.0), pitch=0.5,
        pins_x=4, pins_y=4, span_x=2.925, span_y=2.925, pad_w=0.25, pad_l=0.825,
        ep=ft.ExposedPad(1.7, 1.7, paste_grid=(2, 2), paste_coverage=(0.69 / 0.85) ** 2,
                         vias=ft.ThermalVias(2, 2, pitch=1.2, drill=0.2, size=0.5)))),
    ("dfn", {"name": "DFN-8_2x2mm_P0.5mm", "body": {"w": 2, "h": 2}, "pitch": 0.5,
             "pins_per_side": 4, "span": 1.9, "pad_w": 0.25, "pad_l": 0.6,
             "ep": {"w": 0.9, "h": 1.6}},
     lambda: ft.dfn("DFN-8_2x2mm_P0.5mm", body=ft.Body(2.0, 2.0), pitch=0.5, pins_per_side=4,
                    span=1.9, pad_w=0.25, pad_l=0.6, ep=ft.ExposedPad(0.9, 1.6))),
    ("lcc_castellated", {"name": "ESP32-like_LCC", "body": {"w": 18, "h": 25.5}, "pitch": 1.27,
                         "pins": [14, 10, 14, 0], "pad_w": 0.9, "pad_l": 1.5,
                         "edge_offset": 0.25},
     lambda: ft.lcc_castellated("ESP32-like_LCC", body=ft.Body(18.0, 25.5), pitch=1.27,
                                pins=(14, 10, 14, 0), pad_w=0.9, pad_l=1.5, edge_offset=0.25)),
    ("pin_header", {"name": "PinHeader_2x03_P2.54mm", "rows": 3, "cols": 2},
     lambda: ft.pin_header("PinHeader_2x03_P2.54mm", rows=3, cols=2)),
    ("pin_header", {"name": "H1", "rows": 4, "pitch": 2.0, "drill": 0.8, "pad": 1.35},
     lambda: ft.pin_header("H1", rows=4, pitch=2.0, drill=0.8, pad=1.35)),
    ("lg290p", {}, ft.lg290p),
    ("lga_grid", lg290p_as_lga_grid_json(), lambda: ft.lg290p(
        descr="", tags="", datasheet="")),
], ids=lambda v: v if isinstance(v, str) else "")
def test_every_template_from_json_equals_the_direct_call(template, params, direct):
    assert fj.spec_from_json(template, params) == direct()


def test_lg290p_as_a_generic_lga_grid_has_the_datasheet_pads():
    spec = fj.spec_from_json("lga_grid", lg290p_as_lga_grid_json())
    assert spec.pads == ft.lg290p().pads and spec.body == ft.lg290p().body
    assert len(spec.pads) == 79 and spec.pin1_side == "T"


def test_templates_list_and_key_descriptions():
    assert fj.TEMPLATES == ("lga_grid", "qfn", "dfn", "lcc_castellated", "sop", "pin_header",
                            "lg290p")
    keys = fj.template_keys("sop")
    assert keys["required"] == ["body", "name", "pad_l", "pad_w", "pins", "pitch", "span"]
    assert {"ep", "shape", "descr", "model", "pin1", "courtyard_excess"} <= set(keys["optional"])
    assert fj.template_keys("lg290p")["required"] == []
    assert "name" not in fj.template_keys("lg290p")["optional"]
    with pytest.raises(fj.FpSpecJsonError, match="unknown template 'bga'"):
        fj.template_keys("bga")


# ---------------------------------------------------------------------------
# Common FootprintSpec keywords
# ---------------------------------------------------------------------------
def test_common_spec_keywords_are_forwarded():
    params = {**soic8_json(), "descr": "d", "tags": "t", "datasheet": "https://x/y.pdf",
              "model": {"path": "${KIPRJMOD}/3d/s.step", "offset": [0, 0, 0.1],
                        "rotate": [0, 0, 90]},
              "pin1": 4, "pin1_side": "B", "attr_flags": ["exclude_from_bom"],
              "courtyard_excess": 0.5}
    spec = fj.spec_from_json("sop", params)
    assert (spec.descr, spec.tags, spec.datasheet) == ("d", "t", "https://x/y.pdf")
    assert spec.model == ft.Model("${KIPRJMOD}/3d/s.step", offset=(0.0, 0.0, 0.1),
                                  rotate=(0.0, 0.0, 90.0))
    assert spec.pin1 == "4" and spec.pin1_side == "B"                 # an int pad name is a str
    assert spec.attr_flags == ("exclude_from_bom",) and spec.courtyard_excess == 0.5


def test_pin1_null_means_no_marker_and_absent_keeps_the_default():
    assert fj.spec_from_json("sop", soic8_json()).pin1 == "1"
    spec = fj.spec_from_json("sop", {**soic8_json(), "pin1": None})
    assert spec.pin1 is None and fa.pin1_marker(spec) is None
    assert fj.spec_from_json("lg290p", {"pin1": None}).pin1 is None


def test_a_json_built_spec_renders_and_passes_the_static_checks(tmp_path):
    for template, params in (("qfn", qfn16_json()), ("lga_grid", lg290p_as_lga_grid_json())):
        path = fa.write_footprint(fj.spec_from_json(template, params), tmp_path / "x.pretty")
        assert fa.static_problems(fa.read_footprint(path), path) == [], template


def test_json_text_is_accepted_and_bad_text_is_a_clear_error():
    assert fj.spec_from_json("sop", json.dumps(soic8_json())) == fj.spec_from_json(
        "sop", soic8_json())
    with pytest.raises(fj.FpSpecJsonError, match=r"sop: params is not valid JSON"):
        fj.spec_from_json("sop", "{not json")
    with pytest.raises(fj.FpSpecJsonError, match=r"sop: params: expected an object, got array"):
        fj.spec_from_json("sop", [1, 2])


# ---------------------------------------------------------------------------
# Runs and grid numbering
# ---------------------------------------------------------------------------
def _grid_spec(numbering: dict, xs: object = None, ys: object = None, **extra) -> ft.FootprintSpec:
    params = {"name": "G", "body": {"w": 6, "h": 6},
              "grid": {"xs": xs if xs is not None else {"count": 3, "pitch": 1.2},
                       "ys": ys if ys is not None else {"count": 2, "pitch": 1.2},
                       "w": 0.6, "h": 0.6},
              "numbering": numbering, **extra}
    return fj.spec_from_json("lga_grid", params)


def _names(spec: ft.FootprintSpec) -> dict[tuple[float, float], str]:
    return {(p.x, p.y): p.number for p in spec.pads}


def test_runs_lists_start_centred_and_mixed():
    spec = _grid_spec({"rule": "linear", "order": "row_major"},
                      xs=[-1.0, {"start": 0.5, "pitch": 0.5, "count": 2}],
                      ys={"count": 2, "pitch": 2.0, "centre": 1.0})
    assert sorted({p.x for p in spec.pads}) == [-1.0, 0.5, 1.0]
    assert sorted({p.y for p in spec.pads}) == [0.0, 2.0]
    assert _names(spec)[(-1.0, 0.0)] == "1" and _names(spec)[(1.0, 2.0)] == "6"


def test_linear_numbering_orders_and_skip_is_positional():
    col = _names(_grid_spec({"rule": "linear", "order": "col_major", "start": 10}, pin1=10))
    assert col[(-1.2, -0.6)] == "10" and col[(-1.2, 0.6)] == "11" and col[(0.0, -0.6)] == "12"
    row = _names(_grid_spec({"rule": "linear", "order": "row_major", "skip": [[1, 0]]}))
    assert row[(-1.2, -0.6)] == "1" and (0.0, -0.6) not in row and row[(1.2, -0.6)] == "3"
    assert row[(-1.2, 0.6)] == "4" and len(row) == 5


def test_grid_ref_numbering_skips_the_jedec_letters():
    spec = _grid_spec({"rule": "grid_ref", "skip": [[1, 1]]}, pin1="A1",
                      ys={"count": 2, "pitch": 1.2})
    assert sorted(p.number for p in spec.pads) == ["A1", "A2", "A3", "B1", "B3"]
    assert fj.grid_ref(0, 0) == "A1" and fj.grid_ref(2, 7) == "H3" and fj.grid_ref(0, 8) == "J1"
    assert fj.grid_ref(0, 19) == "Y1" and fj.grid_ref(0, 20) == "AA1"
    assert fj.grid_ref(4, 21) == "AB5" and fj.grid_ref(0, 419) == "YY1"
    with pytest.raises(ValueError, match="outside"):
        fj.grid_ref(0, 420)


def test_table_numbering_with_holes():
    spec = _grid_spec({"rule": "table", "names": [["1", None, 3], ["4", "5", "6"]]})
    names = _names(spec)
    assert names[(-1.2, -0.6)] == "1" and (0.0, -0.6) not in names and names[(1.2, -0.6)] == "3"
    assert len(spec.pads) == 5


@pytest.mark.parametrize(("numbering", "msg"), [
    ({"rule": "table", "names": [["1", "2"], ["3", "4"]]},
     "params.numbering.names: 2 x 2 table does not match the 3 x 2 grid (columns x rows)"),
    ({"rule": "linear", "order": "diagonal"},
     "params.numbering.order: 'diagonal' is not one of col_major, row_major"),
    ({"rule": "spiral"}, "params.numbering.rule: 'spiral' is not one of grid_ref, linear, table"),
    ({"rule": ["linear"]}, "params.numbering.rule: ['linear'] is not one of grid_ref"),
    ({"rule": "linear"}, "params.numbering: missing required key(s) order"),
    ({"rule": "grid_ref", "skip": [[3, 0]]}, "params.numbering.skip[0]: [3, 0] is outside"),
    ({"rule": "linear", "order": "row_major", "skip": [[0]]},
     "params.numbering.skip[0]: expected [col, row]"),
    ({"order": "row_major"}, "params.numbering: missing required key(s) rule"),
])
def test_bad_numbering_is_named(numbering, msg):
    with pytest.raises(fj.FpSpecJsonError) as err:
        _grid_spec(numbering)
    assert msg in str(err.value), str(err.value)


def test_numbering_without_a_grid_and_a_grid_without_numbering():
    with pytest.raises(fj.FpSpecJsonError, match="params.numbering: needs a grid"):
        fj.spec_from_json("lga_grid", {"name": "G", "body": {"w": 2, "h": 2},
                                       "numbering": {"rule": "grid_ref"}})
    with pytest.raises(fj.FpSpecJsonError, match="lga_grid: G: a pad grid needs a numbering"):
        fj.spec_from_json("lga_grid", {"name": "G", "body": {"w": 2, "h": 2},
                                       "grid": {"xs": [0], "ys": [0], "w": 1, "h": 1}})


@pytest.mark.parametrize(("xs", "msg"), [
    ([], "params.grid.xs: expected a non-empty array"),
    ({"count": 0, "pitch": 1}, "params.grid.xs.count: must be >= 1"),
    ({"count": 2, "pitch": 1, "start": 0, "centre": 0},
     "params.grid.xs: give 'start' or 'centre', not both"),
    ([0, "1"], "params.grid.xs[1]: expected a number, got string '1'"),
    ({"count": 2.5, "pitch": 1}, "params.grid.xs.count: expected an integer, got number 2.5"),
])
def test_bad_runs_are_named(xs, msg):
    with pytest.raises(fj.FpSpecJsonError) as err:
        _grid_spec({"rule": "grid_ref"}, xs=xs)
    assert msg in str(err.value), str(err.value)


# ---------------------------------------------------------------------------
# Clear errors
# ---------------------------------------------------------------------------
def test_unknown_keys_name_the_path_a_suggestion_and_the_allowed_keys():
    params = {**soic8_json(), "pich": 1.27}
    with pytest.raises(fj.FpSpecJsonError) as err:
        fj.spec_from_json("sop", params)
    msg = str(err.value)
    assert "sop: params: unknown key 'pich' (did you mean 'pitch'?)" in msg
    assert "allowed: attr_flags, body, courtyard_excess, datasheet, descr, ep" in msg


@pytest.mark.parametrize(("template", "params", "msg"), [
    ("sop", {**soic8_json(), "body": {"w": 3.9, "h": 4.9, "hieght": 1}},
     "params.body: unknown key 'hieght' (did you mean 'height'?)"),
    ("qfn", {**qfn16_json(), "ep": {**qfn16_json()["ep"], "vias": {"nx": 2, "ny": 2,
                                                                     "pitch": 1, "dril": 0.3}}},
     "params.ep.vias: unknown key 'dril' (did you mean 'drill'?)"),
    ("lga_grid", {**lg290p_as_lga_grid_json(),
                  "edge_rows": [{"side": "L", "at": 0, "positions": [0], "first": 1,
                                 "length": 1, "width": 0.5, "lenght": 2}]},
     "params.edge_rows[0]: unknown key 'lenght' (did you mean 'length'?)"),
    ("sop", {**soic8_json(), "model": {"path": "x.step", "rotation": [0, 0, 0]}},
     "params.model: unknown key 'rotation' (did you mean 'rotate'?)"),
    ("lg290p", {"name": "Other"}, "params: unknown key 'name'"),
])
def test_unknown_nested_keys(template, params, msg):
    with pytest.raises(fj.FpSpecJsonError) as err:
        fj.spec_from_json(template, params)
    assert msg in str(err.value), str(err.value)


def test_every_problem_is_reported_at_once():
    params = {"name": 7, "body": {"w": True}, "pins": "8", "pitch": 1.27, "span": -1,
              "pad_w": 0.6, "extra": 1}
    with pytest.raises(fj.FpSpecJsonError) as err:
        fj.spec_from_json("sop", params)
    problems = err.value.problems
    assert isinstance(err.value, ValueError)
    assert "sop: params: unknown key 'extra'" in problems[0]
    joined = "\n".join(problems)
    for msg in ("params: missing required key(s) pad_l",
                "params.name: expected a string, got number 7",
                "params.body.w: expected a number, got boolean true",
                "params.body: missing required key(s) h",
                "params.pins: expected an integer, got string '8'",
                "params.span: must be > 0, got -1"):
        assert msg in joined, joined
    assert all(p.startswith("sop: ") for p in problems)


@pytest.mark.parametrize(("template", "params", "msg"), [
    ("sop", {**soic8_json(), "pins": 7}, "sop: SOIC-8_3.9x4.9mm_P1.27mm: an SOP needs an even"),
    ("sop", {**soic8_json(), "pin1_side": "X"}, "sop: footprint 'SOIC-8_3.9x4.9mm_P1.27mm': "
                                                "pin1_side 'X'"),
    ("sop", {**soic8_json(), "body": {"w": 3.9, "h": 4.9, "w_max": 3}},
     "sop: params.body: body max size 3.0 x None is below nominal"),
    ("qfn", {**qfn16_json(), "shape": "star"}, "shape 'star' not in"),
    ("qfn", {**qfn16_json(), "ep": {"w": 1, "h": 1, "paste_grid": [0, 1]}}, "paste grid"),
    ("pin_header", {"name": "H", "rows": 0}, "pin_header: H: rows and cols must be >= 1"),
    ("lcc_castellated", {"name": "L", "body": {"w": 5, "h": 5}, "pitch": 1, "pins": [1, 1, 1],
                         "pad_w": 0.5, "pad_l": 1}, "params.pins: expected an array of 4"),
])
def test_template_errors_are_wrapped_with_the_template_name(template, params, msg):
    with pytest.raises(fj.FpSpecJsonError) as err:
        fj.spec_from_json(template, params)
    assert msg in str(err.value), str(err.value)


def test_unknown_template_suggests_the_closest():
    with pytest.raises(fj.FpSpecJsonError) as err:
        fj.spec_from_json("qfm", {})
    assert "unknown template 'qfm' (did you mean 'qfn'?); templates: lga_grid, qfn" in str(
        err.value)


def test_null_is_named_once():
    with pytest.raises(fj.FpSpecJsonError) as err:
        fj.spec_from_json("sop", None)
    assert str(err.value) == "sop: params: expected an object, got null"
    with pytest.raises(fj.FpSpecJsonError) as err:
        fj.spec_from_json("sop", {**soic8_json(), "pitch": None})
    assert err.value.problems == ("sop: params.pitch: expected a number, got null",)


# ---------------------------------------------------------------------------
# Size limit: an oversized footprint is refused before anything is built
# ---------------------------------------------------------------------------
def _square_grid(cols: int, rows: int) -> dict:
    return {"name": "BGA", "body": {"w": 100, "h": 100},
            "grid": {"xs": {"count": cols, "pitch": 0.5}, "ys": {"count": rows, "pitch": 0.5},
                     "w": 0.3, "h": 0.3, "shape": "circle"},
            "numbering": {"rule": "linear", "order": "row_major"}}


def test_a_footprint_at_the_pad_limit_builds_and_one_more_is_refused():
    assert fj.MAX_PADS == 10_000
    assert len(fj.spec_from_json("lga_grid", _square_grid(100, 100)).pads) == fj.MAX_PADS
    with pytest.raises(fj.FpSpecJsonError) as err:
        fj.spec_from_json("lga_grid", _square_grid(100, 101))
    assert err.value.problems == (
        "lga_grid: params describe up to 10100 pads (copper, paste windows and vias); "
        "the limit is 10000",)


_HUGE = 10 ** 8
_LCC = {"name": "L", "body": {"w": 5, "h": 5}, "pitch": 1, "pad_w": 0.5, "pad_l": 1}
_DFN = {"name": "D", "body": {"w": 2, "h": 2}, "pitch": 0.5, "span": 1.9, "pad_w": 0.25,
        "pad_l": 0.6}


@pytest.mark.parametrize(("template", "params", "msg"), [
    # one count beyond the limit
    ("lga_grid", _square_grid(_HUGE, 1), "params.grid.xs.count: must be <= 10000, got 100000000"),
    ("sop", {**soic8_json(), "pins": _HUGE}, "params.pins: must be <= 10000, got 100000000"),
    ("pin_header", {"name": "H", "rows": 2, "cols": _HUGE}, "params.cols: must be <= 10000"),
    ("qfn", {**qfn16_json(), "ep": {"w": 1.7, "h": 1.7, "paste_grid": [_HUGE, 1]}},
     "params.ep.paste_grid[0]: must be <= 10000"),
    ("lga_grid", {**_square_grid(1, 1), "grid": {
        **_square_grid(1, 1)["grid"], "xs": [{"count": 6000, "pitch": 1}] * 2}},
     "params.grid.xs: more than 10000 coordinates"),
    # counts within the limit whose footprint is not
    ("lga_grid", _square_grid(5000, 5000), "lga_grid: params describe up to 25000000 pads"),
    ("qfn", {**qfn16_json(), "ep": {"w": 1.7, "h": 1.7, "paste_grid": [200, 200]}},
     "qfn: params describe up to 40017 pads"),
    ("lcc_castellated", {**_LCC, "pins": [5000, 5000, 5000, 0]}, "up to 15000 pads"),
    ("dfn", {**_DFN, "pins_per_side": 6000}, "dfn: params describe up to 12000 pads"),
    ("sop", {**soic8_json(), "pins": 9998, "ep": {"w": 1, "h": 1, "paste_grid": [2, 2]}},
     "sop: params describe up to 10003 pads"),                 # 9998 + EP copper + 4 windows
    ("pin_header", {"name": "H", "rows": 5000, "cols": 5000}, "up to 25000000 pads"),
])
def test_oversized_footprints_are_refused_before_they_are_built(template, params, msg):
    start = time.perf_counter()
    with pytest.raises(fj.FpSpecJsonError) as err:
        fj.spec_from_json(template, params)
    assert msg in str(err.value), str(err.value)
    assert time.perf_counter() - start < 5.0       # nothing big was allocated (uncapped: minutes)


def test_pad_numbers_are_not_sizes():
    """start / first name pads; they are not capped by the pad limit."""
    spec = _grid_spec({"rule": "linear", "order": "row_major", "start": 20001}, pin1=20001)
    assert sorted(p.number for p in spec.pads)[0] == "20001"
    row = {"side": "L", "at": 0, "positions": [0, 1], "first": 50000, "length": 1, "width": 0.5}
    spec = fj.spec_from_json("lga_grid", {"name": "E", "body": {"w": 3, "h": 3},
                                          "edge_rows": [row], "pin1": 50000})
    assert sorted(p.number for p in spec.pads) == ["50000", "50001"]


# ---------------------------------------------------------------------------
# Never mutates its input
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(("template", "params"), [
    ("qfn", qfn16_json()), ("lga_grid", lg290p_as_lga_grid_json()),
    ("sop", {**soic8_json(), "model": {"path": "m.step", "scale": [1, 1, 1]},
             "attr_flags": ["dnp"]}),
])
def test_input_is_never_mutated(template, params):
    before = copy.deepcopy(params)
    fj.spec_from_json(template, params)
    assert params == before


def test_read_only_mappings_are_accepted():
    params = lg290p_as_lga_grid_json()
    frozen = MappingProxyType({**params, "body": MappingProxyType(params["body"]),
                               "numbering": MappingProxyType(params["numbering"])})
    assert fj.spec_from_json("lga_grid", frozen) == fj.spec_from_json("lga_grid", params)


# ---------------------------------------------------------------------------
# Importable by KiCad's bundled Python (stdlib only, 3.11)
# ---------------------------------------------------------------------------
STDLIB_OK = {"__future__", "difflib", "json", "math", "typing", "dataclasses", "collections",
             "fp_author_templates"}


def test_imports_are_stdlib_plus_fp_author_templates_only():
    tree = ast.parse((TOOLS / "fp_spec_json.py").read_text(encoding="utf-8"))
    mods = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import)
            for a in n.names}
    mods |= {n.module.split(".")[0] for n in ast.walk(tree)
             if isinstance(n, ast.ImportFrom) and n.module}
    assert mods <= STDLIB_OK, mods - STDLIB_OK


def _kicad9_python() -> Path | None:
    for inst in env.discover():
        if inst.version_tuple[0] == 9 and inst.python is not None:
            return inst.python
    return None


def test_live_kicad9_python_imports_and_builds_from_json():
    python = _kicad9_python()
    if python is None:
        pytest.skip("no KiCad 9 bundled Python (3.11) to import fp_spec_json with")
    code = ("import json, sys; sys.path.insert(0, sys.argv[1]); import fp_spec_json as fj; "
            "s = fj.spec_from_json('sop', json.loads(sys.argv[2])); "
            "print(json.dumps([sys.version_info[:2], len(s.pads)]))")
    r = subprocess.run([str(python), "-c", code, str(TOOLS), json.dumps(soic8_json())],
                       capture_output=True, stdin=subprocess.DEVNULL, encoding="utf-8",
                       errors="replace", timeout=60)
    assert r.returncode == 0, r.stderr
    version, pads = json.loads(r.stdout.strip().splitlines()[-1])
    assert version[0] == 3 and version[1] >= 11 and pads == 8
