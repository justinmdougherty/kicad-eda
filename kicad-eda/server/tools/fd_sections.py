"""Cross-sections for the 2-D FD field solver (ADR D7.6, task D7-06). Stdlib only.

Ported from the [R-imp] research section builders (ADR D7.14 vectors) and generalised to a
tline LineGeometry. Frame: mm, x across the line (every
section is mirror-symmetric about x = 0), y up; y = 0 is the surface of the reference plane
below the line ('cpw' has no back plane: y = 0 is the bottom of its substrate).

Geometry conventions (LineGeometry, D7-04/D7-05):
- `below` / `above` are the plies strictly between copper surfaces, ordered outward from the
  trace. `below` leads to the reference plane; `above` leads to the top plane (closed kinds:
  stripline, dual_stripline) or is the cover of an open kind (embedded microstrip).
- A copper layer between the trace and a reference that is treated as dielectric (dual
  stripline `dielectric_copper`, a voided layer) is listed as a ply of its own (thickness = the
  copper thickness, er = the resin that fills it). A `Void` puts 0 V copper outside a cut
  corridor on such a ply; a void without a corridor leaves the ply pure dielectric.
- The line's own copper layer is filled beside the copper with the lower-Er adjacent ply: the
  resin-rich prepreg that flows around etched copper (its Er is below the core's in every
  preset: JLC 7628 4.4 vs 4.43-4.6, 2116 4.16 vs 4.41, RO4450F 3.52 vs RO4350B 3.66).
  SectionOptions.trace_fill_er overrides it. An exposed trace (open kind without a cover)
  sits in air or in the solder mask.
- Broadside pairs: the partner trace lies on the `below` side (else on `above`); `pair_gap` is
  the face gap (the dielectric between the facing surfaces) and must equal the summed thickness
  of the first plies on that side; the partner's own copper (thickness t) is inserted there.
- Solder mask (Polar convention): c1 = over_substrate over bare substrate, c2 = over_trace
  conformal on copper tops and sidewalls, c3 = between filling the gaps between copper; over a
  cover stack the mask is a flat c1 layer on top. Closed kinds ignore it.
- Same-layer (coplanar) grounds: `ground_gap` from the outer copper edge, `ground_width` per
  side (default max(1.0 mm, h), D7.6; h = the dielectric to the reference below, for closed
  kinds the thicker side). They are modelled on EVERY kind that sets ground_gap (D7.6 step 4:
  `line: cpwg` adds same-layer ground on any eligible layer), so a stripline with grounds is
  solved with them, never silently without; cpwg/cpw require ground_gap. A ground_width
  without a ground_gap and grounds beside a broadside pair are rejected (ValueError). Every
  reference is 0 V: GND and PWR planes are both AC ground.
- Etch: `etch` is the total top-width reduction, a trapezoid from `width` at the base (the
  `below` face) to width - etch at the top, staircased in ETCH_STEPS rectangles.
Boxes ([R-imp]): open kinds are solved in a Dirichlet and in a Neumann box 20 h beyond the
copper and the two answers averaged; closed kinds are one Dirichlet box whose side walls sit
8 b beyond the copper (the same-layer grounds included).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from itertools import pairwise

from tline.types import LineGeometry

OPEN_KINDS = ("microstrip", "embedded_microstrip", "cpwg", "cpw")
CLOSED_KINDS = ("stripline", "dual_stripline")
SUPPORTED_KINDS = OPEN_KINDS + CLOSED_KINDS
COPLANAR_KINDS = ("cpwg", "cpw")
BOX_H = 20.0                  # open box: sides and top 20 h beyond the copper (research)
WALL_B = 8.0                  # closed box: side walls 8 b beyond the copper (research)
GROUND_WIDTH_MIN_MM = 1.0     # coplanar ground default max(1.0 mm, h) (D7.6)
ETCH_STEPS = 8                # trapezoid staircase (research run_extra.py)
GAP_MATCH_MM = 1e-6           # broadside pair_gap vs the summed plies
GND = "gnd"
SIDES = ("below", "above")


def _require(ok: bool, message: str) -> None:
    if not ok:
        raise ValueError(message)


def _positive(value: float | None) -> bool:
    return value is not None and math.isfinite(value) and value > 0.0


@dataclass(frozen=True)
class Rect:
    """Axis-aligned rectangle, mm (a zero-height rectangle is a zero-thickness strip)."""

    x0: float
    x1: float
    y0: float
    y1: float

    def __post_init__(self) -> None:
        values = (self.x0, self.x1, self.y0, self.y1)
        _require(all(math.isfinite(v) for v in values) and self.x0 <= self.x1
                 and self.y0 <= self.y1, f"bad rectangle {values}")


@dataclass(frozen=True)
class Void:
    """A copper ply (`side`, index `ply`, 0 = next to the trace) cut away in a corridor of
    width `corridor` centred on the line; the copper outside it is 0 V. None: cut entirely."""

    side: str
    ply: int
    corridor: float | None = None

    def __post_init__(self) -> None:
        _require(self.side in SIDES, f"void side must be one of {SIDES}, got {self.side!r}")
        _require(isinstance(self.ply, int) and self.ply >= 0,
                 f"void ply must be an index >= 0, got {self.ply!r}")
        _require(self.corridor is None or _positive(self.corridor),
                 f"void corridor must be None or > 0 mm, got {self.corridor!r}")


@dataclass(frozen=True)
class SectionOptions:
    """Section details LineGeometry does not carry: the fill Er of the line's own copper
    layer(s) and voided plies."""

    trace_fill_er: float | None = None
    voids: tuple[Void, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "voids", tuple(self.voids))
        fill = self.trace_fill_er
        _require(fill is None or (math.isfinite(fill) and fill >= 1.0),
                 f"trace_fill_er must be None or >= 1, got {fill!r}")
        for void in self.voids:
            _require(isinstance(void, Void), f"voids holds {type(void).__name__}, expected Void")


@dataclass(frozen=True)
class Mode:
    """One excitation: conductor potentials (V; 'gnd' and unnamed conductors are 0 V), the
    x = 0 mirror ('even' = magnetic wall, 'odd' = electric wall) and the number of active
    lines in the full structure (the per-line capacitance is 2 W / lines)."""

    name: str
    potentials: tuple[tuple[str, float], ...]
    mirror: str
    lines: int


@dataclass(frozen=True)
class Section:
    """One box: x in [-x_half, x_half], y in [y0, y1]; dielectrics painted in order over air
    (Er 1); `boundary` applies to the sides and top (and to the bottom unless it is grounded);
    h_ref scales the grid's largest cell (None: no scaling)."""

    x_half: float
    y0: float
    y1: float
    dielectrics: tuple[tuple[Rect, float], ...]
    conductors: tuple[tuple[str, Rect], ...]
    boundary: str
    bottom_ground: bool
    top_ground: bool
    h_ref: float | None


@dataclass(frozen=True)
class Problem:
    """Sections (averaged by the solver) and the modes to solve in each."""

    sections: tuple[Section, ...]
    modes: tuple[Mode, ...]
    pair: str


@dataclass(frozen=True)
class _Slab:
    """One layer of the vertical stack: a ply, or the line's / the partner's copper layer
    (er = its fill, None when exposed)."""

    y0: float
    y1: float
    er: float | None
    role: str                 # 'ply' | 'trace' | 'partner'
    side: str = ""
    index: int = -1


# ---------------------------------------------------------------- validation
def _check(geom: LineGeometry) -> None:
    _require(geom.kind in SUPPORTED_KINDS,
             f"fd_sections: unsupported kind {geom.kind!r}; supported: {SUPPORTED_KINDS}")
    _require(geom.width > 0.0, f"{geom.kind}: width must be > 0 mm, got {geom.width!r}")
    _require(geom.etch < geom.width,
             f"{geom.kind}: etch {geom.etch} mm must be below the width {geom.width} mm")
    _require(bool(geom.below), f"{geom.kind}: no plies below the trace (its reference stack)")
    _require(geom.kind not in CLOSED_KINDS or bool(geom.above),
             f"{geom.kind}: needs plies above the trace (to its top reference)")
    _require(geom.pair == "single" or _positive(geom.pair_gap),
             f"{geom.kind}/{geom.pair}: pair_gap must be > 0 mm, got {geom.pair_gap!r}")
    _check_grounds(geom)


def _check_grounds(geom: LineGeometry) -> None:
    if geom.kind in COPLANAR_KINDS or geom.ground_gap is not None:
        _require(_positive(geom.ground_gap),
                 f"{geom.kind}: ground_gap must be > 0 mm, got {geom.ground_gap!r}")
    _require(geom.ground_width is None or geom.ground_gap is not None,
             f"{geom.kind}: ground_width {geom.ground_width} mm without a ground_gap "
             "(same-layer grounds need both, or neither)")
    _require(geom.ground_width is None or _positive(geom.ground_width),
             f"{geom.kind}: ground_width must be None or > 0 mm, got {geom.ground_width!r}")
    _require(geom.ground_gap is None or geom.pair != "broadside",
             f"{geom.kind}/broadside: same-layer grounds (ground_gap {geom.ground_gap} mm) "
             "beside a broadside pair are not modelled")


def default_ground_width(geom: LineGeometry) -> float:
    """The same-layer ground width the line has or would have: declared, else max(1.0 mm, h)
    (D7.6), h = the dielectric to the reference below (closed kinds: the thicker side)."""
    if geom.ground_width is not None:
        return geom.ground_width
    h = sum(p.thickness for p in geom.below)
    if geom.kind in CLOSED_KINDS:
        h = max(h, sum(p.thickness for p in geom.above))
    return max(GROUND_WIDTH_MIN_MM, h)


def effective_ground_width(geom: LineGeometry) -> float | None:
    """Same-layer ground width (see default_ground_width); None without grounds."""
    return None if geom.ground_gap is None else default_ground_width(geom)


def partner_split(geom: LineGeometry) -> tuple[str, int] | None:
    """(side, k) of a broadside partner: it sits after the first k plies of `side`."""
    if geom.pair != "broadside":
        return None
    for side in SIDES:
        plies, depth = getattr(geom, side), 0.0
        for k, ply in enumerate(plies[:-1], start=1):
            depth += ply.thickness
            if abs(depth - geom.pair_gap) <= GAP_MATCH_MM:
                return side, k
    raise ValueError(f"{geom.kind}/broadside: pair_gap {geom.pair_gap} mm must equal the summed "
                     "thickness of the first plies on one side, with plies beyond the partner")


# ---------------------------------------------------------------- vertical stack
def _side_layers(geom: LineGeometry, side: str, split: tuple[str, int] | None) -> list[tuple]:
    """(role, index, thickness, er) outward from the trace on one side."""
    out: list[tuple] = []
    for i, ply in enumerate(getattr(geom, side)):
        if split == (side, i):
            out.append(("partner", -1, geom.t, None))
        out.append(("ply", i, ply.thickness, ply.er))
    return out


def _fill(layers: list[tuple], at: int, override: float | None) -> float | None:
    """Fill Er of the copper layer at `at`: the override, else the lower-Er adjacent ply when
    plies sit on both sides, else None (exposed)."""
    if override is not None:
        return override
    near = [layers[j][3] for j in (at - 1, at + 1)
            if 0 <= j < len(layers) and layers[j][0] == "ply"]
    return min(near) if len(near) == 2 else None


def _stack(geom: LineGeometry, options: SectionOptions) -> tuple[_Slab, ...]:
    """The vertical stack bottom-up from the reference plane surface (y = 0)."""
    split = partner_split(geom)
    below = _side_layers(geom, "below", split)
    layers = [*below[::-1], ("trace", -1, geom.t, None), *_side_layers(geom, "above", split)]
    slabs, y = [], 0.0
    for at, (role, index, thickness, er) in enumerate(layers):
        if role != "ply":
            er = _fill(layers, at, options.trace_fill_er)
        side = "" if role != "ply" else ("below" if at < len(below) else "above")
        slabs.append(_Slab(y, y + thickness, er, role, side, index))
        y += thickness
    return tuple(slabs)


# ---------------------------------------------------------------- copper
def _line_spans(geom: LineGeometry) -> tuple[tuple[str, float, float], ...]:
    """(name, x0, x1) of the line's own traces at their base."""
    w = geom.width
    if geom.pair == "edge":
        s = geom.pair_gap / 2.0
        return (("p", s, s + w), ("n", -s - w, -s))
    if geom.pair == "broadside":
        return (("p", -w / 2.0, w / 2.0),)
    return (("sig", -w / 2.0, w / 2.0),)


def _ground_spans(geom: LineGeometry, extent: float) -> tuple[tuple[float, float], ...]:
    width = effective_ground_width(geom)
    if width is None:
        return ()
    x0 = extent + geom.ground_gap
    return ((-x0 - width, -x0), (x0, x0 + width))


def _trace_rects(x0: float, x1: float, y0: float, t: float, etch: float) -> tuple[Rect, ...]:
    """A trace at base [x0, x1] on y0: one rectangle, or the etch trapezoid staircase."""
    if etch <= 0.0 or t <= 0.0:
        return (Rect(x0, x1, y0, y0 + t),)
    xc, w = 0.5 * (x0 + x1), x1 - x0
    rects = []
    for i in range(ETCH_STEPS):
        wi = w - etch * (i + 0.5) / ETCH_STEPS
        rects.append(Rect(xc - wi / 2.0, xc + wi / 2.0, y0 + t * i / ETCH_STEPS,
                          y0 + t * (i + 1) / ETCH_STEPS))
    return tuple(rects)


def _copper(geom: LineGeometry, stack: tuple[_Slab, ...], x_half: float,
            options: SectionOptions) -> tuple[tuple[str, Rect], ...]:
    trace = next(s for s in stack if s.role == "trace")
    spans = _line_spans(geom)
    out = [(name, r) for name, x0, x1 in spans
           for r in _trace_rects(x0, x1, trace.y0, geom.t, geom.etch)]
    partner = next((s for s in stack if s.role == "partner"), None)
    if partner is not None:
        w = geom.width / 2.0
        out += [("n", r) for r in _trace_rects(-w, w, partner.y0, geom.t, geom.etch)]
    extent = max(abs(v) for _, x0, x1 in spans for v in (x0, x1))
    out += [(GND, Rect(x0, x1, trace.y0, trace.y1)) for x0, x1 in _ground_spans(geom, extent)]
    return tuple(out + list(_void_copper(stack, x_half, options)))


def _void_copper(stack: tuple[_Slab, ...], x_half: float,
                 options: SectionOptions) -> tuple[tuple[str, Rect], ...]:
    out = []
    for void in options.voids:
        slab = next((s for s in stack if (s.side, s.index) == (void.side, void.ply)), None)
        _require(slab is not None, f"void {void}: no ply {void.ply} on the {void.side} side")
        if void.corridor is None or void.corridor / 2.0 >= x_half:
            continue
        c = void.corridor / 2.0
        out += [(GND, Rect(-x_half, -c, slab.y0, slab.y1)),
                (GND, Rect(c, x_half, slab.y0, slab.y1))]
    return tuple(out)


# ---------------------------------------------------------------- dielectrics
def _mask(geom: LineGeometry, copper: tuple[tuple[str, Rect], ...], trace: _Slab,
          x_half: float) -> list[tuple[Rect, float]]:
    """Conformal solder mask over an exposed trace layer (Polar c1 / c2 / c3)."""
    m, ys = geom.mask, trace.y0
    on_layer = [r for _, r in copper if r.y0 >= ys - GAP_MATCH_MM and r.y1 <= trace.y1 + 1e-12]
    out = [(Rect(-x_half, x_half, ys, ys + m.over_substrate), m.er)]
    out += [(Rect(r.x0 - m.over_trace, r.x1 + m.over_trace, ys, r.y1 + m.over_trace), m.er)
            for r in on_layer]
    bases = sorted({(r.x0, r.x1) for r in on_layer if r.y0 <= ys + 1e-12})
    out += [(Rect(a1, b0, ys, ys + m.between), m.er)
            for (_, a1), (b0, _) in pairwise(bases) if b0 > a1]
    return out


def _dielectrics(geom: LineGeometry, stack: tuple[_Slab, ...], copper: tuple,
                 x_half: float) -> tuple[tuple[Rect, float], ...]:
    out = [(Rect(-x_half, x_half, s.y0, s.y1), s.er) for s in stack
           if s.er is not None and s.y1 > s.y0]
    trace = next(s for s in stack if s.role == "trace")
    if geom.mask is not None and geom.kind in OPEN_KINDS:
        if geom.above:
            top = stack[-1].y1
            out.append((Rect(-x_half, x_half, top, top + geom.mask.over_substrate), geom.mask.er))
        else:
            out += _mask(geom, copper, trace, x_half)
    return tuple((r, er) for r, er in out if r.y1 > r.y0)


# ---------------------------------------------------------------- boxes and modes
def _modes(pair: str) -> tuple[Mode, ...]:
    if pair == "single":
        return (Mode("se", (("sig", 1.0),), "even", 1),)
    odd_mirror = "odd" if pair == "edge" else "even"
    return (Mode("even", (("p", 1.0), ("n", 1.0)), "even", 2),
            Mode("odd", (("p", 1.0), ("n", -1.0)), odd_mirror, 2))


def _x_half(geom: LineGeometry, b_or_h: float, closed: bool) -> float:
    """Box half-width ([R-imp] spans), widened by the same-layer grounds."""
    w, gap = geom.width, geom.pair_gap if geom.pair == "edge" else 0.0
    ground_width = effective_ground_width(geom)
    grounds = 0.0 if ground_width is None else geom.ground_gap + ground_width
    if closed:
        return (w if geom.pair == "broadside" else 2.0 * w + gap) + grounds + WALL_B * b_or_h
    return BOX_H * b_or_h + 2.0 * (w + gap + grounds)


def build(geom: LineGeometry, options: SectionOptions | None = None) -> Problem:
    """The FD problem for a line cross-section (see the module docstring for conventions)."""
    options = options or SectionOptions()
    _check(geom)
    stack = _stack(geom, options)
    closed = geom.kind in CLOSED_KINDS
    trace = next(s for s in stack if s.role == "trace")
    h = trace.y0
    x_half = _x_half(geom, stack[-1].y1 if closed else h, closed)
    copper = _copper(geom, stack, x_half, options)
    dielectrics = _dielectrics(geom, stack, copper, x_half)
    if closed:
        sec = Section(x_half, 0.0, stack[-1].y1, dielectrics, copper, "dirichlet", True, True, None)
        return Problem((sec,), _modes(geom.pair), geom.pair)
    top = max(h, stack[-1].y1 if geom.above else h) + BOX_H * h
    grounded = geom.kind != "cpw"
    y0 = 0.0 if grounded else -BOX_H * h
    sections = tuple(Section(x_half, y0, top, dielectrics, copper, bnd, grounded, False, h)
                     for bnd in ("dirichlet", "neumann"))
    return Problem(sections, _modes(geom.pair), geom.pair)
