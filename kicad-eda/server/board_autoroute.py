"""Freerouting stage of the net-map pipeline: Specctra DSN export (SWIG) ->
canonical DSN order (dsn_canon, venv side) -> Freerouting under Java -> SES
import + zone refill (SWIG) -> DRC unconnected count, plus the pre-KiCad-11
snapshot fallback (ROADMAP D5).

run() is the body of the MCP tool board_pipeline_tools.autoroute_board (the
tool keeps its name, signature and docstring there as a thin wrapper).
Split out of board_pipeline_tools.py by X-09; later owners locate code here by
function name (freerouting_args, route, unreadable, autoroute_preflight,
seed_refusal, run, fallback_from_snapshot, autoroute, export_dsn,
canonical_dsn, parse_ses_import).

F5-02: KiCad writes footprints, vias and zones in random-UUID order and
Freerouting 1.9.0 is order-sensitive, so the exported DSN is rewritten in
place in dsn_canon's canonical order (dsn_seed 0) or a seeded alternate
footprint order (dsn_seed k > 0) before routing; the result carries
dsn_sha256 (of the DSN Freerouting read) and dsn_seed.

toolchain, kicad_ipc and board_build are imported AS MODULES and their
functions looked up at call time, so tests that monkeypatch tc.run /
ipc.write_guard / tc.restore_snapshot reach this code. server/tools is never
put on sys.path at import time (canonical_dsn puts it there when it runs).

F5-14 (F5-05 spike; 1.9.0 stays): every Freerouting child is kept off the
network, in its own work dir and, on Windows, on a private desktop via this
module as a script (`board_autoroute.py --hidden-desktop -- <argv>`).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import board_build
import kicad_ipc as ipc
import toolchain as tc

if sys.platform == "win32":
    import _winapi
    import ctypes
    from ctypes import wintypes as w

DEAD_PORT = 9                 # nothing listens there: every proxied connect is refused
HIDDEN_DESKTOP = "--hidden-desktop"


def on_windows() -> bool:
    return os.name == "nt"


def offline_jvm_args(work: str) -> list[str]:
    """Every version (F5-05: 0 outbound connects, same SES): proxies on a dead
    port; tmpdir / home in the work dir (a stale 1.9 .frb pops a dialog)."""
    return ["-Djava.net.useSystemProxies=false",
            "-Dhttp.proxyHost=127.0.0.1", f"-Dhttp.proxyPort={DEAD_PORT}",
            "-Dhttps.proxyHost=127.0.0.1", f"-Dhttps.proxyPort={DEAD_PORT}",
            "-DsocksProxyHost=127.0.0.1", f"-DsocksProxyPort={DEAD_PORT}",
            f"-Djava.io.tmpdir={Path(work) / 'tmp'}", f"-Duser.home={Path(work) / 'home'}"]


def v2_settings(work: str, route_timeout: int) -> list[str]:
    """2.x: headless, a budget so it WRITES the SES before the kill, one
    thread, no analytics / API / MCP server, its data in the work dir."""
    budget = max(60, route_timeout - 60)
    hh, rem = divmod(budget, 3600)
    mm_, ss = divmod(rem, 60)
    return ["--gui.enabled=false", f"--router.job_timeout={hh:02d}:{mm_:02d}:{ss:02d}",
            "--feature_flags.multi_threading=false", "-da",
            f"--user_data_path={Path(work) / 'fr'}", "--api_server.enabled=false",
            "--mcp_server.enabled=false"]


def freerouting_args(java: str, jar: str, w_dsn: str, w_ses: str,
                     max_passes: int, route_timeout: int) -> list[str] | dict:
    """The Freerouting command line (F5-05's offline lists; the work dir is
    w_dsn's directory), or an {ok: False} dict when the platform cannot run
    this jar (1.x needs a display: xvfb-run on a headless Linux)."""
    work = str(Path(w_dsn).parent)
    is_v2 = "freerouting-2" in Path(jar).name
    # -dct 0: skip the "Autorouter is about to start" countdown dialog (1.9
    # and 2.x both take it) — no window may wait on a click in a hands-off run.
    args = [java, "-Xmx4g", *offline_jvm_args(work), "-jar", jar, "-de", w_dsn, "-do", w_ses,
            "-mp", str(max_passes), "-dct", "0"]
    args += v2_settings(work, route_timeout) if is_v2 else ["-da"]      # -da: no analytics
    # Freerouting 1.x initializes AWT/Swing even in CLI mode — on a
    # display-less Linux box (Docker, CI) it dies with HeadlessException
    # before routing anything. Wrap it in a virtual framebuffer.
    if not on_windows() and not os.environ.get("DISPLAY") and not is_v2:
        if not shutil.which("xvfb-run"):
            return {"ok": False, "reason":
                    "no display and xvfb-run not found — Freerouting 1.x "
                    "needs one (install xvfb, e.g. apt-get install xvfb)"}
        args = ["xvfb-run", "-a"] + args
    return args


def route(java: str, jar: str, dsn: str, ses: str, max_passes: int,
          route_timeout: int) -> dict:
    """Run Freerouting on a copy of the DSN in its own temp dir (cwd, tmp/,
    home/; no FREEROUTING__* env; on Windows behind the private-desktop
    launcher: 1.9 opens windows) and copy the SES back. {ok, [reason|stderr]}"""
    work = Path(tempfile.mkdtemp(prefix="frt_"))
    w_dsn, w_ses = str(work / "in.dsn"), str(work / "out.ses")
    shutil.copyfile(dsn, w_dsn)
    args = freerouting_args(java, jar, w_dsn, w_ses, max_passes, route_timeout)
    if isinstance(args, dict):
        shutil.rmtree(work, ignore_errors=True)
        return args
    for sub in ("tmp", "home"):
        (work / sub).mkdir()
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("FREEROUTING__")}
    if on_windows():
        args = [sys.executable, str(Path(__file__).resolve()), HIDDEN_DESKTOP, "--", *args]
    try:
        fr = tc.run(args, timeout=route_timeout, cwd=str(work), env=env)
        if Path(w_ses).is_file():
            shutil.copyfile(w_ses, ses)
        return {"ok": Path(ses).is_file(), "stderr": (fr.stdout + fr.stderr)[-1500:]}
    except subprocess.TimeoutExpired:
        return {"ok": False, "reason": f"Freerouting exceeded {route_timeout}s and was killed"}
    finally:
        shutil.rmtree(work, ignore_errors=True)


def unreadable(pcb: str, from_snapshot: bool) -> dict | None:
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


def autoroute_preflight(pcb: str, jar: str,
                        from_snapshot: bool = False) -> tuple[str, str, str] | dict:
    """(kicad_python, java, freerouting_jar) for autoroute_board, or the refusal
    dict: no pcbnew Python, a board it cannot read (one saved by the KiCad 11
    API session: route it with the board API tools, or pass from_snapshot to
    route its pre-KiCad-11 snapshot), no Java, no jar."""
    kpy = tc.kicad_python()
    if not kpy:
        return {"ok": False, "reason": "KiCad bundled Python not found"}
    refusal = unreadable(pcb, from_snapshot)
    if refusal:
        return refusal
    java = tc.find_java()
    if not java:
        return {"ok": False, "reason": "java not found (Freerouting needs a JRE/JDK)"}
    fj = tc.freerouting_jar(jar)
    if not fj:
        return {"ok": False,
                "reason": "freerouting jar not found in server/tools or $FREEROUTING_JAR"}
    return kpy, java, fj


def seed_refusal(dsn_seed: object) -> dict | None:
    """The refusal for a dsn_seed that is not an int >= 0, else None."""
    if isinstance(dsn_seed, bool) or not isinstance(dsn_seed, int) or dsn_seed < 0:
        return {"ok": False, "reason": "dsn_seed must be an integer >= 0 (0 = the canonical "
                                       f"DSN order, k > 0 = an alternate order), got {dsn_seed!r}"}
    return None


def run(pcb: str, max_passes: int = 100, jar: str = "",
        route_timeout: int = 300, from_snapshot: bool = False, dsn_seed: int = 0) -> dict:
    """autoroute_board's body: dsn_seed check, preflight, write guard, session
    release, the optional snapshot restore, then autoroute() (see the tool's
    docstring in board_pipeline_tools)."""
    bad_seed = seed_refusal(dsn_seed)
    if bad_seed:
        return bad_seed
    pre = autoroute_preflight(pcb, jar, from_snapshot)
    if isinstance(pre, dict):
        return pre
    kpy, java, fj = pre
    guard = ipc.write_guard(pcb)
    if guard:
        return guard
    held = board_build.release_board_session(pcb)
    if held.get("ok") is False:
        return held
    fallback = fallback_from_snapshot(pcb) if from_snapshot else {}
    if fallback.get("ok") is False:
        return {**fallback, **held}
    routed = autoroute(pcb, kpy, java, fj, max_passes, route_timeout, dsn_seed)
    return {**routed, **held, **fallback}


def fallback_from_snapshot(pcb: str) -> dict:
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


def export_dsn(kpy: str, pcb: str, dsn: str, dsn_seed: int) -> dict:
    """swig_export_dsn.py writes `dsn` (any older copy removed first, so a
    failed export can never route a stale DSN), then canonical_dsn rewrites
    it: {dsn_sha256} or the {ok: False, step: "dsn" | "dsn_canon"} refusal."""
    Path(dsn).unlink(missing_ok=True)
    d = tc.run([kpy, str(tc.TOOLS_DIR / "swig_export_dsn.py"), pcb, dsn], timeout=120)
    if not Path(dsn).is_file():
        return {"ok": False, "step": "dsn", "stderr": (d.stdout + d.stderr)[-1200:]}
    return canonical_dsn(dsn, dsn_seed)


def canonical_dsn(dsn: str, dsn_seed: int) -> dict:
    """Rewrite the exported DSN in dsn_canon's canonical order (dsn_seed 0)
    or the seed's alternate footprint order, venv side: {dsn_sha256}, or
    {ok: False, step: "dsn_canon", reason} when the DSN does not parse (the
    file is then left as exported)."""
    tools = str(tc.TOOLS_DIR)
    if tools not in sys.path:          # server/tools is not on sys.path under the MCP server
        sys.path.insert(0, tools)
    import dsn_canon
    try:
        return {"dsn_sha256": dsn_canon.canonicalize_file(dsn, seed=dsn_seed)}
    except (ValueError, OSError) as exc:          # DsnError is a ValueError
        return {"ok": False, "step": "dsn_canon", "dsn": dsn, "dsn_seed": dsn_seed,
                "reason": f"{type(exc).__name__}: {exc}"}


def autoroute(pcb: str, kpy: str, java: str, fj: str, max_passes: int,
              route_timeout: int, dsn_seed: int = 0) -> dict:
    """DSN export -> canonical DSN order -> Freerouting -> SES import + refill
    -> DRC unconnected count. Every result after the canonical DSN carries
    dsn_sha256 and dsn_seed."""
    base = str(Path(pcb).with_suffix(""))
    dsn, ses = base + ".dsn", base + ".ses"
    exported = export_dsn(kpy, pcb, dsn, dsn_seed)
    if exported.get("ok") is False:
        return exported
    stamp = {"dsn_sha256": exported["dsn_sha256"], "dsn_seed": dsn_seed}
    # remove any stale SES so a router timeout can't import a previous result
    Path(ses).unlink(missing_ok=True)
    routed = route(java, fj, dsn, ses, max_passes, route_timeout)
    if not routed["ok"]:
        return {"ok": False, "step": "route", **{k: v for k, v in routed.items() if k != "ok"},
                **stamp}
    im = tc.run([kpy, str(tc.TOOLS_DIR / "swig_import_ses.py"), pcb, ses], timeout=180)
    imp = parse_ses_import(im.stdout)
    if not imp["ok"]:
        return {"ok": False, "step": "import", "pcb": pcb, "dsn": dsn, "ses": ses,
                "reason": imp.get("error") or "SES import failed",
                "warnings": imp["warnings"], "import_log": (im.stdout + im.stderr)[-600:],
                **stamp}
    # verify route completeness — Freerouting may leave a stub (the same one every
    # time for one canonical DSN); report the unconnected count to the caller.
    unconnected = None
    drc_note = None
    if tc.board_cli(pcb):
        c = tc.drc_counts(pcb)
        unconnected = c["unconnected"] if "error" not in c else None
        drc_note = c.get("error")
    return {"ok": unconnected in (0, None), "pcb": pcb, "dsn": dsn, "ses": ses,
            "tracks": imp["tracks"], "vias": imp["vias"], "unconnected": unconnected,
            "warnings": imp["warnings"] + ([drc_note] if drc_note else []),
            "import_log": im.stdout.strip()[-300:], **stamp,
            "note": None if unconnected in (0, None) else
            f"{unconnected} net(s) left unrouted — the route is deterministic for this "
            "placement: retry with another dsn_seed or a looser placement (pack_scale)"}


def parse_ses_import(stdout: str) -> dict:
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


# script mode (Windows): the private-desktop launcher route() puts in front of Java
def _spawn_suspended(argv: list[str], desktop: str | None) -> tuple[int, int, int]:
    """CreateProcessW(argv) on `desktop`, suspended, no console window, with
    this launcher's std handles: (process handle, thread handle, pid)."""
    class StartupInfo(ctypes.Structure):      # STARTUPINFOW; dw[7] is dwFlags
        _fields_ = [("cb", w.DWORD), ("reserved", w.LPWSTR), ("desktop", w.LPWSTR),
                    ("title", w.LPWSTR), ("dw", w.DWORD * 8), ("show", w.WORD * 2),
                    ("reserved2", w.LPVOID), ("std", w.HANDLE * 3)]

    class ProcessInfo(ctypes.Structure):      # PROCESS_INFORMATION
        _fields_ = [("handles", w.HANDLE * 2), ("ids", w.DWORD * 2)]

    si, pi = StartupInfo(), ProcessInfo()
    si.cb, si.desktop, si.dw[7] = ctypes.sizeof(si), desktop, 0x100     # STARTF_USESTDHANDLES
    si.std[:] = [_winapi.GetStdHandle(n) for n in (
        _winapi.STD_INPUT_HANDLE, _winapi.STD_OUTPUT_HANDLE, _winapi.STD_ERROR_HANDLE)]
    cmd = ctypes.create_unicode_buffer(subprocess.list2cmdline(argv))
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    if not k32.CreateProcessW(None, cmd, None, None, True, 0x08000004,    # NO_WINDOW|SUSPENDED
                              None, None, ctypes.byref(si), ctypes.byref(pi)):
        raise ctypes.WinError(ctypes.get_last_error())
    return pi.handles[0], pi.handles[1], pi.ids[0]


def run_hidden(argv: list[str]) -> int:
    """Run argv on a new desktop nobody switches to; its exit code. Its job
    kills the child tree when this launcher dies (route timeout) or exits;
    its handles close then too. No desktop (non-interactive): current one."""
    from backends.child_guard import ChildGuard
    u32 = ctypes.WinDLL("user32", use_last_error=True)
    u32.CreateDesktopW.restype = w.HANDLE
    name = f"kicad_eda_fr_{os.getpid()}"
    desk = u32.CreateDesktopW(name, None, None, 0, 0x10000000, None)    # GENERIC_ALL
    if not desk:
        print(f"no private desktop ({ctypes.WinError(ctypes.get_last_error())}): "
              "Freerouting's windows may show", file=sys.stderr)
    try:
        proc, thread, pid = _spawn_suspended(argv, name if desk else None)  # this winstation
    except OSError as exc:
        print(f"cannot start {argv[0]}: {exc}", file=sys.stderr)
        return 1
    try:
        ChildGuard.bind(pid)
    except OSError as exc:
        print(f"Freerouting is not tied to its launcher: {exc}", file=sys.stderr)
    ctypes.WinDLL("kernel32").ResumeThread(w.HANDLE(thread))
    _winapi.WaitForSingleObject(proc, _winapi.INFINITE)
    return _winapi.GetExitCodeProcess(proc)


def main(argv: list[str]) -> int:
    """`board_autoroute.py --hidden-desktop -- <command...>` (Windows); 2 = usage."""
    if len(argv) < 3 or argv[:2] != [HIDDEN_DESKTOP, "--"] or not on_windows():
        print(f"usage (Windows): board_autoroute.py {HIDDEN_DESKTOP} -- <command> [args...]",
              file=sys.stderr)
        return 2
    return run_hidden(argv[2:])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
