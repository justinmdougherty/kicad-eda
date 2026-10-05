"""Harness stages between G2 and G4: the route cache, then G3 placement and
the pack_scale ladder that places, gates and routes (or re-judges a cached
routed board). Feature hooks (rp_hooks): pack_scales, g3_inputs, g3_verdict,
after_route, on_exhausted (route_provider is rp_route's)."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import rp_paths  # noqa: F401  first: puts server/ and server/tools/ on sys.path
import gates
import kicad_server as k
import toolchain as tc
import rp_context
import rp_hooks
import rp_kicad
import rp_route
import rp_route_hash

# auto-placement looseness ladder (a placement override gets one try)
PACK_SCALES: tuple[float, ...] = (1.0, 1.5, 2.0, 2.6, 3.2)


@rp_context.stage
def stage_route_cache(ctx: rp_context.RunContext) -> bool:
    """ROUTE CACHE: when nothing that moves copper changed since the last
    successful run, reuse the routed board and skip build+route entirely —
    the G3/G4 GATES still re-run against the cached artifact (the cache skips
    work, never judgement). Hashed AFTER G2 so a G2 netlist remediation
    correctly invalidates. PIPELINE_NO_CACHE=1 bypasses. Never stops the run."""
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
        ctx.report["route_cache_reason"] = reason
    return True


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
    passed G3 with 0 unconnected goes to the after_route hook before G4."""
    if ctx.use_cache:
        revalidate_cached_board(ctx)
    else:
        preserve_unreadable_board(ctx)
        if place_and_route(ctx) and rp_hooks.first("on_exhausted", ctx):
            rerun_after_exhaustion(ctx)
    ctx.report["board"] = ctx.board_rep
    ctx.report["autoroute"] = ctx.route_rep
    passed = rp_context.record_gate(ctx, ctx.g3)
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
                            gates.g3_placement(ctx.board_rep, cy, pl["issues"], pl["warnings"]))
    ctx.route_rep = {"cached": True,
                     "pack_scale": ctx.cached.get("pack_scale"),
                     "tracks": ctx.cached.get("tracks"),
                     "unconnected": ctx.cached.get("unconnected", 0)}


def preserve_unreadable_board(ctx: rp_context.RunContext) -> None:
    """The build below overwrites the board. One saved by a KiCad newer than
    the pcbnew install (agent routing through the KiCad 11 API) is the
    agent's work: copy it (and its project) to <stem>.agent.* first, and
    refuse to go on when that copy cannot be made."""
    pcb = Path(ctx.pcb)
    if not pcb.is_file() or tc.swig_readable(ctx.pcb) is None:
        return
    keep = Path(ctx.base + rp_context.AGENT_SUFFIX + ".kicad_pcb")
    pro = Path(ctx.base + ".kicad_pro")
    try:
        shutil.copy2(pcb, keep)
        if pro.is_file():
            shutil.copy2(pro, ctx.base + rp_context.AGENT_SUFFIX + ".kicad_pro")
    except OSError as e:
        raise RuntimeError(f"{pcb.name} was saved by a newer KiCad and could not be "
                           f"preserved as {keep.name} before the rebuild: {e}") from e
    ctx.report["agent_board_preserved"] = str(keep)


def place_and_route(ctx: rp_context.RunContext) -> bool:
    """The G3 -> route scale loop. A G3 failure whose tasks are all auto
    ("loosen placement") re-places at the next pack_scale and re-tests; a
    routing shortfall does the same (the deterministic-router escape).
    Manual blockers (inward connector, missing keepout, bad pad ref) stop the
    loop — scale cannot fix design intent. True = the ladder is EXHAUSTED:
    every scale was tried, or a looser retry no longer fits, without a
    finished route (the on_exhausted case)."""
    scales = (1.0,) if ctx.placement else tuple(
        rp_hooks.first("pack_scales", ctx, PACK_SCALES) or PACK_SCALES)
    last_good: rp_context.Attempt | None = None
    for attempt, scale in enumerate(scales, 1):
        rb = k.build_board_from_netlist(ctx.netlist, ctx.w, ctx.h, ctx.pcb, "",
                                        ctx.placement, scale)
        ctx.board_rep = {kk: rb.get(kk) for kk in rp_context.BOARD_REP_KEYS}
        if not rb.get("ok") and last_good is not None:
            restore_attempt(ctx, last_good, attempt, scale)
            return True
        ctx.g3 = placement_gate(ctx, rb, attempt)
        if not ctx.g3["passed"]:
            if not (ctx.g3["tasks"] and all(t["auto"] for t in ctx.g3["tasks"])):
                return False            # design intent: no scale can fix it
            if attempt == len(scales):
                return True
            ctx.log_remediation("G3", attempt, [f"re-placed at pack_scale {scales[attempt]}"])
            continue
        done = rp_route.route_attempt(ctx, attempt, scale)
        last_good = rp_context.Attempt(scale, ctx.g3, ctx.board_rep, ctx.route_rep)
        if done:
            return False
        if attempt < len(scales):
            ctx.log_remediation("G4", attempt, [
                f"{ctx.route_rep['unconnected']} unrouted -> re-placed at "
                f"pack_scale {scales[attempt]} and re-routed"])
    return True


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
                          gates.g3_placement(rb, cy, pl["issues"], pl["warnings"]))
