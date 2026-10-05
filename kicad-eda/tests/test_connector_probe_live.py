"""swig_connector_probe: connector geometry for the napkin face_connector_out policy.

The probe (server/tools/swig_connector_probe.py) runs under KiCad's bundled
Python and prints one JSON line {ok, board_bbox, sheet_origin, connectors: [...]}
for every J* part, reusing swig_gate_checks' connector logic read-only
(_world_mouth, interior_refs, is_edge_connector).

Unit tier (the dev venv, no pcbnew): the pure helpers (rotate_mouth, the pad
summary that lets F5-08 recognise headers on imported libraries) and the
argument / missing-file failures, which are decided before pcbnew is imported.

Live tier (skipped without a KiCad Python that imports pcbnew; runs on 9.0.4
locally and 10.0.5 in Docker): pipeline/run_all.py's negative_g3 netlist
(_build_g3_fixture: cp2102n with J1, a USB-C, fixed at x=45, y=14, rot=270,
mouth inward, plus the J9 2x20 socket wall flagged interior) is rebuilt under
tmp with board_pipeline_tools.build_board_from_netlist and probed: J1's mouth
points INTO the board by the dot-product test swig_gate_checks.check_placement
uses; a rebuild with J1 at rot + 90 shows the probed mouth turned by the
documented convention (rot + 90: (mx, my) -> (my, -mx)); B-side USB-Cs obey
the same convention; a stock PinHeader_2x05_P2.54mm_Vertical reports pads
{count 10, tht true, rows 2, pitch_mm 2.54}; a missing or unreadable board
gives ok false (never a crash).
"""
from __future__ import annotations

import json
import math
import subprocess
from pathlib import Path

import pytest

import board_pipeline_tools as bpt
import swig_anchor as sa
import swig_connector_probe as probe_mod
import toolchain as tc
from conftest import load_example
from fixtures.swig_python import require_kicad_python

TOOLS = Path(__file__).resolve().parents[1] / "server" / "tools"
PROBE = TOOLS / "swig_connector_probe.py"
TOL_MM = 0.05
HEADER_2X05 = "Connector_PinHeader_2.54mm:PinHeader_2x05_P2.54mm_Vertical"
USB_C = "Connector_USB:USB_C_Receptacle_HRO_TYPE-C-31-M-12"


# =============================================================================
# unit tier: pure helpers
# =============================================================================
@pytest.mark.parametrize("mouth,delta,expected", [
    ((1, 0), 90, (0, -1)),          # right -> up (Y down: counter-clockwise on screen)
    ((0, -1), 90, (-1, 0)),         # up -> left
    ((-1, 0), 90, (0, 1)),          # left -> down
    ((0, 1), 90, (1, 0)),           # down -> right
    ((1, 0), 0, (1, 0)),
    ((1, 0), 180, (-1, 0)),
    ((1, 0), 270, (0, 1)),
    ((1, 0), -90, (0, 1)),
    ((0, 1), 450, (1, 0)),
    ((1.0, 0.0), 90.0, (0, -1)),
])
def test_rotate_mouth_follows_the_documented_convention(mouth, delta, expected):
    assert probe_mod.rotate_mouth(mouth, delta) == expected


def test_rotate_mouth_plus_90_is_my_minus_mx_for_every_axis():
    for mx, my in ((1, 0), (0, 1), (-1, 0), (0, -1)):
        assert probe_mod.rotate_mouth((mx, my), 90) == (my, -mx)


@pytest.mark.parametrize("delta", [45, 12.5, 89.0])
def test_rotate_mouth_rejects_non_quarter_turns(delta):
    with pytest.raises(ValueError, match="multiple of 90"):
        probe_mod.rotate_mouth((1, 0), delta)


def _grid(cols: int, rows: int, pitch: float = 2.54, kind: str = "pth") -> list:
    """Pads of a cols x rows header in footprint coordinates (pin 1 at 0, 0)."""
    return [(c * pitch, r * pitch, kind) for r in range(rows) for c in range(cols)]


def test_pads_summary_of_a_vertical_2x05_header():
    assert probe_mod.pads_summary(_grid(2, 5)) == {
        "count": 10, "tht": True, "rows": 2, "pitch_mm": 2.54}


def test_pads_summary_of_a_horizontal_header_rows_follow_the_long_axis():
    assert probe_mod.pads_summary(_grid(8, 2, 2.0)) == {
        "count": 16, "tht": True, "rows": 2, "pitch_mm": 2.0}
    assert probe_mod.pads_summary(_grid(6, 1)) == {
        "count": 6, "tht": True, "rows": 1, "pitch_mm": 2.54}


def test_pads_summary_square_and_single_pad():
    assert probe_mod.pads_summary(_grid(2, 2, 1.27)) == {
        "count": 4, "tht": True, "rows": 2, "pitch_mm": 1.27}
    assert probe_mod.pads_summary([(0.0, 0.0, "pth")]) == {
        "count": 1, "tht": True, "rows": 1, "pitch_mm": None}
    assert probe_mod.pads_summary([]) == {
        "count": 0, "tht": False, "rows": 0, "pitch_mm": None}


def test_pads_summary_mixed_smd_is_not_tht_and_npth_is_not_counted():
    pads = _grid(1, 4) + [(1.0, 1.0, "smd")]
    assert probe_mod.pads_summary(pads)["tht"] is False
    pegs = [(-3.0, 2.0, "npth"), (6.0, 2.0, "npth")]
    assert probe_mod.pads_summary(_grid(2, 5) + pegs) == {
        "count": 10, "tht": True, "rows": 2, "pitch_mm": 2.54}
    assert probe_mod.pads_summary(pegs) == {
        "count": 0, "tht": False, "rows": 0, "pitch_mm": None}


def test_pads_summary_row_tolerance_is_0_05_mm():
    jitter = [(x, y + (0.03 if i % 2 else 0.0), k) for i, (x, y, k) in enumerate(_grid(2, 5))]
    assert probe_mod.pads_summary(jitter)["rows"] == 2
    staggered = [(x + (0.1 if y > 5 else 0.0), y, k) for x, y, k in _grid(2, 5)]
    assert probe_mod.pads_summary(staggered)["rows"] == 4


def test_pads_summary_pitch_ignores_stacked_pads_and_gaps():
    stacked = _grid(1, 4) + [(0.0, 0.0, "pth")]           # two pads on one centre
    assert probe_mod.pads_summary(stacked)["pitch_mm"] == 2.54
    keyed = [p for p in _grid(1, 6) if p[1] != 2 * 2.54]  # a missing pin (keying)
    assert probe_mod.pads_summary(keyed)["pitch_mm"] == 2.54
    noisy = [(0.0, 0.0, "pth"), (0.0, 2.5400000001, "pth")]
    assert probe_mod.pads_summary(noisy)["pitch_mm"] == 2.54


def test_pads_summary_does_not_mutate_its_input():
    pads = _grid(2, 3)
    before = list(pads)
    probe_mod.pads_summary(pads)
    assert pads == before


def _run_main(argv: list[str], capsys) -> tuple[int, dict]:
    rc = probe_mod.main(argv)
    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    return rc, json.loads(lines[-1])


def test_main_usage_error_is_ok_false(capsys):
    rc, out = _run_main([], capsys)
    assert rc == 2 and out["ok"] is False and "usage" in out["reason"]


def test_main_missing_board_is_ok_false_before_pcbnew(tmp_path, capsys):
    missing = tmp_path / "nope" / "board.kicad_pcb"
    rc, out = _run_main([str(missing)], capsys)
    assert rc == 1 and out["ok"] is False
    assert "board file not found" in out["reason"] and str(missing) in out["reason"]


@pytest.mark.parametrize("text,match", [
    ("{not json", "netlist"),
    ("[1, 2]", "netlist"),
])
def test_main_unreadable_netlist_is_ok_false(tmp_path, capsys, text, match):
    pcb = tmp_path / "b.kicad_pcb"
    pcb.write_text("(kicad_pcb)", encoding="utf-8")
    netlist = tmp_path / "n.json"
    netlist.write_text(text, encoding="utf-8")
    rc, out = _run_main([str(pcb), str(netlist)], capsys)
    assert rc == 1 and out["ok"] is False and match in out["reason"]


def test_main_missing_netlist_is_ok_false(tmp_path, capsys):
    pcb = tmp_path / "b.kicad_pcb"
    pcb.write_text("(kicad_pcb)", encoding="utf-8")
    rc, out = _run_main([str(pcb), str(tmp_path / "absent.json")], capsys)
    assert rc == 1 and out["ok"] is False and "netlist" in out["reason"]


# =============================================================================
# live tier: real boards built and probed under KiCad's bundled Python
# =============================================================================
def _negative_g3(j1_rot: float) -> dict:
    """pipeline/run_all.py _build_g3_fixture, reproduced (its builder writes
    into examples/): J1 fixed mid-board at rot `j1_rot`, C2 below the wall,
    the J9 2x20 socket wall flagged interior and no-connected."""
    d = load_example("cp2102n_uart")
    d["title"] = "NEGATIVE TEST - inward connector + plane pinch wall"
    d["name"] = "negative_g3"
    d["board"] = {"w": 54.0, "h": 52.0,
                  "auto_x": 2.0, "auto_y": 2.0, "auto_w": 34.0, "auto_h": 28.0}
    for c in d["components"]:
        if c["ref"] == "J1":
            c.update({"x": 45.0, "y": 14.0, "rot": j1_rot})
        if c["ref"] == "C2":
            c.update({"x": 10.0, "y": 46.0})
    d["components"].append({
        "ref": "J9", "lib_id": "Connector_Generic:Conn_02x20_Odd_Even", "value": "WALL",
        "footprint": "Connector_PinSocket_2.54mm:PinSocket_2x20_P2.54mm_Vertical",
        "x": 27.0, "y": 34.0, "rot": 90, "interior": True})
    d.setdefault("no_connect", [])
    d["no_connect"] += [["J9", str(i)] for i in range(1, 41)]
    return d


def _headers_board() -> dict:
    """A stock 2x05 header (rot 90: the pad summary is in footprint
    coordinates) and two B-side USB-Cs a quarter turn apart."""
    usb = {"lib_id": "Connector:USB_C_Receptacle", "value": "USB-C", "footprint": USB_C,
           "side": "B"}
    return {
        "name": "probe_headers", "title": "connector probe: header + B-side USB-C",
        "board": {"w": 40.0, "h": 30.0},
        "components": [
            {"ref": "J3", "lib_id": "Connector_Generic:Conn_02x05_Odd_Even", "value": "HDR",
             "footprint": HEADER_2X05, "x": 8.0, "y": 15.0, "rot": 90},
            {"ref": "J4", **usb, "x": 28.0, "y": 8.0, "rot": 0},
            {"ref": "J5", **usb, "x": 28.0, "y": 22.0, "rot": 90},
        ],
        "nets": {},
        "no_connect": [["J3", str(i)] for i in range(1, 11)]}


def _build(root: Path, name: str, design: dict, allowed_missing: str = "") -> tuple[Path, Path]:
    """Build `design` with build_board_from_netlist; return (pcb, netlist)."""
    work = root / name
    work.mkdir()
    netlist = work / "netlist.json"
    netlist.write_text(json.dumps(design, indent=2), encoding="utf-8")
    pcb = work / "netlist.kicad_pcb"
    res = bpt.build_board_from_netlist(str(netlist), 56, 40, out_pcb=str(pcb))
    assert pcb.is_file() and "error" not in res, res
    # cp2102n's netlist wires J1.SH, a pad the stock HRO footprint does not have:
    # the builder saves the board and reports it (ok False), nothing else may miss
    missing = res.get("missing_pads") or []
    assert all(allowed_missing and m.startswith(allowed_missing) for m in missing), res
    assert res.get("ok") or missing, res
    return pcb, netlist


def _probe(*args: Path | str) -> tuple[int, dict, str]:
    kpy = require_kicad_python()
    proc = subprocess.run([kpy, str(PROBE), *map(str, args)], capture_output=True,
                          encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL,
                          timeout=300)
    out = tc.last_json(proc.stdout)
    assert out is not None, f"probe printed no JSON\n{proc.stdout}\n{proc.stderr}"
    return proc.returncode, out, proc.stderr


def _ok(*args: Path | str) -> dict:
    rc, out, err = _probe(*args)
    assert rc == 0 and out["ok"] is True, (rc, out, err[-2000:])
    return {**out, "by_ref": {c["ref"]: c for c in out["connectors"]}}


@pytest.fixture(scope="module")
def g3(tmp_path_factory: pytest.TempPathFactory) -> dict:
    require_kicad_python()
    root = tmp_path_factory.mktemp("probe_g3")
    base_pcb, base_nl = _build(root, "base", _negative_g3(270), allowed_missing="J1.")
    turned_pcb, turned_nl = _build(root, "turned", _negative_g3((270 + 90) % 360),
                                   allowed_missing="J1.")
    return {"pcb": base_pcb, "netlist": base_nl, "base": _ok(base_pcb, base_nl),
            "turned": _ok(turned_pcb, turned_nl)}


@pytest.fixture(scope="module")
def headers(tmp_path_factory: pytest.TempPathFactory) -> dict:
    require_kicad_python()
    root = tmp_path_factory.mktemp("probe_headers")
    pcb, netlist = _build(root, "headers", _headers_board())
    return _ok(pcb, netlist)


def _is_axis(v) -> bool:
    return v is not None and sorted(abs(c) for c in v) == [0, 1]


def _centre(box) -> tuple[float, float]:
    return (box[0] + box[2]) / 2, (box[1] + box[3]) / 2


def test_live_every_j_part_is_reported_with_its_interior_flag(g3):
    by_ref = g3["base"]["by_ref"]
    assert sorted(by_ref) == ["J1", "J2", "J9"]
    assert by_ref["J9"]["interior"] is True
    assert by_ref["J1"]["interior"] is False and by_ref["J2"]["interior"] is False


def test_live_board_bbox_is_the_outline_in_the_board_frame(g3):
    out = g3["base"]
    assert out["board_bbox"] == pytest.approx([0.0, 0.0, 54.0, 52.0], abs=1e-6)
    ox, oy = out["sheet_origin"]
    assert ox > 0 and oy > 0                     # the board sits on the A4 sheet


def test_live_j1_mouth_points_into_the_interior(g3):
    out = g3["base"]
    j1 = out["by_ref"]["J1"]
    assert _is_axis(j1["mouth"]), j1
    # swig_gate_checks.check_placement's test: mouth . (courtyard centre - board centre) <= 0
    (mx, my), (ccx, ccy) = j1["mouth"], _centre(j1["courtyard"])
    bcx, bcy = _centre(out["board_bbox"])
    assert mx * (ccx - bcx) + my * (ccy - bcy) <= 0, j1
    assert j1["mouth"] == [-1, 0]                # x = 45 of 54: it faces the board's middle


def test_live_j1_geometry_is_in_the_board_frame(g3):
    j1 = g3["base"]["by_ref"]["J1"]
    assert j1["side"] == "F" and sa.same_rot(j1["rot"], 270) and 0 <= j1["rot"] < 360
    # the courtyard cache is rebuilt after LoadBoard (empty on 9.0.4 until then)
    assert j1["courtyard_source"] == "courtyard"
    x0, y0, x1, y1 = j1["courtyard"]
    assert x0 < x1 and y0 < y1
    # D3: the netlist x/y is the courtyard-bbox centre in the board frame
    assert j1["centre"] == pytest.approx([45.0, 14.0], abs=TOL_MM)
    assert j1["centre"] == pytest.approx(list(_centre(j1["courtyard"])), abs=1e-6)
    assert x0 <= j1["origin"][0] <= x1 and y0 <= j1["origin"][1] <= y1
    assert j1["footprint"].endswith("USB_C_Receptacle_HRO_TYPE-C-31-M-12")


def test_live_rot_plus_90_turns_the_mouth_by_the_documented_convention(g3):
    base, turned = g3["base"]["by_ref"]["J1"], g3["turned"]["by_ref"]["J1"]
    assert sa.same_rot(turned["rot"], base["rot"] + 90), (base["rot"], turned["rot"])
    mx, my = base["mouth"]
    assert turned["mouth"] == [my, -mx]
    assert tuple(turned["mouth"]) == probe_mod.rotate_mouth(base["mouth"], 90)
    assert turned["centre"] == pytest.approx([45.0, 14.0], abs=TOL_MM)


def test_live_vertical_headers_have_no_mating_direction(g3):
    by_ref = g3["base"]["by_ref"]
    assert by_ref["J2"]["mouth"] is None and by_ref["J9"]["mouth"] is None
    assert by_ref["J2"]["pads"] == {"count": 6, "tht": True, "rows": 1, "pitch_mm": 2.54}
    assert by_ref["J9"]["pads"] == {"count": 40, "tht": True, "rows": 2, "pitch_mm": 2.54}
    assert by_ref["J1"]["pads"]["tht"] is False and by_ref["J1"]["pads"]["count"] >= 16


def test_live_stock_2x05_header_pads_summary(headers):
    j3 = headers["by_ref"]["J3"]
    assert j3["footprint"].endswith("PinHeader_2x05_P2.54mm_Vertical")
    assert sa.same_rot(j3["rot"], 90)
    assert j3["pads"] == {"count": 10, "tht": True, "rows": 2, "pitch_mm": 2.54}
    assert j3["mouth"] is None


def test_live_back_side_connectors_follow_the_same_convention(headers):
    j4, j5 = headers["by_ref"]["J4"], headers["by_ref"]["J5"]
    assert j4["side"] == "B" and j5["side"] == "B"
    assert j4["courtyard_source"] == "courtyard" and j5["courtyard_source"] == "courtyard"
    assert _is_axis(j4["mouth"]) and _is_axis(j5["mouth"]), (j4, j5)
    delta = j5["rot"] - j4["rot"]
    assert math.isclose(delta % 90, 0, abs_tol=1e-6) and not sa.same_rot(delta, 0)
    assert tuple(j5["mouth"]) == probe_mod.rotate_mouth(j4["mouth"], delta), (j4, j5)


def test_live_without_a_netlist_nothing_is_interior(g3):
    out = _ok(g3["pcb"])
    assert out["by_ref"]["J9"]["interior"] is False
    assert out["by_ref"]["J1"]["mouth"] == g3["base"]["by_ref"]["J1"]["mouth"]


def test_live_malformed_netlist_components_give_ok_false(g3, tmp_path):
    netlist = tmp_path / "bad.json"
    netlist.write_text(json.dumps({"components": [{"interior": True}]}), encoding="utf-8")
    rc, out, err = _probe(g3["pcb"], netlist)
    assert rc == 1 and out["ok"] is False, (out, err)
    assert "malformed" in out["reason"]


def test_live_missing_board_gives_ok_false(tmp_path):
    missing = tmp_path / "absent.kicad_pcb"
    rc, out, err = _probe(missing)
    assert rc == 1 and out["ok"] is False, (out, err)
    assert "board file not found" in out["reason"]


def test_live_unreadable_board_gives_ok_false_not_a_crash(tmp_path):
    bad = tmp_path / "garbage.kicad_pcb"
    bad.write_text("this is not a KiCad board\n", encoding="utf-8")
    rc, out, err = _probe(bad)
    assert rc == 1 and out["ok"] is False, (rc, out, err[-1500:])
    assert str(bad) in out["reason"]
