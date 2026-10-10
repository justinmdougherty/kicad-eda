"""run_pipeline.run as a sequence of stages sharing a RunContext.

Every KiCad-facing call the harness makes is replaced by the scripted
stand-ins of tests/fixtures/pipeline_world.py (World), so these tests drive
the real stage logic -- gate sequencing, the G2 / G3 / G4 corrective-action
loops, the route cache, the persisted report -- with no KiCad installed. The
board "file" the fakes write is a header-only stub whose generator_version
decides what toolchain.swig_readable says about it.
"""
from __future__ import annotations

import ast
import copy
import json
from pathlib import Path

import pytest

import remediate
import rp_context
import rp_route
import rp_route_hash
import rp_stage_place
import rp_stage_schematic
import run_pipeline as rp
from fixtures import pipeline_world
from fixtures.pipeline_world import (CLEAN_DRC, DESIGN, K9_HEADER, K11_HEADER, OK_ANALYSIS,
                                     OK_BUILD, World)

world = pipeline_world.world        # the fixture (an assignment: see pipeline_world)

PIPELINE = Path(rp.__file__).resolve().parent
# every harness module: run_pipeline.py plus the pipeline/rp_*.py it is split into
HARNESS_FILES = sorted([PIPELINE / "run_pipeline.py", *PIPELINE.glob("rp_*.py")])
# the helper modules a stage reaches only through its module object
HELPER_MODULES = {"kicad_server", "toolchain", "lint_netlist", "design_report", "gates",
                  "remediate", "thermal_check"}


def _run(world: World, placement: str = "") -> dict:
    """A strict Freerouting-only run (World.run pins napkin=False and
    route='freerouting'): these tests prove today's behaviour, a G4 failure
    included (the route modes have tests/test_run_pipeline_route_modes.py)."""
    return world.run(placement=placement)


def _persisted(world: World) -> dict:
    return json.loads((world.dir / "netlist_pipeline_report.json").read_text(encoding="utf-8"))


def _gate(rep: dict, name: str) -> dict:
    return next(g for g in rep["gates"] if g["gate"] == name)


# --- structure ------------------------------------------------------------------

def test_run_delegates_to_the_stage_table_and_stops_at_the_first_failure(world, monkeypatch):
    seen: list[tuple[str, str]] = []

    def stage(name: str, passed: bool):
        def fn(ctx: rp_context.RunContext) -> bool:
            seen.append((name, type(ctx).__name__))
            ctx.report["gates"].append({"gate": name, "name": name, "feeds": "-",
                                        "passed": passed, "blockers": [], "warnings": []})
            return passed
        return fn

    monkeypatch.setattr(rp, "STAGES", (stage("A", True), stage("B", False), stage("C", True)))
    rep = _run(world)
    assert seen == [("A", "RunContext"), ("B", "RunContext")]
    assert rep["PASS"] is False and [g["gate"] for g in rep["gates"]] == ["A", "B"]


def test_stage_table_is_the_gate_order():
    assert [s.__name__ for s in rp.STAGES] == [
        "stage_g1", "stage_g2", "stage_route_cache", "stage_place_route",
        "stage_g4", "stage_cache_manifest", "stage_g5"]
    assert [s.__module__ for s in rp.STAGES] == [
        "rp_stage_schematic", "rp_stage_schematic", "rp_stage_place", "rp_stage_place",
        "rp_stage_layout", "rp_stage_fab", "rp_stage_fab"]


def _functions(path: Path) -> list[tuple[str, int]]:
    """(name, source lines incl. decorators) of every def in `path`, methods
    and nested functions included."""
    out = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            first = min([node.lineno, *(d.lineno for d in node.decorator_list)])
            out.append((node.name, node.end_lineno - first + 1))
    return out


def test_no_function_in_the_harness_is_longer_than_80_lines():
    assert {"run_pipeline", "rp_paths", "rp_cli", "rp_context", "rp_kicad", "rp_route_hash",
            "rp_route", "rp_route_modes", "rp_stage_schematic", "rp_stage_place",
            "rp_stage_layout", "rp_stage_fab", "rp_napkin"} <= {p.stem for p in HARNESS_FILES}
    long = {f"{path.stem}.{name}": n for path in HARNESS_FILES
            for name, n in _functions(path) if n > 80}
    assert long == {}


def test_every_harness_module_stays_under_800_lines():
    sizes = {path.name: len(path.read_text(encoding="utf-8").splitlines())
             for path in HARNESS_FILES}
    assert {name: n for name, n in sizes.items() if n >= 800} == {}


def _helper_from_imports(path: Path) -> list[str]:
    """'from <helper> import ...' statements (any level), plus every from-import
    inside a function body, in one harness module."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    bad = [f"line {n.lineno}: from {n.module} import ..." for n in ast.walk(tree)
           if isinstance(n, ast.ImportFrom) and n.module
           and (n.module.startswith("rp_") or n.module in HELPER_MODULES)]
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            bad += [f"line {n.lineno}: from {n.module} import ... inside {fn.name}()"
                    for n in ast.walk(fn) if isinstance(n, ast.ImportFrom)]
    return bad


def test_stage_modules_call_helpers_through_module_objects():
    """The run_pipeline docstring rule: rp_kicad.drc(...), never 'from rp_kicad
    import drc' -- so one monkeypatch on the module object reaches every caller."""
    stage_files = [p for p in HARNESS_FILES if p.stem.startswith("rp_stage_")
                   or p.stem in ("rp_route", "rp_route_modes", "rp_napkin", "run_pipeline")]
    assert len(stage_files) >= 8
    assert {p.name: _helper_from_imports(p) for p in stage_files
            if _helper_from_imports(p)} == {}


def test_run_context_derives_every_path_from_the_netlist(tmp_path):
    ctx = rp_context.RunContext(netlist=str(tmp_path / "board.json"), w=30, h=20)
    assert ctx.base == str(tmp_path / "board")
    assert ctx.sch == str(tmp_path / "board.kicad_sch")
    assert ctx.pcb == str(tmp_path / "board.kicad_pcb")
    assert ctx.cache_path == tmp_path / "board.cache.json"
    assert ctx.report_path == tmp_path / "board_pipeline_report.json"
    assert ctx.report == {"netlist": str(tmp_path / "board.json"), "gates": []}


def test_log_remediation_keeps_one_log_in_the_report(tmp_path):
    ctx = rp_context.RunContext(netlist=str(tmp_path / "n.json"), w=1, h=1)
    assert "remediation" not in ctx.report
    ctx.log_remediation("G2", 1, ["added C9"])
    ctx.log_remediation("G3", 2, ["re-placed"])
    assert ctx.report["remediation"] == ctx.remediation == [
        {"gate": "G2", "round": 1, "applied": ["added C9"]},
        {"gate": "G3", "round": 2, "applied": ["re-placed"]}]


def test_g2_review_keeps_the_design_analysis_on_the_context(world):
    """ctx.analysis holds design_report.analyze()'s result (the review report's
    input); the JSON report keeps only its summary, as before the split."""
    ctx = rp_context.RunContext(netlist=str(world.netlist), w=30, h=30)
    assert ctx.analysis == {}
    rp_stage_schematic.stage_load(ctx)
    verdict = rp_stage_schematic.g2_review(ctx)
    assert verdict["passed"] is True
    assert ctx.analysis == OK_ANALYSIS
    assert ctx.report["design_report"] == {"decoupling_uncovered": [], "flags": {}, "errors": []}
    assert "analysis" not in ctx.report


def test_world_fake_route_records_keyword_arguments(world):
    res = world.fake_route(str(world.pcb), 5, "", 9, dsn_seed=3, from_snapshot=True)
    assert res == {"ok": True, "tracks": 7, "unconnected": 0}
    assert world.args_of("route") == [(5, 9)]
    assert world.route_kw == [{"dsn_seed": 3, "from_snapshot": True}]


# --- the persisted report (every exit) -----------------------------------------

def test_pass_runs_every_gate_and_persists_the_report(world):
    rep = _run(world)
    assert rep["PASS"] is True
    assert [g["gate"] for g in rep["gates"]] == ["G1", "G2", "G3", "G4", "G5"]
    assert rep["pipeline_report"] == str(world.dir / "netlist_pipeline_report.json")
    assert _persisted(world) == json.loads(json.dumps(rep))
    assert "[PASS] G5" in _persisted(world)["gate_summary"]


def test_forced_g1_failure_still_persists_a_failing_report(world):
    world.lint = [{"ok": False, "errors": ["E3 single-pin net N9"], "warnings": []}]
    rep = _run(world)
    assert rep["PASS"] is False and [g["gate"] for g in rep["gates"]] == ["G1"]
    assert world.names() == ["lint"]                        # halted: nothing downstream ran
    saved = _persisted(world)
    assert saved["PASS"] is False
    assert saved["gates"][0]["blockers"] == ["E3 single-pin net N9"]
    assert "[FAIL] G1" in saved["gate_summary"]


def test_a_crash_mid_run_persists_a_failing_report_and_reraises(world):
    world.routes = [RuntimeError("kicad-cli not found")]
    with pytest.raises(RuntimeError, match="kicad-cli not found"):
        _run(world)
    saved = _persisted(world)
    assert saved["PASS"] is False
    assert saved["error"] == "RuntimeError: kicad-cli not found"
    assert [g["gate"] for g in saved["gates"]] == ["G1", "G2"]


def test_an_unreadable_netlist_persists_a_failing_report(world):
    world.netlist.write_text("{ not json", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        _run(world)
    saved = _persisted(world)
    assert saved["PASS"] is False and saved["gates"] == []
    assert saved["error"].startswith("JSONDecodeError")


def test_a_report_that_cannot_be_written_is_flagged_not_hidden(world, monkeypatch, capsys):
    monkeypatch.setattr(rp_context, "REPORT_SUFFIX", "/no/such/dir/r.json")   # parent dir missing
    rep = _run(world)
    assert rep["PASS"] is True
    assert "pipeline_report" not in rep
    assert "r.json" in rep["pipeline_report_error"]
    assert "could not write" in capsys.readouterr().err


# --- G2 corrective-action loop ---------------------------------------------------

def test_g2_remediation_rewrites_the_netlist_and_retests(world, monkeypatch):
    missing = {"decoupling": {"uncovered": [{"ref": "R1", "rails_without_cap": ["+5V"]}]},
               "flags": {}, "errors": []}
    world.analyses = [missing, OK_ANALYSIS]

    def fake_apply(design: dict, tasks: list) -> tuple[dict, list]:
        fixed = copy.deepcopy(design)
        fixed["components"].append({"ref": "C9", "footprint": "Capacitor_SMD:C_0402"})
        return fixed, [f"added C9 for {tasks[0]['fix']['rail']}"]

    monkeypatch.setattr(remediate, "apply", fake_apply)
    rep = _run(world)
    assert _gate(rep, "G2")["passed"] and rep["PASS"]
    assert rep["remediation"][0] == {"gate": "G2", "round": 1, "applied": ["added C9 for +5V"]}
    on_disk = json.loads(world.netlist.read_text(encoding="utf-8"))
    assert [c["ref"] for c in on_disk["components"]] == ["R1", "R2", "C9"]
    assert world.args_of("analyze") == [(2,), (3,)]          # the re-review saw the fixed design


def test_g2_stops_when_only_designer_tasks_remain(world):
    world.schematics = [{"ok": True, "erc_violations": 3}]
    rep = _run(world)
    assert rep["PASS"] is False and [g["gate"] for g in rep["gates"]] == ["G1", "G2"]
    assert world.names().count("schematic") == 1
    assert "build" not in world.names()
    assert "remediation" not in rep


# --- G3 / routing scale loop -----------------------------------------------------

def test_courtyard_collision_escalates_pack_scale_and_retests(world):
    world.drcs = [{**CLEAN_DRC, "types": {"courtyards_overlap": 2}}, CLEAN_DRC]
    rep = _run(world)
    assert rep["PASS"] is True
    assert [a[3] for a in world.args_of("build")] == [1.0, 1.5]
    assert rep["remediation"] == [{"gate": "G3", "round": 1,
                                   "applied": ["re-placed at pack_scale 1.5"]}]
    assert rep["autoroute"] == {"attempt": 2, "pack_scale": 1.5, "tracks": 7, "unconnected": 0}


def test_manual_g3_blocker_stops_without_routing(world):
    world.gate_checks["placement"] = [{"issues": ["J1 mating face points inward"],
                                       "warnings": []}]
    rep = _run(world)
    assert rep["PASS"] is False and _gate(rep, "G3")["blockers"] == ["J1 mating face points inward"]
    assert "route" not in world.names() and len(world.args_of("build")) == 1


def test_missing_mounting_holes_are_a_g3_blocker(world):
    design = copy.deepcopy(DESIGN)
    design["mounting_holes"] = {"positions": [[3, 3]]}
    world.write_design(design)
    rep = _run(world)
    assert "mounting holes requested but none placed on the board" in _gate(rep, "G3")["blockers"]


def test_ep_parts_get_a_via_farm_before_the_placement_review(world):
    world.thermal = [{"blockers": [], "warnings": [], "ep_via_parts": ["U1"]}]
    world.fixes["ep_vias"] = [["U1: 9 thermal vias"]]
    rep = _run(world)
    assert rep["remediation"][0] == {"gate": "G3", "round": 1, "applied": ["U1: 9 thermal vias"]}
    names = world.names()
    assert names.index("board_fix") < names.index("gate_check")


def test_placement_override_routes_once_at_scale_one(world):
    world.drcs = [{**CLEAN_DRC, "types": {"courtyards_overlap": 1}}]
    rep = _run(world, placement="place.json")
    assert world.args_of("build") == [(30, 30, "place.json", 1.0)]
    assert rep["route_cache_reason"] == "a placement override was given"
    assert _gate(rep, "G3")["passed"] is False


def test_router_failure_surfaces_at_g4_without_escalating(world):
    world.routes = [{"ok": False, "error": "java not found"}]
    rep = _run(world)
    assert len(world.args_of("build")) == 1
    assert rep["autoroute"]["error"] == "java not found"
    assert _gate(rep, "G4")["blockers"][0] == "autoroute failed: java not found"
    assert world.args_of("route") == [
        (rp_route.AUTOROUTE_MAX_PASSES, rp_route.AUTOROUTE_TIMEOUT_S)]


def test_near_miss_is_healed_in_place(world):
    """A near miss on the same placement: the alternate DSN orders first (each
    logged), then the verified heal; no re-placement."""
    world.routes = [{"tracks": 20, "unconnected": 2}]
    world.drcs = [CLEAN_DRC, {**CLEAN_DRC, "unconnected": 2}, CLEAN_DRC]
    world.fixes["heal"] = [["bridged 0.4 mm gap on N1"]]
    rep = _run(world)
    assert rep["PASS"] is True and len(world.args_of("build")) == 1
    assert world.route_seeds() == [0, *rp_route.ALT_DSN_SEEDS]
    assert rep["autoroute"]["unconnected"] == 0
    alternate = ("near miss: 2 unrouted with the canonical DSN order -> re-routed the placed "
                 "board with alternate DSN order dsn_seed {}: 2 unrouted")
    assert rep["remediation"] == [
        {"gate": "G4", "round": 1, "applied": [alternate.format(1)]},
        {"gate": "G4", "round": 1, "applied": [alternate.format(2)]},
        {"gate": "G4", "round": 1, "applied": ["bridged 0.4 mm gap on N1"]}]
    snap = Path(str(world.pcb) + ".attempt").read_text(encoding="utf-8")
    assert snap == world.pcb.read_text(encoding="utf-8")


def test_retry_that_no_longer_fits_restores_the_routed_snapshot(world):
    world.routes = [{"tracks": 15, "unconnected": 9}]
    world.builds = [dict(OK_BUILD), {"ok": False, "error": "auto cluster does not fit"}]
    world.drcs = [CLEAN_DRC, {**CLEAN_DRC, "unconnected": 9}]
    rep = _run(world)
    assert world.pcb.read_text(encoding="utf-8") == f"{K9_HEADER}(built 1.0)\n(routed)\n"
    assert rep["remediation"] == [
        {"gate": "G4", "round": 1,
         "applied": ["9 unrouted -> re-placed at pack_scale 1.5 and re-routed"]},
        {"gate": "G3", "round": 2,
         "applied": ["pack_scale 1.5 does not fit the fixed board -> restored routed "
                     "board at pack_scale 1.0"]}]
    assert rep["board"]["ok"] is True and _gate(rep, "G3")["passed"]
    assert rep["autoroute"]["pack_scale"] == 1.0
    assert _gate(rep, "G4")["passed"] is False


def test_unrouted_on_the_last_scale_is_judged_at_g4(world):
    world.routes = [{"tracks": 15, "unconnected": 9}]
    world.drcs = [CLEAN_DRC] * 5 + [{**CLEAN_DRC, "unconnected": 9}]
    rep = _run(world)
    assert [a[3] for a in world.args_of("build")] == list(rp_stage_place.PACK_SCALES)
    assert len(rep["remediation"]) == len(rp_stage_place.PACK_SCALES) - 1
    assert "9 net(s) unrouted" in _gate(rep, "G4")["blockers"]


# --- G4 fix loop + EMC -------------------------------------------------------------

def test_g4_fix_loop_repairs_and_retests(world):
    world.drcs = [CLEAN_DRC, {**CLEAN_DRC, "errors": 2, "types": {"via_dangling": 2}},
                  CLEAN_DRC]
    world.fixes["all"] = [["P5V: 2 parallel vias at layer transition"]]
    rep = _run(world)
    assert rep["PASS"] is True
    assert rep["remediation"] == [{"gate": "G4", "round": 1,
                                   "applied": ["P5V: 2 parallel vias at layer transition"]}]
    assert rep["autoroute"]["unconnected"] == 0 and rep["drc"]["errors"] == 0


def test_g4_fix_loop_stops_when_only_open_items_come_back(world):
    world.drcs = [CLEAN_DRC, {**CLEAN_DRC, "errors": 1, "types": {"clearance": 1}}]
    world.fixes["all"] = [["OPEN: clearance at (10, 4) needs a re-route"]]
    rep = _run(world)
    assert rep["PASS"] is False and "remediation" not in rep
    assert world.args_of("board_fix") == [("all",)]


def test_emc_stitching_runs_on_a_passing_board_and_is_rejudged(world):
    design = copy.deepcopy(DESIGN)
    design["emc"] = {"stitch": True, "max_freq_hz": 1e9, "teardrops": True}
    world.write_design(design)
    rep = _run(world)
    assert rep["emc"]["vias_added"] == 12
    warnings = _gate(rep, "G4")["warnings"]
    assert warnings[0] == "EMC stitching: 12 GND vias (fence 8, interior 4, pitch 3.0 mm)"
    assert "teardrops requested" in warnings[1]
    assert world.names().count("drc") == 3                    # courtyard, G4, post-stitch


def test_emc_failure_is_a_warning_and_skips_the_redrc(world):
    design = copy.deepcopy(DESIGN)
    design["emc"] = {"stitch": True}
    world.write_design(design)
    world.emc = [{"ok": False, "reason": "no GND net"}]
    rep = _run(world)
    assert "EMC stitching FAILED: no GND net" in _gate(rep, "G4")["warnings"]
    assert world.names().count("drc") == 2


# --- route cache --------------------------------------------------------------------

def test_second_run_reuses_the_routed_board_and_still_judges_it(world):
    first = _run(world)
    manifest = json.loads((world.dir / "netlist.cache.json").read_text(encoding="utf-8"))
    assert manifest["route_hash"] == rp_route_hash.route_hash(DESIGN)
    assert manifest["routed_ok"] is True
    world.calls.clear()
    second = _run(world)
    assert first["route_cache"] == "miss" and second["route_cache"] == "hit"
    assert "route_cache_reason" not in second
    assert "build" not in world.names() and "route" not in world.names()
    assert ("gate_check", ("placement_routed",)) in world.calls
    assert second["autoroute"] == {"cached": True, "pack_scale": 1.0, "tracks": 7,
                                   "unconnected": 0}
    assert second["PASS"] is True


def test_emc_is_not_restitched_on_a_cached_board(world):
    design = copy.deepcopy(DESIGN)
    design["emc"] = {"stitch": True}
    world.write_design(design)
    _run(world)
    world.calls.clear()
    _run(world)
    assert "emc" not in world.names()


def _context_with_manifest(world: World, header: str) -> rp_context.RunContext:
    ctx = rp_context.RunContext(netlist=str(world.netlist), w=30, h=30)
    rp_stage_schematic.stage_load(ctx)
    world.pcb.write_text(header + "(routed)\n", encoding="utf-8")
    (world.dir / "netlist.cache.json").write_text(json.dumps(
        {"route_hash": rp_route_hash.route_hash(ctx.design), "routed_ok": True, "unconnected": 0,
         "pack_scale": 1.0, "tracks": 7, "board_rep": {"ok": True}}), encoding="utf-8")
    return ctx


def test_route_cache_uses_a_board_pcbnew_can_read(world):
    ctx = _context_with_manifest(world, K9_HEADER)
    assert rp_stage_place.stage_route_cache(ctx) is True
    assert ctx.use_cache is True and ctx.report["route_cache"] == "hit"


def test_route_cache_refuses_a_board_saved_by_kicad_11(world):
    ctx = _context_with_manifest(world, K11_HEADER)
    assert rp_stage_place.stage_route_cache(ctx) is True
    assert ctx.use_cache is False and ctx.report["route_cache"] == "miss"
    reason = ctx.report["route_cache_reason"]
    assert reason.startswith("the cached board is not readable by the SWIG gate scripts: ")
    assert "saved by KiCad 10.99 (format 20260901)" in reason


def test_a_kicad_11_board_is_preserved_then_rebuilt_never_fed_to_swig(world):
    _context_with_manifest(world, K11_HEADER)
    (world.dir / "netlist.kicad_pro").write_text('{"agent": true}', encoding="utf-8")
    rep = _run(world)
    kept = world.dir / "netlist.agent.kicad_pcb"
    assert rep["agent_board_preserved"] == str(kept)
    assert kept.read_text(encoding="utf-8") == K11_HEADER + "(routed)\n"
    assert (world.dir / "netlist.agent.kicad_pro").read_text(encoding="utf-8") == '{"agent": true}'
    assert ("gate_check", ("placement_routed",)) not in world.calls
    assert len(world.args_of("build")) == 1 and rep["PASS"] is True
    assert world.pcb.read_text(encoding="utf-8").startswith(K9_HEADER)


@pytest.mark.parametrize("setup, reason", [
    (lambda w, ctx: ctx.cache_path.unlink(), "no cache manifest from a routed run"),
    (lambda w, ctx: ctx.cache_path.write_text("[1, 2]", encoding="utf-8"),
     "no cache manifest from a routed run"),
    (lambda w, ctx: ctx.design.__setitem__("planes", {"In2.Cu": "N1"}),
     "placement/routing inputs changed since the cached run"),
    (lambda w, ctx: ctx.cache_path.write_text(json.dumps(
        {"route_hash": rp_route_hash.route_hash(ctx.design), "routed_ok": False}),
        encoding="utf-8"),
     "the cached run did not finish routing"),
    (lambda w, ctx: w.pcb.unlink(), "the cached board file is missing"),
], ids=["no-manifest", "junk-manifest", "hash-changed", "not-routed", "no-board"])
def test_route_cache_miss_reasons(world, setup, reason):
    ctx = _context_with_manifest(world, K9_HEADER)
    setup(world, ctx)
    rp_stage_place.stage_route_cache(ctx)
    assert ctx.use_cache is False and ctx.report["route_cache_reason"] == reason


def test_pipeline_no_cache_env_bypasses_the_cache(world, monkeypatch):
    monkeypatch.setenv("PIPELINE_NO_CACHE", "1")          # read when the context is made
    ctx = _context_with_manifest(world, K9_HEADER)
    rp_stage_place.stage_route_cache(ctx)
    assert ctx.use_cache is False and ctx.report["route_cache_reason"] == "PIPELINE_NO_CACHE is set"


def test_failed_g4_writes_no_cache_manifest(world):
    world.drcs = [CLEAN_DRC, {**CLEAN_DRC, "errors": 1, "types": {"clearance": 1}}]
    _run(world)
    assert not (world.dir / "netlist.cache.json").exists()


@pytest.mark.parametrize("setup", [
    lambda w, ctx: ctx.cache_path.write_text("[1, 2]", encoding="utf-8"),
    lambda w, ctx: ctx.design.__setitem__("planes", {"In2.Cu": "N1"}),
    lambda w, ctx: ctx.cache_path.write_text(json.dumps(
        {"route_hash": rp_route_hash.route_hash(ctx.design), "routed_ok": False}),
        encoding="utf-8"),
    lambda w, ctx: w.pcb.unlink(),
    lambda w, ctx: w.pcb.write_text(K11_HEADER + "(routed)\n", encoding="utf-8"),
    lambda w, ctx: setattr(ctx, "no_cache", True),
    lambda w, ctx: setattr(ctx, "placement", "placement.json"),
], ids=["junk-manifest", "hash-changed", "not-routed", "no-board", "kicad-11-board",
        "no-cache", "placement-override"])
def test_a_route_cache_miss_removes_the_stale_manifest(world, setup):
    """A miss re-places the board, so the manifest of an earlier run no longer
    describes the board on disk: it is removed (a passing run writes a new
    one), so a later run can never reuse a board this run left unrouted."""
    ctx = _context_with_manifest(world, K9_HEADER)
    setup(world, ctx)
    rp_stage_place.stage_route_cache(ctx)
    assert ctx.use_cache is False and not ctx.cache_path.exists()


def test_a_route_cache_hit_keeps_the_manifest(world):
    ctx = _context_with_manifest(world, K9_HEADER)
    before = ctx.cache_path.read_bytes()
    rp_stage_place.stage_route_cache(ctx)
    assert ctx.use_cache is True and ctx.cache_path.read_bytes() == before


def test_a_stale_manifest_that_cannot_be_removed_is_named_in_the_miss_reason(world,
                                                                             monkeypatch):
    ctx = _context_with_manifest(world, K9_HEADER)
    ctx.no_cache = True
    real_unlink = Path.unlink

    def unlink(self: Path, missing_ok: bool = False) -> None:
        if self == ctx.cache_path:
            raise PermissionError("locked by another process")
        real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", unlink)
    assert rp_stage_place.stage_route_cache(ctx) is True
    assert ctx.report["route_cache_reason"] == (
        "PIPELINE_NO_CACHE is set; the stale cache manifest could not be removed "
        "(PermissionError: locked by another process)")
    assert ctx.cache_path.is_file()


def test_a_reverted_netlist_never_reuses_the_board_a_failed_run_left(world):
    """Run 1 passes; run 2 (the netlist changed) re-places and fails G4; run
    3 (the netlist reverted) has run 1's route hash but must not reuse run 2's
    board as run 1's: run 2's miss removed run 1's manifest."""
    _run(world)
    changed = copy.deepcopy(DESIGN)
    changed["planes"] = {"In2.Cu": "N1"}
    world.write_design(changed)
    world.drcs = [CLEAN_DRC, {**CLEAN_DRC, "errors": 1, "types": {"clearance": 1}}]
    assert _run(world)["PASS"] is False
    assert not (world.dir / "netlist.cache.json").exists()
    world.write_design(copy.deepcopy(DESIGN))
    world.drcs = [CLEAN_DRC]
    world.calls.clear()
    rep = _run(world)
    assert rep["route_cache"] == "miss" and len(world.args_of("build")) == 1
    assert rep["PASS"] is True


# --- G5 ------------------------------------------------------------------------------

def test_g5_records_fab_renders_and_sourcing(world):
    rep = _run(world)
    assert rep["fab"] == {"ok": True, "zip": "netlist_JLCPCB.zip", "files": 12,
                          "bom": "BOM.csv", "cpl": "CPL.csv"}
    assert rep["renders_final"] == ["final_top.png"]
    assert rep["sourcing"]["missing"] == ["R1", "R2"]
    assert any("neither an LCSC number nor an MPN" in w for w in _gate(rep, "G5")["warnings"])
