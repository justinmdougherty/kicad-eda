"""rule_areas (ADR D7.5, D7.7, D7.10): version-safe rule areas on top of board_outline.

venv tier (always runs): the pure helpers (name patterns, layer specs, polygon and
rectangle points, flag and layer matching), the module importing without pcbnew,
and the round-2 erratum: no second CopperPour / ZoneFills shim, no copy of
copper_lset (board_outline owns both).

live tier (KiCad 9.0.4 locally, 10.0.5 under the K10 recipe; skipped without a
pcbnew Python): add_rule_area on 2L / 4L / 6L boards ('all' = every enabled
copper layer, named layers checked with IsLayerEnabled, a SHAPE_POLY_SET or
points, never None), the five flags read back, has_rule_area (patterns, layers,
flags, stock footprint keep-outs: RF_Module:ESP32-C3-WROOM-02 keeps out every
copper layer, RF_Antenna:Texas_SWRA416 F.Cu only), remove_rule_areas (prefix and
glob, footprint areas untouched), and the areas surviving SaveBoard + LoadBoard.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

import rule_areas as ra
from fixtures.swig_python import PREAMBLE, kicad_footprints, require_kicad_python, run_child

SOURCE = Path(ra.__file__).read_text(encoding="utf-8")
CU4 = ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]
CU6 = ["F.Cu", "In1.Cu", "In2.Cu", "In3.Cu", "In4.Cu", "B.Cu"]


# ---------------------------------------------------------------------------
# venv tier
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name,pattern,hit", [
    ("ANTENNA", None, True), ("", None, True), ("ANTENNA", "ANT", True),
    ("ANTSCAF_AE1", "ANT", True), ("ANTENNA", "ant", False), ("RFSCAF_RF_1", "*SCAF*", True),
    ("RFREFSCAF_RF_In1.Cu", "*SCAF*", True), ("ANTENNA", "*SCAF*", False),
    ("RFCOR_X", "RFCOR_?", True), ("RFCOR_XY", "RFCOR_?", False), ("SCAF", "[RS]CAF", True),
    ("XCAF", "[RS]CAF", False),
    ("ANTENNA", "", True)])
def test_name_matches(name, pattern, hit):
    assert ra.name_matches(name, pattern) is hit


def test_resolve_layers_all_and_names_in_stack_order():
    assert ra.resolve_layers("all", CU4) == tuple(CU4)
    assert ra.resolve_layers(["B.Cu", "F.Cu", "B.Cu"], CU4) == ("F.Cu", "B.Cu")
    assert ra.resolve_layers(("In2.Cu",), CU4) == ("In2.Cu",)


@pytest.mark.parametrize("spec,words", [
    (["In3.Cu"], "not an enabled copper layer"), (["In1.Cu"], "not an enabled copper layer"),
    (["Bogus.Cu"], "not an enabled copper layer"), ([], "at least one"),
    ("F.Cu", "'all' or a list"), (None, "'all' or a list"), ([3], "layer names")])
def test_resolve_layers_refuses(spec, words):
    enabled = ["F.Cu", "B.Cu"] if spec in (["In1.Cu"],) else CU4
    with pytest.raises(ValueError, match=words):
        ra.resolve_layers(spec, enabled)


def test_polygon_and_rect_points():
    assert ra.polygon_points([(0, 0), [1, 0], (1, 2)]) == ((0.0, 0.0), (1.0, 0.0), (1.0, 2.0))
    assert ra.rect_points(1, 2, 3, 4) == ((1.0, 2.0), (4.0, 2.0), (4.0, 6.0), (1.0, 6.0))
    for bad in ([(0, 0), (1, 1)], [(0, 0), (1, 0), (float("nan"), 1)], [(0, 0, 0), (1, 0), (1, 1)],
                "abc", [(0, 0), (1, 0), (True, 1)]):
        with pytest.raises(ValueError):
            ra.polygon_points(bad)
    for w, h in ((0, 1), (1, -1), (float("inf"), 1)):
        with pytest.raises(ValueError):
            ra.rect_points(0, 0, w, h)


def test_flags_and_layer_cover():
    have = {f: False for f in ra.FLAGS} | {"no_pour": True, "no_vias": True}
    assert ra.flags_match(have, {"no_pour": True})
    assert ra.flags_match(have, {"no_pour": True, "no_tracks": False, "no_pads": None})
    assert not ra.flags_match(have, {"no_tracks": True})
    with pytest.raises(ValueError, match="unknown"):
        ra.flags_match(have, {"no_copper": True})
    assert ra.covers(CU4, ["F.Cu", "B.Cu"]) and ra.covers(CU4, [])
    assert not ra.covers(["F.Cu"], CU4)


def test_module_imports_without_pcbnew_and_refuses_board_work():
    if ra.pcbnew is not None:
        pytest.skip("this interpreter has pcbnew")
    with pytest.raises(RuntimeError, match="pcbnew"):
        ra.add_rule_area(object(), [(0, 0), (1, 0), (1, 1)], name="X")
    with pytest.raises(RuntimeError, match="pcbnew"):
        ra.enabled_copper(object())


def test_builds_on_board_outline_with_no_second_compat_layer():
    code = "\n".join(ln for ln in SOURCE.splitlines() if not ln.lstrip().startswith("#"))
    body = re.sub(r'"""[\s\S]*?"""', "", code)                 # docstrings may name them
    for shim in ("DoNotAllowCopperPour", "DoNotAllowZoneFills", "AllCuMask", "_POUR_FLAG",
                 "RemoveAllContours"):
        assert shim not in body, shim
    # the pour flag is read with bo.blocks_pour, 'all' is bo.copper_lset, and the area is
    # made by board_shapes.add_keepout_polygon (bo.set_no_pour + bo.fill_zone_outline)
    for helper in ("bo.blocks_pour", "bo.copper_lset", "board_shapes.add_keepout_polygon"):
        assert helper in body, helper
    assert "import board_outline as bo" in SOURCE
    assert "import kicad_quiet" in SOURCE.split("import pcbnew")[0]
    assert len(SOURCE.splitlines()) < 400


# ---------------------------------------------------------------------------
# live tier
# ---------------------------------------------------------------------------
LIVE = PREAMBLE + r"""
import board_outline as bo
import rule_areas as ra

out, fp_root = sys.argv[1], sys.argv[2]

def board_of(n):
    b = pcbnew.CreateEmptyBoard()
    b.SetCopperLayerCount(n)
    return b

def err(fn):
    try:
        fn()
    except ValueError as e:
        return str(e)
    return None

res = {}
b4 = board_of(4)
square = ra.rect_points(110, 110, 10, 5)
a1 = ra.add_rule_area(b4, square, "all", name="ANTENNA", no_pour=True, no_vias=True, no_pads=True)
a2 = ra.add_rule_area(b4, [(130, 110), (140, 110), (140, 115)], ["B.Cu", "F.Cu"],
                      name="RFSCAF_RF_1", no_tracks=True, no_vias=True)
a3 = ra.add_rule_area(b4, square, ["In1.Cu"], name="RFREF_RF_In1.Cu")
a4 = ra.add_rule_area(b4, square, "all", name="ANTSCAF_AE1", no_tracks=True, no_vias=True,
                      no_pour=True, no_pads=True, no_footprints=True)
res["types"] = [type(z).__name__ for z in (a1, a2, a3, a4)]
res["areas4"] = ra.find_rule_areas(b4)
res["errors"] = {
    "in3": err(lambda: ra.add_rule_area(b4, square, ["In3.Cu"], name="X")),
    "bogus": err(lambda: ra.add_rule_area(b4, square, ["Bogus.Cu"], name="X")),
    "noname": err(lambda: ra.add_rule_area(b4, square, name=" ")),
    "two_pts": err(lambda: ra.add_rule_area(b4, [(0, 0), (1, 1)], name="X")),
    "in1_on_2l": err(lambda: ra.add_rule_area(board_of(2), square, ["In1.Cu"], name="X"))}
res["count_after_errors"] = len([z for z in b4.Zones() if z.GetIsRuleArea()])
res["has"] = {
    "ant_all_pour": ra.has_rule_area(b4, "ANTENNA", "all", no_pour=True),
    "ant_all_tracks": ra.has_rule_area(b4, "ANTENNA", "all", no_tracks=True),
    "ant_in2": ra.has_rule_area(b4, "ANT", ["In2.Cu"], no_vias=True, no_tracks=False),
    "scaf_all": ra.has_rule_area(b4, "*SCAF*", "all", no_tracks=True),
    "rfscaf_all": ra.has_rule_area(b4, "RFSCAF", "all"),
    "any_in1": ra.has_rule_area(b4, None, ["In1.Cu"]),
    "missing": ra.has_rule_area(b4, "NOPE", None)}
res["removed"] = ra.remove_rule_areas(b4, "*SCAF*")
res["left"] = sorted(z.GetZoneName() for z in b4.Zones() if z.GetIsRuleArea())
res["refuse_remove_all"] = err(lambda: ra.remove_rule_areas(b4, None))
pcbnew.SaveBoard(out, b4)
back = bo.load_board(out)
res["reloaded"] = ra.find_rule_areas(back)
res["reloaded_has"] = ra.has_rule_area(back, "ANTENNA", "all", no_pour=True, no_vias=True)

b6 = board_of(6)
bo_shape = pcbnew.SHAPE_POLY_SET()
bo_shape.NewOutline()
for x, y in ((100, 100), (120, 100), (120, 120), (100, 120)):
    bo_shape.Append(bo.nm(x), bo.nm(y))
ra.add_rule_area(b6, bo_shape, "all", name="POLYSET", no_pour=True)
res["areas6"] = ra.find_rule_areas(b6)
res["enabled2"] = list(ra.enabled_copper(board_of(2)))

# stock footprint keep-outs (no board area at all)
bf = board_of(4)
for ref, lib, name, x in (("U1", "RF_Module", "ESP32-C3-WROOM-02", 120),
                          ("AE1", "RF_Antenna", "Texas_SWRA416_868MHz_915MHz", 160)):
    fp = pcbnew.FootprintLoad(fp_root + "/" + lib + ".pretty", name)
    fp.SetReference(ref)
    fp.SetPosition(pcbnew.VECTOR2I(bo.nm(x), bo.nm(120)))
    bf.Add(fp)
res["fp_areas"] = ra.find_rule_areas(bf, include_footprints=True)
res["fp_has"] = {
    "board_only": ra.has_rule_area(bf, None, "all", no_pour=True),
    "with_fps": ra.has_rule_area(bf, None, "all", no_pour=True, include_footprints=True),
    "fcu_tracks_ok": ra.has_rule_area(bf, None, ["F.Cu"], no_pour=True, no_tracks=False,
                                      include_footprints=True)}
res["fp_removed"] = ra.remove_rule_areas(bf, "*")
res["major"] = int(str(pcbnew.GetBuildVersion()).lstrip("(").split(".")[0])
print(json.dumps(res))
"""


@pytest.fixture(scope="module")
def live(tmp_path_factory) -> dict:
    kpy = require_kicad_python()
    fp_root = kicad_footprints(kpy, "RF_Module.pretty").parent
    work = tmp_path_factory.mktemp("rule_areas")
    pcb = work / "areas.kicad_pcb"
    res = run_child(LIVE, [pcb, fp_root], work)
    return {**res, "pcb": pcb}


def _by_name(records: list[dict]) -> dict[str, dict]:
    return {r["name"]: r for r in records}


def test_live_add_rule_area_layers_and_flags(live):
    assert live["types"] == ["ZONE"] * 4                      # never None
    areas = _by_name(live["areas4"])
    ant = areas["ANTENNA"]
    assert ant["layers"] == CU4 and ant["footprint"] is None
    assert ant["flags"] == {"no_tracks": False, "no_vias": True, "no_pads": True,
                            "no_pour": True, "no_footprints": False}
    assert areas["RFSCAF_RF_1"]["layers"] == ["F.Cu", "B.Cu"]
    assert areas["RFSCAF_RF_1"]["flags"]["no_tracks"] and not areas["RFSCAF_RF_1"]["flags"][
        "no_pour"]
    assert areas["RFREF_RF_In1.Cu"]["layers"] == ["In1.Cu"]
    assert not any(areas["RFREF_RF_In1.Cu"]["flags"].values())        # a named area, no flags
    assert all(areas["ANTSCAF_AE1"]["flags"].values())
    assert _by_name(live["areas6"])["POLYSET"]["layers"] == CU6
    assert live["enabled2"] == ["F.Cu", "B.Cu"]


def test_live_add_rule_area_refuses_bad_input(live):
    errors = live["errors"]
    assert "not an enabled copper layer" in errors["in3"] and "In3.Cu" in errors["in3"]
    assert "not an enabled copper layer" in errors["bogus"]
    assert "not an enabled copper layer" in errors["in1_on_2l"]       # GetLayerID is valid
    assert "name" in errors["noname"] and "3 points" in errors["two_pts"]
    assert live["count_after_errors"] == 4                           # nothing half-added


def test_live_has_rule_area(live):
    assert live["has"] == {"ant_all_pour": True, "ant_all_tracks": False, "ant_in2": True,
                           "scaf_all": True, "rfscaf_all": False, "any_in1": True,
                           "missing": False}


def test_live_remove_rule_areas_and_reload(live):
    assert sorted(live["removed"]) == ["ANTSCAF_AE1", "RFSCAF_RF_1"]
    assert live["left"] == ["ANTENNA", "RFREF_RF_In1.Cu"]
    assert "pattern" in live["refuse_remove_all"]
    back = _by_name(live["reloaded"])
    assert sorted(back) == ["ANTENNA", "RFREF_RF_In1.Cu"]
    assert back["ANTENNA"]["layers"] == CU4 and back["ANTENNA"]["flags"]["no_pour"]
    assert live["reloaded_has"]
    text = Path(live["pcb"]).read_text(encoding="utf-8")
    assert "(copperpour not_allowed)" in text and '(name "ANTENNA")' in text


def test_live_stock_footprint_keepouts(live):
    by_fp = {r["footprint"]: r for r in live["fp_areas"]}
    esp, ti = by_fp["U1"], by_fp["AE1"]
    assert esp["layers"] == CU4 and esp["flags"]["no_pour"] and esp["flags"]["no_tracks"]
    assert ti["layers"] == ["F.Cu"] and ti["flags"]["no_pour"] and not ti["flags"]["no_tracks"]
    assert live["fp_has"] == {"board_only": False, "with_fps": True, "fcu_tracks_ok": True}
    assert live["fp_removed"] == []                       # footprint areas are never removed


def test_live_saved_board_format(live, record_property):
    version = re.search(r"\(version (\d+)\)", Path(live["pcb"]).read_text(encoding="utf-8"))
    assert version and version.group(1) in ("20241229", "20260206")   # 9.0 / 10.0 boards
    record_property("pcbnew_major", live["major"])
    print(f"pcbnew {live['major']}: board format {version.group(1)}")


def test_live_records_are_json(live):
    for rec in live["areas4"] + live["fp_areas"]:
        assert set(rec) == {"name", "footprint", "layers", "flags"}
        assert set(rec["flags"]) == set(ra.FLAGS)
    json.dumps(live["areas4"])
