"""Board-side headless tools: DRC / Gerbers / render via kicad-cli, and the
net-map pipeline's board stages (netlist.json -> placed board -> Freerouting
-> DRC -> EMC stitching -> fab zip).

They shell out to the scripts in server/tools/ — the SWIG board/route steps
under KiCad's bundled Python (needs pcbnew: pick_swig, KiCad 9/10; they refuse
a board newer than that pcbnew can read, toolchain.swig_readable), DRC /
Gerbers / drill / pos / render with the kicad-cli of the KiCad that WROTE the
board file (toolchain.board_cli(pcb): the pcbnew install for a SWIG-built
board, kicad-cli 11 for a board saved by the KiCad 11 API session, never a
mismatched nightly), Freerouting under Java. No IPC required.

Plain functions; kicad_server registers everything in TOOLS with FastMCP.

The stage bodies live in their own modules (X-09 split; locate code by
function name): board_build.build (build_board_from_netlist,
release_board_session, apply_netclasses), board_autoroute.run
(autoroute_board, freerouting_args and the rest of the Freerouting stage),
board_fab.export (export_fab_package, write_assembly_csvs). Every tool keeps
its name, signature and docstring here as a thin wrapper; the private aliases
below keep the old names other code uses.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import board_autoroute
import board_build
import board_fab
import kicad_env as env
import kicad_ipc as ipc
import toolchain as tc

# the pre-split private names (tests/test_freerouting_args.py and later owners)
_freerouting_args = board_autoroute.freerouting_args
_release_board_session = board_build.release_board_session
_apply_netclasses = board_build.apply_netclasses


def drc_pcb(pcb: str, fab_profile: str = "") -> dict:
    """Run KiCad DRC (error severity) on a SAVED .kicad_pcb with the kicad-cli
    of the KiCad that wrote it (board_cli) and return the error / unconnected
    counts, a per-type histogram and the JSON report path. Read-only: safe on
    a board that is open in the editor, but unsaved GUI changes are not
    reflected. The fab profile is echoed; the rules themselves live in the
    board/.kicad_pro. `kicad_cli_note` appears when no installed kicad-cli is
    as new as the file and the newest one was tried."""
    profile = fab_profile or tc.FAB_PROFILE
    if not tc.board_cli(pcb):
        return {"ok": False, "reason": "kicad-cli not found; install KiCad.", "profile": profile}
    if not Path(pcb).is_file():
        raise ValueError(f"PCB not found: {pcb}")
    c = tc.drc_counts(pcb)
    if "error" in c:
        return {"ok": False, "reason": c["error"], "report": c["report"], "profile": profile}
    cli_note = {"kicad_cli_note": c["kicad_cli_note"]} if c.get("kicad_cli_note") else {}
    res = {"ok": c["errors"] == 0 and c["unconnected"] == 0, "errors": c["errors"],
           "unconnected": c["unconnected"], "types": c["types"], "report": c["report"],
           "kicad_cli": c["kicad_cli"], "profile": profile, **cli_note}
    if ipc.open_in_editor(pcb):
        return {**res, "note": "file is open in the editor; unsaved GUI changes are not reflected"}
    return res


def export_gerbers(pcb: str, out_dir: str = "") -> dict:
    """Export Gerbers + drill with the kicad-cli of the KiCad that wrote the
    board (board_cli(pcb))."""
    cli = tc.board_cli(pcb)
    if not cli:
        return {"ok": False, "reason": "kicad-cli not found; install KiCad."}
    out = out_dir or str(Path(pcb).with_suffix("")) + "_gerbers"
    Path(out).mkdir(parents=True, exist_ok=True)
    g = tc.run([cli, "pcb", "export", "gerbers", "-o", out, pcb])
    d = tc.run([cli, "pcb", "export", "drill", "-o", out, pcb])
    files = [p.name for p in Path(out).iterdir()] if Path(out).is_dir() else []
    return {"ok": g.returncode == 0 and d.returncode == 0, "out_dir": out,
            "files": files, "kicad_cli": cli, "stderr": (g.stderr + d.stderr)[-1500:]}


def render_pcb(pcb: str, side: str = "top", out_png: str = "") -> dict:
    """Render the board to a PNG (top/bottom) with the kicad-cli of the KiCad
    that wrote the board (board_cli(pcb))."""
    cli = tc.board_cli(pcb)
    if not cli:
        return {"ok": False, "reason": "kicad-cli not found; install KiCad."}
    out = out_png or str(Path(pcb).with_suffix(f".{side}.png"))
    r = tc.run([cli, "pcb", "render", "--side", side, "-o", out, pcb])
    return {"ok": r.returncode == 0, "output": out, "kicad_cli": cli, "stderr": r.stderr[-1000:]}


def build_board_from_netlist(netlist: str, board_w: float = 0, board_h: float = 0,
                             out_pcb: str = "", footprints_dir: str = "",
                             placement: str = "", pack_scale: float = 1.0) -> dict:
    """Generate a placed .kicad_pcb with copper planes from the netlist.json.

    Loads every footprint by lib_id, places it (board-relative coords, optionally
    overridden by a placement.json of ref -> [x, y, rot]), centers the board on
    its sheet, assigns every pad to its net (all copper elements of split thermal
    pads), draws the Edge.Cuts outline, sets JLCPCB-class rules, and pours a GND
    plane on In1.Cu + the first power rail on In2.Cu (filled). Runs under KiCad's
    bundled Python (KiCad 9/10) with THAT install's footprint libraries.
    The netlist board block is resolved first: outline.loop or outline.dxf
    (read here, relative to the netlist), corner_radius, cutouts and datum
    make an outline board, with exact Edge.Cuts, the aux origin at its datum
    and a <pcb stem>.outline.json sidecar beside the board; a plain w/h block
    sizes a board with fixed parts as before. The result's "outline" gives
    the source, fingerprint and sidecar. A block that cannot be resolved (e.g.
    a missing DXF) refuses before KiCad starts. board_w/board_h (mm) are
    optional: 0 = from the netlist; they size only a fully placed board that
    has no board block (auto placement sizes the board to its parts). A value
    that differs from the board block / outline by more than 0.01 mm is
    reported as "board_w/board_h ignored" in the warnings.
    pack_scale (>1) loosens auto-placement spacing to give a deterministic
    router more room on a re-route. A board-API session (open_board) that
    still holds out_pcb is closed first (released_boards), or the build is
    refused while it has unsaved changes (blocking_board)."""
    return board_build.build(netlist, board_w, board_h, out_pcb=out_pcb,
                             footprints_dir=footprints_dir, placement=placement,
                             pack_scale=pack_scale)


def autoroute_board(pcb: str, max_passes: int = 100, jar: str = "",
                    route_timeout: int = 300, from_snapshot: bool = False,
                    dsn_seed: int = 0) -> dict:
    """Autoroute a board with Freerouting: export Specctra DSN, route, import the
    SES session back, and refill zones. Every copper layer is routable in the
    exported DSN (no per-layer restriction yet), so Freerouting may also lay
    signal tracks on the inner plane layers; the refilled planes clear around
    them. A board with a non-rectangular outline (arcs, cutouts, more than 4
    vertices or slanted edges) gets its DSN boundary shrunk and its cutout
    keepouts grown so the routed copper keeps the board's copper-to-edge
    clearance; a rectangular board keeps the outline KiCad exported. The
    router is killed after route_timeout seconds so a pathological board can
    never hang the pipeline.
    Returns the resulting track count and unconnected count. Routing is
    deterministic for a given placement: the exported DSN is rewritten in a
    canonical order (KiCad writes footprints and vias in random-UUID order and
    Freerouting is order-sensitive), so the same placement gives the same
    route every run; the result carries dsn_sha256 (the DSN Freerouting read)
    and dsn_seed. dsn_seed > 0 routes a deterministic alternate footprint
    order instead, for a retry when seed 0 leaves nets unrouted (a bad DSN
    stops at step "dsn_canon"). A board newer
    than the pcbnew install can read (one saved by the KiCad 11 API session)
    is refused with {ok: False, reason}: route it with the board API tools.
    from_snapshot=true is the fallback when agent routing stalls (route_check
    stalled): the KiCad 11 copy moves to X.agent.kicad_pcb (+ .kicad_pro /
    .kicad_prl), the pre-KiCad-11 snapshot X.k9.* is restored as X.kicad_pcb
    and routed here; the response adds fallback_from_snapshot,
    pre_k11_snapshot and agent_copy (a KiCad 9/10 board is routed as it is:
    fallback_from_snapshot false). A board-API session still holding the
    file is closed first (released_boards); one with unsaved changes refuses."""
    return board_autoroute.run(pcb, max_passes=max_passes, jar=jar,
                               route_timeout=route_timeout, from_snapshot=from_snapshot,
                               dsn_seed=dsn_seed)


def export_fab_package(pcb: str, out_dir: str = "", zip_name: str = "",
                       netlist: str = "") -> dict:
    """Export a complete fabrication + assembly package — Gerbers, drill,
    pick-and-place — then zip it for the fab house. When `netlist` is given,
    also writes the SMT-assembly pair the fab needs to BUILD the board, in
    JLCPCB format: BOM.csv (Comment/Designator/Footprint/LCSC Part #, grouped
    line items; per-component "lcsc"/"mpn" netlist keys are carried through)
    and CPL.csv (Designator/Mid X/Mid Y/Layer/Rotation). Requires kicad-cli
    (the one of the KiCad that wrote the board, board_cli(pcb))."""
    return board_fab.export(pcb, out_dir=out_dir, zip_name=zip_name, netlist=netlist)


def check_design_gates(pcb: str, netlist: str, stage: str = "placement") -> dict:
    """Run the formal design-review gate checks on a board (IEEE 15288.2 working
    reviews). stage="placement" (pre-route, G3): connectors must mouth-out at the
    board edge, antenna keepout present if requested. stage="routed" (pre-CDR,
    G4): every net bound to an IPC net class must be routed at >= the class
    width. Returns {"passed": bool, "issues": [...]} — do NOT proceed to the next
    pipeline stage while issues remain. A board newer than the pcbnew install
    can read (one saved by the KiCad 11 API session): stage "routed" is then
    judged on the board API instead (board_route_tools.routed_gate, same
    shape, backend "api"); the other stages are refused with {ok: False, reason}."""
    kpy = tc.kicad_python()
    script = tc.TOOLS_DIR / "swig_gate_checks.py"
    if not kpy:
        return {"ok": False, "reason": "KiCad bundled Python (pcbnew) not found"}
    refusal = tc.swig_readable(pcb)
    if refusal:
        if stage == "routed":
            import board_route_tools
            return board_route_tools.routed_gate(pcb, netlist)
        return {"ok": False, "reason": refusal}
    if not script.is_file():
        return {"ok": False, "reason": f"missing {script}"}
    r = tc.run([kpy, str(script), stage, pcb, netlist], timeout=120)
    res = tc.last_json(r.stdout) or {"issues": [f"no result: {r.stderr[-300:]}"]}
    issues = res.get("issues", [])
    return {"ok": True, "stage": stage, "passed": not issues, "issues": issues}


def _stitch_baseline(pcb: str, bak: str) -> dict | None:
    """A board that isn't already G4-clean can't be judged after stitching —
    require a clean baseline. Returns a refusal dict, else None."""
    base = tc.drc_counts(pcb)
    if "error" in base:
        Path(bak).unlink(missing_ok=True)
        return {"ok": False, "reason": f"baseline DRC failed: {base['error']}"}
    if base["errors"] or base["unconnected"]:
        Path(bak).unlink(missing_ok=True)
        return {"ok": False, "reason": f"board not clean before stitching "
                f"({base['errors']} violations, {base['unconnected']} unconnected) "
                f"— pass the G4 layout gate first", "types": base["types"]}
    return None


def _stitch_preflight(pcb: str) -> tuple[str, Path] | dict:
    """(kicad_python, emc_stitch.py) for add_ground_stitching, or the refusal
    dict: no pcbnew Python, a board it cannot read (one saved by the KiCad 11
    API session), or the script missing."""
    kpy = tc.kicad_python()
    if not kpy:
        return {"ok": False, "reason": f"KiCad bundled Python (pcbnew) not found: "
                                       f"{tc.kicad_python_error()}"}
    refusal = tc.swig_readable(pcb)
    if refusal:
        return {"ok": False, "reason": refusal}
    script = tc.TOOLS_DIR / "emc_stitch.py"
    if not script.is_file():
        return {"ok": False, "reason": f"missing {script}"}
    return kpy, script


def add_ground_stitching(pcb: str, gnd_net: str = "GND", max_freq_hz: float = 0.0,
                         er: float = 4.2, interior: bool = True,
                         via_diameter_mm: float = 0.6, via_drill_mm: float = 0.3) -> dict:
    """Add a DRC-safe GND via fence + interior stitching grid to a ROUTED board
    (EMC: ties the ground planes/pours, contains edge fields, keeps the return
    path under the signal). Vias land only where they clear all non-GND copper and
    every hole, so the board stays DRC-clean. Pitch is lambda/20 at `max_freq_hz`
    (set it to your highest signal/RF frequency) or a 3 mm default. Run AFTER
    autoroute_board, BEFORE export_fab_package. Runs under KiCad's bundled Python;
    a board that pcbnew cannot read (KiCad 11 format) is refused. A board-API
    session still holding the file is closed first (released_boards); one with
    unsaved changes refuses (blocking_board) before the board is touched."""
    pre = _stitch_preflight(pcb)
    if isinstance(pre, dict):
        return pre
    kpy, script = pre
    if not Path(pcb).is_file():
        raise ValueError(f"PCB not found: {pcb}")
    guard = ipc.write_guard(pcb)
    if guard:
        return guard
    # a stale session could later save its old copy over the stitched board
    held = board_build.release_board_session(pcb)
    if held.get("ok") is False:
        return held
    # emc_stitch.py argv: <pcb> <max_freq_hz> <mode> [gnd_net] [er] [via_dia_mm] [via_drill_mm]
    argv = [str(script), pcb, str(max_freq_hz)]
    extra = [gnd_net, str(er), str(via_diameter_mm), str(via_drill_mm)]
    modes = ("full", "fence") if interior else ("fence",)
    return {**_stitch_transaction(pcb, kpy, argv, extra, modes), **held}


def _stitch_transaction(pcb: str, kpy: str, argv: list[str], extra: list[str],
                        modes: tuple[str, ...]) -> dict:
    """add_ground_stitching's TRANSACTIONAL body: stitching must never make
    the board worse. Apply, verify with DRC; on any violation/unconnected roll
    back and retry the next mode (fence-only: a dense interior grid can
    fragment a power plane); if even the fence fails, restore the original
    board and report the open task."""
    bak = pcb + ".prestitch"
    shutil.copyfile(pcb, bak)
    cli = tc.board_cli(pcb)
    if cli:
        baseline = _stitch_baseline(pcb, bak)
        if baseline:
            return baseline
    res: dict = {"ok": False}
    for mode in modes:
        r = tc.run([kpy, *argv, mode, *extra], timeout=180)
        res = tc.last_json(r.stdout) or {"ok": False, "stderr": (r.stdout + r.stderr)[-600:]}
        if not res.get("ok"):
            break
        if not cli:
            Path(bak).unlink(missing_ok=True)
            return {**res, "mode": mode, "note": "kicad-cli not found: stitching NOT DRC-verified"}
        after = tc.drc_counts(pcb)
        if "error" not in after and after["errors"] == 0 and after["unconnected"] == 0:
            Path(bak).unlink(missing_ok=True)
            return {**res, "mode": mode, "drc_errors": 0, "unconnected": 0}
        shutil.copyfile(bak, pcb)            # roll back, try the next mode
        res = {"ok": False, "mode": mode, "drc_errors": after["errors"],
               "unconnected": after["unconnected"], "types": after["types"],
               **({"drc_error": after["error"]} if "error" in after else {})}
    Path(bak).unlink(missing_ok=True)
    return {**res, "reason": res.get("reason") or res.get("error") or
            "stitching rolled back — it violated DRC or broke plane connectivity "
            "even fence-only; board left unmodified"}


def rescan_kicad_installs() -> dict:
    """Forget every cached KiCad probe (versions, bundled-Python pcbnew checks,
    api-server support) and discover the installs again, without restarting
    the server. Use after installing or removing a KiCad, or when
    server_status shows kicad_python null with a transient reason (a probe
    that timed out or could not start). Returns the fresh picks."""
    env.clear_cache()
    return {"installs": tc.installs(), "kicad_cli_schematic": tc.kicad_cli(),
            "kicad_cli_board": tc.board_cli(), "kicad_python": tc.kicad_python(),
            "kicad_python_error": tc.kicad_python_error(),
            "install_probe_errors": env.probe_errors()}


TOOLS = (drc_pcb, export_gerbers, render_pcb, build_board_from_netlist, autoroute_board,
         export_fab_package, check_design_gates, add_ground_stitching, rescan_kicad_installs)
