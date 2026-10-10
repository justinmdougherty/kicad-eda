"""pipeline/rp_feature_libs.py (F2-15, ROADMAP D1): project-local libraries in
the gated run through rp_hooks hooks only -- the libraries loaded at stage_load,
the lint context, the G1 stop on a library error, the design review's resolver,
the per-part library report and the footprint digest in the route-cache key.

Every hook runs inside a scripted run (tests/fixtures/pipeline_world.py) with
ONLY this feature enabled; the World's fake KiCad 9.0.4 install has no stock
libraries unless a test creates them (the `stock` fixture)."""
from __future__ import annotations

import copy
import json
import re
import shutil
from pathlib import Path

import pytest

import fp_dialect
import project_libs
import rp_context
import rp_feature_libs as rfl
import rp_hooks
import rp_route_hash
import rp_stage_schematic
from fixtures import pipeline_world
from fixtures.pipeline_world import DESIGN, World
from lint_context import LintContext, is_resolver

world = pipeline_world.world            # the fixture (an assignment: see pipeline_world)

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
EXAMPLE_NETLISTS = sorted(EXAMPLES.glob("*/netlist.json"))
HEX64 = re.compile(r"^[0-9a-f]{64}$")
NICK = "Proj_parts"
FP_NAME = "R_X"
STOCK_LIB, STOCK_FP = "Resistor_SMD", "R_0603_1608Metric"     # DESIGN's footprint
FP_TEXT = (f'(footprint "{FP_NAME}" (version 20241229) (generator "pcbnew") (layer "F.Cu")\n'
           '  (descr "test resistor")\n'
           '  (pad "1" smd rect (at -0.8 0) (size 0.8 0.9) (layers "F.Cu" "F.Paste" "F.Mask"))\n'
           '  (pad "2" smd rect (at 0.8 0) (size 0.8 0.9) (layers "F.Cu" "F.Paste" "F.Mask"))\n'
           ')\n')
DRIFT = "placement/routing inputs changed since the cached run"


@pytest.fixture
def use(monkeypatch):
    """use(*features): exactly these features; discovery comes back at teardown."""
    monkeypatch.setattr(rp_hooks, "_override", rp_hooks.selection())
    return lambda *features: rp_hooks.set_features(list(features))


@pytest.fixture
def stock(world: World) -> Path:
    """Stock symbols/ and footprints/ for the World's fake install (with
    DESIGN's resistor footprint), so toolchain finds stock libraries."""
    share = world.dir / "kicad9" / "share" / "kicad"
    (share / "symbols").mkdir(parents=True)
    lib = share / "footprints" / f"{STOCK_LIB}.pretty"
    lib.mkdir(parents=True)
    (lib / f"{STOCK_FP}.kicad_mod").write_text(FP_TEXT.replace(FP_NAME, STOCK_FP),
                                               encoding="utf-8")
    return share


def _library(base: Path, rel: str = f"libs/{NICK}.pretty") -> Path:
    lib = base / rel
    lib.mkdir(parents=True, exist_ok=True)
    (lib / f"{FP_NAME}.kicad_mod").write_text(FP_TEXT, encoding="utf-8")
    return lib / f"{FP_NAME}.kicad_mod"


def _project(world: World, **extra) -> dict:
    """R1 on the project footprint Proj_parts:R_X (declared in the block), R2 stock."""
    _library(world.dir)
    design = copy.deepcopy(DESIGN)
    design["components"][0]["footprint"] = f"{NICK}:{FP_NAME}"
    design = {**design, "libraries": {"footprints": {NICK: f"libs/{NICK}.pretty"}}, **extra}
    world.write_design(design)
    return design


def _flip_byte(path: Path) -> bytes:
    """Change one byte of the footprint's descr (still a valid footprint)."""
    original = path.read_bytes()
    path.write_bytes(original.replace(b"test resistor", b"test resistoR"))
    assert path.read_bytes() != original
    return original


def _loaded_ctx(world: World) -> rp_context.RunContext:
    ctx = rp_context.RunContext(netlist=str(world.netlist), w=30, h=30)
    rp_stage_schematic.stage_load(ctx)
    return ctx


def _gate(rep: dict, name: str) -> dict:
    return next(g for g in rep["gates"] if g["gate"] == name)


# --- the module ------------------------------------------------------------------------

def test_the_feature_is_discovered_with_its_order_keys_and_hooks():
    assert "libs" in rp_hooks.available()
    assert rp_hooks.load("libs") is rfl and rp_hooks.feature_name(rfl) == rfl.EXT_KEY
    assert rfl.ORDER == 10 and rfl.ROUTE_HASH_EXCLUDED == ("libraries",)
    assert not hasattr(rfl, "ROUTE_HASH_DESIGN_KEYS")
    hooks = {name for name in rp_hooks.HOOKS if callable(getattr(rfl, name, None))}
    assert hooks == {"on_load", "lint_kwargs", "g1_verdict", "analysis_kwargs",
                     "g2_verdict", "route_hash_extra"}


def test_the_module_stays_compact():
    lines = Path(rfl.__file__).read_text(encoding="utf-8").splitlines()
    assert len(lines) < 200


# --- on_load and the library report ------------------------------------------------------

def test_on_load_keeps_the_project_libraries_in_the_feature_state(world, use):
    _project(world)
    use("libs")
    ctx = _loaded_ctx(world)
    libs = ctx.ext["libs"]
    assert isinstance(libs, project_libs.ProjectLibs) and libs.ok
    assert libs.netlist_dir == world.dir.resolve()
    assert libs.resolver.resolve("footprint", NICK).source == "netlist"


def test_the_report_names_each_parts_library_source(world):
    design = _project(world)
    world.use_features("libs")
    rep = world.run()
    want = project_libs.sources(project_libs.load(world.netlist, design), design)
    assert rep["PASS"] is True and rep["libraries"] == want
    assert want["R1"]["footprint"] == "project" and set(want) == {"R1", "R2"}
    saved = json.loads(Path(rep["pipeline_report"]).read_text(encoding="utf-8"))
    assert saved["libraries"] == want


def test_the_report_marks_stock_parts_stock(world, stock):
    world.use_features("libs")
    rep = world.run()
    assert {ref: src["footprint"] for ref, src in rep["libraries"].items()} == \
        {"R1": "stock", "R2": "stock"}


def test_a_library_report_failure_is_recorded_and_never_stops_the_run(world, monkeypatch):
    def boom(libs, design):
        raise RuntimeError("cannot read the sources")
    monkeypatch.setattr(project_libs, "sources", boom)
    world.use_features("libs")
    rep = world.run()
    assert rep["PASS"] is True and "libraries" not in rep
    assert rep["libraries_error"] == "RuntimeError: cannot read the sources"


def test_g2_verdict_reports_the_design_g2_settled_on(world, use):
    _project(world)
    use("libs")
    ctx = _loaded_ctx(world)
    ctx.report["libraries_error"] = "an earlier failure"
    added = {"ref": "C9", "lib_id": "Device:C", "footprint": f"{NICK}:{FP_NAME}"}
    ctx.design = {**ctx.design, "components": [*ctx.design["components"], added]}
    verdict = {"gate": "G2", "passed": True, "blockers": [], "warnings": [], "tasks": []}
    assert rfl.g2_verdict(ctx, verdict) is None                  # the verdict is unchanged
    assert ctx.report["libraries"]["C9"]["footprint"] == "project"
    assert "libraries_error" not in ctx.report


# --- lint_kwargs ---------------------------------------------------------------------------

def test_lint_gets_the_project_library_context(world):
    _project(world)
    world.use_features("libs")
    world.run()
    (kw,) = world.lint_kw
    ctx = kw["ctx"]
    assert set(kw) == {"ctx"} and isinstance(ctx, LintContext)
    assert ctx.resolver is not None and ctx.library_error == ""
    assert ctx.netlist_dir == world.dir.resolve()             # outline / import lint read it
    assert ctx.symbols == ""                                   # no stock symbols: W2 skipped
    assert ctx.swig_fp_version_max == fp_dialect.pcbnew_footprint_limit("9.0.4")


def test_lint_reads_symbols_through_the_resolver_when_stock_symbols_exist(world, stock):
    world.use_features("libs")
    world.run()
    ctx = world.lint_kw[0]["ctx"]
    assert ctx.symbols is ctx.resolver and ctx.symbols_dir == str(stock / "symbols")


def test_lint_kwargs_loads_the_libraries_when_on_load_did_not_run(world):
    _project(world)
    ctx = rp_context.RunContext(netlist=str(world.netlist), w=30, h=30)
    ctx.design = json.loads(world.netlist.read_text(encoding="utf-8"))
    lint_ctx = rfl.lint_kwargs(ctx)["ctx"]
    assert lint_ctx.resolver is ctx.ext["libs"].resolver       # the state the later hooks use
    assert lint_ctx.resolver.resolve("footprint", NICK).source == "netlist"


def test_lint_judges_the_libraries_of_the_design_g1_prepare_settled_on(world):
    """A later feature's g1_prepare declares the library (after on_load ran):
    lint, the G1 check and the design review see it."""
    design = _project(world)
    world.write_design({k: v for k, v in design.items() if k != "libraries"})

    def declare(ctx: rp_context.RunContext) -> None:
        ctx.design = {**ctx.design, "libraries": design["libraries"]}
    world.use_features("libs", pipeline_world.recording_feature("d7", 50, g1_prepare=declare))
    rep = world.run()
    resolver = world.lint_kw[0]["ctx"].resolver
    assert resolver.resolve("footprint", NICK).source == "netlist"
    assert rep["PASS"] is True and rep["libraries"]["R1"]["footprint"] == "project"


# --- g1_verdict: a library error stops the run at G1 -----------------------------------------

def test_a_missing_declared_library_stops_the_run_at_g1_naming_nick_and_path(world):
    _project(world)
    shutil.rmtree(world.dir / "libs")
    world.use_features("libs")
    rep = world.run()
    g1 = rep["gates"][-1]
    assert g1["gate"] == "G1" and g1["passed"] is False and rep["PASS"] is False
    (blocker,) = g1["blockers"]
    missing = world.dir.resolve() / "libs" / f"{NICK}.pretty"
    assert blocker == (f"{rfl.BLOCKER_PREFIX}footprint library '{NICK}' (netlist libraries "
                       f"block): {missing} does not exist")
    assert "schematic" not in world.names()                   # nothing after G1 ran


def test_a_malformed_libraries_block_stops_the_run_at_g1(world):
    world.write_design({**DESIGN, "libraries": {"footprints": "libs/x.pretty"}})
    world.use_features("libs")
    rep = world.run()
    g1 = rep["gates"][-1]
    assert g1["gate"] == "G1" and g1["passed"] is False
    assert g1["blockers"] == [rfl.BLOCKER_PREFIX + "libraries.footprints must be an object "
                                                   "mapping nickname -> path, got str"]
    assert "schematic" not in world.names()


def test_a_library_error_lint_already_reported_is_not_repeated(world):
    design = _project(world)
    shutil.rmtree(world.dir / "libs")
    (message,) = project_libs.load(world.netlist, design).resolver.errors()
    world.lint = [{"ok": False, "errors": [f"LIB-E2 {message}"], "warnings": []}]
    world.use_features("libs")
    rep = world.run()
    assert _gate(rep, "G1")["blockers"] == [f"LIB-E2 {message}"]


def test_g1_verdict_leaves_a_clean_verdict_alone(world, use):
    _project(world)
    use("libs")
    ctx = _loaded_ctx(world)
    verdict = {"gate": "G1", "passed": True, "blockers": [], "warnings": ["w"], "tasks": []}
    assert rfl.g1_verdict(ctx, verdict) is None


def test_with_the_libraries_loaded_the_run_passes(world):
    _project(world)
    world.use_features("libs")
    rep = world.run()
    assert rep["PASS"] is True and [g["gate"] for g in rep["gates"]] == \
        ["G1", "G2", "G3", "G4", "G5"]


# --- analysis_kwargs ---------------------------------------------------------------------------

def test_the_design_review_gets_the_resolver_when_stock_symbols_exist(world, stock):
    _project(world)
    world.use_features("libs")
    world.run()
    (kw,) = world.analyze_kw
    assert set(kw) == {"symbols"} and is_resolver(kw["symbols"])
    assert kw["symbols"].resolve("footprint", NICK).source == "netlist"


def test_the_design_review_keeps_the_stock_dir_without_stock_symbols(world):
    _project(world)
    world.use_features("libs")
    world.run()
    assert world.analyze_kw == [{}]


def test_analysis_kwargs_is_empty_when_the_libraries_did_not_load(world, use, stock):
    world.write_design({**DESIGN, "libraries": "junk"})
    use("libs")
    ctx = _loaded_ctx(world)
    assert ctx.ext["libs"].resolver is None and rfl.analysis_kwargs(ctx) == {}


# --- route_hash_extra and the route cache ----------------------------------------------------

def test_route_hash_extra_is_the_footprint_digest(world):
    design = _project(world)
    extra = rfl.route_hash_extra(design, world.dir)
    want = project_libs.footprint_digest(project_libs.load(world.netlist, design), design)
    assert extra == {"footprint_digest": want} and HEX64.match(want)
    assert rfl.route_hash_extra(copy.deepcopy(design), world.dir) == extra     # stable


def test_route_hash_extra_without_a_netlist_dir_is_empty(world):
    assert rfl.route_hash_extra(_project(world), None) == {}


def test_changing_a_project_kicad_mod_byte_changes_the_route_hash(world, use):
    design = _project(world)
    footprint = world.dir / "libs" / f"{NICK}.pretty" / f"{FP_NAME}.kicad_mod"
    use()
    core = rp_route_hash.route_hash(design, world.dir)
    use("libs")
    first = rp_route_hash.route_hash(design, world.dir)
    original = _flip_byte(footprint)
    assert rp_route_hash.route_hash(design, world.dir) != first
    use()
    assert rp_route_hash.route_hash(design, world.dir) == core   # the core hash cannot see it
    footprint.write_bytes(original)
    use("libs")
    assert rp_route_hash.route_hash(design, world.dir) == first


def test_a_stock_footprint_byte_also_moves_the_route_hash(world, use, stock):
    use("libs")
    design = copy.deepcopy(DESIGN)
    first = rp_route_hash.route_hash(design, world.dir)
    _flip_byte(stock / "footprints" / f"{STOCK_LIB}.pretty" / f"{STOCK_FP}.kicad_mod")
    assert rp_route_hash.route_hash(design, world.dir) != first


def test_the_libraries_block_itself_is_not_hashed(world, use):
    design = _project(world)
    use("libs")
    reg = rp_route_hash.registry()
    assert "libraries" in reg.excluded and "libraries" not in reg.design_keys
    first = rp_route_hash.route_hash(design, world.dir)
    _library(world.dir, f"copy/{NICK}.pretty")                  # an identical copy elsewhere
    moved = {**design, "libraries": {"footprints": {NICK: f"copy/{NICK}.pretty"}}}
    assert rp_route_hash.route_hash(moved, world.dir) == first


@pytest.mark.parametrize("netlist", EXAMPLE_NETLISTS, ids=lambda p: p.parent.name)
def test_every_example_hashes_with_the_feature(use, netlist):
    design = json.loads(netlist.read_text(encoding="utf-8"))
    use("libs")
    extra = rfl.route_hash_extra(design, netlist.parent)
    assert HEX64.match(extra["footprint_digest"])
    assert HEX64.match(rp_route_hash.route_hash(design, netlist.parent))


def test_the_route_cache_hits_until_a_project_footprint_changes(world):
    _project(world)
    footprint = world.dir / "libs" / f"{NICK}.pretty" / f"{FP_NAME}.kicad_mod"
    world.use_features("libs")
    first, second = world.run(), world.run()
    assert (first["route_cache"], second["route_cache"]) == ("miss", "hit")
    _flip_byte(footprint)
    third = world.run()
    assert third["route_cache"] == "miss" and third["route_cache_reason"] == DRIFT
    assert third["PASS"] is True and len(world.args_of("build")) == 2


# --- parity: a stock-only design ------------------------------------------------------------

def _verdicts(rep: dict) -> list[tuple]:
    return [(g["gate"], g["passed"], g["blockers"]) for g in rep["gates"]]


def test_a_stock_only_design_gets_the_same_verdicts_with_and_without_the_feature(
        world, monkeypatch, stock):
    monkeypatch.setenv("PIPELINE_NO_CACHE", "1")
    without = world.run()
    world.use_features("libs")
    with_libs = world.run()
    assert _verdicts(with_libs) == _verdicts(without) and with_libs["PASS"] is True
    assert set(with_libs) - set(without) == {"libraries"}
