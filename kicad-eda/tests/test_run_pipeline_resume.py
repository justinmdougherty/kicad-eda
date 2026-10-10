"""--resume routed | stalled (F5-13b, ROADMAP D5): a run that picks up the
board a routing handoff gave the router subagent (<stem>_route_handoff.json).

  routed   the agent finished routing through the KiCad board API: G1/G2 re-run,
           G3 carries the handoff's verdict plus a courtyard DRC of the agent's
           board, G4 judges it (DRC + the KiCad 11 API routed gate on a board
           pcbnew cannot load), G5 as usual. Nothing is placed or routed.
  stalled  agent routing stalled: Freerouting on the pre-KiCad-11 snapshot
           (autoroute_board(from_snapshot=True)), the near-miss alternates /
           verified heal, then the pack_scale ladder from the handoff's
           pack_scale + 1, then stop (G4 FAIL, no new handoff).

Every KiCad-facing call is a scripted stand-in of tests/fixtures/pipeline_world.py
(World); the board API judge (board_route_tools.routed_gate) and the snapshot
step of autoroute_board(from_snapshot=True) are stand-ins here (the latter runs
the real file step, board_autoroute.fallback_from_snapshot, on the stub files).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import board_autoroute
import board_route_tools
import decisions as dec
import kicad_server as k
import remediate
import rp_cli
import rp_context
import rp_route
import rp_route_modes
import rp_stage_place
import run_pipeline as rp
import toolchain as tc
from fixtures import pipeline_world
from fixtures.pipeline_world import (CLEAN_DRC, DESIGN, K9_HEADER, K11_HEADER, OK_ANALYSIS,
                                     World, recording_feature)

world = pipeline_world.world        # the fixture (an assignment: see pipeline_world)

AGENT = ('(segment (start 1 2) (end 3 4) (width 0.25) (layer "F.Cu") (net "N1"))\n'
         '(segment (start 3 4) (end 5 4) (width 0.25) (layer "F.Cu") (net "N1"))\n)\n')
STALLED = '(segment (start 1 2) (end 3 4) (width 0.25) (layer "F.Cu") (net "N1"))\n)\n'
HANDOFF = "netlist_route_handoff.json"
ROUTED_WHY = "the router subagent routed it through the KiCad 11 board API"
UNROUTED = {"tracks": 15, "unconnected": 9}
GATE_OK = {"ok": True, "stage": "routed", "backend": "api", "passed": True, "issues": [],
           "warnings": [], "unrouted": 0, "complete": True}
OVERLAP = {**CLEAN_DRC, "types": {"courtyards_overlap": 2}}


class ApiJudge:
    """board_route_tools.routed_gate / release_for_rewrite stand-ins: each
    routed_gate call consumes one of `results` (the last repeats)."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.results: list[dict] = [dict(GATE_OK)]
        self.calls: list[tuple[str, str, str]] = []
        self.released: list[str] = []
        monkeypatch.setattr(board_route_tools, "routed_gate", self.gate)
        monkeypatch.setattr(board_route_tools, "release_for_rewrite", self.release)

    def gate(self, pcb: str, netlist: str = "") -> dict:
        self.calls.append((pcb, netlist, Path(pcb).read_text(encoding="utf-8")))
        return dict(self.results.pop(0) if len(self.results) > 1 else self.results[0])

    def release(self, pcb: str) -> dict:
        self.released.append(pcb)
        return {}


@pytest.fixture
def api(world: World, monkeypatch: pytest.MonkeyPatch) -> ApiJudge:
    return ApiJudge(monkeypatch)


def _gate(rep: dict, name: str) -> dict:
    return next(g for g in rep["gates"] if g["gate"] == name)


def _doc(world: World) -> dict:
    return json.loads((world.dir / HANDOFF).read_text(encoding="utf-8"))


def _routing_decisions(world: World) -> list[dict]:
    return [e for e in dec.read_decisions(world.netlist) if e["stage"] == "routing"]


def _side(world: World, name: str) -> Path:
    return world.dir / name


def _handed(world: World, **kw: Any) -> dict:
    """A run that handed the placed board to the agent (--route agent)."""
    rep = world.run(route="agent", **kw)
    assert rep["status"] == "handoff"
    return _doc(world)


def _agent(world: World, header: str = K11_HEADER, body: str = AGENT) -> None:
    """The router subagent's work: a save through the KiCad 11 API first keeps
    the pre-KiCad-11 board as X.k9.* (the real snapshot step), then the board
    is a KiCad 11 file. A KiCad 9 GUI session keeps the format: no snapshot."""
    if header == K11_HEADER:
        tc.take_snapshot(str(world.pcb))
    world.pcb.write_text(header + body, encoding="utf-8")
    world.calls.clear()
    world.route_kw.clear()
    world.route_args.clear()


def _resumable(world: World, **kw: Any) -> dict:
    doc = _handed(world, **kw)
    _agent(world)
    return doc


def _snapshot_router(world: World, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """k.autoroute_board as the real tool treats from_snapshot=True: the real
    file step (the KiCad 11 set moves to X.agent.*, X.k9.* comes back as X.*),
    then the world's scripted route. Returns the board text each route starts
    from."""
    starts: list[str] = []
    inner = world.fake_route

    def route(pcb: str, passes: int, jar: str, timeout: int, **kw: Any) -> dict:
        extra = board_autoroute.fallback_from_snapshot(pcb) if kw.get("from_snapshot") else {}
        if extra.get("ok") is False:
            world.route_kw.append(dict(kw))
            world.route_args.append((pcb, passes, jar, timeout))
            return extra
        starts.append(Path(pcb).read_text(encoding="utf-8"))
        return {**inner(pcb, passes, jar, timeout, **kw), **extra}

    monkeypatch.setattr(k, "autoroute_board", route)
    return starts


# --- the command line ---------------------------------------------------------------

@pytest.mark.parametrize("argv, resume", [
    (["n.json", "--resume", "routed"], "routed"),
    (["--resume=stalled", "n.json", "30", "40"], "stalled"),
    (["n.json", "--strict", "--resume", "routed"], "routed"),
    (["n.json"], "")])
def test_parse_cli_resume(argv, resume):
    cli = rp_cli.parse_cli(argv)
    assert cli.resume == resume and cli.netlist == "n.json" and cli.route == "auto"


@pytest.mark.parametrize("argv, message", [
    (["n.json", "--resume"], "--resume needs one of routed, stalled"),
    (["n.json", "--resume", "done"], "--resume must be one of routed, stalled, got 'done'"),
    (["n.json", "--resume", "routed", "--resume", "stalled"], "--resume given more than once"),
    (["n.json", "p.json", "--resume", "routed"], "--resume cannot be combined with a placement"),
    (["n.json", "--route", "agent", "--resume", "stalled"], "--resume takes no --route"),
])
def test_parse_cli_resume_errors(argv, message):
    with pytest.raises(rp_cli.UsageError) as err:
        rp_cli.parse_cli(argv)
    assert str(err.value).startswith(message)


def test_usage_and_run_options_name_resume():
    assert "--resume routed|stalled" in rp_cli.USAGE
    assert rp_cli.run_options(rp_cli.parse_cli(["n.json", "--resume", "stalled"])) == {
        "resume": "stalled"}
    assert rp_cli.run_options(rp_cli.parse_cli(["n.json"])) == {}
    assert rp_route_modes.RESUME_MODES == rp_context.RESUME_MODES == ("routed", "stalled")


@pytest.mark.parametrize("kw, match", [
    ({"resume": "done"}, "resume must be one of"),
    ({"resume": "routed", "placement": "p.json"}, "placement override")])
def test_run_refuses_a_bad_resume(world, kw, match):
    with pytest.raises(ValueError, match=match):
        world.run(**kw)


# --- what a resume needs: the handoff, the same design, the same board ----------------------

def _error(rep: dict) -> str:
    assert rep["PASS"] is False and "status" not in rep
    return rep["error"]


def test_resume_without_a_handoff_is_an_error_before_g1(world):
    rep = world.run(resume="routed")
    assert _error(rep).startswith("--resume routed: no routing handoff netlist_route_handoff.json")
    assert rep["gates"] == [] and "lint" not in world.names()
    assert "Verdict: FAIL" in (world.dir / "netlist_review.md").read_text(encoding="utf-8")


@pytest.mark.parametrize("text, why", [
    ("{ not json", "cannot be read"),
    ('{"kind": "something else", "version": 1}', "is not a version 1"),
    ('[1, 2]', "is not a version 1")])
def test_a_malformed_handoff_is_an_error(world, text, why):
    _resumable(world)
    (world.dir / HANDOFF).write_text(text, encoding="utf-8")
    assert why in _error(world.run(resume="stalled"))
    assert "route" not in world.names()


def test_a_changed_netlist_is_an_error_before_g1(world):
    _resumable(world)
    design = json.loads(world.netlist.read_text(encoding="utf-8"))
    world.write_design({**design, "nets": {**design["nets"], "N3": [["R1", "1"], ["R2", "2"]]}})
    error = _error(world.run(resume="routed"))
    assert "the netlist changed since the handoff" in error and "rerun without --resume" in error
    assert "lint" not in world.names()


def test_a_value_change_that_moves_no_copper_resumes(world, api):
    _resumable(world)
    design = json.loads(world.netlist.read_text(encoding="utf-8"))
    world.write_design({**design, "title": "renamed"})
    assert world.run(resume="routed")["PASS"] is True


def test_a_g2_remediation_in_a_resumed_run_is_an_error(world, monkeypatch):
    _resumable(world)
    world.analyses = [{"decoupling": {"uncovered": [{"ref": "R1", "rails_without_cap": ["N1"]}]},
                       "flags": {}, "errors": []}, OK_ANALYSIS]

    def fake_apply(design: dict, tasks: list) -> tuple[dict, list]:
        return {**design, "components": [*design["components"], {"ref": "C9"}]}, ["added C9"]

    monkeypatch.setattr(remediate, "apply", fake_apply)
    rep = world.run(resume="routed")
    error = _error(rep)
    assert error.startswith("--resume routed: G2 remediated the netlist in this resumed run")
    assert "added C9" in error and "rerun without --resume" in error
    assert [g["gate"] for g in rep["gates"]] == ["G1", "G2"]
    assert not {"drc", "build", "route"} & set(world.names())


def test_a_board_held_by_a_kicad_editor_is_an_error(world):
    """Napkin sends the outputs to <stem>.napkin.* while an editor holds the
    board: that is not the board the agent routed."""
    _resumable(world)
    world.open_documents = [{"type": "pcb", "path": str(world.pcb)}]
    error = _error(world.run(resume="routed", napkin=True))
    assert "netlist.napkin.kicad_pcb" in error and "close it" in error


def test_main_exits_1_on_a_resume_error(world, capsys):
    assert rp.main([str(world.netlist), "30", "30", "--strict", "--resume", "stalled"]) == 1
    assert "no routing handoff" in capsys.readouterr().err


# --- --resume routed ---------------------------------------------------------------------

def test_resume_routed_judges_the_agents_kicad_11_board_and_passes(world, api):
    doc = _resumable(world)
    rep = world.run(resume="routed")
    assert rep["PASS"] is True and "status" not in rep and "error" not in rep
    assert [g["gate"] for g in rep["gates"]] == ["G1", "G2", "G3", "G4", "G5"]
    names = world.names()
    assert not {"build", "route", "board_fix", "emc"} & set(names) and "fab" in names
    assert ("gate_check", ("routed",)) not in world.calls
    assert api.calls == [(str(world.pcb), str(world.netlist), K11_HEADER + AGENT)]
    assert api.released == [str(world.pcb)]
    assert world.pcb.read_text(encoding="utf-8") == K11_HEADER + AGENT     # never rewritten
    path = str(world.dir / HANDOFF)
    assert rep["routing"] == {"method": "agent", "requested": "agent", "why": ROUTED_WHY,
                              "handoff": path}
    assert rep["resume"] == {"mode": "routed", "handoff": path, "requested": "agent",
                             "pack_scale": 1.0}
    assert rep["autoroute"] == {"attempt": "agent", "pack_scale": 1.0, "tracks": 2,
                                "unconnected": 0}
    assert rep["board"] == doc["board"]
    assert rep["board_file"]["format_version"] == 20260901
    assert rep["pre_k11_snapshot"] == str(_side(world, "netlist.k9.kicad_pcb"))
    manifest = json.loads((world.dir / "netlist.cache.json").read_text(encoding="utf-8"))
    assert manifest["route_hash"] == doc["route_hash"] and manifest["routed_ok"] is True


def test_resume_routed_g3_carries_the_handoff_verdict_and_checks_courtyards(world, api):
    doc = _resumable(world)
    world.drcs = [OVERLAP, CLEAN_DRC]
    rep = world.run(resume="routed")
    g3 = _gate(rep, "G3")
    assert g3["passed"] is False and "2 courtyard overlap(s) — parts collide" in g3["blockers"]
    assert rp_route_modes.CARRIED_WARNING == "placement checks carried from the handoff run"
    assert g3["warnings"] == [*doc["g3"]["warnings"], rp_route_modes.CARRIED_WARNING]
    assert ("gate_check", ("placement_routed",)) not in world.calls
    assert api.calls == [] and "status" not in rep and rep["routing"]["method"] == "agent"


def test_the_review_of_a_resumed_routed_run(world, api):
    _resumable(world)
    world.run(resume="routed")
    text = (world.dir / "netlist_review.md").read_text(encoding="utf-8")
    assert "- **Verdict: PASS**" in text
    assert "- Method: agent (requested: agent)" in text and f"- Why: {ROUTED_WHY}" in text
    assert "KiCad 11 dialect" in text and "netlist.k9.kicad_pcb" in text
    routing = _routing_decisions(world)
    assert len(routing) == 2 and "--resume routed" in routing[-1]["decision"]


def test_resume_routed_on_a_kicad_9_board_uses_the_swig_routed_check(world, api):
    """The agent routed in a KiCad 9 GUI session: the board stays pcbnew-readable."""
    _handed(world)
    _agent(world, header=K9_HEADER)
    rep = world.run(resume="routed")
    assert rep["PASS"] is True and api.calls == []
    assert ("gate_check", ("routed",)) in world.calls
    assert "KiCad 11" not in rep["routing"]["why"] and "board API" in rep["routing"]["why"]


def test_a_routed_gate_that_cannot_judge_is_a_g4_blocker(world, api):
    _resumable(world)
    api.results = [{"ok": False, "passed": False, "issues": [], "warnings": [],
                    "reason": "no KiCad >= 10.99 api-server"}]
    rep = world.run(resume="routed")
    g4 = _gate(rep, "G4")
    assert g4["passed"] is False
    assert any("no KiCad >= 10.99 api-server" in b and "netlist.kicad_pcb" in b
               for b in g4["blockers"])
    assert api.released == [str(world.pcb)]


def test_a_failed_resumed_g4_never_hands_off_again(world, api):
    """No handoff loop: the G4 FAIL stands (exit 1); the SWIG fix loop cannot
    load a KiCad 11 board, so it is skipped with a warning; the handoff the run
    resumed is kept for the agent's next attempt."""
    _resumable(world)
    before = (world.dir / HANDOFF).read_bytes()
    world.drcs = [CLEAN_DRC, {**CLEAN_DRC, "errors": 1, "types": {"clearance": 1}}]
    api.results = [{**GATE_OK, "passed": False, "issues": ["1 connection(s) still unrouted"]}]
    rep = world.run(resume="routed")
    g4 = _gate(rep, "G4")
    assert g4["passed"] is False and "1 connection(s) still unrouted" in g4["blockers"]
    assert any("board-fix loop skipped" in w for w in g4["warnings"])
    assert "board_fix" not in world.names() and "status" not in rep and "handoff" not in rep
    assert (world.dir / HANDOFF).read_bytes() == before
    assert rp_cli.exit_code(rep) == 1


def test_emc_stitching_is_skipped_on_a_kicad_11_board(world, api):
    world.write_design({**DESIGN, "emc": {"stitch": True, "max_freq_hz": 1e9}})
    _resumable(world)
    rep = world.run(resume="routed")
    assert rep["PASS"] is True and "emc" not in world.names() and "emc" not in rep
    assert any(w.startswith("EMC stitching skipped") for w in _gate(rep, "G4")["warnings"])


def test_resumed_runs_call_the_feature_judge_hooks(world, api):
    _resumable(world)
    feature = recording_feature("resume")
    world.use_features(feature)
    world.run(resume="routed")
    seen = {hook for hook, _ in feature.seen}
    assert {"g3_inputs", "g3_verdict", "g4_inputs", "g4_verdict", "g5_verdict"} <= seen
    assert not {"route_provider", "handoff_veto", "pack_scales", "after_route"} & seen


def test_a_hook_that_cannot_judge_a_kicad_11_board_blocks_g4(world, api):
    _resumable(world)
    why = "layer-role checks cannot run on a KiCad 11 board"

    def g4_inputs(ctx: Any, drc_rep: dict, routed: dict) -> tuple[dict, dict] | None:
        if tc.swig_readable(ctx.pcb) is None:
            return None
        return drc_rep, {**routed, "issues": [*routed["issues"], why]}

    world.use_features(recording_feature("d7", g4_inputs=g4_inputs))
    rep = world.run(resume="routed")
    assert why in _gate(rep, "G4")["blockers"] and rp_cli.exit_code(rep) == 1


def test_a_fresh_run_never_uses_the_api_judge(world, api, monkeypatch):
    """The KiCad 11 handling of G4 is the resumed run's: a fresh run whose
    router leaves a KiCad 11 board is judged as always (the outline feature's
    board-text check covers that case)."""
    inner = world.fake_route

    def route_as_kicad11(pcb: str, *args: Any, **kw: Any) -> dict:
        res = inner(pcb, *args, **kw)
        Path(pcb).write_text(K11_HEADER + AGENT, encoding="utf-8")
        return res

    monkeypatch.setattr(k, "autoroute_board", route_as_kicad11)
    world.drcs = [CLEAN_DRC, {**CLEAN_DRC, "errors": 1, "types": {"clearance": 1}}]
    rep = world.run()
    assert api.calls == [] and ("gate_check", ("routed",)) in world.calls
    assert ("board_fix", ("all",)) in world.calls
    assert not any("skipped" in w for w in _gate(rep, "G4")["warnings"])


def test_main_resume_routed_exits_0(world, api):
    _resumable(world)
    assert rp.main([str(world.netlist), "30", "30", "--strict", "--resume", "routed"]) == 0


# --- --resume stalled ----------------------------------------------------------------------

def _stalled(world: World, monkeypatch: pytest.MonkeyPatch, **kw: Any) -> tuple[dict, list]:
    doc = _handed(world, **kw)
    _agent(world, body=STALLED)
    world.drcs = [CLEAN_DRC]
    return doc, _snapshot_router(world, monkeypatch)


def test_resume_stalled_restores_the_snapshot_and_freerouting_routes_it(world, monkeypatch):
    _doc_, starts = _stalled(world, monkeypatch)
    placed = K9_HEADER + "(built 1.0)\n"
    rep = world.run(resume="stalled")
    assert rep["PASS"] is True and "status" not in rep
    assert world.route_kw == [{"from_snapshot": True}]
    assert world.route_args == [(str(world.pcb), rp_route.AUTOROUTE_MAX_PASSES, "",
                                 rp_route.AUTOROUTE_TIMEOUT_S)]
    assert starts == [placed] and "build" not in world.names()
    assert world.pcb.read_text(encoding="utf-8") == placed + "(routed)\n"
    agent = _side(world, "netlist.agent.kicad_pcb")
    assert agent.read_text(encoding="utf-8") == K11_HEADER + STALLED
    snapshot = str(_side(world, "netlist.k9.kicad_pcb"))
    chain = rep["stall_chain"]
    assert (chain["snapshot"], chain["agent_copy"], chain["outcome"]) == (
        snapshot, str(agent), "routed")
    assert [s["step"] for s in chain["steps"]] == ["snapshot"]
    assert rep["autoroute"]["fallback_from_snapshot"] is True
    assert rep["autoroute"]["agent_copy"] == str(agent)
    assert rep["routing"]["method"] == rp_route_modes.STALL_METHOD
    assert rep["routing"]["why"].startswith("agent routing stalled")
    routing = _routing_decisions(world)
    assert len(routing) == 2 and snapshot in routing[-1]["decision"]
    assert str(agent) in routing[-1]["decision"]
    assert rep["superseded_handoff"] == str(world.dir / HANDOFF)
    assert not (world.dir / HANDOFF).exists()


def test_the_review_of_a_stall_fallback_names_the_copies_and_decisions(world, monkeypatch):
    _stalled(world, monkeypatch)
    world.run(resume="stalled")
    text = (world.dir / "netlist_review.md").read_text(encoding="utf-8")
    assert "- **Verdict: PASS**" in text and "netlist.agent.kicad_pcb" in text
    assert f"- Method: {rp_route_modes.STALL_METHOD} (requested: agent)" in text
    assert "Freerouting on the pre-KiCad-11 snapshot" in text


def test_a_near_miss_after_the_snapshot_route_gets_the_alternates(world, monkeypatch):
    _doc_, starts = _stalled(world, monkeypatch)
    world.routes = [{"tracks": 20, "unconnected": 2}]
    world.routes_by_seed = {1: [{"tracks": 22, "unconnected": 0}]}
    rep = world.run(resume="stalled")
    assert world.route_kw == [{"from_snapshot": True}, {"dsn_seed": 1}]
    assert starts == [K9_HEADER + "(built 1.0)\n"] * 2          # both from the snapshot
    assert rep["PASS"] is True and rep["autoroute"]["dsn_seed"] == 1
    assert "build" not in world.names()
    assert ("near miss: 2 unrouted with the canonical DSN order; alternate DSN order dsn_seed 1 "
            "finished it") in _routing_decisions(world)[-1]["decision"]


def test_a_near_miss_the_heal_finishes_says_so(world, monkeypatch):
    _stalled(world, monkeypatch)
    world.routes = [{"tracks": 20, "unconnected": 2}]
    world.routes_by_seed = {seed: [{"tracks": 20, "unconnected": 2}] for seed in (1, 2)}
    world.drcs = [{**CLEAN_DRC, "unconnected": 2}, CLEAN_DRC]     # before / after the heal
    world.fixes["heal"] = [["bridged 0.4 mm gap on N1"]]
    rep = world.run(resume="stalled")
    assert rep["PASS"] is True and ("board_fix", ("heal",)) in world.calls
    assert "build" not in world.names() and len(rep["stall_chain"]["steps"]) == 1
    assert "the verified micro-gap heal finished dsn_seed 0" in _routing_decisions(world)[-1][
        "decision"]


def test_still_unrouted_continues_the_ladder_from_the_handoffs_pack_scale(world, monkeypatch):
    """Handed off at pack_scale 1.5 (1.0 collided): the ladder goes on at 2.0."""
    world.drcs = [OVERLAP, CLEAN_DRC]
    doc, _starts = _stalled(world, monkeypatch)
    assert doc["pack_scale"] == 1.5
    world.routes = [UNROUTED, {"tracks": 30, "unconnected": 0}]
    rep = world.run(resume="stalled")
    assert [a[3] for a in world.args_of("build")] == [2.0]
    assert world.route_kw == [{"from_snapshot": True}, {}]
    assert rep["PASS"] is True
    assert (rep["autoroute"]["attempt"], rep["autoroute"]["pack_scale"]) == (2, 2.0)
    assert [s["step"] for s in rep["stall_chain"]["steps"]] == ["snapshot", "ladder"]
    assert rep["stall_chain"]["steps"][1]["pack_scales"] == [2.0, 2.6, 3.2]
    assert len(_routing_decisions(world)) == 3


def test_an_exhausted_stall_chain_stops_with_g4_fail_and_no_handoff(world, monkeypatch):
    _stalled(world, monkeypatch)
    feature = recording_feature("stall")
    world.use_features(feature)
    world.routes = [UNROUTED]
    code = rp.main([str(world.netlist), "30", "30", "--strict", "--resume", "stalled"])
    assert code == 1
    rep = json.loads((world.dir / "netlist_pipeline_report.json").read_text(encoding="utf-8"))
    assert [a[3] for a in world.args_of("build")] == list(rp_stage_place.PACK_SCALES[1:])
    assert world.route_kw[0] == {"from_snapshot": True}
    assert _gate(rep, "G4")["passed"] is False and "status" not in rep
    assert rep["routing"]["why"].startswith(rp_route_modes.STALL_EXHAUSTED)
    assert rp_route_modes.STALL_EXHAUSTED == "stall chain exhausted"
    assert rep["stall_chain"]["outcome"] == "exhausted"
    assert not (world.dir / HANDOFF).exists() and "handoff" not in rep
    seen = {hook for hook, _ in feature.seen}
    assert "handoff_veto" not in seen and "on_exhausted" not in seen
    assert len(_routing_decisions(world)) == 4                    # handoff, 3 fallback steps
    assert _routing_decisions(world)[-1]["decision"].startswith("stop")


def test_no_pack_scale_left_stops_after_the_snapshot_route(world, monkeypatch):
    _stalled(world, monkeypatch)
    doc = _doc(world)
    (world.dir / HANDOFF).write_text(json.dumps({**doc, "pack_scale": 3.2}), encoding="utf-8")
    world.routes = [UNROUTED]
    rep = world.run(resume="stalled")
    assert "build" not in world.names() and rep["PASS"] is False
    assert [s["step"] for s in rep["stall_chain"]["steps"]] == ["snapshot"]
    assert rep["routing"]["why"].startswith("stall chain exhausted")
    assert "no pack_scale above 3.2" in rep["routing"]["why"]


def test_a_g3_failure_after_the_last_scale_is_named_in_the_stop(world, monkeypatch):
    _stalled(world, monkeypatch)
    doc = _doc(world)
    (world.dir / HANDOFF).write_text(json.dumps({**doc, "pack_scale": 3.2}), encoding="utf-8")
    world.drcs = [OVERLAP]
    rep = world.run(resume="stalled")
    assert [g["gate"] for g in rep["gates"]][-1] == "G3" and rep["PASS"] is False
    assert rep["routing"]["why"].startswith("stall chain exhausted")
    assert "G3 failed (2 courtyard overlap(s)" in rep["routing"]["why"]


def test_a_failed_snapshot_restore_goes_on_with_the_ladder(world, monkeypatch):
    """No X.k9 snapshot (the restore refuses): the agent's board was kept as
    X.agent.* at load, the ladder re-places at the next pack_scale."""
    _stalled(world, monkeypatch)
    _side(world, "netlist.k9.kicad_pcb").unlink()
    rep = world.run(resume="stalled")
    first = rep["stall_chain"]["steps"][0]
    assert first["unconnected"] is None and "snapshot" in first["error"]
    assert [a[3] for a in world.args_of("build")] == [1.5] and rep["PASS"] is True
    assert rep["agent_board_preserved"] == str(_side(world, "netlist.agent.kicad_pcb"))
    assert _side(world, "netlist.agent.kicad_pcb").read_text(encoding="utf-8") == \
        K11_HEADER + STALLED
    assert "failed" in _routing_decisions(world)[1]["decision"]


def test_resume_stalled_calls_the_feature_judge_hooks(world, monkeypatch):
    _stalled(world, monkeypatch)
    feature = recording_feature("stall")
    world.use_features(feature)
    world.run(resume="stalled")
    seen = {hook for hook, _ in feature.seen}
    assert {"g3_inputs", "g3_verdict", "g4_inputs", "g4_verdict", "g5_verdict",
            "after_route"} <= seen
