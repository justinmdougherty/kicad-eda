"""swig_anchor: D3 ``at`` placement and footprint attributes, unit tier.

No KiCad: a fake pcbnew module, board and footprint record every call, so the
tests pin the exact recipe the live tier (test_swig_anchor_live.py) proves on
9.0.4 and 10.0.5:

  * normalize_rot / same_rot: [0, 360), -0.0 -> 0.0, comparison modulo 360;
  * parse_at / norm_side validate the netlist values before any pcbnew call;
  * place_at: SetPosition(origin + at, rounded to the nm) -> board.Add ->
    Flip(position, TOP_BOTTOM) for side B only -> SetOrientationDegrees(rot);
  * apply_attrs: board_only / dnp / in_bom=false -> the matching setters, a
    missing setter is a warning and never a faked attribute;
  * read_back: (x, y, rot, side) in the board frame;
  * the module is stdlib-only (+ kicad_quiet) and Python 3.11 syntax, because
    KiCad 9's bundled Python imports it.
"""
from __future__ import annotations

import ast
import copy
import math
import sys
import types
from pathlib import Path

import pytest

import swig_anchor as sa

SOURCE = Path(sa.__file__).resolve()
NM = 1_000_000


# ---------------------------------------------------------------------------
# Fakes: just the pcbnew surface swig_anchor touches
# ---------------------------------------------------------------------------
class FakeVec:
    def __init__(self, x: int, y: int) -> None:
        assert isinstance(x, int) and isinstance(y, int), (x, y)   # SWIG wants ints
        self.x, self.y = x, y

    def __eq__(self, other: object) -> bool:
        return isinstance(other, FakeVec) and (self.x, self.y) == (other.x, other.y)

    def __repr__(self) -> str:
        return f"FakeVec({self.x}, {self.y})"


def fake_pcbnew(*, flip_const: bool = True) -> types.SimpleNamespace:
    mod = types.SimpleNamespace(VECTOR2I=FakeVec, Version=lambda: "9.0.4-fake")
    if flip_const:
        mod.FLIP_DIRECTION_TOP_BOTTOM = 1
        mod.FLIP_DIRECTION_LEFT_RIGHT = 0
    return mod


SETTERS = ("SetBoardOnly", "SetDNP", "SetExcludedFromBOM", "SetExcludedFromPosFiles")


class FakeFootprint:
    """Records calls in order; Flip mirrors like pcbnew (orientation -> -rot)."""

    def __init__(self, ref: str = "R1", missing: tuple[str, ...] = ()) -> None:
        self.calls: list[tuple] = []
        self.ref = ref
        self.pos = FakeVec(0, 0)
        self.orient = 0.0
        self.flipped = False
        self.parent = None
        self.flags: dict[str, bool] = {}
        for name in SETTERS:
            if name not in missing:
                setattr(self, name, self._setter(name))

    def _setter(self, name: str):
        def set_flag(value: bool) -> None:
            self.calls.append((name, value))
            self.flags[name] = value
        return set_flag

    def GetReference(self) -> str:  # noqa: N802 (pcbnew API)
        return self.ref

    def GetParent(self):  # noqa: N802
        return self.parent

    def SetPosition(self, v: FakeVec) -> None:  # noqa: N802
        self.calls.append(("SetPosition", v))
        self.pos = v

    def GetPosition(self) -> FakeVec:  # noqa: N802
        return FakeVec(self.pos.x, self.pos.y)

    def Flip(self, centre: FakeVec, direction: object) -> None:  # noqa: N802
        self.calls.append(("Flip", centre, direction))
        self.pos = FakeVec(self.pos.x, 2 * centre.y - self.pos.y)
        self.orient = -self.orient
        self.flipped = not self.flipped

    def IsFlipped(self) -> bool:  # noqa: N802
        return self.flipped

    def SetOrientationDegrees(self, deg: float) -> None:  # noqa: N802
        self.calls.append(("SetOrientationDegrees", deg))
        self.orient = deg

    def GetOrientationDegrees(self) -> float:  # noqa: N802
        return self.orient


class FakeBoard:
    def __init__(self) -> None:
        self.added: list[FakeFootprint] = []

    def Add(self, fp: FakeFootprint) -> None:  # noqa: N802
        fp.calls.append(("Add",))
        fp.parent = self
        self.added.append(fp)


def names(fp: FakeFootprint) -> list[str]:
    return [c[0] for c in fp.calls]


# ---------------------------------------------------------------------------
# normalize_rot / same_rot
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(("raw", "want"), [
    (0, 0.0), (0.0, 0.0), (-0.0, 0.0), (360, 0.0), (720.0, 0.0), (-360, 0.0),
    (30, 30.0), (-30, 330.0), (-45, 315.0), (-90, 270.0), (270, 270.0),
    (450, 90.0), (359.5, 359.5), (-180, 180.0), (180, 180.0),
])
def test_normalize_rot(raw: float, want: float):
    got = sa.normalize_rot(raw)
    assert got == want and isinstance(got, float)
    assert math.copysign(1.0, got) == 1.0          # never -0.0


@pytest.mark.parametrize("raw", [-1e-14, -1e-300, 720 - 1e-13, -5e-324])
def test_normalize_rot_never_returns_360(raw: float):
    got = sa.normalize_rot(raw)
    assert 0.0 <= got < 360.0
    assert math.copysign(1.0, got) == 1.0


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf, "90", None, True])
def test_normalize_rot_rejects_non_numbers(bad):
    with pytest.raises(ValueError, match="rotation"):
        sa.normalize_rot(bad)


@pytest.mark.parametrize(("a", "b", "same"), [
    (0, 360, True), (-90, 270, True), (-45, 315, True), (90, -270, True),
    (359.9999999, 0, True), (0, 359.9999999, True), (-0.0, 0.0, True),
    (0, 1e-3, False), (90, 270, False), (30, -30, False), (180, -180, True),
])
def test_same_rot(a: float, b: float, same: bool):
    assert sa.same_rot(a, b) is same


def test_same_rot_honours_its_tolerance():
    assert not sa.same_rot(10.0, 10.01)
    assert sa.same_rot(10.0, 10.01, tol=0.02)
    assert sa.same_rot(359.99, 0.0, tol=0.02)


# ---------------------------------------------------------------------------
# parse_at / norm_side
# ---------------------------------------------------------------------------
def test_parse_at_returns_floats():
    assert sa.parse_at([1, 2.5, -45]) == (1.0, 2.5, -45.0)
    assert sa.parse_at((0, 0, 0)) == (0.0, 0.0, 0.0)
    assert all(isinstance(v, float) for v in sa.parse_at([1, 2, 3]))


@pytest.mark.parametrize("bad", [
    None, [], [1, 2], [1, 2, 3, 4], "1,2,3", {"x": 1, "y": 2, "rot": 0},
    [1, "2", 3], [1, 2, None], [True, 2, 3], [1, math.nan, 0], [1, 2, math.inf],
])
def test_parse_at_rejects_malformed(bad):
    with pytest.raises(ValueError, match=r"'at'"):
        sa.parse_at(bad)


@pytest.mark.parametrize(("raw", "want"), [
    ("F", "F"), ("B", "B"), ("f", "F"), ("b", "B"), (None, "F")])
def test_norm_side(raw, want):
    assert sa.norm_side(raw) == want


@pytest.mark.parametrize("bad", ["X", "top", "", 1, True])
def test_norm_side_rejects_unknown(bad):
    with pytest.raises(ValueError, match="side"):
        sa.norm_side(bad)


# ---------------------------------------------------------------------------
# place_at
# ---------------------------------------------------------------------------
def test_place_at_front_side_recipe():
    board, fp, pcb = FakeBoard(), FakeFootprint(), fake_pcbnew()
    sa.place_at(board, fp, [12.3456, 7.5, 30], "F", 100.5, 115.25, pcb)
    assert names(fp) == ["SetPosition", "Add", "SetOrientationDegrees"]
    assert fp.calls[0][1] == FakeVec(112_845_600, 122_750_000)
    assert fp.calls[2][1] == 30.0
    assert board.added == [fp]
    assert sa.read_back(fp, 100.5, 115.25) == (12.3456, 7.5, 30.0, "F")


def test_place_at_back_side_flips_after_add_about_the_origin_then_sets_rot():
    board, fp, pcb = FakeBoard(), FakeFootprint(), fake_pcbnew()
    sa.place_at(board, fp, [10, 20, 45], "B", 100, 100, pcb)
    assert names(fp) == ["SetPosition", "Add", "Flip", "SetOrientationDegrees"]
    _, centre, direction = fp.calls[2]
    assert centre == FakeVec(110 * NM, 120 * NM)          # the origin itself: x, y kept
    assert direction == pcb.FLIP_DIRECTION_TOP_BOTTOM
    assert fp.calls[3][1] == 45.0                          # displayed (post-flip) rotation
    assert sa.read_back(fp, 100, 100) == (10.0, 20.0, 45.0, "B")


def test_place_at_without_the_flip_enum_passes_true():
    """9.0 / 10.0 verified: Flip(c, True) == FLIP_DIRECTION_TOP_BOTTOM."""
    board, fp = FakeBoard(), FakeFootprint()
    sa.place_at(board, fp, [0, 0, 0], "b", 0, 0, fake_pcbnew(flip_const=False))
    assert fp.calls[2][0] == "Flip" and fp.calls[2][2] is True


def test_place_at_rounds_to_the_nearest_nanometre():
    """FromMM truncates (158.51999999999998 mm -> 158519999 nm); place_at rounds."""
    board, fp = FakeBoard(), FakeFootprint()
    ox, x = 138.48, 20.04
    assert int((ox + x) * NM) == 158_519_999
    sa.place_at(board, fp, [x, 0.0000004, 0], "F", ox, 0, fake_pcbnew())
    assert fp.calls[0][1] == FakeVec(158_520_000, 0)


def test_place_at_flips_back_a_footprint_that_is_already_on_b():
    board, fp = FakeBoard(), FakeFootprint()
    fp.flipped = True
    sa.place_at(board, fp, [1, 1, 90], "F", 0, 0, fake_pcbnew())
    assert names(fp) == ["SetPosition", "Add", "Flip", "SetOrientationDegrees"]
    assert sa.read_back(fp, 0, 0)[3] == "F"


@pytest.mark.parametrize(("at", "side", "ox"), [
    ([1, 2], "F", 0), ([1, 2, 3], "top", 0), ([1, 2, 3], "F", math.nan),
    ([1, 2, 3], "F", "100"), ([3000, 0, 0], "F", 0)])
def test_place_at_validates_before_touching_the_footprint(at, side, ox):
    board, fp = FakeBoard(), FakeFootprint()
    with pytest.raises(ValueError):
        sa.place_at(board, fp, at, side, ox, 0, fake_pcbnew())
    assert fp.calls == [] and board.added == []


def test_place_at_refuses_a_footprint_already_on_a_board():
    board, fp = FakeBoard(), FakeFootprint("U7")
    fp.parent = object()
    with pytest.raises(ValueError, match="U7.*already"):
        sa.place_at(board, fp, [1, 2, 0], "F", 0, 0, fake_pcbnew())
    assert fp.calls == [] and board.added == []


def test_place_at_imports_pcbnew_lazily(monkeypatch):
    pcb = types.ModuleType("pcbnew")
    pcb.VECTOR2I = FakeVec
    pcb.FLIP_DIRECTION_TOP_BOTTOM = 1
    monkeypatch.setitem(sys.modules, "pcbnew", pcb)
    board, fp = FakeBoard(), FakeFootprint()
    sa.place_at(board, fp, [5, 6, 180], "B", 0, 0)
    assert fp.calls[2] == ("Flip", FakeVec(5 * NM, 6 * NM), 1)


def test_place_at_does_not_mutate_the_at_list():
    at = [1, 2, -45]
    sa.place_at(FakeBoard(), FakeFootprint(), at, "F", 0, 0, fake_pcbnew())
    assert at == [1, 2, -45]


# ---------------------------------------------------------------------------
# apply_attrs
# ---------------------------------------------------------------------------
BO = ["SetBoardOnly", "SetExcludedFromBOM", "SetExcludedFromPosFiles"]


@pytest.mark.parametrize(("comp", "setters"), [
    ({}, []),
    ({"board_only": False, "dnp": False, "in_bom": True}, []),
    ({"board_only": None, "dnp": None, "in_bom": None}, []),
    ({"board_only": True}, BO),
    ({"dnp": True}, ["SetDNP", "SetExcludedFromPosFiles"]),
    ({"in_bom": False}, ["SetExcludedFromBOM"]),
    ({"dnp": True, "in_bom": False},
     ["SetDNP", "SetExcludedFromPosFiles", "SetExcludedFromBOM"]),
    ({"board_only": True, "dnp": True, "in_bom": False},
     ["SetBoardOnly", "SetExcludedFromBOM", "SetExcludedFromPosFiles", "SetDNP"]),
])
def test_apply_attrs_calls_each_needed_setter_once(comp, setters):
    fp = FakeFootprint()
    assert sa.apply_attrs(fp, {"ref": "R1", **comp}, fake_pcbnew()) == []
    assert fp.calls == [(name, True) for name in setters]


@pytest.mark.parametrize(("comp", "tokens"), [
    ({}, set()),
    ({"in_bom": True, "dnp": False}, set()),
    ({"board_only": True}, {"board_only", "exclude_from_bom", "exclude_from_pos_files"}),
    ({"dnp": True}, {"dnp", "exclude_from_pos_files"}),
    ({"in_bom": False}, {"exclude_from_bom"}),
    ({"board_only": True, "dnp": True, "in_bom": False},
     {"board_only", "dnp", "exclude_from_bom", "exclude_from_pos_files"}),
])
def test_attr_tokens_are_the_saved_file_flags(comp, tokens):
    assert sa.attr_tokens(comp) == frozenset(tokens)
    assert sa.attr_tokens(comp) <= sa.MANAGED_TOKENS


def test_a_missing_setter_is_a_warning_never_faked():
    fp = FakeFootprint("U3", missing=("SetDNP",))
    warnings = sa.apply_attrs(fp, {"ref": "U3", "dnp": True, "in_bom": False}, fake_pcbnew())
    assert len(warnings) == 1
    (w,) = warnings
    assert "U3" in w and "SetDNP" in w and "dnp" in w and "9.0.4-fake" in w
    assert fp.calls == [("SetExcludedFromPosFiles", True), ("SetExcludedFromBOM", True)]
    assert not hasattr(fp, "SetDNP")                       # no dead attribute created


def test_apply_attrs_reads_the_reference_from_the_footprint_when_comp_has_none():
    fp = FakeFootprint("H2", missing=("SetBoardOnly",))
    (w,) = sa.apply_attrs(fp, {"board_only": True}, fake_pcbnew())
    assert w.startswith("H2:") and "SetBoardOnly" in w


def test_apply_attrs_imports_pcbnew_lazily_only_for_a_warning(monkeypatch):
    monkeypatch.setitem(sys.modules, "pcbnew", None)        # `import pcbnew` would fail
    fp = FakeFootprint()
    assert sa.apply_attrs(fp, {"dnp": True}) == []
    (w,) = sa.apply_attrs(FakeFootprint(missing=("SetDNP",)), {"dnp": True})
    assert "unknown version" in w


@pytest.mark.parametrize("comp", [
    {"dnp": "yes"}, {"in_bom": 0}, {"board_only": 1}, {"board_only": "true"}])
def test_apply_attrs_rejects_non_boolean_flags(comp):
    fp = FakeFootprint()
    with pytest.raises(ValueError, match="boolean"):
        sa.apply_attrs(fp, comp, fake_pcbnew())
    assert fp.calls == []


def test_apply_attrs_does_not_mutate_the_component():
    comp = {"ref": "R9", "board_only": True, "dnp": True, "in_bom": False}
    before = copy.deepcopy(comp)
    sa.apply_attrs(FakeFootprint(), comp, fake_pcbnew())
    assert comp == before


# ---------------------------------------------------------------------------
# read_back
# ---------------------------------------------------------------------------
def test_read_back_is_board_frame_and_normalised():
    fp = FakeFootprint()
    fp.pos = FakeVec(112_845_600, 122_750_001)
    fp.orient = -90.0
    fp.flipped = True
    assert sa.read_back(fp, 100.5, 115.25) == (12.3456, 7.500001, 270.0, "B")
    fp.orient, fp.flipped = -0.0, False
    x, y, rot, side = sa.read_back(fp, 100.5, 115.25)
    assert rot == 0.0 and math.copysign(1.0, rot) == 1.0 and side == "F"


def test_read_back_rounds_a_rotation_a_hair_below_360_to_zero():
    fp = FakeFootprint()
    fp.orient = -1e-9
    assert sa.read_back(fp, 0, 0)[2] == 0.0


@pytest.mark.parametrize("side", ["F", "B"])
@pytest.mark.parametrize("rot", [0, 30, 90, 180, 270, -45, 45])
def test_place_then_read_back_round_trips(side, rot):
    fp = FakeFootprint()
    sa.place_at(FakeBoard(), fp, [3.21, 4.56, rot], side, 50.0, 60.0, fake_pcbnew())
    x, y, got, got_side = sa.read_back(fp, 50.0, 60.0)
    assert (x, y, got_side) == (3.21, 4.56, side)
    assert sa.same_rot(got, rot) and 0.0 <= got < 360.0


# ---------------------------------------------------------------------------
# Runs under KiCad 9's bundled Python: stdlib + kicad_quiet, 3.11 syntax
# ---------------------------------------------------------------------------
STDLIB_OK = {"__future__", "collections", "math", "numbers", "typing", "kicad_quiet",
             "pcbnew"}


def test_module_is_stdlib_only_and_python_311_syntax():
    source = SOURCE.read_text(encoding="utf-8")
    tree = ast.parse(source, feature_version=(3, 11))
    mods = {alias.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import)
            for alias in n.names}
    mods |= {n.module.split(".")[0] for n in ast.walk(tree)
             if isinstance(n, ast.ImportFrom) and n.module}
    assert mods <= STDLIB_OK, mods - STDLIB_OK
    module_level = [n for n in tree.body if isinstance(n, ast.Import)]
    assert any(a.name == "kicad_quiet" for n in module_level for a in n.names)
    assert not any(a.name == "pcbnew" for n in module_level for a in n.names), \
        "pcbnew must be passed in or imported lazily (venv-testable)"
    assert len(source.splitlines()) < 800
