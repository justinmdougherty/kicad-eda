"""Route modes of the harness (F5-13a, ROADMAP D5): --route auto|freerouting|agent,
the routing handoff to the agent (<stem>_route_handoff.json, exit 3), the
handoff_veto feature hook, the route_provider rule (never a handoff) and the
HANDOFF verdict of the review.

Every KiCad-facing call is a scripted stand-in of tests/fixtures/pipeline_world.py
(World). World.run pins route='freerouting' (the stage tests prove that mode);
these tests pass the mode they mean.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

import decisions as dec
import gates
import rp_cli
import rp_context
import rp_kicad
import rp_route
import rp_route_hash
import rp_route_modes
import rp_stage_place
import run_pipeline as rp
import toolchain as tc
from fixtures import pipeline_world
from fixtures.pipeline_world import CLEAN_DRC, DESIGN, K9_HEADER, World, recording_feature

world = pipeline_world.world        # the fixture (an assignment: see pipeline_world)

UNROUTED = {"tracks": 15, "unconnected": 9}
JAR_ERROR = "freerouting jar not found in server/tools or $FREEROUTING_JAR"
VETO = "D7 layer-role checks need a KiCad 9/10 board"


def _gate(rep: dict, name: str) -> dict:
    return next(g for g in rep["gates"] if g["gate"] == name)


def _handoff(world: World) -> dict:
    return json.loads((world.dir / "netlist_route_handoff.json").read_text(encoding="utf-8"))


def _log(world: World) -> list[dict]:
    return dec.read_decisions(world.netlist)


def _routing_decisions(world: World) -> list[dict]:
    return [e for e in _log(world) if e["stage"] == "routing"]


def _feature(**hooks: Any) -> Any:
    return recording_feature("modes", **hooks)


def _router_failure(world: World) -> None:
    """Freerouting cannot start (the acceptance scenario: a jar it cannot use)."""
    world.routes = [{"ok": False, "reason": JAR_ERROR}]


# --- the command line and the exit codes --------------------------------------------

@pytest.mark.parametrize("argv, route", [
    (["n.json"], "auto"),
    (["n.json", "--route", "agent"], "agent"),
    (["--route", "freerouting", "n.json", "30", "40"], "freerouting"),
    (["n.json", "--route=auto", "--strict"], "auto"),
])
def test_parse_cli_route(argv, route):
    cli = rp_cli.parse_cli(argv)
    assert cli.route == route and cli.netlist == "n.json"


def test_parse_cli_route_keeps_the_positionals():
    assert rp_cli.parse_cli(["n.json", "30", "--route", "agent", "40", "p.json"]) == \
        rp_cli.CliArgs("n.json", 30.0, 40.0, "p.json", True, "agent")


@pytest.mark.parametrize("argv, message", [
    (["n.json", "--route"], "--route needs one of auto, freerouting, agent"),
    (["n.json", "--route", "fast"], "--route must be one of auto, freerouting, agent, got 'fast'"),
    (["n.json", "--route="], "--route must be one of auto, freerouting, agent, got ''"),
    (["n.json", "--route", "agent", "--route", "auto"], "--route given more than once"),
    (["n.json", "--fast"], "unknown option --fast"),
])
def test_parse_cli_route_errors(argv, message):
    with pytest.raises(rp_cli.UsageError) as err:
        rp_cli.parse_cli(argv)
    assert str(err.value).startswith(message)


def test_usage_names_the_route_modes():
    assert "--route auto|freerouting|agent" in rp_cli.USAGE
    assert rp_route_modes.ROUTE_MODES == ("auto", "freerouting", "agent")
    assert rp_route_modes.DEFAULT_ROUTE == "auto"


@pytest.mark.parametrize("report, code", [
    ({"PASS": True}, 0), ({"PASS": False}, 1),
    ({"PASS": False, "status": "handoff"}, 3)])
def test_exit_code_follows_the_report(report, code):
    assert rp_cli.exit_code(report) == code
    assert (rp_cli.EXIT_PASS, rp_cli.EXIT_FAIL, rp_cli.EXIT_USAGE, rp_cli.EXIT_HANDOFF) == \
        (0, 1, 2, 3)


@pytest.mark.parametrize("extra, kw", [
    ([], {}), (["--route", "auto"], {}), (["--route", "agent"], {"route": "agent"}),
    (["--strict", "--route", "freerouting"], {"napkin": False, "route": "freerouting"})])
def test_main_passes_route_only_when_it_is_not_the_default(tmp_path, monkeypatch, extra, kw):
    calls = []
    monkeypatch.setattr(rp, "run", lambda *a, **k: calls.append((a, k)) or {
        "PASS": False, "status": "handoff", "gates": [], "gate_summary": ""})
    netlist = str(tmp_path / "n.json")
    assert rp.main([netlist, "30", "30", *extra]) == 3
    assert calls == [((netlist, 30.0, 30.0, ""), kw)]


def test_main_bad_route_is_a_usage_error(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(rp, "run", lambda *a, **k: pytest.fail("run() must not start"))
    assert rp.main([str(tmp_path / "n.json"), "--route", "fast"]) == 2
    assert "--route must be one of" in capsys.readouterr().err


@pytest.mark.parametrize("route, code", [("auto", 3), ("freerouting", 1), ("agent", 3)])
def test_main_exit_codes_end_to_end(world, monkeypatch, route, code):
    """main() on the scripted world: Freerouting cannot run, so auto hands off,
    freerouting fails and agent hands off without trying it."""
    _router_failure(world)
    assert rp.main([str(world.netlist), "30", "30", "--strict", "--route", route]) == code
    saved = json.loads((world.dir / "netlist_pipeline_report.json").read_text(encoding="utf-8"))
    assert saved["PASS"] is False
    assert (saved.get("status") == "handoff") is (code == 3)


def test_run_refuses_an_unknown_route(world):
    with pytest.raises(ValueError, match="route must be one of"):
        world.run(route="fast")


def test_world_run_pins_freerouting():
    assert pipeline_world.RUN_DEFAULTS["route"] == "freerouting"


# --- freerouting: today's behaviour ---------------------------------------------------

def test_freerouting_mode_fails_g4_without_a_handoff(world):
    world.routes = [UNROUTED]
    rep = world.run(route="freerouting")
    assert rep["PASS"] is False and "status" not in rep and "handoff" not in rep
    assert "9 net(s) unrouted" in _gate(rep, "G4")["blockers"]
    assert not (world.dir / "netlist_route_handoff.json").exists()
    assert rep["routing"] == {"method": "freerouting", "requested": "freerouting",
                              "why": rp_context.FREEROUTING_WHY}
    assert [a[3] for a in world.args_of("build")] == list(rp_stage_place.PACK_SCALES)


def test_freerouting_mode_keeps_the_last_attempt(world):
    """No best-attempt restore in freerouting mode: G4 judges the last one."""
    world.routes = [{"tracks": 20, "unconnected": 5}, UNROUTED]
    rep = world.run(route="freerouting")
    assert rep["autoroute"]["pack_scale"] == rp_stage_place.PACK_SCALES[-1]
    assert world.pcb.read_text(encoding="utf-8").endswith("(built 3.2)\n(routed)\n")
    assert not [e for e in _routing_decisions(world) if "fewest" in e["decision"]]


# --- auto: Freerouting first, the agent when G4 fails ------------------------------------

def test_auto_pass_is_todays_run(world):
    rep = world.run(route="auto")
    assert rep["PASS"] is True and "status" not in rep and "handoff" not in rep
    assert rep["routing"] == {"method": "freerouting", "requested": "auto",
                              "why": rp_context.ROUTING_WHY}
    assert not (world.dir / "netlist_route_handoff.json").exists()


def test_auto_router_failure_keeps_the_g4_fail_and_hands_off(world):
    _router_failure(world)
    rep = world.run(route="auto")
    g4 = _gate(rep, "G4")
    assert g4["passed"] is False and g4["blockers"][0] == f"autoroute failed: {JAR_ERROR}"
    assert rep["PASS"] is False and rep["status"] == "handoff"
    path = world.dir / "netlist_route_handoff.json"
    assert rep["handoff"] == str(path)
    doc = _handoff(world)
    assert JAR_ERROR in doc["why"] and doc["requested"] == "auto"
    assert rep["routing"] == {"method": "agent (handoff)", "requested": "auto",
                              "why": doc["why"], "handoff": str(path)}
    attempts = [{"attempt": 1, "pack_scale": 1.0, "tracks": None, "unconnected": None,
                 "error": JAR_ERROR}]
    assert doc["freerouting"] == {"attempts": attempts, "attempt": 1, "tracks": None,
                                  "unconnected": None, "error": JAR_ERROR,
                                  "g4_unconnected": None}
    assert [g["gate"] for g in rep["gates"]] == ["G1", "G2", "G3", "G4"]
    assert not (world.dir / "netlist.cache.json").exists()
    assert "[FAIL] G4" in rep["gate_summary"]


def test_auto_why_names_the_router_error_after_the_g4_fixes_remeasured(world):
    """The real-run shape: Freerouting cannot start, the G4 fix loop then
    links clusters on the placed board and re-measures it, so the blockers
    only say what is left -- the handoff's why still names the router error."""
    _router_failure(world)
    world.drcs = [CLEAN_DRC, {**CLEAN_DRC, "unconnected": 3}, {**CLEAN_DRC, "unconnected": 1}]
    world.fixes["all"] = [["GND: linked disconnected clusters"], ["OPEN: OUT_B: no path"]]
    rep = world.run(route="auto")
    assert _gate(rep, "G4")["blockers"] == ["1 net(s) unrouted", "DRC: 1 unconnected item(s)"]
    why = _handoff(world)["why"]
    assert why == (f"Freerouting could not finish the board (pack_scale 1.0): the autoroute "
                   f"failed ({JAR_ERROR}); G4 blockers: 1 net(s) unrouted; DRC: 1 unconnected "
                   f"item(s)")
    assert rep["status"] == "handoff"
    # the summary keeps what Freerouting itself left apart from G4's re-measured count
    summary = _handoff(world)["freerouting"]
    assert (summary["attempt"], summary["unconnected"], summary["error"]) == (1, None, JAR_ERROR)
    assert summary["g4_unconnected"] == 1 == rep["autoroute"]["unconnected"]
    assert summary["attempts"][0]["unconnected"] is None


def test_auto_why_does_not_repeat_an_error_the_blockers_name(world):
    _router_failure(world)
    world.run(route="auto")
    assert _handoff(world)["why"] == (f"Freerouting could not finish the board (pack_scale "
                                      f"1.0); G4 blockers: autoroute failed: {JAR_ERROR}")


def test_auto_handoff_records_one_routing_decision(world):
    _router_failure(world)
    rep = world.run(route="auto")
    routing = _routing_decisions(world)
    assert len(routing) == 1
    assert rep["handoff"] in routing[0]["decision"] and "router subagent" in routing[0]["decision"]
    assert JAR_ERROR in routing[0]["rationale"]
    assert routing[0]["alternatives"] and routing[0]["source"] == "run_pipeline route modes"


def test_auto_unrouted_ladder_hands_off_the_best_routed_attempt(world):
    """The ladder runs as today; the attempt with the fewest unrouted nets is
    restored before G4 judges it, and that board is handed off."""
    world.routes = [{"tracks": 20, "unconnected": 6}, {"tracks": 22, "unconnected": 5},
                    UNROUTED]
    world.drcs = [CLEAN_DRC] * 5 + [{**CLEAN_DRC, "unconnected": 5}]
    rep = world.run(route="auto")
    assert [a[3] for a in world.args_of("build")] == list(rp_stage_place.PACK_SCALES)
    assert rep["autoroute"]["pack_scale"] == 1.5 and rep["autoroute"]["unconnected"] == 5
    assert world.pcb.read_text(encoding="utf-8").endswith("(built 1.5)\n(routed)\n")
    assert rep["status"] == "handoff"
    doc = _handoff(world)
    assert doc["pack_scale"] == 1.5 and doc["freerouting"]["unconnected"] == 5
    assert (doc["freerouting"]["attempt"], doc["freerouting"]["tracks"]) == (2, 22)
    assert doc["freerouting"]["g4_unconnected"] == 5 and "error" not in doc["freerouting"]
    assert [a["unconnected"] for a in doc["freerouting"]["attempts"]] == [6, 5, 9, 9, 9]
    built_15 = f"{K9_HEADER}(built 1.5)\n".replace("\n", os.linesep).encode()
    assert doc["placed_sha256"] == hashlib.sha256(built_15).hexdigest()
    assert "5 net(s) unrouted" in _gate(rep, "G4")["blockers"]
    best = [e for e in _routing_decisions(world) if "fewest" in e["decision"]]
    assert len(best) == 1 and "pack_scale 1.5" in best[0]["decision"]


def _side(world: World, suffix: str) -> Path:
    return Path(str(world.pcb) + suffix)


def test_auto_restore_puts_back_the_best_attempts_placement_view_and_copies(world, monkeypatch):
    """The restored attempt's placement render is drawn from its PLACED board
    (never the routed one), and the copies beside the board are its own: the
    .placed board it was routed from, the .attempt snapshot of the handed
    board, no near-miss copy (.alt<s> / .preheal) of a later placement."""
    near = {"tracks": 9, "unconnected": 3}
    world.routes = [{"tracks": 20, "unconnected": 6}, {"tracks": 22, "unconnected": 2}, near]
    world.routes_by_seed = {seed: [{"tracks": 9, "unconnected": 2}, near] for seed in (1, 2)}
    seen: list[tuple[str, str]] = []
    fake = rp_kicad.render

    def render(pcb: str, tag: str, quality: str = "basic") -> list:
        seen.append((tag, Path(pcb).read_text(encoding="utf-8")))
        return fake(pcb, tag, quality)

    monkeypatch.setattr(rp_kicad, "render", render)
    rep = world.run(route="auto")
    assert rep["autoroute"]["pack_scale"] == 1.5 and rep["status"] == "handoff"
    places = [text for tag, text in seen if tag == "place"]
    assert len(places) == len(rp_stage_place.PACK_SCALES) + 1        # + the restored view
    assert places[-1] == f"{K9_HEADER}(built 1.5)\n"
    assert rep["renders_placement"] == ["place_top.png"]
    placed = _side(world, rp_route.PLACED_SUFFIX).read_bytes()
    assert placed.replace(b"\r\n", b"\n") == f"{K9_HEADER}(built 1.5)\n".encode()
    assert world.pcb.read_text(encoding="utf-8").endswith("(built 1.5)\n(routed)\n")
    assert _side(world, rp_context.ATTEMPT_SUFFIX).read_bytes() == world.pcb.read_bytes()
    assert rp_route.recovery_copies(str(world.pcb)) == [_side(world, rp_route.PLACED_SUFFIX)]
    assert _handoff(world)["placed_sha256"] == hashlib.sha256(placed).hexdigest()


# --- an earlier handoff is superseded --------------------------------------------------

@pytest.mark.parametrize("route, routes, status", [
    ("auto", [{"tracks": 7, "unconnected": 0}], None),
    ("freerouting", [UNROUTED], None),
    ("auto", [{"ok": False, "reason": JAR_ERROR}], "handoff")])
def test_a_run_that_reaches_routing_supersedes_an_earlier_handoff(world, route, routes,
                                                                  status):
    """A handoff describes the board the run left: the next run that places
    or re-judges the board removes it (a run that hands off again writes a
    new one), so a resume can never pick up a stale handoff."""
    _router_failure(world)
    world.run(route="agent")
    old = world.dir / "netlist_route_handoff.json"
    assert old.is_file()
    world.routes = routes
    rep = world.run(route=route)
    assert rep["superseded_handoff"] == str(old)
    assert rep.get("status") == status
    assert old.exists() is (status == "handoff")
    if status:
        assert _handoff(world)["requested"] == route


def test_a_route_cache_hit_supersedes_an_earlier_handoff(world):
    world.run(route="freerouting")                            # a routed run: a manifest
    stale = world.dir / "netlist_route_handoff.json"
    stale.write_text('{"kind": "kicad-eda route handoff"}', encoding="utf-8")
    rep = world.run(route="auto")
    assert rep["route_cache"] == "hit" and rep["PASS"] is True
    assert rep["superseded_handoff"] == str(stale) and not stale.exists()


def test_a_run_that_stops_before_routing_keeps_the_handoff(world):
    _router_failure(world)
    world.run(route="agent")
    world.lint = [{"ok": False, "errors": ["E3 single-pin net N9"], "warnings": []}]
    rep = world.run(route="auto")
    assert "superseded_handoff" not in rep
    assert (world.dir / "netlist_route_handoff.json").is_file()


def test_no_earlier_handoff_no_superseded_key(world):
    assert "superseded_handoff" not in world.run(route="auto")


def test_a_handed_board_is_never_taken_for_an_earlier_cached_one(world, monkeypatch):
    """Run 1 passes (a cache manifest); run 2 bypasses the cache, re-places
    the board and hands it off; run 3 must not reuse the handed board as run
    1's routed one: the miss of run 2 removed the stale manifest, so run 3
    routes again and hands off again (a cache hit would only re-judge it)."""
    assert world.run(route="auto")["PASS"] is True
    manifest = world.dir / "netlist.cache.json"
    assert manifest.is_file()
    _router_failure(world)
    monkeypatch.setenv("PIPELINE_NO_CACHE", "1")
    assert world.run(route="auto")["status"] == "handoff"
    assert not manifest.exists()
    monkeypatch.delenv("PIPELINE_NO_CACHE")
    world.calls.clear()
    rep = world.run(route="auto")
    assert rep["route_cache"] == "miss"
    assert rep["route_cache_reason"] == "no cache manifest from a routed run"
    assert "route" in world.names() and rep["status"] == "handoff"


def test_auto_a_tie_keeps_the_last_attempt(world):
    world.routes = [UNROUTED]
    rep = world.run(route="auto")
    assert rep["autoroute"]["pack_scale"] == rp_stage_place.PACK_SCALES[-1]
    assert not [e for e in _routing_decisions(world) if "fewest" in e["decision"]]
    assert rep["status"] == "handoff"


def test_auto_drc_blockers_on_a_routed_board_hand_off(world):
    world.drcs = [CLEAN_DRC, {**CLEAN_DRC, "errors": 1, "types": {"clearance": 1}}]
    world.fixes["all"] = [["OPEN: clearance at (10, 4) needs a re-route"]]
    rep = world.run(route="auto")
    assert rep["status"] == "handoff" and rep["PASS"] is False
    assert "DRC: 1 violation(s) (clearancex1)" in _handoff(world)["why"]
    assert world.args_of("board_fix") == [("all",)]           # the G4 fix loop ran first


def test_auto_on_a_route_cache_hit_is_judged_as_today(world):
    world.run(route="auto")                                   # routed: a manifest
    world.calls.clear()
    world.drcs = [{**CLEAN_DRC, "errors": 1, "types": {"clearance": 1}}]
    rep = world.run(route="auto")
    assert rep["route_cache"] == "hit" and rep["PASS"] is False
    assert "status" not in rep and not (world.dir / "netlist_route_handoff.json").exists()


# --- agent: the placed board goes to the agent ---------------------------------------------

def test_agent_hands_off_at_the_first_fitting_scale_without_routing(world):
    world.drcs = [{**CLEAN_DRC, "types": {"courtyards_overlap": 2}}, CLEAN_DRC]
    rep = world.run(route="agent")
    assert [a[3] for a in world.args_of("build")] == [1.0, 1.5]
    assert "route" not in world.names()
    assert [(g["gate"], g["passed"]) for g in rep["gates"]] == [
        ("G1", True), ("G2", True), ("G3", True)]
    assert rep["PASS"] is False and rep["status"] == "handoff"
    doc = _handoff(world)
    assert doc["requested"] == "agent" and doc["freerouting"] is None
    assert doc["pack_scale"] == 1.5 and doc["g3"] == _gate(rep, "G3")
    assert doc["placed_sha256"] == hashlib.sha256(world.pcb.read_bytes()).hexdigest()
    assert rep["routing"]["method"] == "agent (handoff)"
    assert rep["autoroute"] == {"attempt": 2, "pack_scale": 1.5, "tracks": None,
                                "unconnected": None}
    assert "fab" not in world.names() and "render" in world.names()   # placement render only
    assert len(_routing_decisions(world)) == 1


def test_agent_design_intent_failure_stops_at_g3_without_a_handoff(world):
    world.gate_checks["placement"] = [{"issues": ["J1 mating face points inward"],
                                       "warnings": []}]
    rep = world.run(route="agent")
    assert rep["PASS"] is False and "status" not in rep
    assert not (world.dir / "netlist_route_handoff.json").exists()


def test_agent_on_a_route_cache_hit_is_judged_as_today(world):
    world.run(route="freerouting")
    world.calls.clear()
    rep = world.run(route="agent")
    assert rep["route_cache"] == "hit" and rep["PASS"] is True and "status" not in rep


# --- the handoff document ---------------------------------------------------------------

def _drc_report(world: World, n: int) -> None:
    items = [{"description": "Missing connection between items",
              "items": [{"description": f"Pad 1 [N{i}] of R1", "pos": {"x": i, "y": 2 * i}},
                        {"description": f"Pad 1 [N{i}] of R2", "pos": {"x": 9, "y": 9}}]}
             for i in range(n)]
    (world.dir / "netlist_drc.json").write_text(json.dumps(
        {"violations": [], "unconnected_items": items}), encoding="utf-8")


def _project(world: World, net_settings: Any) -> None:
    (world.dir / "netlist.kicad_pro").write_text(json.dumps({"net_settings": net_settings}),
                                                 encoding="utf-8")


def test_handoff_document_has_every_key(world):
    _router_failure(world)
    _drc_report(world, 45)
    _project(world, {"classes": [{"name": "Default", "track_width": 0.2, "priority": 2**31 - 1},
                                 {"name": "PWR", "track_width": 0.5, "priority": 0}],
                     "netclass_patterns": [{"pattern": "N1", "netclass": "PWR"}]})
    world.run(route="auto")
    doc = _handoff(world)
    assert doc["kind"] == "kicad-eda route handoff" and doc["version"] == 1
    assert doc["netlist"] == str(world.netlist.resolve())
    assert doc["pcb"] == str(world.pcb.resolve())
    assert doc["project"] == str((world.dir / "netlist.kicad_pro").resolve())
    assert doc["route_hash"] == rp_route_hash.route_hash(DESIGN, world.dir.resolve())
    assert doc["board_file"] == tc.board_file_info(str(world.pcb))
    assert doc["board_file"]["format_version"] == 20241229
    assert len(doc["unconnected_items"]) == 40
    assert doc["unconnected_items"][3] == {
        "description": "Missing connection between items: Pad 1 [N3] of R1; Pad 1 [N3] of R2",
        "x": 3, "y": 6}
    assert doc["net_classes"] == {"N1": ["PWR", 0.5]}
    assert doc["g3"]["gate"] == "G3" and doc["g3"]["passed"] is True
    assert set(doc) >= {"kind", "version", "netlist", "pcb", "project", "requested", "why",
                        "pack_scale", "route_hash", "placed_sha256", "board_file",
                        "freerouting", "unconnected_items", "net_classes", "g3",
                        "instructions", "resume"}


def test_handoff_instructions_name_the_agent_routing_contract(world):
    _router_failure(world)
    world.run(route="auto")
    text = "\n".join(_handoff(world)["instructions"])
    for phrase in ("router subagent", "agent-routing", "k9", "save_board", "close_board",
                   "route_status", "stalled", "close_board(pcb, save=True)", "autoroute_board",
                   "release_for_rewrite", "--resume routed", "--resume stalled"):
        assert phrase in text, phrase


@pytest.mark.parametrize("napkin, strict", [(False, True), (True, False)])
def test_handoff_resume_commands(world, napkin, strict):
    _router_failure(world)
    rep = world.run(route="auto", napkin=napkin)
    resume = _handoff(world)["resume"]
    script = str(Path(rp.__file__).resolve())
    for mode in ("routed", "stalled"):
        cmd = resume[mode]
        assert sys.executable in cmd and script in cmd and str(world.netlist.resolve()) in cmd
        assert cmd.endswith(f"--resume {mode}") and " 30 30 " in cmd
        assert ("--strict" in cmd) is strict
    assert rep["handoff_resume"] == resume


def test_handoff_without_a_drc_report_or_project(world):
    _router_failure(world)
    world.run(route="auto")
    doc = _handoff(world)
    assert doc["unconnected_items"] == [] and doc["net_classes"] == {}


def test_a_malformed_project_is_recorded_not_raised(world):
    _router_failure(world)
    (world.dir / "netlist.kicad_pro").write_text("{ not json", encoding="utf-8")
    rep = world.run(route="auto")
    doc = _handoff(world)
    assert rep["status"] == "handoff" and doc["net_classes"] == {}
    assert "netlist.kicad_pro" in doc["net_classes_error"]


def test_handoff_document_function_is_pure_json(world):
    _router_failure(world)
    world.run(route="auto")
    doc = _handoff(world)
    assert json.loads(json.dumps(doc)) == doc


# --- handoff_veto -----------------------------------------------------------------------

def test_veto_turns_an_auto_handoff_into_a_g4_fail(world):
    _router_failure(world)
    feature = _feature(handoff_veto=lambda ctx: VETO)
    world.use_features(feature)
    rep = world.run(route="auto")
    g4 = _gate(rep, "G4")
    assert g4["passed"] is False and g4["blockers"][0] == f"autoroute failed: {JAR_ERROR}"
    assert g4["blockers"][-1] == f"routing handoff refused: {VETO}"
    assert rep["PASS"] is False and "status" not in rep and "handoff" not in rep
    assert not (world.dir / "netlist_route_handoff.json").exists()
    assert f"routing handoff refused: {VETO}" in rep["gate_summary"]
    assert VETO in rep["routing"]["why"] and rep["routing"]["requested"] == "auto"
    routing = _routing_decisions(world)
    assert len(routing) == 1 and routing[0]["decision"].startswith("no:")
    assert VETO in routing[0]["rationale"]
    assert [h for h, _ in feature.seen].count("handoff_veto") == 1
    assert rp_cli.exit_code(rep) == 1


@pytest.mark.parametrize("route", ["auto", "agent"])
def test_main_exits_1_on_a_vetoed_handoff(world, route):
    _router_failure(world)
    world.use_features(_feature(handoff_veto=lambda ctx: VETO))
    assert rp.main([str(world.netlist), "30", "30", "--route", route]) == 1
    saved = json.loads((world.dir / "netlist_pipeline_report.json").read_text(encoding="utf-8"))
    assert _gate(saved, "G4")["blockers"][-1] == f"routing handoff refused: {VETO}"


def test_veto_halts_agent_mode_at_g4(world):
    world.use_features(_feature(handoff_veto=lambda ctx: VETO))
    rep = world.run(route="agent")
    assert "route" not in world.names()
    g4 = _gate(rep, "G4")
    assert (g4["passed"], g4["blockers"]) == (False, [f"routing handoff refused: {VETO}"])
    assert (g4["name"], g4["feeds"]) == rp_context.GATE_TITLES["G4"]
    assert gates.summarize(rep["gates"]).count("[FAIL]") == 1
    assert rep["PASS"] is False and "status" not in rep
    assert not (world.dir / "netlist_route_handoff.json").exists()
    assert len(_routing_decisions(world)) == 1


def test_no_veto_means_a_handoff(world):
    _router_failure(world)
    feature = _feature()
    world.use_features(feature)
    rep = world.run(route="auto")
    assert rep["status"] == "handoff" and "handoff_veto" in [h for h, _ in feature.seen]


# --- route_provider: never a handoff -------------------------------------------------------

def _provider(world: World, result: dict) -> Any:
    calls: list = []

    def router(*args: Any, **kw: Any) -> dict:
        calls.append(args)
        return dict(result)

    feature = _feature(route_provider=lambda ctx: router)
    world.use_features(feature)
    return calls, feature


def test_an_active_route_provider_never_hands_off_in_auto(world):
    calls, feature = _provider(world, {"ok": False, "tracks": 3, "unconnected": 4})
    rep = world.run(route="auto")
    assert len(calls) == len(rp_stage_place.PACK_SCALES) and "route" not in world.names()
    assert _gate(rep, "G4")["passed"] is False and rep["PASS"] is False
    assert "status" not in rep and not (world.dir / "netlist_route_handoff.json").exists()
    assert "handoff_veto" not in [h for h, _ in feature.seen]


def test_an_active_route_provider_routes_in_agent_mode(world):
    calls, _ = _provider(world, {"ok": True, "tracks": 3, "unconnected": 0})
    rep = world.run(route="agent")
    assert len(calls) == 1 and rep["PASS"] is True and "status" not in rep
    assert rep["routing"]["requested"] == "agent"
    assert "route_provider" in rep["routing"]["why"]
    routing = _routing_decisions(world)
    assert len(routing) == 1 and "route_provider" in routing[0]["rationale"]


def test_an_active_route_provider_failure_in_agent_mode_is_a_g4_fail(world):
    _provider(world, {"ok": False, "error": "no source copper"})
    rep = world.run(route="agent")
    assert _gate(rep, "G4")["blockers"][0] == "autoroute failed: no source copper"
    assert rep["PASS"] is False and "status" not in rep


@pytest.mark.parametrize("route", ["auto", "freerouting"])
def test_a_route_provider_board_is_recorded_as_the_providers(world, route):
    """The routing record and decision name the feature's router, not Freerouting."""
    _provider(world, {"ok": True, "tracks": 3, "unconnected": 0})
    rep = world.run(route=route)
    assert rep["PASS"] is True
    assert rep["routing"] == {"method": rp_context.PROVIDER_METHOD, "requested": route,
                              "why": rp_context.PROVIDER_ROUTED_WHY}
    assert rp_route_modes.PROVIDER_METHOD == rp_context.PROVIDER_METHOD
    routing = _routing_decisions(world)
    assert len(routing) == 1 and "route_provider" in routing[0]["decision"]
    assert routing[0]["rationale"] == rp_context.PROVIDER_ROUTED_WHY
    text = (world.dir / "netlist_review.md").read_text(encoding="utf-8")
    assert f"- Method: {rp_context.PROVIDER_METHOD} (requested: {route})" in text


# --- the review -----------------------------------------------------------------------

def test_the_review_of_a_handoff_says_handoff_and_how_to_resume(world):
    _router_failure(world)
    rep = world.run(route="auto")
    text = (world.dir / "netlist_review.md").read_text(encoding="utf-8")
    verdict = next(line for line in text.splitlines() if line.startswith("- **Verdict"))
    assert verdict.startswith("- **Verdict: HANDOFF**: routing handed to the agent; resume with")
    assert rep["handoff_resume"]["routed"] in verdict
    assert "- Method: agent (handoff) (requested: auto)" in text
    assert f"- Handoff report: `{rep['handoff']}`" in text
