#!/usr/bin/env python3
"""Part presets: footprint + symbol for the SPS GNSS modules (F2, ROADMAP D1).

``PRESETS`` maps a preset name to ``(footprint_fn, symbol_spec_fn)``:

  * ``lg290p``       Quectel LG290P(03), LGA-79 (fp_author_templates.lg290p,
                     sym_author.lg290p_spec);
  * ``lc29h``        Quectel LC29H (AA/AI/BS/DA), LCC-24: 24 oval 2.5 x 0.8 mm
                     pads on 1.1 mm pitch centred on the module edge
                     (x = +/-6.1), in the LG290P's edge rows (pin 1 at y = -7.0);
  * ``lg290p_lc29h`` OPT-IN: one land pattern that takes either module.

``footprint_fn(**spec_kw)`` returns a fp_author FootprintSpec (keywords go to
FootprintSpec: descr, tags, datasheet, model, pin1, pin1_side, attr_flags,
courtyard_excess; the combined one also takes ``stencil="lg290p"|"lc29h"``).
``symbol_spec_fn(footprint="")`` returns a sym_author SymbolSpec whose
Footprint property is ``footprint`` (``"<nick>:<footprint name>"``). Pad
number = pin number for every preset. ``get_preset(name, opt_in=False)``
refuses an opt-in preset unless asked; ``preset_info(name)`` describes one.

Netlist hints (F2-S1b): ``preset_hints(name)`` runs lga_strategy on the
preset's own pad table and ground pads (PRESET_GND_PADS) with F2-S1's winning
strategy (f): the LG290P (and the combined land pattern) get 40 generated GND
escape vias as designer ``preroutes`` in the footprint frame; the LC29H has no
inner grid and gets none. Place them at the part with
``lga_strategy.apply_hints(design, hints, ref=...)``. ``preset_info`` carries
the hints and a ``strategy`` note with F2-S1's measured numbers.

Decision (F2-12): separate footprints by default. The LC29H and LG290P share
a 12.2 x 16.0 mm body and the edge numbering of the supplies, UART1, I2C,
RF_IN, RESET_N and 1PPS, but on one land pattern:

  * pins 1, 4, 5, 6, 7, 15 and 16 conflict (COMBINED_CONFLICTS): 1 RESERVED /
    WAKEUP, 4 EVENT / RESERVED, 5 RESERVED / D_SEL1, 6 TXD2 / D_SEL2, 7 RXD2 /
    VDD_EXT (an LC29H 2.8 V output), 15/16 LG290P UART3 at 3.3 V against
    LC29H UART2 at 1.8 V;
  * the union edge pad spans |x| = 4.3 .. 7.35 (LG290P 1.5 mm land under the
    body, LC29H 2.5 mm land on the edge);
  * the LG290P's 55 inner GND pads would get paste under an LC29H body that
    has no pads there.

So the combined land pattern is opt-in only: it comes as two stencil variants
(``Quectel_LG290P_LC29H_StencilLG290P``: paste on the LG290P lands and the
inner grid; ``..._StencilLC29H``: paste on the LC29H lands, none on the inner
grid; the copper is identical), and its symbol puts every conflicting pin on
N/C. Record the choice in the napkin decision log (``netlist`` is the
netlist.json path):

    >>> import decisions
    >>> entry = decisions.append_decision(
    ...     netlist, stage="parts",
    ...     question="One footprint that takes either an LG290P or an LC29H?",
    ...     decision=("No: Quectel_LG290P for LG290P builds, Quectel_LC29H for "
    ...               "LC29H builds (part_presets lg290p / lc29h)"),
    ...     rationale=("pins 1, 4-7, 15 and 16 differ in function or voltage, and the "
    ...                "LG290P inner-grid paste would sit under an LC29H body with no "
    ...                "pads there"),
    ...     alternatives=["opt-in preset lg290p_lc29h: Quectel_LG290P_LC29H_StencilLG290P"
    ...                   " / _StencilLC29H, conflicting pins N/C"],
    ...     confidence="high",
    ...     source="part_presets (F2-12); LC29H HD V1.3 Fig. 19 p. 47; LG290P HD V1.0 Fig. 18",
    ...     override="components.U1.footprint")
    >>> entry["stage"], entry["confidence"]
    ('parts', 'high')

Footprints and hints need only the stdlib (KiCad's Python 3.11 builds them); the
symbol functions import sym_author (which needs sexpdata, the venv) when called.
"""
from __future__ import annotations

import difflib
from dataclasses import replace
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Callable, Mapping, NamedTuple

import fp_author_templates as ft
import lga_strategy as lga
from fp_author_templates import Body, EdgeRow, FootprintSpec, Pad, has_copper, pad_bbox, r6

if TYPE_CHECKING:                                # sym_author needs sexpdata (venv only)
    from sym_author import PinSpec, SymbolSpec

EDGE_PADS = 24                                   # pins 1-24 on both modules
STENCILS = ("lg290p", "lc29h")
NO_PASTE = ("F.Cu", "F.Mask")
PASTE_ONLY = ("F.Paste",)

# ---------------------------------------------------------------------------
# Quectel LC29H, LCC-24
# ---------------------------------------------------------------------------
LC29H_NAME = "Quectel_LC29H"
LC29H_SOURCE = ("Quectel LC29H Series Hardware Design V1.3 (2024-07-30): Fig. 19 Recommended "
                "Footprint (p. 47), Fig. 20 Top, Side and Bottom View Dimensions (p. 52), "
                "Fig. 4 Pin Assignment (p. 25), Table 6 Pin Description (pp. 26-28)")
# Fig. 19 "Recommended Footprint", printed page 47 (PDF page 48): the 12.2 mm module-edge
# extension lines pass through the pad centres; "1.25" runs from the pad's outer end to the
# module edge and "2.5" is the pad length, so every pad is centred ON the module edge:
# x = +/-12.2 / 2 = +/-6.1. The "1.45" and "15.1" dimensions belong to the SAFE AREA outline
# (0.2 mm beyond the pad ends); reading them as the pad extent gives 6.3 (as the SPS
# lc29h_reference.md did). Measured from the PDF's vector drawing: pad centres 12.200 mm
# apart, pads 2.500 mm long, pin 1 centre 1.0 mm below the module's top edge ("1.15" is
# from the safe-area edge, 0.15 mm above it), pitch 1.1, 3.0 mm gap after pin 7.
LC29H_PAD_X = 6.1
LC29H_PAD = (2.5, 0.8)                           # oval, x by y
EDGE_Y = ft.pitch_run(-7.0, 1.1, 7) + ft.pitch_run(2.6, 1.1, 5)   # pins 1-12 (= LG290P rows)

_RESERVED_NC = "leave N/C, never tie to power or GND"
_IO28_OUT = "DO, 2.8 V I/O: VOLmax 0.35 V, VOHmin 2.1 V"
_IO28_IN = ("DI, 2.8 V I/O: VIL -0.3..0.7 V, VIH 1.75..3.08 V "
            "(VIHmax 3.08 V: 3.3 V logic exceeds it)")
_IO18_IN = "DI, 1.8 V domain: VIL -0.3..0.63 V, VIH 1.17..2.1 V"
_D_SEL = ("interface select, 75 kOhm internal pull-down (sec. 4.1.1.1): both low "
          "(N/C, default) = UART1 + I2C (Table 8)")
# Table 6 (pp. 26-28) for variants AA / AI / BS / DA: (number, name, type, side, group, note)
LC29H_PIN_TABLE: tuple[tuple[str, str, str, str, str, str], ...] = (
    ("23", "VCC", "power_in", "top", "POWER", "PI main supply 3.1..3.6 V (3.3 V typ)"),
    ("22", "V_BCKP", "power_in", "top", "POWER",
     "PI backup supply 2.2..3.6 V (3.3 V typ); needed for start-up, keep powered for hot start"),
    ("9", "VDD_RF", "power_out", "top", "POWER",
     "PO = VCC: supply for an active antenna / external LNA; N/C if unused"),
    ("7", "VDD_EXT", "power_out", "top", "POWER",
     "PO 2.8 V for external circuits, 100 mA max (the 2.8 V I/O reference)"),
    ("11", "RF_IN", "input", "left", "ANT", "AI GNSS antenna input, 50 ohm"),
    ("8", "RESET_N", "input", "left", "SYSTEM",
     "DI reset, active low: VIL -0.3..0.1 V, VIH 1.8..3.6 V"),
    ("1", "WAKEUP", "input", "left", "SYSTEM",
     "DI, backup domain, internal pull-down: VIL -0.3..0.7 V, VIH 3.0..3.6 V; high >= 10 ms "
     "leaves Backup mode; N/C if unused"),
    ("5", "D_SEL1", "input", "left", "SYSTEM", f"{_IO18_IN}; {_D_SEL}"),
    ("6", "D_SEL2", "input", "left", "SYSTEM", f"{_IO18_IN}; {_D_SEL}"),
    ("2", "RESERVED", "no_connect", "left", "RESERVED",
     f"RESERVED (FWD on BA/CA): {_RESERVED_NC}"),
    ("4", "RESERVED", "no_connect", "left", "RESERVED",
     f"RESERVED (WHEELTICK on BA/CA): {_RESERVED_NC}"),
    ("17", "RESERVED", "no_connect", "left", "RESERVED",
     f"RESERVED (WI on BA/CA): {_RESERVED_NC}"),
    ("20", "TXD1/SPI_MISO", "output", "right", "UART1", f"{_IO28_OUT}; UART1 TX / SPI MISO"),
    ("21", "RXD1/SPI_MOSI", "input", "right", "UART1", f"{_IO28_IN}; UART1 RX / SPI MOSI"),
    ("15", "TXD2", "output", "right", "UART2",
     "DO, 1.8 V domain: VOLmax 0.45 V, VOHmin 1.35 V; debug UART2 TX, N/C if unused"),
    ("16", "RXD2", "input", "right", "UART2", f"{_IO18_IN}; debug UART2 RX, N/C if unused"),
    ("18", "I2C_SDA/SPI_CS", "bidirectional", "right", "I2C",
     "DIO, 2.8 V I/O: VIH 1.75..3.08 V (VIHmax 3.08 V), VOLmax 0.35 V, VOHmin 2.1 V"),
    ("19", "I2C_SCL/SPI_CLK", "input", "right", "I2C", _IO28_IN),
    ("3", "1PPS", "output", "right", "STATUS",
     f"{_IO28_OUT}; one pulse per second, rising edge; N/C if unused"),
    ("14", "ANT_ON", "output", "right", "STATUS",
     f"{_IO28_OUT}; external LNA / active-antenna power control"),
    *((n, "GND", "power_in", "bottom", "GND", "ground") for n in ("10", "12", "13", "24")),
)
LC29H_PIN_NOTES: Mapping[str, str] = MappingProxyType(
    {row[0]: f"{row[1]}: {row[5]}" for row in LC29H_PIN_TABLE})
LC29H_DESCRIPTION = (
    "Quectel LC29H (AA/AI/BS/DA) dual-band multi-constellation GNSS module (BS: RTK base, "
    "DA: RTK rover), 24-pin LCC, 12.2 x 16.0 x 2.5 mm; I/O 2.8 V (outputs VOHmin 2.1 V; "
    "RXD1 / I2C inputs VIHmax 3.08 V), D_SEL1/D_SEL2 and UART2 1.8 V, VDD_EXT 2.8 V out "
    "(100 mA max)")


def lc29h(**spec_kw: Any) -> FootprintSpec:
    """Quectel LC29H, LCC-24, per Hardware Design V1.3 Fig. 19 (p. 47).

    24 oval pads 2.5 x 0.8 mm at 1.1 mm pitch centred on the module edge,
    x = +/-6.1 (|x| 4.85 .. 7.35): pins 1-7 at y = -7.0 .. -0.4, a 3.0 mm gap,
    pins 8-12 at y = 2.6 .. 7.0; pins 13-24 mirror them, 13 bottom, 24 top.
    Body 12.2 x 16.0 (+/-0.15) x 2.5 +/- 0.2 mm (Fig. 20): the courtyard uses
    the max body (12.35 x 16.15), the height the max (2.7). Keyword arguments
    go to the FootprintSpec; the pin-1 marker sits above pin 1 ("T").
    """
    rows = (EdgeRow("L", -LC29H_PAD_X, EDGE_Y, 1, *LC29H_PAD, shape="oval"),
            EdgeRow("R", LC29H_PAD_X, tuple(reversed(EDGE_Y)), 13, *LC29H_PAD, shape="oval"))
    defaults = {
        "descr": ("Quectel LC29H dual-band GNSS module, LCC-24, 12.2x16.0x2.5 mm; "
                  f"{LC29H_SOURCE}; keep other components >= 3 mm from the module"),
        "tags": "Quectel LC29H GNSS RTK LCC-24", "pin1_side": "T"}
    return ft.lga_grid(LC29H_NAME, body=Body(12.2, 16.0, w_max=12.35, h_max=16.15, height=2.7),
                       edge_rows=rows, **{**defaults, **spec_kw})


def _pin_specs(rows: tuple[tuple[str, ...], ...]) -> tuple[PinSpec, ...]:
    from sym_author import PinSpec          # lazy: keeps footprints sexpdata-free
    return tuple(PinSpec(n, name, etype, side, group) for n, name, etype, side, group, _ in rows)


def lc29h_symbol(footprint: str = "") -> SymbolSpec:
    """Quectel LC29H (AA/AI/BS/DA) symbol: one pin per pad, names and I/O
    types from Table 6 (PI -> power_in, PO -> power_out, DI/AI -> input, DO ->
    output, DIO -> bidirectional, RESERVED -> no_connect); the four GND pads
    stack. Per-pin voltage notes: LC29H_PIN_NOTES."""
    from sym_author import SymbolSpec
    return SymbolSpec(name="LC29H", pins=_pin_specs(LC29H_PIN_TABLE), reference="U",
                      value="LC29H", footprint=footprint, description=LC29H_DESCRIPTION,
                      keywords="GNSS GPS RTK dual-band Quectel LC29H module",
                      fp_filters="*LC29H*")


def _lg290p_symbol(footprint: str = "") -> SymbolSpec:
    from sym_author import lg290p_spec
    return lg290p_spec(footprint)


# ---------------------------------------------------------------------------
# Opt-in: one land pattern for either module
# ---------------------------------------------------------------------------
COMBINED_NAMES: Mapping[str, str] = MappingProxyType({
    "lg290p": "Quectel_LG290P_LC29H_StencilLG290P",
    "lc29h": "Quectel_LG290P_LC29H_StencilLC29H"})
# pin -> (LG290P function, LC29H function, why both cannot be connected)
COMBINED_CONFLICTS: Mapping[str, tuple[str, str, str]] = MappingProxyType({
    "1": ("RESERVED", "WAKEUP", "LG290P RESERVED must stay N/C; LC29H backup-domain input"),
    "4": ("EVENT", "RESERVED", "LG290P EVENT input; LC29H RESERVED must stay N/C"),
    "5": ("RESERVED", "D_SEL1", "LG290P RESERVED; LC29H 1.8 V interface-select strap"),
    "6": ("TXD2", "D_SEL2", "LG290P UART2 TX (3.3 V) would drive the LC29H 1.8 V strap"),
    "7": ("RXD2", "VDD_EXT", "LG290P UART2 RX; LC29H 2.8 V power output (100 mA)"),
    "15": ("RXD3", "TXD2", "LG290P UART3 RX at 3.3 V; LC29H UART2 TX at 1.8 V"),
    "16": ("TXD3", "RXD2", "LG290P UART3 TX at 3.3 V would overdrive LC29H RXD2 (VIHmax 2.1 V)"),
})
COMBINED_DESCRIPTION = (
    "Quectel LG290P(03) or LC29H on the opt-in combined land pattern (part_presets "
    "lg290p_lc29h): pins 1, 4-7, 15 and 16 differ between the modules and are N/C; the "
    "LC29H's I/O is 2.8 V (RXD1 / I2C VIHmax 3.08 V) where the LG290P's is 3.3 V")


def _is_edge(pad: Pad) -> bool:
    return has_copper(pad) and pad.number.isdigit() and int(pad.number) <= EDGE_PADS


def _edge_lands(spec: FootprintSpec) -> list[Pad]:
    return [p for p in spec.pads if _is_edge(p)]


def _union_pad(a: Pad, b: Pad) -> Pad:
    """A rect copper pad (no paste) covering pads a and b, numbered like a."""
    boxes = (pad_bbox(a), pad_bbox(b))
    x0, y0 = min(r[0] for r in boxes), min(r[1] for r in boxes)
    x1, y1 = max(r[2] for r in boxes), max(r[3] for r in boxes)
    return Pad(a.number, r6((x0 + x1) / 2), r6((y0 + y1) / 2), r6(x1 - x0), r6(y1 - y0),
               layers=NO_PASTE)


def _combined_pads(stencil: str) -> list[Pad]:
    """Union edge copper, the chosen module's edge paste windows, the LG290P
    inner grid (pasted for an LG290P build only)."""
    lg, lc = ft.lg290p(), lc29h()
    lc_by_number = {p.number: p for p in _edge_lands(lc)}
    copper = [_union_pad(p, lc_by_number[p.number]) for p in _edge_lands(lg)]
    lands = _edge_lands(lg if stencil == "lg290p" else lc)
    paste = [replace(p, number="", layers=PASTE_ONLY) for p in lands]
    inner = [p for p in lg.pads if not _is_edge(p)]
    if stencil == "lc29h":
        inner = [replace(p, layers=NO_PASTE) for p in inner]
    return copper + paste + inner


def lg290p_lc29h(*, stencil: str = "lg290p", **spec_kw: Any) -> FootprintSpec:
    """OPT-IN combined land pattern for an LG290P(03) or an LC29H.

    Copper: 24 rect edge pads 3.05 x 0.8 mm covering both lands (|x| 4.3 ..
    7.35, F.Cu + F.Mask) and the LG290P's 55 inner GND pads. Paste per build:
    ``stencil="lg290p"`` (Quectel_LG290P_LC29H_StencilLG290P) pastes the
    LG290P lands (1.5 x 0.8 at x = +/-5.05) and the inner grid;
    ``stencil="lc29h"`` (..._StencilLC29H) pastes the LC29H lands (2.5 x 0.8
    oval at x = +/-6.1) and leaves the inner grid bare (mask open, no paste:
    no LC29H pads sit there). The copper is identical, so a board can swap
    the footprint to make the other build's stencil. Body: the LG290P's max
    (12.5 x 16.3 x 2.8 mm). Keyword arguments go to the FootprintSpec.
    """
    if stencil not in STENCILS:
        raise ValueError(f"stencil {stencil!r} not in {STENCILS}")
    defaults = {
        "descr": (f"Opt-in combined land pattern for a Quectel LG290P(03) (LGA-79) or LC29H "
                  f"(LCC-24), {stencil.upper()} build stencil; pins 1, 4-7, 15, 16 must stay "
                  f"N/C; {ft.LG290P_SOURCE}; {LC29H_SOURCE}"),
        "tags": "Quectel LG290P LC29H GNSS RTK combined", "pin1_side": "T"}
    return FootprintSpec(name=COMBINED_NAMES[stencil], pads=tuple(_combined_pads(stencil)),
                         body=Body(12.2, 16.0, w_max=12.5, h_max=16.3, height=2.8),
                         **{**defaults, **spec_kw})


def lg290p_lc29h_symbol(footprint: str = "") -> SymbolSpec:
    """The LG290P symbol with every COMBINED_CONFLICTS pin turned into a
    no_connect pin named "<LG290P function>/<LC29H function>"."""
    base = _lg290p_symbol(footprint)
    pins = tuple(replace(p, name=f"{COMBINED_CONFLICTS[p.number][0]}/"
                                 f"{COMBINED_CONFLICTS[p.number][1]}",
                         etype="no_connect", side="left", group="CONFLICT")
                 if p.number in COMBINED_CONFLICTS else p for p in base.pins)
    return replace(base, name="LG290P_LC29H", value="LG290P_LC29H", pins=pins,
                   description=COMBINED_DESCRIPTION,
                   keywords="GNSS GPS RTK Quectel LG290P LC29H combined",
                   fp_filters="*LG290P_LC29H*")


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------
class Preset(NamedTuple):
    """(footprint_fn(**spec_kw) -> FootprintSpec, symbol_spec_fn(footprint) -> SymbolSpec)."""
    footprint_fn: Callable[..., FootprintSpec]
    symbol_spec_fn: Callable[..., SymbolSpec]


class PresetError(ValueError):
    """An unknown preset, or an opt-in preset requested without opt_in=True."""


PRESETS: Mapping[str, Preset] = MappingProxyType({
    "lg290p": Preset(ft.lg290p, _lg290p_symbol),
    "lc29h": Preset(lc29h, lc29h_symbol),
    "lg290p_lc29h": Preset(lg290p_lc29h, lg290p_lc29h_symbol),
})
DEFAULT_PRESETS: tuple[str, ...] = ("lg290p", "lc29h")
OPT_IN_PRESETS: frozenset[str] = frozenset({"lg290p_lc29h"})


def get_preset(name: str, *, opt_in: bool = False) -> Preset:
    """PRESETS[name]; PresetError for an unknown name (with the closest one)
    or an opt-in preset without ``opt_in=True``."""
    if name not in PRESETS:
        near = difflib.get_close_matches(str(name), list(PRESETS), n=1)
        hint = f" (did you mean {near[0]!r}?)" if near else ""
        raise PresetError(f"unknown preset {name!r}{hint}; presets: {', '.join(PRESETS)}")
    if name in OPT_IN_PRESETS and not opt_in:
        raise PresetError(f"preset {name!r} is opt-in (separate footprints are the default: "
                          f"{', '.join(DEFAULT_PRESETS)}); pass opt_in=True to use it")
    return PRESETS[name]


# ---------------------------------------------------------------------------
# Netlist hints (F2-S1b): the inner GND pads, F2-S1's strategy (f)
# ---------------------------------------------------------------------------
GNSS_MAX_FREQ_HZ = 1.61e9           # emc.stitch spacing: the top GNSS band (F2-S1's emc)
_LG290P_GND = frozenset(n for n, f in ft.LG290P_PAD_FUNCTIONS.items() if f == "GND")
PRESET_GND_PADS: Mapping[str, frozenset[str]] = MappingProxyType({
    "lg290p": _LG290P_GND,                                           # 10, 12, 13, 24-79
    "lc29h": frozenset(row[0] for row in LC29H_PIN_TABLE if row[1] == "GND"),
    "lg290p_lc29h": _LG290P_GND})            # the LC29H's GND pads 10, 12, 13, 24 included
PRESET_STRATEGY: Mapping[str, str] = MappingProxyType({
    "lg290p": (
        "F2-S1 strategy (f), the winner: lga_strategy generates 40 GND vias 0.5/0.25 mm, one "
        "per 2x2 channel of the 5 x 11 inner grid, each with two 0.25 mm F.Cu stubs to its "
        "diagonal corner pads, as designer preroutes (netlist_hints; place them at the part "
        "with lga_strategy.apply_hints). F2-S1, LG290P breakout: DRC 0 / unconnected 0 on "
        "KiCad 9.0.4 (Freerouting 12.0 s) and 10.0.5 (15.3 s), against 35.1 s / 66.8 s for "
        "the default planes (a); every F.Cu GND zone variant (b, e1, e2, e4) failed with 1-4 "
        "unconnected. Keep the builder's planes (In1.Cu GND); never pour GND on F.Cu or B.Cu "
        "before routing"),
    "lc29h": (
        "The LC29H has no inner pad grid (LCC-24; GND edge pads 10, 12, 13, 24), so F2-S1's "
        "escape-via generator (strategy f) adds nothing: the GND pads route as ordinary pads "
        "to the builder's In1.Cu GND plane, strategy (a), which F2-S1 measured at DRC 0 / "
        "unconnected 0 on KiCad 9.0.4 and 10.0.5 on its LG290P breakout (F2-S1 built no "
        "LC29H board)"),
    "lg290p_lc29h": (
        "The inner grid is the LG290P's, so the hints are lg290p's 40 escape vias (F2-S1 "
        "strategy f: DRC 0 / unconnected 0 on KiCad 9.0.4, Freerouting 12.0 s, and 10.0.5, "
        "15.3 s). On an LC29H build (StencilLC29H) the vias tie the bare inner pads to GND "
        "under a body that has no pads there"),
})


def preset_hints(name: str, *, opt_in: bool = False, strategy: str = lga.WINNER,
                 ref: str = "U1", fab: Mapping[str, float] | None = None) -> dict:
    """lga_strategy.netlist_hints on the preset's own pads and PRESET_GND_PADS (footprint
    frame, pad points on part ``ref``; a fresh dict every call). An opt-in preset needs
    ``opt_in=True``, as in get_preset."""
    preset = get_preset(name, opt_in=opt_in)
    return lga.netlist_hints(preset.footprint_fn().pads, gnd_pads=sorted(PRESET_GND_PADS[name]),
                             strategy=strategy, ref=ref, fab=fab,
                             emc_max_freq_hz=GNSS_MAX_FREQ_HZ)


def _combined_info() -> dict:
    return {
        "footprint": COMBINED_NAMES["lg290p"], "footprints": dict(COMBINED_NAMES),
        "symbol": "LG290P_LC29H", "source": f"{ft.LG290P_SOURCE}; {LC29H_SOURCE}",
        "conflicts": {n: {"lg290p": a, "lc29h": b, "why": why}
                      for n, (a, b, why) in COMBINED_CONFLICTS.items()},
        "notes": [
            "F2-12 decision: separate footprints by default (lg290p / lc29h); this "
            "combined land pattern is opt-in",
            "LC29H build: use the StencilLC29H variant (no inner-grid paste); the inner GND "
            "pads stay bare copper under the LC29H body, which has no pads there (Fig. 20)",
            "conflicting pins 1, 4, 5, 6, 7, 15, 16 must stay N/C on the board",
            "shared inputs RXD1 (21), I2C_SDA (18), I2C_SCL (19): LG290P I/O is 3.3 V, LC29H "
            "VIHmax 3.08 V; LC29H outputs (TXD1, 1PPS, ANT_ON) are 2.8 V (VOHmin 2.1 V)"]}


def preset_info(name: str) -> dict:
    """What a preset is (a fresh dict): name, opt_in, footprint(s), symbol,
    source, notes, netlist_hints (preset_hints), the F2-S1 strategy note, and
    the LC29H pin notes or the combined conflicts."""
    get_preset(name, opt_in=True)
    base = {"name": name, "opt_in": name in OPT_IN_PRESETS,
            "netlist_hints": preset_hints(name, opt_in=True), "strategy": PRESET_STRATEGY[name]}
    if name == "lg290p":
        return {**base, "footprint": ft.LG290P_NAME, "symbol": "LG290P",
                "source": ft.LG290P_SOURCE,
                "notes": ["I/O at VCC, 3.3 V (LG290P HD V1.1 Table 6: VOHmin = VCC - 0.4 V)",
                          "59 GND pads (10, 12, 13, 24-79), stacked on the symbol"]}
    if name == "lc29h":
        return {**base, "footprint": LC29H_NAME, "symbol": "LC29H", "source": LC29H_SOURCE,
                "pin_notes": dict(LC29H_PIN_NOTES),
                "notes": [LC29H_DESCRIPTION,
                          "pad centre x = +/-6.1 (on the module edge), Fig. 19 p. 47"]}
    return {**base, **_combined_info()}
