#!/usr/bin/env python3
"""Gated pipeline harness: drive a netlist through the concept->Gerbers flow
with FORMAL GO/NO-GO GATES between stages (IEEE 15288.2 review structure).
The pipeline halts at the first failing gate — a design with open blockers
never proceeds to the next stage, so an error caught at the schematic gate
can never survive to fabrication.

  G1 Requirements (SRR)  ->  G2 Schematic (PDR)  ->  G3 Placement (hard gate)
  ->  G4 Layout (CDR)  ->  G5 Fab package (release)

Usage:
  <venv-python> run_pipeline.py <netlist.json> <board_w> <board_h> [placement.json]
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

SERVER = Path(__file__).resolve().parents[1] / "server"
sys.path.insert(0, str(SERVER))
sys.path.insert(0, str(SERVER / "tools"))
import kicad_server as k       # noqa: E402
import lint_netlist as lint    # noqa: E402
import design_report as drpt   # noqa: E402
import gates                   # noqa: E402
import remediate               # noqa: E402
import thermal_check           # noqa: E402


def _drc(pcb: str) -> dict:
    rpt = str(Path(pcb).with_suffix("")) + "_drc.json"
    subprocess.run([k.KICAD_CLI, "pcb", "drc", pcb, "--severity-error",
                    "-o", rpt, "--format", "json"], capture_output=True, text=True)
    d = json.loads(Path(rpt).read_text())
    return {"errors": len(d.get("violations", [])),
            "unconnected": len(d.get("unconnected_items", [])),
            "types": _counts(d.get("violations", []))}


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
                       capture_output=True, text=True, timeout=300)
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
                       capture_output=True, text=True, timeout=300)
    for line in reversed(r.stdout.strip().splitlines()):
        if line.startswith("{"):
            try:
                d = json.loads(line)
                return d.get("applied", []) + \
                    [f"OPEN: {o}" for o in d.get("open", [])]
            except Exception:
                break
    return []


def _route_hash(design: dict) -> str:
    """Hash of everything that determines PLACEMENT + ROUTING. A change to a
    value, sourcing field, or thermal annotation does not move copper — only
    these keys force the expensive build+route stages to re-run."""
    geo_keys = ("ref", "footprint", "x", "y", "rot", "side", "interior")
    comps = sorted(({k: c.get(k) for k in geo_keys if k in c}
                    for c in design.get("components", [])),
                   key=lambda c: c["ref"])
    payload = {
        "components": comps,
        "nets": design.get("nets"),
        "net_specs": design.get("net_specs"),
        "preroutes": design.get("preroutes"),
        "board": design.get("board"),
        "mounting_holes": design.get("mounting_holes"),
        "antenna_keepout": design.get("antenna_keepout"),
        "class": design.get("class"),
        "emc": design.get("emc"),
    }
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
    for side in ("top", "bottom"):
        png = f"{base}_{tag}_{side}.png"
        try:
            r = subprocess.run(
                [k.KICAD_CLI, "pcb", "render", pcb, "-o", png,
                 "--side", side, "--quality", quality,
                 "--width", "1600", "--height", "1400"],
                capture_output=True, text=True, timeout=300)
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
        capture_output=True, text=True, timeout=600)
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


def _halt(report: dict) -> dict:
    report["PASS"] = all(g["passed"] for g in report["gates"])
    report["gate_summary"] = gates.summarize(report["gates"])
    return report


def run(netlist: str, w: float, h: float, placement: str = "") -> dict:
    base = str(Path(netlist).with_suffix(""))
    sch, pcb = base + ".kicad_sch", base + ".kicad_pcb"
    report: dict = {"netlist": netlist, "gates": []}
    design = json.loads(Path(netlist).read_text(encoding="utf-8"))
    syms = str(k._kicad_share() / "symbols") if k._kicad_share() else ""

    # ---- G1: requirements / netlist review (feeds SRR) ----
    lrep = lint.lint(design, syms)
    report["lint"] = {"ok": lrep["ok"], "errors": lrep["errors"],
                      "warnings": lrep["warnings"]}
    g1 = gates.g1_requirements(lrep, design)
    report["gates"].append(g1)
    if not g1["passed"]:
        return _halt(report)

    # ---- G2: schematic review (feeds PDR), with corrective-action loop ----
    # A failing gate translates its blockers into tasks; the auto-resolvable
    # ones (e.g. a missing bypass cap) are APPLIED to the design, the netlist
    # is rewritten, the stage re-runs, and the gate is re-tested. Only tasks
    # needing design intent stop the flow — with the open task list attached.
    g2 = None
    for rnd in range(1, 4):
        r = k.build_schematic_from_netlist(netlist, sch)
        report["schematic"] = {kk: r.get(kk) for kk in
                               ("ok", "components", "nets", "erc_ok",
                                "erc_violations")}
        dr = drpt.analyze(design, syms)
        report["design_report"] = {
            "decoupling_uncovered": dr["decoupling"]["uncovered"],
            "flags": dr["flags"]}
        trep = thermal_check.check(design)
        report["thermal"] = trep
        g2 = gates.g2_schematic(r, dr, trep)
        if g2["passed"]:
            break
        fixed, log = remediate.apply(design, g2["tasks"])
        if not log:
            break                       # only designer tasks remain — stop here
        design = fixed
        Path(netlist).write_text(json.dumps(design, indent=2), encoding="utf-8")
        report.setdefault("remediation", []).append(
            {"gate": "G2", "round": rnd, "applied": log})
    report["gates"].append(g2)
    if not g2["passed"]:
        return _halt(report)

    # ---- ROUTE CACHE: when nothing that moves copper changed since the last
    # successful run, reuse the routed board and skip build+route entirely —
    # the G3/G4 GATES still re-run against the cached artifact (the cache
    # skips work, never judgement). Hash computed AFTER G2 so a G2 netlist
    # remediation correctly invalidates. PIPELINE_NO_CACHE=1 bypasses.
    rhash = _route_hash(design)
    cache_p = Path(base + ".cache.json")
    cached = {}
    try:
        cached = json.loads(cache_p.read_text(encoding="utf-8"))
    except Exception:
        pass
    use_cache = (not placement
                 and not os.environ.get("PIPELINE_NO_CACHE")
                 and cached.get("route_hash") == rhash
                 and cached.get("routed_ok")
                 and Path(pcb).is_file())

    # ---- G3 + G4: place (gate) then route (gate), self-feeding. A G3 failure
    # whose tasks are all auto ("loosen placement") re-places at the next
    # pack_scale and re-tests; a G4 routing shortfall does the same (the
    # deterministic-router escape). Manual blockers (inward connector, missing
    # keepout, bad pad ref) stop the loop — scale cannot fix design intent.
    scales = [1.0] if placement else [1.0, 1.5, 2.0, 2.6, 3.2]
    g3 = g4 = None
    board_rep, route_rep, drc_rep = {}, {}, {}
    last_good = None              # (scale, g3, board_rep) of last buildable attempt
    if use_cache:
        report["route_cache"] = "hit"
        board_rep = cached.get("board_rep") or {"ok": True}
        cy = _courtyard_drc(pcb)
        pl = _gate_check("placement", pcb, netlist)
        g3 = gates.g3_placement(board_rep, cy, pl["issues"], pl["warnings"])
        route_rep = {"cached": True,
                     "pack_scale": cached.get("pack_scale"),
                     "tracks": cached.get("tracks"),
                     "unconnected": cached.get("unconnected", 0)}
        scales = []                # skip the build+route loop below
    else:
        report["route_cache"] = "miss"
    for attempt, scale in enumerate(scales, 1):
        rb = k.build_board_from_netlist(netlist, w, h, pcb, "", placement, scale)
        board_rep = {kk: rb.get(kk) for kk in
                     ("ok", "footprints", "pads_assigned", "zones",
                      "antenna_keepout", "mounting_holes", "missing_pads",
                      "error", "reason", "stderr")}
        if not rb.get("ok") and last_good is not None:
            # the RETRY made things worse (a looser pack no longer fits a
            # fixed board). Restore the last attempt's ROUTED board from its
            # snapshot — rebuilding would wipe the routing — and stop
            # escalating; its own gate verdict stands.
            scale, g3, board_rep, route_rep = last_good
            snap = Path(pcb + ".attempt")
            if snap.is_file():
                Path(pcb).write_bytes(snap.read_bytes())
            report.setdefault("remediation", []).append(
                {"gate": "G3", "round": attempt,
                 "applied": [f"pack_scale {scales[attempt - 1]} does not fit "
                             f"the fixed board -> restored routed board at "
                             f"pack_scale {scale}"]})
            break
        cy = _courtyard_drc(pcb) if rb.get("ok") else {"courtyard_violations": 0}
        pl = (_gate_check("placement", pcb, netlist) if rb.get("ok")
              else {"issues": [], "warnings": []})
        if design.get("mounting_holes") and not rb.get("mounting_holes"):
            pl["issues"] = pl["issues"] + ["mounting holes requested but "
                                           "none placed on the board"]
        g3 = gates.g3_placement(rb, cy, pl["issues"], pl["warnings"])
        if not g3["passed"]:
            if (g3["tasks"] and all(t["auto"] for t in g3["tasks"])
                    and attempt < len(scales)):
                report.setdefault("remediation", []).append(
                    {"gate": "G3", "round": attempt,
                     "applied": [f"re-placed at pack_scale {scales[attempt]}"]})
                continue
            break
        report["renders_placement"] = _render(pcb, "place")
        ra = k.autoroute_board(pcb, 600, "", 1500)
        route_rep = {"attempt": attempt, "pack_scale": scale,
                     "tracks": ra.get("tracks"),
                     "unconnected": ra.get("unconnected")}
        # snapshot the ROUTED board so a failed retry-build can restore it
        try:
            Path(pcb + ".attempt").write_bytes(Path(pcb).read_bytes())
        except OSError:
            pass
        last_good = (scale, g3, board_rep, route_rep)
        if ra.get("unconnected") is None:
            # the ROUTER failed outright — placement is not the problem, so
            # scale escalation cannot help. Surface the router error at G4.
            route_rep["error"] = (ra.get("error") or ra.get("reason")
                                  or ra.get("stderr") or "autoroute failed")
            break
        if ra.get("unconnected") == 0:
            break
        if ra.get("unconnected") <= 4:
            # near-miss: same-net micro-gaps are router/SES artifacts, not a
            # placement problem — heal in place instead of escalating scale
            # (the router is deterministic; a rebuild can't clear them)
            _drc(pcb)                  # writes the report the fixer reads
            healed = _board_fix(pcb, netlist, "heal")
            if healed:
                report.setdefault("remediation", []).append(
                    {"gate": "G4", "round": attempt, "applied": healed})
                if _drc(pcb)["unconnected"] == 0:
                    route_rep["unconnected"] = 0
                    try:
                        Path(pcb + ".attempt").write_bytes(
                            Path(pcb).read_bytes())
                    except OSError:
                        pass
                    break
        if attempt < len(scales):
            report.setdefault("remediation", []).append(
                {"gate": "G4", "round": attempt,
                 "applied": [f"{ra.get('unconnected')} unrouted -> re-placed at "
                             f"pack_scale {scales[attempt]} and re-routed"]})
    report["board"] = board_rep
    report["autoroute"] = route_rep
    report["gates"].append(g3)
    if not g3["passed"]:
        return _halt(report)

    # ---- G4: layout / board review (feeds CDR), with corrective-action loop:
    # board-level blockers with an unambiguous remedy (same-net micro-gaps,
    # under-ampacity layer transitions) are FIXED on the routed board and the
    # gate re-tested — re-routing is never required for them.
    drc_rep = _drc(pcb)
    report["drc"] = drc_rep
    wc = _gate_check("routed", pcb, netlist)
    g4 = gates.g4_layout(route_rep, drc_rep, wc["issues"], wc["warnings"])
    for rnd in (1, 2):
        if g4["passed"]:
            break
        fixes = _board_fix(pcb, netlist, "all")
        if not any(not f.startswith("OPEN:") for f in fixes):
            break
        report.setdefault("remediation", []).append(
            {"gate": "G4", "round": rnd, "applied": fixes})
        drc_rep = _drc(pcb)
        report["drc"] = drc_rep
        route_rep = {**route_rep, "unconnected": drc_rep["unconnected"]}
        report["autoroute"] = route_rep
        wc = _gate_check("routed", pcb, netlist)
        g4 = gates.g4_layout(route_rep, drc_rep, wc["issues"], wc["warnings"])

    # ---- EMC post-pass (opt-in via netlist "emc": {"stitch": true, ...}):
    # perimeter GND via fence + interior stitching grid (Ott/Montrose,
    # lambda/20 pitch from max_freq_hz), then re-DRC — the pass is
    # clearance-aware but the gate trusts measurements, not intentions.
    emc = design.get("emc") or {}
    if g4["passed"] and emc.get("stitch") and not use_cache:
        er = _emc_stitch(pcb, emc)
        report["emc"] = er
        notes = []
        if er.get("ok"):
            notes.append(f"EMC stitching: {er.get('vias_added')} GND vias "
                         f"(fence {er.get('fence')}, interior "
                         f"{er.get('interior')}, pitch {er.get('pitch_mm')} mm)")
            drc_rep = _drc(pcb)
            report["drc"] = drc_rep
        else:
            notes.append(f"EMC stitching FAILED: {er.get('reason')}")
        if emc.get("teardrops"):
            notes.append("teardrops requested but KiCad's teardrop generator "
                         "has no headless API — generate in the GUI "
                         "(Edit > Edit Teardrops) before fab if required")
        g4 = gates.g4_layout(route_rep, drc_rep, wc["issues"],
                             wc["warnings"] + notes)
    report["gates"].append(g4)
    if not g4["passed"]:
        return _halt(report)

    # board is final (routed + healed + stitched): record the cache manifest
    try:
        cache_p.write_text(json.dumps({
            "route_hash": rhash, "routed_ok": True,
            "unconnected": drc_rep.get("unconnected", 0),
            "pack_scale": route_rep.get("pack_scale"),
            "tracks": route_rep.get("tracks"),
            "board_rep": board_rep}, indent=2), encoding="utf-8")
    except OSError:
        pass

    # ---- G5: fabrication package (release) ----
    r = k.export_fab_package(pcb, netlist=netlist)
    fab = {"ok": r.get("ok"), "zip": Path(r.get("zip", "")).name,
           "files": len(r.get("files", [])),
           "bom": r.get("bom"), "cpl": r.get("cpl")}
    report["fab"] = fab
    report["renders_final"] = _render(pcb, "final", quality="high")
    src = _sourcing(design)
    report["sourcing"] = src
    report["gates"].append(gates.g5_fab(fab, require_assembly=True,
                                        sourcing=src))
    return _halt(report)


def main() -> int:
    netlist = sys.argv[1]
    w, h = float(sys.argv[2]), float(sys.argv[3])
    placement = sys.argv[4] if len(sys.argv) > 4 and sys.argv[4] not in ("", "-") else ""
    rep = run(netlist, w, h, placement)
    print(json.dumps(rep, indent=2))
    print("\n" + rep["gate_summary"], file=sys.stderr)
    return 0 if rep["PASS"] else 1


if __name__ == "__main__":
    sys.exit(main())
