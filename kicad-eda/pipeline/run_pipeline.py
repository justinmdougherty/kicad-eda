#!/usr/bin/env python3
"""Gated pipeline harness: drive a netlist through the concept->Gerbers flow
with FORMAL GO/NO-GO GATES between stages (IEEE 15288.2 review structure).
The pipeline halts at the first failing gate — a design with open blockers
never proceeds to the next stage, so an error caught at the schematic gate
can never survive to fabrication.

  G1 Requirements (SRR)  ->  G2 Schematic (PDR)  ->  G3 Placement (hard gate)
  ->  G4 Layout (CDR)  ->  G5 Fab package (release)

run() walks STAGES, functions of one shared RunContext that return False to
stop; _halt is the single exit and writes <stem>_pipeline_report.json next to
the netlist on every exit (pass, gate failure or crash).

Usage:
  <venv-python> run_pipeline.py <netlist.json> [<board_w> <board_h>] [placement.json]
  (no dims: the netlist "board" w/h, else the board is auto-sized to its parts)
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, NamedTuple

SERVER = Path(__file__).resolve().parents[1] / "server"
sys.path.insert(0, str(SERVER))
sys.path.insert(0, str(SERVER / "tools"))
import kicad_server as k       # noqa: E402
import toolchain as tc         # noqa: E402
import lint_netlist as lint    # noqa: E402
import design_report as drpt   # noqa: E402
import gates                   # noqa: E402
import remediate               # noqa: E402
import thermal_check           # noqa: E402

USAGE = "usage: run_pipeline.py <netlist.json> [<board_w> <board_h>] [placement.json]"
# auto-placement looseness ladder (a placement override gets one try)
PACK_SCALES: tuple[float, ...] = (1.0, 1.5, 2.0, 2.6, 3.2)
NEAR_MISS_UNCONNECTED = 4     # 1..4 unrouted = same-net micro-gaps: heal, don't re-place
AUTOROUTE_MAX_PASSES, AUTOROUTE_TIMEOUT_S = 600, 1500
G2_ROUNDS, G4_FIX_ROUNDS = 3, 2                  # corrective-action rounds per gate
# swig_build_board's defaults; an auto-placed board is sized to its parts
AUTO_BOARD_DIMS: tuple[float, float] = (50.0, 25.0)
REPORT_SUFFIX = "_pipeline_report.json"
CACHE_SUFFIX = ".cache.json"
ATTEMPT_SUFFIX = ".attempt"   # <pcb>.attempt: the last ROUTED board of the scale loop
AGENT_SUFFIX = ".agent"       # <stem>.agent.kicad_pcb/.kicad_pro: a preserved KiCad 11 board
SCHEMATIC_REP_KEYS = ("ok", "backend", "components", "nets", "erc_ok", "erc_violations")
BOARD_REP_KEYS = ("ok", "footprints", "pads_assigned", "zones", "antenna_keepout",
                  "mounting_holes", "missing_pads", "error", "reason", "stderr")

# ---- route-cache key: a DECLARATIVE registry of what moves copper ----------
# Per-component: position (x/y/rot, or the D3 origin anchor "at"), footprint,
# side and the packer hints (interior, edge_row, overhang past Edge.Cuts).
ROUTE_HASH_COMPONENT_KEYS: tuple[str, ...] = (
    "ref", "footprint", "x", "y", "rot", "side", "interior", "edge_row", "overhang", "at")
# Design-level paths; a dotted path hashes ONE key of a block whose other keys
# are review-only (thermal.ep_vias_min sizes the EP via farm; t_ambient and
# tj_margin only feed G2). planes = inner pours, stackup = impedance widths.
ROUTE_HASH_DESIGN_KEYS: tuple[str, ...] = (
    "nets", "net_specs", "preroutes", "board", "mounting_holes", "antenna_keepout",
    "class", "emc", "planes", "stackup", "copper_layers", "thermal.ep_vias_min")
# Deliberately NOT hashed — they never move copper, and the G3/G4 gates re-run
# against a cached board anyway: height_zones is a G3 check, not a placement
# input; the design-level thermal block, part values and sourcing feed G2/G5.
ROUTE_HASH_EXCLUDED: tuple[str, ...] = (
    "height_zones", "thermal", "title", "name", "require_sourcing", "no_connect",
    "power_flags", "assumptions", "mode")


def _board_cli(pcb: str) -> str:
    """The kicad-cli for BOARD jobs on `pcb`: the one of the KiCad that wrote
    the file (toolchain.board_cli: the pcbnew install for a SWIG-built board,
    never a newer nightly's). Loud when absent."""
    cli = tc.board_cli(pcb)
    if not cli:
        raise RuntimeError("kicad-cli not found; install KiCad or set KICAD_PATH")
    return cli


def _drc(pcb: str) -> dict:
    rpt = str(Path(pcb).with_suffix("")) + "_drc.json"
    cli = _board_cli(pcb)
    subprocess.run([cli, "pcb", "drc", pcb, "--severity-error",
                    "-o", rpt, "--format", "json"], capture_output=True, text=True, stdin=subprocess.DEVNULL)
    d = json.loads(Path(rpt).read_text())
    return {"errors": len(d.get("violations", [])),
            "unconnected": len(d.get("unconnected_items", [])),
            "types": _counts(d.get("violations", [])),
            "kicad_cli": cli}


def _counts(viols: list) -> dict:
    out: dict[str, int] = {}
    for v in viols:
        out[v.get("type", "?")] = out.get(v.get("type", "?"), 0) + 1
    return out


def _gate_check(mode: str, pcb: str, netlist: str) -> dict:
    """Run the pcbnew-side gate checks (connector orientation / keepout /
    plane integrity / routed-width-vs-class) under KiCad's bundled Python.
    Returns {"issues": [...], "warnings": [...]}."""
    kpy = k._kicad_python()
    script = SERVER / "tools" / "swig_gate_checks.py"
    if not kpy or not script.is_file():
        return {"issues": [f"gate check unavailable ({mode})"], "warnings": []}
    r = subprocess.run([kpy, str(script), mode, pcb, netlist],
                       capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=300)
    for line in reversed(r.stdout.strip().splitlines()):
        if line.startswith("{"):
            try:
                d = json.loads(line)
                return {"issues": d.get("issues", []),
                        "warnings": d.get("warnings", [])}
            except Exception:
                break
    return {"issues": [f"gate check produced no result ({mode}): "
                       f"{r.stderr[-300:]}"], "warnings": []}


def _board_fix(pcb: str, netlist: str, mode: str) -> list:
    """Apply post-route board-level corrective actions (micro-gap heal,
    IPC-2221 parallel vias) under KiCad's Python. Returns the applied log."""
    kpy = k._kicad_python()
    script = SERVER / "tools" / "swig_board_fixes.py"
    drc_rpt = str(Path(pcb).with_suffix("")) + "_drc.json"
    if not kpy or not script.is_file():
        return []
    r = subprocess.run([kpy, str(script), pcb, netlist, drc_rpt, mode],
                       capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=300)
    for line in reversed(r.stdout.strip().splitlines()):
        if line.startswith("{"):
            try:
                d = json.loads(line)
                return d.get("applied", []) + \
                    [f"OPEN: {o}" for o in d.get("open", [])]
            except Exception:
                break
    return []


def _dig(design: dict, path: str) -> Any:
    """The value at a dotted registry path ("thermal.ep_vias_min"); None when
    a level is absent or is not a JSON object."""
    node: Any = design
    for part in path.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    return node


def _route_hash(design: dict) -> str:
    """Hash of everything that determines PLACEMENT + ROUTING (the
    ROUTE_HASH_* registry above). A change to a value, sourcing field, or
    thermal annotation does not move copper — only registry keys force the
    expensive build+route stages to re-run."""
    comps = sorted(({key: c.get(key) for key in ROUTE_HASH_COMPONENT_KEYS if key in c}
                    for c in design.get("components", [])),
                   key=lambda c: c["ref"])
    payload = {"components": comps,
               **{path: _dig(design, path) for path in ROUTE_HASH_DESIGN_KEYS}}
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _sourcing(design: dict) -> dict:
    """Offline BOM sourcing audit for G5: every assembly-relevant part
    should carry an LCSC number or an MPN; LCSC numbers must look like
    Cxxxxx. Test points / mounting holes / fiducials are exempt. The
    netlist 'require_sourcing': true upgrades findings to blockers."""
    import re
    missing, bad = [], []
    for c in design.get("components", []):
        ref = c["ref"]
        if ref.startswith(("TP", "H", "FID")):
            continue
        lcsc = str(c.get("lcsc", "") or "")
        mpn = str(c.get("mpn", "") or "")
        if not lcsc and not mpn:
            missing.append(ref)
        if lcsc and not re.match(r"^C\d{3,}$", lcsc):
            bad.append(f"{ref}: lcsc '{lcsc}' is not Cxxxxx format")
    return {"missing": missing, "bad": bad,
            "required": bool(design.get("require_sourcing"))}


def _render(pcb: str, tag: str, quality: str = "basic") -> list:
    """Render top/bottom PNGs next to the board so every gate exit leaves a
    visual record (placement view at G3, final board at G5)."""
    out = []
    base = str(Path(pcb).with_suffix(""))
    cli = _board_cli(pcb)
    for side in ("top", "bottom"):
        png = f"{base}_{tag}_{side}.png"
        try:
            r = subprocess.run(
                [cli, "pcb", "render", pcb, "-o", png,
                 "--side", side, "--quality", quality,
                 "--width", "1600", "--height", "1400"],
                capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=300)
            if r.returncode == 0 and Path(png).is_file():
                out.append(png)
        except Exception:
            pass
    return out


def _emc_stitch(pcb: str, emc: dict) -> dict:
    """Run the EMC ground-stitching pass (perimeter via fence + interior
    grid) on the routed board under KiCad's Python."""
    kpy = k._kicad_python()
    script = SERVER / "tools" / "emc_stitch.py"
    if not kpy or not script.is_file():
        return {"ok": False, "reason": "emc_stitch unavailable"}
    r = subprocess.run(
        [kpy, str(script), pcb, str(emc.get("max_freq_hz", 0)),
         "fence" if emc.get("fence_only") else "grid"],
        capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=600)
    for line in reversed(r.stdout.strip().splitlines()):
        if line.startswith("{"):
            try:
                return json.loads(line)
            except Exception:
                break
    return {"ok": False, "reason": r.stderr[-300:]}


def _courtyard_drc(pcb: str) -> dict:
    """Placement-stage DRC: count only courtyard violations (routing-dependent
    types are G4's job and would false-flag an unrouted board)."""
    d = _drc(pcb)
    n = sum(v for t, v in d["types"].items() if "courtyard" in t)
    return {"courtyard_violations": n, "types": d["types"]}


# ---- the run context every stage shares ------------------------------------
@dataclass
class RunContext:
    """What the stages share. Inputs, derived paths and mode flags are fixed
    when the run starts; the stages fill in the rest. `design` is the netlist
    as the stages see it (a G2 remediation replaces it and rewrites the file);
    `report` is the JSON report _halt finalises and persists."""
    netlist: str
    w: float
    h: float
    placement: str = ""
    no_cache: bool = field(default_factory=lambda: bool(os.environ.get("PIPELINE_NO_CACHE")))
    design: dict = field(default_factory=dict)
    syms: str = ""
    report: dict = field(default_factory=dict)
    remediation: list[dict] = field(default_factory=list)
    trep: dict = field(default_factory=dict)          # G2 thermal review (G3 reads ep_via_parts)
    rhash: str = ""
    cached: dict = field(default_factory=dict)        # the last successful run's cache manifest
    use_cache: bool = False
    board_rep: dict = field(default_factory=dict)
    route_rep: dict = field(default_factory=dict)
    drc_rep: dict = field(default_factory=dict)
    routed_check: dict = field(default_factory=lambda: {"issues": [], "warnings": []})
    g3: dict = field(default_factory=dict)
    g4: dict = field(default_factory=dict)
    base: str = field(init=False)                     # the netlist path without .json
    sch: str = field(init=False)
    pcb: str = field(init=False)
    cache_path: Path = field(init=False)
    report_path: Path = field(init=False)

    def __post_init__(self) -> None:
        self.base = str(Path(self.netlist).with_suffix(""))
        self.sch, self.pcb = self.base + ".kicad_sch", self.base + ".kicad_pcb"
        self.cache_path = Path(self.base + CACHE_SUFFIX)
        self.report_path = Path(self.base + REPORT_SUFFIX)
        if not self.report:
            self.report = {"netlist": self.netlist, "gates": []}

    def log_remediation(self, gate: str, rnd: int, applied: list) -> None:
        """Record one corrective action round; the report carries the same log."""
        self.remediation.append({"gate": gate, "round": rnd, "applied": applied})
        self.report["remediation"] = self.remediation


class _Attempt(NamedTuple):
    """The scale loop's last ROUTED attempt (restored when a retry no longer fits)."""
    scale: float
    g3: dict
    board_rep: dict
    route_rep: dict


def _record_gate(ctx: RunContext, verdict: dict) -> bool:
    ctx.report["gates"].append(verdict)
    return verdict["passed"]


def _halt(ctx: RunContext, error: str | None = None) -> dict:
    """The single exit of run(): final verdict + summary, written to
    <stem>_pipeline_report.json on every exit (pass, gate failure, or `error`
    for a crash). A write failure is flagged in the report and on stderr."""
    report, path = ctx.report, ctx.report_path
    if error is not None:
        report["error"] = error
    report["PASS"] = (error is None and bool(report["gates"])
                      and all(g["passed"] for g in report["gates"]))
    report["gate_summary"] = gates.summarize(report["gates"])
    report["pipeline_report"] = str(path)
    try:
        path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    except OSError as e:
        del report["pipeline_report"]
        report["pipeline_report_error"] = f"could not write {path}: {e}"
        print(f"run_pipeline: could not write {path}: {e}", file=sys.stderr)
    return report


# ---- stages (run() order: STAGES below) -------------------------------------
def _stage_load(ctx: RunContext) -> None:
    ctx.design = json.loads(Path(ctx.netlist).read_text(encoding="utf-8"))
    share = k._kicad_share()
    ctx.syms = str(share / "symbols") if share else ""


def _stage_g1(ctx: RunContext) -> bool:
    """G1: requirements / netlist review (feeds SRR)."""
    lrep = lint.lint(ctx.design, ctx.syms)
    ctx.report["lint"] = {"ok": lrep["ok"], "errors": lrep["errors"],
                          "warnings": lrep["warnings"]}
    return _record_gate(ctx, gates.g1_requirements(lrep, ctx.design))


def _stage_g2(ctx: RunContext) -> bool:
    """G2: schematic review (feeds PDR), with a corrective-action loop. A
    failing gate translates its blockers into tasks; the auto-resolvable ones
    (e.g. a missing bypass cap) are APPLIED to the design, the netlist is
    rewritten, the stage re-runs, and the gate is re-tested. Only tasks
    needing design intent stop the flow — with the open task list attached."""
    g2: dict = {}
    for rnd in range(1, G2_ROUNDS + 1):
        g2 = _g2_review(ctx)
        if g2["passed"]:
            break
        fixed, log = remediate.apply(ctx.design, g2["tasks"])
        if not log:
            break                       # only designer tasks remain — stop here
        ctx.design = fixed
        Path(ctx.netlist).write_text(json.dumps(fixed, indent=2), encoding="utf-8")
        ctx.log_remediation("G2", rnd, log)
    return _record_gate(ctx, g2)


def _g2_review(ctx: RunContext) -> dict:
    """One G2 round: build the schematic (+ ERC), design review, thermal review."""
    r = k.build_schematic_from_netlist(ctx.netlist, ctx.sch)
    ctx.report["schematic"] = {kk: r.get(kk) for kk in SCHEMATIC_REP_KEYS}
    dr = drpt.analyze(ctx.design, ctx.syms)
    ctx.report["design_report"] = {
        "decoupling_uncovered": dr["decoupling"]["uncovered"],
        "flags": dr["flags"],
        "errors": dr.get("errors", [])}
    ctx.trep = thermal_check.check(ctx.design)
    ctx.report["thermal"] = ctx.trep
    return gates.g2_schematic(r, dr, ctx.trep)


def _stage_route_cache(ctx: RunContext) -> bool:
    """ROUTE CACHE: when nothing that moves copper changed since the last
    successful run, reuse the routed board and skip build+route entirely —
    the G3/G4 GATES still re-run against the cached artifact (the cache skips
    work, never judgement). Hashed AFTER G2 so a G2 netlist remediation
    correctly invalidates. PIPELINE_NO_CACHE=1 bypasses. Never stops the run."""
    ctx.rhash = _route_hash(ctx.design)
    try:
        cached = json.loads(ctx.cache_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cached = {}                    # no (or a corrupt) manifest: a plain miss
    ctx.cached = cached if isinstance(cached, dict) else {}
    reason = _cache_miss_reason(ctx)
    ctx.use_cache = reason is None
    ctx.report["route_cache"] = "hit" if ctx.use_cache else "miss"
    if reason:
        ctx.report["route_cache_reason"] = reason
    return True


def _cache_miss_reason(ctx: RunContext) -> str | None:
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


def _stage_place_route(ctx: RunContext) -> bool:
    """G3 + the routing it gates: place (gate) then route, self-feeding — or
    re-judge the cached routed board. Returns the G3 verdict."""
    if ctx.use_cache:
        _revalidate_cached_board(ctx)
    else:
        _preserve_unreadable_board(ctx)
        _place_and_route(ctx)
    ctx.report["board"] = ctx.board_rep
    ctx.report["autoroute"] = ctx.route_rep
    return _record_gate(ctx, ctx.g3)


def _revalidate_cached_board(ctx: RunContext) -> None:
    """G3 against the cached artifact. It is ROUTED, so the routed variant of
    the placement check runs (plane integrity is a pre-route check; G4's DRC
    re-run proves the net connectivity the plane check exists to predict)."""
    ctx.board_rep = ctx.cached.get("board_rep") or {"ok": True}
    cy = _courtyard_drc(ctx.pcb)
    pl = _gate_check("placement_routed", ctx.pcb, ctx.netlist)
    ctx.g3 = gates.g3_placement(ctx.board_rep, cy, pl["issues"], pl["warnings"])
    ctx.route_rep = {"cached": True,
                     "pack_scale": ctx.cached.get("pack_scale"),
                     "tracks": ctx.cached.get("tracks"),
                     "unconnected": ctx.cached.get("unconnected", 0)}


def _preserve_unreadable_board(ctx: RunContext) -> None:
    """The build below overwrites the board. One saved by a KiCad newer than
    the pcbnew install (agent routing through the KiCad 11 API) is the
    agent's work: copy it (and its project) to <stem>.agent.* first, and
    refuse to go on when that copy cannot be made."""
    pcb = Path(ctx.pcb)
    if not pcb.is_file() or tc.swig_readable(ctx.pcb) is None:
        return
    keep = Path(ctx.base + AGENT_SUFFIX + ".kicad_pcb")
    pro = Path(ctx.base + ".kicad_pro")
    try:
        shutil.copy2(pcb, keep)
        if pro.is_file():
            shutil.copy2(pro, ctx.base + AGENT_SUFFIX + ".kicad_pro")
    except OSError as e:
        raise RuntimeError(f"{pcb.name} was saved by a newer KiCad and could not be "
                           f"preserved as {keep.name} before the rebuild: {e}") from e
    ctx.report["agent_board_preserved"] = str(keep)


def _place_and_route(ctx: RunContext) -> None:
    """The G3 -> route scale loop. A G3 failure whose tasks are all auto
    ("loosen placement") re-places at the next pack_scale and re-tests; a
    routing shortfall does the same (the deterministic-router escape).
    Manual blockers (inward connector, missing keepout, bad pad ref) stop the
    loop — scale cannot fix design intent."""
    scales = (1.0,) if ctx.placement else PACK_SCALES
    last_good: _Attempt | None = None
    for attempt, scale in enumerate(scales, 1):
        rb = k.build_board_from_netlist(ctx.netlist, ctx.w, ctx.h, ctx.pcb, "",
                                        ctx.placement, scale)
        ctx.board_rep = {kk: rb.get(kk) for kk in BOARD_REP_KEYS}
        if not rb.get("ok") and last_good is not None:
            _restore_attempt(ctx, last_good, attempt, scale)
            return
        ctx.g3 = _placement_gate(ctx, rb, attempt)
        if not ctx.g3["passed"]:
            if (ctx.g3["tasks"] and all(t["auto"] for t in ctx.g3["tasks"])
                    and attempt < len(scales)):
                ctx.log_remediation("G3", attempt,
                                    [f"re-placed at pack_scale {scales[attempt]}"])
                continue
            return
        done = _route_attempt(ctx, attempt, scale)
        last_good = _Attempt(scale, ctx.g3, ctx.board_rep, ctx.route_rep)
        if done:
            return
        if attempt < len(scales):
            ctx.log_remediation("G4", attempt, [
                f"{ctx.route_rep['unconnected']} unrouted -> re-placed at "
                f"pack_scale {scales[attempt]} and re-routed"])


def _restore_attempt(ctx: RunContext, good: _Attempt, attempt: int, scale: float) -> None:
    """The RETRY made things worse (a looser pack no longer fits a fixed
    board). Restore the last attempt's ROUTED board from its snapshot —
    rebuilding would wipe the routing — and stop escalating; its own gate
    verdict stands."""
    ctx.g3, ctx.board_rep, ctx.route_rep = good.g3, good.board_rep, good.route_rep
    snap = Path(ctx.pcb + ATTEMPT_SUFFIX)
    if snap.is_file():
        Path(ctx.pcb).write_bytes(snap.read_bytes())
    ctx.log_remediation("G3", attempt, [
        f"pack_scale {scale} does not fit the fixed board -> restored routed board "
        f"at pack_scale {good.scale}"])


def _placement_gate(ctx: RunContext, rb: dict, attempt: int) -> dict:
    """G3 verdict on a freshly built board (an EP via farm goes in first)."""
    if rb.get("ok") and ctx.trep.get("ep_via_parts"):
        # self-remediate the G3 EP check: footprints without a ...ThermalVias
        # variant get an IPC-7093 via farm dropped under the EP before the
        # placement review runs
        farm = _board_fix(ctx.pcb, ctx.netlist, "ep_vias")
        if farm:
            ctx.log_remediation("G3", attempt, farm)
    cy = _courtyard_drc(ctx.pcb) if rb.get("ok") else {"courtyard_violations": 0}
    pl = (_gate_check("placement", ctx.pcb, ctx.netlist) if rb.get("ok")
          else {"issues": [], "warnings": []})
    if ctx.design.get("mounting_holes") and not rb.get("mounting_holes"):
        pl["issues"] = pl["issues"] + ["mounting holes requested but "
                                       "none placed on the board"]
    return gates.g3_placement(rb, cy, pl["issues"], pl["warnings"])


def _route_attempt(ctx: RunContext, attempt: int, scale: float) -> bool:
    """Freerouting on the placed board. True = stop escalating: fully routed,
    healed in place, or the ROUTER failed outright (placement is not the
    problem, so scale escalation cannot help — the error surfaces at G4)."""
    ctx.report["renders_placement"] = _render(ctx.pcb, "place")
    ra = k.autoroute_board(ctx.pcb, AUTOROUTE_MAX_PASSES, "", AUTOROUTE_TIMEOUT_S)
    unconnected = ra.get("unconnected")
    ctx.route_rep = {"attempt": attempt, "pack_scale": scale,
                     "tracks": ra.get("tracks"), "unconnected": unconnected}
    _snapshot_routed(ctx.pcb)   # so a failed retry-build can restore it
    if unconnected is None:
        ctx.route_rep["error"] = (ra.get("error") or ra.get("reason")
                                  or ra.get("stderr") or "autoroute failed")
        return True
    if unconnected == 0:
        return True
    return unconnected <= NEAR_MISS_UNCONNECTED and _heal_near_miss(ctx, attempt)


def _heal_near_miss(ctx: RunContext, attempt: int) -> bool:
    """Near-miss: same-net micro-gaps are router/SES artifacts, not a
    placement problem — heal in place instead of escalating scale (the router
    is deterministic; a rebuild can't clear them). True when fully healed."""
    _drc(ctx.pcb)                      # writes the report the fixer reads
    healed = _board_fix(ctx.pcb, ctx.netlist, "heal")
    if not healed:
        return False
    ctx.log_remediation("G4", attempt, healed)
    if _drc(ctx.pcb)["unconnected"] != 0:
        return False
    ctx.route_rep["unconnected"] = 0
    _snapshot_routed(ctx.pcb)
    return True


def _snapshot_routed(pcb: str) -> None:
    # best effort (as before the refactor): without it a restore leaves the board as is
    try:
        Path(pcb + ATTEMPT_SUFFIX).write_bytes(Path(pcb).read_bytes())
    except OSError:
        pass


def _stage_g4(ctx: RunContext) -> bool:
    """G4: layout / board review (feeds CDR): judge, fix loop, EMC post-pass."""
    _judge_g4(ctx)
    _stage_g4_fixes(ctx)
    _stage_emc(ctx)
    return _record_gate(ctx, ctx.g4)


def _judge_g4(ctx: RunContext, refresh_unconnected: bool = False) -> None:
    """DRC + routed-width check + G4 verdict on the current board."""
    ctx.drc_rep = _drc(ctx.pcb)
    ctx.report["drc"] = ctx.drc_rep
    if refresh_unconnected:
        ctx.route_rep = {**ctx.route_rep, "unconnected": ctx.drc_rep["unconnected"]}
        ctx.report["autoroute"] = ctx.route_rep
    ctx.routed_check = _gate_check("routed", ctx.pcb, ctx.netlist)
    ctx.g4 = gates.g4_layout(ctx.route_rep, ctx.drc_rep, ctx.routed_check["issues"],
                             ctx.routed_check["warnings"])


def _stage_g4_fixes(ctx: RunContext) -> None:
    """G4 corrective-action loop: board-level blockers with an unambiguous
    remedy (same-net micro-gaps, under-ampacity layer transitions) are FIXED
    on the routed board and the gate re-tested — re-routing is never required
    for them."""
    for rnd in range(1, G4_FIX_ROUNDS + 1):
        if ctx.g4["passed"]:
            return
        fixes = _board_fix(ctx.pcb, ctx.netlist, "all")
        if not any(not f.startswith("OPEN:") for f in fixes):
            return
        ctx.log_remediation("G4", rnd, fixes)
        _judge_g4(ctx, refresh_unconnected=True)


def _stage_emc(ctx: RunContext) -> None:
    """EMC post-pass (opt-in via netlist "emc": {"stitch": true, ...}):
    perimeter GND via fence + interior stitching grid (Ott/Montrose, lambda/20
    pitch from max_freq_hz), then re-DRC — the pass is clearance-aware but
    the gate trusts measurements, not intentions. Never on a cached board."""
    emc = ctx.design.get("emc") or {}
    if not (ctx.g4["passed"] and emc.get("stitch") and not ctx.use_cache):
        return
    er = _emc_stitch(ctx.pcb, emc)
    ctx.report["emc"] = er
    notes = []
    if er.get("ok"):
        notes.append(f"EMC stitching: {er.get('vias_added')} GND vias "
                     f"(fence {er.get('fence')}, interior "
                     f"{er.get('interior')}, pitch {er.get('pitch_mm')} mm)")
        ctx.drc_rep = _drc(ctx.pcb)
        ctx.report["drc"] = ctx.drc_rep
    else:
        notes.append(f"EMC stitching FAILED: {er.get('reason')}")
    if emc.get("teardrops"):
        notes.append("teardrops requested but KiCad's teardrop generator "
                     "has no headless API — generate in the GUI "
                     "(Edit > Edit Teardrops) before fab if required")
    ctx.g4 = gates.g4_layout(ctx.route_rep, ctx.drc_rep, ctx.routed_check["issues"],
                             ctx.routed_check["warnings"] + notes)


def _stage_cache_manifest(ctx: RunContext) -> bool:
    """The board is final (routed + healed + stitched): record the cache
    manifest. A write failure only costs the next run its cache hit, so it is
    reported, not fatal."""
    try:
        ctx.cache_path.write_text(json.dumps({
            "route_hash": ctx.rhash, "routed_ok": True,
            "unconnected": ctx.drc_rep.get("unconnected", 0),
            "pack_scale": ctx.route_rep.get("pack_scale"),
            "tracks": ctx.route_rep.get("tracks"),
            "board_rep": ctx.board_rep}, indent=2), encoding="utf-8")
    except OSError as e:
        ctx.report["route_cache_error"] = f"could not write {ctx.cache_path}: {e}"
    return True


def _stage_g5(ctx: RunContext) -> bool:
    """G5: fabrication package (release)."""
    r = k.export_fab_package(ctx.pcb, netlist=ctx.netlist)
    fab = {"ok": r.get("ok"), "zip": Path(r.get("zip", "")).name,
           "files": len(r.get("files", [])),
           "bom": r.get("bom"), "cpl": r.get("cpl")}
    ctx.report["fab"] = fab
    ctx.report["renders_final"] = _render(ctx.pcb, "final", quality="high")
    src = _sourcing(ctx.design)
    ctx.report["sourcing"] = src
    return _record_gate(ctx, gates.g5_fab(fab, require_assembly=True, sourcing=src))


# The gated sequence. Each stage returns False to stop the run.
STAGES: tuple[Callable[[RunContext], bool], ...] = (
    _stage_g1, _stage_g2, _stage_route_cache, _stage_place_route, _stage_g4,
    _stage_cache_manifest, _stage_g5)


def run(netlist: str, w: float, h: float, placement: str = "") -> dict:
    """Drive `netlist` through every gate (stopping at the first failure) and
    return the report; it is also written to <stem>_pipeline_report.json. A
    crash is recorded in that file (PASS false, "error") and re-raised."""
    ctx = RunContext(netlist=netlist, w=w, h=h, placement=placement)
    try:
        _stage_load(ctx)
        for stage in STAGES:
            if not stage(ctx):
                break
    except Exception as exc:
        _halt(ctx, error=f"{type(exc).__name__}: {exc}")
        raise
    return _halt(ctx)


# ---- command line -----------------------------------------------------------
class UsageError(ValueError):
    """A command line (or netlist) the harness cannot start from; main()
    prints it with the usage line and exits 2."""


class CliArgs(NamedTuple):
    netlist: str
    w: float | None
    h: float | None
    placement: str


def _is_number(text: str) -> bool:
    try:
        float(text)
    except ValueError:
        return False
    return True


def _positive_mm(value: Any) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value > 0)


def parse_cli(argv: list[str]) -> CliArgs:
    """<netlist.json> [<board_w> <board_h>] [placement.json]: the dims come
    as a pair or not at all; '' or '-' as the placement means none."""
    if not argv:
        raise UsageError("missing <netlist.json>")
    netlist, rest = argv[0], list(argv[1:])
    w = h = None
    if rest and _is_number(rest[0]):
        if len(rest) < 2 or not _is_number(rest[1]):
            raise UsageError("give both <board_w> and <board_h>, or neither")
        w, h = float(rest[0]), float(rest[1])
        for name, text, value in (("board_w", rest[0], w), ("board_h", rest[1], h)):
            if not _positive_mm(value):
                raise UsageError(f"{name} must be a positive number of mm, got '{text}'")
        rest = rest[2:]
    if len(rest) > 1:
        raise UsageError(f"unexpected argument(s): {' '.join(rest[1:])}")
    placement = rest[0] if rest and rest[0] not in ("", "-") else ""
    return CliArgs(netlist, w, h, placement)


def board_dims(design: dict, w: float | None, h: float | None,
               placement_refs: frozenset[str] = frozenset()) -> tuple[float, float, str]:
    """(w, h, where they came from): the command line, else the netlist
    board block, else AUTO_BOARD_DIMS for a board the builder sizes itself.
    A design whose every part is fixed (x/y, "at", or the placement file)
    has nothing to size the board from: UsageError instead of a guess."""
    if w is not None and h is not None:
        return w, h, "command line"
    board = design.get("board") or {}
    if "w" in board or "h" in board:
        bw, bh = board.get("w"), board.get("h")
        if not (_positive_mm(bw) and _positive_mm(bh)):
            raise UsageError(f"the netlist board block needs positive numeric w and h "
                             f"(mm), got w={bw!r} h={bh!r}")
        return float(bw), float(bh), "netlist board block"
    comps = design.get("components") or []
    fixed = (("x" in c and "y" in c) or "at" in c or c.get("ref") in placement_refs
             for c in comps)
    if comps and not board and all(fixed):
        raise UsageError('every component has a fixed position, so there is nothing to '
                         'size the board from: add "board": {"w": mm, "h": mm} to the '
                         'netlist or pass <board_w> <board_h>')
    return (*AUTO_BOARD_DIMS, "auto-sized: the builder fits the board to its parts")


def _read_json(path: str, what: str) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise UsageError(f"cannot read the {what} {path}: {e}") from e


def _cli_dims(cli: CliArgs) -> tuple[float, float, str]:
    if cli.w is not None and cli.h is not None:
        return cli.w, cli.h, "command line"
    design = _read_json(cli.netlist, "netlist")
    if not isinstance(design, dict):
        raise UsageError(f"the netlist {cli.netlist} is not a JSON object")
    overrides = _read_json(cli.placement, "placement file") if cli.placement else {}
    if not isinstance(overrides, dict):
        raise UsageError(f"the placement file {cli.placement} must map ref -> [x, y, rot]")
    return board_dims(design, None, None, frozenset(overrides))


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args and args[0] in ("-h", "--help"):
        print(f"{USAGE}\n{__doc__}")
        return 0
    try:
        cli = parse_cli(args)
        w, h, source = _cli_dims(cli)
    except UsageError as e:
        print(f"run_pipeline: error: {e}\n{USAGE}", file=sys.stderr)
        return 2
    print(f"run_pipeline: board {w} x {h} mm ({source})", file=sys.stderr)
    rep = run(cli.netlist, w, h, cli.placement)
    print(json.dumps(rep, indent=2))
    print("\n" + rep["gate_summary"], file=sys.stderr)
    return 0 if rep["PASS"] else 1


if __name__ == "__main__":
    sys.exit(main())
