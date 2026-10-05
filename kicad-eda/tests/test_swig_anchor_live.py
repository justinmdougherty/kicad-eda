"""swig_anchor on the real pcbnew (KiCad 9.0.4 locally, 10.0.5 in kicad-eda:wave1).

One KiCad-python child (tests/fixtures/swig_python.run_child: kicad_quiet before
pcbnew, stdin closed, a timeout) builds a board from stock footprints
(R_0603_1608Metric, SOT-23, PinHeader_1x03_P2.54mm_Vertical) on F and B at the
rotations {0, 30, 90, 180, 270, -45}, plus a 45-degree B-side SOT-23, with
swig_anchor.place_at, applies every board_only / dnp / in_bom combination with
swig_anchor.apply_attrs, saves, reloads and reports. The test then reads the
saved file with kicad_import_pcb.read_board and asserts:

  (a) every origin round-trips within 1e-3 mm and every rotation modulo 360,
      both sides (the file, the in-memory read_back and the reloaded read_back);
  (b) every pad sits at its library position, mirrored in Y on B, rotated by
      the displayed rotation about the origin; SMD pads are on the part's side;
  (c) the (attr ...) flags in the file are exactly the ones asked for;
  (d) apply_attrs warned about nothing (every setter exists on 9.0 and 10.0).

Skips cleanly without a KiCad Python that imports pcbnew.
"""
from __future__ import annotations

import itertools
import json
import re
from pathlib import Path

import pytest

import kicad_import_pcb as kpcb
import swig_anchor as sa
from fixtures.swig_python import PREAMBLE, kicad_footprints, require_kicad_python, run_child

OX, OY = 100.5, 115.25                    # where the board frame's origin sits on the sheet
TOL_MM = 1e-3
SAVED_FORMATS = ("20241229", "20260206")  # 9.0 / 10.0 board files
FOOTPRINTS = (("Resistor_SMD", "R_0603_1608Metric"),
              ("Package_TO_SOT_SMD", "SOT-23"),
              ("Connector_PinHeader_2.54mm", "PinHeader_1x03_P2.54mm_Vertical"))
ROTATIONS = (0, 30, 90, 180, 270, -45)
# every board_only x dnp x in_bom combination, plus explicit "off" values
FLAG_KEYS = ("board_only", "dnp", "in_bom")
COMBOS = tuple({k: v for k, v in zip(FLAG_KEYS, combo, strict=True) if v is not None}
               for combo in itertools.product((None, True), (None, True), (None, False)))
COMBOS += ({"board_only": False, "dnp": False, "in_bom": True},)


def _parts(libs: dict[str, str]) -> list[dict]:
    """37 parts on a 6-column grid: 3 footprints x 2 sides x 6 rotations, plus
    a 45-degree B-side SOT-23. Origins are off the 0.1 mm grid on purpose."""
    parts = []
    grid = itertools.product(FOOTPRINTS, ("F", "B"), ROTATIONS)
    for i, ((lib, name), side, rot) in enumerate(grid):
        at = [round(6.1234 + 9.5 * (i % 6), 4), round(5.0567 + 8.25 * (i // 6), 4), rot]
        parts.append({"ref": f"P{i + 1}", "lib": libs[lib], "name": name, "side": side,
                      "at": at, **COMBOS[i % len(COMBOS)]})
    parts.append({"ref": "U45", "lib": libs["Package_TO_SOT_SMD"], "name": "SOT-23",
                  "side": "B", "at": [64.4321, 30.9876, 45], "dnp": True})
    return parts


CHILD = PREAMBLE + """
import swig_anchor as sa

spec = json.load(open(sys.argv[1], encoding="utf-8"))
out_path = sys.argv[2]
OX, OY = spec["origin"]
SMD = pcbnew.PAD_ATTRIB_SMD
FLAGS = (("board_only", "IsBoardOnly"), ("dnp", "IsDNP"),
         ("exclude_from_bom", "IsExcludedFromBOM"),
         ("exclude_from_pos_files", "IsExcludedFromPosFiles"))


def pads_of(fp, dx=0.0, dy=0.0):
    return [[p.GetNumber(), p.GetPosition().x / 1e6 - dx, p.GetPosition().y / 1e6 - dy,
             p.GetAttribute() == SMD, p.IsOnLayer(pcbnew.F_Cu), p.IsOnLayer(pcbnew.B_Cu)]
            for p in fp.Pads()]


def flags_of(fp):
    return sorted(token for token, getter in FLAGS if getattr(fp, getter)())


board = pcbnew.CreateEmptyBoard()
local, placed, warnings = {}, {}, {}
for part in spec["parts"]:
    fp = pcbnew.FootprintLoad(part["lib"], part["name"])
    if fp is None:
        raise SystemExit("FootprintLoad returned None for %s in %s" % (part["name"], part["lib"]))
    if part["name"] not in local:            # library geometry: F side, rotation 0
        if fp.IsFlipped() or fp.GetOrientationDegrees() != 0:
            raise SystemExit("a fresh %s is not at rotation 0 on F" % part["name"])
        pos = fp.GetPosition()
        local[part["name"]] = pads_of(fp, pos.x / 1e6, pos.y / 1e6)
    fp.SetReference(part["ref"])
    sa.place_at(board, fp, part["at"], part["side"], OX, OY, pcbnew)
    warnings[part["ref"]] = sa.apply_attrs(fp, part, pcbnew)
    placed[part["ref"]] = sa.read_back(fp, OX, OY)
pcbnew.SaveBoard(out_path, board)
loaded = pcbnew.LoadBoard(out_path)
if loaded is None:
    raise SystemExit("LoadBoard returned None for " + out_path)
reloaded = {fp.GetReference(): {"at": sa.read_back(fp, OX, OY), "pads": pads_of(fp),
                                "flags": flags_of(fp)}
            for fp in loaded.GetFootprints()}
print(json.dumps({"version": pcbnew.Version(), "local": local, "placed": placed,
                  "warnings": warnings, "reloaded": reloaded}))
"""


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory) -> dict:
    kpy = require_kicad_python()
    libs = {lib: str(kicad_footprints(kpy, f"{lib}.pretty")) for lib, _ in FOOTPRINTS}
    work = tmp_path_factory.mktemp("anchor")
    parts = _parts(libs)
    spec = work / "spec.json"
    spec.write_text(json.dumps({"origin": [OX, OY], "parts": parts}), encoding="utf-8")
    pcb = work / "anchor.kicad_pcb"
    res = run_child(CHILD, [spec, pcb], work)
    text = pcb.read_text(encoding="utf-8")
    board = kpcb.read_board(text)
    return {"res": res, "parts": parts, "text": text,
            "file": {fp.ref: fp for fp in board.footprints}}


def test_fixture_covers_every_case(built):
    parts = built["parts"]
    assert len(parts) == 37 and len(built["file"]) == 37
    keys = {(p["name"], p["side"], p["at"][2]) for p in parts}
    assert len(keys) == 3 * 2 * 6 + 1 and ("SOT-23", "B", 45) in keys
    tokens = {sa.attr_tokens(p) for p in parts}
    assert len({frozenset(c.items()) for c in COMBOS}) == 9
    assert {sa.attr_tokens(c) for c in COMBOS} <= tokens
    assert built["res"]["version"].startswith(("9.", "10."))
    assert re.search(r"\(version (\d+)\)", built["text"]).group(1) in SAVED_FORMATS


def _assert_at(got, part: dict, tol: float, where: str) -> None:
    x, y, rot = part["at"]
    assert abs(got[0] - x) <= tol and abs(got[1] - y) <= tol, (where, part["ref"], got, x, y)
    assert sa.same_rot(got[2], rot), (where, part["ref"], got[2], rot)
    assert 0.0 <= got[2] < 360.0, (where, part["ref"], got[2])
    assert got[3] == part["side"], (where, part["ref"], got[3])


def test_a_every_origin_and_rotation_round_trips_through_the_file(built):
    for part in built["parts"]:
        fp = built["file"][part["ref"]]
        _assert_at((fp.x - OX, fp.y - OY, fp.rot, fp.side), part, TOL_MM, "file")
        assert fp.lib_id.endswith(part["name"])


def test_a_read_back_matches_in_memory_and_after_reload(built):
    res = built["res"]
    for part in built["parts"]:
        _assert_at(res["placed"][part["ref"]], part, 1e-6, "in memory")
        _assert_at(res["reloaded"][part["ref"]]["at"], part, 1e-6, "reloaded")


def test_a_the_45_degree_back_part_displays_45(built):
    fp = built["file"]["U45"]
    assert fp.side == "B" and fp.rot == pytest.approx(45.0, abs=1e-6)
    assert fp.x - OX == pytest.approx(64.4321, abs=TOL_MM)
    assert fp.y - OY == pytest.approx(30.9876, abs=TOL_MM)


def _expected_pad(local_xy: tuple[float, float], part: dict) -> tuple[float, float]:
    lx, ly = local_xy
    if part["side"] == "B":                   # TOP_BOTTOM flip mirrors the children in Y
        ly = -ly
    x, y, rot = part["at"]
    return kpcb.to_sheet((lx, ly), (OX + x, OY + y, rot))


def test_b_pads_are_mirrored_on_b_and_rotated_about_the_origin(built):
    res = built["res"]
    checked = 0
    for part in built["parts"]:
        local = {p[0]: p for p in res["local"][part["name"]]}
        pads = res["reloaded"][part["ref"]]["pads"]
        assert sorted(p[0] for p in pads) == sorted(local), part["ref"]
        for number, px, py, smd, on_f, on_b in pads:
            ex, ey = _expected_pad((local[number][1], local[number][2]), part)
            assert abs(px - ex) <= TOL_MM and abs(py - ey) <= TOL_MM, \
                (part["ref"], part["side"], part["at"], number, (px, py), (ex, ey))
            if smd:
                assert (on_f, on_b) == (part["side"] == "F", part["side"] == "B"), \
                    (part["ref"], number, on_f, on_b)
            else:
                assert on_f and on_b, (part["ref"], number)   # through-hole: both sides
            checked += 1
    assert checked == 12 * 2 + 12 * 3 + 12 * 3 + 3


def test_b_a_b_side_sot23_pin_1_is_mirrored_in_y(built):
    """SOT-23 pin 1 is at (-0.95ish, +0.95ish) on F; at rotation 0 on B its Y flips."""
    res = built["res"]
    part = next(p for p in built["parts"] if p["name"] == "SOT-23" and p["side"] == "B"
                and p["at"][2] == 0)
    lx, ly = {p[0]: p[1:3] for p in res["local"]["SOT-23"]}["1"]
    px, py = {p[0]: p[1:3] for p in res["reloaded"][part["ref"]]["pads"]}["1"]
    assert ly != 0
    assert px - OX - part["at"][0] == pytest.approx(lx, abs=TOL_MM)
    assert py - OY - part["at"][1] == pytest.approx(-ly, abs=TOL_MM)


def test_c_attribute_flags_in_the_file_are_exactly_the_requested_ones(built):
    for part in built["parts"]:
        want = sa.attr_tokens(part)
        fp = built["file"][part["ref"]]
        assert fp.attrs & sa.MANAGED_TOKENS == want, (part["ref"], sorted(fp.attrs), want)
        assert sorted(built["res"]["reloaded"][part["ref"]]["flags"]) == sorted(want)
        if part.get("board_only"):
            assert fp.board_only


def test_d_apply_attrs_warned_about_nothing(built):
    assert all(w == [] for w in built["res"]["warnings"].values()), built["res"]["warnings"]


def test_the_child_imported_swig_anchor_under_kicad_python(built):
    """Acceptance: swig_anchor imports under KiCad's bundled Python (3.11 on 9.0.4)."""
    assert Path(sa.__file__).name == "swig_anchor.py"
    assert built["res"]["placed"]                      # the child ran sa.place_at itself
