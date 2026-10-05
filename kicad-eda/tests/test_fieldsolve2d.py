"""FD tier of ADR D7.6 (task D7-06): fd_sections cross-sections and the fieldsolve2d solver.

Reference values (mm, ohm; ADR D7.14 and the [R-imp] research FD):
- exact Cohn stripline (t = 0, w 0.3, b 0.5, Er 1) 90.611; FD within 0.3 % (ERRATUM for X-06:
  ADR D7.14's 97.963 is the inverted K ratio, see tests/test_tline_stripline.py);
- exact shielded coplanar stripline (t = 0, Er 1, same-layer grounds at gap s between the two
  planes): Z = eta0 / 4 K(k') / K(k), k = tanh(pi a / 2h) / tanh(pi (a + s) / 2h), a = w / 2,
  h = b / 2 (the conductor-backed CPW map on both half-spaces; s -> infinity gives Cohn);
- Cohn 1960 broadside (t = 0) on four geometries: Zdiff within 0.3 % (Zeven where Cohn's
  formula is valid, w/b >= 0.35 (1 - s/b));
- D7.14 rows at the research W_FD: MS50_7628, SL50_off, CPWG50_7628_mask (+ DMS90_7628,
  DSL100_off, BSL100); the masked USB pair on 7628 (W 0.259, S 0.15) Zdiff 86.80;
- D7.14 e2e masked widths (RO4350B 0.254, Dk 3.66, JLC mask) 0.4942 / 0.3975, medium grid.
Numeric tests skip when no backend is installed (available() == 'none'). Slow tests (numpy-only
PCG, the fine grid, the runtime bound) are opt-in:
    pytest tests/test_fieldsolve2d.py -m "slow or not slow"
"""
from __future__ import annotations

import math
import os
import re
import subprocess
import sys
from dataclasses import replace
from importlib import metadata
from pathlib import Path

import pytest

import fd_sections as fs
import fieldsolve2d as fd
from tline.types import ETA0, LineGeometry, MaskModel, Ply

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "server" / "tools"
T1OZ = 0.035
THALF = 0.0152
JLC_MASK = MaskModel(er=3.8, over_trace=0.01524, over_substrate=0.03048, between=0.03048)
SUBPROCESS_TIMEOUT_S = 120
PY_SUPPORTED = ((3, 10), (3, 11), (3, 12), (3, 13), (3, 14))   # docs/SETUP.md: 3.10 or newer
MARKER = re.compile(r'python_version\s*(<=|>=|==|!=|<|>)\s*"(\d+)\.(\d+)"')

needs_fd = pytest.mark.skipif(fd.available() == "none", reason="no FD backend (numpy/scipy)")


def _opt_in_slow(request: pytest.FixtureRequest) -> None:
    if "slow" not in (request.config.option.markexpr or ""):
        pytest.skip("slow: run with -m 'slow or not slow'")


# ---------------------------------------------------------------- geometry helpers
def ms(width: float = 0.3721, h: float = 0.2104, er: float = 4.4, **kw: object) -> LineGeometry:
    base = {"kind": "microstrip", "pair": "single", "width": width, "t": T1OZ,
            "below": (Ply(h, er),), "above": ()}
    return LineGeometry(**{**base, **kw})


def sl_off(width: float = 0.1347, **kw: object) -> LineGeometry:
    """D7.14 SL50_off / DSL100_off: h1 0.1088 @ 4.16, h2 0.55 @ 4.6, 0.5 oz."""
    base = {"kind": "stripline", "pair": "single", "width": width, "t": THALF,
            "below": (Ply(0.1088, 4.16),), "above": (Ply(0.55, 4.6),)}
    return LineGeometry(**{**base, **kw})


def broadside(width: float, core: float, pp: float, er_core: float, er_pp: float,
              t: float) -> LineGeometry:
    """Primary on top; the partner sits pair_gap (= pp) below it inside `below`."""
    return LineGeometry(kind="stripline", pair="broadside", width=width, t=t,
                        below=(Ply(pp, er_pp), Ply(core, er_core)), above=(Ply(core, er_core),),
                        pair_gap=pp)


def cpwg(width: float, h: float, er: float, gap: float | None, **kw: object) -> LineGeometry:
    base = {"kind": "cpwg", "pair": "single", "width": width, "t": T1OZ,
            "below": (Ply(h, er),), "above": (), "ground_gap": gap}
    return LineGeometry(**{**base, **kw})


def air_stripline(gap: float | None = None) -> LineGeometry:
    """Centred t = 0 stripline, w 0.3, b 0.5, Er 1 (+ same-layer grounds at `gap`)."""
    return LineGeometry(kind="stripline", pair="single", width=0.3, t=0.0,
                        below=(Ply(0.25, 1.0),), above=(Ply(0.25, 1.0),), ground_gap=gap)


# ---------------------------------------------------------------- independent closed forms
def _k_of_kp(kp: float) -> float:
    """K(k) from the complementary modulus k' by the AGM."""
    a, b = 1.0, kp
    for _ in range(64):                       # converges in < 10; a 1-ulp cycle must not hang
        if abs(a - b) <= 4e-16 * a:
            break
        a, b = 0.5 * (a + b), math.sqrt(a * b)
    return math.pi / (2.0 * a)


def cohn_stripline(w: float, b: float, er: float) -> float:
    """Exact zero-thickness centred stripline (Cohn 1954)."""
    x = math.pi * w / (2.0 * b)
    return ETA0 / (4.0 * math.sqrt(er)) * _k_of_kp(math.tanh(x)) / _k_of_kp(1.0 / math.cosh(x))


def shielded_coplanar_stripline(w: float, s: float, b: float, er: float) -> float:
    """Exact t = 0 centred stripline with infinite same-layer grounds at gap s."""
    a, h = w / 2.0, b / 2.0
    k = math.tanh(math.pi * a / (2.0 * h)) / math.tanh(math.pi * (a + s) / (2.0 * h))
    return ETA0 / (4.0 * math.sqrt(er)) * _k_of_kp(k) / _k_of_kp(math.sqrt((1 - k) * (1 + k)))


def cohn_broadside(w: float, s: float, b: float, er: float) -> tuple[float, float]:
    """Cohn 1960 broadside-coupled strips, t = 0: (Zeven, Zodd); s = strip separation."""
    sb, wb = s / b, w / b

    def width_of(k: float) -> float:
        r = math.sqrt(k * (k - sb) / (1.0 - sb * k))
        return (2.0 * math.atanh(r) - 2.0 * sb * math.atanh(r / k)) / math.pi

    lo, hi = sb * (1.0 + 1e-9), 1.0 - 1e-15
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        lo, hi = (mid, hi) if width_of(mid) < wb else (lo, mid)
    k = 0.5 * (lo + hi)
    kp = math.sqrt((1.0 - k) * (1.0 + k))
    z_even = ETA0 / (2.0 * math.sqrt(er)) * _k_of_kp(k) / _k_of_kp(kp)
    z_odd = ETA0 * math.pi / (4.0 * math.sqrt(er)) * sb / math.atanh(k)
    return z_even, z_odd


def test_reference_cohn_value_and_the_d7_14_erratum() -> None:
    """Exact Cohn at w 0.3, b 0.5, Er 1 is 90.611 ohm; D7.14's 97.963 inverts the K ratio."""
    assert cohn_stripline(0.3, 0.5, 1.0) == pytest.approx(90.6108, rel=1e-4)
    x = math.pi * 0.3 / (2 * 0.5)
    inverted = 30.0 * math.pi * _k_of_kp(1.0 / math.cosh(x)) / _k_of_kp(math.tanh(x))
    assert inverted == pytest.approx(97.963, abs=0.001)


def test_reference_shielded_coplanar_stripline_tends_to_cohn() -> None:
    assert shielded_coplanar_stripline(0.3, 5.0, 0.5, 1.0) == pytest.approx(
        cohn_stripline(0.3, 0.5, 1.0), rel=1e-9)
    assert shielded_coplanar_stripline(0.3, 0.1, 0.5, 1.0) == pytest.approx(82.2677, rel=1e-5)


# ---------------------------------------------------------------- availability
def test_module_imports_without_numpy_and_reports_none() -> None:
    code = ("import sys; sys.modules['numpy'] = None; sys.modules['scipy'] = None; "
            f"sys.path[:0] = [{str(TOOLS)!r}]; import fieldsolve2d, fd_sections; "
            "print(fieldsolve2d.available())")
    out = subprocess.run([sys.executable, "-c", code], stdin=subprocess.DEVNULL,
                         capture_output=True, encoding="utf-8", errors="replace",
                         timeout=SUBPROCESS_TIMEOUT_S, check=False)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "none"


def test_available_none_in_process_and_solve_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("numpy", "scipy", "scipy.sparse", "scipy.sparse.linalg"):
        monkeypatch.setitem(sys.modules, name, None)
    assert fd.available() == "none"
    with pytest.raises(fd.FdUnavailableError, match="numpy"):
        fd.solve(ms())
    with pytest.raises(fd.FdUnavailableError):
        fd.synthesize_fd(ms(), 50.0, "z0", "width", 0.37, budget=fd.FdBudget(10.0))


def test_available_numpy_only_when_scipy_hidden(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("numpy")
    for name in ("scipy", "scipy.sparse", "scipy.sparse.linalg"):
        monkeypatch.setitem(sys.modules, name, None)
    assert fd.available() == "numpy"
    with pytest.raises(fd.FdUnavailableError, match="scipy"):
        fd.solve(ms(), backend="scipy")


def test_unknown_backend_and_grid_are_rejected() -> None:
    with pytest.raises(ValueError, match="backend"):
        fd.solve(ms(), backend="cuda")
    with pytest.raises(ValueError, match="grid"):
        fd.solve(ms(), grid="ultra")


def _pins(name: str) -> list[tuple[str, str]]:
    """(version, marker) of every `name==version[; marker]` line of server/requirements.txt."""
    text = (ROOT / "server" / "requirements.txt").read_text(encoding="utf-8")
    out = []
    for raw in text.splitlines():
        spec, _, marker = raw.split("#")[0].partition(";")
        package, sep, version = spec.partition("==")
        if sep and package.strip().lower() == name:
            out.append((version.strip(), marker.strip()))
    return out


def _applies(marker: str, py: tuple[int, int]) -> bool:
    """Evaluate the one marker form the pins use: python_version <op> "X.Y"."""
    if not marker:
        return True
    found = MARKER.fullmatch(marker)
    assert found, f"unsupported requirement marker {marker!r}"
    op, version = found.group(1), (int(found.group(2)), int(found.group(3)))
    return {"<": py < version, "<=": py <= version, ">": py > version, ">=": py >= version,
            "==": py == version, "!=": py != version}[op]


def test_requirements_pin_numpy_and_scipy_for_every_supported_python() -> None:
    """Exactly one exact pin of each applies on every supported Python (3.10+): a pin a
    Python has no wheel for fails the whole `pip install -r` (the launcher's ensureDeps)."""
    for name in ("numpy", "scipy"):
        pins = _pins(name)
        assert pins, f"{name} is not pinned"
        assert all(part.isdigit() for version, _ in pins for part in version.split("."))
        for py in PY_SUPPORTED:
            assert sum(_applies(marker, py) for _, marker in pins) == 1, (name, py, pins)
    assert _applies('python_version >= "3.12"', (3, 12)) and not _applies(
        'python_version >= "3.12"', (3, 11))


def test_installed_pins_carry_their_python_floor() -> None:
    """An installed numpy / scipy that matches a pin: that pin's marker must not reach a
    supported Python below the wheel's Requires-Python (numpy 2.5.3 / scipy 1.18.1: >= 3.12)."""
    checked = 0
    for name in ("numpy", "scipy"):
        try:
            dist = metadata.distribution(name)
        except metadata.PackageNotFoundError:
            continue
        floor = re.fullmatch(r">=\s*(\d+)\.(\d+)", dist.metadata["Requires-Python"] or "")
        for version, marker in _pins(name):
            if version == dist.version and floor:
                low = (int(floor.group(1)), int(floor.group(2)))
                reach = [py for py in PY_SUPPORTED if _applies(marker, py)]
                assert all(py >= low for py in reach), (name, version, marker, low)
                checked += 1
    if not checked:
        pytest.skip("no installed numpy / scipy matches a pin")


# ---------------------------------------------------------------- budget
def test_budget_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(fd.BUDGET_ENV, raising=False)
    assert fd.FdBudget.from_env().seconds == fd.DEFAULT_BUDGET_S == 300.0
    monkeypatch.setenv(fd.BUDGET_ENV, "12.5")
    assert fd.FdBudget.from_env().seconds == 12.5
    assert fd.FdBudget.from_env({fd.BUDGET_ENV: "0"}).seconds == 0.0
    for bad in ("abc", "-1", "nan", "inf"):
        with pytest.raises(ValueError, match=fd.BUDGET_ENV):
            fd.FdBudget.from_env({fd.BUDGET_ENV: bad})


def test_budget_charges_and_never_goes_negative() -> None:
    budget = fd.FdBudget(10.0)
    assert budget.remaining() == 10.0 and not budget.exhausted()
    assert budget.charge(4.0) == 6.0
    assert budget.charge(7.5) == 0.0 and budget.exhausted() and budget.spent == 11.5
    with pytest.raises(ValueError):
        budget.charge(-1.0)
    with pytest.raises(ValueError):
        fd.FdBudget(-1.0)


def test_budget_zero_exhausts_without_solving(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args: object, **kw: object) -> None:
        raise AssertionError("synthesize_fd solved with a zero budget")

    monkeypatch.setattr(fd, "solve", boom)
    seed_geom = ms(0.30)
    geom, result, stats = fd.synthesize_fd(seed_geom, 50.0, "z0", "width", 0.37,
                                           budget=fd.FdBudget(0.0))
    assert result is None and stats["solves"] == 0 and stats["exhausted"] is True
    assert stats["converged"] is False and geom == replace(seed_geom, width=0.37)


def test_solve_refuses_an_exhausted_budget() -> None:
    with pytest.raises(fd.FdBudgetExhausted):
        fd.solve(ms(), budget=fd.FdBudget(0.0))


def test_synthesize_validates_arguments() -> None:
    good = {"target": 50.0, "target_kind": "z0", "solve_for": "width", "seed": 0.3}
    bad_cases = [({"target": 0.0}, "target"), ({"target_kind": "zfoo"}, "target_kind"),
                 ({"solve_for": "height"}, "solve_for"), ({"seed": -1.0}, "seed"),
                 ({"solve_for": "pair_gap"}, "pair_gap"),
                 ({"target_kind": "zdiff"}, "zdiff")]
    for change, word in bad_cases:
        args = {**good, **change}
        with pytest.raises(ValueError, match=word):
            fd.synthesize_fd(ms(), args["target"], args["target_kind"], args["solve_for"],
                             args["seed"], budget=fd.FdBudget(0.0))
    with pytest.raises(ValueError, match="max_solves"):
        fd.synthesize_fd(ms(), 50.0, "z0", "width", 0.3, max_solves=0)
    with pytest.raises(ValueError, match="tol_pct"):
        fd.synthesize_fd(ms(), 50.0, "z0", "width", 0.3, tol_pct=0.0)
    # the geometry at the seed is checked even when the budget allows no solve
    with pytest.raises(ValueError, match="broadside"):
        fd.synthesize_fd(broadside(0.12, 0.4, 0.2028, 4.6, 4.4, THALF), 100.0, "zdiff",
                         "ground_gap", 0.2, budget=fd.FdBudget(0.0))


def test_pair_gap_slope_guess_follows_the_mode() -> None:
    """Z(pair gap) rises for the odd / differential mode and falls for the even / common one."""
    assert fd.slope_guess("pair_gap", "zdiff") > 0 and fd.slope_guess("pair_gap", "zodd") > 0
    assert fd.slope_guess("pair_gap", "zeven") < 0 and fd.slope_guess("pair_gap", "zcomm") < 0
    assert fd.slope_guess("width", "zeven") < 0 and fd.slope_guess("ground_gap", "zeven") > 0


# ---------------------------------------------------------------- cache key
def test_cross_section_hash_names_the_problem_not_the_start_point() -> None:
    key = fd.cross_section_hash(ms(0.30), 50.0, "z0", "width", "medium")
    assert len(key) == 64 and int(key, 16) >= 0
    assert key == fd.cross_section_hash(ms(0.41, layers=("In1.Cu",)), 50.0, "z0", "width", "medium")
    assert key == fd.cross_section_hash(ms(0.30, below=(Ply(0.2104, 4.4, df=0.02),)),
                                        50.0, "z0", "width", "medium")
    others = [fd.cross_section_hash(ms(0.30), 50.5, "z0", "width", "medium"),
              fd.cross_section_hash(ms(0.30), 50.0, "z0", "width", "fine"),
              fd.cross_section_hash(ms(0.30, h=0.2105), 50.0, "z0", "width", "medium"),
              fd.cross_section_hash(ms(0.30, mask=JLC_MASK), 50.0, "z0", "width", "medium"),
              fd.cross_section_hash(ms(0.30, etch=0.01), 50.0, "z0", "width", "medium"),
              fd.cross_section_hash(ms(0.30, ground_gap=0.2), 50.0, "z0", "width", "medium"),
              fd.cross_section_hash(ms(0.30), 50.0, "z0", "width", "medium",
                                    options=fs.SectionOptions(trace_fill_er=3.0))]
    assert len({key, *others}) == 1 + len(others)
    # a verification key (no synthesis) keeps the width
    assert (fd.cross_section_hash(ms(0.30), None, None, None, "medium")
            != fd.cross_section_hash(ms(0.31), None, None, None, "medium"))
    # solving for the ground gap: the key does not depend on the start gap (or its absence)
    assert (fd.cross_section_hash(sl_off(), 45.0, "z0", "ground_gap")
            == fd.cross_section_hash(sl_off(ground_gap=0.2), 45.0, "z0", "ground_gap"))


# ---------------------------------------------------------------- fd_sections: geometry
def _mirror_symmetric(section: fs.Section) -> bool:
    def flipped(r: fs.Rect) -> tuple[float, ...]:
        return tuple(round(v, 12) for v in (-r.x1, -r.x0, r.y0, r.y1))

    def key(r: fs.Rect) -> tuple[float, ...]:
        return tuple(round(v, 12) for v in (r.x0, r.x1, r.y0, r.y1))

    diel = {(key(r), er) for r, er in section.dielectrics}
    cond = {key(r) for _, r in section.conductors}
    return ({(flipped(r), er) for r, er in section.dielectrics} == diel
            and {flipped(r) for _, r in section.conductors} == cond)


def _conductors(section: fs.Section, name: str) -> list[fs.Rect]:
    return [r for n, r in section.conductors if n == name]


def _box(r: fs.Rect) -> tuple[float, float, float, float]:
    return (r.x0, r.x1, r.y0, r.y1)


def _boxes(rects: list[fs.Rect]) -> list[tuple[float, float, float, float]]:
    return sorted(_box(r) for r in rects)


def approx_boxes(*boxes: tuple[float, float, float, float]) -> list:
    return [pytest.approx(b) for b in sorted(boxes)]


def test_microstrip_section_layout() -> None:
    prob = fs.build(ms(0.4, h=0.2))
    assert [s.boundary for s in prob.sections] == ["dirichlet", "neumann"]
    sec = prob.sections[0]
    assert sec.bottom_ground and not sec.top_ground and sec.y0 == 0.0
    assert _boxes(_conductors(sec, "sig")) == approx_boxes((-0.2, 0.2, 0.2, 0.2 + T1OZ))
    assert sec.y1 == pytest.approx(0.2 + 20 * 0.2) and sec.x_half == pytest.approx(20 * 0.2 + 0.8)
    assert sec.h_ref == pytest.approx(0.2)
    assert [(m.name, m.mirror, m.lines) for m in prob.modes] == [("se", "even", 1)]
    assert all(_mirror_symmetric(s) for s in prob.sections)


def test_multi_ply_reference_stack_is_painted_bottom_up() -> None:
    geom = ms(0.3, below=(Ply(0.1, 3.0), Ply(0.12, 4.6)))      # outward from the trace
    sec = fs.build(geom).sections[0]
    slabs = sorted((r.y0, r.y1, er) for r, er in sec.dielectrics if r.x0 <= -sec.x_half)
    assert slabs[:2] == [(0.0, pytest.approx(0.12), 4.6),
                         (pytest.approx(0.12), pytest.approx(0.22), 3.0)]


def test_mask_is_conformal_with_gap_fill() -> None:
    geom = ms(0.259, pair="edge", pair_gap=0.15, mask=JLC_MASK)
    sec = fs.build(geom).sections[0]
    ys = 0.2104
    masks = [_box(r) for r, er in sec.dielectrics if er == 3.8]
    assert pytest.approx((-sec.x_half, sec.x_half, ys, ys + 0.03048)) in masks            # c1
    assert pytest.approx((0.075 - 0.01524, 0.334 + 0.01524, ys, ys + T1OZ + 0.01524)) in masks
    assert pytest.approx((-0.075, 0.075, ys, ys + 0.03048)) in masks                      # c3
    assert _mirror_symmetric(sec)


def test_edge_pair_modes_and_positions() -> None:
    prob = fs.build(ms(0.259, pair="edge", pair_gap=0.15))
    sec = prob.sections[0]
    assert _boxes(_conductors(sec, "p")) == approx_boxes((0.075, 0.334, 0.2104, 0.2104 + T1OZ))
    assert _boxes(_conductors(sec, "n")) == approx_boxes((-0.334, -0.075, 0.2104, 0.2104 + T1OZ))
    modes = {m.name: m for m in prob.modes}
    assert (modes["even"].mirror, modes["odd"].mirror) == ("even", "odd")
    assert dict(modes["odd"].potentials) == {"p": 1.0, "n": -1.0}
    assert modes["even"].lines == modes["odd"].lines == 2


def test_cpwg_grounds_default_width_and_declared_width() -> None:
    thin = fs.build(cpwg(0.3, 0.2104, 4.4, 0.15)).sections[0]
    assert _boxes(_conductors(thin, fs.GND)) == approx_boxes(
        (-1.3, -0.3, 0.2104, 0.2104 + T1OZ), (0.3, 1.3, 0.2104, 0.2104 + T1OZ))
    thick = fs.build(cpwg(1.0, 1.51, 4.5, 0.2)).sections[0]
    assert max(r.x1 for r in _conductors(thick, fs.GND)) == pytest.approx(0.5 + 0.2 + 1.51)
    declared = fs.build(cpwg(1.0, 1.51, 4.5, 0.2, ground_width=1.5)).sections[0]
    assert max(r.x1 for r in _conductors(declared, fs.GND)) == pytest.approx(2.2)
    with pytest.raises(ValueError, match="ground_gap"):
        fs.build(cpwg(0.3, 0.2104, 4.4, None))


def test_ground_gap_puts_same_layer_ground_beside_a_stripline() -> None:
    """ADR D7.6 step 4: `line: cpwg` adds same-layer ground on any layer, so ground_gap is
    modelled on every kind (a stripline's closed form ignores it and sends the line to FD)."""
    sec = fs.build(sl_off(ground_gap=0.1)).sections[0]
    a, ys = 0.1347 / 2 + 0.1, (0.1088, 0.1088 + THALF)
    assert _boxes(_conductors(sec, fs.GND)) == approx_boxes(
        (-a - 1.0, -a, *ys), (a, a + 1.0, *ys))               # default max(1.0, thicker side)
    b = 0.1088 + THALF + 0.55
    assert sec.x_half == pytest.approx(2 * 0.1347 + 0.1 + 1.0 + 8 * b)
    assert _mirror_symmetric(sec)
    wide = fs.build(sl_off(ground_gap=0.1, ground_width=2.5)).sections[0]
    assert max(r.x1 for r in _conductors(wide, fs.GND)) == pytest.approx(a + 2.5)


def test_ground_gap_on_every_kind_and_pairs() -> None:
    assert fs.build(ms(0.3, ground_gap=0.15)).sections == fs.build(
        cpwg(0.3, 0.2104, 4.4, 0.15)).sections            # microstrip + grounds is a CPWG
    emb = fs.build(ms(0.3, above=(Ply(0.1, 4.4),), kind="embedded_microstrip",
                      ground_gap=0.2)).sections[0]
    assert len(_conductors(emb, fs.GND)) == 2
    pair = fs.build(sl_off(0.0894, pair="edge", pair_gap=0.15, ground_gap=0.2)).sections[0]
    x0 = 0.075 + 0.0894 + 0.2
    assert min(r.x0 for r in _conductors(pair, fs.GND) if r.x0 > 0) == pytest.approx(x0)
    dual = LineGeometry(kind="dual_stripline", pair="single", width=0.1, t=THALF,
                        below=(Ply(0.3, 4.41),), above=(Ply(0.4168, 4.4), Ply(THALF, 4.4),
                                                        Ply(0.3, 4.41)), ground_gap=0.15)
    assert len(_conductors(fs.build(dual).sections[0], fs.GND)) == 2


def test_ground_fields_that_cannot_be_modelled_are_rejected() -> None:
    with pytest.raises(ValueError, match="broadside"):
        fs.build(replace(broadside(0.12, 0.4, 0.2028, 4.6, 4.4, THALF), ground_gap=0.2))
    with pytest.raises(ValueError, match="ground_width"):
        fs.build(sl_off(ground_width=1.0))
    with pytest.raises(ValueError, match="ground_gap"):
        fs.build(sl_off(ground_gap=0.0))


def test_cpw_without_back_plane_floats_the_bottom() -> None:
    sec = fs.build(cpwg(0.3, 0.5, 4.4, 0.15, kind="cpw")).sections[0]
    assert not sec.bottom_ground and sec.y0 < 0.0
    substrate = [r for r, er in sec.dielectrics if er == 4.4]
    assert substrate and min(r.y0 for r in substrate) == 0.0


def test_stripline_is_a_closed_box() -> None:
    prob = fs.build(sl_off())
    assert len(prob.sections) == 1
    sec = prob.sections[0]
    assert sec.boundary == "dirichlet" and sec.bottom_ground and sec.top_ground
    assert sec.y1 == pytest.approx(0.1088 + THALF + 0.55)
    assert sec.x_half == pytest.approx(2 * 0.1347 + 8 * sec.y1)
    assert not _conductors(sec, fs.GND)
    # the trace's own layer is filled with the lower-Er neighbour (the prepreg)
    fill = [er for r, er in sec.dielectrics if r.y0 == pytest.approx(0.1088) and r.y1 > r.y0]
    assert fill == [4.16]
    over = fs.build(sl_off(), fs.SectionOptions(trace_fill_er=4.6)).sections[0]
    assert [er for r, er in over.dielectrics if r.y0 == pytest.approx(0.1088)] == [4.6]


def test_dual_stripline_dielectric_copper_is_a_ply() -> None:
    geom = LineGeometry(kind="dual_stripline", pair="single", width=0.1, t=THALF,
                        below=(Ply(0.3, 4.41),),
                        above=(Ply(0.4168, 4.4), Ply(THALF, 4.4), Ply(0.3, 4.41)),
                        dielectric_copper=(("In3.Cu", 4.4),))
    sec = fs.build(geom).sections[0]
    assert sec.y1 == pytest.approx(0.3 + THALF + 0.4168 + THALF + 0.3)
    assert {n for n, _ in sec.conductors} == {"sig"}


def test_broadside_partner_sits_pair_gap_below() -> None:
    prob = fs.build(broadside(0.12, 0.4, 0.2028, 4.6, 4.4, THALF))
    sec = prob.sections[0]
    assert _boxes(_conductors(sec, "n")) == approx_boxes((-0.06, 0.06, 0.4, 0.4 + THALF))
    assert _boxes(_conductors(sec, "p")) == approx_boxes(
        (-0.06, 0.06, 0.4 + THALF + 0.2028, 0.4 + 2 * THALF + 0.2028))
    assert sec.y1 == pytest.approx(0.8 + 2 * THALF + 0.2028)
    assert all(m.mirror == "even" and m.lines == 2 for m in prob.modes)
    with pytest.raises(ValueError, match="pair_gap"):
        fs.build(replace(broadside(0.12, 0.4, 0.2028, 4.6, 4.4, THALF), pair_gap=0.3))


def test_etch_staircase_narrows_toward_the_top() -> None:
    sec = fs.build(ms(0.348, etch=0.025)).sections[0]
    steps = sorted(_conductors(sec, "sig"), key=lambda r: r.y0)
    assert len(steps) == fs.ETCH_STEPS
    widths = [r.x1 - r.x0 for r in steps]
    assert widths == sorted(widths, reverse=True)
    assert widths[0] < 0.348 and widths[-1] > 0.348 - 0.025
    assert sum(widths) / len(widths) == pytest.approx(0.348 - 0.0125)
    with pytest.raises(ValueError, match="etch"):
        fs.build(ms(0.02, etch=0.02))


def test_voids_leave_copper_outside_the_corridor() -> None:
    geom = ms(0.3, below=(Ply(0.1, 4.4), Ply(THALF, 4.4), Ply(0.2, 4.4)))
    sec = fs.build(geom, fs.SectionOptions(voids=(fs.Void("below", 1, 1.2),))).sections[0]
    gnd = sorted(_conductors(sec, fs.GND), key=lambda r: r.x0)
    assert len(gnd) == 2 and gnd[1].x0 == pytest.approx(0.6) and gnd[1].x1 == sec.x_half
    assert gnd[1].y0 == pytest.approx(0.2) and gnd[1].y1 == pytest.approx(0.2 + THALF)
    whole = fs.build(geom, fs.SectionOptions(voids=(fs.Void("below", 1, None),))).sections[0]
    assert not _conductors(whole, fs.GND)
    with pytest.raises(ValueError, match="void"):
        fs.build(geom, fs.SectionOptions(voids=(fs.Void("below", 3, 1.0),)))


def test_unsupported_geometry_is_rejected() -> None:
    with pytest.raises(ValueError, match="kind"):
        fs.build(ms(kind="slotline"))
    with pytest.raises(ValueError, match="width"):
        fs.build(ms(0.0))
    with pytest.raises(ValueError, match="pair_gap"):
        fs.build(ms(pair="edge"))
    with pytest.raises(ValueError, match="above"):
        fs.build(sl_off(above=()))
    with pytest.raises(ValueError, match="trace_fill_er"):
        fs.build(sl_off(), fs.SectionOptions(trace_fill_er=0.5))


# ---------------------------------------------------------------- numerics: exact references
@needs_fd
def test_fd_matches_exact_cohn_stripline() -> None:
    res = fd.solve(air_stripline())
    assert res.z0 == pytest.approx(cohn_stripline(0.3, 0.5, 1.0), rel=3e-3)
    assert res.eeff == pytest.approx(1.0, rel=1e-9)
    assert res.method == fd.available() and res.cells > 0 and res.seconds > 0
    assert res.z_diff is None and res.grid == "medium"


@needs_fd
@pytest.mark.parametrize("gap", [0.05, 0.1, 0.25])
def test_fd_matches_exact_shielded_coplanar_stripline(gap: float) -> None:
    """Same-layer grounds beside a stripline are solved, not ignored (exact conformal map)."""
    res = fd.solve(air_stripline(gap))
    assert res.z0 == pytest.approx(shielded_coplanar_stripline(0.3, gap, 0.5, 1.0), rel=3e-3)
    assert res.z0 < cohn_stripline(0.3, 0.5, 1.0)


@needs_fd
def test_ground_gap_lowers_an_offset_stripline() -> None:
    plain, grounded = fd.solve(sl_off()), fd.solve(sl_off(ground_gap=0.1))
    assert grounded.z0 < 0.99 * plain.z0


BROADSIDE_T0 = [(0.1434, 0.4, 0.2028, 4.55), (0.3, 0.4, 0.2028, 4.55),
                (0.6, 0.4, 0.2028, 4.55), (0.3, 0.25, 0.1, 3.66)]


@needs_fd
@pytest.mark.parametrize("w, core, pp, er", BROADSIDE_T0)
def test_fd_matches_cohn_broadside_at_zero_thickness(w: float, core: float, pp: float,
                                                     er: float) -> None:
    b = 2 * core + pp
    z_even, z_odd = cohn_broadside(w, pp, b, er)
    res = fd.solve(broadside(w, core, pp, er, er, 0.0))
    assert res.z_diff == pytest.approx(2 * z_odd, rel=3e-3)
    if w / b >= 0.35 * (1 - pp / b):                     # Cohn's Zeven validity
        assert res.z_even == pytest.approx(z_even, rel=3e-3)
    assert res.z_comm == pytest.approx(res.z_even / 2)
    assert res.z0 == res.z_odd and res.eeff == res.eeff_odd   # tline pair convention


# ---------------------------------------------------------------- numerics: D7.14 vectors
VECTORS = {
    "MS50_7628": (ms(0.3721), "z0", 50.01, 0.25),
    "SL50_off": (sl_off(0.1347), "z0", 50.0, 0.5),
    "CPWG50_7628_mask": (cpwg(0.2567, 0.2104, 4.4, 0.15, ground_width=1.0, mask=JLC_MASK),
                         "z0", 50.0, 0.5),
    "DMS90_7628": (ms(0.2816, pair="edge", pair_gap=0.15), "z_diff", 90.0, 0.9),
    "DSL100_off": (sl_off(0.0894, pair="edge", pair_gap=0.15), "z_diff", 100.0, 1.0),
    "BSL100": (broadside(0.1157, 0.4, 0.2028, 4.6, 4.4, THALF), "z_diff", 100.0, 1.0),
    "DMS90_7628_mask_at_0.259": (ms(0.259, pair="edge", pair_gap=0.15, mask=JLC_MASK),
                                 "z_diff", 86.80, 0.868),
}


@needs_fd
@pytest.mark.parametrize("name", sorted(VECTORS))
def test_d7_14_vectors(name: str) -> None:
    geom, attr, z_ref, tol = VECTORS[name]
    res = fd.solve(geom)
    assert getattr(res, attr) == pytest.approx(z_ref, abs=tol), name


@needs_fd
def test_open_structures_average_dirichlet_and_neumann_boxes() -> None:
    res = fd.solve(ms(0.3721))
    assert 0.0 < res.box_spread < 0.25            # research: 0.079 ohm on MS50_7628
    assert fd.solve(sl_off()).box_spread == 0.0   # closed box: one solve


# ---------------------------------------------------------------- numerics: synthesis
E2E_RO4350B = [("microstrip", ms(0.5231, h=0.254, er=3.66, mask=JLC_MASK), 0.4942),
               ("cpwg", cpwg(0.4408, 0.254, 3.66, 0.2, ground_width=1.0, mask=JLC_MASK), 0.3975)]


@needs_fd
@pytest.mark.parametrize("label, seed_geom, w_ref", E2E_RO4350B)
def test_d7_14_e2e_masked_widths(label: str, seed_geom: LineGeometry, w_ref: float) -> None:
    geom, res, stats = fd.synthesize_fd(seed_geom, 50.0, "z0", "width", seed_geom.width,
                                        budget=fd.FdBudget(600.0))
    assert stats["converged"] and not stats["exhausted"] and stats["solves"] <= 8, stats
    assert geom.width == pytest.approx(w_ref, rel=0.01), label
    assert res.z0 == pytest.approx(50.0, rel=0.002)


@needs_fd
@pytest.mark.parametrize("geom, kind, target, seed, w_ref", [
    (sl_off(), "z0", 50.0, 0.1536, 0.1347),
    (sl_off(pair="edge", pair_gap=0.15), "zdiff", 100.0, 0.1003, 0.0894)])
def test_synthesize_fd_reproduces_offset_striplines(geom: LineGeometry, kind: str, target: float,
                                                    seed: float, w_ref: float) -> None:
    budget = fd.FdBudget(600.0)
    out, res, stats = fd.synthesize_fd(geom, target, kind, "width", seed, budget=budget)
    assert stats["converged"] and stats["solves"] <= 8, stats
    assert out.width == pytest.approx(w_ref, rel=0.01)
    z = res.z0 if kind == "z0" else res.z_diff
    assert abs(z / target - 1) * 100 <= 0.2 + 1e-9 and stats["error_pct"] == pytest.approx(
        100 * (z / target - 1))
    assert budget.spent == pytest.approx(stats["seconds"], rel=1e-6)
    assert out == replace(geom, width=out.width)


@needs_fd
def test_synthesize_fd_solves_for_the_pair_gap() -> None:
    geom = sl_off(0.0894, pair="edge", pair_gap=0.25)
    out, res, stats = fd.synthesize_fd(geom, 100.0, "zdiff", "pair_gap", 0.25)
    assert stats["converged"], stats
    assert out.pair_gap == pytest.approx(0.15, rel=0.03) and out.width == 0.0894
    assert res.z_diff == pytest.approx(100.0, rel=0.002)


@needs_fd
@pytest.mark.parametrize("kind, attr", [("zeven", "z_even"), ("zcomm", "z_comm")])
def test_synthesize_fd_pair_gap_for_even_and_common_targets(kind: str, attr: str) -> None:
    """Z_even falls as the gap grows: the synthesis must step the other way (it diverged)."""
    target = getattr(fd.solve(sl_off(0.0894, pair="edge", pair_gap=0.15)), attr)
    out, res, stats = fd.synthesize_fd(sl_off(0.0894, pair="edge", pair_gap=0.25), target,
                                       kind, "pair_gap", 0.25)
    assert stats["converged"] and stats["solves"] <= 8, stats
    assert out.pair_gap == pytest.approx(0.15, rel=0.03)
    assert getattr(res, attr) == pytest.approx(target, rel=0.002)


@needs_fd
def test_synthesize_fd_solves_the_ground_gap_beside_a_stripline() -> None:
    target = fd.solve(sl_off(ground_gap=0.08)).z0
    out, res, stats = fd.synthesize_fd(sl_off(), target, "z0", "ground_gap", 0.15)
    assert stats["converged"] and stats["solves"] <= 8, stats
    assert out.ground_gap == pytest.approx(0.08, rel=0.03) and out.width == 0.1347
    assert res.z0 == pytest.approx(target, rel=0.002)


@needs_fd
def test_synthesize_fd_returns_best_so_far_when_the_budget_runs_out() -> None:
    geom = sl_off()
    out, res, stats = fd.synthesize_fd(geom, 50.0, "z0", "width", 0.2, budget=fd.FdBudget(1e-6))
    assert stats["solves"] == 1 and stats["exhausted"] and not stats["converged"]
    assert out.width == 0.2 and res is not None and res.z0 < 50.0


@needs_fd
def test_solve_charges_the_budget() -> None:
    budget = fd.FdBudget(1000.0)
    res = fd.solve(sl_off(), budget=budget)
    assert budget.spent == pytest.approx(res.seconds)


# ---------------------------------------------------------------- slow (opt-in)
@pytest.mark.slow
def test_numpy_pcg_equals_scipy(request: pytest.FixtureRequest) -> None:
    _opt_in_slow(request)
    if fd.available() != "scipy":
        pytest.skip("needs scipy and numpy")
    geom = sl_off()
    ref = fd.solve(geom, backend="scipy")
    pcg = fd.solve(geom, backend="numpy")
    assert pcg.method == "numpy" and pcg.z0 == pytest.approx(ref.z0, rel=1e-3)
    assert pcg.eeff == pytest.approx(ref.eeff, rel=1e-3)


@pytest.mark.slow
def test_fine_grid_ms50_7628(request: pytest.FixtureRequest) -> None:
    _opt_in_slow(request)
    if fd.available() == "none":
        pytest.skip("no FD backend")
    res = fd.solve(ms(0.3721), grid="fine")
    assert res.z0 == pytest.approx(50.0096, abs=0.05) and res.grid == "fine"


@pytest.mark.slow
def test_medium_solve_under_20_s_with_scipy(request: pytest.FixtureRequest) -> None:
    _opt_in_slow(request)
    if fd.available() != "scipy":
        pytest.skip("needs scipy")
    for geom in (ms(0.3721), ms(0.3721, mask=JLC_MASK), cpwg(0.2567, 0.2104, 4.4, 0.15)):
        assert fd.solve(geom).seconds < 20.0, os.environ.get("COMPUTERNAME", "?")
