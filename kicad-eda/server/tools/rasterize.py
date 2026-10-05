#!/usr/bin/env python3
"""PDF / image schematics -> PNG pages and overlapping tiles (ROADMAP_DECISIONS D4).

The first step of the vision path: a schematic that exists only as a PDF or an
image is rasterized so a reader (the ``schematic-reader`` subagent) can look
at it at a resolution where pin numbers and values stay legible.

* A PDF page renders at ``dpi``; an image keeps its native pixels (it is
  passed through, re-encoded as PNG, transparency flattened on white).
* A page larger than ``tile_px`` on either edge is cut into a grid of
  overlapping tiles, each at most ``tile_px`` square, plus one downscaled
  ``overview`` of the whole sheet. Neighbouring tiles overlap by at least
  ``overlap_px`` so a label or small part that straddles a seam is whole in
  one of them. A page that fits is one image that is its own single tile.
* PDFs with a text layer (every CAD export) also get a ``words`` file: each
  word with its box, in the same pixel frame as the tiles, which gives exact
  references, values and pin numbers where the PDF has them.

Coordinates: ``box_px`` = ``[x0, y0, x1, y1]`` in pixels of the full page
raster (origin top-left, y down, x1/y1 exclusive); a tile pixel (u, v) is page
pixel (x0 + u, y0 + v). ``box_pt`` is the same box in PDF points of the page as
displayed (rotation applied); pixel = point * dpi / 72. Words are
``[x0, y0, x1, y1, text]`` in page pixels.

``rasterize(src, out_dir, ...)`` writes ``<stem>_p<N>.png`` (a page that
fits), or ``<stem>_p<N>_overview.png`` + ``<stem>_p<N>_r<row>c<col>.png``
(tiled), ``<stem>_p<N>_words.json``, and the manifest
``<stem>_raster.json``, and returns the manifest. Files from an earlier run
with other settings are not deleted: the manifest lists the current set.

PyMuPDF (``pymupdf``) is an OPTIONAL dependency
(server/requirements-optional.txt), imported lazily: without it this module
still imports and its pure helpers work; rasterize() raises
MissingDependencyError saying how to install it.

CLI: ``rasterize.py SRC OUT_DIR [--dpi N] [--tile N] [--overlap N]
[--pages 1,3-5] [--no-text]`` prints the manifest; exit 0, or 2 on error.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

SERVER_DIR = Path(__file__).resolve().parents[1]
OPTIONAL_REQUIREMENTS = SERVER_DIR / "requirements-optional.txt"

DEFAULT_DPI = 300           # KiCad's 1.27 mm field text is 15 px tall: pin numbers stay legible
DEFAULT_TILE_PX = 1568      # the long edge Claude's vision input keeps without downscaling
DEFAULT_OVERLAP_PX = 200    # 17 mm at 300 dpi: a label or small part on a seam is whole once
MIN_DPI, MAX_DPI = 36, 1200
MIN_TILE_PX = 256
MAX_TILES_PER_PAGE = 400
MAX_UNTILED_PIXELS = 64_000_000     # tile_px = 0 renders the whole page in one pixmap
PT_PER_INCH = 72
PDF, IMAGE = "pdf", "image"
PDF_SUFFIXES = (".pdf",)
IMAGE_SUFFIXES = (".bmp", ".gif", ".jpeg", ".jpg", ".png", ".tif", ".tiff")
MANIFEST_SUFFIX = "_raster.json"
COORDINATES = ("box_px = [x0, y0, x1, y1] in pixels of the full page raster (origin top-left, "
               "y down, x1/y1 exclusive); tile pixel (u, v) = page pixel (x0 + u, y0 + v); "
               "box_pt = the same box in PDF points of the page as displayed; "
               "pixel = point * dpi / 72; words = [x0, y0, x1, y1, text] in page pixels")

_UNSAFE_NAME = re.compile(r"[^\w.-]+")
_PAGE_ITEM = re.compile(r"(\d+)(?:-(\d+))?")


class MissingDependencyError(RuntimeError):
    """An optional package this feature needs is not installed."""


class RasterizeError(RuntimeError):
    """The document cannot be opened or rendered."""


# ----------------------------------------------------------------------------
# Pure helpers: tile grid, settings, page lists
# ----------------------------------------------------------------------------
@dataclass(frozen=True)
class TileBox:
    """One tile: grid position and pixel box in the page raster (x1/y1 exclusive)."""
    row: int
    col: int
    x0: int
    y0: int
    x1: int
    y1: int

    @property
    def width(self) -> int:
        return self.x1 - self.x0

    @property
    def height(self) -> int:
        return self.y1 - self.y0

    def as_list(self) -> list[int]:
        return [self.x0, self.y0, self.x1, self.y1]


def tile_starts(length: int, tile_px: int, overlap_px: int) -> tuple[int, ...]:
    """Start offsets of the fewest ``tile_px`` windows covering ``length``
    pixels with neighbours overlapping by at least ``overlap_px``. They are
    spread evenly, the first at 0 and the last ending at ``length``, so every
    tile is full size and the spare overlap is shared out."""
    if length <= tile_px:
        return (0,)
    count = -(-(length - overlap_px) // (tile_px - overlap_px))
    span = length - tile_px
    return tuple(i * span // (count - 1) for i in range(count))


def tile_grid(width: int, height: int, tile_px: int, overlap_px: int) -> tuple[TileBox, ...]:
    """Row-major tile boxes for a ``width`` x ``height`` raster; ``tile_px``
    0 means no tiling (one box, the whole page)."""
    if tile_px <= 0:
        return (TileBox(0, 0, 0, 0, width, height),)
    xs = tile_starts(width, tile_px, overlap_px)
    ys = tile_starts(height, tile_px, overlap_px)
    return tuple(TileBox(r, c, x, y, min(width, x + tile_px), min(height, y + tile_px))
                 for r, y in enumerate(ys) for c, x in enumerate(xs))


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def check_settings(dpi: int, tile_px: int, overlap_px: int) -> None:
    """ValueError unless dpi, tile_px (0 = no tiling) and overlap_px are usable."""
    if not _is_int(dpi) or not MIN_DPI <= dpi <= MAX_DPI:
        raise ValueError(f"dpi must be an integer from {MIN_DPI} to {MAX_DPI}, got {dpi!r}")
    if not _is_int(tile_px) or (tile_px != 0 and tile_px < MIN_TILE_PX):
        raise ValueError(f"tile_px must be 0 (no tiling) or an integer >= {MIN_TILE_PX}, "
                         f"got {tile_px!r}")
    if not _is_int(overlap_px) or overlap_px < 0:
        raise ValueError(f"overlap_px must be an integer >= 0, got {overlap_px!r}")
    if tile_px and 2 * overlap_px >= tile_px:
        raise ValueError(f"overlap_px ({overlap_px}) must be less than half of tile_px "
                         f"({tile_px})")


def parse_pages(spec: str) -> tuple[int, ...]:
    """'1,3-5' -> (1, 3, 4, 5): 1-based, sorted, duplicates dropped."""
    pages: set[int] = set()
    for item in spec.split(","):
        m = _PAGE_ITEM.fullmatch(item.strip())
        if not m:
            raise ValueError(f"bad page list {spec!r}: use 1-based pages like 1,3-5")
        first, last = int(m.group(1)), int(m.group(2) or m.group(1))
        if first < 1 or last < first:
            raise ValueError(f"bad page range {item.strip()!r} in {spec!r}: pages are 1-based, "
                             "ranges ascending")
        pages.update(range(first, last + 1))
    return tuple(sorted(pages))


def load_pymupdf() -> ModuleType:
    """The ``pymupdf`` module, or MissingDependencyError with the install command."""
    try:
        import pymupdf
    except ImportError as e:
        raise MissingDependencyError(
            "rasterizing PDF / image schematics needs PyMuPDF (package 'pymupdf'), an optional "
            f"dependency of the kicad-eda server that is not installed for {sys.executable}. "
            f'Install it with:\n  "{sys.executable}" -m pip install -r '
            f'"{OPTIONAL_REQUIREMENTS}"\n(it pins pymupdf; requirements-optional.txt). '
            f"Import error: {e}") from e
    return pymupdf


# ----------------------------------------------------------------------------
# Opening and planning
# ----------------------------------------------------------------------------
def _source_kind(src: Path) -> str:
    if not src.is_file():
        raise FileNotFoundError(f"no such file: {src}")
    suffix = src.suffix.lower()
    if suffix in PDF_SUFFIXES:
        return PDF
    if suffix in IMAGE_SUFFIXES:
        return IMAGE
    raise ValueError(f"unsupported input {src.name} ({suffix or 'no suffix'}): expected .pdf or "
                     f"an image ({', '.join(IMAGE_SUFFIXES)})")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_stem(src: Path) -> str:
    return _UNSAFE_NAME.sub("_", src.stem).strip("._") or "sheet"


def _open(fitz: ModuleType, src: Path, kind: str) -> Any:
    try:
        doc = fitz.open(str(src))
    except Exception as e:      # PyMuPDF raises its own FileDataError / FzError* classes
        raise RasterizeError(f"cannot open {src.name}: {e}") from e
    problem = ""
    if kind == PDF and not doc.is_pdf:
        problem = f"{src.name} is not a PDF"
    elif doc.needs_pass:
        problem = (f"{src.name} is password-protected; decrypt it first "
                   "(for example: qpdf --decrypt in.pdf out.pdf)")
    elif doc.page_count < 1:
        problem = f"{src.name} has no pages"
    if problem:
        doc.close()
        raise RasterizeError(problem)
    return doc


def _select(pages: Sequence[int] | None, count: int, src: Path) -> tuple[int, ...]:
    if pages is None:
        return tuple(range(1, count + 1))
    chosen: list[int] = []
    for n in pages:
        if not _is_int(n) or n < 1:
            raise ValueError(f"page numbers are 1-based integers, got {n!r}")
        if n > count:
            raise ValueError(f"page {n} is out of range: {src.name} has {count} page(s)")
        if n not in chosen:
            chosen.append(n)
    if not chosen:
        raise ValueError("no pages selected")
    return tuple(chosen)


@dataclass(frozen=True)
class _Plan:
    number: int                 # 1-based page number
    page: Any                   # pymupdf.Page (lives as long as the document)
    zoom_x: float               # page points -> raster pixels
    zoom_y: float
    width: int                  # full page raster, pixels
    height: int
    boxes: tuple[TileBox, ...]


def _zoom(page: Any, kind: str, dpi: int, where: str) -> tuple[float, float]:
    """PDF: dpi / 72. Image: the factor that maps the page onto the image's
    native pixels (MuPDF sizes an image page from its resolution tag)."""
    if kind == PDF:
        return dpi / PT_PER_INCH, dpi / PT_PER_INCH
    info = page.get_image_info()
    if not info:
        raise RasterizeError(f"{where}: no image found")
    width, height, t = info[0]["width"], info[0]["height"], info[0]["transform"]
    if abs(t[1]) > 1e-9 or abs(t[2]) > 1e-9:          # drawn rotated (EXIF orientation)
        width, height = height, width
    return width / page.rect.width, height / page.rect.height


def _plan(fitz: ModuleType, doc: Any, number: int, kind: str, dpi: int, tile_px: int,
          overlap_px: int, src: Path) -> _Plan:
    where = f"{src.name} page {number}"
    try:
        page = doc.load_page(number - 1)
        zx, zy = _zoom(page, kind, dpi, where)
        irect = (page.rect * fitz.Matrix(zx, zy)).irect
    except RasterizeError:
        raise
    except Exception as e:      # a damaged page or image surfaces here, not at open()
        raise RasterizeError(f"{where}: cannot read it: {e}") from e
    width, height = irect.width, irect.height
    if width < 1 or height < 1:
        raise RasterizeError(f"{where}: empty page ({width} x {height} px)")
    at = f"at {dpi} dpi" if kind == PDF else "at its native resolution"
    boxes = tile_grid(width, height, tile_px, overlap_px)
    if len(boxes) > MAX_TILES_PER_PAGE:
        raise ValueError(f"page {number} {at} is {width} x {height} px: {len(boxes)} tiles of "
                         f"{tile_px} px (limit {MAX_TILES_PER_PAGE}); lower the dpi or raise "
                         "tile_px")
    if not tile_px and width * height > MAX_UNTILED_PIXELS:
        raise ValueError(f"page {number} {at} is {width} x {height} px, too large to render "
                         f"untiled (limit {MAX_UNTILED_PIXELS // 1_000_000} Mpx); tile it "
                         "(tile_px) or lower the dpi")
    return _Plan(number, page, zx, zy, width, height, boxes)


# ----------------------------------------------------------------------------
# Rendering
# ----------------------------------------------------------------------------
def _render(fitz: ModuleType, plan: _Plan, path: Path, box: TileBox | None = None,
            scale: float = 1.0) -> Any:
    """Render the page (or the tile ``box``) on white and save it as PNG."""
    zx, zy = plan.zoom_x, plan.zoom_y
    clip = None if box is None else fitz.Rect(box.x0 / zx, box.y0 / zy, box.x1 / zx, box.y1 / zy)
    try:
        pix = plan.page.get_pixmap(matrix=fitz.Matrix(zx * scale, zy * scale), clip=clip,
                                   alpha=False)
        pix.save(str(path))
    except Exception as e:      # MuPDF render / encode errors, an unwritable path
        raise RasterizeError(f"page {plan.number}: cannot render {path.name}: {e}") from e
    return pix


def _words(fitz: ModuleType, plan: _Plan) -> list[list[Any]]:
    """The PDF text layer as [x0, y0, x1, y1, text] in page-raster pixels.
    PyMuPDF reports words unrotated; the rotation matrix maps them onto the
    page as displayed (what the raster shows)."""
    to_pixels = plan.page.rotation_matrix * fitz.Matrix(plan.zoom_x, plan.zoom_y)
    out: list[list[Any]] = []
    for word in plan.page.get_text("words", sort=True):
        r = fitz.Rect(word[:4]) * to_pixels
        out.append([round(r.x0, 1), round(r.y0, 1), round(r.x1, 1), round(r.y1, 1), word[4]])
    return out


def _tile_record(path: Path, box: TileBox, kind: str, dpi: int) -> dict[str, Any]:
    rec: dict[str, Any] = {"path": str(path), "row": box.row, "col": box.col,
                           "box_px": box.as_list()}
    if kind == PDF:
        rec["box_pt"] = [round(v * PT_PER_INCH / dpi, 2) for v in box.as_list()]
    return rec


def _actual_box(pix: Any, box: TileBox) -> TileBox:
    """The box MuPDF really rendered (its pixmap origin and size)."""
    return TileBox(box.row, box.col, pix.x, pix.y, pix.x + pix.width, pix.y + pix.height)


def _render_page(fitz: ModuleType, plan: _Plan, out: Path, stem: str, kind: str, dpi: int,
                 tile_px: int, text: bool) -> dict[str, Any]:
    base = f"{stem}_p{plan.number}"
    record: dict[str, Any] = {
        "page": plan.number, "width_px": plan.width, "height_px": plan.height,
        "rotation": plan.page.rotation,
        "size_pt": ([round(plan.page.rect.width, 2), round(plan.page.rect.height, 2)]
                    if kind == PDF else None)}
    if len(plan.boxes) == 1:
        path = out / f"{base}.png"
        pix = _render(fitz, plan, path)
        tiles = [_tile_record(path, TileBox(0, 0, 0, 0, pix.width, pix.height), kind, dpi)]
        record.update(image=str(path), overview=str(path), overview_scale=1.0, grid=[1, 1])
    else:
        overview = out / f"{base}_overview.png"
        pix = _render(fitz, plan, overview, scale=tile_px / max(plan.width, plan.height))
        tiles = []
        for box in plan.boxes:
            path = out / f"{base}_r{box.row}c{box.col}.png"
            tiles.append(_tile_record(path, _actual_box(_render(fitz, plan, path, box), box),
                                      kind, dpi))
        record.update(image=None, overview=str(overview),
                      overview_scale=round(pix.width / plan.width, 6),
                      grid=[plan.boxes[-1].row + 1, plan.boxes[-1].col + 1])
    record["tiles"] = tiles
    record["words"], record["word_count"] = None, 0
    if text:
        words = _words(fitz, plan)
        words_path = out / f"{base}_words.json"
        words_path.write_text(json.dumps(words), encoding="utf-8")
        record["words"], record["word_count"] = str(words_path), len(words)
    return record


def rasterize(src: str | Path, out_dir: str | Path, *, dpi: int = DEFAULT_DPI,
              tile_px: int = DEFAULT_TILE_PX, overlap_px: int = DEFAULT_OVERLAP_PX,
              pages: Sequence[int] | None = None, text: bool = True) -> dict[str, Any]:
    """Rasterize a PDF (at ``dpi``) or an image (native pixels) into PNG pages
    and tiles under ``out_dir``; returns the manifest, also written to
    ``<out_dir>/<stem>_raster.json``. ``pages`` (1-based) picks pages,
    ``tile_px`` 0 disables tiling, ``text`` writes the PDF text layer.

    Raises FileNotFoundError, ValueError (bad settings / pages / input type,
    a page that would need too many tiles), MissingDependencyError (no
    PyMuPDF) and RasterizeError (unreadable, encrypted or damaged document).
    Every page is planned before anything is written."""
    check_settings(dpi, tile_px, overlap_px)
    source = Path(src).resolve()
    kind = _source_kind(source)
    fitz = load_pymupdf()
    doc = _open(fitz, source, kind)
    try:
        numbers = _select(pages, doc.page_count, source)
        plans = [_plan(fitz, doc, n, kind, dpi, tile_px, overlap_px, source) for n in numbers]
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        stem = _safe_stem(source)
        records = [_render_page(fitz, plan, out, stem, kind, dpi, tile_px, text and kind == PDF)
                   for plan in plans]
        page_count = doc.page_count
    finally:
        doc.close()
    manifest_path = out / f"{stem}{MANIFEST_SUFFIX}"
    manifest = {
        "source": {"path": str(source), "kind": kind, "sha256": _sha256(source),
                   "page_count": page_count},
        "tool": f"pymupdf {fitz.__version__}",
        "dpi": dpi if kind == PDF else None,
        "tile_px": tile_px, "overlap_px": overlap_px,
        "coordinates": COORDINATES,
        "pages": records,
        "manifest": str(manifest_path),
    }
    manifest_path.write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    return manifest


# ----------------------------------------------------------------------------
# Command line
# ----------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    """``rasterize.py SRC OUT_DIR``: print the manifest; exit 0, or 2 on error."""
    ap = argparse.ArgumentParser(description="Rasterize a PDF / image schematic into PNG pages "
                                 "and overlapping tiles for the vision path (needs PyMuPDF).")
    ap.add_argument("src", help="the .pdf or image (.png, .jpg, .tif, ...)")
    ap.add_argument("out_dir", help="directory for the PNGs and <stem>_raster.json")
    ap.add_argument("--dpi", type=int, default=DEFAULT_DPI,
                    help=f"PDF render resolution (default {DEFAULT_DPI}; images keep their pixels)")
    ap.add_argument("--tile", type=int, default=DEFAULT_TILE_PX,
                    help=f"max tile edge in px, 0 = no tiling (default {DEFAULT_TILE_PX})")
    ap.add_argument("--overlap", type=int, default=DEFAULT_OVERLAP_PX,
                    help=f"min overlap of neighbouring tiles in px (default {DEFAULT_OVERLAP_PX})")
    ap.add_argument("--pages", help="1-based pages, e.g. 1,3-5 (default: all)")
    ap.add_argument("--no-text", action="store_true", help="skip the PDF text-layer words file")
    args = ap.parse_args(argv)
    try:
        pages = parse_pages(args.pages) if args.pages is not None else None
        manifest = rasterize(args.src, args.out_dir, dpi=args.dpi, tile_px=args.tile,
                             overlap_px=args.overlap, pages=pages, text=not args.no_text)
    except (OSError, ValueError, RuntimeError) as e:
        print(f"rasterize: {e}", file=sys.stderr)
        return 2
    print(json.dumps(manifest, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
