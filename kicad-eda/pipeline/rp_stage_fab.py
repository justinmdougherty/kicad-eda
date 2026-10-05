"""Harness stages after G4: the route-cache manifest of the final board, and
G5 (fabrication package, final renders and the offline sourcing audit).
Feature hook (rp_hooks): g5_verdict."""
from __future__ import annotations

import json
import re
from pathlib import Path

import rp_paths  # noqa: F401  first: puts server/ and server/tools/ on sys.path
import gates
import kicad_server as k
import rp_context
import rp_hooks
import rp_kicad


@rp_context.stage
def stage_cache_manifest(ctx: rp_context.RunContext) -> bool:
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


@rp_context.stage
def stage_g5(ctx: rp_context.RunContext) -> bool:
    """G5: fabrication package (release)."""
    r = k.export_fab_package(ctx.pcb, netlist=ctx.netlist)
    fab = {"ok": r.get("ok"), "zip": Path(r.get("zip", "")).name,
           "files": len(r.get("files", [])),
           "bom": r.get("bom"), "cpl": r.get("cpl")}
    ctx.report["fab"] = fab
    ctx.report["renders_final"] = rp_kicad.render(ctx.pcb, "final", quality="high")
    src = sourcing(ctx.design)
    ctx.report["sourcing"] = src
    verdict = gates.g5_fab(fab, require_assembly=True, sourcing=src)
    return rp_context.record_gate(ctx, rp_hooks.chain("g5_verdict", ctx, verdict))


def sourcing(design: dict) -> dict:
    """Offline BOM sourcing audit for G5: every assembly-relevant part
    should carry an LCSC number or an MPN; LCSC numbers must look like
    Cxxxxx. Test points / mounting holes / fiducials are exempt. The
    netlist 'require_sourcing': true upgrades findings to blockers."""
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
