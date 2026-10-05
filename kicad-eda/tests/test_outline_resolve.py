"""Unit tests for server/tools/outline_resolve.py: the one board-block resolver
(ROADMAP_DECISIONS D2) shared by lint, the builder, the mechanical tools and G5,
its fingerprint (route hash, includes the DXF bytes) and the KICAD_EDA_OUTLINE
sidecar contract that swig_build_board reads (F3-11).

DXF fixtures are generated in tmp with tests/fixtures/dxf_builders.py.
"""
from __future__ import annotations

import ast
import copy
import hashlib
import json
import math
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

import outline_resolve as orz
from fixtures.dxf_builders import (EXPECTED_SLOT, SKETCH_LAYER, Sketch, board_rounded_rect,
                                   canon, cutout_circles, cutout_loops, fusion_board,
                                   fusion_doc, new_doc, rect_poly, save)
from outline import OutlineError
from outline_resolve import ResolveError, ResolvedBoard, resolve_board

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "server" / "tools"
T90 = math.tan(math.pi / 8)
T10_LOOP = [[4, 0], [56, 0, T90], [60, 4], [60, 36, T90], [56, 40], [4, 40, T90], [0, 36],
            [0, 4, T90]]
STADIUM = {"loop": [[27, 7], [27, 11, -1], [33, 11], [33, 7, -1]]}
HOLE = {"circle": [30, 20, 2.5]}
L_LOOP = [[0, 0], [32, 0], [32, 20], [60, 20], [60, 45], [0, 45]]
AUTO = {"auto_x": 5.0, "auto_y": 6.0, "auto_w": 20.0, "auto_h": 12.0,
        "auto_b": {"x": 30.0, "y": 8.0, "w": 10.0, "h": 9.0}}
OUTLINE_KEYS = ("outline", "corner_radius", "cutouts")


def _design(board: object = None, **extra: object) -> dict:
    design: dict = {"components": [{"ref": "R1", "lib_id": "Device:R",
                                    "footprint": "Resistor_SMD:R_0603_1608Metric"}], **extra}
    if board is not None:
        design["board"] = board
    return design


def _t10(**extra: object) -> dict:
    return {"outline": {"loop": T10_LOOP}, "cutouts": [STADIUM, HOLE], "datum": [10, 30],
            **extra}


def _fusion_dxf(folder: Path, name: str = "outline.dxf", point: bool = False) -> Path:
    """The Fusion-like 60 x 40 r3 board (cm, sketch layer): a 1.6 mm hole on
    the source origin and a 10 x 4 slot; the origin lands at (10, 10)."""
    doc, msp = fusion_doc()
    fusion_board(Sketch(msp, k=10.0))
    if point:                                  # sketch point: skipped, no warning
        msp.add_point((1, 1), dxfattribs={"layer": SKETCH_LAYER})
    folder.mkdir(parents=True, exist_ok=True)
    return save(doc, folder, name)


def _dxf_design(tmp_path: Path, **board: object) -> dict:
    _fusion_dxf(tmp_path / "mech")
    return _design({"outline": {"dxf": "mech/outline.dxf"}, **board})


# --- sources and the legacy flag -----------------------------------------------------
@pytest.mark.parametrize("design", [_design(), _design({}), {"components": [], "board": None}],
                         ids=["absent", "empty", "null"])
def test_no_board_block_is_source_none(design: dict) -> None:
    r = resolve_board(design, None)
    assert (r.source, r.is_legacy, r.outline, r.dxf_path, r.warnings) == \
        ("none", True, None, None, ())
    assert r.block == {} and orz.outline_bbox_dims(r) == (0.0, 0.0)
    assert orz.resolved_design(design, r) == design


NO_GEOMETRY = [dict(AUTO), {"auto_x": 5.0}, {"panel_cutouts": True}, {"w": None, "h": None},
               {"corner_radius": 2.0}, {"cutouts": [HOLE]}, {"datum": [0, 0]}]
NO_GEOMETRY_IDS = ["auto", "auto_x", "panel_cutouts", "null-w-h", "radius-only", "cutouts-only",
                   "datum-only"]


@pytest.mark.parametrize("board", NO_GEOMETRY, ids=NO_GEOMETRY_IDS)
def test_block_without_w_h_or_outline_raises(board: dict) -> None:
    """A non-empty board block needs w/h or an outline (as netlist_schema
    says: auto_* windows only exist inside a fixed board); no 'none' source."""
    with pytest.raises(ResolveError, match="give w and h") as ei:
        resolve_board(_design(board), None)
    assert isinstance(ei.value.__cause__, OutlineError)
    assert str(ei.value) == str(ei.value.__cause__)


def test_legacy_w_h_is_rect() -> None:
    board = {"w": 65.0, "h": 56.5, **AUTO}
    r = resolve_board(_design(board), None)
    assert (r.source, r.is_legacy, r.dxf_path, r.warnings) == ("rect", True, None, ())
    assert r.block == board and r.block is not board
    assert orz.outline_bbox_dims(r) == (65.0, 56.5)
    assert r.outline is not None and orz.ol.area(r.outline) == pytest.approx(65.0 * 56.5)


def test_zero_corner_radius_is_still_a_legacy_rect() -> None:
    r = resolve_board(_design({"w": 60, "h": 40, "corner_radius": 0}), None)
    assert (r.source, r.is_legacy) == ("rect", True)


def test_corner_radius_is_rounded_rect() -> None:
    r = resolve_board(_design({"w": 60, "h": 40, "corner_radius": 3.0}), None)
    assert (r.source, r.is_legacy) == ("rounded_rect", False)
    assert orz.ol.area(r.outline) == pytest.approx(2400 - (4 - math.pi) * 9)
    assert orz.outline_bbox_dims(r) == (60.0, 40.0)


@pytest.mark.parametrize("extra", [{"cutouts": [HOLE]}, {"datum": [0, 40]}],
                         ids=["cutouts", "datum"])
def test_rect_with_cutouts_or_datum_is_not_legacy(extra: dict) -> None:
    r = resolve_board(_design({"w": 60, "h": 40, **extra}), None)
    assert (r.source, r.is_legacy) == ("rect", False)


def test_rect_with_empty_cutouts_is_legacy() -> None:
    assert resolve_board(_design({"w": 60, "h": 40, "cutouts": []}), None).is_legacy


def test_loop_with_cutouts_and_datum() -> None:
    board = _t10(**AUTO)
    r = resolve_board(_design(board), None)
    assert (r.source, r.is_legacy, r.warnings) == ("loop", False, ())
    assert r.block == board
    assert r.outline.datum == (10.0, 30.0) and len(r.outline.cutouts) == 2
    assert orz.outline_bbox_dims(r) == pytest.approx((60.0, 40.0))


def test_loop_without_w_h_takes_its_bbox() -> None:
    r = resolve_board(_design({"outline": {"loop": L_LOOP}}), None)
    assert r.source == "loop" and orz.outline_bbox_dims(r) == pytest.approx((60.0, 45.0))
    assert orz.ol.area(r.outline) == pytest.approx(60 * 45 - 28 * 20)


# --- DXF outlines ---------------------------------------------------------------------
def _assert_fusion_block(block: dict) -> None:
    assert canon(block["outline"]["loop"]) == canon(board_rounded_rect(60, 40, 3))
    assert block["datum"] == pytest.approx([10, 10])
    assert cutout_circles(block) == [[10, 10, 1.6]]
    assert cutout_loops(block) == [canon(EXPECTED_SLOT)]
    assert (block["w"], block["h"]) == pytest.approx((60, 40))
    assert "dxf" not in block["outline"]


def test_fusion_dxf_reference_is_replaced_by_loops_and_datum(tmp_path: Path) -> None:
    r = resolve_board(_dxf_design(tmp_path, **AUTO), tmp_path)
    assert (r.source, r.is_legacy) == ("dxf", False)
    _assert_fusion_block(r.block)
    assert {k: r.block[k] for k in AUTO} == AUTO               # auto_* preserved
    assert r.dxf_path == os.path.abspath(tmp_path / "mech" / "outline.dxf")
    assert r.outline.datum == pytest.approx((10, 10)) and len(r.outline.cutouts) == 2
    assert orz.outline_bbox_dims(r) == pytest.approx((60, 40))


@pytest.mark.parametrize("ref", ["${KIPRJMOD}/mech/outline.dxf", "ABS"])
def test_dxf_paths_kiprjmod_and_absolute(tmp_path: Path, ref: str) -> None:
    path = _fusion_dxf(tmp_path / "mech")
    spec = {"dxf": str(path) if ref == "ABS" else ref, "layer": SKETCH_LAYER, "units": "cm"}
    r = resolve_board(_design({"outline": spec}), tmp_path)
    assert r.dxf_path == os.path.abspath(path)
    _assert_fusion_block(r.block)


def test_dxf_reader_warnings_pass_through(tmp_path: Path) -> None:
    doc, msp = new_doc(insunits=0)                         # unitless: read as mm
    rect_poly(msp, 0, 0, 50, 30)
    save(doc, tmp_path, "plain.dxf")
    r = resolve_board(_design({"outline": {"dxf": "plain.dxf"}}), tmp_path)
    assert any("$INSUNITS" in w for w in r.warnings)
    assert orz.outline_bbox_dims(r) == pytest.approx((50, 30))


def test_dxf_with_matching_w_h_resolves(tmp_path: Path) -> None:
    r = resolve_board(_dxf_design(tmp_path, w=60, h=40.0000001), tmp_path)
    assert r.source == "dxf" and r.block["w"] == pytest.approx(60)


@pytest.mark.parametrize("extra, needle", [
    ({"w": 61, "h": 40}, "board.w"),
    ({"corner_radius": 2.0}, "corner_radius"),
    ({"cutouts": [HOLE]}, "board.cutouts"),
    ({"datum": [0, 0]}, "board.datum"),
], ids=["w-contradicts", "corner_radius", "cutouts", "datum"])
def test_dxf_conflicting_board_keys_raise(tmp_path: Path, extra: dict, needle: str) -> None:
    with pytest.raises(ResolveError, match=needle.replace(".", r"\.")):
        resolve_board(_dxf_design(tmp_path, **extra), tmp_path)


# --- fingerprint ------------------------------------------------------------------------
def test_fingerprint_is_sha256_of_canonical_json() -> None:
    r = resolve_board(_design({"w": 60, "h": 40}), None)
    assert r.fingerprint == hashlib.sha256(b'{"h":40,"w":60}').hexdigest()


def test_fingerprint_includes_the_dxf_bytes(tmp_path: Path) -> None:
    design = _dxf_design(tmp_path)
    first = resolve_board(design, tmp_path)
    assert resolve_board(design, tmp_path).fingerprint == first.fingerprint   # stable
    _fusion_dxf(tmp_path / "mech", point=True)            # same geometry, new bytes
    second = resolve_board(design, tmp_path)
    assert second.block == first.block and second.warnings == first.warnings
    assert second.fingerprint != first.fingerprint
    canonical = json.dumps(first.block, sort_keys=True, separators=(",", ":")).encode()
    assert first.fingerprint != hashlib.sha256(canonical).hexdigest()


def test_fingerprint_changes_with_a_cutout() -> None:
    a = resolve_board(_design(_t10()), None)
    moved = _t10(cutouts=[STADIUM, {"circle": [30, 21, 2.5]}])
    assert resolve_board(_design(moved), None).fingerprint != a.fingerprint


def test_fingerprint_is_stable_under_key_reordering() -> None:
    board = _t10(**AUTO)
    shuffled = {k: board[k] for k in reversed(list(board))}
    shuffled["auto_b"] = {k: AUTO["auto_b"][k] for k in reversed(list(AUTO["auto_b"]))}
    assert list(shuffled) != list(board)
    assert resolve_board(_design(shuffled), None).fingerprint == \
        resolve_board(_design(board), None).fingerprint


# --- errors -------------------------------------------------------------------------------
def test_missing_dxf_raises_with_the_reader_message(tmp_path: Path) -> None:
    design = _design({"outline": {"dxf": "mech/nope.dxf"}})
    with pytest.raises(ResolveError, match="DXF file not found") as ei:
        resolve_board(design, tmp_path)
    assert type(ei.value.__cause__).__name__ == "DxfOutlineError"
    assert str(ei.value) == str(ei.value.__cause__)


BAD_SPECS = [
    ({"dxf": "mech/outline.dxf", "loop": L_LOOP}, "exactly one of 'loop' or 'dxf'"),
    ({"loop": L_LOOP, "units": "cm"}, "only apply to a 'dxf' outline"),
    ({"loop": L_LOOP, "layer": "OUTLINE"}, "only apply to a 'dxf' outline"),
    ({"dxf": "mech/outline.dxf", "scale": 2}, "unknown key"),
    ({"loop": L_LOOP, "dxf_layer": "X"}, "unknown key"),
]
BAD_SPEC_IDS = ["dxf-and-loop", "loop-units", "loop-layer", "dxf-unknown", "loop-unknown"]


@pytest.mark.parametrize("spec, needle", BAD_SPECS, ids=BAD_SPEC_IDS)
def test_outline_spec_keys_are_checked(tmp_path: Path, spec: dict, needle: str) -> None:
    """Never silently drop or misread user geometry (a loop next to a dxf, a
    loop 'in cm'): the resolver refuses what the schema refuses."""
    _fusion_dxf(tmp_path / "mech")
    with pytest.raises(ResolveError, match=re.escape(needle)):
        resolve_board(_design({"outline": spec}), tmp_path)


@pytest.mark.parametrize("board", [None, {}, {"w": 60, "h": 40, **AUTO}, _t10(),
                                   {"outline": {"dxf": "mech/outline.dxf"}},
                                   *NO_GEOMETRY, *({"outline": s} for s, _ in BAD_SPECS)])
def test_resolver_and_schema_agree_on_the_board_block(tmp_path: Path, board: object) -> None:
    import netlist_schema

    _fusion_dxf(tmp_path / "mech")
    design = _design(board)
    board_errors = [e for e in netlist_schema.validate(design) if e.startswith("$.board")]
    try:
        resolve_board(design, tmp_path)
        rejected = False
    except ResolveError:
        rejected = True
    assert rejected is bool(board_errors), board_errors


def test_dxf_without_base_dir_raises(tmp_path: Path) -> None:
    path = _fusion_dxf(tmp_path)
    with pytest.raises(ResolveError, match="base_dir"):
        resolve_board(_design({"outline": {"dxf": str(path)}}), None)


@pytest.mark.parametrize("board, needle", [
    ({"outline": {"loop": [[5, 5], [20, 5], [20, 20], [5, 20]]}}, "bbox starts at"),
    ({"outline": {"loop": [[0, 0], [10, 10], [10, 0], [0, 10]]}}, "intersect"),
    ({"outline": {"loop": L_LOOP}, "w": 61}, "contradicts"),
    ({"w": 60, "h": 40, "corner_radius": 25}, "corner_radius"),
    ({"w": 60}, "give w and h"),
    ({"w": 60, "h": 40, "cutouts": [{"circle": [100, 100, 2]}]}, "outside the outline"),
    ({"w": 60, "h": 40, "cutouts": [HOLE, {"circle": [31, 20, 2.5]}]}, "overlap"),
], ids=["off-frame", "self-crossing", "w-vs-loop", "radius", "w-only", "cutout-off-board",
        "cutouts-overlap"])
def test_bad_outline_raises_with_the_outline_message(board: dict, needle: str) -> None:
    with pytest.raises(ResolveError, match=needle) as ei:
        resolve_board(_design(board), None)
    assert isinstance(ei.value.__cause__, OutlineError)
    assert str(ei.value) == str(ei.value.__cause__)


@pytest.mark.parametrize("design", [[], {"board": [60, 40]}, {"board": "60x40"}],
                         ids=["design-list", "board-list", "board-str"])
def test_non_object_inputs_raise(design: object) -> None:
    with pytest.raises(ResolveError, match="object"):
        resolve_board(design, None)


def test_resolve_error_is_a_value_error() -> None:
    assert issubclass(ResolveError, ValueError)


# --- immutability -------------------------------------------------------------------------
@pytest.mark.parametrize("board", [None, {"w": 60, "h": 40, **AUTO}, _t10(**AUTO),
                                   {"w": 60, "h": 40, "corner_radius": 3}, "DXF"],
                         ids=["none", "rect", "loop", "rounded", "dxf"])
def test_inputs_are_never_mutated(tmp_path: Path, board: object) -> None:
    design = _dxf_design(tmp_path, **AUTO) if board == "DXF" else _design(board)
    before = copy.deepcopy(design)
    r = resolve_board(design, tmp_path)
    out = orz.resolved_design(design, r)
    assert design == before
    assert out is not design and out["components"] is not design["components"]
    if r.source != "none":
        assert out["board"] == r.block and out["board"] is not r.block
        out["board"]["w"] = -1                        # the copy is independent
        assert design == before and r.block.get("w") != -1


def test_resolved_design_swaps_in_the_resolved_dxf_block(tmp_path: Path) -> None:
    design = _dxf_design(tmp_path, **AUTO)
    out = orz.resolved_design(design, resolve_board(design, tmp_path))
    _assert_fusion_block(out["board"])
    assert out["components"] == design["components"]
    assert design["board"]["outline"] == {"dxf": "mech/outline.dxf"}


# --- sidecar ------------------------------------------------------------------------------
def test_sidecar_path() -> None:
    got = orz.sidecar_path(Path("out") / "board.kicad_pcb")
    assert got == str(Path("out") / "board.outline.json")


def test_sidecar_round_trip_and_pinned_format(tmp_path: Path) -> None:
    r = resolve_board(_dxf_design(tmp_path, **AUTO), tmp_path)
    side = tmp_path / "out" / "board.outline.json"
    assert orz.write_sidecar(r, side) == side
    raw = json.loads(side.read_text(encoding="utf-8"))
    assert list(raw) == ["version", "board", "source", "dxf", "fingerprint", "warnings"]
    assert raw == {"version": 1, "board": r.block, "source": "dxf", "dxf": r.dxf_path,
                   "fingerprint": r.fingerprint, "warnings": list(r.warnings)}
    assert orz.read_sidecar(side) == raw
    assert [p.name for p in side.parent.iterdir()] == ["board.outline.json"]   # no tmp left


def test_sidecar_contract_is_documented_in_the_module_docstring() -> None:
    doc = orz.__doc__
    assert orz.ENV_VAR == "KICAD_EDA_OUTLINE"
    assert orz.ENV_VAR in doc and "swig_build_board" in doc
    for key in orz.SIDECAR_KEYS:
        assert f'"{key}"' in doc
    assert orz.SIDECAR_KEYS == ("version", "board", "source", "dxf", "fingerprint", "warnings")
    assert orz.SIDECAR_VERSION == 1 and all(s in doc for s in orz.SOURCES)


def test_sidecar_write_is_atomic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    side = tmp_path / "board.outline.json"
    side.write_text("OLD", encoding="utf-8")

    def boom(src: str, dst: str) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(orz.os, "replace", boom)
    with pytest.raises(OSError, match="disk full"):
        orz.write_sidecar(resolve_board(_design(_t10()), None), side)
    assert side.read_text(encoding="utf-8") == "OLD"
    assert [p.name for p in tmp_path.iterdir()] == ["board.outline.json"]


def _sidecar(**changes: object) -> dict:
    r = resolve_board(_design(_t10()), None)
    return {**{"version": 1, "board": r.block, "source": r.source, "dxf": None,
               "fingerprint": r.fingerprint, "warnings": []}, **changes}


@pytest.mark.parametrize("data, needle", [
    (_sidecar(version=2), "version"),
    (_sidecar(version=True), "version"),
    ({k: v for k, v in _sidecar().items() if k != "fingerprint"}, "missing"),
    (_sidecar(extra=1), "unknown"),
    (_sidecar(board=[1, 2]), "board"),
    (_sidecar(board={"outline": {"dxf": "a.dxf"}}), "resolved"),
    (_sidecar(source="circle"), "source"),
    (_sidecar(source="dxf"), "dxf"),
    (_sidecar(dxf="a.dxf"), "dxf"),
    (_sidecar(fingerprint="abc"), "fingerprint"),
    (_sidecar(warnings="careful"), "warnings"),
    ([1, 2], "object"),
], ids=["v2", "v-bool", "missing", "unknown", "board-list", "unresolved-dxf", "source",
        "dxf-source-no-path", "path-not-dxf", "fingerprint", "warnings", "list"])
def test_read_sidecar_rejects_bad_contracts(tmp_path: Path, data: object, needle: str) -> None:
    side = tmp_path / "s.outline.json"
    side.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ResolveError, match=needle):
        orz.read_sidecar(side)


def test_read_sidecar_unreadable_or_not_json(tmp_path: Path) -> None:
    with pytest.raises(ResolveError, match="cannot read"):
        orz.read_sidecar(tmp_path / "missing.json")
    bad = tmp_path / "bad.json"
    bad.write_text("{nope", encoding="utf-8")
    with pytest.raises(ResolveError, match="JSON"):
        orz.read_sidecar(bad)


# --- CLI ----------------------------------------------------------------------------------
def _netlist(folder: Path, design: dict) -> Path:
    path = folder / "netlist.json"
    path.write_text(json.dumps(design), encoding="utf-8")
    return path


def test_cli_ok_prints_summary_and_writes_sidecar(tmp_path: Path,
                                                   capsys: pytest.CaptureFixture) -> None:
    netlist = _netlist(tmp_path, _dxf_design(tmp_path))
    side = tmp_path / "x.outline.json"
    assert orz.main([str(netlist), "--sidecar", str(side)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True and out["source"] == "dxf" and out["warnings"] == []
    assert out["bbox"] == pytest.approx([0, 0, 60, 40])
    assert out["fingerprint"] == orz.read_sidecar(side)["fingerprint"]


def test_cli_errors_exit_1(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    netlist = _netlist(tmp_path, _design({"outline": {"dxf": "gone.dxf"}}))
    assert orz.main([str(netlist)]) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False and "DXF file not found" in out["error"]
    assert orz.main([str(tmp_path / "absent.json")]) == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False


def test_cli_as_a_script_from_another_directory(tmp_path: Path) -> None:
    netlist = _netlist(tmp_path, _design(_t10()))
    proc = subprocess.run([sys.executable, str(TOOLS / "outline_resolve.py"), str(netlist)],
                          cwd=tmp_path, capture_output=True, encoding="utf-8",
                          errors="replace", timeout=60, stdin=subprocess.DEVNULL)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    out = json.loads(proc.stdout)
    assert (out["ok"], out["source"], out["is_legacy"]) == (True, "loop", False)
    bad = _netlist(tmp_path, _design({"w": -1, "h": 5}))
    proc = subprocess.run([sys.executable, str(TOOLS / "outline_resolve.py"), str(bad)],
                          capture_output=True, encoding="utf-8", errors="replace",
                          timeout=60, stdin=subprocess.DEVNULL)
    assert proc.returncode == 1 and json.loads(proc.stdout)["ok"] is False


# --- examples and import hygiene ---------------------------------------------------------
def _declares_outline(board: dict) -> bool:
    return any(board.get(k) for k in OUTLINE_KEYS) or board.get("datum") is not None


def test_every_example_resolves(example_netlists: list[Path]) -> None:
    """Every example resolves; a board that declares no outline/corner radius/
    cutouts/datum (all of them today) is legacy with no warnings."""
    problems = []
    for path in example_netlists:
        design = json.loads(path.read_text(encoding="utf-8"))
        before = copy.deepcopy(design)
        r = resolve_board(design, path.parent)
        legacy_expected = not _declares_outline(design.get("board") or {})
        if r.is_legacy is not legacy_expected or (legacy_expected and r.warnings):
            problems.append(f"{path.parent.name}: {r.source} legacy={r.is_legacy} {r.warnings}")
        if design != before:
            problems.append(f"{path.parent.name}: input mutated")
    assert not problems, problems


def test_dxf_io_is_imported_lazily() -> None:
    tree = ast.parse((TOOLS / "outline_resolve.py").read_text(encoding="utf-8"))
    top = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    names = {a.name for n in top for a in n.names} | \
        {n.module for n in top if isinstance(n, ast.ImportFrom)}
    assert not names & {"dxf_io", "ezdxf", "dxf_loops"}, names
    code = ("import sys; sys.path.insert(0, sys.argv[1]); import outline_resolve as o; "
            "r = o.resolve_board({'board': {'w': 60, 'h': 40}}, None); "
            "print(r.source, 'dxf_io' in sys.modules, 'ezdxf' in sys.modules)")
    proc = subprocess.run([sys.executable, "-c", code, str(TOOLS)], capture_output=True,
                          encoding="utf-8", errors="replace", timeout=60,
                          stdin=subprocess.DEVNULL)
    assert proc.stdout.split() == ["rect", "False", "False"], proc.stdout + proc.stderr


def test_sidecar_reads_under_kicad_python(tmp_path: Path) -> None:
    """The builder's interpreter (KiCad 9.0's bundled Python 3.11 locally,
    /usr/bin/python3 in the K10 image) imports the module stdlib-only and reads
    the sidecar back to the same outline."""
    import toolchain as tc

    kpy = tc.kicad_python()
    if not kpy:
        pytest.skip(f"no KiCad pcbnew Python: {tc.kicad_python_error()}")
    r = resolve_board(_design(_t10()), None)
    side = orz.write_sidecar(r, tmp_path / "t10.outline.json")
    code = ("import json, sys; sys.path.insert(0, sys.argv[1]); import kicad_quiet; "
            "import outline_resolve as o; d = o.read_sidecar(sys.argv[2]); "
            "m = o.ol.from_board_block(d['board']); "
            "print(json.dumps({'source': d['source'], 'area': o.ol.area(m), "
            "'dxf_io': 'dxf_io' in sys.modules, 'py': list(sys.version_info[:2])}))")
    proc = subprocess.run([kpy, "-c", code, str(TOOLS), str(side)], capture_output=True,
                          encoding="utf-8", errors="replace", timeout=120,
                          stdin=subprocess.DEVNULL)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["source"] == "loop" and out["dxf_io"] is False
    assert out["area"] == pytest.approx(orz.ol.area(r.outline), abs=1e-9)


def test_resolved_board_is_frozen() -> None:
    r = resolve_board(_design({"w": 60, "h": 40}), None)
    assert isinstance(r, ResolvedBoard)
    with pytest.raises(AttributeError):
        r.source = "loop"  # type: ignore[misc]
