"""Harness determinism around routing (F5-11): the near-miss recovery of the
G3 scale loop (pipeline/rp_route.py: alternate DSN orders of the same
placement, then a verified heal that is rolled back byte-for-byte when it does
not make the board better), the guarded DRC wrapper (rp_kicad.drc ->
drc_guard.run_drc: a fresh report or an error, never a stale read), the
rp_kicad child-process contract, and the transactional EMC post-pass
(rp_stage_layout.stage_emc).

Every KiCad-facing call is a scripted stand-in of tests/fixtures/pipeline_world.py
(World); the board "file" is a text stub the fakes append to (a build writes
"(built <scale>)", a route "(routed)" / "(routed seed <s>)", an applied fix
"(fixed <mode>)", a stitch "(stitched)"), so restores can be compared byte for
byte.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

import kicad_server as k
import rp_context
import rp_kicad
import rp_route
import rp_stage_schematic
import toolchain as tc
from fixtures import pipeline_world
from fixtures.pipeline_world import CLEAN_DRC, DESIGN, K9_HEADER, World, recording_feature

world = pipeline_world.world        # the fixture (an assignment: see pipeline_world)

SHA = {seed: str(seed) * 64 for seed in range(3)}
BUILT = f"{K9_HEADER}(built 1.0)\n"
NEAR = {**CLEAN_DRC, "unconnected": 2}
CLEAN_CHECK = {"issues": [], "warnings": []}
WIDTH = "VIN: open-field segment routed at 0.300 mm but class NC_VIN requires 0.528 mm"
OPEN = "OPEN: VIN: no clear path at the 0.528 mm class width"
CLI = "S:/9.0/kicad-cli.exe"


def _route(unconnected: int | None, tracks: int = 20, seed: int = 0) -> dict:
    """An autoroute result as board_autoroute returns it (canonical DSN stamp)."""
    return {"ok": unconnected == 0, "tracks": tracks, "unconnected": unconnected,
            "dsn_sha256": SHA[seed], "dsn_seed": seed}


def _side(world: World, suffix: str) -> Path:
    return Path(str(world.pcb) + suffix)


def _alt_note(first: int, seed: int, result: str) -> str:
    return (f"near miss: {first} unrouted with the canonical DSN order -> re-routed the "
            f"placed board with alternate DSN order dsn_seed {seed}: {result}")


def _gate(rep: dict, name: str) -> dict:
    return next(g for g in rep["gates"] if g["gate"] == name)


def _boards_at_build(world: World, monkeypatch: pytest.MonkeyPatch) -> list[bytes]:
    """Wrap the fake build: the board bytes on disk when each build starts."""
    seen: list[bytes] = []
    inner = world.fake_build

    def build(netlist: str, w: float, h: float, pcb: str, fps: str, placement: str,
              scale: float) -> dict:
        seen.append(Path(pcb).read_bytes() if Path(pcb).is_file() else b"")
        return inner(netlist, w, h, pcb, fps, placement, scale)

    monkeypatch.setattr(k, "build_board_from_netlist", build)
    return seen


# --- alternate DSN orders on a near miss --------------------------------------------

def test_a_full_route_records_the_dsn_stamp_and_tries_no_alternate(world):
    world.routes = [_route(0, tracks=7)]
    rep = world.run()
    assert world.route_kw == [{}]                    # the first call is the plain one
    assert rep["autoroute"] == {"attempt": 1, "pack_scale": 1.0, "tracks": 7, "unconnected": 0,
                                "dsn_sha256": SHA[0], "dsn_seed": 0}
    assert _side(world, ".placed").read_text(encoding="utf-8") == BUILT
    assert "remediation" not in rep and rep["PASS"] is True


def test_near_miss_tries_the_seeds_in_order_and_stops_at_the_first_full_route(world):
    world.routes = [_route(2)]
    world.routes_by_seed = {1: [_route(0, tracks=22, seed=1)], 2: [_route(0, seed=2)]}
    rep = world.run()
    assert world.route_seeds() == [0, 1]             # seed 2 is never routed
    assert rep["PASS"] is True and len(world.args_of("build")) == 1
    assert ("board_fix", ("heal",)) not in world.calls
    assert rep["autoroute"] == {
        "attempt": 1, "pack_scale": 1.0, "tracks": 22, "unconnected": 0,
        "dsn_sha256": SHA[1], "dsn_seed": 1,
        "alternates": [{"seed": 0, "unconnected": 2, "tracks": 20, "dsn_sha256": SHA[0]},
                       {"seed": 1, "unconnected": 0, "tracks": 22, "dsn_sha256": SHA[1]}]}
    assert rep["remediation"] == [{"gate": "G4", "round": 1,
                                   "applied": [_alt_note(2, 1, "0 unrouted")]}]
    assert _side(world, ".placed").read_text(encoding="utf-8") == BUILT
    assert _side(world, ".alt0").read_text(encoding="utf-8") == BUILT + "(routed)\n"
    assert world.pcb.read_text(encoding="utf-8") == BUILT + "(routed seed 1)\n"
    assert world.pcb.read_bytes() == _side(world, ".alt1").read_bytes()
    assert _side(world, ".attempt").read_bytes() == world.pcb.read_bytes()
    assert not _side(world, ".alt2").exists()


def test_every_alternate_starts_from_the_placed_board(world, monkeypatch):
    starts: list[str] = []
    inner = world.fake_route

    def route(pcb: str, passes: int, jar: str, timeout: int, **kw: Any) -> dict:
        starts.append(Path(pcb).read_text(encoding="utf-8"))
        return inner(pcb, passes, jar, timeout, **kw)

    monkeypatch.setattr(k, "autoroute_board", route)
    world.routes = [_route(3)]
    world.drcs = [CLEAN_DRC, NEAR, CLEAN_DRC]
    world.fixes["heal"] = [["bridged 0.4 mm gap on N1"]]
    world.run()
    assert starts == [BUILT] * 3


def test_without_a_full_route_the_best_seed_is_kept_and_healed(world):
    world.routes = [_route(3)]
    world.routes_by_seed = {1: [_route(2, tracks=21, seed=1)], 2: [_route(2, tracks=19, seed=2)]}
    world.drcs = [CLEAN_DRC, NEAR, CLEAN_DRC]
    world.fixes["heal"] = [["bridged 0.4 mm gap on N1"]]
    rep = world.run()
    assert world.route_seeds() == [0, 1, 2]
    auto = rep["autoroute"]
    assert (auto["dsn_seed"], auto["dsn_sha256"], auto["tracks"], auto["unconnected"]) == (
        1, SHA[1], 21, 0)                               # fewest unrouted, then the lowest seed
    assert [(a["seed"], a["unconnected"]) for a in auto["alternates"]] == [(0, 3), (1, 2), (2, 2)]
    assert rep["remediation"] == [
        {"gate": "G4", "round": 1, "applied": [_alt_note(3, 1, "2 unrouted")]},
        {"gate": "G4", "round": 1, "applied": [_alt_note(3, 2, "2 unrouted")]},
        {"gate": "G4", "round": 1, "applied": ["bridged 0.4 mm gap on N1"]}]
    assert _side(world, ".preheal").read_bytes() == _side(world, ".alt1").read_bytes()
    assert world.pcb.read_text(encoding="utf-8") == BUILT + "(routed seed 1)\n(fixed heal)\n"
    assert rep["PASS"] is True and len(world.args_of("build")) == 1


def test_a_tie_keeps_the_lowest_seed(world):
    world.routes = [_route(2)]
    world.routes_by_seed = {1: [_route(2, seed=1)], 2: [_route(2, seed=2)]}
    world.drcs = [CLEAN_DRC, NEAR, CLEAN_DRC]
    world.fixes["heal"] = [["bridged 0.4 mm gap on N1"]]
    rep = world.run()
    assert (rep["autoroute"]["dsn_seed"], rep["autoroute"]["dsn_sha256"]) == (0, SHA[0])
    assert _side(world, ".preheal").read_text(encoding="utf-8") == BUILT + "(routed)\n"


def test_a_failed_alternate_is_logged_and_never_kept(world):
    stale = _side(world, ".alt1")
    stale.write_text("an older run's seed-1 board\n", encoding="utf-8")
    world.routes = [_route(1)]
    world.routes_by_seed = {1: [{"ok": False, "step": "route", "reason": "router timed out"}],
                            2: [_route(3, seed=2)]}
    world.drcs = [CLEAN_DRC, {**CLEAN_DRC, "unconnected": 1}, CLEAN_DRC]
    world.fixes["heal"] = [["bridged 0.4 mm gap on N1"]]
    rep = world.run()
    alternates = rep["autoroute"]["alternates"]
    assert alternates[1] == {"seed": 1, "unconnected": None, "tracks": None, "dsn_sha256": None,
                             "error": "router timed out"}
    assert not stale.exists()
    assert rep["remediation"][0]["applied"] == [
        _alt_note(1, 1, "the router failed (router timed out)")]
    assert rep["autoroute"]["dsn_seed"] == 0 and rep["PASS"] is True


def test_more_than_a_near_miss_escalates_without_alternates_or_heal(world):
    world.routes = [_route(rp_route.NEAR_MISS_UNCONNECTED + 1), _route(0, tracks=30)]
    rep = world.run()
    assert world.route_seeds() == [0, 0]
    assert [a[3] for a in world.args_of("build")] == [1.0, 1.5]
    assert "alternates" not in rep["autoroute"]
    assert ("board_fix", ("heal",)) not in world.calls and rep["PASS"] is True


def test_a_route_provider_is_called_once_per_attempt_without_alternates_or_heal(world):
    got: list[tuple[tuple, dict]] = []

    def provider(*args: Any, **kw: Any) -> dict:
        got.append((args, kw))
        return {"ok": len(got) > 1, "tracks": 9, "unconnected": 0 if len(got) > 1 else 1}

    world.use_features(recording_feature("carry", route_provider=lambda ctx: provider))
    rep = world.run()
    assert "route" not in world.names() and ("board_fix", ("heal",)) not in world.calls
    assert [(args[1:], kw) for args, kw in got] == [
        ((rp_route.AUTOROUTE_MAX_PASSES, "", rp_route.AUTOROUTE_TIMEOUT_S), {})] * 2
    assert [a[3] for a in world.args_of("build")] == [1.0, 1.5]
    assert "alternates" not in rep["autoroute"] and rep["PASS"] is True
    assert not _side(world, ".placed").exists()


# --- the verified heal ----------------------------------------------------------------

@pytest.mark.parametrize("fixes, drcs, routed, reason", [
    (["bridged 0.4 mm gap on N1"], [NEAR, {**CLEAN_DRC, "errors": 1, "types": {"clearance": 1}}],
     [CLEAN_CHECK, CLEAN_CHECK], "the heal added DRC error(s) (0 -> 1: clearance x1)"),
    (["bridged 0.4 mm gap on N1"], [NEAR, CLEAN_DRC],
     [CLEAN_CHECK, {"issues": [WIDTH], "warnings": []}],
     f"the heal added routed-check issue(s): {WIDTH}"),
    (["bridged 14.35 mm on F.Cu", OPEN], [NEAR], [CLEAN_CHECK],
     f"the healer left 1 break(s) open ({OPEN})"),
    (["bridged 0.4 mm gap on N1"], [NEAR, NEAR], [CLEAN_CHECK, CLEAN_CHECK],
     "2 unconnected item(s) left after the heal"),
], ids=["new-drc-error", "new-width-issue", "open-entry", "still-unconnected"])
def test_a_heal_that_is_not_better_is_rolled_back_and_the_ladder_escalates(
        world, monkeypatch, fixes, drcs, routed, reason):
    boards = _boards_at_build(world, monkeypatch)
    world.routes = [{"tracks": 20, "unconnected": 2}] * 3 + [{"tracks": 30, "unconnected": 0}]
    world.drcs = [CLEAN_DRC, *drcs, CLEAN_DRC]
    world.gate_checks["routed"] = [*routed, CLEAN_CHECK]
    world.fixes["heal"] = [fixes]
    rep = world.run()
    assert len(rep["remediation"]) == 4                  # seed 1, seed 2, rollback, escalation
    assert rep["remediation"][2:] == [
        {"gate": "G4", "round": 1, "applied": [f"heal rolled back: {reason}"]},
        {"gate": "G4", "round": 1,
         "applied": ["2 unrouted -> re-placed at pack_scale 1.5 and re-routed"]}]
    assert [a[3] for a in world.args_of("build")] == [1.0, 1.5]
    preheal = _side(world, ".preheal").read_bytes()
    assert boards[1] == preheal                          # restored before the rebuild
    assert b"(fixed heal)" not in preheal
    assert rep["PASS"] is True and rep["autoroute"]["pack_scale"] == 1.5


def test_an_open_entry_is_not_measured_again(world):
    world.routes = [{"tracks": 20, "unconnected": 2}] * 3 + [{"tracks": 30, "unconnected": 0}]
    world.drcs = [CLEAN_DRC, NEAR, CLEAN_DRC]
    world.fixes["heal"] = [[OPEN]]
    world.run()
    names = world.names()
    after_heal = names[names.index("board_fix") + 1:]
    assert after_heal[:1] == ["build"]                   # no DRC of a board about to be restored


def _routed_context(world: World) -> rp_context.RunContext:
    ctx = rp_context.RunContext(netlist=str(world.netlist), w=30, h=30)
    rp_stage_schematic.stage_load(ctx)
    ctx.route_rep = {"attempt": 1, "pack_scale": 1.0, "tracks": 20, "unconnected": 2}
    world.calls.clear()
    return ctx


def test_heal_rollback_restores_the_routed_board_byte_for_byte(world):
    ctx = _routed_context(world)
    routed = bytes(range(256)) * 4 + b"\r\n(routed)\n"          # any bytes, CRLF kept as is
    world.pcb.write_bytes(routed)
    world.drcs = [NEAR, {**CLEAN_DRC, "errors": 3, "types": {"clearance": 3}}]
    world.fixes["heal"] = [["bridged 0.4 mm gap on N1"]]
    assert rp_route.heal_near_miss(ctx, 1) is False
    assert world.pcb.read_bytes() == routed == _side(world, ".preheal").read_bytes()
    assert ctx.route_rep["unconnected"] == 2
    assert world.names() == ["drc", "gate_check", "board_fix", "drc", "gate_check"]
    assert ctx.remediation == [{"gate": "G4", "round": 1, "applied": [
        "heal rolled back: the heal added DRC error(s) (0 -> 3: clearance x3)"]}]


def test_an_accepted_heal_may_keep_old_findings_but_adds_none(world):
    ctx = _routed_context(world)
    world.pcb.write_text("routed\n", encoding="utf-8")
    dirty = {**CLEAN_DRC, "errors": 1, "types": {"clearance": 1}}
    world.drcs = [{**dirty, "unconnected": 2}, dirty]
    world.gate_checks["routed"] = [{"issues": [WIDTH], "warnings": []}]
    world.fixes["heal"] = [["bridged 0.4 mm gap on N1"]]
    assert rp_route.heal_near_miss(ctx, 2) is True
    assert ctx.route_rep["unconnected"] == 0
    assert ctx.remediation == [{"gate": "G4", "round": 2, "applied": ["bridged 0.4 mm gap on N1"]}]
    assert world.pcb.read_text(encoding="utf-8") == "routed\n(fixed heal)\n"
    assert _side(world, ".attempt").read_bytes() == world.pcb.read_bytes()


def test_a_heal_that_applies_nothing_is_not_logged(world):
    ctx = _routed_context(world)
    world.pcb.write_text("routed\n", encoding="utf-8")
    world.drcs = [NEAR]
    assert rp_route.heal_near_miss(ctx, 1) is False
    assert ctx.remediation == [] and world.names().count("drc") == 1
    assert world.pcb.read_text(encoding="utf-8") == "routed\n"


def test_the_heal_docstring_no_longer_claims_a_rebuild_cannot_help():
    doc = " ".join((rp_route.heal_near_miss.__doc__ or "").split())
    assert "a rebuild can't clear them" not in doc
    assert "deterministic per placement" in doc and "changes the placement" in doc


# --- rp_kicad.drc: a fresh report or an error -------------------------------------------

def _fake_cli(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, write: dict | None = None,
              rc: int = 0, err: str = "") -> tuple[Path, list]:
    calls: list[tuple[list[str], dict]] = []

    def fake_run(args: list[str], **kw: Any) -> subprocess.CompletedProcess:
        calls.append((list(args), kw))
        if write is not None:
            Path(args[args.index("-o") + 1]).write_text(json.dumps(write), encoding="utf-8")
        return subprocess.CompletedProcess(args, rc, "", err)

    monkeypatch.setattr(tc, "board_cli", lambda pcb="": CLI)
    monkeypatch.setattr(subprocess, "run", fake_run)
    board = tmp_path / "b.kicad_pcb"
    board.write_text(K9_HEADER + ")\n", encoding="utf-8")
    return board, calls


def test_drc_with_a_cli_that_writes_nothing_raises_and_never_reads_the_old_report(
        tmp_path, monkeypatch):
    board, calls = _fake_cli(monkeypatch, tmp_path, err="Loading board\nRunning DRC...")
    old = tmp_path / "b_drc.json"
    old.write_text(json.dumps({"violations": [{"type": "clearance"}] * 5,
                               "unconnected_items": []}), encoding="utf-8")
    with pytest.raises(RuntimeError, match="wrote no DRC report") as err:
        rp_kicad.drc(str(board))
    assert "Running DRC..." in str(err.value)            # kicad-cli's output tail
    assert not old.exists()                              # deleted before the run, never read
    assert calls[0][0][:3] == [CLI, "pcb", "drc"]


def test_drc_counts_this_runs_report(tmp_path, monkeypatch):
    board, calls = _fake_cli(monkeypatch, tmp_path, write={
        "violations": [{"type": "clearance"}, {"type": "clearance"}],
        "unconnected_items": [{}]})
    (tmp_path / "b_drc.json").write_text('{"violations": [], "unconnected_items": []}',
                                         encoding="utf-8")
    assert rp_kicad.drc(str(board)) == {"errors": 2, "unconnected": 1,
                                        "types": {"clearance": 2}, "kicad_cli": CLI}
    argv, kw = calls[0]
    assert "--severity-error" in argv and argv[-1] == str(board)
    assert kw["stdin"] is subprocess.DEVNULL and kw["timeout"] > 0
    assert (kw["encoding"], kw["errors"]) == ("utf-8", "replace")


@pytest.mark.parametrize("rc, err, match", [
    (3221226356, "", "crashed"),
    (3, "Error: could not compile custom design rules", "exited 3"),
    (0, "12:00:01: Error: Failed to load board", "Failed to load board"),
], ids=["crash", "rc-3", "error-line-with-rc-0"])
def test_drc_failures_raise_naming_the_kicad_cli_output(tmp_path, monkeypatch, rc, err, match):
    board, _ = _fake_cli(monkeypatch, tmp_path, write={"violations": [], "unconnected_items": []},
                         rc=rc, err=err)
    with pytest.raises(RuntimeError, match=match) as raised:
        rp_kicad.drc(str(board))
    assert err in str(raised.value) and "b.kicad_pcb" in str(raised.value)


# --- rp_kicad children: UTF-8, stdin closed, a timeout ------------------------------------

def _capture_children(monkeypatch: pytest.MonkeyPatch, stdout: str = "") -> list:
    calls: list[tuple[list[str], dict]] = []

    def fake_run(args: list[str], **kw: Any) -> subprocess.CompletedProcess:
        calls.append((list(args), kw))
        return subprocess.CompletedProcess(args, 0, stdout, "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(k, "_kicad_python", lambda: "K:/9.0/bin/python.exe")
    monkeypatch.setattr(tc, "board_cli", lambda pcb="": CLI)
    return calls


def test_every_kicad_child_decodes_utf8_with_stdin_closed_and_a_timeout(tmp_path, monkeypatch):
    calls = _capture_children(monkeypatch, stdout='{"issues": [], "warnings": [], "ok": true}\n')
    pcb = str(tmp_path / "b.kicad_pcb")
    assert rp_kicad.gate_check("routed", pcb, "n.json") == CLEAN_CHECK
    rp_kicad.board_fix(pcb, "n.json", "heal")
    rp_kicad.render(pcb, "final")
    rp_kicad.emc_stitch(pcb, {"max_freq_hz": 1e9})
    assert len(calls) == 5                               # gate, fix, 2 renders, stitch
    for argv, kw in calls:
        assert kw["stdin"] is subprocess.DEVNULL, argv
        assert (kw["encoding"], kw["errors"]) == ("utf-8", "replace"), argv
        assert kw["timeout"] > 0, argv
    assert calls[1][0][4] == str(tmp_path / "b_drc.json")   # the report rp_kicad.drc wrote


def test_board_fix_returns_open_findings_with_their_prefix(tmp_path, monkeypatch):
    _capture_children(monkeypatch, stdout='noise\n{"applied": ["bridged"], "open": ["VIN: x"]}\n')
    assert rp_kicad.board_fix(str(tmp_path / "b.kicad_pcb"), "n.json", "heal") == [
        "bridged", "OPEN: VIN: x"]


def test_emc_stitch_appends_the_feature_argv(monkeypatch):
    calls = _capture_children(monkeypatch, stdout='{"ok": true}\n')
    assert rp_kicad.emc_stitch("b.kicad_pcb", {"max_freq_hz": 1e9}, ["--stackup", "a.json"]) == {
        "ok": True}
    rp_kicad.emc_stitch("b.kicad_pcb", {"fence_only": True})
    assert calls[0][0][-4:] == [str(1e9), "grid", "--stackup", "a.json"]
    assert calls[1][0][-2:] == ["0", "fence"]


def test_render_never_reports_an_older_png(tmp_path, monkeypatch):
    _capture_children(monkeypatch)                       # rc 0, but writes nothing
    old = tmp_path / "b_final_top.png"
    old.write_bytes(b"an older render")
    assert rp_kicad.render(str(tmp_path / "b.kicad_pcb"), "final") == []
    assert not old.exists()


def test_render_reports_the_pngs_this_run_wrote(tmp_path, monkeypatch):
    def fake_run(args: list[str], **kw: Any) -> subprocess.CompletedProcess:
        Path(args[args.index("-o") + 1]).write_bytes(b"png")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(tc, "board_cli", lambda pcb="": CLI)
    monkeypatch.setattr(subprocess, "run", fake_run)
    pcb = str(tmp_path / "b.kicad_pcb")
    assert rp_kicad.render(pcb, "place") == [str(tmp_path / "b_place_top.png"),
                                             str(tmp_path / "b_place_bottom.png")]


# --- transactional EMC stitching ----------------------------------------------------------

def _emc_world(world: World) -> None:
    world.write_design({**DESIGN, "emc": {"stitch": True, "max_freq_hz": 1e9}})


def test_emc_stitching_that_keeps_g4_green_is_kept_and_judged_with_the_routed_check(world):
    _emc_world(world)
    rep = world.run()
    assert world.pcb.read_text(encoding="utf-8").endswith("(stitched)\n")
    assert world.args_of("gate_check").count(("routed",)) == 2
    assert "rolled_back" not in rep["emc"] and rep["PASS"] is True
    assert _gate(rep, "G4")["warnings"][0].startswith("EMC stitching: 12 GND vias")


@pytest.mark.parametrize("drcs, routed, finding", [
    ([CLEAN_DRC, CLEAN_DRC, {**CLEAN_DRC, "errors": 2, "types": {"clearance": 2}}],
     [CLEAN_CHECK], "DRC: 2 violation(s) (clearancex2)"),
    ([CLEAN_DRC], [CLEAN_CHECK, {"issues": [WIDTH], "warnings": []}, CLEAN_CHECK], WIDTH),
], ids=["new-drc-error", "new-routed-issue"])
def test_emc_stitching_that_regresses_g4_is_rolled_back_byte_for_byte(
        world, monkeypatch, drcs, routed, finding):
    _emc_world(world)
    before: list[bytes] = []
    inner = world.fake_emc

    def emc(pcb: str, emc: dict, extra: list[str] | None = None) -> dict:
        before.append(Path(pcb).read_bytes())
        return inner(pcb, emc, extra)

    monkeypatch.setattr(rp_kicad, "emc_stitch", emc)
    world.drcs = [*drcs, CLEAN_DRC]
    world.gate_checks["routed"] = routed
    rep = world.run()
    assert world.pcb.read_bytes() == before[0]          # the stitched board was undone
    g4 = _gate(rep, "G4")
    assert g4["passed"] is True and rep["PASS"] is True
    rolled = [w for w in g4["warnings"] if w.startswith("EMC stitching rolled back: ")]
    assert len(rolled) == 1 and finding in rolled[0]
    assert not any(w.startswith("EMC stitching: ") for w in g4["warnings"])
    assert rep["emc"]["rolled_back"] is True and rep["emc"]["vias_added"] == 12
    assert rep["drc"] == CLEAN_DRC                       # re-judged on the restored board
    assert world.names().count("drc") == 4               # courtyard, G4, stitched, restored
    assert world.args_of("gate_check").count(("routed",)) == 3


def test_a_failed_emc_stitch_leaves_the_judged_board(world, monkeypatch):
    _emc_world(world)

    def broken(pcb: str, emc: dict, extra: list[str] | None = None) -> dict:
        with open(pcb, "a", encoding="utf-8") as f:
            f.write("(half stitched)\n")
        return {"ok": False, "reason": "zone refill failed"}

    monkeypatch.setattr(rp_kicad, "emc_stitch", broken)
    rep = world.run()
    assert "(half stitched)" not in world.pcb.read_text(encoding="utf-8")
    assert "EMC stitching FAILED: zone refill failed" in _gate(rep, "G4")["warnings"]
    assert world.names().count("drc") == 2 and rep["PASS"] is True
