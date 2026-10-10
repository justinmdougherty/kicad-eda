"""Routing inside the G3 scale loop: one route of the placed board (Freerouting,
or the router a feature's route_provider hook supplies), the near-miss recovery
on the SAME placement, and the routed-board snapshot a failed retry restores.

With the canonical DSN (dsn_canon, F5-02) Freerouting is deterministic per
placement: the same placed board routes the same way on every run, so a near
miss (1..NEAR_MISS_UNCONNECTED unrouted) is recovered deterministically on this
placement before the scale loop re-places at the next pack_scale (which changes
the placement, and so the route):
  1. ALT_DSN_SEEDS: the placed board routed again with deterministic alternate
     DSN orders (T07: 29 of 32 orders routed buck_5v3a completely); the first
     seed that routes everything wins, else the best routed copy is kept
     (fewest unrouted, then the lowest seed, the canonical order 0 included);
  2. the micro-gap heal, accepted only when the healed board is better: DRC
     unconnected 0, no more DRC errors than before, no new routed-check issue
     and no 'OPEN:' finding; otherwise the routed board is restored byte for
     byte and the scale loop escalates.
Both apply only to Freerouting: a route_provider is called once per attempt.

Files beside the board (git-ignored):
  <pcb>.placed    the placed board before the first route (every alternate starts there)
  <pcb>.alt<s>    the board routed with dsn_seed s (0 = the canonical order)
  <pcb>.preheal   the routed board the heal started from (restored on a rollback)
  <pcb>.attempt   the last ROUTED board of the scale loop (rp_context.ATTEMPT_SUFFIX)

ctx.route_rep = {attempt, pack_scale, tracks, unconnected[, dsn_sha256, dsn_seed]
[, alternates: [{seed, unconnected, tracks, dsn_sha256[, error]}, ...]][, error]};
the dsn stamp is the router's (board_autoroute) for the KEPT board, alternates
lists every DSN order routed at a near miss, the canonical one first. The
<stem>.dsn / <stem>.ses on disk are those of the last order routed, which after
a near miss need not be the kept one (compare their sha256 with alternates)."""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Callable

import rp_paths  # noqa: F401  first: puts server/ and server/tools/ on sys.path
import kicad_server as k
import rp_context
import rp_hooks
import rp_kicad

NEAR_MISS_UNCONNECTED = 4     # 1..4 unrouted = same-net micro-gaps: recover in place
AUTOROUTE_MAX_PASSES, AUTOROUTE_TIMEOUT_S = 600, 1500
ALT_DSN_SEEDS: tuple[int, ...] = (1, 2)   # alternate DSN orders tried on a near miss, in order
PLACED_SUFFIX = ".placed"
PREHEAL_SUFFIX = ".preheal"
ALT_SUFFIX = ".alt"
DSN_STAMP = ("dsn_sha256", "dsn_seed")    # what board_autoroute stamps on its result
HEAL_ROLLED_BACK = "heal rolled back: "


def route_attempt(ctx: rp_context.RunContext, attempt: int, scale: float) -> bool:
    """Route the placed board. True = stop escalating: fully routed, recovered
    in place (alternate DSN order or verified heal), or the ROUTER failed
    outright (placement is not the problem, so scale escalation cannot help —
    the error surfaces at G4). A feature's route_provider replaces
    k.autoroute_board: same call, same result, no alternates and no heal."""
    ctx.report["renders_placement"] = rp_kicad.render(ctx.pcb, "place")
    provider = rp_hooks.first("route_provider", ctx)
    if provider is None:
        copy_board(ctx.pcb, ctx.pcb + PLACED_SUFFIX)
    router: Callable[..., dict] = provider or k.autoroute_board
    ra = router(ctx.pcb, AUTOROUTE_MAX_PASSES, "", AUTOROUTE_TIMEOUT_S)
    ctx.route_rep = {"attempt": attempt, "pack_scale": scale, **route_fields(ra)}
    snapshot_routed(ctx.pcb)   # so a failed retry-build can restore it
    unconnected = ra.get("unconnected")
    if unconnected is None:
        ctx.route_rep = {**ctx.route_rep, "error": router_error(ra)}
        return True
    if unconnected == 0:
        return True
    if provider is not None or unconnected > NEAR_MISS_UNCONNECTED:
        return False
    return route_alternates(ctx, attempt, ra) or heal_near_miss(ctx, attempt)


def route_fields(ra: dict) -> dict:
    """The route_rep fields of one router result: tracks, unconnected and the
    router's DSN stamp when it carries one."""
    return {"tracks": ra.get("tracks"), "unconnected": ra.get("unconnected"),
            **{key: ra[key] for key in DSN_STAMP if key in ra}}


def router_error(ra: dict) -> str:
    return ra.get("error") or ra.get("reason") or ra.get("stderr") or "autoroute failed"


# --- 1. alternate DSN orders of the same placement -----------------------------------

def route_alternates(ctx: rp_context.RunContext, attempt: int, first: dict) -> bool:
    """Near miss with the canonical DSN: route the placed board with each
    ALT_DSN_SEEDS order (one remediation entry per seed), stop at the first
    that routes everything, else keep the best routed copy (fewest unrouted,
    then the lowest seed). True when the kept board is fully routed."""
    pcb = ctx.pcb
    copy_board(pcb, alt_path(pcb, 0))
    tried = [alt_entry(0, first)]
    for seed in ALT_DSN_SEEDS:
        tried.append(alt_entry(seed, route_alternate(pcb, seed)))
        ctx.log_remediation("G4", attempt, [alternate_note(first, tried[-1])])
        if tried[-1]["unconnected"] == 0:
            break
    best = min((e for e in tried if e["unconnected"] is not None),
               key=lambda e: (e["unconnected"], e["seed"]))
    copy_board(alt_path(pcb, best["seed"]), pcb)
    ctx.route_rep = {**ctx.route_rep, "tracks": best["tracks"],
                     "unconnected": best["unconnected"], "dsn_sha256": best["dsn_sha256"],
                     "dsn_seed": best["seed"], "alternates": tried}
    snapshot_routed(pcb)
    return best["unconnected"] == 0


def route_alternate(pcb: str, seed: int) -> dict:
    """Restore the placed board and route it with alternate DSN order `seed`;
    a routed result is kept as <pcb>.alt<seed> (an older copy never is)."""
    keep = Path(alt_path(pcb, seed))
    keep.unlink(missing_ok=True)
    copy_board(pcb + PLACED_SUFFIX, pcb)
    ra = k.autoroute_board(pcb, AUTOROUTE_MAX_PASSES, "", AUTOROUTE_TIMEOUT_S, dsn_seed=seed)
    if ra.get("unconnected") is not None:
        copy_board(pcb, str(keep))
    return ra


def alt_entry(seed: int, ra: dict) -> dict:
    """route_rep['alternates'] entry of one DSN order (error when it failed)."""
    entry = {"seed": seed, "unconnected": ra.get("unconnected"), "tracks": ra.get("tracks"),
             "dsn_sha256": ra.get("dsn_sha256")}
    return entry if entry["unconnected"] is not None else {**entry, "error": router_error(ra)}


def alternate_note(first: dict, entry: dict) -> str:
    result = (f"{entry['unconnected']} unrouted" if entry["unconnected"] is not None
              else f"the router failed ({entry['error']})")
    return (f"near miss: {first.get('unconnected')} unrouted with the canonical DSN order -> "
            f"re-routed the placed board with alternate DSN order dsn_seed {entry['seed']}: "
            f"{result}")


def alt_path(pcb: str, seed: int) -> str:
    return f"{pcb}{ALT_SUFFIX}{seed}"


# --- 2. the verified heal -------------------------------------------------------------

def heal_near_miss(ctx: rp_context.RunContext, attempt: int) -> bool:
    """Heal the near miss the alternates left: same-net micro-gaps are
    router/SES artifacts. With the canonical DSN Freerouting is deterministic
    per placement (re-routing the same placement repeats the route); a rebuild
    at a new pack_scale changes the placement, so it is the fallback when the
    heal is not accepted. Accepted only when it made the board better (DRC
    unconnected 0, DRC errors <= before, routed-check issues a subset of
    before, no 'OPEN:' finding); otherwise <pcb>.preheal is restored byte for
    byte, 'heal rolled back: <reason>' is logged and False lets the scale loop
    escalate. True when healed."""
    pcb = ctx.pcb
    before = routed_findings(ctx)          # its DRC writes the report the healer reads
    copy_board(pcb, pcb + PREHEAL_SUFFIX)
    healed = rp_kicad.board_fix(pcb, ctx.netlist, "heal")
    reason = heal_refusal(healed)
    if reason is None:
        reason = heal_regression(before, routed_findings(ctx))
    if reason is not None:
        copy_board(pcb + PREHEAL_SUFFIX, pcb)
        if healed:
            ctx.log_remediation("G4", attempt, [HEAL_ROLLED_BACK + reason])
        return False
    ctx.log_remediation("G4", attempt, healed)
    ctx.route_rep = {**ctx.route_rep, "unconnected": 0}
    snapshot_routed(pcb)
    return True


def routed_findings(ctx: rp_context.RunContext) -> dict:
    """What the heal is judged on: this board's DRC, then its routed-check issues."""
    drc = rp_kicad.drc(ctx.pcb)
    routed = rp_kicad.gate_check("routed", ctx.pcb, ctx.netlist)
    return {"drc": drc, "issues": list(routed.get("issues", []))}


def heal_refusal(healed: list) -> str | None:
    """Why the healer's own answer is not a heal: an 'OPEN:' finding (a break
    left open, e.g. no clear path at the class width) or nothing applied."""
    open_items = [str(h) for h in healed if str(h).lstrip().startswith(rp_context.OPEN_PREFIX)]
    if open_items:
        return f"the healer left {len(open_items)} break(s) open ({'; '.join(open_items)})"
    return None if healed else "the healer applied nothing"


def heal_regression(before: dict, after: dict) -> str | None:
    """Why the healed board is not better than the routed one, or None."""
    problems = []
    if after["drc"]["unconnected"]:
        problems.append(f"{after['drc']['unconnected']} unconnected item(s) left after the heal")
    was, now = before["drc"]["errors"], after["drc"]["errors"]
    if now > was:
        types = ", ".join(f"{t} x{n}" for t, n in sorted(after["drc"].get("types", {}).items()))
        problems.append(f"the heal added DRC error(s) ({was} -> {now}: {types})")
    new = [issue for issue in after["issues"] if issue not in before["issues"]]
    if new:
        problems.append(f"the heal added routed-check issue(s): {'; '.join(new)}")
    return "; ".join(problems) or None


# --- board copies ---------------------------------------------------------------------

def copy_board(src: str, dst: str) -> None:
    """Byte-exact copy of a board file (a failure is loud: a restore point the
    loop relies on must exist)."""
    shutil.copyfile(src, dst)


def snapshot_routed(pcb: str) -> None:
    # best effort (as before the refactor): without it a restore leaves the board as is
    try:
        Path(pcb + rp_context.ATTEMPT_SUFFIX).write_bytes(Path(pcb).read_bytes())
    except OSError:
        pass
