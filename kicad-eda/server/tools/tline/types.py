"""Value types of the tline package (ADR D7.6): plies, solder mask, line geometry, line result.

Stdlib only: the venv and KiCad's bundled Python 3.11 both import it. Units: lengths mm,
frequencies Hz, impedances ohm. Every type is frozen; sequences are normalised to tuples so
geometries hash (the FD cache keys on them).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import NamedTuple

C0 = 299_792_458.0            # speed of light, m/s
ETA0 = 376.730313668          # free-space impedance, ohm (CODATA 2018)
EPS0 = 8.8541878128e-12       # vacuum permittivity, F/m

KINDS = ("microstrip", "embedded_microstrip", "stripline", "dual_stripline", "cpwg", "cpw")
PAIRS = ("single", "edge", "broadside")
CLASSES = ("A", "B", "C")
# ADR D7.6 accuracy policy: closed-form target vs FD per class (C has none: FD decides)
CLASS_BOUND_PCT = {"A": 1.0, "B": 3.0}


def _require(ok: bool, message: str) -> None:
    if not ok:
        raise ValueError(message)


def _finite_at_least(value: float, low: float) -> bool:
    return isinstance(value, (int, float)) and math.isfinite(value) and value >= low


@dataclass(frozen=True)
class Ply:
    """One dielectric ply: thickness (mm), relative permittivity, loss tangent, and the
    frequency (Hz) the er/df values were resolved at."""

    thickness: float
    er: float
    df: float = 0.0
    f_spec_hz: float = 1e9

    def __post_init__(self) -> None:
        _require(_finite_at_least(self.thickness, 0.0) and self.thickness > 0.0,
                 f"ply thickness must be > 0 mm, got {self.thickness!r}")
        _require(_finite_at_least(self.er, 1.0), f"ply er must be >= 1, got {self.er!r}")
        _require(_finite_at_least(self.df, 0.0), f"ply df must be >= 0, got {self.df!r}")
        _require(_finite_at_least(self.f_spec_hz, 0.0) and self.f_spec_hz > 0.0,
                 f"ply f_spec_hz must be > 0, got {self.f_spec_hz!r}")


@dataclass(frozen=True)
class MaskModel:
    """Solder-mask coating: er and the thicknesses (mm) over the trace, over bare substrate and
    between close traces (JLC C2 / C1 / C3)."""

    er: float
    over_trace: float
    over_substrate: float
    between: float

    def __post_init__(self) -> None:
        _require(_finite_at_least(self.er, 1.0), f"mask er must be >= 1, got {self.er!r}")
        for name in ("over_trace", "over_substrate", "between"):
            value = getattr(self, name)
            _require(_finite_at_least(value, 0.0), f"mask {name} must be >= 0 mm, got {value!r}")


def _optional_length(name: str, value: float | None) -> None:
    _require(value is None or _finite_at_least(value, 0.0),
             f"{name} must be None or >= 0 mm, got {value!r}")


def _check_plies(name: str, plies: tuple) -> None:
    for ply in plies:
        if not isinstance(ply, Ply):
            raise TypeError(f"LineGeometry.{name} holds {type(ply).__name__}, expected Ply")


@dataclass(frozen=True)
class LineGeometry:
    """A line cross-section (ADR D7.6). `below`/`above` hold the plies between the trace and
    its reference (or the covering plies) ordered outward from the trace; `dielectric_copper`
    lists signal layers treated as dielectric (dual stripline) as (layer, er). A width of 0 is
    an unsolved placeholder; the models require width > 0."""

    kind: str
    pair: str
    width: float
    t: float
    below: tuple[Ply, ...]
    above: tuple[Ply, ...]
    pair_gap: float | None = None
    ground_gap: float | None = None
    ground_width: float | None = None
    mask: MaskModel | None = None
    etch: float = 0.0
    layers: tuple[str, ...] = ()
    dielectric_copper: tuple[tuple[str, float], ...] = ()

    def __post_init__(self) -> None:
        for name in ("below", "above", "layers"):
            object.__setattr__(self, name, tuple(getattr(self, name)))
        object.__setattr__(self, "dielectric_copper",
                           tuple(tuple(item) for item in self.dielectric_copper))
        _require(isinstance(self.kind, str) and bool(self.kind),
                 f"kind must be a non-empty string, got {self.kind!r}")
        _require(self.pair in PAIRS, f"pair must be one of {PAIRS}, got {self.pair!r}")
        _require(_finite_at_least(self.width, 0.0), f"width must be >= 0 mm, got {self.width!r}")
        _require(_finite_at_least(self.t, 0.0), f"t must be >= 0 mm, got {self.t!r}")
        _require(_finite_at_least(self.etch, 0.0), f"etch must be >= 0 mm, got {self.etch!r}")
        for name in ("pair_gap", "ground_gap", "ground_width"):
            _optional_length(name, getattr(self, name))
        _check_plies("below", self.below)
        _check_plies("above", self.above)
        if self.mask is not None and not isinstance(self.mask, MaskModel):
            raise TypeError(f"LineGeometry.mask must be MaskModel or None, "
                            f"got {type(self.mask).__name__}")


@dataclass(frozen=True)
class LineResult:
    """One evaluated line (ADR D7.6). z0 is the impedance at the evaluation frequency (static
    unless a Z0(f) model applies); eeff is static, eeff_f at the frequency; the pair fields are
    None for single lines. est_error_pct is the model's documented error vs FD."""

    z0: float
    eeff: float
    eeff_f: float
    z_even: float | None
    z_odd: float | None
    z_diff: float | None
    z_comm: float | None
    delay_ps_per_mm: float
    model: str
    accuracy_class: str
    est_error_pct: float
    warnings: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "warnings", tuple(self.warnings))
        _require(self.accuracy_class in CLASSES,
                 f"accuracy_class must be one of {CLASSES}, got {self.accuracy_class!r}")


class Issue(NamedTuple):
    """A geometry / resolution finding (ST-* id, 'error' | 'warning' | 'info', text, JSON path)."""

    id: str
    severity: str
    message: str
    path: str


class Verdict(NamedTuple):
    """One model's or one check's say on a result: class, error estimate (%), warnings."""

    accuracy_class: str
    est_error_pct: float
    warnings: tuple[str, ...] = ()


def delay_ps_per_mm(eeff: float) -> float:
    """Phase delay of a TEM / quasi-TEM line: sqrt(eeff) / c, in ps per mm."""
    return math.sqrt(eeff) * 1e9 / C0


def worst_class(*classes: str) -> str:
    """The least accurate of the given classes (A < B < C)."""
    return max(classes, key=CLASSES.index)


def combine(*verdicts: Verdict) -> Verdict:
    """Worst class, largest error estimate, every warning in order."""
    return Verdict(worst_class(*(v.accuracy_class for v in verdicts)),
                   max(v.est_error_pct for v in verdicts),
                   tuple(w for v in verdicts for w in v.warnings))


def single_line_result(z0: float, eeff: float, eeff_f: float, *, model: str,
                       accuracy_class: str, est_error_pct: float,
                       warnings: tuple[str, ...] = ()) -> LineResult:
    """LineResult for a single (unpaired) line; the delay follows from eeff_f."""
    return LineResult(z0=z0, eeff=eeff, eeff_f=eeff_f, z_even=None, z_odd=None, z_diff=None,
                      z_comm=None, delay_ps_per_mm=delay_ps_per_mm(eeff_f), model=model,
                      accuracy_class=accuracy_class, est_error_pct=est_error_pct,
                      warnings=tuple(warnings))
