"""Dielectric frequency dependence (ADR D7.3 / D7.6): Djordjevic-Sarkar and log-f interpolation.

Djordjevic, Biljic, Likar-Smiljanic & Sarkar 2001 (IEEE T-EMC 43(4):662), the causal
"wideband Debye" model, written in frequency (the angular-frequency factors cancel):

    eps(f) = eps_inf + delta_eps * ln((f_hi + j f) / (f_lo + j f)) / ln(f_hi / f_lo)

with eps = Dk - j Dk Df. Fitted to one (Dk, Df, f) point over the default band 1 kHz - 1 THz,
it gives the near-constant Df and the slow Dk roll-off of glass-epoxy laminates. The materials
layer uses it only for plies that ask for it ("dielectric_model": "djordjevic_sarkar");
otherwise values move between published points by interp_log_f (linear in log f, the nearest
point outside the band). Stdlib only.
"""
from __future__ import annotations

import bisect
import cmath
import math
from collections.abc import Sequence
from dataclasses import dataclass

F_LO_HZ = 1e3
F_HI_HZ = 1e12


def _shape(f_hz: float, f_lo_hz: float, f_hi_hz: float) -> complex:
    """F(f) = ln((f_hi + j f)/(f_lo + j f)) / ln(f_hi/f_lo); F(0) = 1, F(inf) = 0."""
    return cmath.log(complex(f_hi_hz, f_hz) / complex(f_lo_hz, f_hz)) / math.log(f_hi_hz / f_lo_hz)


@dataclass(frozen=True)
class DjordjevicSarkar:
    """A fitted Djordjevic-Sarkar dielectric: eps(f) = eps_inf + delta_eps F(f)."""

    eps_inf: float
    delta_eps: float
    f_lo_hz: float = F_LO_HZ
    f_hi_hz: float = F_HI_HZ

    def eps(self, f_hz: float) -> complex:
        """Complex relative permittivity eps' - j eps'' at f_hz (>= 0)."""
        if not (math.isfinite(f_hz) and f_hz >= 0.0):
            raise ValueError(f"frequency must be finite and >= 0 Hz, got {f_hz!r}")
        return self.eps_inf + self.delta_eps * _shape(f_hz, self.f_lo_hz, self.f_hi_hz)

    def dk(self, f_hz: float) -> float:
        """Real relative permittivity at f_hz."""
        return self.eps(f_hz).real

    def df(self, f_hz: float) -> float:
        """Loss tangent eps''/eps' at f_hz."""
        value = self.eps(f_hz)
        return -value.imag / value.real


def fit_djordjevic_sarkar(dk: float, df: float, f_hz: float, *, f_lo_hz: float = F_LO_HZ,
                          f_hi_hz: float = F_HI_HZ) -> DjordjevicSarkar:
    """The Djordjevic-Sarkar model through one published point (dk, df at f_hz)."""
    if not (math.isfinite(dk) and dk >= 1.0):
        raise ValueError(f"dk must be >= 1, got {dk!r}")
    if not (math.isfinite(df) and 0.0 <= df < 1.0):
        raise ValueError(f"df must be in [0, 1), got {df!r}")
    if not (0.0 < f_lo_hz < f_hi_hz and math.isfinite(f_hi_hz)):
        raise ValueError(f"need 0 < f_lo_hz < f_hi_hz, got {f_lo_hz!r}, {f_hi_hz!r}")
    if not (math.isfinite(f_hz) and f_lo_hz < f_hz < f_hi_hz):
        raise ValueError(f"anchor frequency {f_hz!r} Hz outside the model band "
                         f"[{f_lo_hz:g}, {f_hi_hz:g}] Hz")
    shape = _shape(f_hz, f_lo_hz, f_hi_hz)
    delta = dk * df / -shape.imag
    return DjordjevicSarkar(eps_inf=dk - delta * shape.real, delta_eps=delta,
                            f_lo_hz=f_lo_hz, f_hi_hz=f_hi_hz)


def _sorted_points(points: Sequence[tuple[float, float]]) -> list[tuple[float, float]]:
    if not points:
        raise ValueError("no published points to interpolate")
    out = sorted((float(f), float(v)) for f, v in points)
    if out[0][0] <= 0.0:
        raise ValueError(f"point frequencies must be > 0 Hz, got {out[0][0]!r}")
    return out


def in_band(points: Sequence[tuple[float, float]], f_hz: float) -> bool:
    """True when f_hz lies within the published frequency span (inclusive)."""
    pts = _sorted_points(points)
    return pts[0][0] <= f_hz <= pts[-1][0]


def interp_log_f(points: Sequence[tuple[float, float]], f_hz: float) -> float:
    """Value at f_hz, linear in log(frequency) between published (f_hz, value) points; the
    nearest point's value outside their span (the caller flags that, ST-W6)."""
    pts = _sorted_points(points)
    if f_hz <= pts[0][0]:
        return pts[0][1]
    if f_hz >= pts[-1][0]:
        return pts[-1][1]
    i = bisect.bisect_right([f for f, _ in pts], f_hz)
    (f0, v0), (f1, v1) = pts[i - 1], pts[i]
    frac = math.log(f_hz / f0) / math.log(f1 / f0)
    return v0 + frac * (v1 - v0)
