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
"""

from __future__ import annotations

import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

import kicad_env as env
import kicad_ipc as ipc
import toolchain as tc


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


def build_board_from_netlist(netlist: str, board_w: float, board_h: float,
                             out_pcb: str = "", footprints_dir: str = "",
                             placement: str = "", pack_scale: float = 1.0) -> dict:
    """Generate a placed .kicad_pcb with copper planes from the netlist.json.

    Loads every footprint by lib_id, places it (board-relative coords, optionally
    overridden by a placement.json of ref -> [x, y, rot]), centers the board on an
    A4 sheet, assigns every pad to its net (all copper elements of split thermal
    pads), draws the Edge.Cuts outline, sets JLCPCB-class rules, and pours a GND
    plane on In1.Cu + the first power rail on In2.Cu (filled). Runs under KiCad's
    bundled Python (KiCad 9/10) with THAT install's footprint libraries.
    pack_scale (>1) loosens auto-placement spacing to give a deterministic
    router more room on a re-route. A board-API session (open_board) that
    still holds out_pcb is closed first (released_boards), or the build is
    refused while it has unsaved changes (blocking_board)."""
    kpy = tc.kicad_python()
    if not kpy:
        return {"ok": False, "reason": "KiCad bundled Python (with pcbnew) not found"}
    script = tc.TOOLS_DIR / "swig_build_board.py"
    if not script.is_file():
        return {"ok": False, "reason": f"missing {script}"}
    out = out_pcb or str(Path(netlist).with_suffix(".kicad_pcb"))
    fps = tc.footprints_dir(footprints_dir)
    if not fps:
        return {"ok": False, "reason": "footprints_dir not given and KiCad share dir not found"}
    guard = ipc.write_guard(out)
    if guard:
        return guard
    held = _release_board_session(out)
    if held.get("ok") is False:
        return held
    args = [kpy, str(script), netlist, out, fps, str(board_w), str(board_h)]
    if placement:
        args.append(placement)
    env = {**os.environ, "PACK_SCALE": str(pack_scale)}
    r = tc.run(args, env=env)
    res = tc.last_json(r.stdout) or {"ok": False}
    res["ok"] = r.returncode == 0 and res.get("ok", False)
    res["pcb"] = out
    if r.stderr:
        res["stderr"] = r.stderr[-1200:]
    if res["ok"]:
        res.update(_apply_netclasses(netlist, out, res.get("zones", [])))
    return {**res, **held}


def _release_board_session(pcb: str) -> dict:
    """Before this module rewrites `pcb`: a board-API session that still holds
    it would keep the old board in memory and could save it over the new
    file. board_route_tools.release_for_rewrite closes an idle one (its
    released_boards fields go into the response) and refuses a dirty one."""
    import board_route_tools
    return board_route_tools.release_for_rewrite(pcb)


def _apply_netclasses(netlist: str, out_pcb: str, zones: list[str]) -> dict:
    """Resolve electrical intent (net_specs + reliability class) into IPC-2221
    net classes and write them into the .kicad_pro so BOTH the Specctra DSN
    export (Freerouting routes power copper wider) and kicad-cli DRC honor
    per-net widths/clearances. Default class 3 (high-reliability) unless the
    netlist overrides "class". A netlist WITH net_specs whose classes cannot be
    written fails the build (ok False) — a board routed at default widths must
    never pass silently. A netlist without net_specs only reports the slip."""
    design = json.loads(Path(netlist).read_text(encoding="utf-8"))
    has_specs = bool(design.get("net_specs"))
    try:
        if str(tc.TOOLS_DIR) not in sys.path:
            sys.path.insert(0, str(tc.TOOLS_DIR))
        import netclass_writer
        pro = str(Path(out_pcb).with_suffix(".kicad_pro"))
        # nets backed by a poured plane ("In1.Cu:GND") or by designer preroutes
        # are exempt from track-width classing — the plane or the prerouted
        # copper carries the current, and a width class would make the net's
        # signal taps unreachable for the router
        planes = tuple(z.split(":", 1)[1] for z in zones if ":" in z)
        planes += tuple(p["net"] for p in design.get("preroutes", []))
        nc = netclass_writer.apply_to_project(
            pro, design, int(design.get("class", 3)), plane_nets=planes)
    except Exception as e:
        nc = {"ok": False, "reason": f"{type(e).__name__}: {e}"}
    summary = {"profile": nc.get("class_profile"), "written": nc.get("classes_written", []),
               "warnings": nc.get("warnings", [])}
    if nc.get("ok"):
        return {"netclasses": summary}
    failure = {"netclasses": {**summary, "error": nc.get("reason", "unknown failure")}}
    if not has_specs:
        return failure
    return {**failure, "ok": False,
            "reason": f"net classes not written: {nc.get('reason', 'unknown failure')}"}


def _freerouting_args(java: str, jar: str, w_dsn: str, w_ses: str,
                      max_passes: int, route_timeout: int) -> list[str] | dict:
    """The Freerouting command line, or an {ok: False} dict when the platform
    cannot run this jar (1.x needs a display: xvfb-run on a headless Linux)."""
    # -dct 0: skip the "Autorouter is about to start" countdown dialog (1.9
    # and 2.x both take it) — no window may wait on a click in a hands-off run.
    args = [java, "-Xmx4g", "-jar", jar, "-de", w_dsn, "-do", w_ses,
            "-mp", str(max_passes), "-dct", "0"]
    is_v2 = "freerouting-2" in Path(jar).name
    # Freerouting 1.x initializes AWT/Swing even in CLI mode — on a
    # display-less Linux box (Docker, CI) it dies with HeadlessException
    # before routing anything. Wrap it in a virtual framebuffer.
    if os.name != "nt" and not os.environ.get("DISPLAY") and not is_v2:
        if not shutil.which("xvfb-run"):
            return {"ok": False, "reason":
                    "no display and xvfb-run not found — Freerouting 1.x "
                    "needs one (install xvfb, e.g. apt-get install xvfb)"}
        args = ["xvfb-run", "-a"] + args
    if is_v2:
        # Freerouting 2.x: true headless, internal wall-clock budget (so it
        # finishes and WRITES the SES instead of being killed with nothing),
        # multi-threaded routing, no interactive dialog stalls.
        budget = max(60, route_timeout - 60)
        hh, rem = divmod(budget, 3600)
        mm_, ss = divmod(rem, 60)
        args += ["--gui.enabled=false",
                 f"--router.job_timeout={hh:02d}:{mm_:02d}:{ss:02d}",
                 "--feature_flags.multi_threading=true"]
    return args


def _route(java: str, jar: str, dsn: str, ses: str, max_passes: int,
           route_timeout: int) -> dict:
    """Run Freerouting on a copy of the DSN in a space-free temp dir (its CLI
    re-splits arguments on spaces, so paths like "OneDrive - NTS" break it)
    and copy the SES back. Returns {ok, [reason|stderr]}."""
    work = Path(tempfile.mkdtemp(prefix="frt_"))
    w_dsn, w_ses = str(work / "in.dsn"), str(work / "out.ses")
    shutil.copyfile(dsn, w_dsn)
    args = _freerouting_args(java, jar, w_dsn, w_ses, max_passes, route_timeout)
    if isinstance(args, dict):
        shutil.rmtree(work, ignore_errors=True)
        return args
    try:
        fr = tc.run(args, timeout=route_timeout)
        if Path(w_ses).is_file():
            shutil.copyfile(w_ses, ses)
        return {"ok": Path(ses).is_file(), "stderr": (fr.stdout + fr.stderr)[-1500:]}
    except subprocess.TimeoutExpired:
        return {"ok": False, "reason": f"Freerouting exceeded {route_timeout}s and was killed"}
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _unreadable(pcb: str, from_snapshot: bool) -> dict | None:
    """The refusal for a board this pcbnew cannot read (one saved by the KiCad
    11 API session), else None. from_snapshot with a pcbnew-readable X.k9
    snapshot beside it: None (autoroute_board restores and routes that).
    Without from_snapshot the refusal names the fallback when it exists."""
    refusal = tc.swig_readable(pcb)
    if not refusal:
        return None
    snapshot = tc.existing_snapshot(pcb)
    restorable = snapshot is not None and not tc.is_pre_k11(pcb)   # a KiCad 11 board + snapshot
    if from_snapshot and restorable:
        unusable = tc.swig_readable(snapshot)
        return None if not unusable else {"ok": False, "pre_k11_snapshot": snapshot,
                                          "reason": f"the snapshot is unreadable too: {unusable}"}
    if from_snapshot:
        why = (f"no pre-KiCad-11 snapshot {Path(tc.pre_k11_snapshot_path(pcb)).name} exists"
               if snapshot is None else f"{Path(pcb).name} is not a KiCad 11 board")
        return {"ok": False, "reason": f"{refusal}; {why}, so there is nothing to fall back to "
                                       "(rebuild with build_board_from_netlist)"}
    if restorable:
        return {"ok": False, "pre_k11_snapshot": snapshot,
                "reason": f"{refusal}; or fall back to Freerouting on its pre-KiCad-11 snapshot: "
                          "autoroute_board(pcb, from_snapshot=true) (the KiCad 11 copy moves to "
                          f"{Path(tc.agent_copy_path(pcb)).name})"}
    return {"ok": False, "reason": refusal}


def _autoroute_preflight(pcb: str, jar: str,
                         from_snapshot: bool = False) -> tuple[str, str, str] | dict:
    """(kicad_python, java, freerouting_jar) for autoroute_board, or the refusal
    dict: no pcbnew Python, a board it cannot read (one saved by the KiCad 11
    API session: route it with the board API tools, or pass from_snapshot to
    route its pre-KiCad-11 snapshot), no Java, no jar."""
    kpy = tc.kicad_python()
    if not kpy:
        return {"ok": False, "reason": "KiCad bundled Python not found"}
    refusal = _unreadable(pcb, from_snapshot)
    if refusal:
        return refusal
    java = tc.find_java()
    if not java:
        return {"ok": False, "reason": "java not found (Freerouting needs a JRE/JDK)"}
    fj = tc.freerouting_jar(jar)
    if not fj:
        return {"ok": False, "reason": "freerouting jar not found in server/tools or $FREEROUTING_JAR"}
    return kpy, java, fj


def autoroute_board(pcb: str, max_passes: int = 100, jar: str = "",
                    route_timeout: int = 300, from_snapshot: bool = False) -> dict:
    """Autoroute a board with Freerouting: export Specctra DSN, route, import the
    SES session back, and refill zones. Signal layers route on F.Cu/B.Cu while
    GND/power return to the inner planes. The router is killed after
    route_timeout seconds so a pathological board can never hang the pipeline.
    Returns the resulting track count and unconnected count. A board newer
    than the pcbnew install can read (one saved by the KiCad 11 API session)
    is refused with {ok: False, reason}: route it with the board API tools.
    from_snapshot=true is the fallback when agent routing stalls (route_check
    stalled): the KiCad 11 copy moves to X.agent.kicad_pcb (+ .kicad_pro /
    .kicad_prl), the pre-KiCad-11 snapshot X.k9.* is restored as X.kicad_pcb
    and routed here; the response adds fallback_from_snapshot,
    pre_k11_snapshot and agent_copy (a KiCad 9/10 board is routed as it is:
    fallback_from_snapshot false). A board-API session still holding the
    file is closed first (released_boards); one with unsaved changes refuses."""
    pre = _autoroute_preflight(pcb, jar, from_snapshot)
    if isinstance(pre, dict):
        return pre
    kpy, java, fj = pre
    guard = ipc.write_guard(pcb)
    if guard:
        return guard
    held = _release_board_session(pcb)
    if held.get("ok") is False:
        return held
    fallback = _fallback_from_snapshot(pcb) if from_snapshot else {}
    if fallback.get("ok") is False:
        return {**fallback, **held}
    return {**_autoroute(pcb, kpy, java, fj, max_passes, route_timeout), **held, **fallback}


def _fallback_from_snapshot(pcb: str) -> dict:
    """autoroute_board(from_snapshot=True)'s file step: restore the pre-KiCad-11
    snapshot over a KiCad 11 board (toolchain.restore_snapshot); a KiCad 9/10
    board stays as it is; a failed restore is {ok: False, step: "restore"}."""
    if tc.is_pre_k11(pcb):
        return {"fallback_from_snapshot": False,
                "fallback_note": f"{Path(pcb).name} is already a KiCad 9/10 board: routed as it "
                                 "is, no snapshot restored"}
    try:
        restored = tc.restore_snapshot(pcb)
    except (OSError, ValueError) as exc:
        return {"ok": False, "step": "restore", "pcb": pcb,
                "reason": f"restoring the pre-KiCad-11 snapshot failed: {exc}"}
    return {"fallback_from_snapshot": True, "pre_k11_snapshot": restored["snapshot"],
            "agent_copy": restored["agent_copy"], "restored": restored["restored"],
            "moved": restored["moved"]}


def _autoroute(pcb: str, kpy: str, java: str, fj: str, max_passes: int,
               route_timeout: int) -> dict:
    """DSN export -> Freerouting -> SES import + refill -> DRC unconnected count."""
    base = str(Path(pcb).with_suffix(""))
    dsn, ses = base + ".dsn", base + ".ses"
    d = tc.run([kpy, str(tc.TOOLS_DIR / "swig_export_dsn.py"), pcb, dsn], timeout=120)
    if not Path(dsn).is_file():
        return {"ok": False, "step": "dsn", "stderr": (d.stdout + d.stderr)[-1200:]}
    # remove any stale SES so a router timeout can't import a previous result
    Path(ses).unlink(missing_ok=True)
    routed = _route(java, fj, dsn, ses, max_passes, route_timeout)
    if not routed["ok"]:
        return {"ok": False, "step": "route", **{k: v for k, v in routed.items() if k != "ok"}}
    im = tc.run([kpy, str(tc.TOOLS_DIR / "swig_import_ses.py"), pcb, ses], timeout=180)
    imp = _parse_ses_import(im.stdout)
    if not imp["ok"]:
        return {"ok": False, "step": "import", "pcb": pcb, "dsn": dsn, "ses": ses,
                "reason": imp.get("error") or "SES import failed",
                "warnings": imp["warnings"], "import_log": (im.stdout + im.stderr)[-600:]}
    # verify route completeness — Freerouting is non-deterministic and may leave a
    # stub; report the unconnected count so the caller can re-run if it isn't zero.
    unconnected = None
    drc_note = None
    if tc.board_cli(pcb):
        c = tc.drc_counts(pcb)
        unconnected = c["unconnected"] if "error" not in c else None
        drc_note = c.get("error")
    return {"ok": unconnected in (0, None), "pcb": pcb, "dsn": dsn, "ses": ses,
            "tracks": imp["tracks"], "vias": imp["vias"], "unconnected": unconnected,
            "warnings": imp["warnings"] + ([drc_note] if drc_note else []),
            "import_log": im.stdout.strip()[-300:],
            "note": None if unconnected in (0, None) else
            f"{unconnected} net(s) left unrouted — re-run autoroute_board to converge"}


def _parse_ses_import(stdout: str) -> dict:
    """Result of swig_import_ses.py: its final JSON line
    {"ok", "tracks", "vias", "warnings"[, "error"]} when present, else the
    legacy 'ses_import True tracks N' token line."""
    j = tc.last_json(stdout)
    if j is not None and "ok" in j:
        return {"ok": bool(j.get("ok")), "tracks": j.get("tracks"), "vias": j.get("vias"),
                "warnings": list(j.get("warnings", [])), "error": j.get("error")}
    digits = [int(tok) for tok in stdout.split() if tok.isdigit()]
    return {"ok": "True" in stdout, "tracks": digits[-1] if digits else None,
            "vias": None, "warnings": [],
            "error": None if "True" in stdout else stdout.strip()[-300:]}


def _write_assembly_csvs(netlist: str, out: str, pos: str) -> tuple[bool, bool]:
    """JLCPCB BOM.csv + CPL.csv from the netlist and the kicad-cli pos file."""
    import csv
    design = json.loads(Path(netlist).read_text(encoding="utf-8"))
    groups: dict[tuple, list] = {}
    for c in design.get("components", []):
        key = (c.get("value", ""), c.get("footprint", ""),
               c.get("lcsc", ""), c.get("mpn", ""))
        groups.setdefault(key, []).append(c["ref"])
    with open(Path(out) / "BOM.csv", "w", newline="", encoding="utf-8") as f:
        wr = csv.writer(f)
        wr.writerow(["Comment", "Designator", "Footprint", "LCSC Part #", "MPN"])
        for (val, fpr, lcsc, mpn), refs in sorted(groups.items()):
            wr.writerow([val, ",".join(sorted(refs)), fpr.split(":")[-1], lcsc, mpn])
    if not Path(pos).is_file():
        return True, False
    with open(pos, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    with open(Path(out) / "CPL.csv", "w", newline="", encoding="utf-8") as f:
        wr = csv.writer(f)
        wr.writerow(["Designator", "Mid X", "Mid Y", "Layer", "Rotation"])
        for r0 in rows:
            wr.writerow([r0.get("Ref"), r0.get("PosX"), r0.get("PosY"),
                         r0.get("Side"), r0.get("Rot")])
    return True, True


def export_fab_package(pcb: str, out_dir: str = "", zip_name: str = "",
                       netlist: str = "") -> dict:
    """Export a complete fabrication + assembly package — Gerbers, drill,
    pick-and-place — then zip it for the fab house. When `netlist` is given,
    also writes the SMT-assembly pair the fab needs to BUILD the board, in
    JLCPCB format: BOM.csv (Comment/Designator/Footprint/LCSC Part #, grouped
    line items; per-component "lcsc"/"mpn" netlist keys are carried through)
    and CPL.csv (Designator/Mid X/Mid Y/Layer/Rotation). Requires kicad-cli
    (the one of the KiCad that wrote the board, board_cli(pcb))."""
    cli = tc.board_cli(pcb)
    if not cli:
        return {"ok": False, "reason": "kicad-cli not found; install KiCad."}
    out = out_dir or (str(Path(pcb).with_suffix("")) + "_gerbers")
    Path(out).mkdir(parents=True, exist_ok=True)
    g = tc.run([cli, "pcb", "export", "gerbers", "-o", out, pcb])
    dr = tc.run([cli, "pcb", "export", "drill", "-o", out, pcb])
    pos = str(Path(out) / (Path(pcb).stem + "-pos.csv"))
    p = tc.run([cli, "pcb", "export", "pos", "-o", pos, "--format", "csv", "--units", "mm", pcb])
    bom_written = cpl_written = False
    if netlist and Path(netlist).is_file():
        bom_written, cpl_written = _write_assembly_csvs(netlist, out, pos)
    zpath = zip_name or (str(Path(pcb).with_suffix("")) + f"_{tc.FAB_PROFILE}.zip")
    files = [f for f in glob.glob(str(Path(out) / "*")) if not f.endswith(".zip")]
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for f in files:
            z.write(f, arcname=Path(f).name)
    return {"ok": g.returncode == 0 and dr.returncode == 0,
            "out_dir": out, "zip": zpath, "fab_profile": tc.FAB_PROFILE, "kicad_cli": cli,
            "bom": bom_written, "cpl": cpl_written,
            "files": [Path(f).name for f in files],
            "stderr": (g.stderr + dr.stderr + p.stderr)[-1200:]}


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
    a board that pcbnew cannot read (KiCad 11 format) is refused."""
    pre = _stitch_preflight(pcb)
    if isinstance(pre, dict):
        return pre
    kpy, script = pre
    if not Path(pcb).is_file():
        raise ValueError(f"PCB not found: {pcb}")
    guard = ipc.write_guard(pcb)
    if guard:
        return guard
    # TRANSACTIONAL: stitching must never make the board worse. Apply, verify
    # with DRC; on any violation/unconnected roll back and retry fence-only
    # (a dense interior grid can fragment a power plane); if even the fence
    # fails, restore the original board and report the open task.
    bak = pcb + ".prestitch"
    shutil.copyfile(pcb, bak)
    cli = tc.board_cli(pcb)
    if cli:
        baseline = _stitch_baseline(pcb, bak)
        if baseline:
            return baseline
    # emc_stitch.py argv: <pcb> <max_freq_hz> <mode> [gnd_net] [er] [via_dia_mm] [via_drill_mm]
    extra = [gnd_net, str(er), str(via_diameter_mm), str(via_drill_mm)]
    res: dict = {"ok": False}
    for mode in (("full", "fence") if interior else ("fence",)):
        r = tc.run([kpy, str(script), pcb, str(max_freq_hz), mode] + extra, timeout=180)
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
