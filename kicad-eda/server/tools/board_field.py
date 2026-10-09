"""Auto-placement fields on board outlines (ROADMAP_DECISIONS D2); stdlib only (KiCad Python 3.11).
A field is the axis-aligned rect (board frame, mm, Y down) one side's auto parts pack into:
legacy_fields (w x h boards, bit-identical to swig_build_board.py at 6a8f94d), auto_fields (legacy,
explicit auto_* clipped to the board with a warning, or the largest inscribed rect minus holes)."""
from __future__ import annotations

import dataclasses
import math
import sys
from collections.abc import Mapping, Sequence
from itertools import pairwise
from pathlib import Path
from typing import NamedTuple

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import outline as ol  # noqa: E402

__all__ = ["DEFAULT_HOLE_DRILL_MM", "HOLE_KEEPOUT_MM", "INSCRIBED_MARGIN_MM", "NO_FIELD",
           "SIDES", "Field", "LegacyRect", "auto_fields", "cluster_origin", "fits",
           "is_legacy_block", "legacy_fields", "snap_to_edge"]

SIDES = ("F", "B")
INSCRIBED_MARGIN_MM = 0.5     # an inscribed field keeps edge clearance + this
HOLE_KEEPOUT_MM = 1.0         # hole keepout: square of half-side drill / 2 + this
DEFAULT_HOLE_DRILL_MM = 3.2   # the builder's default MountingHole_3.2mm_M3
NO_FIELD = "no placement field fits inside the outline"
AUTO_KEYS = ("auto_x", "auto_y", "auto_w", "auto_h", "auto_b")
_EDGE_MM, _FLUSH_TOP_MM = 2.0, 1.2   # legacy margin (x pack_scale); top when a J* sits flush
_CLIP_CELLS = 24              # uniform grid steps per axis when clipping an explicit field
_MAX_EDGE_LINES = 64          # most extra straight-edge grid lines per axis in a clip
_GROW_TOL_MM = 1e-5           # bisection resolution when pushing a side out
_MOUTHS = {"top": (0.0, -1.0), "bottom": (0.0, 1.0), "left": (-1.0, 0.0), "right": (1.0, 0.0)}

Rect = tuple[float, float, float, float]


def _num(v: object, what: str, lo: float | None = None) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        raise ValueError(f"{what} must be a finite number, got {v!r}")
    if lo is not None and v < lo:
        raise ValueError(f"{what} must be >= {lo:g} mm, got {v!r}")
    return float(v)


def _point(p: object, what: str) -> tuple[float, float]:
    if not isinstance(p, (list, tuple)) or len(p) != 2:
        raise ValueError(f"{what} must be [x, y], got {p!r}")
    return (_num(p[0], f"{what} x"), _num(p[1], f"{what} y"))


def _fmt(r: Sequence[float]) -> str:
    return f"({r[0]:.3f}, {r[1]:.3f})-({r[2]:.3f}, {r[3]:.3f})"


def _block(board_block: Mapping | None) -> Mapping:
    if board_block is not None and not isinstance(board_block, Mapping):
        raise ValueError(f"the board block must be an object, got {board_block!r}")
    return board_block or {}


# --- fields -------------------------------------------------------------------------------
class LegacyRect(NamedTuple):
    """A legacy field as the builder computes it (x0 = auto_x; None: centred on the board)."""
    x0: float | None
    y0: float
    w: float
    h: float
    board_w: float


@dataclasses.dataclass(frozen=True)
class Field:
    """Side `side`'s field (x0, y0)-(x1, y1); source: legacy | explicit | clipped | inscribed.
    ``legacy`` keeps the builder's numbers so w, h and cluster_origin are bit-identical."""
    x0: float
    y0: float
    x1: float
    y1: float
    source: str
    side: str = "F"
    legacy: LegacyRect | None = None

    def __post_init__(self) -> None:
        for name in ("x0", "y0", "x1", "y1"):
            object.__setattr__(self, name, _num(getattr(self, name), f"Field.{name}"))
        if self.side not in SIDES:
            raise ValueError(f"Field.side must be 'F' or 'B', got {self.side!r}")

    @property
    def w(self) -> float:
        return self.legacy.w if self.legacy is not None else self.x1 - self.x0

    @property
    def h(self) -> float:
        return self.legacy.h if self.legacy is not None else self.y1 - self.y0

    @property
    def area(self) -> float:
        return max(0.0, self.w) * max(0.0, self.h)

    @property
    def rect(self) -> Rect:
        return (self.x0, self.y0, self.x1, self.y1)


def fits(field: Field, w: float, h: float) -> bool:
    """A w x h cluster fits the field (the builder's test: no side larger)."""
    return not (w > field.w or h > field.h)


def cluster_origin(field: Field, used_w: float) -> tuple[float, float]:
    """Top-left of a cluster `used_w` wide centred across the field top (legacy: as built)."""
    lg = field.legacy
    if lg is None:
        return (field.x0 + (field.w - used_w) / 2.0, field.y0)
    x0 = (float(lg.x0) + (lg.w - used_w) / 2.0
          if lg.x0 is not None else (lg.board_w - used_w) / 2.0)
    return (x0, lg.y0)


# --- (1) legacy boards ----------------------------------------------------------------------
def _legacy_rects(block: Mapping, board_w: float, board_h: float, scale: float,
                  has_conn: bool) -> tuple[LegacyRect, LegacyRect]:
    """swig_build_board's mixed branch: the same float operations, same order."""
    edge = _EDGE_MM * scale
    edge_top = _FLUSH_TOP_MM if has_conn else edge
    y0 = edge_top + _num(block.get("auto_y", 0.0), "board.auto_y")
    x0_spec = None if block.get("auto_x") is None else _num(block["auto_x"], "board.auto_x")
    avail_w = _num(block.get("auto_w", board_w - 2 * edge), "board.auto_w")
    avail_h = _num(block.get("auto_h", board_h - y0 - edge), "board.auto_h")
    ab = block.get("auto_b") or {}
    if not isinstance(ab, Mapping):
        raise ValueError(f"board.auto_b must be an object {{x, y, w, h}}, got {ab!r}")
    bx = ab.get("x", x0_spec)
    back = LegacyRect(None if bx is None else _num(bx, "board.auto_b.x"),
                      _num(ab.get("y", y0), "board.auto_b.y"),
                      _num(ab.get("w", avail_w), "board.auto_b.w"),
                      _num(ab.get("h", avail_h), "board.auto_b.h"), board_w)
    return LegacyRect(x0_spec, y0, avail_w, avail_h, board_w), back


def legacy_fields(board_block: Mapping | None, board_w: float, board_h: float, *,
                  scale: float = 1.0, has_conn: bool = False) -> list[Field]:
    """[F, B] fields of today's mixed placement on a fixed board_w x board_h board. `scale` =
    PACK_SCALE; `has_conn`: an auto J* without "interior" sits flush. No auto_b: B = F."""
    if _num(scale, "scale") <= 0:
        raise ValueError(f"scale must be > 0, got {scale!r}")
    rects = _legacy_rects(_block(board_block), _num(board_w, "board_w"),
                          _num(board_h, "board_h"), float(scale), bool(has_conn))
    return [Field(x0, lg.y0, x0 + lg.w, lg.y0 + lg.h, "legacy", side, lg)
            for lg, side in zip(rects, SIDES, strict=True)
            for x0 in [float(lg.x0) if lg.x0 is not None else (lg.board_w - lg.w) / 2.0]]


def is_legacy_block(board_block: Mapping | None) -> bool:
    """The builder keeps today's path (outline_resolve's ResolvedBoard.is_legacy)."""
    b = _block(board_block)
    return (b.get("outline") is None and not b.get("corner_radius")
            and not b.get("cutouts") and b.get("datum") is None)


# --- rectangle search ------------------------------------------------------------------------
def _lines(lo: float, hi: float, extra: Sequence[float], steps: int) -> list[float]:
    """Grid lines over [lo, hi]: `steps` equal steps plus the `extra` values inside."""
    inner = {lo + (hi - lo) * i / steps for i in range(1, steps)} | set(extra)
    return [lo, *sorted(v for v in inner if lo < v < hi), hi]


def _best_block(free: list[list[bool]], xs: list[float], ys: list[float]) -> Rect | None:
    """Largest rectangle of free grid cells (free[j][i] = xs[i]..xs[i+1] x ys[j]..ys[j+1]);
    top[i] is the first row of column i's free run down to row j (j + 1: none)."""
    best, best_area, top = None, 0.0, [0] * (len(xs) - 1)
    for j, row in enumerate(free):
        top = [t if f else j + 1 for t, f in zip(top, row, strict=True)]
        for i, t in enumerate(top):
            if t > j:
                continue
            a, b = i, i + 1
            while a > 0 and top[a - 1] <= t:
                a -= 1
            while b < len(top) and top[b] <= t:
                b += 1
            area = (xs[b] - xs[a]) * (ys[j + 1] - ys[t])
            if area > best_area:
                best, best_area = (xs[a], ys[t], xs[b], ys[j + 1]), area
    return best


def _largest_free_subrect(rect: Rect, boxes: list[Rect]) -> Rect | None:
    """Largest sub-rectangle of `rect` overlapping no box (exact: a maximal one is
    bounded by rect and box edges, which are the grid lines)."""
    xs = _lines(rect[0], rect[2], [v for b in boxes for v in (b[0], b[2])], 1)
    ys = _lines(rect[1], rect[3], [v for b in boxes for v in (b[1], b[3])], 1)
    free = [[not any(xa < b[2] and b[0] < xb and ya < b[3] and b[1] < yb for b in boxes)
             for xa, xb in pairwise(xs)] for ya, yb in pairwise(ys)]
    return _best_block(free, xs, ys)


def _grow(o: ol.Outline, r: Rect, box: Rect, clearance: float, order: tuple) -> Rect:
    """Push r's sides towards box's (in `order`, twice round: one side frees another) as
    far as r stays on the board with `clearance`: box's side first, then bisection."""
    for side in order * 2:
        good, bad, v = r[side], box[side], box[side]
        while abs(bad - good) > _GROW_TOL_MM:
            ok = ol.contains_rect(o, (*r[:side], v, *r[side + 1:]), clearance)
            good, bad = (v, bad) if ok else (good, v)
            v = (good + bad) / 2.0
        r = (*r[:side], good, *r[side + 1:])
    return r


def _edge_lines(o: ol.Outline, rect: Rect, clearance: float, axis: int) -> list[float]:
    """Straight-edge vertex coordinates +- clearance inside rect on `axis`: grid lines
    there make a rectilinear clip exact (none past _MAX_EDGE_LINES: a tessellated DXF)."""
    vals = {p[axis] + s * clearance for e in ol.edges(o) if isinstance(e.prim, ol.Line)
            for p in (e.prim.a, e.prim.b) for s in (-1.0, 1.0)}
    inner = [v for v in vals if rect[axis] < v < rect[axis + 2]]
    return inner if len(inner) <= _MAX_EDGE_LINES else []


def _cell_free(o: ol.Outline, cell: Rect, clearance: float) -> bool:
    """contains_rect(cell); the centre decides alone when it is clearance + half a
    diagonal (+ margin over contains' 1e-6 slack) inside, or not even clearance inside."""
    c = ((cell[0] + cell[2]) / 2.0, (cell[1] + cell[3]) / 2.0)
    half_diag = math.hypot(cell[2] - cell[0], cell[3] - cell[1]) / 2.0
    return ol.contains(o, c, clearance + half_diag + _GROW_TOL_MM) or (
        ol.contains(o, c, clearance) and ol.contains_rect(o, cell, clearance))


def _clip(o: ol.Outline, rect: Rect, clearance: float) -> Rect | None:
    """Largest rectangle inside `rect` on the board with `clearance`, or None: the best
    block of on-board grid cells, pushed out x sides first and y first (larger wins)."""
    bb = ol.bbox(o)               # grid only the part of the request the board can reach
    rect = (max(rect[0], bb[0] + clearance), max(rect[1], bb[1] + clearance),
            min(rect[2], bb[2] - clearance), min(rect[3], bb[3] - clearance))
    if rect[0] >= rect[2] or rect[1] >= rect[3]:
        return None
    xs = _lines(rect[0], rect[2], _edge_lines(o, rect, clearance, 0), _CLIP_CELLS)
    ys = _lines(rect[1], rect[3], _edge_lines(o, rect, clearance, 1), _CLIP_CELLS)
    free = [[_cell_free(o, (xa, ya, xb, yb), clearance) for xa, xb in pairwise(xs)]
            for ya, yb in pairwise(ys)]
    seed = _best_block(free, xs, ys)
    if seed is None:
        return None
    if not ol.contains_rect(o, seed, clearance):
        raise ol.OutlineError(f"internal: the seed rectangle {seed} failed the exact test")
    grown = [_grow(o, seed, rect, clearance, order) for order in ((0, 2, 1, 3), (1, 3, 0, 2))]
    return max(grown, key=lambda r: (r[2] - r[0]) * (r[3] - r[1]))


# --- (2) explicit auto_* and (3) inscribed fields on an outline --------------------------------
def _checked(o: ol.Outline, f: Field, clearance: float) -> tuple[Field | None, str | None]:
    name = f"explicit {f.side} auto field {_fmt(f.rect)}"
    if f.area <= 0.0:
        return None, f"{name} has no area; no {f.side} field"
    if ol.contains_rect(o, f.rect, clearance):
        return dataclasses.replace(f, source="explicit"), None
    clipped = _clip(o, f.rect, clearance)
    if clipped is None:
        return None, (f"{name} keeps no usable part on the board with {clearance:g} mm "
                      f"edge clearance; no {f.side} field")
    kept = Field(*clipped, "clipped", f.side)
    return kept, (f"{name} leaves the board (edge clearance {clearance:g} mm); clipped to "
                  f"{_fmt(clipped)}, {100.0 * kept.area / f.area:.0f}% of it")


def _explicit_fields(block: Mapping, o: ol.Outline, clearance: float, scale: float,
                     has_conn: bool) -> tuple[list[Field], list[str]]:
    bx0, by0, bx1, by1 = ol.bbox(o)
    w = block["w"] if block.get("w") is not None else bx1 - bx0
    h = block["h"] if block.get("h") is not None else by1 - by0
    checked = [_checked(o, f, clearance)
               for f in legacy_fields(block, w, h, scale=scale, has_conn=has_conn)]
    return ([f for f, _ in checked if f is not None],
            [warning for _, warning in checked if warning is not None])


def _hole_box(i: int, hole: object) -> Rect:
    if not isinstance(hole, (list, tuple)) or len(hole) not in (2, 3):
        raise ValueError(f"mounting hole {i} must be (x, y) or (x, y, drill), got {hole!r}")
    x, y = _num(hole[0], f"mounting hole {i} x"), _num(hole[1], f"mounting hole {i} y")
    drill = _num(hole[2], f"mounting hole {i} drill") if len(hole) == 3 else DEFAULT_HOLE_DRILL_MM
    if drill <= 0:
        raise ValueError(f"mounting hole {i} drill must be > 0 mm, got {hole!r}")
    k = drill / 2 + HOLE_KEEPOUT_MM
    return (x - k, y - k, x + k, y + k)


def _inscribed_fields(o: ol.Outline, clearance: float,
                      holes: list[Rect]) -> tuple[list[Field], list[str]]:
    margin = clearance + INSCRIBED_MARGIN_MM
    rect = ol.largest_inscribed_rect(o, clearance=margin)
    if rect is None:
        return [], [f"{NO_FIELD} (edge clearance {margin:g} mm)"]
    free = _largest_free_subrect(rect, holes)
    if free is None:
        return [], [f"{NO_FIELD} once the mounting-hole keepouts (drill / 2 + "
                    f"{HOLE_KEEPOUT_MM:g} mm) are removed from {_fmt(rect)}"]
    return [Field(*free, "inscribed", side) for side in SIDES], []


def auto_fields(board_block: Mapping | None, outline: ol.Outline | None,
                edge_clearance: float, mounting_holes: Sequence = (), *,
                scale: float = 1.0, has_conn: bool = False) -> tuple[list[Field], list[str]]:
    """([F, B] fields, warnings): (1) a legacy block (whatever `outline` is): legacy_fields;
    (2) explicit auto_*: kept or clipped to `outline` (None: built from the block; resolve a
    DXF first); (3) the inscribed rect minus mounting_holes [(x, y[, drill 3.2])] keepouts."""
    block = _block(board_block)
    clearance = _num(edge_clearance, "edge_clearance", lo=0.0)
    holes = [_hole_box(i, hole) for i, hole in enumerate(mounting_holes or ())]
    if is_legacy_block(block):
        if block.get("w") is None or block.get("h") is None:
            return [], []                   # auto-sized board: the builder sizes it to fit
        return legacy_fields(block, block["w"], block["h"], scale=scale, has_conn=has_conn), []
    o = outline if outline is not None else ol.from_board_block(block)
    if any(block.get(k) not in (None, {}) for k in AUTO_KEYS):
        return _explicit_fields(block, o, clearance, scale, has_conn)
    return _inscribed_fields(o, clearance, holes)


# --- edge snapping ---------------------------------------------------------------------------
def _direction(mouth_dir: object) -> tuple[float, float]:
    if isinstance(mouth_dir, str) and mouth_dir.lower() in _MOUTHS:
        return _MOUTHS[mouth_dir.lower()]
    dx, dy = _point(mouth_dir, f"mouth_dir (one of {', '.join(_MOUTHS)} or a vector)")
    n = math.hypot(dx, dy)
    if n < 1e-9:
        raise ValueError(f"mouth_dir {mouth_dir!r} is the zero vector: no mouth to snap along")
    return (dx / n, dy / n)


def snap_to_edge(outline: ol.Outline, center: Sequence[float], mouth_dir: object,
                 half_extent_along_dir: float, overhang: float = 0.0,
                 clearance: float = 0.0) -> tuple[float, float] | None:
    """The part's new centre: slid along `mouth_dir` ('top' | 'bottom' | 'left' | 'right' in
    the Y-down board frame, or a vector) until its courtyard edge that way (half_extent_along_dir
    from the centre) sits `clearance` inside the first edge the ray from `center` crosses, or
    `overhang` past it if > 0. None if no edge lies ahead (a ray miss; centre off the board)."""
    c, d = _point(center, "center"), _direction(mouth_dir)
    half = _num(half_extent_along_dir, "half_extent_along_dir", lo=0.0)
    over, gap = _num(overhang, "overhang", lo=0.0), _num(clearance, "clearance", lo=0.0)
    hit = ol.ray_to_edge(outline, c, d)
    if hit is None or hit.normal[0] * d[0] + hit.normal[1] * d[1] <= 0.0:
        return None
    shift = hit.distance - half + (over if over > 0.0 else -gap)
    return (c[0] + d[0] * shift, c[1] + d[1] * shift)
