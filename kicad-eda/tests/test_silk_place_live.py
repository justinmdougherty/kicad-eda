"""silk_place on the real pcbnew (KiCad 9.0.4 locally, 10.0.5 in kicad-eda:wave1).

Every check builds a board in a KiCad-python child (tests/fixtures/swig_python.py),
runs silk_place in a second child on the saved file (load_board, as the runnable
form does) and checks the reference texts here, in the venv, against the stdlib
outline model. Skips cleanly without a KiCad Python.

  (a) T10 outline (rounded corners, stadium slot, round hole) with a part next to
      the rounded corner, one under the slot, one under the hole and a connector
      label authored in the rounded corner: every final reference text bbox is on
      the material with the margin; the corner / slot / hole refs leave the
      bbox-legal "above" spot; the label walks in from the corner. The same board
      with the outline forced off (the legacy bbox rule) leaves those refs off
      the material, so the scenario really discriminates.
  (b) A rectangular lm358-style board: text positions and the legacy counters are
      identical to the silk_place.py of commit BASELINE_COMMIT (the last before
      F3-14), run from `git show` in tmp when the checkout has git (local) and
      frozen below as GOLDEN per pcbnew major (Docker mounts no .git; after the
      wave is committed HEAD would compare the new code with itself).
  (c) Edge.Cuts that do not close: the bbox rule plus the warning. The last
      resort (no step toward the part fits): centred on the body and shifted back
      into the legacy bbox (never past the legacy margin, even for a part hanging
      over the edge); when that is still off the material, the better of it and
      the bbox-clamped spot by the clearance kept; reported in warnings.
  (d) The runnable form: load_board (a missing file is a clean error, rc 1).

MARGIN keeps its legacy meaning: measured from KiCad's Edge.Cuts bbox, which
includes half the 0.1 mm edge stroke, so a text keeps MARGIN - 0.05 mm from the
edge line (FIT, less the 1 um slack the clamp's FromMM truncation needs). The
stroke estimate is capped by the Edge.Cuts line width (FENCE).

The baseline runs with fp.BuildCourtyardCaches() on every footprint first: on
9.0.4 LoadBoard leaves the caches EMPTY, so the old code silently used the
footprint bbox as the obstacle model (the courtyard-cache fix this task makes).
On 10.x the caches build lazily and the plain old run must match as well.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

import outline as ol
from fixtures.swig_python import (PREAMBLE, T10_BOARD, TOOLS, kicad_footprints,
                                  require_kicad_python, run_child)

MARGIN, EDGE_HALF_WIDTH, SLACK = 0.8, 0.05, 0.001
FIT = MARGIN - EDGE_HALF_WIDTH - SLACK
OX, OY = 100.5, 115.0                      # T10 sheet origin
RECT = {"w": 30.0, "h": 30.0, "origin": [100.0, 100.0]}
BASELINE_COMMIT = "6a8f94d"
REPO = Path(__file__).resolve().parents[1]
LEGACY_KEYS = ("adjacent", "centered", "clamped", "errors")
NOT_CLOSED = "silk: Edge.Cuts not closed, using bbox"
CANNOT = "ref text cannot sit 0.8 mm inside the outline near its part"

SOIC8 = "Package_SO:SOIC-8_3.9x4.9mm_P1.27mm"
C0603 = "Capacitor_SMD:C_0603_1608Metric"
R0603 = "Resistor_SMD:R_0603_1608Metric"
PIN1 = "Connector_PinHeader_2.54mm:PinHeader_1x01_P2.54mm_Vertical"

T10_PARTS = [
    {"ref": "C1", "fp": C0603, "at": [2.3, 3.0]},           # next to the r=4 corner
    {"ref": "R1", "fp": R0603, "at": [30.0, 15.0]},         # just under the stadium slot
    {"ref": "U1", "fp": SOIC8, "at": [30.0, 26.0]},         # just under the round hole
    {"ref": "C2", "fp": C0603, "at": [45.0, 20.0]},         # open field
    {"ref": "J1", "fp": "Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical",
     "at": [56.5, 32.0], "ref_at": [59.3, 39.4]},           # label in the r=4 corner
]
# Parts hanging over the T10 edge whose label no step toward the part fits:
CORNER_PARTS = [
    # bbox-clamped label grazes the bottom-right r=4 arc (~0.71 mm kept); centred and
    # clamped it would sit deeper in that corner, off the material: the clamped spot wins
    {"ref": "J2", "fp": PIN1, "at": [60.5, 38.0], "ref_at": [60.5, 35.9]},
    # over the top-left corner: every spot off the material; it stays in the bbox
    {"ref": "H2", "fp": "MountingHole:MountingHole_3.2mm_M3", "at": [0.8, 0.8],
     "ref_at": [0.8, 0.8]},
    # off the left edge, label authored up by the corner: centred on its body and
    # shifted back into the bbox, it lands on the material (straight edge)
    {"ref": "J5", "fp": PIN1, "at": [-2.0, 6.0], "ref_at": [-2.0, 2.5]},
    T10_PARTS[3],
]
RECT_PARTS = [
    {"ref": "U1", "fp": SOIC8, "at": [15.0, 15.0]},
    {"ref": "U2", "fp": "Package_TO_SOT_SMD:SOT-23-5", "at": [22.0, 10.0]},
    {"ref": "C1", "fp": C0603, "at": [15.0, 1.6]},          # top edge: "above" off the board
    {"ref": "C2", "fp": C0603, "at": [28.4, 8.0], "rot": 90},
    {"ref": "C3", "fp": C0603, "at": [22.0, 22.0]},         # boxed in by C4..C7: centred
    {"ref": "C4", "fp": C0603, "at": [22.0, 20.54]},
    {"ref": "C5", "fp": C0603, "at": [22.0, 23.46]},
    {"ref": "C6", "fp": C0603, "at": [24.96, 22.0]},
    {"ref": "C7", "fp": C0603, "at": [19.04, 22.0]},
    {"ref": "R1", "fp": R0603, "at": [8.0, 28.6]},
    {"ref": "R2", "fp": R0603, "at": [8.0, 20.0], "side": "B"},
    {"ref": "J1", "fp": "Connector_PinHeader_2.54mm:PinHeader_1x06_P2.54mm_Vertical",
     "at": [2.0, 10.0], "ref_at": [0.3, 7.6]},              # label off the left edge
]

# Text positions (sheet nm) and legacy counters of the BASELINE_COMMIT silk_place on
# the RECT board, captured with the PLACE child below in mode "head" on KiCad 9.0.4
# (local) and 10.0.5 (kicad-eda:wave1): the same on both, and on 10.0.5 also the same
# as "head_plain" (lazy caches). J1's 100.749999 mm left edge is the clamp's FromMM
# truncation (1 nm short of the margin), which the outline test must tolerate.
_RECT_BASELINE = {
    "pos": {"C1": [115000000, 103127550], "C2": [128400000, 105722450],
            "C3": [122000000, 122000000], "C4": [122000000, 119012450],
            "C5": [122000000, 124987550], "C6": [124960000, 120472450],
            "C7": [119040000, 120472450], "J1": [101732142, 107600000],
            "R1": [108000000, 127072450], "R2": [108000000, 118472450],
            "U1": [115000000, 111502450], "U2": [122000000, 107502450]},
    "stats": {"adjacent": 10, "centered": 1, "clamped": 1, "errors": []}}
GOLDEN: dict[int, dict] = {9: _RECT_BASELINE, 10: _RECT_BASELINE}

BUILD = PREAMBLE + """
from pathlib import Path

import board_outline as bo
import board_shapes
import outline as ol

spec, out = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8")), sys.argv[2]
ox, oy = spec["origin"]
board = pcbnew.CreateEmptyBoard()


def at(x, y):
    return pcbnew.VECTOR2I(bo.nm(x + ox), bo.nm(y + oy))


def seg(a, b):
    s = pcbnew.PCB_SHAPE(board)
    s.SetShape(pcbnew.SHAPE_T_SEGMENT)
    s.SetLayer(pcbnew.Edge_Cuts)
    s.SetWidth(bo.nm(0.1))
    s.SetStart(at(*a))
    s.SetEnd(at(*b))
    board.Add(s)


if spec["outline"] == "rect":
    board_shapes.add_outline(board, spec["w"], spec["h"], ox, oy)
elif spec["outline"] == "loop":
    bo.draw_outline(board, ol.from_board_block(spec["board"]), ox, oy)
else:                                   # "open": the last edge stops 0.1 mm short
    w, h = spec["w"], spec["h"]
    pts = [(0, 0), (w, 0), (w, h), (0, h), (0, 0.1)]
    for a, b in zip(pts[:-1], pts[1:]):
        seg(a, b)
for p in spec["parts"]:
    lib, name = p["fp"].split(":")
    fp = pcbnew.FootprintLoad(str(Path(spec["lib"]) / (lib + ".pretty")), name)
    if fp is None:
        raise SystemExit("FootprintLoad returned None for " + p["fp"])
    fp.SetReference(p["ref"])
    fp.SetPosition(at(*p["at"]))
    fp.SetOrientationDegrees(p.get("rot", 0))
    board.Add(fp)
    if p.get("side") == "B":
        fp.Flip(fp.GetPosition(), getattr(pcbnew, "FLIP_DIRECTION_TOP_BOTTOM", True))
    if "ref_at" in p:
        fp.Reference().SetPosition(at(*p["ref_at"]))
pcbnew.SaveBoard(out, board)
print(json.dumps({"version": pcbnew.Version(), "parts": len(spec["parts"])}))
"""

PLACE = PREAMBLE + """
import importlib

import board_outline as bo

mode, path = sys.argv[1], sys.argv[2]
if mode.startswith("head"):             # the baseline module, written to tmp
    sys.path.insert(0, sys.argv[3])
    sp = importlib.import_module("silk_place_head")
else:
    import silk_place as sp
board = bo.load_board(path)
fps = sorted(board.GetFootprints(), key=lambda f: f.GetReference())


def side(f):
    return "B" if f.GetLayer() == pcbnew.B_Cu else "F"


def crtyd(f):
    return pcbnew.B_CrtYd if side(f) == "B" else pcbnew.F_CrtYd


def box(b):
    return [pcbnew.ToMM(v) for v in (b.GetLeft(), b.GetTop(), b.GetRight(), b.GetBottom())]


raw = sum(1 for f in fps if f.GetCourtyard(crtyd(f)).OutlineCount() > 0)
if mode == "head":                      # the caches the old code never built
    for f in fps:
        f.BuildCourtyardCaches()
if mode == "bbox":                      # the legacy rule: no usable outline
    def no_outline(*_a, **_k):
        raise bo.OutlineBuildError("forced off by the test")
    bo.outline_from_board = no_outline
stats = sp.place_refs(board) if mode != "dump" else {}
refs = {}
for f in fps:
    t, cy = f.Reference(), bo.courtyard(f, crtyd(f))
    refs[f.GetReference()] = {
        "pos": [t.GetPosition().x, t.GetPosition().y], "text": box(t.GetBoundingBox()),
        "courtyard": box(cy.BBox()) if cy.OutlineCount() else None, "side": side(f)}
print(json.dumps({"version": pcbnew.Version(), "raw_courtyards": raw, "stats": stats,
                  "edges": box(board.GetBoardEdgesBoundingBox()), "refs": refs}))
"""

FENCE = PREAMBLE + """
import board_outline as bo
import silk_place as sp

board, grow = bo.load_board(sys.argv[1]), pcbnew.FromMM(float(sys.argv[2]))


class Grown:
    \"\"\"The board, its KiCad Edge.Cuts bbox reaching `grow` past the outline.\"\"\"
    def __init__(self, b):
        self._b = b

    def __getattr__(self, name):
        return getattr(self._b, name)

    def GetBoardEdgesBoundingBox(self):
        bb = self._b.GetBoardEdgesBoundingBox()
        bb.Inflate(grow)
        return bb


fence, warnings = sp.make_fence(Grown(board))
print(json.dumps({"box": list(fence.box), "clearance": fence.clearance, "warnings": warnings}))
"""


# --- helpers --------------------------------------------------------------------------------
def _build(spec: dict, work: Path, name: str) -> Path:
    kpy = require_kicad_python()
    lib = kicad_footprints(kpy, "Package_SO.pretty").parent
    spec_path, pcb = work / f"{name}.json", work / f"{name}.kicad_pcb"
    spec_path.write_text(json.dumps({**spec, "lib": str(lib)}), encoding="utf-8")
    res = run_child(BUILD, [spec_path, pcb], work)
    assert res["parts"] == len(spec["parts"])
    return pcb


def _place(mode: str, pcb: Path, work: Path, head_dir: Path | None = None) -> dict:
    """Run PLACE on a fresh copy of `pcb` (every mode starts from the same file)."""
    copy = work / f"{pcb.stem}_{mode}.kicad_pcb"
    shutil.copyfile(pcb, copy)
    return run_child(PLACE, [mode, copy] + ([head_dir] if head_dir else []), work)


def _head_dir(work: Path) -> Path | None:
    """BASELINE_COMMIT's silk_place.py as tmp/head/silk_place_head.py, or None
    when this checkout has no git history (the Docker mount)."""
    if shutil.which("git") is None:
        return None
    proc = subprocess.run(["git", "-C", str(REPO), "show",
                           f"{BASELINE_COMMIT}:kicad-eda/server/tools/silk_place.py"],
                          capture_output=True, text=True, encoding="utf-8", errors="replace",
                          stdin=subprocess.DEVNULL, timeout=60)
    if proc.returncode != 0:
        return None
    head = work / "head"
    head.mkdir(exist_ok=True)
    (head / "silk_place_head.py").write_text(proc.stdout, encoding="utf-8")
    return head


def _frame(text: list[float], origin: tuple[float, float]) -> tuple[float, ...]:
    ox, oy = origin
    return (text[0] - ox, text[1] - oy, text[2] - ox, text[3] - oy)


def _positions(res: dict) -> dict[str, list[int]]:
    return {ref: r["pos"] for ref, r in sorted(res["refs"].items())}


def _major(res: dict) -> int:
    return int(res["version"].split(".")[0])


def _overlaps(a: list[float], b: list[float]) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _is_chip(ref: str) -> bool:
    return ref[:1] not in ("J", "H") and not ref.startswith("TP")


def _centred(r: dict) -> bool:
    t, c = r["text"], r["courtyard"]
    return abs((t[0] + t[2]) - (c[0] + c[2])) < 2e-3 and abs((t[1] + t[3]) - (c[1] + c[3])) < 2e-3


def _in_legacy_box(text: list[float], edges: list[float], tol: float = 1e-5) -> bool:
    """MARGIN inside KiCad's Edge.Cuts bbox: the old rule (tol: FromMM truncation)."""
    return (text[0] >= edges[0] + MARGIN - tol and text[1] >= edges[1] + MARGIN - tol
            and text[2] <= edges[2] - MARGIN + tol and text[3] <= edges[3] - MARGIN + tol)


def _assert_clear_of_courtyards(res: dict) -> None:
    """Every adjacent (not centred) chip ref is clear of the other courtyards on
    its side: the obstacle model is the real courtyard after load_board."""
    refs = res["refs"]
    for ref, r in refs.items():
        if not _is_chip(ref) or _centred(r):
            continue
        for other, o in refs.items():
            if other != ref and o["side"] == r["side"] and o["courtyard"]:
                assert not _overlaps(r["text"], o["courtyard"]), (ref, other, r, o)


# --- (a) outline-aware candidates and clamp -------------------------------------------------
@pytest.fixture(scope="module")
def t10_board(tmp_path_factory: pytest.TempPathFactory) -> Path:
    work = tmp_path_factory.mktemp("silk_t10")
    spec = {"outline": "loop", "board": T10_BOARD, "origin": [OX, OY], "parts": T10_PARTS}
    return _build(spec, work, "t10")


def test_a_t10_every_ref_lies_on_the_material(t10_board: Path, tmp_path: Path) -> None:
    res = _place("new", t10_board, tmp_path)
    o, refs, stats = ol.from_board_block(T10_BOARD), res["refs"], res["stats"]
    for ref, r in refs.items():
        assert ol.contains_rect(o, _frame(r["text"], (OX, OY)), clearance=FIT), (ref, r)
        assert _in_legacy_box(r["text"], res["edges"]), (ref, r, res["edges"])
    assert stats["containment"] == "outline"
    assert stats["errors"] == [] and stats["warnings"] == []
    assert stats["adjacent"] == 4 and stats["centered"] == 0
    # the bbox-legal "above" spot is refused next to the corner, the slot and the hole
    for ref in ("C1", "R1", "U1"):
        r = refs[ref]
        assert r["text"][3] > r["courtyard"][1], (ref, r)
    # the connector label authored in the rounded corner walks in toward its part
    assert stats["stepped"] == 1 and stats["clamped"] >= 1
    assert stats["body_fallback"] == 0 and stats["off_outline"] == 0
    assert not _centred(refs["J1"]), refs["J1"]
    _assert_clear_of_courtyards(res)


def test_a_bbox_rule_alone_leaves_refs_off_the_material(t10_board: Path, tmp_path: Path) -> None:
    res = _place("bbox", t10_board, tmp_path)
    o, refs, stats = ol.from_board_block(T10_BOARD), res["refs"], res["stats"]
    assert stats["containment"] == "bbox"
    assert stats["warnings"] == [f"{NOT_CLOSED} (forced off by the test)"]
    off = sorted(ref for ref, r in refs.items()
                 if not ol.contains_rect(o, _frame(r["text"], (OX, OY)), clearance=FIT))
    assert off == ["C1", "J1", "R1", "U1"], off
    head = _head_dir(tmp_path)
    if head is not None:        # the bbox fallback IS the old behaviour (with courtyards)
        assert _positions(res) == _positions(_place("head", t10_board, tmp_path, head))


# --- (b) rectangular boards: identical to the baseline --------------------------------------
@pytest.fixture(scope="module")
def rect_board(tmp_path_factory: pytest.TempPathFactory) -> Path:
    work = tmp_path_factory.mktemp("silk_rect")
    return _build({"outline": "rect", **RECT, "parts": RECT_PARTS}, work, "rect")


def test_b_rect_board_positions_identical_to_baseline(rect_board: Path, tmp_path: Path) -> None:
    new = _place("new", rect_board, tmp_path)
    major = _major(new)
    assert major in GOLDEN, f"no baseline golden for pcbnew {new['version']}"
    golden = GOLDEN[major]
    assert _positions(new) == golden["pos"]
    assert {k: new["stats"][k] for k in LEGACY_KEYS} == golden["stats"]
    # the board exercises the edge rule, the edge clamp and the dense fallback
    assert golden["stats"]["clamped"] >= 1 and golden["stats"]["centered"] >= 1
    assert new["stats"]["containment"] == "outline" and new["stats"]["warnings"] == []
    assert new["stats"]["stepped"] == 0 and new["stats"]["body_fallback"] == 0
    assert new["stats"]["off_outline"] == 0
    _assert_clear_of_courtyards(new)
    # the legacy bbox rule (outline forced off) gives the same result
    assert _positions(_place("bbox", rect_board, tmp_path)) == golden["pos"]
    head = _head_dir(tmp_path)
    if head is not None:        # local checkout: the live baseline still agrees
        assert _positions(_place("head", rect_board, tmp_path, head)) == golden["pos"]
        plain = _place("head_plain", rect_board, tmp_path, head)
        if plain["raw_courtyards"] == len(RECT_PARTS):      # 10.x: lazy caches
            assert _positions(plain) == golden["pos"]


def test_b_courtyards_are_rebuilt_after_load_board(rect_board: Path, tmp_path: Path) -> None:
    """The obstacle model is the courtyard even where LoadBoard left the cache
    empty (9.0.4): no 'courtyard unavailable' error, every adjacent ref clear."""
    res = _place("new", rect_board, tmp_path)
    assert all(r["courtyard"] for r in res["refs"].values())
    assert not any("courtyard" in e for e in res["stats"]["errors"])
    assert res["raw_courtyards"] in (0, len(RECT_PARTS))
    _assert_clear_of_courtyards(res)


# --- (c) open Edge.Cuts, the clamp's last resort, the stroke estimate -----------------------
def test_c_open_edge_cuts_fall_back_to_the_bbox_with_a_warning(tmp_path: Path) -> None:
    spec = {"outline": "open", **RECT, "parts": RECT_PARTS[:4]}
    res = _place("new", _build(spec, tmp_path, "open"), tmp_path)
    stats = res["stats"]
    assert stats["containment"] == "bbox"
    assert len(stats["warnings"]) == 1 and stats["warnings"][0].startswith(NOT_CLOSED + " (")
    assert stats["adjacent"] == 4 and stats["errors"] == []


def test_c_a_label_that_cannot_reach_the_material_is_centred_and_reported(tmp_path: Path) -> None:
    """H1 sits over the round cutout with its label there too: no step toward
    its centre fits, so the label is centred on its body and named."""
    parts = [{"ref": "H1", "fp": "MountingHole:MountingHole_3.2mm_M3", "at": [30.0, 20.0],
              "ref_at": [30.0, 20.0]}, T10_PARTS[3]]
    spec = {"outline": "loop", "board": T10_BOARD, "origin": [OX, OY], "parts": parts}
    res = _place("new", _build(spec, tmp_path, "fallback"), tmp_path)
    stats, h1 = res["stats"], res["refs"]["H1"]
    assert stats["containment"] == "outline" and stats["errors"] == []
    assert stats["body_fallback"] == 1 and stats["off_outline"] == 1
    assert stats["stepped"] == 0 and stats["adjacent"] == 1
    assert stats["warnings"] == [f"H1: {CANNOT}; centred on its body, off the board material"]
    assert _centred(h1), h1


def test_c_the_last_resort_never_passes_the_legacy_margin(tmp_path: Path) -> None:
    """CORNER_PARTS: every final text stays MARGIN inside KiCad's edge bbox (the
    old rule) although each part hangs over the edge; J5 lands on the material
    centred on its body, H2 cannot reach it, and J2 keeps the legacy spot because
    that clears the outline more than its centred one."""
    spec = {"outline": "loop", "board": T10_BOARD, "origin": [OX, OY], "parts": CORNER_PARTS}
    pcb = _build(spec, tmp_path, "corners")
    res, legacy = _place("new", pcb, tmp_path), _place("bbox", pcb, tmp_path)
    o, refs, stats = ol.from_board_block(T10_BOARD), res["refs"], res["stats"]
    assert stats["containment"] == "outline" and stats["errors"] == []
    assert stats["adjacent"] == 1 and stats["stepped"] == 0
    assert stats["body_fallback"] == 2 and stats["off_outline"] == 2
    for ref, r in refs.items():
        assert _in_legacy_box(r["text"], res["edges"]), (ref, r, res["edges"])
    j5, h2, j2 = (_frame(refs[ref]["text"], (OX, OY)) for ref in ("J5", "H2", "J2"))
    assert ol.contains_rect(o, j5, clearance=FIT), refs["J5"]
    t, c = refs["J5"]["text"], refs["J5"]["courtyard"]
    assert abs((t[1] + t[3]) - (c[1] + c[3])) < 2e-3, refs["J5"]       # centred (in y)
    assert not ol.contains_rect(o, h2), refs["H2"]
    assert refs["J2"]["pos"] == legacy["refs"]["J2"]["pos"]
    assert ol.contains_rect(o, j2) and not ol.contains_rect(o, j2, clearance=FIT), refs["J2"]
    warn = {w.split(":")[0]: w for w in stats["warnings"]}
    assert sorted(warn) == ["H2", "J2", "J5"], stats["warnings"]
    assert warn["J5"] == f"J5: {CANNOT}; centred on its body"
    assert warn["H2"] == f"H2: {CANNOT}; centred on its body, off the board material"
    m = re.fullmatch(re.escape(f"J2: {CANNOT}; left at its edge-clamped spot, clearing the "
                               "edge by only ") + r"(\d+\.\d{3}) mm", warn["J2"])
    assert m and 0.0 < float(m.group(1)) < FIT, warn["J2"]


def test_c_the_stroke_estimate_is_capped_by_the_edge_line_width(t10_board: Path,
                                                                 tmp_path: Path) -> None:
    """KiCad's Edge.Cuts bbox reaching 0.5 mm past the outline (more than the
    0.05 mm line half-width) widens the legacy box but must not eat the outline
    clearance: it stays MARGIN less the half-width (and the slack)."""
    plain = run_child(FENCE, [t10_board, "0"], tmp_path)
    grown = run_child(FENCE, [t10_board, "0.5"], tmp_path)
    assert plain["warnings"] == [] and grown["warnings"] == []
    assert plain["clearance"] == pytest.approx(FIT, abs=1e-9)
    assert grown["clearance"] == pytest.approx(FIT, abs=1e-9)
    assert grown["box"] == pytest.approx([plain["box"][0] - 0.5, plain["box"][1] - 0.5,
                                          plain["box"][2] + 0.5, plain["box"][3] + 0.5])


# --- (d) the runnable form ------------------------------------------------------------------
def _run_script(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run([require_kicad_python(), str(TOOLS / "silk_place.py"), *args],
                          capture_output=True, text=True, encoding="utf-8", errors="replace",
                          stdin=subprocess.DEVNULL, timeout=300)


def test_d_runnable_form_places_and_saves(t10_board: Path, tmp_path: Path) -> None:
    pcb = tmp_path / "cli.kicad_pcb"
    shutil.copyfile(t10_board, pcb)
    proc = _run_script([str(pcb)])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    stats = json.loads(proc.stdout.strip().splitlines()[-1])
    assert stats["containment"] == "outline" and stats["stepped"] == 1
    saved = run_child(PLACE, ["dump", pcb], tmp_path)
    o = ol.from_board_block(T10_BOARD)
    for ref, r in saved["refs"].items():
        assert ol.contains_rect(o, _frame(r["text"], (OX, OY)), clearance=FIT), (ref, r)


def test_d_runnable_form_refuses_a_missing_board(tmp_path: Path) -> None:
    proc = _run_script([str(tmp_path / "missing.kicad_pcb")])
    assert proc.returncode == 1, proc.stdout + proc.stderr
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["ok"] is False and "board file not found" in out["errors"][0]
