"""Routing inside the G3 scale loop: one Freerouting attempt on the placed
board (or the router a feature's route_provider hook supplies), the near-miss
heal, and the routed-board snapshot a failed retry restores."""
from __future__ import annotations

from pathlib import Path

import rp_paths  # noqa: F401  first: puts server/ and server/tools/ on sys.path
import kicad_server as k
import rp_context
import rp_hooks
import rp_kicad

NEAR_MISS_UNCONNECTED = 4     # 1..4 unrouted = same-net micro-gaps: heal, don't re-place
AUTOROUTE_MAX_PASSES, AUTOROUTE_TIMEOUT_S = 600, 1500


def route_attempt(ctx: rp_context.RunContext, attempt: int, scale: float) -> bool:
    """Freerouting on the placed board. True = stop escalating: fully routed,
    healed in place, or the ROUTER failed outright (placement is not the
    problem, so scale escalation cannot help — the error surfaces at G4). A
    feature's route_provider replaces k.autoroute_board: same call, same result."""
    ctx.report["renders_placement"] = rp_kicad.render(ctx.pcb, "place")
    router = rp_hooks.first("route_provider", ctx) or k.autoroute_board
    ra = router(ctx.pcb, AUTOROUTE_MAX_PASSES, "", AUTOROUTE_TIMEOUT_S)
    unconnected = ra.get("unconnected")
    ctx.route_rep = {"attempt": attempt, "pack_scale": scale,
                     "tracks": ra.get("tracks"), "unconnected": unconnected}
    snapshot_routed(ctx.pcb)   # so a failed retry-build can restore it
    if unconnected is None:
        ctx.route_rep["error"] = (ra.get("error") or ra.get("reason")
                                  or ra.get("stderr") or "autoroute failed")
        return True
    if unconnected == 0:
        return True
    return unconnected <= NEAR_MISS_UNCONNECTED and heal_near_miss(ctx, attempt)


def heal_near_miss(ctx: rp_context.RunContext, attempt: int) -> bool:
    """Near-miss: same-net micro-gaps are router/SES artifacts, not a
    placement problem — heal in place instead of escalating scale (the router
    is deterministic; a rebuild can't clear them). True when fully healed."""
    rp_kicad.drc(ctx.pcb)              # writes the report the fixer reads
    healed = rp_kicad.board_fix(ctx.pcb, ctx.netlist, "heal")
    if not healed:
        return False
    ctx.log_remediation("G4", attempt, healed)
    if rp_kicad.drc(ctx.pcb)["unconnected"] != 0:
        return False
    ctx.route_rep["unconnected"] = 0
    snapshot_routed(ctx.pcb)
    return True


def snapshot_routed(pcb: str) -> None:
    # best effort (as before the refactor): without it a restore leaves the board as is
    try:
        Path(pcb + rp_context.ATTEMPT_SUFFIX).write_bytes(Path(pcb).read_bytes())
    except OSError:
        pass
