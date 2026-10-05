"""board_outline — the pcbnew 9/10 outline adapter, unit tier (no pcbnew).

The pure helpers (records -> primitives, the nm snap cache, the area
tolerance, the GetBoardPolygonOutlines keyword probe) are tested directly.
The pcbnew-facing helpers are tested against a small fake pcbnew module
patched onto ``board_outline.pcbnew``, which pins down the call contract the
live tier (tests/test_board_outline_live.py, KiCad 9.0.4 + 10.0.5) proves on
the real thing: never a positional second GetBoardPolygonOutlines argument,
success judged only by the returned bool, zones filled in place (never
SetOutline), the 9.x / 10.x pour-flag names, and load_board refusing a
missing path before pcbnew is touched.
"""
from __future__ import annotations

import ast
import math
import re
import subprocess
import sys
import types
from pathlib import Path

import pytest

import board_outline as bo
import outline as ol
from fixtures import swig_python

TOOLS = Path(__file__).resolve().parents[1] / "server" / "tools"

# The T10 outline spike board (C:/Development/Code/kicad-eda-wave2-plan/scratch/wave1/
# T10-outlinespike/outline_A.json): 60 x 40 with r=4 corners, a stadium slot, a hole.
T10 = {"outline": {"loop": [[4, 0], [56, 0, 0.41421356237309503], [60, 4],
                            [60, 36, 0.41421356237309503], [56, 40],
                            [4, 40, 0.41421356237309503], [0, 36],
                            [0, 4, 0.41421356237309503]]},
       "cutouts": [{"loop": [[27, 7], [27, 11, -1], [33, 11], [33, 7, -1]]},
                   {"circle": [30, 20, 2.5]}],
       "datum": [10, 30]}
T10_AREA = 2400 - 4 * (16 - 4 * math.pi) - (24 + 9 * math.pi) - 6.25 * math.pi


def test_t10_literal_is_the_shared_fixture_and_has_the_spike_area():
    assert T10 == swig_python.T10_BOARD
    assert ol.area(ol.from_board_block(T10)) == pytest.approx(T10_AREA, abs=1e-9)
    assert T10_AREA == pytest.approx(2314.36, abs=0.005)


# --- import without pcbnew ------------------------------------------------------------
def test_module_imports_without_pcbnew():
    code = ("import sys; sys.modules['pcbnew'] = None; "
            f"sys.path[:0] = [{str(TOOLS)!r}]; import board_outline as bo; "
            "assert bo.pcbnew is None; print('imported')")
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL,
                          timeout=60)
    assert proc.returncode == 0 and "imported" in proc.stdout, proc.stdout + proc.stderr


def test_pcbnew_helpers_say_pcbnew_is_missing(monkeypatch):
    monkeypatch.setattr(bo, "pcbnew", None)
    with pytest.raises(RuntimeError, match="pcbnew is not importable"):
        bo.polygon_outlines(object())


# --- records -> primitives (pure) -------------------------------------------------------
def _chain_area(records: list[dict]) -> float:
    return ol.area(ol.from_kicad_primitives(bo.shape_records_to_prims(records)))


def test_segment_arc_circle_records_map_one_to_one():
    prims = bo.shape_records_to_prims([
        {"shape": "segment", "start": [0, 0], "end": [10, 0]},
        {"shape": "arc", "start": [10, 0], "mid": [12, 2], "end": [10, 4]},
        {"shape": "circle", "center": [5, 5], "radius": 1.5}])
    assert [p["kind"] for p in prims] == ["line", "arc", "circle"]
    assert prims[0]["start"] == (0.0, 0.0) and prims[0]["end"] == (10.0, 0.0)
    assert prims[1]["mid"] == (12.0, 2.0)
    assert prims[2]["center"] == (5.0, 5.0) and prims[2]["radius"] == 1.5


def test_rect_record_is_four_lines_in_either_corner_order():
    for start, end in (([2, 3], [12, 8]), ([12, 8], [2, 3])):
        prims = bo.shape_records_to_prims([{"shape": "rect", "start": start, "end": end}])
        assert [p["kind"] for p in prims] == ["line"] * 4
        assert _chain_area([{"shape": "rect", "start": start, "end": end}]) == \
            pytest.approx(50.0)


def test_rounded_rect_record_is_four_lines_and_four_arcs():
    rec = {"shape": "rect", "start": [0, 0], "end": [20, 10], "corner_radius": 2.0}
    prims = bo.shape_records_to_prims([rec])
    assert sorted(p["kind"] for p in prims) == ["arc"] * 4 + ["line"] * 4
    assert _chain_area([rec]) == pytest.approx(200 - (4 - math.pi) * 4, abs=1e-9)


def test_rect_corner_radius_is_clamped_to_half_the_short_side():
    rec = {"shape": "rect", "start": [0, 0], "end": [20, 10], "corner_radius": 99}
    assert _chain_area([rec]) == pytest.approx(10 * 10 + math.pi * 25, abs=1e-9)


def test_poly_record_is_closed_lines_and_drops_a_repeated_last_point():
    open_ring = {"shape": "poly", "points": [[0, 0], [10, 0], [10, 5], [0, 5]]}
    closed = {"shape": "poly", "points": [[0, 0], [10, 0], [10, 5], [0, 5], [0, 0]]}
    for rec in (open_ring, closed):
        prims = bo.shape_records_to_prims([rec])
        assert [p["kind"] for p in prims] == ["line"] * 4
        assert prims[-1]["end"] == (0.0, 0.0)
    assert _chain_area([open_ring]) == pytest.approx(50.0)


def test_bezier_record_is_an_open_polyline_through_its_points():
    pts = [[0, 0], [1, 0.2], [2, 0.8], [3, 2]]
    prims = bo.shape_records_to_prims([{"shape": "bezier", "points": pts}])
    assert [(p["start"], p["end"]) for p in prims] == \
        [((0.0, 0.0), (1.0, 0.2)), ((1.0, 0.2), (2.0, 0.8)), ((2.0, 0.8), (3.0, 2.0))]


def test_zero_length_segments_are_dropped():
    prims = bo.shape_records_to_prims([{"shape": "segment", "start": [1, 1], "end": [1, 1]},
                                       {"shape": "bezier", "points": [[0, 0], [0, 0], [1, 1]]}])
    assert [(p["start"], p["end"]) for p in prims] == [((0.0, 0.0), (1.0, 1.0))]


@pytest.mark.parametrize("rec, match", [
    ({"shape": "spline"}, "shape"),
    ({"start": [0, 0]}, "shape"),
    ("segment", "shape"),
    ({"shape": "segment", "start": [0, 0]}, "end"),
    ({"shape": "circle", "center": [0, 0], "radius": "big"}, "radius"),
    ({"shape": "rect", "start": [0, 0], "end": [0, 5]}, "zero"),
    ({"shape": "poly", "points": [[0, 0], [1, 1]]}, "3 points"),
    ({"shape": "bezier", "points": [[0, 0]]}, "2 points"),
])
def test_bad_records_raise_outline_error(rec, match):
    with pytest.raises(ol.OutlineError, match=match):
        bo.shape_records_to_prims([rec])


def _prims_to_records(prims: list[dict]) -> list[dict]:
    kinds = {"line": "segment", "arc": "arc", "circle": "circle"}
    return [{"shape": kinds[p["kind"]], **{k: v for k, v in p.items()
                                          if k not in ("kind", "ring") and v is not None}}
            for p in prims]


def test_t10_round_trips_through_primitives_and_records():
    model = ol.from_board_block(T10)
    records = _prims_to_records(ol.to_kicad_primitives(model))
    assert [r["shape"] for r in records].count("arc") == 6
    back = ol.from_kicad_primitives(bo.shape_records_to_prims(records), datum=model.datum)
    swig_python.assert_same_outline(back, model, 1e-6)


def test_assert_same_outline_catches_a_moved_vertex():
    model = ol.from_board_block(T10)
    moved = ol.translate(model, 2e-6, 0)
    swig_python.assert_same_outline(moved, model, 1e-5)
    with pytest.raises(AssertionError):
        swig_python.assert_same_outline(moved, model, 1e-6)


def test_assert_same_outline_ignores_the_start_vertex():
    model = ol.from_board_block(T10)
    v = model.outer.vertices
    rotated = ol.Outline(ol.Loop(v[3:] + v[:3]), model.cutouts, model.datum)
    swig_python.assert_same_outline(rotated, model, 1e-9)
    bent = ol.Outline(ol.Loop(v[:1] + ((v[1][0], v[1][1], 0.3),) + v[2:]), model.cutouts,
                      model.datum)
    with pytest.raises(AssertionError):
        swig_python.assert_same_outline(bent, model, 1e-3)


def test_assert_same_outline_matches_cutouts_in_any_order():
    model = ol.from_board_block(T10)
    swapped = ol.Outline(model.outer, model.cutouts[::-1], model.datum)
    swig_python.assert_same_outline(swapped, model, 1e-9)
    moved = ol.Outline(model.outer, (model.cutouts[0], ol.Circle(30, 21, 2.5)), model.datum)
    with pytest.raises(AssertionError):
        swig_python.assert_same_outline(moved, model, 1e-3)
    with pytest.raises(AssertionError):
        swig_python.assert_same_outline(ol.translate(model, 0, 0.01), model, 1e-3)


# --- nm snapping, area tolerance, keyword probe (pure) --------------------------------------
def test_nm_rounds_half_away_from_zero():
    assert bo.nm(104.5) == 104_500_000
    assert bo.nm(169.18499999999997) == 169_185_000          # FromMM would truncate
    assert bo.nm(-0.0000016) == -2 and bo.nm(0.0000016) == 2
    assert bo.nm(0.0000004) == 0 and bo.nm(-0.0000004) == 0


def test_snap_cache_returns_the_identical_object_for_one_nm_point():
    made = []

    def factory(x: int, y: int) -> list:
        made.append((x, y))
        return [x, y]

    snap = bo.make_snapper(factory)
    a = snap(104.5, 115.0)
    assert snap(104.5 + 1e-10, 115.0 - 1e-10) is a
    assert snap(104.500001, 115.0) is not a
    assert made == [(104_500_000, 115_000_000), (104_500_001, 115_000_000)]


def test_snappers_do_not_share_their_cache():
    s1, s2 = bo.make_snapper(lambda x, y: [x, y]), bo.make_snapper(lambda x, y: [x, y])
    assert s1(1, 2) is not s2(1, 2)


def test_area_tolerance_is_absolute_plus_relative():
    assert bo.area_matches(2314.36, 2314.36 + 0.05 + 2.31)
    assert not bo.area_matches(2314.36, 2314.36 + 0.05 + 2.32)
    assert bo.area_matches(1.0, 1.05) and not bo.area_matches(1.0, 1.052)


DOC_9 = ("GetBoardPolygonOutlines(BOARD self, SHAPE_POLY_SET aOutlines, OUTLINE_ERROR_HANDLER "
         "* aErrorHandler=None, bool aAllowUseArcsInPolygons=False, bool "
         "aIncludeNPTHAsOutlines=False) -> bool")
DOC_10 = ("GetBoardPolygonOutlines(BOARD self, SHAPE_POLY_SET aOutlines, bool "
          "aInferOutlineIfNecessary, OUTLINE_ERROR_HANDLER * aErrorHandler=None, bool "
          "aAllowUseArcsInPolygons=False, bool aIncludeNPTHAsOutlines=False) -> bool")


def test_infer_keyword_probe_reads_the_swig_docstring():
    assert bo.infer_keyword(DOC_10) and not bo.infer_keyword(DOC_9)
    assert not bo.infer_keyword(None)


# --- a fake pcbnew ------------------------------------------------------------------------
class Vec:
    def __init__(self, x: int, y: int) -> None:
        self.x, self.y = x, y


class Chain:
    def __init__(self, area_nm2: float) -> None:
        self.area_nm2 = area_nm2


class PolySet:
    """SHAPE_POLY_SET: outlines as (Chain, [Chain holes]); Area() = outer - holes."""

    def __init__(self, src: PolySet | None = None) -> None:
        self.outlines = [(o, list(h)) for o, h in (src.outlines if src else [])]
        self.bbox_area = src.bbox_area if src else 0.0
        self.deflated: tuple | None = None
        self.after_deflate: list | None = src.after_deflate if src else None

    def Area(self) -> float:
        if not self.outlines:
            return self.bbox_area
        return sum(o.area_nm2 - sum(h.area_nm2 for h in hs) for o, hs in self.outlines)

    def OutlineCount(self) -> int:
        return len(self.outlines)

    def HoleCount(self, i: int) -> int:
        return len(self.outlines[i][1])

    def COutline(self, i: int) -> Chain:
        return self.outlines[i][0]

    def CHole(self, i: int, h: int) -> Chain:
        return self.outlines[i][1][h]

    def RemoveAllContours(self) -> None:
        self.outlines = []

    def AddOutline(self, chain: Chain) -> int:
        self.outlines.append((chain, []))
        return len(self.outlines) - 1

    def AddHole(self, chain: Chain, outline: int = -1) -> int:
        assert outline == len(self.outlines) - 1, "holes go on the outline just added"
        self.outlines[outline][1].append(chain)
        return len(self.outlines[outline][1]) - 1

    def Deflate(self, amount: int, strategy: int, max_error: int) -> None:
        self.deflated = (amount, strategy, max_error)
        if self.after_deflate is not None:
            self.outlines = self.after_deflate


def _fake_pcbnew(doc: str = DOC_10) -> types.SimpleNamespace:
    class BOARD:
        def GetBoardPolygonOutlines(self, *a, **kw):  # docstring patched below
            raise NotImplementedError

    BOARD.GetBoardPolygonOutlines.__doc__ = doc

    class PCB_SHAPE:  # noqa: N801  (pcbnew's name)
        pass

    lset_calls = []

    class LSET:
        @staticmethod
        def AllCuMask(n: int) -> tuple:
            lset_calls.append(n)
            return ("AllCuMask", n)

    return types.SimpleNamespace(
        BOARD=BOARD, PCB_SHAPE=PCB_SHAPE, LSET=LSET, lset_calls=lset_calls,
        SHAPE_POLY_SET=PolySet, VECTOR2I=Vec, Edge_Cuts=25, F_CrtYd=31,
        SHAPE_T_SEGMENT=0, SHAPE_T_RECTANGLE=1, SHAPE_T_ARC=2, SHAPE_T_CIRCLE=3,
        SHAPE_T_POLY=4, SHAPE_T_BEZIER=5, CORNER_STRATEGY_ROUND_ALL_CORNERS=7,
        ToMM=lambda v: v / 1e6, FromMM=lambda v: int(v * 1e6), LoadBoard=None)


class FakeBoard:
    """Answers GetBoardPolygonOutlines like pcbnew 9.0.4 (no keyword) or 10.0.5."""

    def __init__(self, ok: bool = True, outlines=None, bbox_area: float = 0.0,
                 accepts_infer: bool = True) -> None:
        self.ok, self.outlines, self.bbox_area = ok, outlines or [], bbox_area
        self.accepts_infer, self.calls = accepts_infer, []

    def GetBoardPolygonOutlines(self, sp, *args, **kw):
        self.calls.append((len(args), dict(kw)))
        if args:
            raise AssertionError("never a positional second argument")
        if kw and not self.accepts_infer:
            raise TypeError("unexpected keyword argument 'aInferOutlineIfNecessary'")
        if not kw and self.accepts_infer:
            raise TypeError("missing required argument 'aInferOutlineIfNecessary'")
        sp.outlines = [(o, list(h)) for o, h in self.outlines]
        sp.bbox_area = self.bbox_area      # 9.0.4 fills a bbox fallback on failure
        return self.ok


@pytest.fixture
def fake(monkeypatch) -> types.SimpleNamespace:
    pcb = _fake_pcbnew()
    monkeypatch.setattr(bo, "pcbnew", pcb)
    return pcb


def test_polygon_outlines_passes_the_infer_keyword_on_10(fake):
    board = FakeBoard(outlines=[(Chain(5e12), [])])
    sp = bo.polygon_outlines(board)
    assert board.calls == [(0, {"aInferOutlineIfNecessary": False})]
    assert sp.Area() == 5e12


def test_polygon_outlines_uses_the_one_argument_form_on_9(monkeypatch):
    monkeypatch.setattr(bo, "pcbnew", _fake_pcbnew(DOC_9))
    board = FakeBoard(outlines=[(Chain(5e12), [])], accepts_infer=False)
    bo.polygon_outlines(board)
    assert board.calls == [(0, {})]


@pytest.mark.parametrize("doc, accepts", [(DOC_10, False), (DOC_9, True)])
def test_polygon_outlines_tries_the_other_form_on_type_error(monkeypatch, doc, accepts):
    monkeypatch.setattr(bo, "pcbnew", _fake_pcbnew(doc))
    board = FakeBoard(outlines=[(Chain(1e12), [])], accepts_infer=accepts)
    bo.polygon_outlines(board)
    assert len(board.calls) == 2 and all(n == 0 for n, _ in board.calls)


def test_polygon_outlines_judges_only_the_bool_despite_a_bbox_fallback(fake):
    board = FakeBoard(ok=False, bbox_area=605e12)      # 9.0.4 on an open board
    with pytest.raises(bo.OutlineBuildError, match="Edge.Cuts does not form one closed outline"):
        bo.polygon_outlines(board)
    assert issubclass(bo.OutlineBuildError, RuntimeError)


def test_plane_polyset_deflates_a_copy_round_all_corners(fake):
    board = FakeBoard(outlines=[(Chain(10e12), [Chain(1e12)]), (Chain(2e12), [])])
    ps = bo.plane_polyset(board, 0.3)
    assert ps.deflated == (300_000, 7, 5_000)
    assert ps.OutlineCount() == 2                         # a narrow neck: keep every piece


def test_plane_polyset_raises_when_nothing_is_left(fake, monkeypatch):
    board = FakeBoard(outlines=[(Chain(1e12), [])])
    monkeypatch.setattr(PolySet, "__init__", _polyset_emptied_by_deflate)
    with pytest.raises(bo.OutlineBuildError, match="no outline"):
        bo.plane_polyset(board, 5.0)


def _polyset_emptied_by_deflate(self, src=None):
    self.outlines = [(o, list(h)) for o, h in (src.outlines if src else [])]
    self.bbox_area, self.deflated, self.after_deflate = 0.0, None, []


def test_plane_polyset_refuses_a_negative_inset(fake):
    with pytest.raises(ValueError, match="inset"):
        bo.plane_polyset(FakeBoard(outlines=[(Chain(1e12), [])]), -0.1)


class Zone:
    def __init__(self) -> None:
        self.outline = PolySet()
        self.outline.outlines = [(Chain(99e12), [])]      # stale contour to be replaced

    def Outline(self) -> PolySet:
        return self.outline


def test_fill_zone_outline_fills_in_place_with_holes_on_their_outline(fake):
    src = PolySet()
    src.outlines = [(Chain(10e12), [Chain(1e12), Chain(2e12)]), (Chain(3e12), [Chain(1e12)])]
    zone = Zone()
    kept = zone.outline
    assert bo.fill_zone_outline(zone, src) is zone
    assert zone.Outline() is kept                          # never replaced (no SetOutline)
    assert [(o.area_nm2, len(h)) for o, h in kept.outlines] == [(10e12, 2), (3e12, 1)]


def test_fill_zone_outline_raises_on_a_zero_area_result(fake):
    with pytest.raises(bo.OutlineBuildError, match="area"):
        bo.fill_zone_outline(Zone(), PolySet())


class RuleZone:
    def __init__(self, rule_area: bool = True) -> None:
        self.pour, self.rule_area = False, rule_area

    def GetIsRuleArea(self) -> bool:
        return self.rule_area


class Zone9(RuleZone):
    """pcbnew 9.0.4: Set/GetDoNotAllowCopperPour, no ZoneFills."""

    def SetDoNotAllowCopperPour(self, v: bool) -> None:
        self.pour = v

    def GetDoNotAllowCopperPour(self) -> bool:
        return self.pour


class Zone10(RuleZone):
    """pcbnew 10.0.5: Set/GetDoNotAllowZoneFills, no CopperPour."""

    def SetDoNotAllowZoneFills(self, v: bool) -> None:
        self.pour = v

    def GetDoNotAllowZoneFills(self) -> bool:
        return self.pour


@pytest.mark.parametrize("zone_cls", [Zone9, Zone10])
def test_no_pour_flag_works_on_both_versions(zone_cls):
    zone = zone_cls()
    assert not bo.blocks_pour(zone)
    bo.set_no_pour(zone)
    assert zone.pour is True and bo.blocks_pour(zone)
    bo.set_no_pour(zone, False)
    assert not bo.blocks_pour(zone)


def test_blocks_pour_needs_a_rule_area():
    zone = Zone9(rule_area=False)
    zone.pour = True
    assert not bo.blocks_pour(zone)


def test_no_pour_flag_raises_when_neither_name_exists():
    with pytest.raises(AttributeError, match="DoNotAllowCopperPour"):
        bo.set_no_pour(object())


def test_copper_lset_uses_the_board_copper_count(fake):
    class Board:
        def GetCopperLayerCount(self) -> int:
            return 4
    assert bo.copper_lset(Board()) == ("AllCuMask", 4)


class DS:
    def __init__(self) -> None:
        self.aux = self.grid = Vec(0, 0)
        self.m_CopperEdgeClearance = 300_000

    def GetAuxOrigin(self) -> Vec:
        return self.aux

    def SetAuxOrigin(self, v: Vec) -> None:
        self.aux = v

    def SetGridOrigin(self, v: Vec) -> None:
        self.grid = v


class DSBoard:
    def __init__(self) -> None:
        self.ds = DS()

    def GetDesignSettings(self) -> DS:
        return self.ds


def test_set_datum_sets_aux_and_grid_origin(fake):
    board = DSBoard()
    assert bo.get_aux_origin(board) == (0.0, 0.0)
    bo.set_datum(board, 138.5, 145)
    assert (board.ds.aux.x, board.ds.aux.y) == (138_500_000, 145_000_000)
    assert (board.ds.grid.x, board.ds.grid.y) == (138_500_000, 145_000_000)
    assert bo.get_aux_origin(board) == (138.5, 145.0)
    assert bo.edge_clearance_mm(board) == 0.3


def test_courtyard_builds_the_caches_first(fake):
    calls = []

    class FP:
        def BuildCourtyardCaches(self) -> None:
            calls.append("build")

        def GetCourtyard(self, layer: int) -> str:
            calls.append(("get", layer))
            return "poly"

    assert bo.courtyard(FP(), 31) == "poly"
    assert calls == ["build", ("get", 31)]


def test_load_board_refuses_a_missing_path_before_pcbnew(tmp_path, monkeypatch):
    called = []
    monkeypatch.setattr(bo, "pcbnew", types.SimpleNamespace(LoadBoard=called.append))
    with pytest.raises(FileNotFoundError, match="nope.kicad_pcb"):
        bo.load_board(tmp_path / "nope.kicad_pcb")
    monkeypatch.setattr(bo, "pcbnew", None)               # no pcbnew at all: same answer
    with pytest.raises(FileNotFoundError):
        bo.load_board(str(tmp_path / "nope.kicad_pcb"))
    assert called == []


def test_load_board_raises_when_pcbnew_returns_none_or_fails(tmp_path, monkeypatch):
    pcb = tmp_path / "x.kicad_pcb"
    pcb.write_text("(kicad_pcb)", encoding="utf-8")
    monkeypatch.setattr(bo, "pcbnew", types.SimpleNamespace(LoadBoard=lambda p: None))
    with pytest.raises(RuntimeError, match="returned None"):
        bo.load_board(pcb)

    def boom(p: str) -> None:
        raise OSError("IO_ERROR: bad token")

    monkeypatch.setattr(bo, "pcbnew", types.SimpleNamespace(LoadBoard=boom))
    with pytest.raises(RuntimeError, match="bad token"):
        bo.load_board(pcb)


# --- draw_outline / outline_from_board on fakes -------------------------------------------
class Shape:
    """PCB_SHAPE stand-in: records the setters draw_outline calls."""

    def __init__(self, board=None) -> None:
        self.calls: dict = {}

    def __getattr__(self, name: str):
        if name.startswith("Set"):
            return lambda *a: self.calls.__setitem__(name, a)
        raise AttributeError(name)


class DrawBoard(FakeBoard):
    def __init__(self, area_mm2: float) -> None:
        super().__init__(outlines=[(Chain(area_mm2 * 1e12), [])])
        self.added: list = []

    def Add(self, item) -> None:
        self.added.append(item)


def test_draw_outline_draws_exact_primitives_with_shared_end_points(fake, monkeypatch):
    monkeypatch.setattr(fake, "PCB_SHAPE", Shape, raising=False)
    board = DrawBoard(T10_AREA)
    shapes = bo.draw_outline(board, ol.from_board_block(T10), 100.5, 115.0)
    assert board.added == shapes and len(shapes) == 13
    kinds = [s.calls["SetShape"][0] for s in shapes]
    assert kinds.count(fake.SHAPE_T_ARC) == 6 and kinds.count(fake.SHAPE_T_CIRCLE) == 1
    assert all(s.calls["SetLayer"] == (25,) and s.calls["SetWidth"] == (100_000,)
               for s in shapes)
    ends = [_ends(s) for s in shapes[:8]]                  # the outer ring, in order
    for (_, end), (start, _) in zip(ends, ends[1:] + ends[:1], strict=True):
        assert end is start
    assert (ends[0][0].x, ends[0][0].y) == (104_500_000, 115_000_000)
    circle = shapes[-1].calls
    assert (circle["SetCenter"][0].x, circle["SetEnd"][0].x) == (130_500_000, 133_000_000)


def _ends(shape: Shape) -> tuple:
    c = shape.calls
    if "SetArcGeometry" in c:
        return c["SetArcGeometry"][0], c["SetArcGeometry"][2]
    return c["SetStart"][0], c["SetEnd"][0]


def test_draw_outline_raises_when_kicad_sees_another_area(fake, monkeypatch):
    monkeypatch.setattr(fake, "PCB_SHAPE", Shape, raising=False)
    with pytest.raises(bo.OutlineBuildError, match="area"):
        bo.draw_outline(DrawBoard(T10_AREA + 3.0), ol.from_board_block(T10), 0, 0)


class EdgeShape:
    def __init__(self, kind: int, layer: int = 25, **pts) -> None:
        self.kind, self.layer, self.pts = kind, layer, pts

    def GetShape(self) -> int:
        return self.kind

    def GetLayer(self) -> int:
        return self.layer

    def __getattr__(self, name: str):
        key = name[3:]
        if name.startswith("Get") and key in self.pts:
            return lambda: self.pts[key]
        raise AttributeError(name)


def _mm(x: float, y: float) -> Vec:
    return Vec(round(x * 1e6), round(y * 1e6))


def test_outline_from_board_reads_drawings_footprints_and_datum(fake, monkeypatch):
    monkeypatch.setattr(fake, "PCB_SHAPE", EdgeShape, raising=False)
    seg = fake.SHAPE_T_SEGMENT
    corners = [(100, 50), (130, 50), (130, 70), (100, 70)]
    drawings = [EdgeShape(seg, Start=_mm(*a), End=_mm(*b))
                for a, b in zip(corners, corners[1:] + corners[:1], strict=True)]
    drawings.append(EdgeShape(seg, layer=0, Start=_mm(0, 0), End=_mm(500, 500)))  # F.Cu
    hole = EdgeShape(fake.SHAPE_T_CIRCLE, Center=_mm(110, 60), Radius=2_000_000)
    fp = types.SimpleNamespace(GraphicalItems=lambda: [hole])
    board = types.SimpleNamespace(GetDrawings=lambda: drawings, GetFootprints=lambda: [fp],
                                  GetDesignSettings=lambda: types.SimpleNamespace(
                                      GetAuxOrigin=lambda: _mm(105, 70)))
    o, origin = bo.outline_from_board(board)
    assert origin == pytest.approx((100.0, 50.0))
    assert ol.bbox(o) == pytest.approx((0, 0, 30, 20))
    assert len(o.cutouts) == 1 and o.cutouts[0] == ol.Circle(10, 10, 2)
    assert o.datum == pytest.approx((5.0, 20.0))
    without, _ = bo.outline_from_board(board, include_footprints=False)
    assert without.cutouts == ()


def test_outline_from_board_without_aux_origin_has_no_datum(fake, monkeypatch):
    monkeypatch.setattr(fake, "PCB_SHAPE", EdgeShape, raising=False)
    rect = EdgeShape(fake.SHAPE_T_RECTANGLE, Start=_mm(10, 10), End=_mm(20, 15))
    board = types.SimpleNamespace(GetDrawings=lambda: [rect], GetFootprints=lambda: [],
                                  GetDesignSettings=lambda: types.SimpleNamespace(
                                      GetAuxOrigin=lambda: Vec(0, 0)))
    o, origin = bo.outline_from_board(board)
    assert origin == (10.0, 10.0) and o.datum is None and ol.area(o) == pytest.approx(50)


def test_outline_from_board_raises_build_error_on_open_or_missing_edges(fake, monkeypatch):
    monkeypatch.setattr(fake, "PCB_SHAPE", EdgeShape, raising=False)
    seg = fake.SHAPE_T_SEGMENT
    pts = [(0, 0), (10, 0), (10, 10), (0, 10), (0, 0.01)]     # last edge 0.01 mm short
    edges = [EdgeShape(seg, Start=_mm(*a), End=_mm(*b)) for a, b in zip(pts[:-1], pts[1:],
                                                                         strict=True)]
    ds = types.SimpleNamespace(GetAuxOrigin=lambda: Vec(0, 0))
    for drawn in (edges, []):
        board = types.SimpleNamespace(GetDrawings=lambda d=drawn: d, GetFootprints=lambda: [],
                                      GetDesignSettings=lambda: ds)
        with pytest.raises(bo.OutlineBuildError, match="Edge.Cuts"):
            bo.outline_from_board(board)


# --- source contract (grep + AST) ---------------------------------------------------------
@pytest.mark.parametrize("name, max_lines", [("board_outline.py", 450),
                                             ("board_shapes.py", 200)])
def test_source_contract(name, max_lines):
    src = (TOOLS / name).read_text(encoding="utf-8")
    assert len(src.splitlines()) < max_lines
    assert not re.search(r"\.SetOutline\s*\(", src), "never ZONE.SetOutline(<Python set>)"
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            assert node.end_lineno - node.lineno < 50, f"{node.name} is too long"
            args = node.args.posonlyargs + node.args.args + node.args.kwonlyargs
            unannotated = [a.arg for a in args if a.annotation is None and a.arg != "self"]
            assert not unannotated and node.returns is not None, \
                f"{name}:{node.name} is not fully annotated"
