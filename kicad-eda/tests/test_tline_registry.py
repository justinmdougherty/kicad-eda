"""tline package core (ADR D7.6, task D7-04): kind registry, value types, elliptic kernels,
Djordjevic-Sarkar dispersion, package hygiene.

The registry is the extension point that lets D7-05 (stripline, broadside, coupled
microstrip) and D7-05c (fallback) add models without editing tline/__init__.py: the
package imports a FIXED module list and skips a module only while it does not exist.
Every test that registers something works on a copy of the registry (monkeypatched).
"""
from __future__ import annotations

import ast
import dataclasses
import importlib
import math
import subprocess
import sys
from itertools import pairwise
from pathlib import Path

import pytest

import tline
import toolchain as tc
from tline import dispersion, elliptic
from tline.types import Issue, LineGeometry, LineResult, MaskModel, Ply

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "server" / "tools"
TLINE = TOOLS / "tline"
FIXED_LIST = ("microstrip", "coplanar", "stripline", "broadside", "coupled_microstrip", "fallback")
KICAD_PY_TIMEOUT_S = 120


def _geom(kind: str = "microstrip", pair: str = "single", **kw: object) -> LineGeometry:
    base = {"kind": kind, "pair": pair, "width": 0.3721, "t": 0.035,
            "below": (Ply(0.2104, 4.4),), "above": ()}
    return LineGeometry(**{**base, **kw})


def _result(z0: float = 42.0) -> LineResult:
    return LineResult(z0=z0, eeff=2.0, eeff_f=2.0, z_even=None, z_odd=None, z_diff=None,
                      z_comm=None, delay_ps_per_mm=4.7, model="fake", accuracy_class="A",
                      est_error_pct=0.0, warnings=())


@pytest.fixture
def isolated_registry(monkeypatch: pytest.MonkeyPatch):
    """A private copy of the registry; restored after the test."""
    monkeypatch.setattr(tline, "_REGISTRY", tline._REGISTRY)
    return tline


@pytest.fixture
def scratch_models(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_registry):
    """Write throw-away model modules into a directory on tline.__path__; unload them after."""
    monkeypatch.setattr(tline, "__path__", [*tline.__path__, str(tmp_path)])
    written: list[str] = []

    def write(name: str, body: str) -> str:
        (tmp_path / f"{name}.py").write_text(body, encoding="utf-8")
        written.append(name)
        return name

    yield write
    for name in written:
        sys.modules.pop(f"tline.{name}", None)
        if hasattr(tline, name):
            delattr(tline, name)


# ---------------------------------------------------------------- registry
def test_fixed_module_list_is_the_adr_list():
    assert tline.MODEL_MODULES == FIXED_LIST


def test_core_kinds_are_registered_at_import():
    keys = tline.registered()
    for key in [("microstrip", "single"), ("embedded_microstrip", "single"),
                ("cpwg", "single"), ("cpw", "single")]:
        assert key in keys
    assert list(keys) == sorted(keys)
    assert isinstance(keys, tuple)


def test_every_present_listed_module_was_loaded():
    present = [m for m in FIXED_LIST if (TLINE / f"{m}.py").is_file()]
    assert {"microstrip", "coplanar"} <= set(present)
    assert tline.LOADED == tuple(present)
    for name in present:
        assert f"tline.{name}" in sys.modules


def test_duplicate_register_raises(isolated_registry):
    def fn(geom, freq_hz):
        return _result()

    tline.register("fake_dup", "single", fn)
    with pytest.raises(ValueError, match="fake_dup"):
        tline.register("fake_dup", "single", fn)
    with pytest.raises(ValueError, match="microstrip"):
        tline.register("microstrip", "single", fn)


def test_register_validates_its_arguments(isolated_registry):
    def fn(geom, freq_hz):
        return _result()

    with pytest.raises(ValueError):
        tline.register("", "single", fn)
    with pytest.raises(ValueError, match="pair"):
        tline.register("fake_kind", "diagonal", fn)
    with pytest.raises(TypeError):
        tline.register("fake_kind", "single", "not callable")


def test_register_does_not_mutate_the_published_mapping(isolated_registry):
    before = tline._REGISTRY
    tline.register("fake_new", "edge", lambda g, f: _result())
    assert ("fake_new", "edge") not in before
    assert ("fake_new", "edge") in tline.registered()
    with pytest.raises(TypeError):
        tline._REGISTRY[("x", "single")] = None          # read-only view


def test_unknown_kind_raises_not_implemented_naming_it():
    with pytest.raises(NotImplementedError, match="no_such_kind"):
        tline.evaluate(_geom(kind="no_such_kind"), 0.0)


def test_unregistered_pair_raises_not_implemented():
    with pytest.raises(NotImplementedError, match="cpwg.*broadside|broadside.*cpwg"):
        tline.evaluate(_geom(kind="cpwg", pair="broadside", ground_gap=0.15), 0.0)


@pytest.mark.parametrize("freq", [-1.0, math.nan, math.inf])
def test_evaluate_rejects_bad_frequencies(freq):
    with pytest.raises(ValueError, match="freq"):
        tline.evaluate(_geom(), freq)


def test_evaluate_rejects_non_geometry():
    with pytest.raises(TypeError):
        tline.evaluate({"kind": "microstrip", "pair": "single"}, 0.0)


def test_evaluate_rejects_a_model_that_returns_the_wrong_type(isolated_registry):
    tline.register("fake_bad", "single", lambda g, f: {"z0": 50})
    with pytest.raises(TypeError, match="fake_bad"):
        tline.evaluate(_geom(kind="fake_bad"), 0.0)


def test_evaluate_dispatches_with_the_frequency(isolated_registry):
    seen = []

    def fn(geom, freq_hz):
        seen.append((geom.kind, freq_hz))
        return _result(z0=77.0)

    tline.register("fake_dispatch", "broadside", fn)
    out = tline.evaluate(_geom(kind="fake_dispatch", pair="broadside"), 9.15e8)
    assert out.z0 == 77.0
    assert seen == [("fake_dispatch", 9.15e8)]


def test_a_fake_model_module_registers_without_editing_init(scratch_models):
    """What D7-05 does: a new module file calls register() at import."""
    init_before = (TLINE / "__init__.py").read_bytes()
    name = scratch_models("fake_model_d705", (
        "from . import register\n"
        "from .types import LineResult\n\n"
        "def _fake(geom, freq_hz):\n"
        "    return LineResult(z0=12.5, eeff=1.0, eeff_f=1.0, z_even=None, z_odd=None,\n"
        "                      z_diff=None, z_comm=None, delay_ps_per_mm=3.3, model='fake',\n"
        "                      accuracy_class='B', est_error_pct=1.0, warnings=())\n\n"
        "register('fake_stripline', 'edge', _fake)\n"))
    loaded = tline.load_models((name,))
    assert loaded == (name,)
    assert ("fake_stripline", "edge") in tline.registered()
    assert tline.evaluate(_geom(kind="fake_stripline", pair="edge"), 0.0).z0 == 12.5
    assert (TLINE / "__init__.py").read_bytes() == init_before


def test_a_missing_listed_module_is_skipped(isolated_registry):
    assert tline.load_models(("module_that_does_not_exist_d704",)) == ()


def test_a_module_with_a_missing_dependency_propagates(scratch_models):
    name = scratch_models("broken_dep_d704", "import numpy_is_not_installed_d704  # noqa\n")
    with pytest.raises(ModuleNotFoundError) as info:
        tline.load_models((name,))
    assert info.value.name == "numpy_is_not_installed_d704"


def test_a_module_that_fails_at_import_propagates(scratch_models):
    name = scratch_models("broken_body_d704", "raise RuntimeError('model module is broken')\n")
    with pytest.raises(RuntimeError, match="broken"):
        tline.load_models((name,))


def test_reloading_the_package_keeps_the_registry():
    """Submodules stay cached across a package reload, so the registry must survive it."""
    before = tline.registered()
    importlib.reload(tline)
    assert tline.registered() == before
    assert tline.evaluate(_geom()).z0 == pytest.approx(50.0, abs=0.05)


def test_load_models_skips_only_the_missing_one(scratch_models):
    good = scratch_models("good_model_d704",
                          "from . import register\n"
                          "register('fake_good', 'single', lambda g, f: None)\n")
    assert tline.load_models(("absent_model_d704", good)) == (good,)


# ---------------------------------------------------------------- types
def test_types_have_the_adr_fields_in_order():
    def names(cls):
        return [f.name for f in dataclasses.fields(cls)]

    assert names(Ply) == ["thickness", "er", "df", "f_spec_hz"]
    assert names(MaskModel) == ["er", "over_trace", "over_substrate", "between"]
    assert names(LineGeometry) == ["kind", "pair", "width", "t", "below", "above", "pair_gap",
                                   "ground_gap", "ground_width", "mask", "etch", "layers",
                                   "dielectric_copper"]
    assert names(LineResult) == ["z0", "eeff", "eeff_f", "z_even", "z_odd", "z_diff", "z_comm",
                                 "delay_ps_per_mm", "model", "accuracy_class", "est_error_pct",
                                 "warnings"]
    assert Issue._fields == ("id", "severity", "message", "path")


def test_types_are_frozen_and_hashable():
    geom = _geom()
    with pytest.raises(dataclasses.FrozenInstanceError):
        geom.width = 1.0
    assert hash(geom) == hash(_geom())
    assert Ply(0.2, 4.4).df == 0.0 and Ply(0.2, 4.4).f_spec_hz == 1e9


def test_geometry_normalises_sequences_to_tuples():
    geom = LineGeometry(kind="microstrip", pair="single", width=0.3, t=0.035,
                        below=[Ply(0.2, 4.4)], above=[], layers=["F.Cu"],
                        dielectric_copper=[("In3.Cu", 4.4)])
    assert geom.below == (Ply(0.2, 4.4),) and geom.above == ()
    assert geom.layers == ("F.Cu",)
    assert geom.dielectric_copper == (("In3.Cu", 4.4),)


@pytest.mark.parametrize("kwargs", [
    {"thickness": 0.0, "er": 4.4}, {"thickness": -0.1, "er": 4.4}, {"thickness": 0.1, "er": 0.9},
    {"thickness": math.nan, "er": 4.4}, {"thickness": 0.1, "er": 4.4, "df": -0.01},
    {"thickness": 0.1, "er": 4.4, "f_spec_hz": 0.0}])
def test_ply_rejects_unphysical_values(kwargs):
    with pytest.raises(ValueError):
        Ply(**kwargs)


@pytest.mark.parametrize("kwargs", [
    {"pair": "diagonal"}, {"kind": ""}, {"width": -0.1}, {"t": -0.01}, {"etch": -0.01},
    {"ground_gap": -0.1}, {"below": (0.2,)}, {"mask": {"er": 3.8}}])
def test_geometry_rejects_bad_fields(kwargs):
    with pytest.raises((ValueError, TypeError)):
        _geom(**kwargs)


def test_mask_and_result_validation():
    with pytest.raises(ValueError):
        MaskModel(er=0.5, over_trace=0.0152, over_substrate=0.0305, between=0.0305)
    with pytest.raises(ValueError):
        MaskModel(er=3.8, over_trace=-0.01, over_substrate=0.0305, between=0.0305)
    with pytest.raises(ValueError, match="accuracy"):
        dataclasses.replace(_result(), accuracy_class="D")


# ---------------------------------------------------------------- elliptic kernels
def test_complete_elliptic_integral_known_values():
    assert elliptic.ellipk(0.0) == pytest.approx(math.pi / 2, rel=1e-15)
    # K(1/sqrt 2) = Gamma(1/4)^2 / (4 sqrt(pi))
    assert elliptic.ellipk(math.sqrt(0.5)) == pytest.approx(
        math.gamma(0.25) ** 2 / (4 * math.sqrt(math.pi)), rel=1e-14)
    assert elliptic.ellipk(0.5) == pytest.approx(1.685750354812596, rel=1e-14)
    assert elliptic.k_ratio(math.sqrt(0.5)) == pytest.approx(1.0, rel=1e-14)
    with pytest.raises(ValueError):
        elliptic.ellipk(1.0)


def test_elliptic_complementary_modulus_keeps_precision_near_one():
    kp = 1e-12                               # k = 1 - 5e-25: unrepresentable as a float
    expected = math.log(4 / kp)              # K ~ ln(4/k') + O(k'^2)
    assert elliptic.ellipk_from_kp(kp) == pytest.approx(expected, rel=1e-12)
    assert elliptic.k_ratio(1.0, kp) == pytest.approx(expected / (math.pi / 2), rel=1e-12)
    assert elliptic.ellipk_from_kp(0.0) == math.inf


@pytest.mark.parametrize("xa, xb", [(0.3, 0.9), (1.2, 1.25), (5.0, 9.0), (0.01, 0.02)])
def test_analytic_complements_match_direct_evaluation(xa, xb):
    k, kp = elliptic.tanh_ratio_modulus(xa, xb)
    assert k == pytest.approx(math.tanh(xa) / math.tanh(xb), rel=1e-12)
    assert kp == pytest.approx(math.sqrt(1 - k * k), rel=1e-9)
    k, kp = elliptic.sinh_ratio_modulus(xa, xb)
    assert k == pytest.approx(math.sinh(xa) / math.sinh(xb), rel=1e-12)
    assert kp == pytest.approx(math.sqrt(1 - k * k), rel=1e-9)
    (ke, kep), (ko, kop) = elliptic.coupled_moduli(xa, xb)
    assert ke == pytest.approx(math.tanh(xa) * math.tanh(xb), rel=1e-12)
    assert ko == pytest.approx(math.tanh(xa) / math.tanh(xb), rel=1e-12)
    assert kep == pytest.approx(math.sqrt(1 - ke * ke), rel=1e-9)
    assert kop == pytest.approx(math.sqrt(1 - ko * ko), rel=1e-9)


def test_moduli_do_not_overflow_for_large_arguments():
    k, kp = elliptic.sinh_ratio_modulus(800.0, 801.0)
    assert k == pytest.approx(math.exp(-1.0), rel=1e-12)
    assert kp == pytest.approx(math.sqrt(1 - math.exp(-2.0)), rel=1e-12)
    # tanh a = tanh b = 1.0 in floats; k' ~ sqrt(8 sinh(b - a)) exp(-(a + b)/2) survives
    k, kp = elliptic.tanh_ratio_modulus(300.0, 301.0)
    assert k == pytest.approx(1.0, rel=1e-15)
    assert kp == pytest.approx(math.sqrt(8 * math.sinh(1.0)) * math.exp(-300.5), rel=1e-9)


@pytest.mark.parametrize("xa, xb", [(39.0, 39.4), (25.0, 25.0 + 1e-9), (300.0, 300.5),
                                    (0.6, 0.6 + 1e-12), (19.9, 20.1),
                                    (18.596468569795846, 19.87199578415073)])
def test_moduli_never_leave_the_unit_interval(xa, xb):
    """Wide lines on thin dielectric: tanh a = tanh b = 1.0, ratios must not round above 1."""
    for k, kp in (elliptic.tanh_ratio_modulus(xa, xb), elliptic.sinh_ratio_modulus(xa, xb),
                  *elliptic.coupled_moduli(xa, xb)):
        assert 0.0 <= k <= 1.0 and 0.0 <= kp <= 1.0
        assert math.isfinite(elliptic.k_ratio(k, kp)) or kp == 0.0


def test_solve_monotone_brackets_and_converges():
    assert elliptic.solve_monotone(lambda x: 1 / x, 4.0, 0.01, 10.0) == pytest.approx(0.25)
    assert elliptic.solve_monotone(lambda x: x * x, 2.0, 0.0, 3.0) == pytest.approx(math.sqrt(2))
    with pytest.raises(ValueError, match="bracket"):
        elliptic.solve_monotone(lambda x: x, 5.0, 0.0, 1.0)


# ---------------------------------------------------------------- Djordjevic-Sarkar
def test_djordjevic_sarkar_reproduces_its_anchor_point():
    model = dispersion.fit_djordjevic_sarkar(4.5, 0.02, 1e9)
    assert model.dk(1e9) == pytest.approx(4.5, rel=1e-12)
    assert model.df(1e9) == pytest.approx(0.02, rel=1e-12)


def test_djordjevic_sarkar_wideband_behaviour():
    model = dispersion.fit_djordjevic_sarkar(4.5, 0.02, 1e9)
    freqs = [10.0 ** e for e in range(4, 12)]
    dks = [model.dk(f) for f in freqs]
    assert all(a > b for a, b in pairwise(dks))               # Dk falls with frequency
    # about Dk*Df*ln(10)/(pi/2) = 0.13 per decade for FR-4 4.5/0.02 mid-band
    assert model.dk(1e10) == pytest.approx(4.368, abs=0.005)
    assert model.df(1e7) == pytest.approx(0.02, rel=0.1)       # flat loss mid-band
    assert model.dk(0.0) == pytest.approx(model.eps_inf + model.delta_eps, rel=1e-12)
    lossless = dispersion.fit_djordjevic_sarkar(3.66, 0.0, 1e10)
    assert lossless.dk(1e6) == pytest.approx(3.66) and lossless.df(1e6) == 0.0


@pytest.mark.parametrize("args", [(0.9, 0.02, 1e9), (4.5, -0.01, 1e9), (4.5, 0.02, 1e2),
                                  (4.5, 0.02, 2e12), (4.5, 1.5, 1e9)])
def test_djordjevic_sarkar_rejects_bad_inputs(args):
    with pytest.raises(ValueError):
        dispersion.fit_djordjevic_sarkar(*args)


def test_log_frequency_interpolation_between_published_points():
    points = [(2.5e9, 0.0031), (1e10, 0.0037)]                 # RO4350B Df [S-RO4000]
    assert dispersion.interp_log_f(points, 5e9) == pytest.approx(0.0034, rel=1e-12)
    assert dispersion.interp_log_f(points, 1e9) == 0.0031      # nearest outside the band
    assert dispersion.interp_log_f(points, 4e10) == 0.0037
    assert dispersion.in_band(points, 5e9) and not dispersion.in_band(points, 1e9)
    assert dispersion.interp_log_f([(1e10, 3.66)], 9.15e8) == 3.66
    with pytest.raises(ValueError):
        dispersion.interp_log_f([], 1e9)


# ---------------------------------------------------------------- hygiene
def test_tline_files_stay_under_400_lines():
    for path in sorted(TLINE.glob("*.py")):
        lines = path.read_text(encoding="utf-8").count("\n")
        assert lines < 400, f"{path.name}: {lines} lines"


def test_tline_imports_only_the_standard_library():
    allowed = set(sys.stdlib_module_names)
    for path in sorted(TLINE.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                roots = [(node.module or "").split(".")[0]]
            else:
                continue
            for root in roots:
                assert root in allowed or root == "__future__", f"{path.name} imports {root}"


def test_package_imports_and_evaluates_under_kicad_python():
    """Acceptance: tline runs in KiCad's bundled Python (9.0.4 ships 3.11.5)."""
    py = tc.kicad_python()
    if py is None:
        pytest.skip(f"no KiCad bundled Python: {tc.kicad_python_error()}")
    code = ("import sys; sys.path.insert(0, sys.argv[1]); import kicad_quiet; import tline\n"
            "from tline.types import LineGeometry, Ply\n"
            "g = LineGeometry(kind='cpwg', pair='single', width=0.2957, t=0.035,\n"
            "                 below=(Ply(0.2104, 4.4),), above=(), ground_gap=0.15)\n"
            "m = LineGeometry(kind='microstrip', pair='single', width=0.3721, t=0.035,\n"
            "                 below=(Ply(0.2104, 4.4),), above=())\n"
            "print(sys.version_info[:2], tline.LOADED,\n"
            "      round(tline.evaluate(m, 2.45e9).z0, 3), round(tline.evaluate(g).z0, 3))\n")
    proc = subprocess.run([py, "-c", code, str(TOOLS)], stdin=subprocess.DEVNULL,
                          capture_output=True, encoding="utf-8", errors="replace",
                          timeout=KICAD_PY_TIMEOUT_S, check=False)
    assert proc.returncode == 0, proc.stderr
    assert "'microstrip', 'coplanar'" in proc.stdout
    z_ms, z_cpwg = (float(x) for x in proc.stdout.split(")")[-1].split())
    assert z_ms == pytest.approx(50.0, abs=0.05)
    assert z_cpwg == pytest.approx(50.0, abs=0.5)
