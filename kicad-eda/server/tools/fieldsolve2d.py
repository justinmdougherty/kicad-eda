"""2-D quasi-static finite-volume field solver: the FD tier of ADR D7.6 (task D7-06).

Port of the [R-imp] research solver (the FD behind the ADR D7.14 vectors, its evaluation and
secant synthesis): a tensor-product grid graded from every material and conductor edge, piecewise-
constant Er per cell, conductors as fixed-potential nodes, the stored energy per mode, the
per-line capacitance C = 2 W / lines, Z = 1 / (c sqrt(C Cair)) and eeff = C / Cair. Open
lines average a Dirichlet and a Neumann box (fd_sections). Every section is mirror-symmetric
about x = 0, so only x >= 0 is solved (x = 0 a magnetic wall for even modes, an electric wall
for the odd mode of an edge pair): the same discrete problem with half the unknowns.
Accuracy: -0.1 % against exact Cohn (90.611 ohm at w 0.3, b 0.5, Er 1; ADR bound 0.3 %),
0.16-0.7 % of atlc [R-imp]; this port matches the research solver to 3e-4 % on the D7.14 rows.

Backends, imported lazily so the module imports with neither: scipy SuperLU (one
factorisation per box, mirror and medium, shared by the modes), else a numpy-only Jacobi-PCG
(same answer; 17x slower in [R-imp], 34x on MS50_7628 here: 42 s vs 1.2 s), else
available() == 'none' and solve() raises FdUnavailableError.

Runtime budget (D7.6): FdBudget is a per-run wall-clock meter (KICAD_EDA_FD_BUDGET_S, default
300 s) shared by every solve of a run; solve() refuses an exhausted budget and charges its run
time, synthesize_fd() stops before an evaluation that would exceed it and returns the best
geometry so far. Nothing here writes files; seconds and cells belong to the run report, never
to the byte-stable artifact (D7.4).
"""
from __future__ import annotations

import hashlib
import importlib
import json
import math
import os
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from itertools import pairwise
from types import ModuleType
from typing import Any

import fd_sections
from tline.types import C0, EPS0, LineGeometry

BACKENDS = ("scipy", "numpy")
BUDGET_ENV = "KICAD_EDA_FD_BUDGET_S"
DEFAULT_BUDGET_S = 300.0
FD_VERSION = "fd2d-3"         # part of the cache key: bump when the numerics or results change
H_SCALE_REF_MM = 0.2104       # the research grids were tuned on 7628 (h 0.2104); thicker
H_SCALE_CAP_MM = 0.6          # substrates scale the largest cell up to 0.6 / 0.2104 (fd_eval)
X_TOL_MM = 1e-9
NODE_TOL_MM = 1e-12
PCG_TOL = 1e-10               # relative residual of the numpy-only PCG
PCG_MAX_ITER = 200_000
TARGET_ATTR = {"z0": "z0", "zdiff": "z_diff", "zeven": "z_even", "zodd": "z_odd",
               "zcomm": "z_comm"}
PAIR_TARGETS = ("zdiff", "zeven", "zodd", "zcomm")
SOLVE_FOR = ("width", "pair_gap", "ground_gap")
SLOPE_GUESS = {"width": -0.8, "pair_gap": 0.3, "ground_gap": 0.3}  # dlnZ/dln(x), first step
FALLING_WITH_PAIR_GAP = ("zeven", "zcomm")   # less coupling lowers the even / common mode Z
FLAT_IN_PAIR_GAP = ("z0",)    # a pair's sqrt(z_even z_odd) is nearly flat in the gap
MAX_STEP_LN = math.log(2.0)   # one synthesis step changes the dimension by at most 2x
VALUE_BOUNDS_MM = (1e-3, 50.0)


class FdUnavailableError(RuntimeError):
    """No usable FD backend (numpy, and scipy for the fast path) is installed."""


class FdBudgetExhausted(RuntimeError):
    """The run's FD budget is spent."""


class FdSolveError(RuntimeError):
    """The linear solve failed (PCG did not converge)."""


@dataclass(frozen=True)
class GridSpec:
    """Smallest cell, largest cell (mm) and the grading ratio between neighbours."""

    dmin: float
    dmax: float
    ratio: float


GRIDS: Mapping[str, GridSpec] = {"medium": GridSpec(5e-4, 0.02, 1.08),
                                 "fine": GridSpec(2.5e-4, 0.01, 1.04)}


@dataclass(frozen=True)
class FdResult:
    """One FD evaluation. Single lines: z0 / eeff. Pairs, in the one convention shared with
    tline's LineResult (stripline.pair_line_result): z0 = sqrt(z_even z_odd) (the coupler Z0
    of Rogers note 3.2.1), z_diff = 2 z_odd, z_comm = z_even / 2, eeff = the odd
    (differential) mode's (+ eeff_even / eeff_odd).
    box_spread (ohm): half the Dirichlet-Neumann difference of z0 for a single line and of
    z_diff for a pair (the quantity a pair's est_error_pct refers to); 0 for a closed box."""

    z0: float
    eeff: float
    z_even: float | None
    z_odd: float | None
    z_diff: float | None
    cells: int
    seconds: float
    method: str
    grid: str = "medium"
    z_comm: float | None = None
    eeff_even: float | None = None
    eeff_odd: float | None = None
    box_spread: float = 0.0


class FdBudget:
    """Per-run FD wall-clock budget (seconds). Deliberately the one stateful object here: a
    meter every solve of a run charges; thread-safe."""

    __slots__ = ("_limit", "_spent", "_lock")

    def __init__(self, seconds: float) -> None:
        if not (isinstance(seconds, (int, float)) and math.isfinite(seconds) and seconds >= 0):
            raise ValueError(f"FD budget must be a finite number of seconds >= 0, got {seconds!r}")
        self._limit = float(seconds)
        self._spent = 0.0
        self._lock = threading.Lock()

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> FdBudget:
        """The budget from KICAD_EDA_FD_BUDGET_S (default 300 s); a bad value raises."""
        env = os.environ if environ is None else environ
        raw = env.get(BUDGET_ENV)
        if raw is None or not raw.strip():
            return cls(DEFAULT_BUDGET_S)
        try:
            value = float(raw)
        except ValueError:
            value = math.nan
        if not (math.isfinite(value) and value >= 0):
            raise ValueError(f"{BUDGET_ENV}={raw!r}: expected a number of seconds >= 0")
        return cls(value)

    @property
    def seconds(self) -> float:
        return self._limit

    @property
    def spent(self) -> float:
        with self._lock:
            return self._spent

    def remaining(self) -> float:
        with self._lock:
            return max(0.0, self._limit - self._spent)

    def exhausted(self) -> bool:
        return self.remaining() <= 0.0

    def charge(self, seconds: float) -> float:
        """Record `seconds` of FD work; return what remains."""
        if not (math.isfinite(seconds) and seconds >= 0):
            raise ValueError(f"cannot charge {seconds!r} s to the FD budget")
        with self._lock:
            self._spent += seconds
            return max(0.0, self._limit - self._spent)

    def __repr__(self) -> str:
        return f"FdBudget(seconds={self._limit}, spent={self.spent:.3f})"


# ---------------------------------------------------------------- backends
def _module(name: str) -> ModuleType | None:
    try:
        return importlib.import_module(name)
    except ImportError:
        return None


def available() -> str:
    """'scipy' (numpy + scipy SuperLU), 'numpy' (numpy-only PCG) or 'none'."""
    if _module("numpy") is None:
        return "none"
    if _module("scipy.sparse") is None or _module("scipy.sparse.linalg") is None:
        return "numpy"
    return "scipy"


def _check_backend_name(backend: str | None) -> None:
    if backend is not None and backend not in BACKENDS:
        raise ValueError(f"backend must be one of {BACKENDS} or None, got {backend!r}")


def _resolve_backend(backend: str | None) -> str:
    best = available()
    if best == "none":
        raise FdUnavailableError("no FD backend: numpy (and scipy for speed) are not installed; "
                                 "pip install -r server/requirements.txt")
    if backend == "scipy" and best != "scipy":
        raise FdUnavailableError("FD backend 'scipy' requested but scipy is not installed")
    return backend or best


def _grid_spec(grid: str) -> GridSpec:
    if grid not in GRIDS:
        raise ValueError(f"grid must be one of {tuple(GRIDS)}, got {grid!r}")
    return GRIDS[grid]


# ---------------------------------------------------------------- mesh
def _graded(keys: Sequence[float], dmin: float, dmax: float, ratio: float) -> list[float]:
    """Nodes between sorted key lines: cells grow by `ratio` from each key up to dmax."""
    ks = sorted({round(k, 12) for k in keys})
    pts = [ks[0]]
    for a, b in pairwise(ks):
        half = a + (b - a) / 2.0
        left, d, x = [], dmin, a
        while x + d < half:
            x += d
            left.append(x)
            d = min(d * ratio, dmax)
        right, d, x = [], dmin, b
        while x - d > half:
            x -= d
            right.append(x)
            d = min(d * ratio, dmax)
        lo, hi = (left[-1] if left else a), (right[-1] if right else b)
        n = max(1, math.ceil((hi - lo) / dmax))
        mid = [lo + (hi - lo) * i / n for i in range(1, n)] if hi - lo > 1.5 * dmin else []
        pts += left + mid + right[::-1] + [b]
    return sorted(set(pts))


def _mesh(np: ModuleType, sec: fd_sections.Section, spec: GridSpec) -> tuple[Any, Any]:
    """Half-domain node coordinates: x from 0 (the mirror plane) to x_half, y over the box."""
    scale = 1.0 if sec.h_ref is None else max(1.0, min(sec.h_ref, H_SCALE_CAP_MM) / H_SCALE_REF_MM)
    dmax = spec.dmax * scale
    rects = [r for r, _ in sec.dielectrics] + [r for _, r in sec.conductors]
    xs = {-sec.x_half, sec.x_half} | {v for r in rects for v in (r.x0, r.x1)
                                      if abs(v) <= sec.x_half}
    ys = {sec.y0, sec.y1} | {v for r in rects for v in (r.y0, r.y1) if sec.y0 <= v <= sec.y1}
    x_full = _graded(sorted(xs), spec.dmin, dmax, spec.ratio)
    x = [0.0] + [v for v in x_full if v > X_TOL_MM]
    return np.asarray(x), np.asarray(_graded(sorted(ys), spec.dmin, dmax, spec.ratio))


def _paint(np: ModuleType, x: Any, y: Any, sec: fd_sections.Section) -> Any:
    """Cell permittivities: air, then each dielectric over the cells whose centre it holds."""
    xc, yc = 0.5 * (x[:-1] + x[1:]), 0.5 * (y[:-1] + y[1:])
    eps = np.ones((len(xc), len(yc)))
    for r, er in sec.dielectrics:
        i0, i1 = np.searchsorted(xc, r.x0, "right"), np.searchsorted(xc, r.x1, "left")
        j0, j1 = np.searchsorted(yc, r.y0, "right"), np.searchsorted(yc, r.y1, "left")
        eps[i0:i1, j0:j1] = er
    return eps


def _fixed(np: ModuleType, x: Any, y: Any, sec: fd_sections.Section,
           potentials: Mapping[str, float], mirror: str) -> tuple[Any, Any]:
    """Fixed-node mask and values: conductors, grounded planes, Dirichlet walls, odd mirror."""
    fixed = np.zeros((len(x), len(y)), dtype=bool)
    val = np.zeros((len(x), len(y)))
    for name, r in sec.conductors:
        i0 = np.searchsorted(x, r.x0 - NODE_TOL_MM, "left")
        i1 = np.searchsorted(x, r.x1 + NODE_TOL_MM, "right")
        j0 = np.searchsorted(y, r.y0 - NODE_TOL_MM, "left")
        j1 = np.searchsorted(y, r.y1 + NODE_TOL_MM, "right")
        fixed[i0:i1, j0:j1] = True
        val[i0:i1, j0:j1] = potentials.get(name, 0.0)
    walls = sec.boundary == "dirichlet"
    fixed[:, 0] |= sec.bottom_ground or walls
    fixed[:, -1] |= sec.top_ground or walls
    fixed[-1, :] |= walls
    if mirror == "odd":
        fixed[0, :] = True
        val[0, :] = 0.0
    return fixed, val


def _conductances(np: ModuleType, x: Any, y: Any, eps: Any) -> tuple[Any, Any]:
    """Edge conductances (per eps0): gx along x between (i, j)-(i+1, j), gy along y."""
    dx, dy = np.diff(x), np.diff(y)
    gx = np.zeros((len(x) - 1, len(y)))
    gx[:, :-1] += eps * dy[None, :] / 2.0
    gx[:, 1:] += eps * dy[None, :] / 2.0
    gx /= dx[:, None]
    gy = np.zeros((len(x), len(y) - 1))
    gy[:-1, :] += eps * dx[:, None] / 2.0
    gy[1:, :] += eps * dx[:, None] / 2.0
    gy /= dy[None, :]
    return gx, gy


def _energy(np: ModuleType, gx: Any, gy: Any, phi: Any) -> float:
    """Sum of g * (dphi)^2 over every edge = 2 W / eps0 in the half domain."""
    return float((gx * np.diff(phi, axis=0) ** 2).sum() + (gy * np.diff(phi, axis=1) ** 2).sum())


# ---------------------------------------------------------------- linear solvers
def _solve_scipy(np: ModuleType, gx: Any, gy: Any, fixed: Any, values: Sequence[Any]) -> list:
    """Potentials for each value set (same fixed mask): one SuperLU factorisation."""
    sp = importlib.import_module("scipy.sparse")
    spla = importlib.import_module("scipy.sparse.linalg")
    nx, ny = fixed.shape
    idx = np.arange(nx * ny).reshape(nx, ny)
    a = np.concatenate([idx[:-1, :].ravel(), idx[:, :-1].ravel()])
    b = np.concatenate([idx[1:, :].ravel(), idx[:, 1:].ravel()])
    g = np.concatenate([gx.ravel(), gy.ravel()])
    free = ~fixed.ravel()
    nf = int(free.sum())
    pos = np.full(nx * ny, -1)
    pos[free] = np.arange(nf)
    pa, pb = pos[a], pos[b]
    fa, fb = pa >= 0, pb >= 0
    diag = np.bincount(pa[fa], g[fa], nf) + np.bincount(pb[fb], g[fb], nf)
    both = fa & fb
    rows = np.concatenate([pa[both], pb[both], np.arange(nf)])
    cols = np.concatenate([pb[both], pa[both], np.arange(nf)])
    data = np.concatenate([-g[both], -g[both], diag])
    lu = spla.splu(sp.csc_matrix((data, (rows, cols)), shape=(nf, nf)),
                   permc_spec="MMD_AT_PLUS_A")
    out = []
    for val in values:
        v = val.ravel()
        m1, m2 = fa & ~fb, fb & ~fa
        rhs = np.bincount(pa[m1], g[m1] * v[b[m1]], nf) + np.bincount(pb[m2], g[m2] * v[a[m2]], nf)
        phi = v.copy()
        phi[free] = lu.solve(rhs)
        out.append(phi.reshape(nx, ny))
    return out


def _apply(np: ModuleType, gx: Any, gy: Any, p: Any) -> Any:
    """A p for the conductance Laplacian (matrix-free)."""
    out = np.zeros_like(p)
    f = gx * (p[1:, :] - p[:-1, :])
    out[:-1, :] -= f
    out[1:, :] += f
    f = gy * (p[:, 1:] - p[:, :-1])
    out[:, :-1] -= f
    out[:, 1:] += f
    return -out


def _pcg(np: ModuleType, gx: Any, gy: Any, fixed: Any, val: Any) -> Any:
    """Jacobi-preconditioned conjugate gradients on the free nodes (numpy only)."""
    diag = np.zeros(fixed.shape)
    diag[:-1, :] += gx
    diag[1:, :] += gx
    diag[:, :-1] += gy
    diag[:, 1:] += gy
    phi = np.where(fixed, val, 0.0)
    r = -_apply(np, gx, gy, phi)
    r[fixed] = 0.0
    m_inv = np.where(fixed, 0.0, 1.0 / np.where(diag > 0, diag, 1.0))
    z = m_inv * r
    p, rz = z.copy(), float((r * z).sum())
    r0 = math.sqrt(float((r * r).sum())) or 1.0
    for _ in range(PCG_MAX_ITER):
        ap = _apply(np, gx, gy, p)
        ap[fixed] = 0.0
        alpha = rz / float((p * ap).sum())
        phi += alpha * p
        r -= alpha * ap
        if math.sqrt(float((r * r).sum())) < PCG_TOL * r0:
            return phi
        z = m_inv * r
        rz_new = float((r * z).sum())
        p = z + (rz_new / rz) * p
        rz = rz_new
    raise FdSolveError(f"numpy PCG did not converge in {PCG_MAX_ITER} iterations")


def _potentials(np: ModuleType, backend: str, gx: Any, gy: Any, fixed: Any,
                values: Sequence[Any]) -> list:
    if backend == "scipy":
        return _solve_scipy(np, gx, gy, fixed, values)
    return [_pcg(np, gx, gy, fixed, v) for v in values]


# ---------------------------------------------------------------- evaluation
def _section_energies(np: ModuleType, sec: fd_sections.Section, modes: Sequence,
                      spec: GridSpec, backend: str) -> tuple[dict, int]:
    """{mode name: (energy with dielectrics, energy in air)} for one box, and its node count."""
    x, y = _mesh(np, sec, spec)
    eps = _paint(np, x, y, sec)
    er0 = float(eps.flat[0])
    homogeneous = bool(np.all(eps == er0))
    out: dict = {}
    for mirror in ("even", "odd"):
        group = [m for m in modes if m.mirror == mirror]
        if not group:
            continue
        masks = [_fixed(np, x, y, sec, dict(m.potentials), mirror) for m in group]
        fixed, vals = masks[0][0], [v for _, v in masks]
        media = (eps,) if homogeneous else (eps, np.ones_like(eps))
        energies = []
        for medium in media:
            gx, gy = _conductances(np, x, y, medium)
            phis = _potentials(np, backend, gx, gy, fixed, vals)
            energies.append([_energy(np, gx, gy, phi) for phi in phis])
        air = energies[1] if not homogeneous else [e / er0 for e in energies[0]]
        out.update({m.name: (energies[0][i], air[i]) for i, m in enumerate(group)})
    return out, len(x) * len(y)


def _line(energy: float, energy_air: float, lines: int) -> tuple[float, float]:
    """(Z, eeff) of one line from the half-domain energies."""
    c = 2.0 * EPS0 * energy / lines
    ca = 2.0 * EPS0 * energy_air / lines
    return 1.0 / (C0 * math.sqrt(c * ca)), c / ca


def _evaluate(problem: fd_sections.Problem, spec: GridSpec, backend: str) -> tuple[dict, int]:
    """Per mode: (mean Z, mean eeff, half the box spread of Z) over the problem's boxes."""
    np = importlib.import_module("numpy")
    per_box, cells = [], 0
    for sec in problem.sections:
        energies, nodes = _section_energies(np, sec, problem.modes, spec, backend)
        per_box.append({m.name: _line(*energies[m.name], m.lines) for m in problem.modes})
        cells = max(cells, nodes)
    out = {}
    for m in problem.modes:
        zs = [box[m.name][0] for box in per_box]
        es = [box[m.name][1] for box in per_box]
        out[m.name] = (sum(zs) / len(zs), sum(es) / len(es), (max(zs) - min(zs)) / 2.0)
    return out, cells


def _result(modes: dict, cells: int, seconds: float, backend: str, grid: str) -> FdResult:
    if "se" in modes:
        z, eeff, spread = modes["se"]
        return FdResult(z, eeff, None, None, None, cells, seconds, backend, grid, box_spread=spread)
    (ze, ee, _), (zo, eo, spread) = modes["even"], modes["odd"]
    return FdResult(math.sqrt(ze * zo), eo, ze, zo, 2.0 * zo, cells, seconds, backend, grid,
                    z_comm=ze / 2.0, eeff_even=ee, eeff_odd=eo, box_spread=2.0 * spread)


def solve(geom: LineGeometry, *, grid: str = "medium", budget: FdBudget | None = None,
          options: fd_sections.SectionOptions | None = None,
          backend: str | None = None) -> FdResult:
    """Quasi-static Z / eeff of a line cross-section (see fd_sections for the conventions).
    Raises FdUnavailableError without numpy, FdBudgetExhausted on a spent budget, ValueError
    on a geometry the section builder rejects; charges its run time to `budget`."""
    spec = _grid_spec(grid)
    _check_backend_name(backend)
    if budget is not None and budget.exhausted():
        raise FdBudgetExhausted(f"FD budget of {budget.seconds:g} s is spent")
    name = _resolve_backend(backend)
    problem = fd_sections.build(geom, options)
    t0 = time.perf_counter()
    modes, cells = _evaluate(problem, spec, name)
    seconds = time.perf_counter() - t0
    if budget is not None:
        budget.charge(seconds)
    return _result(modes, cells, seconds, name, grid)


# ---------------------------------------------------------------- synthesis
def _check_synthesis(geom: LineGeometry, target: float, target_kind: str, solve_for: str,
                     start: float, tol_pct: float, max_solves: int) -> None:
    if not (isinstance(target, (int, float)) and math.isfinite(target) and target > 0):
        raise ValueError(f"target must be an impedance > 0 ohm, got {target!r}")
    if target_kind not in TARGET_ATTR:
        raise ValueError(f"target_kind must be one of {tuple(TARGET_ATTR)}, got {target_kind!r}")
    if target_kind in PAIR_TARGETS and geom.pair == "single":
        raise ValueError(f"target_kind {target_kind!r} needs a pair, the line is single")
    if solve_for not in SOLVE_FOR:
        raise ValueError(f"solve_for must be one of {SOLVE_FOR}, got {solve_for!r}")
    if solve_for == "pair_gap" and geom.pair != "edge":
        raise ValueError("solve_for 'pair_gap' needs an edge-coupled pair")
    _check_pair_gap_target(solve_for, target_kind)
    if not (isinstance(start, (int, float)) and math.isfinite(start) and start > 0):
        raise ValueError(f"seed for {solve_for} must be > 0 mm, got {start!r}")
    if not (isinstance(max_solves, int) and max_solves >= 1):
        raise ValueError(f"max_solves must be an int >= 1, got {max_solves!r}")
    if not (math.isfinite(tol_pct) and tol_pct > 0):
        raise ValueError(f"tol_pct must be > 0, got {tol_pct!r}")


def _bounds_ln(geom: LineGeometry, solve_for: str) -> tuple[float, float]:
    lo, hi = VALUE_BOUNDS_MM
    if solve_for == "width":
        lo = max(lo, geom.etch * 1.001)
    return math.log(lo), math.log(hi)


def _check_pair_gap_target(solve_for: str, target_kind: str) -> None:
    if solve_for == "pair_gap" and target_kind in FLAT_IN_PAIR_GAP:
        raise ValueError(f"target_kind {target_kind!r} cannot set solve_for 'pair_gap': a pair's "
                         "z0 = sqrt(z_even z_odd) is nearly flat in the gap and never exceeds "
                         "the uncoupled line's Z0; target zdiff, zodd, zeven or zcomm")


def slope_guess(solve_for: str, target_kind: str) -> float:
    """First-step d ln Z / d ln(dimension), whose sign is also the accepted secant sign: Z falls
    with the width and rises with a ground gap; with the pair gap it rises for zodd / zdiff
    (less coupling) but falls for zeven / zcomm (z0 with the pair gap: ValueError)."""
    _check_pair_gap_target(solve_for, target_kind)
    guess = SLOPE_GUESS[solve_for]
    if solve_for == "pair_gap" and target_kind in FALLING_WITH_PAIR_GAP:
        return -guess
    return guess


def _next_x(points: Sequence[tuple[float, float]], guess: float,
            bounds: tuple[float, float]) -> float:
    """Secant step in (ln value, ln Z - ln target), clamped to 2x per step and kept inside the
    tightest bracket seen so far (bisection in log space when the secant leaves it). A secant
    whose sign disagrees with the physical slope `guess` (grid noise) is not used."""
    x1, f1 = points[-1]
    slope = guess
    if len(points) >= 2:
        x0, f0 = points[-2]
        if x1 != x0 and (f1 - f0) / (x1 - x0) * guess > 0:
            slope = (f1 - f0) / (x1 - x0)
    nxt = x1 + max(-MAX_STEP_LN, min(MAX_STEP_LN, -f1 / slope))
    neg = [p for p in points if p[1] < 0]
    pos = [p for p in points if p[1] > 0]
    if neg and pos:
        a, b = max(neg, key=lambda p: p[1])[0], min(pos, key=lambda p: p[1])[0]
        if not min(a, b) < nxt < max(a, b):
            nxt = 0.5 * (a + b)
    return max(bounds[0], min(bounds[1], nxt))


def _would_exceed(budget: FdBudget | None, last_seconds: float) -> bool:
    if budget is None:
        return False
    remaining = budget.remaining()
    return remaining <= 0.0 or last_seconds > remaining


def synthesize_fd(geom: LineGeometry, target: float, target_kind: str, solve_for: str,
                  seed: float | None = None, *, tol_pct: float = 0.2, max_solves: int = 8,
                  budget: FdBudget | None = None, grid: str = "medium",
                  options: fd_sections.SectionOptions | None = None,
                  backend: str | None = None) -> tuple[LineGeometry, FdResult | None, dict]:
    """FD synthesis of one dimension (`width`, `pair_gap` or `ground_gap`; a ground gap puts
    same-layer ground beside any kind) for a target impedance (`target_kind` z0 | zdiff |
    zeven | zodd | zcomm; z0 of a pair = sqrt(z_even z_odd) as in tline, refused with
    `pair_gap` where it is nearly flat), seeded by the caller's closed form or fallback
    (`seed`, default the geometry's current value). Secant in log space; stops when
    |Z / target - 1| <= tol_pct %, after max_solves evaluations, or before one the budget
    cannot cover. The geometry at the seed is validated first (ValueError) even when the
    budget allows no solve. Returns (best geometry, its FdResult or None when nothing was
    solved, {solves, exhausted, converged, error_pct, seconds, history})."""
    start = getattr(geom, solve_for, None) if seed is None else seed
    _check_synthesis(geom, target, target_kind, solve_for, start, tol_pct, max_solves)
    _grid_spec(grid)
    _check_backend_name(backend)
    attr, bounds = TARGET_ATTR[target_kind], _bounds_ln(geom, solve_for)
    x = max(bounds[0], min(bounds[1], math.log(start)))
    first = start if x == math.log(start) else math.exp(x)    # the seed itself, unless clamped
    fd_sections.build(replace(geom, **{solve_for: first}), options)
    points: list[tuple[float, float]] = []
    tried: list[tuple[LineGeometry, FdResult]] = []
    exhausted, converged, seconds = False, False, 0.0
    if not _would_exceed(budget, 0.0):
        _resolve_backend(backend)
    while len(tried) < max_solves:
        if _would_exceed(budget, tried[-1][1].seconds if tried else 0.0):
            exhausted = True
            break
        trial = replace(geom, **{solve_for: first if not tried else math.exp(x)})
        res = solve(trial, grid=grid, budget=budget, options=options, backend=backend)
        tried.append((trial, res))
        seconds += res.seconds
        points.append((x, math.log(getattr(res, attr)) - math.log(target)))
        if abs(math.expm1(points[-1][1])) * 100.0 <= tol_pct:
            converged = True
            break
        x = _next_x(points, slope_guess(solve_for, target_kind), bounds)
        if any(abs(x - p[0]) < 1e-12 for p in points):
            break                                   # pinned at a bound: no progress possible
    return _synthesis_outcome(geom, solve_for, start, attr, target, tried,
                              {"exhausted": exhausted, "converged": converged, "seconds": seconds})


def _synthesis_outcome(geom: LineGeometry, solve_for: str, start: float, attr: str,
                       target: float, tried: Sequence[tuple[LineGeometry, FdResult]],
                       flags: Mapping[str, Any]) -> tuple[LineGeometry, FdResult | None, dict]:
    history = tuple((getattr(g, solve_for), getattr(r, attr)) for g, r in tried)
    stats = {"solves": len(tried), **flags, "history": history, "error_pct": None}
    if not tried:
        return replace(geom, **{solve_for: start}), None, stats
    best_geom, best = min(tried, key=lambda gr: abs(math.log(getattr(gr[1], attr) / target)))
    return best_geom, best, {**stats, "error_pct": 100.0 * (getattr(best, attr) / target - 1.0)}


# ---------------------------------------------------------------- cache key
def _num(value: float | None) -> float | None:
    return None if value is None else round(float(value), 9)


def _geometry_key(geom: LineGeometry, solve_for: str | None) -> dict:
    """The physics of a cross-section (layer names, df and spec frequencies left out); the
    solved dimension is normalised out so the key names the problem, not the start point
    (solving for the ground gap implies the grounds, whatever the start geometry says)."""
    mask = geom.mask
    grounds = geom.ground_gap is not None or solve_for == "ground_gap"
    key = {"kind": geom.kind, "pair": geom.pair, "width": _num(geom.width), "t": _num(geom.t),
           "below": [[_num(p.thickness), _num(p.er)] for p in geom.below],
           "above": [[_num(p.thickness), _num(p.er)] for p in geom.above],
           "pair_gap": _num(geom.pair_gap), "ground_gap": _num(geom.ground_gap),
           "ground_width": _num(fd_sections.default_ground_width(geom)) if grounds else None,
           "mask": None if mask is None else [_num(mask.er), _num(mask.over_trace),
                                              _num(mask.over_substrate), _num(mask.between)],
           "etch": _num(geom.etch)}
    if solve_for in SOLVE_FOR:
        key[solve_for] = None
    return key


def _options_key(options: fd_sections.SectionOptions | None) -> dict | None:
    if options is None or options == fd_sections.SectionOptions():
        return None
    return {"trace_fill_er": _num(options.trace_fill_er),
            "voids": [[v.side, v.ply, _num(v.corridor)] for v in options.voids]}


def cross_section_hash(geom: LineGeometry, target: float | None, target_kind: str | None,
                       solve_for: str | None, grid: str = "medium", *,
                       options: fd_sections.SectionOptions | None = None) -> str:
    """sha256 FD cache key (D7.4): geometry + target + target_kind + solve_for + grid (+ the
    section options and the solver version). Pass None for a verification-only key."""
    payload = {"v": FD_VERSION, "geom": _geometry_key(geom, solve_for), "target": _num(target),
               "target_kind": target_kind, "solve_for": solve_for, "grid": grid,
               "options": _options_key(options)}
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
