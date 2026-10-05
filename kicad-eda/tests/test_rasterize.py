"""server/tools/rasterize.py: PDF / image schematics -> PNG pages and tiles
for the vision path (ROADMAP_DECISIONS D4).

Unit tier, no PyMuPDF needed: the tile grid, page-list parsing, settings
validation and the actionable error when PyMuPDF is missing (simulated by
blocking the import). Unit tier with PyMuPDF (skipped when it is not
installed; it is an optional dependency, server/requirements-optional.txt):
synthetic PDFs and images built in the test with PyMuPDF itself, checked
pixel by pixel against the tile coordinates in the manifest.

Live tier: a KiCad demo schematic exported to PDF with that install's
kicad-cli, rasterized and tiled; skipped without KiCad or PyMuPDF.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import rasterize as rz
from fixtures.kicad_install import KICAD, requires_kicad_cli

try:
    import pymupdf
except ImportError:          # optional dependency: the PyMuPDF tests skip
    pymupdf = None

requires_pymupdf = pytest.mark.skipif(pymupdf is None, reason="PyMuPDF not installed "
                                      "(server/requirements-optional.txt)")

BLACK = (0, 0, 0)
WHITE = (255, 255, 255)


# ----------------------------------------------------------------------------
# Tile grid (pure)
# ----------------------------------------------------------------------------
@pytest.mark.parametrize("length, tile, overlap, expected", [
    (500, 1000, 100, (0,)),                    # fits: one tile
    (1000, 1000, 100, (0,)),                   # exactly one tile
    (1001, 1000, 100, (0, 1)),                 # one pixel over: two nearly full overlaps
    (1900, 1000, 100, (0, 900)),               # stride 900: two tiles meet the overlap exactly
    (1901, 1000, 100, (0, 450, 901)),          # one more pixel: three, spread evenly
])
def test_tile_starts(length: int, tile: int, overlap: int, expected: tuple[int, ...]) -> None:
    assert rz.tile_starts(length, tile, overlap) == expected


@pytest.mark.parametrize("length", [1, 255, 1567, 1568, 1569, 3000, 3508, 4961, 12345])
def test_tile_starts_cover_with_at_least_the_overlap(length: int) -> None:
    tile, overlap = 1568, 200
    starts = rz.tile_starts(length, tile, overlap)
    assert starts[0] == 0
    assert min(length, starts[-1] + tile) == length              # the last tile ends at the edge
    assert list(starts) == sorted(set(starts))
    for a, b in zip(starts, starts[1:], strict=False):          # neighbouring pairs
        assert a + tile - b >= overlap                           # neighbours overlap enough
    if length > tile:                                            # the fewest tiles that can
        assert len(starts) == -(-(length - overlap) // (tile - overlap))


def test_tile_grid_boxes_rows_and_columns() -> None:
    boxes = rz.tile_grid(2500, 1200, 1000, 100)
    assert [(b.row, b.col) for b in boxes] == [(r, c) for r in range(2) for c in range(3)]
    assert {(b.x0, b.x1) for b in boxes} == {(0, 1000), (750, 1750), (1500, 2500)}
    assert {(b.y0, b.y1) for b in boxes} == {(0, 1000), (200, 1200)}
    assert all(b.width == 1000 and b.height == 1000 for b in boxes)
    assert boxes[0].as_list() == [0, 0, 1000, 1000]


def test_tile_grid_small_page_is_one_tile() -> None:
    (box,) = rz.tile_grid(640, 480, 1568, 200)
    assert box.as_list() == [0, 0, 640, 480] and (box.row, box.col) == (0, 0)


@pytest.mark.parametrize("kwargs, message", [
    ({"dpi": 10}, "dpi"),
    ({"dpi": 5000}, "dpi"),
    ({"tile_px": 100}, "tile"),
    ({"overlap_px": -1}, "overlap"),
    ({"tile_px": 1000, "overlap_px": 500}, "overlap"),
    ({"dpi": 300.5}, "dpi"),
])
def test_check_settings_rejects(kwargs: dict, message: str) -> None:
    settings = {"dpi": 300, "tile_px": 1568, "overlap_px": 200, **kwargs}
    with pytest.raises(ValueError, match=message):
        rz.check_settings(**settings)


def test_check_settings_accepts_no_tiling() -> None:
    rz.check_settings(dpi=150, tile_px=0, overlap_px=0)


@pytest.mark.parametrize("spec, expected", [
    ("1", (1,)), ("1,3", (1, 3)), ("2-4", (2, 3, 4)), (" 1, 3-4 ,3", (1, 3, 4)),
])
def test_parse_pages(spec: str, expected: tuple[int, ...]) -> None:
    assert rz.parse_pages(spec) == expected


@pytest.mark.parametrize("spec", ["", "0", "a", "3-1", "1,,2", "-2"])
def test_parse_pages_rejects(spec: str) -> None:
    with pytest.raises(ValueError, match="page"):
        rz.parse_pages(spec)


# ----------------------------------------------------------------------------
# PyMuPDF missing: a clear, actionable error (criterion 2)
# ----------------------------------------------------------------------------
@pytest.fixture
def no_pymupdf(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``import pymupdf`` fail the way it does when it is not installed."""
    monkeypatch.setitem(sys.modules, "pymupdf", None)


def test_missing_pymupdf_raises_actionable_error(tmp_path: Path, no_pymupdf: None) -> None:
    src = tmp_path / "sheet.pdf"
    src.write_bytes(b"%PDF-1.4\n%%EOF\n")
    with pytest.raises(rz.MissingDependencyError) as err:
        rz.rasterize(src, tmp_path / "out")
    text = str(err.value)
    assert "PyMuPDF" in text and "optional" in text
    assert "pip install" in text and "requirements-optional.txt" in text
    assert sys.executable in text                       # the interpreter to install into
    assert not (tmp_path / "out").exists()              # nothing written


def test_missing_pymupdf_is_a_runtime_error_and_names_the_package(no_pymupdf: None) -> None:
    with pytest.raises(RuntimeError, match="pymupdf"):
        rz.load_pymupdf()


def test_missing_pymupdf_cli_exits_2(tmp_path: Path, no_pymupdf: None,
                                     capsys: pytest.CaptureFixture[str]) -> None:
    src = tmp_path / "sheet.png"
    src.write_bytes(b"\x89PNG\r\n\x1a\n")
    assert rz.main([str(src), str(tmp_path / "out")]) == 2
    assert "requirements-optional.txt" in capsys.readouterr().err


def test_optional_requirements_file_pins_pymupdf() -> None:
    lines = [ln.strip() for ln in rz.OPTIONAL_REQUIREMENTS.read_text(encoding="utf-8").splitlines()]
    assert any(re.fullmatch(r"pymupdf==\d+\.\d+\.\d+", ln) for ln in lines), lines


# ----------------------------------------------------------------------------
# Input validation before PyMuPDF is touched
# ----------------------------------------------------------------------------
def test_missing_source_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="nope.pdf"):
        rz.rasterize(tmp_path / "nope.pdf", tmp_path / "out")


def test_unsupported_suffix(tmp_path: Path) -> None:
    src = tmp_path / "notes.txt"
    src.write_text("hello", encoding="utf-8")
    with pytest.raises(ValueError, match=r"\.txt.*\.pdf"):
        rz.rasterize(src, tmp_path / "out")


# ----------------------------------------------------------------------------
# Synthetic documents (PyMuPDF builds them)
# ----------------------------------------------------------------------------
A3_LANDSCAPE = (1190.55, 841.89)       # pt
A6_PORTRAIT = (297.64, 419.53)
# filled black squares, in page points: (x0, y0, x1, y1)
MARKS = {"left": (100.0, 100.0, 130.0, 130.0),
         "seam": (330.0, 300.0, 360.0, 330.0),       # sits in the first column overlap at 100 dpi
         "corner": (1150.0, 800.0, 1180.0, 830.0)}


def _make_pdf(path: Path, *, rotate_second: int = 0) -> Path:
    doc = pymupdf.open()
    page = doc.new_page(width=A3_LANDSCAPE[0], height=A3_LANDSCAPE[1])
    for rect in MARKS.values():
        page.draw_rect(pymupdf.Rect(rect), color=BLACK, fill=BLACK)
    page.insert_text((200, 500), "R1 10k U3", fontsize=14)
    small = doc.new_page(width=A6_PORTRAIT[0], height=A6_PORTRAIT[1])
    small.draw_rect(pymupdf.Rect(20, 40, 60, 60), color=BLACK, fill=BLACK)
    small.insert_text((20, 100), "C7", fontsize=12)
    if rotate_second:
        small.set_rotation(rotate_second)
    doc.save(str(path))
    doc.close()
    return path


def _pix(path: str | Path) -> "pymupdf.Pixmap":
    return pymupdf.Pixmap(str(path))


def _rgb(pix: "pymupdf.Pixmap", x: int, y: int) -> tuple[int, ...]:
    return tuple(pix.pixel(x, y)[:3])


def _px(value_pt: float, dpi: int) -> int:
    return int(value_pt * dpi / 72)


def _tiles_holding(page: dict, x: int, y: int) -> list[dict]:
    return [t for t in page["tiles"]
            if t["box_px"][0] <= x < t["box_px"][2] and t["box_px"][1] <= y < t["box_px"][3]]


@requires_pymupdf
def test_pdf_pages_tiles_and_manifest(tmp_path: Path) -> None:
    src = _make_pdf(tmp_path / "sheet one.pdf")
    out = tmp_path / "raster"
    man = rz.rasterize(src, out, dpi=100, tile_px=700, overlap_px=64)

    assert man["source"]["kind"] == "pdf" and man["source"]["page_count"] == 2
    assert man["source"]["sha256"] == hashlib.sha256(src.read_bytes()).hexdigest()
    assert man["source"]["path"] == str(src.resolve())
    assert man["tool"] == f"pymupdf {pymupdf.__version__}"
    assert (man["dpi"], man["tile_px"], man["overlap_px"]) == (100, 700, 64)
    assert Path(man["manifest"]).is_file()
    assert json.loads(Path(man["manifest"]).read_text(encoding="utf-8")) == man

    p1, p2 = man["pages"]
    w, h = p1["width_px"], p1["height_px"]
    assert (w, h) == (1654, 1170)               # ceil(1190.55 * 100/72), ceil(841.89 * 100/72)
    assert p1["size_pt"] == [round(A3_LANDSCAPE[0], 2), round(A3_LANDSCAPE[1], 2)]
    grid = rz.tile_grid(w, h, 700, 64)
    assert p1["grid"] == [2, 3]
    assert [t["box_px"] for t in p1["tiles"]] == [b.as_list() for b in grid]
    assert p1["image"] is None                         # tiled: no full-resolution page file
    for tile in p1["tiles"]:
        pix = _pix(tile["path"])
        x0, y0, x1, y1 = tile["box_px"]
        assert (pix.width, pix.height) == (x1 - x0, y1 - y0)
        assert tile["box_pt"] == [round(v * 72 / 100, 2) for v in tile["box_px"]]
        assert Path(tile["path"]).parent == out and " " not in Path(tile["path"]).name

    # every mark is black at its page-raster position in EVERY tile that covers it
    for name, (x0, y0, x1, y1) in MARKS.items():
        cx, cy = _px((x0 + x1) / 2, 100), _px((y0 + y1) / 2, 100)
        holders = _tiles_holding(p1, cx, cy)
        assert holders, name
        for tile in holders:
            tx, ty = tile["box_px"][:2]
            assert _rgb(_pix(tile["path"]), cx - tx, cy - ty) == BLACK, (name, tile["path"])
        if name == "seam":
            assert len(holders) == 2                   # the overlap shows it whole twice
    # and white just outside the left mark
    (tile,) = _tiles_holding(p1, _px(90, 100), _px(90, 100))
    assert _rgb(_pix(tile["path"]), _px(90, 100), _px(90, 100)) == WHITE

    overview = _pix(p1["overview"])
    assert max(overview.width, overview.height) == 700
    assert p1["overview_scale"] == pytest.approx(overview.width / w, abs=1e-3)

    # the small page fits one tile: that tile IS the full page image
    assert p2["grid"] == [1, 1] and p2["image"] == p2["overview"] == p2["tiles"][0]["path"]
    assert p2["tiles"][0]["box_px"] == [0, 0, p2["width_px"], p2["height_px"]]
    assert p2["overview_scale"] == 1.0
    pix2 = _pix(p2["image"])
    assert _rgb(pix2, _px(40, 100), _px(50, 100)) == BLACK


@requires_pymupdf
def test_pdf_text_layer_words_in_raster_pixels(tmp_path: Path) -> None:
    man = rz.rasterize(_make_pdf(tmp_path / "s.pdf"), tmp_path / "o", dpi=144, tile_px=0)
    p1 = man["pages"][0]
    words = json.loads(Path(p1["words"]).read_text(encoding="utf-8"))
    assert p1["word_count"] == len(words) == 3
    by_text = {w[4]: w for w in words}
    assert set(by_text) == {"R1", "10k", "U3"}
    x0, y0, x1, y1, _ = by_text["R1"]
    assert x0 == pytest.approx(200 * 2, abs=2)         # 144 dpi = 2 px per pt
    assert y0 < 500 * 2 < y1                           # the baseline at y = 500 pt
    p2 = man["pages"][1]
    assert [w[4] for w in json.loads(Path(p2["words"]).read_text(encoding="utf-8"))] == ["C7"]


@requires_pymupdf
def test_pdf_no_text_option(tmp_path: Path) -> None:
    man = rz.rasterize(_make_pdf(tmp_path / "s.pdf"), tmp_path / "o", dpi=72, text=False)
    assert all(p["words"] is None and p["word_count"] == 0 for p in man["pages"])


@requires_pymupdf
def test_untiled_page_is_one_full_image(tmp_path: Path) -> None:
    man = rz.rasterize(_make_pdf(tmp_path / "s.pdf"), tmp_path / "o", dpi=100, tile_px=0)
    p1 = man["pages"][0]
    assert p1["grid"] == [1, 1] and p1["image"] == p1["tiles"][0]["path"]
    pix = _pix(p1["image"])
    assert (pix.width, pix.height) == (p1["width_px"], p1["height_px"]) == (1654, 1170)
    cx, cy = _px(1165, 100), _px(815, 100)
    assert _rgb(pix, cx, cy) == BLACK


@requires_pymupdf
def test_page_selection(tmp_path: Path) -> None:
    man = rz.rasterize(_make_pdf(tmp_path / "s.pdf"), tmp_path / "o", dpi=72, pages=[2])
    assert [p["page"] for p in man["pages"]] == [2]
    assert Path(man["pages"][0]["image"]).name == "s_p2.png"
    with pytest.raises(ValueError, match="page 3.*2 page"):
        rz.rasterize(tmp_path / "s.pdf", tmp_path / "o", pages=[3])


@requires_pymupdf
def test_rotated_page_marks_and_words_follow_the_rotation(tmp_path: Path) -> None:
    src = _make_pdf(tmp_path / "s.pdf", rotate_second=90)
    man = rz.rasterize(src, tmp_path / "o", dpi=72, tile_px=0)
    p2 = man["pages"][1]
    assert p2["rotation"] == 90
    assert (p2["width_px"], p2["height_px"]) == (420, 298)     # landscape once rotated
    # unrotated (x, y) -> displayed (H - y, x) for a 90 degree page of height H
    height_pt = A6_PORTRAIT[1]
    pix = _pix(p2["image"])
    assert _rgb(pix, int(height_pt - 50), 40) == BLACK          # the square at (20..60, 40..60)
    assert _rgb(pix, 40, 50) == WHITE                           # not where it sits unrotated
    ((x0, y0, x1, y1, text),) = json.loads(Path(p2["words"]).read_text(encoding="utf-8"))
    assert text == "C7"
    with pymupdf.open(str(src)) as doc:                        # PyMuPDF's own transform
        page = doc[1]
        expected = pymupdf.Rect(page.get_text("words")[0][:4]) * page.rotation_matrix
    assert [x0, y0, x1, y1] == pytest.approx(list(expected), abs=0.11)
    assert x0 < height_pt - 100 < x1                            # baseline 100 pt -> x = H - 100


@requires_pymupdf
def test_tile_limit_guard(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="tiles.*dpi"):
        rz.rasterize(_make_pdf(tmp_path / "s.pdf"), tmp_path / "o", dpi=1200, tile_px=256,
                     overlap_px=0)


@requires_pymupdf
def test_encrypted_pdf_is_rejected(tmp_path: Path) -> None:
    src = tmp_path / "locked.pdf"
    doc = pymupdf.open()
    doc.new_page()
    doc.save(str(src), encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="u", owner_pw="o")
    doc.close()
    with pytest.raises(rz.RasterizeError, match="password"):
        rz.rasterize(src, tmp_path / "o")


@requires_pymupdf
@pytest.mark.parametrize("name, data", [
    ("broken.pdf", b"%PDF-1.4\n%%EOF\n"),
    ("broken.png", b"this is not an image"),
])
def test_unreadable_document_is_a_rasterize_error(tmp_path: Path, name: str, data: bytes) -> None:
    src = tmp_path / name
    src.write_bytes(data)
    with pytest.raises(rz.RasterizeError, match=re.escape(name)):
        rz.rasterize(src, tmp_path / "o")


# ----------------------------------------------------------------------------
# Images: native pixels, passed through or tiled
# ----------------------------------------------------------------------------
def _pattern_image(path: Path, w: int, h: int, *, dpi: int = 96) -> "pymupdf.Pixmap":
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, w, h), False)
    pix.clear_with(255)
    for y in range(0, h, 7):
        for x in range(0, w, 5):
            pix.set_pixel(x, y, (x % 256, y % 256, (x + y) % 256))
    pix.set_dpi(dpi, dpi)
    pix.save(str(path))
    return pymupdf.Pixmap(str(path))


def _crop(src: "pymupdf.Pixmap", box: list[int]) -> bytes:
    irect = pymupdf.IRect(*box)
    out = pymupdf.Pixmap(src.colorspace, irect, False)
    out.copy(src, irect)
    return out.samples


@requires_pymupdf
@pytest.mark.parametrize("dpi", [96, 300])
def test_large_image_tiles_are_exact_crops(tmp_path: Path, dpi: int) -> None:
    src = tmp_path / "scan.png"
    original = _pattern_image(src, 900, 500, dpi=dpi)
    man = rz.rasterize(src, tmp_path / "o", tile_px=400, overlap_px=50)
    assert man["source"]["kind"] == "image" and man["dpi"] is None
    (page,) = man["pages"]
    assert (page["width_px"], page["height_px"]) == (900, 500)
    assert page["grid"] == [2, 3] and page["words"] is None and page["size_pt"] is None
    for tile in page["tiles"]:
        assert "box_pt" not in tile
        assert _pix(tile["path"]).samples == _crop(original, tile["box_px"]), tile


@requires_pymupdf
def test_small_image_passes_through_unchanged(tmp_path: Path) -> None:
    src = tmp_path / "photo.png"
    original = _pattern_image(src, 320, 200)
    man = rz.rasterize(src, tmp_path / "o")
    (page,) = man["pages"]
    assert page["grid"] == [1, 1] and page["image"] == page["tiles"][0]["path"]
    assert _pix(page["image"]).samples == original.samples


@requires_pymupdf
def test_jpeg_input_becomes_png(tmp_path: Path) -> None:
    png = tmp_path / "a.png"
    _pattern_image(png, 200, 120)
    jpg = tmp_path / "a.jpg"
    _pix(png).save(str(jpg))
    man = rz.rasterize(jpg, tmp_path / "o")
    out = Path(man["pages"][0]["image"])
    assert out.suffix == ".png" and out.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    assert (_pix(out).width, _pix(out).height) == (200, 120)


@requires_pymupdf
def test_transparent_image_is_flattened_on_white(tmp_path: Path) -> None:
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 40, 20), True)
    for y in range(20):
        for x in range(40):
            pix.set_pixel(x, y, (0, 0, 0, 255) if y == 10 else (255, 0, 0, 0))
    src = tmp_path / "alpha.png"
    pix.save(str(src))
    page = rz.rasterize(src, tmp_path / "o")["pages"][0]
    out = _pix(page["image"])
    assert not out.alpha
    assert _rgb(out, 3, 3) == WHITE and _rgb(out, 3, 10) == BLACK


@requires_pymupdf
def test_cli_prints_the_manifest(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    src = _make_pdf(tmp_path / "s.pdf")
    rc = rz.main([str(src), str(tmp_path / "o"), "--dpi", "72", "--tile", "512",
                  "--overlap", "32", "--pages", "1", "--no-text"])
    assert rc == 0
    man = json.loads(capsys.readouterr().out)
    assert [p["page"] for p in man["pages"]] == [1] and man["tile_px"] == 512
    assert man["pages"][0]["words"] is None


def test_cli_bad_settings_exit_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    src = tmp_path / "s.pdf"
    src.write_bytes(b"%PDF-1.4\n%%EOF\n")
    assert rz.main([str(src), str(tmp_path / "o"), "--tile", "100"]) == 2
    assert "tile" in capsys.readouterr().err


# ----------------------------------------------------------------------------
# Live: a real KiCad schematic PDF (criterion 1)
# ----------------------------------------------------------------------------
def _sheet_refs(sch: Path) -> set[str]:
    """References of the placed symbols in a .kicad_sch (power symbols '#...' excluded)."""
    text = sch.read_text(encoding="utf-8")
    refs = set(re.findall(r'\(reference "([^"#][^"]*)"\)', text))
    return refs or set(re.findall(r'\(property "Reference" "([^"#][^"]*)"', text))


@requires_kicad_cli
@requires_pymupdf
def test_live_kicad_schematic_pdf(tmp_path: Path) -> None:
    demo = KICAD.root / "share" / "kicad" / "demos" / "pic_programmer"
    if not demo.is_dir():
        pytest.skip(f"KiCad demo not installed: {demo}")
    project = tmp_path / "pic_programmer"
    shutil.copytree(demo, project)
    pdf = tmp_path / "pic_programmer.pdf"
    r = subprocess.run([str(KICAD.cli), "sch", "export", "pdf", "-o", str(pdf),
                        str(project / "pic_programmer.kicad_sch")],
                       capture_output=True, text=True, timeout=300, stdin=subprocess.DEVNULL)
    assert r.returncode == 0 and pdf.is_file(), r.stdout + r.stderr

    man = rz.rasterize(pdf, tmp_path / "raster", dpi=300)
    assert man["source"]["page_count"] == 2 and len(man["pages"]) == 2     # root + pic_sockets
    for page in man["pages"]:
        w, h = page["width_px"], page["height_px"]
        assert max(w, h) > rz.DEFAULT_TILE_PX          # A4 at 300 dpi needs tiling
        assert page["grid"][0] * page["grid"][1] == len(page["tiles"]) > 1
        assert [t["box_px"] for t in page["tiles"]] == [
            b.as_list() for b in rz.tile_grid(w, h, rz.DEFAULT_TILE_PX, rz.DEFAULT_OVERLAP_PX)]
        for tile in page["tiles"]:
            pix = _pix(tile["path"])
            x0, y0, x1, y1 = tile["box_px"]
            assert (pix.width, pix.height) == (x1 - x0, y1 - y0)
            share, _colour = pix.color_topusage()
            assert share < 0.995, f"blank tile {tile['path']}"
        assert page["word_count"] > 100

    words = {w[4] for page in man["pages"]
             for w in json.loads(Path(page["words"]).read_text(encoding="utf-8"))}
    refs = _sheet_refs(project / "pic_programmer.kicad_sch") | _sheet_refs(
        project / "pic_sockets.kicad_sch")
    assert len(refs) > 20
    found = refs & words
    assert len(found) >= 0.8 * len(refs), sorted(refs - words)
