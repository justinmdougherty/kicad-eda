"""Routing modes of the harness (ROADMAP D5) and the routing handoff to the agent.

--route (run(..., route=...); rp_context.ROUTE_MODES):
  freerouting  the pack_scale ladder routes with Freerouting; a G4 failure is final (exit 1).
  auto         (the default) the same ladder; the attempt with the fewest unrouted nets is
               kept (its routed and placed boards in memory, restored before G4 when a
               later attempt did worse: the board, <pcb>.placed, <pcb>.attempt and its
               placement view), and when G4 then fails (unrouted nets, an autoroute
               error, DRC or width blockers) its failed verdict stands and the board is
               handed to the router subagent (exit 3).
  agent        no Freerouting: the board placed at the first pack_scale that passes G3 is
               handed off at once (no G4 verdict; exit 3).
The modes apply on a route-cache miss only: a cache hit is judged as today.

Before any handoff the feature hook handoff_veto (rp_hooks) is asked: a reason turns
the would-be handoff into a G4 FAIL with the blocker 'routing handoff refused:
<reason>' (exit 1) and a routing decision (D7 and RF boards veto: their G4 checks need
a KiCad 9/10 board). When a feature's route_provider supplies the router, auto and
agent never hand off: the provider routes and its failure is a G4 FAIL.

A handoff writes <stem>_route_handoff.json (handoff_document: what the agent and a
resumed run need), sets report['status'] = 'handoff', report['handoff'] (its path),
report['handoff_resume'] (the two resume command lines) and report['routing'] {method
'agent (handoff)', requested, why, handoff}, and records a routing decision; PASS is
false (rp_context.halt) and run_pipeline.main exits 3 (rp_cli.exit_code).
A handoff describes the board the run left, so the next fresh run that reaches
routing (retire_handoff: a route-cache miss re-places the board, a hit re-judges
it) removes it (report['superseded_handoff']); only a new handoff writes one again.

--resume routed|stalled (run(..., resume=...); RunContext.resume) continues the
handoff. load_resume (before G1) reads <stem>_route_handoff.json and refuses
(ResumeError: report 'error', PASS false, exit 1) a missing or malformed one, a
board other than the one it names (a KiCad editor holding it sends a napkin run
to the siblings) and a design whose route hash changed since; resume_after_g2
(in place of the route cache) refuses a G2 remediation (the routed board would
be stale) and checks the hash again. A resumed run never hands off again
(after_g4_failure, agent_takes_over, note_attempt step aside).
  routed   agent_board: the route record of the agent's board (tracks counted
           from its text), report['routing'] {method 'agent', why: the router
           subagent routed it through the KiCad 11 board API} and its decision;
           G3 = the handoff's verdict + a courtyard DRC (rp_stage_place), G4
           judges it (rp_stage_layout: the API routed gate on a KiCad 11 board).
           The handoff stays (the agent may fix the board and resume again).
  stalled  rp_stage_place.stall_chain: Freerouting on the pre-KiCad-11 snapshot
           (rp_route.route_from_snapshot, near-miss alternates / heal included),
           then the pack_scale ladder from the handoff's pack_scale + 1, then
           stop. stall_step records each step in report['stall_chain'] {snapshot,
           agent_copy, steps, outcome} with one routing decision; end_stall_chain
           sets report['routing'] (why 'stall chain exhausted: ...' when nothing
           finished the board, plus a 'stop' decision) and retires the consumed
           handoff (the board is no longer the one it describes).
"""
from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import rp_paths  # noqa: F401  first: puts server/ and server/tools/ on sys.path
import board_text_copper as btc
import drc_guard
import netclass_read
import rp_context
import rp_hooks
import rp_kicad
import rp_route
import rp_route_hash
import toolchain as tc

ROUTE_MODES = rp_context.ROUTE_MODES
DEFAULT_ROUTE = rp_context.ROUTE_AUTO
HANDOFF_SUFFIX = "_route_handoff.json"
HANDOFF_KIND = "kicad-eda route handoff"
HANDOFF_VERSION = 1
HANDOFF_METHOD = "agent (handoff)"
PROVIDER_METHOD = rp_context.PROVIDER_METHOD
UNCONNECTED_LIMIT = 40          # unconnected items copied from <stem>_drc.json
REFUSED = "routing handoff refused: "
SOURCE = "run_pipeline route modes"
RESUME_MODES = rp_context.RESUME_MODES
RUN_PIPELINE = Path(__file__).resolve().parent / "run_pipeline.py"
# --resume (module doc)
CARRIED_WARNING = "placement checks carried from the handoff run"
AGENT_METHOD = "agent"
AGENT_ROUTED_WHY = "the router subagent routed it through the KiCad 11 board API"
AGENT_K9_WHY = ("the router subagent routed it through the KiCad board API in a KiCad 9/10 "
                "session (the board stays readable by pcbnew)")
STALL_METHOD = "freerouting (stall fallback)"
STALL_EXHAUSTED = "stall chain exhausted"
STALL_RULE = ("ROADMAP D5 stall fallback order: Freerouting from the pre-KiCad-11 snapshot (the "
              "agent's KiCad 11 board kept as <stem>.agent.*) with the near-miss alternates and "
              "verified heal, then a higher pack_scale, then stop with a report; a resumed run "
              "never hands the board back to the agent")


class ResumeError(RuntimeError):
    """A --resume run that cannot continue the handoff: run() records it as
    the report's error (PASS false, exit 1), never as a crash."""
AGENT_WHY = ("--route agent: the router subagent routes the placed board through the KiCad "
             "board API (no Freerouting attempt)")
PROVIDER_WHY = ("--route agent, but a pipeline feature supplies the router (route_provider): "
                "it routes the board and a failure is a G4 FAIL, never a handoff")
INSTRUCTIONS: tuple[str, ...] = (
    "Route the board ('pcb') with the router subagent, following the agent-routing skill: "
    "open_board, then route_net / route_check / remove_routing through the KiCad board API "
    "(a KiCad 11 nightly, or a KiCad GUI with its API server on) until route_status passes.",
    "The first save through a KiCad 11 session snapshots the pre-KiCad-11 board as "
    "<stem>.k9.kicad_pcb (+ .kicad_pro) automatically: never copy or edit the board files "
    "yourself.",
    "When route_status passes: save_board, then close_board.",
    "If route_check reports stalled (no new best in 3 rounds): stop routing and call "
    "close_board(pcb, save=True).",
    "Never call autoroute_board, build_board_from_netlist or any other rebuild yourself: the "
    "harness owns Freerouting and placement (--resume stalled runs the fallback chain).",
    "Close the MCP server's board session (close_board) before resuming: the harness runs in "
    "another process and release_for_rewrite only sees its own registry.",
    "Then run resume['routed'] (--resume routed) once route_status passed, or "
    "resume['stalled'] (--resume stalled) after a stall, and read the new report.",
)


def check_route(route: str) -> str:
    """`route` when it is a --route mode, else ValueError naming the modes."""
    if route not in ROUTE_MODES:
        raise ValueError(f"route must be one of {', '.join(ROUTE_MODES)}, got {route!r}")
    return route


def handoff_path(netlist: str) -> Path:
    """<stem>_route_handoff.json beside the netlist."""
    return Path(str(Path(netlist).with_suffix("")) + HANDOFF_SUFFIX)


def retire_handoff(ctx: rp_context.RunContext) -> None:
    """The run reached routing (rp_stage_place.stage_route_cache): an earlier
    run's <stem>_route_handoff.json no longer describes the board (a cache
    miss re-places it, a hit re-judges it), so it is removed and named in
    report['superseded_handoff']; a file that cannot be removed raises (the
    run's crash path records it): a stale handoff must never be resumed."""
    path = handoff_path(ctx.netlist)
    if path.is_file():
        path.unlink()
        ctx.report["superseded_handoff"] = str(path)


# --- the pack_scale ladder (rp_stage_place) -------------------------------------------

def start_ladder(ctx: rp_context.RunContext) -> None:
    """A new ladder (also the on_exhausted re-run): forget the attempts of an
    earlier one."""
    ctx.route_attempts, ctx.best_attempt = [], None
    ctx.best_board, ctx.best_placed = b"", b""
    ctx.handoff_pending = False


def agent_takes_over(ctx: rp_context.RunContext) -> bool:
    """At a passing G3 in agent mode: True = stop the ladder and hand the
    placed board off. An active route_provider routes instead (recorded once:
    routing record + decision). Never in a resumed run (no handoff loop)."""
    if ctx.route != rp_context.ROUTE_AGENT or ctx.resume:
        return False
    if rp_hooks.first("route_provider", ctx) is None:
        return True
    if "routing" not in ctx.report:
        ctx.report["routing"] = {"method": PROVIDER_METHOD, "requested": ctx.route,
                                 "why": PROVIDER_WHY}
        ctx.decide("routing", "--route agent: hand the board to the agent?",
                   "no: the pipeline feature's router (route_provider) routes it",
                   PROVIDER_WHY, source=SOURCE, alternatives=("hand off to the router subagent",))
    return False


def hold_for_agent(ctx: rp_context.RunContext, attempt: int, scale: float) -> None:
    """Agent mode: the board placed at `scale` is not routed here; render the
    placement and mark the handoff pending (stage_place_route hands it off
    once G3 is recorded)."""
    ctx.report["renders_placement"] = rp_kicad.render(ctx.pcb, "place")
    rp_route.clear_recovery_copies(ctx.pcb)
    ctx.route_rep = {"attempt": attempt, "pack_scale": scale, "tracks": None,
                     "unconnected": None}
    ctx.handoff_pending = True


def _rank(route_rep: dict | None) -> float:
    """Unrouted nets of an attempt; a router failure (None) ranks last."""
    unconnected = (route_rep or {}).get("unconnected")
    return float("inf") if unconnected is None else float(unconnected)


def note_attempt(ctx: rp_context.RunContext, attempt: int, scale: float) -> None:
    """Auto mode, after each routed attempt (never under a route_provider):
    record it in ctx.route_attempts and keep its routed and placed boards'
    bytes when it is the best so far (fewest unrouted; a later attempt wins a
    tie). Not in a resumed run: nothing is handed off, G4 judges the last one."""
    if ctx.route != rp_context.ROUTE_AUTO or ctx.provider_routed or ctx.resume:
        return
    rep = ctx.route_rep
    placed = Path(ctx.pcb + rp_route.PLACED_SUFFIX)
    placed_bytes = placed.read_bytes() if placed.is_file() else b""
    summary = {"attempt": attempt, "pack_scale": scale, "tracks": rep.get("tracks"),
               "unconnected": rep.get("unconnected"),
               **({"error": rep["error"]} if "error" in rep else {}),
               "placed_sha256": hashlib.sha256(placed_bytes).hexdigest() if placed_bytes
               else None}
    ctx.route_attempts = [*ctx.route_attempts, summary]
    best = ctx.best_attempt
    if best is None or _rank(rep) <= _rank(best.route_rep):
        ctx.best_board, ctx.best_placed = Path(ctx.pcb).read_bytes(), placed_bytes
        ctx.best_attempt = rp_context.Attempt(scale, ctx.g3, ctx.board_rep, rep)


def keep_best_attempt(ctx: rp_context.RunContext) -> None:
    """Auto mode, once the ladder stopped: when an earlier attempt left fewer
    nets unrouted than the board on disk, restore it (restore_best) with its
    G3 verdict and reports and record the choice; G4 then judges the board
    the agent will get."""
    best, last = ctx.best_attempt, ctx.route_rep
    if best is None or _rank(best.route_rep) >= _rank(last):
        return
    restore_best(ctx)
    ctx.g3, ctx.board_rep, ctx.route_rep = best.g3, best.board_rep, best.route_rep
    ctx.decide("routing", "Freerouting did not finish the board at any pack_scale: which "
                          "routed attempt does G4 judge (and the agent get)?",
               f"attempt {best.route_rep.get('attempt')} at pack_scale {best.scale} "
               f"({_unrouted(best.route_rep)}, the fewest of the ladder)",
               "the agent starts from the board with the least routing left",
               source=SOURCE, alternatives=(f"the last attempt (pack_scale "
                                            f"{last.get('pack_scale')}, {_unrouted(last)})",))


def restore_best(ctx: rp_context.RunContext) -> None:
    """The best attempt's files back on disk, byte for byte: the near-miss
    copies of the later placement are deleted, its placement view is rendered
    from its placed board (never from routed copper), <pcb>.placed is its
    placed board, and the board and the <pcb>.attempt snapshot its routed one
    (put back even when the render raises)."""
    board = Path(ctx.pcb)
    rp_route.clear_recovery_copies(ctx.pcb)
    try:
        if ctx.best_placed:
            board.write_bytes(ctx.best_placed)
            Path(ctx.pcb + rp_route.PLACED_SUFFIX).write_bytes(ctx.best_placed)
            ctx.report["renders_placement"] = rp_kicad.render(ctx.pcb, "place")
    finally:
        board.write_bytes(ctx.best_board)
    rp_route.snapshot_routed(ctx.pcb)


def _unrouted(route_rep: dict) -> str:
    unconnected = route_rep.get("unconnected")
    return ("the router failed" if unconnected is None
            else f"{unconnected} unrouted")


# --- the handoff decision -------------------------------------------------------------

def hand_off_placed(ctx: rp_context.RunContext) -> bool:
    """Agent mode, G3 recorded as passed: hand the placed board off, or, when
    a feature vetoes it, fail G4 with the reason. False: the run stops here."""
    veto = rp_hooks.first("handoff_veto", ctx)
    if veto:
        verdict = {"gate": "G4", "name": rp_context.GATE_TITLES["G4"][0],
                   "feeds": rp_context.GATE_TITLES["G4"][1], "passed": False,
                   "blockers": [REFUSED + veto], "warnings": [], "tasks": []}
        ctx.g4 = verdict
        rp_context.record_gate(ctx, verdict)
        refuse(ctx, veto, "the placed board (--route agent)", "none (handoff refused)",
               REFUSED + veto)
        return False
    hand_off(ctx, AGENT_WHY, _sha256(Path(ctx.pcb)), None)
    return False


def after_g4_failure(ctx: rp_context.RunContext) -> None:
    """Auto mode, G4 recorded as failed on a board this run routed with
    Freerouting: hand it off (or fail on a veto, the blocker added to G4).
    Nothing for freerouting / agent mode, a route-cache hit, a
    route_provider's board or a resumed run (no handoff loop: its G4 FAIL
    stands)."""
    if (ctx.route != rp_context.ROUTE_AUTO or ctx.use_cache or ctx.provider_routed
            or ctx.resume):
        return
    why = freerouting_why(ctx)
    veto = rp_hooks.first("handoff_veto", ctx)
    if veto:
        amend_g4(ctx, REFUSED + veto)
        refuse(ctx, veto, "the Freerouting board that failed G4", "freerouting",
               f"{why}; {REFUSED}{veto}")
        return
    hand_off(ctx, why, _handed_placed_sha256(ctx), freerouting_summary(ctx))


def _flat(text: object) -> str:
    return " ".join(str(text).split())


def freerouting_why(ctx: rp_context.RunContext) -> str:
    """Why the Freerouting board goes to the agent: the autoroute's own error
    when it failed (the G4 fix loop may have re-measured the board since, so
    the blockers no longer name it), then the G4 blockers."""
    rep, blockers = ctx.route_rep, [_flat(b) for b in ctx.g4["blockers"]]
    error = _flat(rep["error"]) if rep.get("error") else ""
    failed = (f": the autoroute failed ({error})"
              if error and not any(error in b for b in blockers) else "")
    return (f"Freerouting could not finish the board (pack_scale {rep.get('pack_scale')})"
            f"{failed}; G4 blockers: {'; '.join(blockers)}")


def amend_g4(ctx: rp_context.RunContext, blocker: str) -> None:
    """The recorded G4 verdict (the last one) with one more blocker."""
    gate_list = ctx.report["gates"]
    at = max(i for i, g in enumerate(gate_list) if g.get("gate") == "G4")
    amended = {**gate_list[at], "passed": False,
               "blockers": [*gate_list[at]["blockers"], blocker]}
    ctx.report["gates"] = [*gate_list[:at], amended, *gate_list[at + 1:]]
    ctx.g4 = amended


def refuse(ctx: rp_context.RunContext, veto: str, what: str, method: str, why: str) -> None:
    """A feature vetoed the handoff: the routing record and its decision."""
    ctx.report["routing"] = {"method": method, "requested": ctx.route, "why": why}
    ctx.decide("routing", f"Hand {what} to the router subagent?",
               f"no: {REFUSED}{veto}; G4 fails instead",
               f"a pipeline feature forbids the handoff (handoff_veto): {veto}",
               source=SOURCE, alternatives=("hand the board to the router subagent",))


def hand_off(ctx: rp_context.RunContext, why: str, placed_sha256: str | None,
             freerouting: dict | None) -> Path:
    """Write <stem>_route_handoff.json and mark the report a handoff (status,
    handoff, handoff_resume, routing) with its routing decision. A handoff
    that cannot be written raises (the run's crash path records it)."""
    path = handoff_path(ctx.netlist)
    doc = handoff_document(ctx, why, placed_sha256, freerouting)
    path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    ctx.report["status"] = rp_context.STATUS_HANDOFF
    ctx.report["handoff"] = str(path)
    ctx.report["handoff_resume"] = doc["resume"]
    ctx.report["routing"] = {"method": HANDOFF_METHOD, "requested": ctx.route, "why": why,
                             "handoff": str(path)}
    scale = ctx.route_rep.get("pack_scale")
    if ctx.route == rp_context.ROUTE_AGENT:
        ctx.decide("routing", "How is the board routed?",
                   f"hand the placed board (pack_scale {scale}) to the router subagent without "
                   f"trying Freerouting; handoff report {path}", AGENT_WHY, source=SOURCE,
                   alternatives=("Freerouting first, the agent only when it fails "
                                 "(--route auto)",))
    else:
        ctx.decide("routing", "Freerouting could not finish the board (G4 failed): who "
                              "routes it now?",
                   f"hand the board (pack_scale {scale}) to the router subagent for "
                   f"{rp_context.AGENT_ROUTING}; handoff report {path}",
                   f"ROADMAP D5 --route auto: Freerouting first, the agent when it fails. {why}",
                   source=SOURCE, alternatives=("stop with the G4 FAIL (--route freerouting)",))
    return path


# --- the handoff document ---------------------------------------------------------------

def handoff_document(ctx: rp_context.RunContext, why: str, placed_sha256: str | None,
                     freerouting: dict | None) -> dict:
    """The JSON the agent and a resumed run read: the board and its project,
    why it was handed off, the placement it carries (pack_scale, route hash of
    the design, placed board sha256, G3 verdict, the board build summary
    'board' a resumed G3 reuses), what Freerouting did, what is still
    unrouted, the net-class widths, the instructions and the resume command
    lines. A DRC report or project file that cannot be read is named in
    unconnected_items_error / net_classes_error, never raised."""
    items, items_error = unconnected_items(ctx.pcb)
    classes, classes_error = net_classes(ctx)
    doc = {"kind": HANDOFF_KIND, "version": HANDOFF_VERSION,
           "netlist": str(Path(ctx.netlist).resolve()), "pcb": str(Path(ctx.pcb).resolve()),
           "project": str(project_path(ctx)), "requested": ctx.route, "why": why,
           "pack_scale": ctx.route_rep.get("pack_scale"),
           "route_hash": rp_route_hash.route_hash(ctx.design, ctx.netlist_dir),
           "placed_sha256": placed_sha256, "board_file": tc.board_file_info(ctx.pcb),
           "freerouting": freerouting, "unconnected_items": items, "net_classes": classes,
           "g3": ctx.g3, "board": ctx.board_rep, "instructions": list(INSTRUCTIONS),
           "resume": resume_commands(ctx)}
    errors = {"unconnected_items_error": items_error, "net_classes_error": classes_error}
    return {**doc, **{key: value for key, value in errors.items() if value}}


def freerouting_summary(ctx: rp_context.RunContext) -> dict:
    """What Freerouting did: every attempt of the ladder, then the handed
    attempt's own result as the router (and its in-place near-miss recovery)
    left it: attempt, tracks, unconnected (None: the router failed) and error.
    g4_unconnected is the count G4 judged on the handed board, which the G4
    board-fix loop may have re-measured (report['autoroute'] carries that one)."""
    rep = ctx.route_rep
    attempts = [{k: v for k, v in a.items() if k != "placed_sha256"} for a in ctx.route_attempts]
    handed = next((a for a in ctx.route_attempts if a["attempt"] == rep.get("attempt")), rep)
    return {"attempts": attempts, "attempt": rep.get("attempt"), "tracks": handed.get("tracks"),
            "unconnected": handed.get("unconnected"),
            **({"error": handed["error"]} if "error" in handed else {}),
            "g4_unconnected": rep.get("unconnected")}


def _handed_placed_sha256(ctx: rp_context.RunContext) -> str | None:
    """sha256 of the placed board of the attempt being handed off (recorded
    before it was routed), else of <pcb>.placed when it exists."""
    number = ctx.route_rep.get("attempt")
    found = next((a.get("placed_sha256") for a in ctx.route_attempts
                  if a["attempt"] == number), None)
    placed = Path(ctx.pcb + rp_route.PLACED_SUFFIX)
    return found or (_sha256(placed) if placed.is_file() else None)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def project_path(ctx: rp_context.RunContext) -> Path:
    return Path(ctx.base + tc.PROJECT_EXT).resolve()


def unconnected_items(pcb: str) -> tuple[list[dict], str | None]:
    """The first UNCONNECTED_LIMIT unconnected items of <stem>_drc.json (the
    last DRC of this board) as {description, x, y}; ([], None) without a
    report, ([], why) for one that cannot be read."""
    path = drc_guard.report_path(pcb)
    if not path.is_file():
        return [], None
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return [], f"{path.name}: {e}"
    entries = report.get("unconnected_items") if isinstance(report, dict) else None
    entries = [e for e in (entries or []) if isinstance(e, dict)]
    return [_unconnected(e) for e in entries[:UNCONNECTED_LIMIT]], None


def _unconnected(entry: dict) -> dict:
    """One kicad-cli unconnected item: its description with the items it
    joins, at the first item's position."""
    items = [i for i in (entry.get("items") or []) if isinstance(i, dict)]
    joined = "; ".join(str(i["description"]) for i in items if i.get("description"))
    what = str(entry.get("description") or "unconnected items")
    pos = (items[0].get("pos") or {}) if items else {}
    return {"description": f"{what}: {joined}" if joined else what,
            "x": pos.get("x"), "y": pos.get("y")}


def net_classes(ctx: rp_context.RunContext) -> tuple[dict, str | None]:
    """{net: [class, track width mm]} of the design's nets from the board's
    .kicad_pro (netclass_read, the widths the G4 width gate demands); ({}, why)
    for a project file that cannot be read."""
    nets = ctx.design.get("nets")
    names = [str(n) for n in nets] if isinstance(nets, dict) else []
    try:
        widths = netclass_read.net_class_widths(project_path(ctx), names)
    except (OSError, ValueError) as e:
        return {}, str(e)
    return {net: [cls, width] for net, (cls, width) in widths.items()}, None


def resume_commands(ctx: rp_context.RunContext) -> dict[str, str]:
    """The command lines that resume this run: the same python, run_pipeline.py,
    netlist, board size and mode (--strict when strict), plus --resume
    routed | stalled."""
    base = [sys.executable, str(RUN_PIPELINE), str(Path(ctx.netlist).resolve()),
            _mm(ctx.w), _mm(ctx.h), *([] if ctx.napkin else ["--strict"])]
    return {mode: _command([*base, "--resume", mode]) for mode in RESUME_MODES}


def _mm(value: float) -> str:
    number = float(value)
    return str(int(number)) if number.is_integer() else repr(number)


def _command(args: list[str]) -> str:
    """One shell line: Windows quoting on Windows, POSIX quoting elsewhere."""
    return subprocess.list2cmdline(args) if os.name == "nt" else shlex.join(args)


# --- --resume: what a resumed run needs ----------------------------------------------------

def check_resume(resume: str, placement: str) -> str:
    """`resume` when it is '' or a --resume mode, else ValueError; a resume
    with a placement override is a ValueError too (the run continues the
    board the handoff describes)."""
    if resume and resume not in RESUME_MODES:
        raise ValueError(f"resume must be one of {', '.join(RESUME_MODES)} (or ''), "
                         f"got {resume!r}")
    if resume and placement:
        raise ValueError(f"resume cannot be combined with a placement override ({placement})")
    return resume


def load_resume(ctx: rp_context.RunContext) -> None:
    """Before G1 of a resumed run: the handoff, the board it names and the
    design it was made from (module doc); ResumeError when any is missing or
    differs. Nothing for a fresh run or one stage_load already halted."""
    if not ctx.resume or ctx.halted_by:
        return
    path = handoff_path(ctx.netlist)
    doc = read_handoff(path)
    _check_board(ctx, doc)
    check_route_hash(ctx, doc)
    ctx.handoff_doc = doc
    ctx.report["resume"] = {"mode": ctx.resume, "handoff": str(path),
                            "requested": doc.get("requested"), "pack_scale": doc["pack_scale"]}


def read_handoff(path: Path) -> dict:
    """The handoff document at `path`, or ResumeError naming what is wrong."""
    if not path.is_file():
        raise ResumeError(f"no routing handoff {path.name} beside the netlist: --resume "
                          f"continues a run that handed routing to the agent (exit 3); run "
                          f"without --resume")
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise ResumeError(f"the routing handoff {path.name} cannot be read: {e}") from e
    if (not isinstance(doc, dict) or doc.get("kind") != HANDOFF_KIND
            or doc.get("version") != HANDOFF_VERSION):
        raise ResumeError(f"{path.name} is not a version {HANDOFF_VERSION} '{HANDOFF_KIND}' "
                          f"document")
    scale = doc.get("pack_scale")
    if isinstance(scale, bool) or not isinstance(scale, (int, float)) or not scale > 0:
        raise ResumeError(f"{path.name} records no usable pack_scale ({scale!r})")
    return doc


def _check_board(ctx: rp_context.RunContext, doc: dict) -> None:
    """The board this run judges must be the one the handoff names, on disk."""
    named, board = Path(str(doc.get("pcb") or "")), Path(ctx.pcb)
    if named.resolve() != board.resolve():
        held = ("; a KiCad editor holds the board (the D5 hard stop moved this run to the "
                "napkin siblings): close it in KiCad, then resume again"
                if ctx.report.get("hard_stop") else "")
        raise ResumeError(f"the handoff names the board {named}, but this run's board is "
                          f"{board}{held}")
    if not board.is_file():
        raise ResumeError(f"the handed-off board {board} is missing")


def check_route_hash(ctx: rp_context.RunContext, doc: dict) -> str:
    """The design's route hash when it equals the handoff's, else ResumeError."""
    current = rp_route_hash.route_hash(ctx.design, ctx.netlist_dir)
    if doc.get("route_hash") != current:
        raise ResumeError(f"the netlist changed since the handoff (route hash {current[:12]}, "
                          f"the handoff's {str(doc.get('route_hash'))[:12]}): the handed-off "
                          f"board no longer matches the design; rerun without --resume")
    return current


def resume_after_g2(ctx: rp_context.RunContext) -> None:
    """In place of the route cache in a resumed run: a G2 remediation that
    changed the netlist is a ResumeError (the routed board would be stale),
    then the route hash is checked again on the design G1/G2 left."""
    fixes = [str(item) for entry in ctx.remediation if entry["gate"] == "G2"
             for item in entry["applied"]
             if not str(item).lstrip().startswith(rp_context.OPEN_PREFIX)]
    if fixes:
        raise ResumeError(f"G2 remediated the netlist in this resumed run ({'; '.join(fixes)}): "
                          f"the routed board would be stale; rerun without --resume to place "
                          f"and route the remediated design")
    ctx.rhash = check_route_hash(ctx, ctx.handoff_doc)
    ctx.use_cache = False


# --- --resume routed ------------------------------------------------------------------------

def count_tracks(pcb: str) -> int | None:
    """Tracks + arcs of a board read from its text (any KiCad 5..11 format);
    None when the text cannot be read."""
    try:
        items = btc.read_copper_file(pcb).items
    except (OSError, ValueError):
        return None
    return sum(1 for item in items if item.get("kind") in ("track", "arc"))


def agent_board(ctx: rp_context.RunContext) -> None:
    """--resume routed, before G3: the route record of the agent's board (G4
    measures its unconnected count), report['routing'] and the decision."""
    doc, path = ctx.handoff_doc, ctx.report["resume"]["handoff"]
    ctx.route_rep = {"attempt": AGENT_METHOD, "pack_scale": doc.get("pack_scale"),
                     "tracks": count_tracks(ctx.pcb), "unconnected": None}
    why = AGENT_ROUTED_WHY if tc.swig_readable(ctx.pcb) else AGENT_K9_WHY
    ctx.report["routing"] = {"method": AGENT_METHOD, "requested": doc.get("requested"),
                             "why": why, "handoff": path}
    ctx.decide("routing", "How is the board routed?",
               f"the router subagent's board (--resume routed after the handoff {path}): G3 "
               f"carries the handoff's placement checks, G4 judges the agent's copper", why,
               source=SOURCE, alternatives=("--resume stalled: Freerouting on the "
                                            "pre-KiCad-11 snapshot, then a higher pack_scale",))


# --- --resume stalled -----------------------------------------------------------------------

def _outcome(rep: dict) -> str:
    unconnected = rep.get("unconnected")
    if unconnected is None:
        return f"the router failed ({_flat(rep.get('error') or 'no result')})"
    return ("every net routed" if unconnected == 0 else f"{unconnected} unrouted") + _recovery(rep)


def _recovery(rep: dict) -> str:
    """What the near-miss recovery (rp_route: alternate DSN orders, then the
    verified heal) did to an attempt, '' when it did not run."""
    alts = [a for a in rep.get("alternates") or [] if isinstance(a, dict)]
    if not alts:
        return ""
    kept = next((a for a in alts if a.get("seed") == rep.get("dsn_seed")), {})
    if rep.get("unconnected"):
        how = "neither the alternate DSN orders nor the verified heal finished it"
    elif kept.get("unconnected"):
        how = f"the verified micro-gap heal finished dsn_seed {rep.get('dsn_seed')}"
    else:
        how = f"alternate DSN order dsn_seed {rep.get('dsn_seed')} finished it"
    first = alts[0].get("unconnected")
    return f" (near miss: {first} unrouted with the canonical DSN order; {how})"


def _scales(scales: tuple[float, ...]) -> str:
    return ", ".join(str(s) for s in scales)


def stall_step(ctx: rp_context.RunContext, step: str, scales: tuple[float, ...] = ()) -> None:
    """One step of the stall chain ('snapshot' or 'ladder', on ctx.route_rep
    as the step left it): an entry of report['stall_chain'] and its routing
    decision."""
    rep = ctx.route_rep
    chain = ctx.report.get("stall_chain") or {
        "snapshot": rep.get("pre_k11_snapshot") or tc.existing_snapshot(ctx.pcb),
        "agent_copy": rep.get("agent_copy") or ctx.report.get("agent_board_preserved"),
        "steps": []}
    entry = {"step": step, "attempt": rep.get("attempt"), "pack_scale": rep.get("pack_scale"),
             "tracks": rep.get("tracks"), "unconnected": rep.get("unconnected"),
             **({"error": rep["error"]} if rep.get("error") else {}),
             **({"fallback_from_snapshot": bool(rep.get("fallback_from_snapshot"))}
                if step == "snapshot" else {"pack_scales": list(scales)})}
    ctx.report["stall_chain"] = {**chain, "steps": [*chain["steps"], entry]}
    if step == "snapshot":
        _decide_snapshot(ctx, chain)
    else:
        ctx.decide("routing", "The snapshot route did not finish the board: re-place at a "
                              "higher pack_scale?",
                   f"re-placed along the pack_scale ladder {_scales(scales)} (from the "
                   f"handoff's {ctx.handoff_doc.get('pack_scale')} + 1) and routed with "
                   f"Freerouting: {_outcome(rep)} at pack_scale {rep.get('pack_scale')}",
                   STALL_RULE,
                   source=SOURCE, alternatives=("stop with a report",))


def _decide_snapshot(ctx: rp_context.RunContext, chain: dict) -> None:
    rep = ctx.route_rep
    if rep.get("fallback_from_snapshot"):
        what = (f"Freerouting on the pre-KiCad-11 snapshot {chain['snapshot']} (the agent's "
                f"KiCad 11 board kept as {chain['agent_copy']}): {_outcome(rep)}")
    elif rep.get("unconnected") is None:
        what = (f"Freerouting on the pre-KiCad-11 snapshot failed: {_outcome(rep)}; the board "
                f"stays as the agent left it")
    else:
        what = (f"Freerouting on the board as it is ({rep.get('fallback_note') or 'no snapshot'}"
                f"): {_outcome(rep)}")
    ctx.decide("routing", "Agent routing stalled (--resume stalled): how is the board routed "
                          "now?", what, STALL_RULE, source=SOURCE,
               alternatives=("re-place at the next pack_scale and route with Freerouting",
                             "stop with a report"))


def end_stall_chain(ctx: rp_context.RunContext, finished: bool,
                    scales: tuple[float, ...]) -> None:
    """The chain's outcome: report['routing'] (why 'stall chain exhausted:
    ...' and a 'stop' decision when nothing finished the board) and the
    consumed handoff retired."""
    rep, steps = ctx.route_rep, ctx.report["stall_chain"]["steps"]
    first = ("Freerouting on the pre-KiCad-11 snapshot" if steps[0].get("fallback_from_snapshot")
             else "Freerouting on the board as the agent left it")
    if finished:
        how = (first if len(steps) == 1 else
               f"Freerouting at pack_scale {rep.get('pack_scale')} (the pack_scale ladder)")
        why = f"agent routing stalled (--resume stalled); the D5 fallback chain finished it: {how}"
    else:
        ladder = (f"the pack_scale ladder ({_scales(scales)}) ended with {_outcome(rep)} at "
                  f"pack_scale {rep.get('pack_scale')}" if len(steps) > 1 else
                  f"no pack_scale above {ctx.handoff_doc.get('pack_scale')} is left")
        g3 = ("" if ctx.g3.get("passed") else
              f"; G3 failed ({'; '.join(str(b) for b in ctx.g3.get('blockers') or [])})")
        why = (f"{STALL_EXHAUSTED}: {first} left {_outcome(steps[0])}; {ladder}{g3}; the run "
               f"fails and no new handoff is written")
        ctx.decide("routing", "The stall fallback chain is exhausted: what now?",
                   "stop: the last attempt is judged and fails; no new handoff (a resumed run "
                   "never hands the board back to the agent)", f"{STALL_RULE}. {why}",
                   source=SOURCE, alternatives=("hand the board to the router subagent again",))
    ctx.report["stall_chain"] = {**ctx.report["stall_chain"],
                                 "outcome": "routed" if finished else "exhausted"}
    ctx.report["routing"] = {"method": STALL_METHOD, "requested": ctx.handoff_doc.get("requested"),
                             "why": why, "handoff": ctx.report["resume"]["handoff"]}
    consumed = handoff_path(ctx.netlist)
    if consumed.is_file():
        consumed.unlink()
        ctx.report["superseded_handoff"] = str(consumed)
