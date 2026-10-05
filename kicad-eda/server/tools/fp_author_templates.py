#!/usr/bin/env python3
"""Footprint specs and package templates for fp_author (F2, ROADMAP D1).

The spec vocabulary is a set of frozen dataclasses that validate on
construction and fail loudly: Pad, Body, Model and FootprintSpec. fp_author
renders, writes, checks and compares specs, and re-exports these names.

The templates turn the land-pattern numbers a datasheet gives (pad sizes, row
spans, pitches) into a spec:

  * lga_grid        - straight edge rows plus an inner grid; the numbering
                      rule is a callable (col, row) -> number | None;
  * qfn / dfn       - perimeter pads numbered counter-clockwise from the top
                      of the left side, an optional exposed pad with paste
                      windows and a thermal-via grid;
  * lcc_castellated - perimeter pads straddling the module edge;
  * sop             - two rows (SOIC / SSOP / TSSOP), optional exposed pad;
  * pin_header      - through-hole rows x cols; pin 1 square, zig-zag numbering;
  * lg290p          - Quectel LG290P(03), LGA-79: the first authoring target.

Also the helpers fp_author, fp_reader and fp_compare share: has_copper,
pad_bbox, natural_key, r6. Units are mm, in the KiCad footprint frame (+Y
down). Stdlib-only (Python 3.11: KiCad-python code may import it).
"""
from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

COURTYARD_EXCESS_MM = 0.25          # IPC-7351B density level B (nominal); see fp_author
PAD_TYPES = ("smd", "thru_hole", "np_thru_hole")
PAD_SHAPES = ("rect", "roundrect", "circle", "oval")
ATTR_TYPES = ("smd", "through_hole")
ATTR_FLAGS = ("board_only", "exclude_from_pos_files", "exclude_from_bom",
              "allow_missing_courtyard", "dnp")
SIDES = ("L", "T", "R", "B")
DEFAULT_LAYERS = {"smd": ("F.Cu", "F.Mask", "F.Paste"),
                  "thru_hole": ("*.Cu", "*.Mask"),
                  "np_thru_hole": ("*.Cu", "*.Mask")}
EPS = 1e-6
_BAD_NAME_CHARS = set('/\\:*?"<>|')


def has_copper(pad: Any) -> bool:
    """True when a pad (spec or parsed) has a copper layer."""
    return any(layer.endswith(".Cu") for layer in pad.pad_layers)


def pad_bbox(pad: Any) -> tuple[float, float, float, float]:
    """Axis-aligned extent (x0, y0, x1, y1) of a pad (spec or parsed), any rotation."""
    a = math.radians(pad.rotation)
    c, s = abs(math.cos(a)), abs(math.sin(a))
    hw, hh = (pad.w * c + pad.h * s) / 2, (pad.w * s + pad.h * c) / 2
    return (pad.x - hw, pad.y - hh, pad.x + hw, pad.y + hh)


def natural_key(name: str) -> list[tuple[int, int, str]]:
    """Sort key for pad names: digit runs compare as numbers, so "2" < "10"
    and "A2" < "A10" < "B2"."""
    return [(0, int(t), "") if t.isdigit() else (1, 0, t) for t in re.findall(r"\d+|\D+", name)]


def r6(v: float) -> float:
    """``v`` rounded to 1 nm (6 decimals in mm); -0.0 becomes 0.0."""
    return round(v, 6) + 0.0


# ---------------------------------------------------------------------------
# The spec
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Pad:
    """One pad. ``w``/``h`` are the unrotated X/Y size; ``rotation`` degrees.
    ``layers`` None = the type's default; a paste-only window is
    ``Pad("", ..., layers=("F.Paste",))``."""
    number: str
    x: float
    y: float
    w: float
    h: float
    type: str = "smd"
    shape: str = "rect"
    drill: float | None = None
    layers: tuple[str, ...] | None = None
    roundrect_ratio: float = 0.25
    rotation: float = 0.0
    heatsink: bool = False

    def __post_init__(self) -> None:
        bad = []
        if not isinstance(self.number, str):
            bad.append(f"number must be a str, got {type(self.number).__name__}")
        if self.type not in PAD_TYPES:
            bad.append(f"type {self.type!r} not in {PAD_TYPES}")
        if self.shape not in PAD_SHAPES:
            bad.append(f"shape {self.shape!r} not in {PAD_SHAPES}")
        if self.w <= 0 or self.h <= 0:
            bad.append(f"size {self.w} x {self.h} must be positive")
        elif self.shape == "circle" and abs(self.w - self.h) > EPS:
            bad.append(f"a circle pad needs w == h, got {self.w} x {self.h}")
        if self.type == "smd" and self.drill is not None:
            bad.append("an smd pad has no drill")
        if self.type != "smd":
            if self.drill is None or self.drill <= 0:
                bad.append(f"a {self.type} pad needs a positive drill")
            elif self.drill > min(self.w, self.h) + EPS or (
                    self.type == "thru_hole" and self.drill >= min(self.w, self.h) - EPS):
                bad.append(f"drill {self.drill} leaves no annular ring in {self.w} x {self.h}")
        if self.shape == "roundrect" and not 0 < self.roundrect_ratio <= 0.5:
            bad.append(f"roundrect_ratio {self.roundrect_ratio} must be in (0, 0.5]")
        if self.layers is not None and not self.layers:
            bad.append("layers must not be empty (None = the type's default)")
        if bad:
            raise ValueError(f"pad {self.number!r}: " + "; ".join(bad))

    @property
    def pad_layers(self) -> tuple[str, ...]:
        return self.layers or DEFAULT_LAYERS[self.type]


@dataclass(frozen=True)
class Body:
    """Package body: nominal ``w`` x ``h`` centred at (x, y); ``w_max``/``h_max``
    are the max-material sizes the courtyard is built on (default nominal);
    ``height`` (mm, max) is reported for the netlist ``height`` key."""
    w: float
    h: float
    x: float = 0.0
    y: float = 0.0
    w_max: float | None = None
    h_max: float | None = None
    height: float | None = None

    def __post_init__(self) -> None:
        if self.w <= 0 or self.h <= 0:
            raise ValueError(f"body size {self.w} x {self.h} must be positive")
        if (self.w_max is not None and self.w_max < self.w) or \
                (self.h_max is not None and self.h_max < self.h):
            raise ValueError(f"body max size {self.w_max} x {self.h_max} is below nominal "
                             f"{self.w} x {self.h}")
        if self.height is not None and self.height <= 0:
            raise ValueError(f"body height {self.height} must be positive")

    def rect(self, grow: float = 0.0) -> tuple[float, float, float, float]:
        return (self.x - self.w / 2 - grow, self.y - self.h / 2 - grow,
                self.x + self.w / 2 + grow, self.y + self.h / 2 + grow)

    def max_rect(self) -> tuple[float, float, float, float]:
        w, h = self.w_max or self.w, self.h_max or self.h
        return (self.x - w / 2, self.y - h / 2, self.x + w / 2, self.y + h / 2)


@dataclass(frozen=True)
class Model:
    """A 3D model reference, e.g. ``${KIPRJMOD}/libs/3d/X.step``."""
    path: str
    offset: tuple[float, float, float] = (0.0, 0.0, 0.0)
    scale: tuple[float, float, float] = (1.0, 1.0, 1.0)
    rotate: tuple[float, float, float] = (0.0, 0.0, 0.0)


@dataclass(frozen=True)
class FootprintSpec:
    """Everything fp_author.render() needs. ``pin1`` is the pad number the fab
    chamfer and silk marker point at (None: neither); ``pin1_side`` puts the
    marker beyond that side of the pad ("auto": the sides pin 1 faces, the
    nearest body edge first; "none": no marker). The marker stays inside the
    courtyard when it fits there (see fp_author.place_pin1_marker)."""
    name: str
    pads: tuple[Pad, ...]
    body: Body
    attr: str = "smd"
    attr_flags: tuple[str, ...] = ()
    descr: str = ""
    tags: str = ""
    datasheet: str = ""
    model: Model | None = None
    pin1: str | None = "1"
    pin1_side: str = "auto"
    courtyard_excess: float = COURTYARD_EXCESS_MM

    def __post_init__(self) -> None:
        bad = []
        if not self.name or self.name != self.name.strip() or _BAD_NAME_CHARS & set(self.name):
            bad.append(f"name {self.name!r} is not usable as a footprint file name")
        if not self.pads:
            bad.append("no pads")
        if self.attr not in ATTR_TYPES:
            bad.append(f"attr {self.attr!r} not in {ATTR_TYPES}")
        if set(self.attr_flags) - set(ATTR_FLAGS):
            bad.append(f"attr_flags {sorted(set(self.attr_flags) - set(ATTR_FLAGS))} "
                       f"not in {ATTR_FLAGS}")
        if self.pin1_side not in SIDES + ("auto", "none"):
            bad.append(f"pin1_side {self.pin1_side!r} not in {SIDES + ('auto', 'none')}")
        if self.pin1 is not None and not any(p.number == self.pin1 and has_copper(p)
                                             for p in self.pads):
            bad.append(f"pin1 {self.pin1!r} is not a copper pad (pass pin1=None for no marker)")
        if self.courtyard_excess < 0:
            bad.append(f"courtyard_excess {self.courtyard_excess} must be >= 0")
        if bad:
            raise ValueError(f"footprint {self.name!r}: " + "; ".join(bad))


# ---------------------------------------------------------------------------
# Building blocks
# ---------------------------------------------------------------------------
def pitch_run(start: float, pitch: float, count: int) -> tuple[float, ...]:
    """start, start + pitch, ... (count values, rounded to 1 nm)."""
    return tuple(round(start + i * pitch, 6) + 0.0 for i in range(count))


def centred_run(count: int, pitch: float, centre: float = 0.0) -> tuple[float, ...]:
    """count values at ``pitch`` centred on ``centre``."""
    return pitch_run(centre - (count - 1) * pitch / 2, pitch, count)


@dataclass(frozen=True)
class EdgeRow:
    """A straight row of pads along body side ``side`` ("L", "R", "T", "B") at
    perpendicular coordinate ``at`` (x for L/R, y for T/B), numbered
    ``first``, ``first`` + 1, ... in the order of ``positions`` (the
    along-row coordinates). ``length`` is the pad size perpendicular to the
    side, ``width`` the size along it."""
    side: str
    at: float
    positions: tuple[float, ...]
    first: int
    length: float
    width: float
    shape: str = "rect"

    def pads(self, shapes: Mapping[str, str] | None = None, ratio: float = 0.25) -> list[Pad]:
        if self.side not in SIDES:
            raise ValueError(f"edge row side {self.side!r} not in {SIDES}")
        vertical = self.side in ("L", "R")
        out = []
        for i, pos in enumerate(self.positions):
            number = str(self.first + i)
            x, y = (self.at, pos) if vertical else (pos, self.at)
            w, h = (self.length, self.width) if vertical else (self.width, self.length)
            out.append(Pad(number, x, y, w, h, shape=(shapes or {}).get(number, self.shape),
                           roundrect_ratio=ratio))
        return out


@dataclass(frozen=True)
class PadGrid:
    """Inner pads at every (xs[col], ys[row]); w x h each."""
    xs: tuple[float, ...]
    ys: tuple[float, ...]
    w: float
    h: float
    shape: str = "rect"


@dataclass(frozen=True)
class ThermalVias:
    """An nx x ny via grid at ``pitch``, centred on the exposed pad; vias are
    plated holes carrying the pad's number (KiCad stock style), plus an
    optional B.Cu copy of the pad to spread heat on the bottom."""
    nx: int
    ny: int
    pitch: float
    drill: float = 0.3
    size: float = 0.6
    bottom_pad: bool = True


@dataclass(frozen=True)
class ExposedPad:
    """A thermal / exposed pad, ``w`` x ``h`` at (x, y). number None = the next
    number after the perimeter pins. Paste is solid unless ``paste_grid`` /
    ``paste_coverage`` split it into windows. IPC-7093 suggests 50-80 %
    coverage on large bottom-termination pads; windows are a uniform grid,
    each cell's window scaled by sqrt(coverage)."""
    w: float
    h: float
    number: str | None = None
    x: float = 0.0
    y: float = 0.0
    paste_grid: tuple[int, int] = (1, 1)
    paste_coverage: float = 1.0
    vias: ThermalVias | None = None


def exposed_pads(ep: ExposedPad, number: str) -> list[Pad]:
    """The exposed pad's copper, paste windows and thermal vias."""
    px, py = ep.paste_grid
    if px < 1 or py < 1 or not 0 < ep.paste_coverage <= 1:
        raise ValueError(f"exposed pad paste grid {ep.paste_grid} / coverage "
                         f"{ep.paste_coverage} invalid (grid >= 1x1, 0 < coverage <= 1)")
    solid = (px, py) == (1, 1) and ep.paste_coverage == 1.0
    layers = ("F.Cu", "F.Mask", "F.Paste") if solid else ("F.Cu", "F.Mask")
    out = [Pad(number, ep.x, ep.y, ep.w, ep.h, layers=layers, heatsink=True)]
    if not solid:
        cw, ch, k = ep.w / px, ep.h / py, ep.paste_coverage ** 0.5
        out += [Pad("", round(ep.x - ep.w / 2 + cw * (i + 0.5), 6),
                    round(ep.y - ep.h / 2 + ch * (j + 0.5), 6), round(cw * k, 6),
                    round(ch * k, 6), shape="roundrect", layers=("F.Paste",))
                for i in range(px) for j in range(py)]
    v = ep.vias
    if v is not None:
        if (v.nx - 1) * v.pitch + v.size > ep.w + EPS or (v.ny - 1) * v.pitch + v.size > ep.h + EPS:
            raise ValueError(f"{v.nx} x {v.ny} vias at {v.pitch} mm do not fit the "
                             f"{ep.w} x {ep.h} exposed pad")
        out += [Pad(number, ep.x + x, ep.y + y, v.size, v.size, type="thru_hole", shape="circle",
                    drill=v.drill, layers=("*.Cu",), heatsink=True)
                for x in centred_run(v.nx, v.pitch) for y in centred_run(v.ny, v.pitch)]
        if v.bottom_pad:
            out.append(Pad(number, ep.x, ep.y, ep.w, ep.h, layers=("B.Cu",), heatsink=True))
    return out


def _with_ep(pins: list[Pad], ep: ExposedPad | None) -> list[Pad]:
    if ep is None:
        return pins
    return pins + exposed_pads(ep, ep.number or str(len(pins) + 1))


def perimeter_pads(counts: Mapping[str, int], at: Mapping[str, float], pitch: float,
                   pad_w: float, pad_l: float, shape: str = "roundrect", first: int = 1,
                   centre: tuple[float, float] = (0.0, 0.0)) -> list[Pad]:
    """Pads on up to four sides, numbered counter-clockwise from the top of the
    left side: L top->bottom, B left->right, R bottom->top, T right->left.
    ``at[side]`` is the row's perpendicular coordinate; rows are centred on
    ``centre``. ``pad_w`` is the size along the row, ``pad_l`` across it."""
    rows = []
    n = first
    for side, reverse in (("L", False), ("B", False), ("R", True), ("T", True)):
        count = counts.get(side, 0)
        if count:
            pos = centred_run(count, pitch, centre[1] if side in ("L", "R") else centre[0])
            rows.append(EdgeRow(side, at[side], tuple(reversed(pos)) if reverse else pos,
                                n, pad_l, pad_w, shape))
            n += count
    return [p for row in rows for p in row.pads()]


def _spec(name: str, pads: Sequence[Pad], body: Body, defaults: dict,
          spec_kw: dict) -> FootprintSpec:
    # exposed-pad parts (top, vias, bottom copy) share a number and are all heatsink pads
    numbered = Counter(p.number for p in pads if p.number and not p.heatsink)
    dupes = sorted(n for n, c in numbered.items() if c > 1)
    if dupes:
        raise ValueError(f"{name}: duplicate pad numbers {dupes[:10]}")
    return FootprintSpec(name=name, pads=tuple(pads), body=body, **{**defaults, **spec_kw})


# ---------------------------------------------------------------------------
# Templates (extra keyword arguments go to FootprintSpec: descr, tags,
# datasheet, model, pin1, pin1_side, attr_flags, courtyard_excess)
# ---------------------------------------------------------------------------
def lga_grid(name: str, *, body: Body, edge_rows: Sequence[EdgeRow] = (),
             grid: PadGrid | None = None,
             numbering: Callable[[int, int], str | None] | None = None,
             shapes: Mapping[str, str] | None = None, **spec_kw: Any) -> FootprintSpec:
    """Edge rows plus an inner grid; ``numbering(col, row)`` names each grid
    pad (col 0 = smallest x, row 0 = smallest y; None = no pad there).
    ``shapes`` overrides the shape of individual pads by number."""
    pads = [p for row in edge_rows for p in row.pads(shapes)]
    if grid is not None:
        if numbering is None:
            raise ValueError(f"{name}: a pad grid needs a numbering(col, row) rule")
        for col, x in enumerate(grid.xs):
            for row, y in enumerate(grid.ys):
                number = numbering(col, row)
                if number is not None:
                    pads.append(Pad(str(number), x, y, grid.w, grid.h,
                                    shape=(shapes or {}).get(str(number), grid.shape)))
    return _spec(name, pads, body, {"pin1_side": "L"}, spec_kw)


def qfn(name: str, *, body: Body, pitch: float, pins_x: int, pins_y: int, span_x: float,
        span_y: float, pad_w: float, pad_l: float, ep: ExposedPad | None = None,
        shape: str = "roundrect", **spec_kw: Any) -> FootprintSpec:
    """QFN: ``pins_y`` pads on each of the left/right sides, ``pins_x`` on each
    of the top/bottom sides; ``span_x``/``span_y`` are the centre-to-centre
    distances of the opposite rows (the land pattern's "C" dimension);
    ``pad_w`` is along the pitch, ``pad_l`` across it."""
    at = {"L": -span_x / 2, "R": span_x / 2, "T": -span_y / 2, "B": span_y / 2}
    pins = perimeter_pads({"L": pins_y, "B": pins_x, "R": pins_y, "T": pins_x}, at, pitch,
                          pad_w, pad_l, shape)
    return _spec(name, _with_ep(pins, ep), body, {"pin1_side": "L"}, spec_kw)


def dfn(name: str, *, body: Body, pitch: float, pins_per_side: int, span: float, pad_w: float,
        pad_l: float, ep: ExposedPad | None = None, shape: str = "roundrect",
        **spec_kw: Any) -> FootprintSpec:
    """DFN / SON: pads on the left and right sides only (QFN numbering)."""
    return qfn(name, body=body, pitch=pitch, pins_x=0, pins_y=pins_per_side, span_x=span,
               span_y=0.0, pad_w=pad_w, pad_l=pad_l, ep=ep, shape=shape, **spec_kw)


def lcc_castellated(name: str, *, body: Body, pitch: float, pins: tuple[int, int, int, int],
                    pad_w: float, pad_l: float, edge_offset: float = 0.0,
                    ep: ExposedPad | None = None, shape: str = "rect",
                    **spec_kw: Any) -> FootprintSpec:
    """Castellated / LCC module: ``pins`` = (left, bottom, right, top) counts;
    each pad centre sits ``edge_offset`` mm OUTSIDE the body edge (0 = pads
    centred on the edge), numbered like a QFN."""
    x0, y0, x1, y1 = body.rect(edge_offset)
    at = {"L": x0, "R": x1, "T": y0, "B": y1}
    pads = perimeter_pads(dict(zip(("L", "B", "R", "T"), pins, strict=True)), at, pitch, pad_w,
                          pad_l, shape, centre=(body.x, body.y))
    return _spec(name, _with_ep(pads, ep), body, {"pin1_side": "L"}, spec_kw)


def sop(name: str, *, body: Body, pins: int, pitch: float, span: float, pad_w: float,
        pad_l: float, ep: ExposedPad | None = None, shape: str = "roundrect",
        **spec_kw: Any) -> FootprintSpec:
    """SOIC / SSOP / TSSOP: ``pins`` (even) in two rows ``span`` mm apart
    (centre to centre), pin 1 top left, numbered down the left and up the
    right; ``pad_w`` along the pitch, ``pad_l`` across it."""
    if pins < 2 or pins % 2:
        raise ValueError(f"{name}: an SOP needs an even pin count >= 2, got {pins}")
    pads = perimeter_pads({"L": pins // 2, "R": pins // 2}, {"L": -span / 2, "R": span / 2},
                          pitch, pad_w, pad_l, shape)
    return _spec(name, _with_ep(pads, ep), body, {"pin1_side": "T"}, spec_kw)


def pin_header(name: str, *, rows: int, cols: int = 1, pitch: float = 2.54, drill: float = 1.0,
               pad: float = 1.7, **spec_kw: Any) -> FootprintSpec:
    """Through-hole header, origin at pin 1, rows along +Y; pin n = row*cols +
    col + 1 (2-row headers zig-zag: 1 left, 2 right). Pin 1 is square."""
    if rows < 1 or cols < 1:
        raise ValueError(f"{name}: rows and cols must be >= 1")
    pads = [Pad(str(r * cols + c + 1), round(c * pitch, 6), round(r * pitch, 6), pad, pad,
                type="thru_hole", shape="rect" if r == c == 0 else "circle", drill=drill)
            for r in range(rows) for c in range(cols)]
    body = Body(cols * pitch, rows * pitch, x=(cols - 1) * pitch / 2, y=(rows - 1) * pitch / 2)
    return _spec(name, pads, body, {"attr": "through_hole", "pin1_side": "L"}, spec_kw)


# ---------------------------------------------------------------------------
# Quectel LG290P(03), LGA-79 (ROADMAP D1)
# ---------------------------------------------------------------------------
LG290P_NAME = "Quectel_LG290P"
LG290P_DATASHEET = ("https://cdn.sparkfun.com/assets/b/1/7/6/3/"
                    "Quectel_LG290P_03__Hardware_Design_V1.0.pdf")
LG290P_SOURCE = ("Quectel LG290P(03) Hardware Design V1.0 (2024-08-21): Fig. 18 Recommended "
                 "Footprint (land pattern = module pads, 1:1), Fig. 3 Pin Assignment, Table 6")
# Fig. 3 / Table 6: pad -> function
LG290P_PAD_FUNCTIONS: dict[str, str] = {
    "1": "RESERVED", "2": "RESERVED", "3": "1PPS", "4": "EVENT", "5": "RESERVED",
    "6": "TXD2", "7": "RXD2", "8": "RESET_N", "9": "VDD_RF", "10": "GND", "11": "RF_IN",
    "12": "GND", "13": "GND", "14": "RTK_STAT/ANT_ON", "15": "RXD3", "16": "TXD3",
    "17": "RESERVED", "18": "I2C_SDA", "19": "I2C_SCL", "20": "TXD1", "21": "RXD1",
    "22": "V_BCKP", "23": "VCC", **{str(n): "GND" for n in range(24, 80)},
}


def lg290p_numbering(col: int, row: int) -> str:
    """Inner grid, column-major from the top-left round pad: 25 + 11*col + row."""
    return str(25 + 11 * col + row)


def lg290p(**spec_kw: Any) -> FootprintSpec:
    """Quectel LG290P(03), LGA-79, exactly per Hardware Design V1.0 Fig. 18.

    24 edge pads 1.5 x 0.8 mm at 1.1 mm pitch, x = +/-5.05 (1.05 mm from the
    body edge): pins 1-7 at y = -7.0 .. -0.4, a 3.0 mm gap, pins 8-12 at
    y = 2.6 .. 7.0; pins 13-24 mirror them on the right, 13 bottom, 24 top.
    55 inner pads 1.0 x 1.0 mm on a 5 x 11 grid at 1.4 mm pitch
    (x = -2.8 .. 2.8, y = -7.0 .. 7.0); pad 25 (top left) is a 1.0 mm round
    pad. Body 12.2 (+0.3/-0.15) x 16.0 (+0.3/-0.15) x 2.6 +/- 0.2 mm; the
    courtyard uses the max body (12.5 x 16.3) and the height the max (2.8).

    Keyword arguments go to the FootprintSpec (descr, tags, datasheet, model,
    pin1, pin1_side, attr_flags, courtyard_excess). The pin-1 marker sits
    above pin 1 ("T"): left of it the courtyard (+/-6.5) has no room for it.
    """
    left = pitch_run(-7.0, 1.1, 7) + pitch_run(2.6, 1.1, 5)            # pins 1-7, gap, 8-12
    rows = (EdgeRow("L", -5.05, left, 1, length=1.5, width=0.8),
            EdgeRow("R", 5.05, tuple(reversed(left)), 13, length=1.5, width=0.8))
    grid = PadGrid(xs=pitch_run(-2.8, 1.4, 5), ys=pitch_run(-7.0, 1.4, 11), w=1.0, h=1.0)
    defaults = {
        "descr": ("Quectel LG290P(03) multi-band RTK GNSS module, LGA-79, 12.2x16.0x2.6 mm; "
                  f"{LG290P_SOURCE}; keep other components >= 3 mm from the module"),
        "tags": "Quectel LG290P GNSS RTK LGA-79", "datasheet": LG290P_DATASHEET,
        "pin1_side": "T"}
    return lga_grid(
        LG290P_NAME, body=Body(12.2, 16.0, w_max=12.5, h_max=16.3, height=2.8),
        edge_rows=rows, grid=grid, numbering=lg290p_numbering, shapes={"25": "circle"},
        **{**defaults, **spec_kw})


def pad_groups(functions: Mapping[str, str]) -> dict[str, list[str]]:
    """{function: [pad numbers in natural order]}: lets a netlist name all 59
    LG290P GND pads at once (D1 ``pin_groups``)."""
    groups: dict[str, list[str]] = {}
    for number in sorted(functions, key=natural_key):
        groups.setdefault(functions[number], []).append(number)
    return groups
