"""Impedance synthesis, the ADR D7.6 accuracy policy and as-built checks (task D7-07).

synthesize() solves width | pair_gap | ground_gap (log scan + bisection) inside `bounds`; out of
reach gives the bound + a warning. Policy (`fd` = fieldsolve2d or a look-alike): A final; B
FD-verified at the rounded value, re-synthesised when the FD Z misses by > 1 % or the width it
implies (closed-form slope) moves by > 1 % (one that cannot run: in the draft, else a warning);
C FD-synthesised from the fallback. Without FD, B keeps the closed form and C the fallback, each
with a G3 decision draft (confidence low). FD entries are cached by cross_section_hash; FD is
static, so a dispersive closed form lends its Z(f)/Z(0). as_built(): A/B by the closed form, C
by its windows. Stdlib only."""
from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from itertools import pairwise
from typing import Any, NamedTuple

from . import evaluate as _registered
from . import fallback
from .geometry import default_ground_gap, pour_clearance, width_window
from .types import LineGeometry, LineResult

GRID_MM, FD_TRIGGER_PCT, DESIGN_ERROR_PCT = 0.005, 1.0, 2.0
MAX_FD_SOLVES, FD_TOL_PCT, SCAN_POINTS, BISECT_STEPS = 8, 0.2, 32, 60
TARGET_ATTR = {"z0": "z0", "zdiff": "z_diff", "zeven": "z_even", "zodd": "z_odd",
               "zcomm": "z_comm"}
SOLVE_FOR, FD_MODES = ("width", "pair_gap", "ground_gap"), ("off", "auto", "force")
NO_SOLVE = "the FD tier ran no solve"
DRAFT_SOURCE = "ADR D7.6 accuracy policy (tline.synth)"
DRAFT_ALTERNATIVES = (
    "install numpy+scipy for the FD tier (pip install -r server/requirements.txt)",
    "raise the FD budget (KICAD_EDA_FD_BUDGET_S, default 300 s)")


@dataclass(frozen=True)
class Bounds:
    """Synthesis bounds (mm): widths from the fab min track, gaps from its min clearance."""
    width: tuple[float, float] = (0.1, 5.0)
    gap: tuple[float, float] = (0.1, 2.0)

    def of(self, solve_for: str) -> tuple[float, float]:
        return self.width if solve_for == "width" else self.gap


def evaluate(geom: LineGeometry, freq_hz: float = 0.0) -> LineResult:
    """The closed form of geom; a class C result corrected by its fallback model (D7-05c)."""
    return fallback.apply(geom, _registered(geom, freq_hz))


@dataclass(frozen=True)
class _Job:
    target: float
    attr: str
    target_kind: str
    solve_for: str
    bounds: Bounds
    grid: float
    freq_hz: float
    tie_gap: bool
    label: str

    def at(self, geom: LineGeometry, x: float) -> LineGeometry:
        g = replace(geom, **{self.solve_for: x})
        return replace(g, pair_gap=x) if self.tie_gap else g

    def z(self, geom: LineGeometry, x: float | None = None, freq_hz: float | None = None
          ) -> float | None:
        try:
            g = geom if x is None else self.at(geom, x)
            value = getattr(evaluate(g, self.freq_hz if freq_hz is None else freq_hz), self.attr)
        except (ValueError, ZeroDivisionError, OverflowError):
            return None
        return value if value is not None and math.isfinite(value) and value > 0 else None

    def ratio(self, g: LineGeometry) -> float:
        """Z(f) / Z(0) of the closed form (carries dispersion over to static FD values)."""
        zf, z0 = (self.z(g), self.z(g, freq_hz=0.0)) if self.freq_hz > 0 else (1.0, 1.0)
        return zf / z0 if zf and z0 else 1.0


_Fd = NamedTuple("_Fd", [("tier", Any), ("mode", str), ("budget", Any), ("grid", str),
                         ("cache", Mapping[str, Mapping[str, Any]])])


# value, z, fd (None: FD did not run), reason (FD did not settle the line -> a draft), entries
# (every cache entry used), new (computed now), solves, seconds, hits, warnings (no decision)
_Outcome = NamedTuple("_Outcome", [("value", float), ("z", float), ("fd", "dict | None"),
                                   ("reason", "str | None"), ("entries", dict), ("new", dict),
                                   ("solves", int), ("seconds", float), ("hits", int),
                                   ("warnings", tuple)])


def _plain(x: float, z: float, reason: str | None = None) -> _Outcome:
    return _Outcome(x, z, None, reason, {}, {}, 0, 0.0, 0, ())


@dataclass(frozen=True)
class Synthesis:
    """A synthesised (or checked) line and how far to trust it; its dicts are read-only JSON."""

    geom: LineGeometry
    target: float
    target_kind: str
    solve_for: str
    value: float
    z_achieved: float
    result: LineResult
    status: str
    fd: dict | None
    pour_clearance: float | None
    warnings: tuple[str, ...]
    decision_draft: dict | None
    cache: dict
    run: dict

    accuracy_class = property(lambda self: self.result.accuracy_class)
    model = property(lambda self: self.result.model)
    error_pct = property(lambda self: 100.0 * (self.z_achieved / self.target - 1.0))
    width_window = property(lambda self: width_window(self.geom.width))

    def record(self) -> dict[str, Any]:
        """The ADR D7.4 per-layer line keys synthesis knows (JSON-ready, byte-stable)."""
        r, fd = self.result, self.fd
        return {"width": self.geom.width, "pair_gap": self.geom.pair_gap,
                "ground_gap": self.geom.ground_gap, "pour_clearance": self.pour_clearance,
                "z_target": self.target, "target_kind": self.target_kind,
                "z_achieved": round(self.z_achieved, 3), "error_pct": round(self.error_pct, 3),
                "eeff_f": round(r.eeff_f, 4), "delay_ps_per_mm": round(r.delay_ps_per_mm, 4),
                "model": r.model, "accuracy_class": r.accuracy_class,
                "est_error_pct": r.est_error_pct, "width_window": list(self.width_window),
                "fd": None if fd is None else {**fd, "cache": {k: dict(v) for k, v in
                                                               fd["cache"].items()}},
                "status": self.status, "warnings": list(self.warnings)}


# ---------------------------------------------------------------- closed form
def _solve(job: _Job, geom: LineGeometry) -> tuple[float, str]:
    """(x, warning) with Z(x) = target inside the bounds, else the bound closest to it."""
    lo, hi = job.bounds.of(job.solve_for)
    xs = [lo * (hi / lo) ** (i / (SCAN_POINTS - 1)) for i in range(SCAN_POINTS)]
    pts = [(x, z) for x in xs if (z := job.z(geom, x)) is not None]
    if not pts:
        raise ValueError(f"{job.label}: no {job.solve_for} in [{lo:g}, {hi:g}] mm gives a line "
                         "the closed forms can evaluate")
    for (a, za), (b, zb) in pairwise(pts):
        if (za - job.target) * (zb - job.target) <= 0:
            for _ in range(BISECT_STEPS):
                m, zm = 0.5 * (a + b), job.z(geom, 0.5 * (a + b))
                a, za, b = (m, zm, b) if zm and (zm - job.target) * (za - job.target) > 0 \
                    else (a, za, m)
            return 0.5 * (a + b), ""
    x, z = min(pts, key=lambda p: abs(p[1] - job.target))
    return x, (f"{job.label}: {job.target:g} ohm ({job.target_kind}) is out of reach for "
               f"{job.solve_for} in [{lo:g}, {hi:g}] mm (fab bound): clamped to the bound {x:g} "
               f"mm, Z {z:.2f} ohm")


def _round(job: _Job, x: float, score: Callable[[float], float | None]) -> tuple[float, float]:
    """The grid neighbour of x (inside the bounds) whose scored Z is closer to the target."""
    lo, hi = job.bounds.of(job.solve_for)
    k, g = math.floor(x / job.grid + 1e-9), job.grid
    cands = [c * g for c in (k, k + 1) if lo - 1e-9 <= c * g <= hi + 1e-9] or [
        (math.ceil(lo / g - 1e-9) if x <= lo else math.floor(hi / g + 1e-9)) * g]
    scored = [(round(c, 6), z) for c in cands if (z := score(round(c, 6))) is not None]
    if not scored:
        raise ValueError(f"{job.label}: no valid {job.solve_for} on the grid near {x:.4f} mm")
    return min(scored, key=lambda cz: abs(cz[1] - job.target))


def _fit_width(job: _Job, geom: LineGeometry, x: float, max_width: float | None
               ) -> tuple[LineGeometry, float, list[str]]:
    """Shrink a coplanar gap toward the fab minimum until the width fits max_width."""
    if max_width is None or job.solve_for != "width" or geom.ground_gap is None:
        return geom, x, []
    while x > max_width and geom.ground_gap > job.bounds.gap[0] + 1e-9:
        gap = max(job.bounds.gap[0], round(geom.ground_gap - job.grid, 6))
        geom, x = replace(geom, ground_gap=gap), _solve(job, replace(geom, ground_gap=gap))[0]
    return geom, x, [f"{job.label}: width {x:.4f} mm exceeds {max_width:g} mm (1.2 x the narrowest"
                     f" chain pad) even at a {geom.ground_gap:g} mm gap"] if x > max_width else []


# ---------------------------------------------------------------- FD tier
def _blocked(fd: _Fd, exc: Exception | None = None) -> str | None:
    """Why the FD tier cannot run (or failed); None when it can."""
    if exc is None and (fd.tier is None or fd.mode == "off"):
        return "the FD tier is off (fd off)"
    if exc is None and fd.tier.available() == "none":
        return "no FD backend: numpy and scipy are not installed"
    if fd.budget is not None and (fd.budget.exhausted() or "Budget" in type(exc).__name__):
        return f"the FD budget of {fd.budget.seconds:g} s is spent"
    return None if exc is None else f"the FD tier failed ({type(exc).__name__}: {exc})"


def _entry(job: _Job, mode: str, key: str, res: Any, value: float | None,
           converged: bool) -> dict[str, Any]:
    return {"mode": mode, "hash": key, "solve_for": job.solve_for if value is not None else None,
            "value": None if value is None else round(value, 6),
            "z": round(getattr(res, job.attr), 4), "eeff": round(res.eeff, 5),
            "solver": res.method, "grid": res.grid, "converged": bool(converged)}


def _settled(job: _Job, entry: dict, z: float, value: float, base: _Outcome, key: str,
             new: dict, solves: int, secs: float, hits: int, what: str) -> _Outcome:
    err = 100.0 * (z / job.target - 1.0)
    reason = None if abs(err) <= FD_TRIGGER_PCT else f"the FD {what} puts Z {err:+.2f} % off"
    entries = {**base.entries, key: entry}
    fd = {**entry, "verified": reason is None, "cache": entries}
    return _Outcome(value, z, fd, reason, entries, new, solves, secs, hits, base.warnings)


def _fd_synth(job: _Job, fd: _Fd, geom: LineGeometry, seed: float, base: _Outcome
              ) -> _Outcome | None:
    """FD synthesis seeded at `seed` (cache first); None when no solve fitted the budget."""
    ratio = job.ratio(job.at(geom, seed))
    key = fd.tier.cross_section_hash(job.at(geom, seed), round(job.target / ratio, 9),
                                     job.target_kind, job.solve_for, fd.grid)
    new, solves, secs, hits = dict(base.new), base.solves, base.seconds, base.hits
    if key in fd.cache:
        entry, hits = dict(fd.cache[key]), hits + 1
    else:
        fit, res, stats = fd.tier.synthesize_fd(
            geom, job.target / ratio, job.target_kind, job.solve_for, seed, tol_pct=FD_TOL_PCT,
            max_solves=MAX_FD_SOLVES, budget=fd.budget, grid=fd.grid)
        if res is None:
            return None
        entry = _entry(job, "synthesize", key, res, getattr(fit, job.solve_for), stats["converged"])
        new, solves, secs = {**new, key: entry}, solves + stats["solves"], secs + stats["seconds"]
    z_fd, z_cf = entry["z"] * ratio, job.z(geom, entry["value"])
    value, z = _round(job, entry["value"], lambda c: (z_fd * zc / z_cf) if z_cf and (
        zc := job.z(geom, c)) else None)
    out = _settled(job, entry, z_fd, value, base, key, new, solves, secs, hits, "synthesis")
    return out._replace(z=z)


def _fd_verify(job: _Job, fd: _Fd, geom: LineGeometry, x: float, fixed: bool) -> _Outcome:
    """FD at the rounded value; re-synthesis when Z or the implied width is > 1 % off."""
    g = job.at(geom, x)
    key = fd.tier.cross_section_hash(g, None, None, None, fd.grid)
    if key in fd.cache:
        entry, new, solves, secs, hits = dict(fd.cache[key]), {}, 0, 0.0, 1
    else:
        res = fd.tier.solve(g, grid=fd.grid, budget=fd.budget)
        entry = _entry(job, "verify", key, res, None, True)
        new, solves, secs, hits = {key: entry}, 1, res.seconds, 0
    z = entry["z"] * job.ratio(g)
    first = _settled(job, entry, z, x, _plain(x, z), key, new, solves, secs, hits, "verification")
    za, zb = job.z(geom, x * 0.98), job.z(geom, x * 1.02)     # implied move of x (slope)
    corr = (math.expm1(math.log(job.target / z) * math.log(1.02 / 0.98) / math.log(zb / za))
            if za and zb and za != zb else 0.0)
    if fixed or (first.reason is None and abs(corr) * 100.0 <= FD_TRIGGER_PCT):
        return first
    lo, hi = job.bounds.of(job.solve_for)
    try:
        out = _fd_synth(job, fd, geom, min(max(x * (1.0 + corr), lo), hi), first)
    except (RuntimeError, ValueError) as exc:
        return _unresynthesized(job, first, corr, _blocked(fd, exc) or str(exc))
    return out or _unresynthesized(job, first, corr, _blocked(fd) or NO_SOLVE)


def _unresynthesized(job: _Job, first: _Outcome, corr: float, why: str) -> _Outcome:
    """`first` without its re-synthesis: a Z miss names why in its draft, else a warning."""
    if first.reason:
        return first._replace(reason=f"{first.reason}; the FD re-synthesis did not run: {why}")
    note = (f"{job.label}: FD Z {first.z:.2f} ohm is within {FD_TRIGGER_PCT:g} % but the slope "
            f"implies a {100.0 * corr:+.2f} % {job.solve_for} move; the FD re-synthesis did not "
            f"run ({why}), so {job.solve_for} {first.value:g} mm is kept")
    return first._replace(warnings=(*first.warnings, note))


def _policy(job: _Job, fd: _Fd, geom: LineGeometry, x0: float, x: float, cls: str,
            fixed: bool) -> _Outcome | None:
    """The D7.6 accuracy table; None when the closed form stands (class A)."""
    if cls == "A" and fd.mode != "force":
        return None
    blocked = _blocked(fd)
    try:
        if blocked is None and cls == "C" and not fixed:
            return _fd_synth(job, fd, geom, x0, _plain(x, 0.0)) or \
                _plain(x, 0.0, _blocked(fd) or NO_SOLVE)
        return _plain(x, 0.0, blocked) if blocked else _fd_verify(job, fd, geom, x, fixed)
    except (RuntimeError, ValueError) as exc:
        return _plain(x, 0.0, _blocked(fd, exc))


# ---------------------------------------------------------------- synthesize
def _check(geom: LineGeometry, target: Any, target_kind: str, solve_for: str, fd_mode: str,
           grid: float) -> None:
    problems = [
        (not (isinstance(target, (int, float)) and math.isfinite(target) and target > 0),
         f"target must be an impedance > 0 ohm, got {target!r}"),
        (target_kind not in TARGET_ATTR, f"target_kind must be one of {tuple(TARGET_ATTR)}"),
        (target_kind != "z0" and geom.pair == "single", f"{target_kind!r} needs a pair line"),
        (solve_for not in SOLVE_FOR or (solve_for == "pair_gap" and geom.pair != "edge"),
         f"solve_for must be one of {SOLVE_FOR} (pair_gap: edge pairs), got {solve_for!r}"),
        (fd_mode not in FD_MODES, f"fd_mode must be one of {FD_MODES}, got {fd_mode!r}"),
        (not (math.isfinite(grid) and grid > 0), f"grid must be > 0 mm, got {grid!r}")]
    for bad, message in problems:
        if bad:
            raise ValueError(message)


def _draft(job: _Job, result: LineResult, status: str, value: float, reason: str) -> dict:
    cls, what = result.accuracy_class, "fallback" if result.accuracy_class == "C" else "closed form"
    return {"stage": "G3", "question": f"{job.label}: accept the {job.target:g} ohm "
                                       f"({job.target_kind}) line without an FD-verified geometry?",
            "decision": f"use {job.solve_for} {value:g} mm, status {status} (class {cls} {what} "
                        f"{result.model}, estimated error {result.est_error_pct:g} %)",
            "rationale": f"{reason}; ADR D7.6: a class {cls} line keeps its {what} when FD cannot "
                         "settle it, and the choice is recorded for review",
            "alternatives": list(DRAFT_ALTERNATIVES), "confidence": "low",
            "source": DRAFT_SOURCE, "reversible": True, "override": None}


def _quality(job: _Job, z: float, clamped: bool, fixed: bool, tol: float | None) -> list[str]:
    err, tol = 100.0 * (z / job.target - 1.0), 5.0 if tol is None else tol
    if abs(err) > tol and not clamped:
        return [f"{job.label}: Z {z:.2f} ohm is {err:+.2f} % off the {job.target:g} ohm target, "
                f"outside the {tol:g} % tolerance"]
    if abs(err) > DESIGN_ERROR_PCT and not (fixed or clamped):
        return [f"{job.label}: design error {err:+.2f} % after rounding to the {job.grid:g} mm "
                f"grid exceeds {DESIGN_ERROR_PCT:g} %"]
    return []


def synthesize(geom: LineGeometry, target: float, *, target_kind: str = "z0",
               solve_for: str = "width", bounds: Bounds | None = None, grid: float = GRID_MM,
               freq_hz: float = 0.0, fd: Any = None, budget: Any = None,
               cache: Mapping[str, Mapping[str, Any]] | None = None, fd_mode: str = "auto",
               fd_grid: str = "medium", tie_gap: bool = False, max_width: float | None = None,
               fixed: bool = False, z_tol_pct: float | None = None,
               label: str = "line") -> Synthesis:
    """Size (or, fixed=True, check) one dimension of `geom` for `target` ohm (module doc)."""
    bounds = bounds or Bounds()
    _check(geom, target, target_kind, solve_for, fd_mode, grid)
    job = _Job(float(target), TARGET_ATTR[target_kind], target_kind, solve_for, bounds, grid,
               float(freq_hz), tie_gap and solve_for == "width" and geom.pair == "edge", label)
    if geom.ground_gap is None and geom.kind in ("cpwg", "cpw"):
        geom = replace(geom, ground_gap=default_ground_gap(bounds.gap[0]))
    notes: list[str] = []                          # clamp / max_width notes
    if fixed:
        x0 = x = float(getattr(geom, solve_for) or 0.0)
        if (z := job.z(geom, x)) is None:
            raise ValueError(f"{label}: the closed forms cannot evaluate {solve_for} {x:g} mm")
    else:
        x0, clamp = _solve(job, geom)
        geom, x0, notes = _fit_width(job, geom, x0, max_width)
        notes = [clamp, *notes] if clamp else notes
        x, z = _round(job, x0, lambda c: job.z(geom, c))
    if job.tie_gap:
        geom, job = job.at(geom, x), replace(job, tie_gap=False)
    cf = evaluate(job.at(geom, x), job.freq_hz)
    out = _policy(job, _Fd(fd, fd_mode, budget, fd_grid, cache or {}), geom, x0, x,
                  cf.accuracy_class, fixed)
    ran = out is not None and out.fd is not None
    value, z = (out.value, out.z) if ran else (x, z)
    out = out or _plain(value, z)
    final = job.at(geom, value)
    result = cf if value == x else evaluate(final, job.freq_hz)
    status = "fd" if ran else {"C": "fallback_unverified"}.get(result.accuracy_class, "closed_form")
    pour, pour_note = pour_clearance(final, gap=bounds.gap, grid=grid)
    warnings = (*notes, *result.warnings, *_quality(job, z, bool(notes), fixed, z_tol_pct),
                *([pour_note] if pour_note else []), *out.warnings)
    return Synthesis(final, job.target, target_kind, solve_for, value, z, result, status, out.fd,
                     pour, tuple(warnings), _draft(job, result, status, value, out.reason)
                     if out.reason else None, dict(out.new),
                     {"solves": out.solves, "seconds": out.seconds, "cache_hits": out.hits})


# ---------------------------------------------------------------- as built
AsBuilt = NamedTuple("AsBuilt", [("ok", bool), ("method", str), ("z", "float | None"),
                                 ("error_pct", "float | None"), ("messages", tuple)])


def as_built(geom: LineGeometry, line: Mapping[str, Any], *, width: float,
             pair_gap: float | None = None, ground_gap: float | None = None,
             freq_hz: float = 0.0, z_tol_pct: float | None = None) -> AsBuilt:
    """Built copper vs a synthesised line record: A/B by the closed form, C by its windows."""
    cls = line.get("accuracy_class")
    if cls in ("A", "B"):
        built = replace(geom, width=width, pair_gap=geom.pair_gap if pair_gap is None else
                        pair_gap, ground_gap=geom.ground_gap if ground_gap is None else ground_gap)
        z = getattr(evaluate(built, freq_hz), TARGET_ATTR[line.get("target_kind", "z0")])
        err = 100.0 * (z / float(line["z_target"]) - 1.0)
        tol = float(z_tol_pct if z_tol_pct is not None else line.get("z_tol_pct") or 5.0)
        msg = () if abs(err) <= tol else (f"as built Z {z:.2f} ohm is {err:+.2f} % off the "
                                          f"{line['z_target']:g} ohm target (tolerance {tol:g} %)",)
        return AsBuilt(not msg, "closed_form", z, err, msg)
    if cls != "C":
        raise ValueError(f"line accuracy_class must be A, B or C, got {cls!r}")
    lo, hi = line["width_window"]
    msgs = [] if lo - 1e-9 <= width <= hi + 1e-9 else [
        f"width {width:g} mm is outside the FD-synthesised window [{lo:g}, {hi:g}] mm"]
    for name, built_gap in (("pair_gap", pair_gap), ("ground_gap", ground_gap)):
        if line.get(name) is not None and built_gap is not None:
            g_lo, g_hi = width_window(line[name])
            if not g_lo - 1e-9 <= built_gap <= g_hi + 1e-9:
                msgs.append(f"{name} {built_gap:g} mm is outside [{g_lo:g}, {g_hi:g}] mm")
    return AsBuilt(not msgs, "window", None, None, tuple(msgs))
