"""fp_compare.compare_footprints and the fp_author split (F2, ROADMAP D1).

Unit tier (venv only, no KiCad):
  * compare_footprints flags the SparkFun LG290P footprint as deviating on the
    inner grid only, with every inner pad numbered "25". The fixture is built
    from its documented geometry; the real file is used when
    $KICAD_EDA_SPARKFUN_LG290P names it;
  * paste-only / aperture pads are compared too, in their own pairing (a paste
    window never pairs with copper): a moved, resized, missing or extra window
    makes two footprints differ;
  * the split: fp_author re-exports the reader and the comparison, fp_reader /
    fp_compare / fp_author_templates import only the stdlib (and each other,
    never fp_author), and compare_footprints stays under 50 lines.
"""
from __future__ import annotations

import ast
import dataclasses
import inspect
import math
import os
import subprocess
import sys
from pathlib import Path
from typing import Callable

import pytest

import fp_author as fa
import fp_author_templates as ft
import fp_compare as fc
import fp_reader as fr

TOOLS = Path(fr.__file__).resolve().parent
# LG290P(03) Hardware Design V1.0, Fig. 18: edge pads 1-12 (left, top to bottom)
LG_EDGE_Y = [-7.0, -5.9, -4.8, -3.7, -2.6, -1.5, -0.4, 2.6, 3.7, 4.8, 5.9, 7.0]


@pytest.fixture(scope="module")
def lg_file(tmp_path_factory) -> Path:
    return fa.write_footprint(ft.lg290p(), tmp_path_factory.mktemp("lg") / "SPS_parts.pretty")


def write_text_fp(tmp_path: Path, name: str, body: str) -> Path:
    path = tmp_path / f"{name}.pretty" / f"{name}.kicad_mod"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'(footprint "{name}" (version 20241229) (layer "F.Cu") (attr smd)\n{body})\n',
                    encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# The SparkFun LG290P
# ---------------------------------------------------------------------------
SF_INNER_X = [-3.0, -1.5, 0.0, 1.5, 3.0]                       # 1.5 pitch (datasheet: 1.4)
SF_INNER_Y = [-7.1, -5.7, -4.3, -2.9, -1.5, 0.0, 1.4, 2.8, 4.2, 5.6, 7.0]   # non-uniform


def sparkfun_like_lg290p() -> str:
    """SparkFun-KiCad-Libraries SparkFun-GNSS.pretty/Quectel_LG290P.kicad_mod as
    documented (toolchain_probe.md section 5): edge pads exact, inner grid off,
    all 55 inner pads numbered "25", some rotated 180."""
    layers = '(layers "F.Cu" "F.Mask" "F.Paste")'
    pads = [f'(pad "{i + 1}" smd rect (at -5.05 {y}) (size 1.5 0.8) {layers})'
            for i, y in enumerate(LG_EDGE_Y)]
    pads += [f'(pad "{24 - i}" smd rect (at 5.05 {y}) (size 1.5 0.8) {layers})'
             for i, y in enumerate(LG_EDGE_Y)]
    for x in SF_INNER_X:
        for y in SF_INNER_Y:
            shape = "circle" if (x, y) == (-3.0, -7.1) else "rect"
            rot = " 180" if y < 0 and shape == "rect" else ""
            pads.append(f'(pad "25" smd {shape} (at {x} {y}{rot}) (size 1 1) {layers})')
    rects = "".join(f'(fp_rect (start -{x} -{y}) (end {x} {y}) (stroke (width {w}) '
                    f'(type default)) (fill no) (layer "{layer}"))\n'
                    for x, y, w, layer in ((6.3, 8.2, 0.1, "F.SilkS"), (6.5, 8.4, 0.05, "F.CrtYd"),
                                           (6.1, 8, 0.1, "F.Fab")))
    return ('(footprint "Quectel_LG290P" (version 20241229) (generator "pcbnew") (layer "F.Cu")\n'
            '(attr smd)\n' + rects + "\n".join(pads) + "\n)\n")


@pytest.fixture
def sparkfun_fixture(tmp_path) -> Path:
    path = tmp_path / "SF.pretty" / "Quectel_LG290P.kicad_mod"
    path.parent.mkdir()
    path.write_text(sparkfun_like_lg290p(), encoding="utf-8")
    return path


def assert_flags_sparkfun_inner_grid(diff: dict) -> None:
    assert not diff["identical"] and diff["pads"] == [79, 79] and diff["matched"] == 79
    deviating = {d["a"] for d in diff["deviations"]}
    assert deviating == {str(n) for n in range(25, 80)}            # the whole inner grid ...
    assert not deviating & {str(n) for n in range(1, 25)}          # ... and no edge pad
    moved = {d["a"] for d in diff["deviations"] if "moved" in d["issues"]}
    # only the x = 0 column (col 2: pads 47-57) at y >= 0 (rows 5-10) is on the datasheet grid
    on_grid = {str(25 + 11 * 2 + row) for row in range(5, 11)}
    assert on_grid == {"52", "53", "54", "55", "56", "57"}
    assert moved == {str(n) for n in range(25, 80)} - on_grid
    assert diff["max_offset"] == pytest.approx(math.hypot(0.2, 0.1))
    assert diff["numbering"]["duplicates_b"] == {"25": 55} and diff["numbering"]["renumbered"] == 54
    assert diff["numbering"]["only_in_a"] == [str(n) for n in range(26, 80)]
    assert "B repeats pad numbers \"25\" x55" in diff["summary"]
    assert diff["apertures"]["pads"] == [0, 0]                       # no paste-only pads


def test_compare_flags_the_sparkfun_inner_grid(lg_file, sparkfun_fixture):
    assert_flags_sparkfun_inner_grid(fa.compare_footprints(lg_file, sparkfun_fixture))


def test_compare_identical_and_unmatched(lg_file, tmp_path):
    same = fa.compare_footprints(lg_file, lg_file)
    assert same["identical"] and same["summary"] == "identical" and same["matched"] == 79
    spec = ft.lg290p()
    path = fa.write_footprint(fa.FootprintSpec(spec.name, spec.pads[:-1], spec.body),
                              tmp_path / "x.pretty")
    diff = fa.compare_footprints(lg_file, path)
    assert diff["unmatched_a"] == [{"number": "79", "at": [2.8, 7.0]}] and not diff["identical"]
    assert diff["numbering"]["only_in_a"] == ["79"] and "1 pads only in A" in diff["summary"]


def test_tolerance_match_radius_and_duplicates_in_a(tmp_path):
    pad = '(pad "{n}" smd rect (at {x} 0) (size 1 1) (layers "F.Cu"))\n'
    a = write_text_fp(tmp_path, "A", pad.format(n="1", x=0) + pad.format(n="1", x=3))
    b = write_text_fp(tmp_path, "B", pad.format(n="1", x=0.0005) + pad.format(n="1", x=4.5))
    diff = fc.compare_footprints(a, b)
    assert diff["matched"] == 1 and diff["deviations"] == []         # 0.5 um is within tolerance
    assert diff["unmatched_a"] == [{"number": "1", "at": [3.0, 0.0]}]   # 1.5 mm > match_radius
    assert diff["numbering"]["duplicates_a"] == {"1": 2} == diff["numbering"]["duplicates_b"]
    assert fc.compare_footprints(a, b, match_radius=2.0)["deviations"][0]["issues"] == ["moved"]


SPARKFUN_REAL = os.environ.get("KICAD_EDA_SPARKFUN_LG290P", "")


@pytest.mark.skipif(not Path(SPARKFUN_REAL).is_file(),
                    reason="set KICAD_EDA_SPARKFUN_LG290P to SparkFun's Quectel_LG290P.kicad_mod")
def test_compare_flags_the_real_sparkfun_file(lg_file, sparkfun_fixture):
    # the fixture is faithful to the real file ...
    assert fa.compare_footprints(sparkfun_fixture, SPARKFUN_REAL)["identical"]
    assert_flags_sparkfun_inner_grid(fa.compare_footprints(lg_file, SPARKFUN_REAL))


# ---------------------------------------------------------------------------
# Paste-only / aperture pads
# ---------------------------------------------------------------------------
def qfn_paste() -> fa.FootprintSpec:
    ep = ft.ExposedPad(1.7, 1.7, paste_grid=(2, 2), paste_coverage=0.65)
    return ft.qfn("Q", body=fa.Body(3.0, 3.0), pitch=0.5, pins_x=4, pins_y=4, span_x=2.925,
                  span_y=2.925, pad_w=0.25, pad_l=0.825, ep=ep)


def with_windows(spec: fa.FootprintSpec,
                 change: Callable[[list[fa.Pad]], list[fa.Pad]]) -> fa.FootprintSpec:
    """spec with change(windows) in place of its paste-only pads."""
    windows = [p for p in spec.pads if not ft.has_copper(p)]
    copper = [p for p in spec.pads if ft.has_copper(p)]
    return dataclasses.replace(spec, pads=tuple(copper + list(change(windows))))


def test_identical_paste_windows_are_matched(tmp_path):
    path = fa.write_footprint(qfn_paste(), tmp_path / "a.pretty")
    diff = fc.compare_footprints(path, path)
    assert diff["identical"] and diff["pads"] == [17, 17]          # copper: 16 pins + the EP
    assert diff["apertures"]["pads"] == [4, 4] and diff["apertures"]["matched"] == 4
    assert diff["apertures"]["deviations"] == []


@pytest.mark.parametrize(("change", "issue"), [
    (lambda w: [dataclasses.replace(w[0], w=w[0].w * 0.9)] + w[1:], "resized"),
    (lambda w: [dataclasses.replace(w[0], x=w[0].x + 0.05)] + w[1:], "moved"),
    (lambda w: [dataclasses.replace(w[0], shape="rect")] + w[1:], "shape"),
    (lambda w: [dataclasses.replace(w[0], layers=("B.Paste",))] + w[1:], "layers"),
])
def test_a_changed_paste_window_makes_the_footprints_differ(tmp_path, change, issue):
    a = fa.write_footprint(qfn_paste(), tmp_path / "a.pretty")
    b = fa.write_footprint(with_windows(qfn_paste(), change), tmp_path / "b.pretty")
    diff = fc.compare_footprints(a, b)
    assert not diff["identical"] and diff["deviations"] == []       # copper is unchanged
    (dev,) = diff["apertures"]["deviations"]
    assert issue in dev["issues"], dev
    assert "apertures: 1 of 4 matched differ" in diff["summary"]


def test_missing_and_extra_paste_windows(tmp_path):
    a = fa.write_footprint(qfn_paste(), tmp_path / "a.pretty")
    b = fa.write_footprint(with_windows(qfn_paste(), lambda w: w[1:]), tmp_path / "b.pretty")
    diff = fc.compare_footprints(a, b)
    assert not diff["identical"] and len(diff["apertures"]["unmatched_a"]) == 1
    back = fc.compare_footprints(b, a)
    assert not back["identical"] and len(back["apertures"]["unmatched_b"]) == 1
    assert "1 only in B" in back["summary"]


def test_apertures_never_pair_with_copper(tmp_path):
    copper = '(pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu" "F.Mask"))\n'
    paste = '(pad "" smd rect (at 0 0) (size 1 1) (layers "F.Paste"))\n'
    mask = '(pad "" smd rect (at 3 0) (size 1 1) (layers "F.Mask"))\n'   # a mask-only opening
    a = write_text_fp(tmp_path, "A", copper + mask)
    b = write_text_fp(tmp_path, "B", copper + paste + mask)
    diff = fc.compare_footprints(a, b)
    assert diff["matched"] == 1 and diff["deviations"] == []
    assert diff["apertures"]["pads"] == [1, 2] and diff["apertures"]["matched"] == 1
    assert diff["apertures"]["unmatched_b"] == [{"number": "", "at": [0.0, 0.0]}]
    assert not diff["identical"]


# ---------------------------------------------------------------------------
# The split
# ---------------------------------------------------------------------------
FP_AUTHOR_API = ("render", "write_footprint", "summary", "parse_sexpr", "read_footprint",
                 "ParsedFootprint", "geometry_problems", "static_problems", "oldest_kicad_cli",
                 "kicad_parse_problems", "validate", "LOAD_CHILD", "pcbnew_load",
                 "compare_footprints")


def test_fp_author_keeps_its_public_api():
    assert set(FP_AUTHOR_API) <= set(fa.__all__)
    assert all(hasattr(fa, name) for name in fa.__all__)
    for name in ("parse_sexpr", "read_footprint", "parse_footprint", "ParsedFootprint",
                 "ParsedPad", "FootprintParseError"):
        assert getattr(fa, name) is getattr(fr, name), name
    assert fa.compare_footprints is fc.compare_footprints


def test_the_acceptance_import_line_runs_in_a_fresh_interpreter():
    code = ("from fp_author import render, validate, kicad_parse_problems, pcbnew_load, "
            "compare_footprints, read_footprint")
    r = subprocess.run([sys.executable, "-c", code], cwd=TOOLS, capture_output=True,
                       stdin=subprocess.DEVNULL, encoding="utf-8", errors="replace", timeout=60)
    assert r.returncode == 0, r.stderr


@pytest.mark.parametrize("module", ["fp_reader.py", "fp_compare.py", "fp_author_templates.py"])
def test_split_modules_are_stdlib_only_and_never_import_fp_author(module):
    tree = ast.parse((TOOLS / module).read_text(encoding="utf-8"))
    mods = {alias.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import)
            for alias in n.names}
    mods |= {n.module.split(".")[0] for n in ast.walk(tree)
             if isinstance(n, ast.ImportFrom) and n.module}
    assert mods - set(sys.stdlib_module_names) <= {"fp_reader", "fp_author_templates"}, mods


def test_compare_footprints_stays_under_50_lines():
    assert len(inspect.getsource(fc.compare_footprints).splitlines()) < 50
