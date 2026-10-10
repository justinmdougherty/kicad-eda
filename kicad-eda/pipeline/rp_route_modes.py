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
false (rp_context.halt) and run_pipeline.main exits 3 (rp_cli.exit_code). Resuming
(--resume routed|stalled) is a later harness feature; the commands are recorded now.
A handoff describes the board the run left, so the next run that reaches routing
(retire_handoff: a route-cache miss re-places the board, a hit re-judges it)
removes it (report['superseded_handoff']); only a new handoff writes one again.
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
RESUME_MODES = ("routed", "stalled")
RUN_PIPELINE = Path(__file__).resolve().parent / "run_pipeline.py"
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
    routing record + decision)."""
    if ctx.route != rp_context.ROUTE_AGENT:
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
    tie)."""
    if ctx.route != rp_context.ROUTE_AUTO or ctx.provider_routed:
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
    Nothing for freerouting / agent mode, a route-cache hit or a
    route_provider's board."""
    if ctx.route != rp_context.ROUTE_AUTO or ctx.use_cache or ctx.provider_routed:
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
    the design, placed board sha256, G3 verdict), what Freerouting did, what
    is still unrouted, the net-class widths, the instructions and the resume
    command lines. A DRC report or project file that cannot be read is named
    in unconnected_items_error / net_classes_error, never raised."""
    items, items_error = unconnected_items(ctx.pcb)
    classes, classes_error = net_classes(ctx)
    doc = {"kind": HANDOFF_KIND, "version": HANDOFF_VERSION,
           "netlist": str(Path(ctx.netlist).resolve()), "pcb": str(Path(ctx.pcb).resolve()),
           "project": str(project_path(ctx)), "requested": ctx.route, "why": why,
           "pack_scale": ctx.route_rep.get("pack_scale"),
           "route_hash": rp_route_hash.route_hash(ctx.design, ctx.netlist_dir),
           "placed_sha256": placed_sha256, "board_file": tc.board_file_info(ctx.pcb),
           "freerouting": freerouting, "unconnected_items": items, "net_classes": classes,
           "g3": ctx.g3, "instructions": list(INSTRUCTIONS), "resume": resume_commands(ctx)}
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
