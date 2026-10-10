"""Harness stages between G2 and G4: the route cache, then G3 placement and
the pack_scale ladder that places, gates and routes (or re-judges a cached
routed board). Feature hooks (rp_hooks): pack_scales, g3_inputs, g3_verdict,
after_route, on_exhausted (route_provider is rp_route's, handoff_veto
rp_route_modes').

Route modes (rp_route_modes, a route-cache miss only): agent stops the ladder
at the first pack_scale that passes G3 and hands the placed board off once G3
is recorded (the run ends there); auto records every routed attempt and,
once the ladder stopped, restores the one with the fewest unrouted nets
before G4 judges it; freerouting is the ladder as it always was. Every run
that reaches the route cache removes an earlier run's handoff file, and a
route-cache miss removes the manifest of the board it is about to re-place.

Napkin mode (ROADMAP D5): G3 is judged with napkin=ctx.napkin, and a failed
G3 with a connector face-out or thermal footprint-swap task gets up to
rp_napkin.G3_POLICY_ROUNDS policy rounds at the SAME pack_scale (the netlist
rewritten, the route-cache key refreshed, the board rebuilt and re-judged)
before the ladder decides; a policy task left open stops the ladder like any
design-intent task. A board SWIG cannot read was kept at load
(rp_stage_schematic.preserve_unreadable_board) before G3 rebuilds over it.

--resume (rp_route_modes): the route cache is neither read nor retired
(resume_after_g2 checks the design instead). routed: nothing is placed or
routed; resume_g3 judges the agent's board (the handoff's G3 verdict carried,
with a warning, plus a courtyard DRC of the board, through the g3_inputs /
g3_verdict hooks). stalled: stall_chain -- Freerouting on the pre-KiCad-11
snapshot (rp_route.route_from_snapshot, near-miss recovery included) judged by
resume_g3, then, while the board is not finished, the pack_scale ladder from
the handoff's pack_scale + 1 (rebuild + Freerouting, the snapshot route as the
attempt a looser retry that no longer fits restores), then stop. No
on_exhausted re-run and no handoff in a resumed run."""
from __future__ import annotations

import json
from pathlib import Path

import rp_paths  # noqa: F401  first: puts server/ and server/tools/ on sys.path
import gates
import kicad_server as k
import toolchain as tc
import rp_context
import rp_hooks
import rp_kicad
import rp_napkin
import rp_route
import rp_route_hash
import rp_route_modes

# auto-placement looseness ladder (a placement override gets one try)
PACK_SCALES: tuple[float, ...] = (1.0, 1.5, 2.0, 2.6, 3.2)


@rp_context.stage
def stage_route_cache(ctx: rp_context.RunContext) -> bool:
    """ROUTE CACHE: when nothing that moves copper changed since the last
    successful run, reuse the routed board and skip build+route entirely —
    the G3/G4 GATES still re-run against the cached artifact (the cache skips
    work, never judgement). Hashed AFTER G2 so a G2 netlist remediation
    correctly invalidates. PIPELINE_NO_CACHE=1 bypasses. Never stops the run.
    An earlier run's routing handoff is superseded here: either way this run
    decides the board's state (rp_route_modes.retire_handoff). A miss also
    removes the stale manifest (retire_manifest). A resumed run continues the
    handoff instead: rp_route_modes.resume_after_g2 (no cache, no retire)."""
    if ctx.resume:
        rp_route_modes.resume_after_g2(ctx)
        return True
    rp_route_modes.retire_handoff(ctx)
    ctx.rhash = rp_route_hash.route_hash(ctx.design, ctx.netlist_dir)
    try:
        cached = json.loads(ctx.cache_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cached = {}                    # no (or a corrupt) manifest: a plain miss
    ctx.cached = cached if isinstance(cached, dict) else {}
    reason = cache_miss_reason(ctx)
    ctx.use_cache = reason is None
    ctx.report["route_cache"] = "hit" if ctx.use_cache else "miss"
    if reason:
        ctx.report["route_cache_reason"] = retire_manifest(ctx, reason)
    return True


def retire_manifest(ctx: rp_context.RunContext, reason: str) -> str:
    """A miss re-places the board, so an earlier run's manifest no longer
    describes the board on disk: remove it (a run that passes G4 writes a new
    one), or a run this one leaves unrouted, failed or handed off could later
    be taken for the cached routed board. The miss reason, with a manifest
    that could not be removed named in it (never raised: a later hit is still
    re-judged by G3 and G4)."""
    try:
        if ctx.cache_path.is_file():
            ctx.cache_path.unlink()
    except OSError as e:
        return f"{reason}; the stale cache manifest could not be removed ({type(e).__name__}: {e})"
    return reason


def cache_miss_reason(ctx: rp_context.RunContext) -> str | None:
    """Why the cached routed board cannot be reused (None: reuse it). A board
    saved by KiCad 11 never is: the SWIG gate scripts cannot read it."""
    if ctx.placement:
        return "a placement override was given"
    if ctx.no_cache:
        return "PIPELINE_NO_CACHE is set"
    if not ctx.cached:
        return "no cache manifest from a routed run"
    if ctx.cached.get("route_hash") != ctx.rhash:
        return "placement/routing inputs changed since the cached run"
    if not ctx.cached.get("routed_ok"):
        return "the cached run did not finish routing"
    if not Path(ctx.pcb).is_file():
        return "the cached board file is missing"
    refusal = tc.swig_readable(ctx.pcb)
    if refusal:
        return f"the cached board is not readable by the SWIG gate scripts: {refusal}"
    return None


@rp_context.stage
def stage_place_route(ctx: rp_context.RunContext) -> bool:
    """G3 + the routing it gates: place (gate) then route, self-feeding — or
    re-judge the cached routed board. Returns the G3 verdict. A board that
    passed G3 with 0 unconnected goes to the after_route hook before G4. In
    agent mode a placed board that passed G3 is handed off here (False: the
    run stops; a veto fails G4 instead). A resumed run judges the agent's
    board (routed) or runs the stall chain (stalled)."""
    if ctx.resume == rp_context.RESUME_ROUTED:
        rp_route_modes.agent_board(ctx)
        resume_g3(ctx)
    elif ctx.resume == rp_context.RESUME_STALLED:
        stall_chain(ctx)
    elif ctx.use_cache:
        revalidate_cached_board(ctx)
    else:
        if place_and_route(ctx) and rp_hooks.first("on_exhausted", ctx):
            rerun_after_exhaustion(ctx)
        rp_route_modes.keep_best_attempt(ctx)
    ctx.report["board"] = ctx.board_rep
    ctx.report["autoroute"] = ctx.route_rep
    passed = rp_context.record_gate(ctx, ctx.g3)
    if passed and ctx.handoff_pending:
        return rp_route_modes.hand_off_placed(ctx)
    if passed and ctx.route_rep.get("unconnected") == 0:
        rp_hooks.notify("after_route", ctx)
    return passed


def rerun_after_exhaustion(ctx: rp_context.RunContext) -> None:
    """A feature answered on_exhausted True (it changed the design: D7-24c adds
    layers): re-run ONCE from after_g2 — the hook, a fresh route-cache key for
    the manifest, the pack-scale ladder. A second exhaustion is final."""
    rp_hooks.notify("after_g2", ctx)
    ctx.rhash = rp_route_hash.route_hash(ctx.design, ctx.netlist_dir)
    place_and_route(ctx)


def revalidate_cached_board(ctx: rp_context.RunContext) -> None:
    """G3 against the cached artifact. It is ROUTED, so the routed variant of
    the placement check runs (plane integrity is a pre-route check; G4's DRC
    re-run proves the net connectivity the plane check exists to predict)."""
    ctx.board_rep = ctx.cached.get("board_rep") or {"ok": True}
    cy = rp_kicad.courtyard_drc(ctx.pcb)
    pl = rp_kicad.gate_check("placement_routed", ctx.pcb, ctx.netlist)
    cy, pl = rp_hooks.chain("g3_inputs", ctx, (cy, pl))
    ctx.g3 = rp_hooks.chain("g3_verdict", ctx,
                            gates.g3_placement(ctx.board_rep, cy, pl["issues"], pl["warnings"],
                                               napkin=ctx.napkin))
    ctx.route_rep = {"cached": True,
                     "pack_scale": ctx.cached.get("pack_scale"),
                     "tracks": ctx.cached.get("tracks"),
                     "unconnected": ctx.cached.get("unconnected", 0)}


def resume_g3(ctx: rp_context.RunContext) -> None:
    """G3 of a resumed run on the board as it is (the agent's, or the
    snapshot Freerouting just routed): the handoff's G3 verdict carried (its
    blockers re-judged as placement issues, its warnings plus
    rp_route_modes.CARRIED_WARNING) and rp_kicad.courtyard_drc of the board
    (kicad-cli of the KiCad that wrote it: a KiCad 11 board works), through
    the g3_inputs / g3_verdict hooks; the handoff's board summary is the
    build report."""
    doc = ctx.handoff_doc
    carried = doc.get("g3") if isinstance(doc.get("g3"), dict) else {}
    ctx.board_rep = dict(doc.get("board") or {"ok": True})
    cy = rp_kicad.courtyard_drc(ctx.pcb)
    pl = {"issues": list(carried.get("blockers") or []),
          "warnings": [*(carried.get("warnings") or []), rp_route_modes.CARRIED_WARNING]}
    cy, pl = rp_hooks.chain("g3_inputs", ctx, (cy, pl))
    verdict = rp_hooks.chain("g3_verdict", ctx,
                             gates.g3_placement(ctx.board_rep, cy, pl["issues"], pl["warnings"],
                                                napkin=ctx.napkin))
    ctx.g3 = {**verdict, "warnings": list(dict.fromkeys(verdict["warnings"]))}


def stall_chain(ctx: rp_context.RunContext) -> None:
    """--resume stalled (ROADMAP D5): Freerouting on the pre-KiCad-11 snapshot
    at the handoff's pack_scale (attempt 1, judged by resume_g3); while the
    board is not finished (G3 passed and nothing unrouted), the pack_scale
    ladder from the handoff's pack_scale + 1 (attempts 2..); then the outcome
    (rp_route_modes.end_stall_chain). One routing decision per step."""
    scale = float(ctx.handoff_doc["pack_scale"])
    rp_route.route_from_snapshot(ctx, 1, scale)
    resume_g3(ctx)
    rp_route_modes.stall_step(ctx, "snapshot")
    scales = remaining_scales(ctx, scale)
    if not stall_finished(ctx) and scales:
        routed = ctx.route_rep.get("unconnected") is not None
        good = rp_context.Attempt(scale, ctx.g3, ctx.board_rep, ctx.route_rep) if routed else None
        place_and_route(ctx, scales, first=2, last_good=good)
        rp_route_modes.stall_step(ctx, "ladder", scales)
    rp_route_modes.end_stall_chain(ctx, stall_finished(ctx), scales)


def stall_finished(ctx: rp_context.RunContext) -> bool:
    return bool(ctx.g3.get("passed")) and ctx.route_rep.get("unconnected") == 0


def ladder_scales(ctx: rp_context.RunContext) -> tuple[float, ...]:
    """The pack_scale ladder: one try at 1.0 with a placement override, else
    the pack_scales hook's ladder or PACK_SCALES."""
    return (1.0,) if ctx.placement else tuple(
        rp_hooks.first("pack_scales", ctx, PACK_SCALES) or PACK_SCALES)


def remaining_scales(ctx: rp_context.RunContext, scale: float) -> tuple[float, ...]:
    """The ladder after `scale` (the handoff's pack_scale + 1 onward)."""
    ladder = ladder_scales(ctx)
    if scale in ladder:
        return ladder[ladder.index(scale) + 1:]
    return tuple(s for s in ladder if s > scale)


def place_and_route(ctx: rp_context.RunContext, scales: tuple[float, ...] | None = None,
                    first: int = 1, last_good: rp_context.Attempt | None = None) -> bool:
    """The G3 -> route scale loop over `scales` (default ladder_scales),
    numbering attempts from `first`. A G3 failure whose tasks are all auto
    ("loosen placement") re-places at the next pack_scale and re-tests; a
    routing shortfall does the same (the deterministic-router escape).
    Manual blockers (inward connector, missing keepout, bad pad ref) stop the
    loop — scale cannot fix design intent (napkin: policy_rounds first).
    True = the ladder is EXHAUSTED: every scale was tried, or a looser retry
    no longer fits (`last_good`, the last routed attempt, is restored), without
    a finished route (the on_exhausted case). Agent mode stops at the first
    scale that passes G3 without routing (rp_route_modes.hold_for_agent);
    auto mode notes every routed attempt."""
    scales = ladder_scales(ctx) if scales is None else scales
    rp_route_modes.start_ladder(ctx)
    for i, scale in enumerate(scales):
        attempt, after = first + i, (scales[i + 1] if i + 1 < len(scales) else None)
        rb = build_board(ctx, scale)
        if not rb.get("ok") and last_good is not None:
            restore_attempt(ctx, last_good, attempt, scale)
            return True
        ctx.g3 = placement_gate(ctx, rb, attempt)
        if ctx.napkin:
            policy_rounds(ctx, attempt, scale)
        if not ctx.g3["passed"]:
            if not scale_can_fix(ctx.g3):
                return False            # design intent: no scale can fix it
            if after is None:
                return True
            ctx.log_remediation("G3", attempt, [f"re-placed at pack_scale {after}"])
            continue
        if rp_route_modes.agent_takes_over(ctx):
            rp_route_modes.hold_for_agent(ctx, attempt, scale)
            return False
        done = rp_route.route_attempt(ctx, attempt, scale)
        rp_route_modes.note_attempt(ctx, attempt, scale)
        last_good = rp_context.Attempt(scale, ctx.g3, ctx.board_rep, ctx.route_rep)
        if done:
            return False
        if after is not None:
            ctx.log_remediation("G4", attempt, [
                f"{ctx.route_rep['unconnected']} unrouted -> re-placed at "
                f"pack_scale {after} and re-routed"])
    return True


def build_board(ctx: rp_context.RunContext, scale: float) -> dict:
    """Place the board at `scale` (the SWIG builder); ctx.board_rep keeps its summary."""
    rb = k.build_board_from_netlist(ctx.netlist, ctx.w, ctx.h, ctx.pcb, "", ctx.placement,
                                    scale)
    ctx.board_rep = {kk: rb.get(kk) for kk in rp_context.BOARD_REP_KEYS}
    return rb


def policy_rounds(ctx: rp_context.RunContext, attempt: int, scale: float) -> None:
    """Napkin: while G3 fails with a face-out / thermal-swap task, at most
    rp_napkin.G3_POLICY_ROUNDS rounds at THIS pack_scale: apply the policy
    (rp_napkin.g3_policy_round: netlist rewritten, decisions recorded), refresh
    the route-cache key, rebuild and re-judge G3 (the g3 hooks included)."""
    for _ in range(rp_napkin.G3_POLICY_ROUNDS):
        if ctx.g3["passed"] or not rp_napkin.g3_policy_round(ctx, attempt):
            return
        ctx.rhash = rp_route_hash.route_hash(ctx.design, ctx.netlist_dir)
        rb = build_board(ctx, scale)
        ctx.g3 = placement_gate(ctx, rb, attempt)


def scale_can_fix(verdict: dict) -> bool:
    """A failed G3 a looser pack_scale may clear: every task auto and none a
    napkin connector / footprint task (rp_napkin.POLICY_FIXES) left open."""
    tasks = verdict.get("tasks") or []
    return (bool(tasks) and all(t["auto"] for t in tasks)
            and not any((t.get("fix") or {}).get("type") in rp_napkin.POLICY_FIXES
                        for t in tasks))


def restore_attempt(ctx: rp_context.RunContext, good: rp_context.Attempt, attempt: int,
                    scale: float) -> None:
    """The RETRY made things worse (a looser pack no longer fits a fixed
    board). Restore the last attempt's ROUTED board from its snapshot —
    rebuilding would wipe the routing — and stop escalating; its own gate
    verdict stands."""
    ctx.g3, ctx.board_rep, ctx.route_rep = good.g3, good.board_rep, good.route_rep
    snap = Path(ctx.pcb + rp_context.ATTEMPT_SUFFIX)
    if snap.is_file():
        Path(ctx.pcb).write_bytes(snap.read_bytes())
    ctx.log_remediation("G3", attempt, [
        f"pack_scale {scale} does not fit the fixed board -> restored routed board "
        f"at pack_scale {good.scale}"])


def placement_gate(ctx: rp_context.RunContext, rb: dict, attempt: int) -> dict:
    """G3 verdict on a freshly built board (an EP via farm goes in first)."""
    if rb.get("ok") and ctx.trep.get("ep_via_parts"):
        # self-remediate the G3 EP check: footprints without a ...ThermalVias
        # variant get an IPC-7093 via farm dropped under the EP before the
        # placement review runs
        farm = rp_kicad.board_fix(ctx.pcb, ctx.netlist, "ep_vias")
        if farm:
            ctx.log_remediation("G3", attempt, farm)
    cy = rp_kicad.courtyard_drc(ctx.pcb) if rb.get("ok") else {"courtyard_violations": 0}
    pl = (rp_kicad.gate_check("placement", ctx.pcb, ctx.netlist) if rb.get("ok")
          else {"issues": [], "warnings": []})
    if ctx.design.get("mounting_holes") and not rb.get("mounting_holes"):
        pl = {**pl, "issues": pl["issues"] + ["mounting holes requested but "
                                              "none placed on the board"]}
    cy, pl = rp_hooks.chain("g3_inputs", ctx, (cy, pl))
    return rp_hooks.chain("g3_verdict", ctx,
                          gates.g3_placement(rb, cy, pl["issues"], pl["warnings"],
                                             napkin=ctx.napkin))
