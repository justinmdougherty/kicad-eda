"""Numerical kernels of the tline package: the complete elliptic integral K and a bisection solver.

K(k) = pi / (2 AGM(1, k')) with k' = sqrt(1 - k^2) the complementary modulus. The conformal
mappings (CPW, CPWG, Cohn's striplines) need K(k)/K(k') for moduli within 1e-12 of 1, where
1 - k^2 cancels; every helper here therefore takes or returns k' computed analytically
(hyperbolic identities, evaluated in log space so large arguments neither overflow nor
underflow early). Stdlib only.
"""
from __future__ import annotations

import math
from collections.abc import Callable

_AGM_ITERATIONS = 64
_LN2 = math.log(2.0)


def agm(a: float, b: float) -> float:
    """Arithmetic-geometric mean of two non-negative numbers."""
    for _ in range(_AGM_ITERATIONS):
        if abs(a - b) <= 1e-16 * a:
            break
        a, b = 0.5 * (a + b), math.sqrt(a * b)
    return a


def ellipk_from_kp(kp: float) -> float:
    """K(k) from the complementary modulus k' (exact near k = 1); K = inf at k' = 0."""
    if not 0.0 <= kp <= 1.0:
        raise ValueError(f"complementary modulus out of [0, 1]: {kp!r}")
    if kp == 0.0:
        return math.inf
    return math.pi / (2.0 * agm(1.0, kp))


def complement(k: float) -> float:
    """k' = sqrt(1 - k^2), factored to limit cancellation."""
    return math.sqrt((1.0 - k) * (1.0 + k))


def ellipk(k: float) -> float:
    """Complete elliptic integral of the first kind, modulus k in [0, 1)."""
    if not 0.0 <= k < 1.0:
        raise ValueError(f"modulus out of [0, 1): {k!r}")
    return ellipk_from_kp(complement(k))


def k_ratio(k: float, kp: float | None = None) -> float:
    """K(k)/K(k'). Pass kp (computed analytically) whenever k may be close to 1."""
    if kp is None:
        if not 0.0 <= k < 1.0:
            raise ValueError(f"modulus out of [0, 1): {k!r}; pass kp near k = 1")
        kp = complement(k)
    if not 0.0 <= k <= 1.0:
        raise ValueError(f"modulus out of [0, 1]: {k!r}")
    return ellipk_from_kp(kp) / ellipk_from_kp(k)


# ---------------------------------------------------------------- log-space hyperbolics
def _ln_sinh(x: float) -> float:
    """ln sinh x for x > 0 without overflow."""
    if x > 20.0:
        return x - _LN2 + math.log1p(-math.exp(-2.0 * x))
    return math.log(math.sinh(x))


def _ln_cosh(x: float) -> float:
    """ln cosh x for x >= 0 without overflow."""
    return x - _LN2 + math.log1p(math.exp(-2.0 * x))


def _ln_tanh(x: float) -> float:
    """ln tanh x for x > 0, as ln((1 - e^-2x)/(1 + e^-2x)) once x is not small: never the
    difference of two large logarithms (that rounds tanh a / tanh b above 1)."""
    if x < 0.5:
        return math.log(math.tanh(x))
    e = math.exp(-2.0 * x)
    return math.log1p(-e) - math.log1p(e)


def _unit(value: float) -> float:
    """Clamp a modulus to [0, 1] against the last-ulp rounding of exp(log ...)."""
    return min(max(value, 0.0), 1.0)


def _check_args(xa: float, xb: float) -> None:
    if not 0.0 < xa < xb or not math.isfinite(xb):
        raise ValueError(f"need 0 < xa < xb, got xa={xa!r}, xb={xb!r}")


def tanh_ratio_modulus(xa: float, xb: float) -> tuple[float, float]:
    """(k, k') for k = tanh(xa)/tanh(xb), 0 < xa < xb, using k'^2 = (tanh^2 xb - tanh^2 xa)
    / tanh^2 xb and tanh xb - tanh xa = sinh(xb - xa)/(cosh xa cosh xb)."""
    _check_args(xa, xb)
    ln_k = _ln_tanh(xa) - _ln_tanh(xb)
    ln_diff = _ln_sinh(xb - xa) - _ln_cosh(xa) - _ln_cosh(xb)
    ln_sum = math.log(math.tanh(xb) + math.tanh(xa))
    ln_kp = 0.5 * (ln_diff + ln_sum) - _ln_tanh(xb)
    return _unit(math.exp(ln_k)), _unit(math.exp(ln_kp))


def sinh_ratio_modulus(xa: float, xb: float) -> tuple[float, float]:
    """(k, k') for k = sinh(xa)/sinh(xb), 0 < xa < xb, using
    sinh^2 xb - sinh^2 xa = sinh(xb - xa) sinh(xb + xa)."""
    _check_args(xa, xb)
    ln_k = _ln_sinh(xa) - _ln_sinh(xb)
    ln_kp = 0.5 * (_ln_sinh(xb - xa) + _ln_sinh(xb + xa)) - _ln_sinh(xb)
    return _unit(math.exp(ln_k)), _unit(math.exp(ln_kp))


def coupled_moduli(xw: float, xs: float) -> tuple[tuple[float, float], tuple[float, float]]:
    """Cohn's edge-coupled stripline moduli with xw = pi w/(2b), xs = pi (w + s)/(2b):
    even k_e = tanh xw tanh xs, odd k_o = tanh xw / tanh xs, each with its complement from
    1 - k_e = cosh(xs - xw)/(cosh xw cosh xs) and 1 - k_o = sinh(xs - xw)/(cosh xw sinh xs).
    Returns ((k_e, k_e'), (k_o, k_o'))."""
    _check_args(xw, xs)
    ke = _unit(math.exp(_ln_tanh(xw) + _ln_tanh(xs)))
    one_minus_ke = math.exp(_ln_cosh(xs - xw) - _ln_cosh(xw) - _ln_cosh(xs))
    ko, kop = tanh_ratio_modulus(xw, xs)
    return (ke, _unit(math.sqrt(one_minus_ke * (1.0 + ke)))), (ko, kop)


# ---------------------------------------------------------------- root finding
def solve_monotone(fn: Callable[[float], float], target: float, lo: float, hi: float, *,
                   rel_tol: float = 1e-10, max_iter: int = 200) -> float:
    """x in [lo, hi] with fn(x) = target for a monotone fn (either direction), by bisection.
    Raises ValueError when the target is not bracketed."""
    f_lo, f_hi = fn(lo) - target, fn(hi) - target
    if f_lo == 0.0:
        return lo
    if f_hi == 0.0:
        return hi
    if (f_lo > 0.0) == (f_hi > 0.0):
        raise ValueError(f"target {target!r} not bracketed: f({lo!r}) = {f_lo + target!r}, "
                         f"f({hi!r}) = {f_hi + target!r}")
    for _ in range(max_iter):
        mid = 0.5 * (lo + hi)
        f_mid = fn(mid) - target
        if f_mid == 0.0:
            return mid
        if (f_mid > 0.0) == (f_lo > 0.0):
            lo, f_lo = mid, f_mid
        else:
            hi = mid
        if hi - lo <= rel_tol * max(abs(mid), 1e-12):
            break
    return 0.5 * (lo + hi)
