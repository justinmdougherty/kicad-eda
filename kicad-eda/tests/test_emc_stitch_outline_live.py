"""emc_stitch.py on polygon outlines (F3-15): a containment-filtered grid and a
perimeter fence on the outline inset, instead of the Edge.Cuts bounding box.

Unit tier (the dev venv: emc_stitch imports without pcbnew): the fence geometry
(every corner of the inset ring, evenly by arc length, at most one pitch apart,
exactly the fence distance from every edge), the rectangle test, the legacy
rectangle walk and the --fence-cutouts option.

Live tier (KiCad 9.0.4 locally, 10.0.5 in kicad-eda:wave1; skips without a KiCad
Python that imports pcbnew). Boards carry GND on In1.Cu AND B.Cu: a via tied to
one copper layer only is via_dangling.
  (a) the T10 board (rounded 60 x 40, a stadium slot and a round hole) stitched
      fence + grid: every via on the board with copper-edge clearance, kicad-cli
      DRC 0 errors, 0 via_dangling, 0 unconnected (HEAD's pass: 11
      copper_edge_clearance errors, 11 dangling vias, 11 unconnected);
  (b) --fence-cutouts also fences the slot and the hole, still DRC clean;
  (c) a 1.6 mm neck cannot be inset by the fence distance: the bbox fence is
      used, filtered by the outline, with a warning; DRC clean;
  (d) an open Edge.Cuts falls back to the bounding box with a warning;
  (e) a rectangular board gets the same vias (count and positions) as HEAD's
      emc_stitch.py (git show into tmp, when a git checkout is available) and as
      the golden recorded from it (the Docker run mounts no checkout).
"""
from __future__ import annotations

import collections
import hashlib
import json
import math
import re
import shutil
import subprocess
from pathlib import Path

import pytest

import emc_stitch as es
import outline as ol
import toolchain
from fixtures.swig_python import PREAMBLE, T10_BOARD, TOOLS, require_kicad_python, run_child

ROOT = TOOLS.parents[1]
SCRIPT = TOOLS / "emc_stitch.py"
ORIGIN = (100.5, 115.0)                  # sheet position of the board frame
KEEPOUT_MM = 0.5 + 0.3                   # default copper-edge clearance + via radius
FENCE_MM = 0.9                           # the fence inset: keep-out + 0.1 mm
ERROR_LINE = re.compile(r"(^|\s)Error:", re.M)
NECK_BOARD = {"outline": {"loop": [[0, 0], [20, 0], [20, 9.2], [26, 9.2], [26, 0], [46, 0],
                                   [46, 20], [26, 20], [26, 10.8], [20, 10.8], [20, 20],
                                   [0, 20]]}}
L_BOARD = {"outline": {"loop": [[0, 0], [30, 0], [30, 12], [14, 12], [14, 25], [0, 25]]}}
# HEAD's emc_stitch.py (6a8f94d) on the rectangle board of test (e), the same on pcbnew
# 9.0.4 and 10.0.5: vias, fence, interior and the sha256 of the sorted [x, y] nm
# positions (sheet - origin)
RECT_GOLDEN = {"vias_added": 133, "fence": 43, "interior": 90,
               "sha256": "3ef834d92ec3a44b7b88ce610128328556cb7bfeffb819dce5641a257e9eee26"}


# =============================================================================
# unit tier
# =============================================================================
def _ring(o: ol.Outline, k: int = 0) -> tuple:
    return o.rings[k]


def _cyclic_gaps(pts: list) -> list[float]:
    return [math.dist(pts[i - 1], pts[i]) for i in range(len(pts))]


def test_a_circle_ring_is_walked_evenly_by_arc_length():
    pts = es.fence_ring_points(_ring(ol.Outline(ol.Circle(10.0, 10.0, 5.0))), 3.0)
    assert len(pts) == math.ceil(2 * math.pi * 5.0 / 3.0) == 11
    assert all(math.dist(p, (10.0, 10.0)) == pytest.approx(5.0, abs=1e-9) for p in pts)
    gaps = _cyclic_gaps(pts)
    assert max(gaps) <= 3.0 and max(gaps) - min(gaps) < 1e-9


def test_every_corner_of_the_inset_ring_gets_a_fence_point():
    o = ol.Outline(ol.rect_loop(20.0, 10.0))
    pts = es.outline_fence_points(o, 1.0, 3.0)
    for corner in ((1.0, 1.0), (19.0, 1.0), (19.0, 9.0), (1.0, 9.0)):
        assert any(math.dist(p, corner) < 1e-9 for p in pts), corner
    # 18 mm sides in 6 steps of 3, 8 mm sides in 3 steps of 2.667
    assert len(pts) == 18 and max(_cyclic_gaps(pts)) <= 3.0 + 1e-9
    assert all(ol.distance_to_edge(o, p) == pytest.approx(1.0, abs=1e-9) for p in pts)


def test_t10_fence_points_lie_on_the_inset_outer_ring():
    o = ol.from_board_block(T10_BOARD)
    pts = es.outline_fence_points(o, FENCE_MM, 3.0)
    assert len(pts) > 50
    assert all(ol.distance_to_edge(o, p) == pytest.approx(FENCE_MM, abs=1e-6) for p in pts)
    assert all(ol.contains(o, p, KEEPOUT_MM + 0.01) for p in pts)
    assert max(_cyclic_gaps(pts)) <= 3.0 + 1e-9
    # every point is on the OUTER ring: none is 0.9 mm from a cutout only
    outer = ol.Outline(o.outer)
    assert all(ol.distance_to_edge(outer, p) == pytest.approx(FENCE_MM, abs=1e-6) for p in pts)


def test_fence_cutouts_adds_the_grown_cutout_rings():
    o = ol.from_board_block(T10_BOARD)
    outer_only = es.outline_fence_points(o, FENCE_MM, 3.0)
    both = es.outline_fence_points(o, FENCE_MM, 3.0, cutouts=True)
    extra = both[len(outer_only):]
    assert both[:len(outer_only)] == outer_only
    # the slot grows to a 7.8 x 11.8 stadium (8 + 2 pi 3.9 mm), the hole to r = 3.4
    assert len(extra) == math.ceil((8 + 2 * math.pi * 3.9) / 3.0) + math.ceil(
        2 * math.pi * 3.4 / 3.0)
    assert all(ol.distance_to_edge(o, p) == pytest.approx(FENCE_MM, abs=1e-6) for p in extra)
    assert all(ol.contains(o, p, KEEPOUT_MM) for p in extra)


def test_the_fence_does_not_depend_on_where_a_file_starts_a_ring():
    """KiCad's save order is not stable: the same board read back may start each
    ring at another vertex and list the cutouts in another order."""
    o = ol.from_board_block(T10_BOARD)
    outer, (slot, hole) = o.outer.vertices, o.cutouts
    turned = ol.Outline(ol.Loop(outer[3:] + outer[:3]),
                        (hole, ol.Loop(slot.vertices[2:] + slot.vertices[:2])))
    assert ol.area(turned) == pytest.approx(ol.area(o), abs=1e-9)
    got, want = (es.outline_fence_points(b, FENCE_MM, 3.0, cutouts=True) for b in (turned, o))
    assert len(got) == len(want) == 82
    assert [c for p in got for c in p] == pytest.approx([c for p in want for c in p], abs=1e-9)


def test_a_reflex_corner_is_fenced_round_and_at_most_a_pitch_apart():
    o = ol.from_board_block(L_BOARD)
    pts = es.outline_fence_points(o, FENCE_MM, 2.5)
    assert all(ol.distance_to_edge(o, p) == pytest.approx(FENCE_MM, abs=1e-6) for p in pts)
    assert max(_cyclic_gaps(pts)) <= 2.5 + 1e-9
    # the reflex corner (14, 12) is rounded (r = 0.9 about it): no point cuts it, and
    # the fence runs on along both arms next to it (y = 11.1 and x = 13.1)
    assert min(math.dist(p, (14.0, 12.0)) for p in pts) >= FENCE_MM - 1e-6
    assert any(abs(p[1] - 11.1) < 1e-6 and 14.0 < p[0] <= 14.0 + 2.5 for p in pts)
    assert any(abs(p[0] - 13.1) < 1e-6 and 12.0 < p[1] <= 12.0 + 2.5 for p in pts)


def test_a_neck_narrower_than_twice_the_fence_inset_cannot_be_fenced():
    with pytest.raises(ol.OutlineError, match="splits the board"):
        es.outline_fence_points(ol.from_board_block(NECK_BOARD), FENCE_MM, 3.0)


@pytest.mark.parametrize(("block", "rect"), [
    ({"w": 40, "h": 30}, True),
    ({"outline": {"loop": [[0, 0], [40, 0], [40, 30], [0, 30]]}}, True),
    ({"w": 40, "h": 30, "corner_radius": 2}, False),
    ({"w": 40, "h": 30, "cutouts": [{"circle": [20, 15, 2]}]}, False),
    ({"outline": {"loop": [[10, 0], [20, 10], [10, 20], [0, 10]]}}, False),
    (L_BOARD, False),
    (T10_BOARD, False),
])
def test_only_a_plain_axis_aligned_rectangle_takes_the_legacy_walk(block, rect):
    assert es.is_axis_rect(ol.from_board_block(block)) is rect


def test_the_legacy_rectangle_walk_keeps_its_order_and_points():
    assert es.rect_fence_points((0, 0, 10, 6), 3) == [
        (0, 0), (0, 6), (3, 0), (3, 6), (6, 0), (6, 6), (9, 0), (9, 6), (0, 3), (10, 3)]
    assert es.grid_points((0, 0, 10, 6), 3) == [(3, 3), (6, 3), (9, 3)]


def test_parse_args_takes_fence_cutouts_anywhere(tmp_path):
    pcb = tmp_path / "b.kicad_pcb"
    pcb.write_text("(kicad_pcb)\n", encoding="utf-8")
    plain = es.parse_args(["emc_stitch.py", str(pcb), "1e9", "fence", "GND2"])
    assert plain["fence_cutouts"] is False and plain["interior"] is False
    assert plain["max_freq_hz"] == 1e9 and plain["gnd_net"] == "GND2"
    flagged = es.parse_args(["emc_stitch.py", str(pcb), "--fence-cutouts", "0", "grid"])
    assert flagged["fence_cutouts"] is True and flagged["interior"] is True
    assert es.parse_args(["emc_stitch.py", str(pcb)])["fence_cutouts"] is False
    with pytest.raises(ValueError, match="--bogus"):
        es.parse_args(["emc_stitch.py", str(pcb), "--bogus"])


# =============================================================================
# live tier
# =============================================================================
BUILD = PREAMBLE + """
import board_outline as bo
import board_shapes
import outline as ol

spec, out = json.loads(sys.argv[1]), sys.argv[2]
M = 1_000_000
board = pcbnew.CreateEmptyBoard()
board.SetCopperLayerCount(4)

def seg(a, b, layer=pcbnew.Edge_Cuts, net=None, width=0.1):
    s = pcbnew.PCB_TRACK(board) if net else pcbnew.PCB_SHAPE(board)
    if not net:
        s.SetShape(pcbnew.SHAPE_T_SEGMENT)
    s.SetLayer(layer)
    s.SetWidth(pcbnew.FromMM(width))
    s.SetStart(pcbnew.VECTOR2I(pcbnew.FromMM(a[0]), pcbnew.FromMM(a[1])))
    s.SetEnd(pcbnew.VECTOR2I(pcbnew.FromMM(b[0]), pcbnew.FromMM(b[1])))
    if net:
        s.SetNet(net)
    board.Add(s)

gnd = pcbnew.NETINFO_ITEM(board, "GND")
board.Add(gnd)
if spec.get("open"):                         # a 40 x 30 rect at (100, 100), 0.1 mm gap
    x0, y0 = 100.0, 100.0
    pts = [(x0, y0), (x0 + 40, y0), (x0 + 40, y0 + 30), (x0, y0 + 30), (x0, y0 + 0.1)]
    for a, b in zip(pts[:-1], pts[1:]):
        seg(a, b)
    planes = [board_shapes.add_plane(board, board.GetLayerID(n), gnd, 40, 30, x0, y0)
              for n in ("In1.Cu", "B.Cu")]
else:
    if spec.get("rect"):
        board_shapes.add_outline(board, *spec["rect"])
    else:
        bo.draw_outline(board, ol.from_board_block(spec["block"]), *spec["origin"])
    planes = [board_shapes.add_plane_outline(board, board.GetLayerID(n), gnd)
              for n in ("In1.Cu", "B.Cu")]
if spec.get("obstacles"):                    # a +3V3 track and via the stitching must clear
    vcc = pcbnew.NETINFO_ITEM(board, "+3V3")
    board.Add(vcc)
    (a, b), v = spec["obstacles"]
    seg(a, b, pcbnew.F_Cu, vcc, 0.25)
    via = pcbnew.PCB_VIA(board)
    via.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(v[0]), pcbnew.FromMM(v[1])))
    via.SetWidth(pcbnew.FromMM(0.6))
    via.SetDrill(pcbnew.FromMM(0.3))
    via.SetNet(vcc)
    board.Add(via)
pcbnew.SaveBoard(out, board)
print(json.dumps({"version": pcbnew.Version(), "planes": len(planes)}))
"""

AUDIT = PREAMBLE + """
import hashlib
import board_outline as bo
import outline as ol

board, ox, oy = bo.load_board(sys.argv[1]), float(sys.argv[2]), float(sys.argv[3])
vias = [t for t in board.GetTracks() if t.GetClass() == "PCB_VIA" and t.GetNetname() == "GND"]
nm = sorted([v.GetPosition().x - bo.nm(ox), v.GetPosition().y - bo.nm(oy)] for v in vias)
res = {"version": pcbnew.Version(), "vias": len(vias),
       "sha256": hashlib.sha256(json.dumps(nm).encode()).hexdigest(), "positions": nm}
try:
    o, (bx, by) = bo.outline_from_board(board)
except bo.OutlineBuildError as e:
    res["outline_error"] = str(e)
else:
    pts = [(pcbnew.ToMM(v.GetPosition().x) - bx, pcbnew.ToMM(v.GetPosition().y) - by)
           for v in vias]
    res.update(off_board=sum(not ol.contains(o, p) for p in pts),
               min_edge=min((ol.distance_to_edge(o, p) for p in pts), default=None),
               edge_clearance=bo.edge_clearance_mm(board))
print(json.dumps(res))
"""

RUNPY = """
import runpy
import sys
sys.path.insert(0, sys.argv[1])
script = sys.argv[2]
sys.argv = [script] + sys.argv[3:]
runpy.run_path(script, run_name="__main__")
"""


def build(tmp_path: Path, name: str, spec: dict) -> Path:
    pcb = tmp_path / f"{name}.kicad_pcb"
    run_child(BUILD, [json.dumps(spec), pcb], tmp_path)
    return pcb


def stitch(pcb: Path, *args: str) -> dict:
    """The CURRENT emc_stitch.py as the pipeline runs it: its own entry point,
    stdin closed, UTF-8, a timeout, rc 0 and the JSON result line."""
    proc = subprocess.run([require_kicad_python(), str(SCRIPT), str(pcb), *args],
                          capture_output=True, text=True, encoding="utf-8", errors="replace",
                          stdin=subprocess.DEVNULL, timeout=300)
    assert proc.returncode == 0, f"rc {proc.returncode}\n{proc.stdout}\n{proc.stderr}"
    return toolchain.last_json(proc.stdout)


def audit(pcb: Path, origin: tuple[float, float]) -> dict:
    return run_child(AUDIT, [pcb, *origin], pcb.parent)


def drc(pcb: Path) -> dict:
    """kicad-cli DRC (the board's own cli, every severity): error-severity
    violations, via_dangling of any severity, unconnected items."""
    cli = toolchain.board_cli(str(pcb))
    assert cli, "no kicad-cli"
    rpt = pcb.with_suffix(".drc.json")
    proc = subprocess.run([cli, "pcb", "drc", str(pcb), "--format", "json", "--severity-all",
                           "-o", str(rpt)], capture_output=True, text=True, encoding="utf-8",
                          errors="replace", stdin=subprocess.DEVNULL, timeout=300)
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0 and not ERROR_LINE.search(out) and rpt.is_file(), out
    report = json.loads(rpt.read_text(encoding="utf-8"))
    viol = report.get("violations", [])
    return {"errors": sum(v.get("severity") == "error" for v in viol),
            "via_dangling": sum(v.get("type") == "via_dangling" for v in viol),
            "unconnected": len(report.get("unconnected_items", [])),
            "types": dict(collections.Counter(f"{v.get('type')}/{v.get('severity')}"
                                              for v in viol))}


def _on_ring(found: dict, o: ol.Outline, k: int) -> int:
    """How many audited vias sit FENCE_MM from ring k of `o` (0 = the outline)."""
    ring = ol.Outline(([o.outer, *o.cutouts])[k])
    return sum(abs(ol.distance_to_edge(ring, (x / 1e6, y / 1e6)) - FENCE_MM) < 1e-3
               for x, y in found["positions"])


def assert_clean(pcb: Path, res: dict) -> dict:
    found = drc(pcb)
    assert (found["errors"], found["via_dangling"], found["unconnected"]) == (0, 0, 0), \
        (found, res)
    return found


@pytest.fixture(scope="module")
def t10_board(tmp_path_factory: pytest.TempPathFactory) -> Path:
    require_kicad_python()
    work = tmp_path_factory.mktemp("t10_emc")
    return build(work, "t10", {"block": T10_BOARD, "origin": ORIGIN})


def test_a_t10_fence_and_grid_stay_on_the_board_and_drc_clean(t10_board, tmp_path):
    pcb = tmp_path / "t10_grid.kicad_pcb"
    shutil.copyfile(t10_board, pcb)
    res = stitch(pcb, "0", "grid")
    assert res["ok"] and res["outline"] == "polygon" and res["warnings"] == [], res
    assert res["fence"] > 50 and res["interior"] > 100, res
    found = audit(pcb, ORIGIN)
    assert found["vias"] == res["vias_added"] and found["off_board"] == 0, found
    assert found["min_edge"] >= KEEPOUT_MM, found
    assert_clean(pcb, res)


def test_b_fence_cutouts_fences_the_slot_and_the_hole(t10_board, tmp_path):
    plain, ringed = tmp_path / "plain.kicad_pcb", tmp_path / "ringed.kicad_pcb"
    shutil.copyfile(t10_board, plain)
    shutil.copyfile(t10_board, ringed)
    base = stitch(plain, "0", "fence")
    res = stitch(ringed, "0", "fence", "GND", "--fence-cutouts")
    assert res["ok"] and res["fence_cutouts"] is True and res["interior"] == 0, res
    found = audit(ringed, ORIGIN)
    assert found["off_board"] == 0 and found["min_edge"] >= KEEPOUT_MM, found
    o = ol.from_board_block(T10_BOARD)
    rings = [_on_ring(audit(plain, ORIGIN), o, k) for k in (1, 2)]
    assert rings == [0, 0], rings            # without the flag nothing rings a cutout
    slot, hole = (_on_ring(found, o, k) for k in (1, 2))
    # 11 + 8 ring points; the grown slot and hole are 1.7 mm apart, under the 2.1 mm
    # own-via spacing, so one of the facing points may be dropped
    assert slot >= 10 and hole >= 7 and slot + hole >= 18, (slot, hole)
    assert res["fence"] == base["fence"] + slot + hole, (base, res)
    assert_clean(ringed, res)


def test_c_a_neck_falls_back_to_the_filtered_bbox_fence(tmp_path):
    pcb = build(tmp_path, "neck", {"block": NECK_BOARD, "origin": ORIGIN})
    res = stitch(pcb, "0", "grid")
    assert res["ok"] and res["outline"] == "polygon", res
    assert any("bbox fence" in w and "splits the board" in w for w in res["warnings"]), res
    found = audit(pcb, ORIGIN)
    assert res["fence"] > 0 and found["off_board"] == 0, (res, found)
    assert found["min_edge"] >= KEEPOUT_MM, found
    assert_clean(pcb, res)


def test_d_an_open_edge_cuts_falls_back_to_the_bounding_box(tmp_path):
    pcb = build(tmp_path, "open", {"open": True})
    res = stitch(pcb, "0", "grid")
    assert res["ok"] and res["outline"] == "bbox" and res["vias_added"] > 0, res
    assert any("Edge.Cuts" in w and "bounding box" in w for w in res["warnings"]), res


def _head_script(tmp_path: Path) -> Path | None:
    """HEAD's emc_stitch.py (git show into tmp), or None without git / a checkout."""
    git = shutil.which("git")
    if not git:
        return None
    proc = subprocess.run([git, "-C", str(ROOT), "show", "HEAD:./server/tools/emc_stitch.py"],
                          capture_output=True, text=True, encoding="utf-8", errors="replace",
                          stdin=subprocess.DEVNULL, timeout=60)
    if proc.returncode != 0 or "def add_stitching" not in proc.stdout:
        return None
    out = tmp_path / "emc_stitch_head.py"
    out.write_text(proc.stdout, encoding="utf-8")
    return out


RECT = {"rect": [40.0, 30.0, 100.0, 100.0],
        "obstacles": [[[104.0, 103.0], [133.0, 126.0]], [121.0, 108.0]]}


def test_e_a_rectangle_board_is_stitched_exactly_as_before(tmp_path):
    pcb = build(tmp_path, "rect", RECT)
    new = tmp_path / "new.kicad_pcb"
    shutil.copyfile(pcb, new)
    res = stitch(new, "0", "grid")
    assert res["ok"] and res["outline"] == "rect" and res["warnings"] == [], res
    got = audit(new, (100.0, 100.0))
    counts = {k: res[k] for k in ("vias_added", "fence", "interior")}
    assert {**counts, "sha256": got["sha256"]} == RECT_GOLDEN, (counts, got["version"])
    head = _head_script(tmp_path)
    if head is not None:                     # the live HEAD, not only its golden
        old = tmp_path / "old.kicad_pcb"
        shutil.copyfile(pcb, old)
        before = run_child(RUNPY, [TOOLS, head, old, "0", "grid"], tmp_path)
        assert {k: before[k] for k in counts} == counts, (before, res)
        assert audit(old, (100.0, 100.0))["positions"] == got["positions"]
    assert hashlib.sha256(json.dumps(got["positions"]).encode()).hexdigest() == got["sha256"]
