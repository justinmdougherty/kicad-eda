"""Freerouting command line and launch (F5-14, from the F5-05 spike; 1.9.0
stays the router).

Every version phones home (1.9: Segment analytics + a GitHub version check;
2.x: api.freerouting.app + the same check, which no flag turns off), so every
child gets a JVM proxy block aimed at a dead local port plus its own
tmp/home before -jar; -da turns the analytics off; 2.x additionally runs
single-threaded with no API / MCP server and its data under the work dir.
route() runs the child in that work dir (1.9 writes logs/ to its cwd) and,
on Windows, through this module's private-desktop launcher (1.9 always
opens Swing windows). No confirmation dialog may wait on a click (-dct 0).

Unit tier: no Java needed. The launcher tests run a Python child (Windows
only) to prove the private desktop, the exit code / std handle pass-through
and the kill-on-close job.
"""
from __future__ import annotations

import ctypes
import subprocess
import sys
from pathlib import Path

import pytest

import board_autoroute as ba
import board_pipeline_tools as bpt
import toolchain as tc

MonkeyPatch = pytest.MonkeyPatch
JARS = ("/x/freerouting-1.9.0.jar", "/x/freerouting-2.1.0.jar", "/x/freerouting-2.4.1.jar")
WINDOWS_ONLY = pytest.mark.skipif(sys.platform != "win32", reason="Windows private desktop")


def _work(tmp_path: Path) -> Path:
    return tmp_path / "frt_work"


def _args(jar: str, tmp_path: Path, route_timeout: int = 600) -> list[str]:
    w = _work(tmp_path)
    args = bpt._freerouting_args("java", jar, str(w / "in.dsn"), str(w / "out.ses"), 20,
                                 route_timeout)
    assert isinstance(args, list), args
    return args


def _block(work: Path) -> list[str]:
    """F5-05 offline_argument_lists.jvm_block_all_versions, in its order."""
    return ["-Djava.net.useSystemProxies=false",
            "-Dhttp.proxyHost=127.0.0.1", "-Dhttp.proxyPort=9",
            "-Dhttps.proxyHost=127.0.0.1", "-Dhttps.proxyPort=9",
            "-DsocksProxyHost=127.0.0.1", "-DsocksProxyPort=9",
            f"-Djava.io.tmpdir={work / 'tmp'}", f"-Duser.home={work / 'home'}"]


@pytest.fixture
def linux(monkeypatch: MonkeyPatch) -> None:
    """A display-less Linux box with xvfb-run installed (Docker / CI)."""
    monkeypatch.setattr(ba, "on_windows", lambda: False)
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.setattr(ba.shutil, "which",
                        lambda name: "/usr/bin/xvfb-run" if name == "xvfb-run" else None)


# ----------------------------------------------------------------------------
# freerouting_args: the offline lists
# ----------------------------------------------------------------------------
def test_v1_skips_the_autorouter_countdown_dialog(tmp_path: Path) -> None:
    args = _args("/x/freerouting-1.9.0.jar", tmp_path)
    i = args.index("-dct")
    assert args[i + 1] == "0"


def test_v2_passes_dct_once_and_stays_headless(tmp_path: Path) -> None:
    args = _args("/x/freerouting-2.1.0.jar", tmp_path)
    assert args.count("-dct") == 1
    assert "--gui.enabled=false" in args


@pytest.mark.parametrize("jar", JARS)
def test_the_network_block_precedes_jar_for_every_version(jar: str, tmp_path: Path,
                                                          monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(ba, "on_windows", lambda: True)          # no xvfb prefix
    args = _args(jar, tmp_path)
    jar_at = args.index("-jar")
    assert args[:jar_at] == ["java", "-Xmx4g", *_block(_work(tmp_path))]
    assert args[jar_at + 1] == jar and args.count("-jar") == 1
    assert args.count("-dct") == 1 and args.count("-da") == 1
    assert args.index("-da") > jar_at                               # an app flag, not a JVM one
    assert not [a for a in args[jar_at:] if a.startswith("-D")]


def test_v19_runs_exactly_the_spike_list(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(ba, "on_windows", lambda: True)
    w = _work(tmp_path)
    assert _args("/x/freerouting-1.9.0.jar", tmp_path) == [
        "java", "-Xmx4g", *_block(w), "-jar", "/x/freerouting-1.9.0.jar",
        "-de", str(w / "in.dsn"), "-do", str(w / "out.ses"), "-mp", "20", "-dct", "0", "-da"]


def test_v2_runs_exactly_the_spike_list(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(ba, "on_windows", lambda: True)
    w = _work(tmp_path)
    assert _args("/x/freerouting-2.4.1.jar", tmp_path) == [
        "java", "-Xmx4g", *_block(w), "-jar", "/x/freerouting-2.4.1.jar",
        "-de", str(w / "in.dsn"), "-do", str(w / "out.ses"), "-mp", "20", "-dct", "0",
        "--gui.enabled=false", "--router.job_timeout=00:09:00",
        "--feature_flags.multi_threading=false", "-da", f"--user_data_path={w / 'fr'}",
        "--api_server.enabled=false", "--mcp_server.enabled=false"]


def test_v2_never_asks_for_multi_threading(tmp_path: Path) -> None:
    args = _args("/x/freerouting-2.1.0.jar", tmp_path)
    assert "--feature_flags.multi_threading=false" in args
    assert "--feature_flags.multi_threading=true" not in args


def test_v1_gets_no_2x_settings(tmp_path: Path) -> None:
    args = _args("/x/freerouting-1.9.0.jar", tmp_path)
    assert not [a for a in args if a.startswith("--")]


@pytest.mark.parametrize("route_timeout, budget", [(600, "00:09:00"), (1500, "00:24:00"),
                                                   (90, "00:01:00"), (7260, "02:00:00")])
def test_v2_job_timeout_leaves_a_minute_before_the_kill(route_timeout: int, budget: str,
                                                        tmp_path: Path) -> None:
    args = _args("/x/freerouting-2.4.1.jar", tmp_path, route_timeout)
    assert f"--router.job_timeout={budget}" in args


# ----------------------------------------------------------------------------
# xvfb-run wrapping (unchanged): 1.x on a display-less Linux only
# ----------------------------------------------------------------------------
def test_v1_on_a_displayless_linux_runs_under_xvfb(linux: None, tmp_path: Path) -> None:
    args = _args("/x/freerouting-1.9.0.jar", tmp_path)
    block = _block(_work(tmp_path))
    assert args[:4] == ["xvfb-run", "-a", "java", "-Xmx4g"]
    assert args[4:4 + len(block)] == block and args[4 + len(block)] == "-jar"


def test_v1_without_xvfb_is_refused(linux: None, tmp_path: Path,
                                    monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(ba.shutil, "which", lambda name: None)
    w = _work(tmp_path)
    res = ba.freerouting_args("java", "/x/freerouting-1.9.0.jar", str(w / "in.dsn"),
                              str(w / "out.ses"), 20, 600)
    assert isinstance(res, dict) and res["ok"] is False and "xvfb-run" in res["reason"]


@pytest.mark.parametrize("jar", JARS[1:])
def test_v2_never_needs_xvfb(jar: str, linux: None, tmp_path: Path) -> None:
    assert _args(jar, tmp_path)[0] == "java"


def test_a_display_or_windows_needs_no_xvfb(linux: None, tmp_path: Path,
                                            monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("DISPLAY", ":0")
    assert _args("/x/freerouting-1.9.0.jar", tmp_path)[0] == "java"
    monkeypatch.delenv("DISPLAY")
    monkeypatch.setattr(ba, "on_windows", lambda: True)
    assert _args("/x/freerouting-1.9.0.jar", tmp_path)[0] == "java"


# ----------------------------------------------------------------------------
# route(): work dir, cwd, env, launcher
# ----------------------------------------------------------------------------
class FakeRouter:
    """tc.run standing in for the Freerouting child: records argv / kwargs and
    what the work dir held when it started, then writes the SES."""

    def __init__(self, ses: bool = True) -> None:
        self.ses = ses
        self.calls: list[tuple[list[str], dict]] = []
        self.dirs: list[set[str]] = []

    def __call__(self, args: list[str], **kw: object) -> subprocess.CompletedProcess:
        self.calls.append((list(args), dict(kw)))
        work = Path(str(kw["cwd"]))
        self.dirs.append({p.name for p in work.iterdir()})
        if self.ses:
            Path(args[args.index("-do") + 1]).write_text("(session out)", encoding="utf-8")
        return subprocess.CompletedProcess(args, 0, stdout="routed\n", stderr="")


def _route(tmp_path: Path, monkeypatch: MonkeyPatch, router: FakeRouter,
           jar: str = "/x/freerouting-1.9.0.jar") -> dict:
    dsn = tmp_path / "board.dsn"
    dsn.write_text("(pcb board)", encoding="utf-8")
    monkeypatch.setattr(tc, "run", router)
    return ba.route("java", jar, str(dsn), str(tmp_path / "board.ses"), 600, 1500)


def test_route_runs_the_child_in_its_own_work_dir(tmp_path: Path,
                                                  monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(ba, "on_windows", lambda: False)
    monkeypatch.setenv("DISPLAY", ":0")
    router = FakeRouter()
    res = _route(tmp_path, monkeypatch, router)
    assert res["ok"] is True and "routed" in res["stderr"], res
    assert (tmp_path / "board.ses").read_text(encoding="utf-8") == "(session out)"
    (args, kw), = router.calls
    work = Path(str(kw["cwd"]))
    assert Path(args[args.index("-de") + 1]).parent == work            # the DSN copy is in it
    assert router.dirs == [{"in.dsn", "tmp", "home"}]                   # made before launch
    assert f"-Djava.io.tmpdir={work / 'tmp'}" in args and kw["timeout"] == 1500
    assert args == ba.freerouting_args("java", "/x/freerouting-1.9.0.jar",
                                       str(work / "in.dsn"), str(work / "out.ses"), 600, 1500)
    assert not work.exists()                                            # cleaned up


def test_route_drops_freerouting_settings_from_the_child_env(tmp_path: Path,
                                                             monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("FREEROUTING__ROUTER__MAX_PASSES", "1")
    monkeypatch.setenv("freerouting__gui__enabled", "true")
    monkeypatch.setenv("KICAD_EDA_KEEP_ME", "yes")
    router = FakeRouter()
    _route(tmp_path, monkeypatch, router, jar="/x/freerouting-2.4.1.jar")
    env = router.calls[0][1]["env"]
    assert isinstance(env, dict) and env["KICAD_EDA_KEEP_ME"] == "yes"
    assert not [k for k in env if k.upper().startswith("FREEROUTING__")]


def test_on_windows_route_goes_through_the_private_desktop_launcher(
        tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(ba, "on_windows", lambda: True)
    router = FakeRouter()
    assert _route(tmp_path, monkeypatch, router)["ok"] is True
    args, kw = router.calls[0]
    work = Path(str(kw["cwd"]))
    assert args[:4] == [sys.executable, str(Path(ba.__file__).resolve()), "--hidden-desktop",
                        "--"]
    assert args[4:] == ba.freerouting_args("java", "/x/freerouting-1.9.0.jar",
                                           str(work / "in.dsn"), str(work / "out.ses"), 600,
                                           1500)


def test_elsewhere_route_runs_freerouting_directly(linux: None, tmp_path: Path,
                                                   monkeypatch: MonkeyPatch) -> None:
    router = FakeRouter()
    assert _route(tmp_path, monkeypatch, router)["ok"] is True
    assert router.calls[0][0][:3] == ["xvfb-run", "-a", "java"]


def test_route_without_an_ses_fails_and_cleans_up(tmp_path: Path,
                                                  monkeypatch: MonkeyPatch) -> None:
    router = FakeRouter(ses=False)
    res = _route(tmp_path, monkeypatch, router)
    assert res["ok"] is False and not (tmp_path / "board.ses").exists()
    assert not Path(str(router.calls[0][1]["cwd"])).exists()


def test_route_timeout_is_reported_and_cleaned_up(tmp_path: Path,
                                                  monkeypatch: MonkeyPatch) -> None:
    seen: list[str] = []

    def hang(args: list[str], **kw: object) -> subprocess.CompletedProcess:
        seen.append(str(kw["cwd"]))
        raise subprocess.TimeoutExpired(args, 1500)

    dsn = tmp_path / "board.dsn"
    dsn.write_text("(pcb board)", encoding="utf-8")
    monkeypatch.setattr(tc, "run", hang)
    res = ba.route("java", "/x/freerouting-1.9.0.jar", str(dsn), str(tmp_path / "b.ses"), 60,
                   1500)
    assert res == {"ok": False, "reason": "Freerouting exceeded 1500s and was killed"}
    assert not Path(seen[0]).exists()


def test_route_refusal_runs_nothing(linux: None, tmp_path: Path,
                                    monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(ba.shutil, "which", lambda name: None)
    monkeypatch.setattr(tc, "run", lambda args, **kw: pytest.fail(f"ran {args[:2]}"))
    dsn = tmp_path / "board.dsn"
    dsn.write_text("(pcb board)", encoding="utf-8")
    res = ba.route("java", "/x/freerouting-1.9.0.jar", str(dsn), str(tmp_path / "b.ses"), 60, 60)
    assert res["ok"] is False and "xvfb-run" in res["reason"]


# ----------------------------------------------------------------------------
# the launcher (this module run as a script; Windows)
# ----------------------------------------------------------------------------
def _launcher(*command: str) -> list[str]:
    return [sys.executable, str(Path(ba.__file__).resolve()), "--hidden-desktop", "--", *command]


@pytest.mark.parametrize("argv", [[], ["--hidden-desktop"], ["--hidden-desktop", "--"],
                                  ["--other", "--", "x"]])
def test_the_launcher_refuses_a_bad_command_line(argv: list[str]) -> None:
    assert ba.main(argv) == 2


def test_the_launcher_is_windows_only(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(ba, "on_windows", lambda: False)
    assert ba.main(["--hidden-desktop", "--", sys.executable, "-c", "raise SystemExit(5)"]) == 2


DESKTOP_PROBE = (
    "import ctypes, sys\n"
    "from ctypes import wintypes as w\n"
    "u = ctypes.WinDLL('user32')\n"
    "u.GetThreadDesktop.restype = w.HANDLE\n"
    "u.GetUserObjectInformationW.argtypes = (w.HANDLE, ctypes.c_int, ctypes.c_void_p,"
    " w.DWORD, ctypes.c_void_p)\n"
    "h = u.GetThreadDesktop(ctypes.WinDLL('kernel32').GetCurrentThreadId())\n"
    "buf = ctypes.create_unicode_buffer(256)\n"
    "u.GetUserObjectInformationW(h, 2, buf, ctypes.sizeof(buf), None)\n"
    "print('desktop=' + buf.value, flush=True)\n"
    "print('to stderr', file=sys.stderr)\n"
    "sys.exit(3)\n")


@WINDOWS_ONLY
def test_the_launcher_runs_the_child_on_a_private_desktop() -> None:
    r = tc.run(_launcher(sys.executable, "-c", DESKTOP_PROBE), timeout=120)
    assert r.returncode == 3, r.stderr
    assert "desktop=kicad_eda_fr_" in r.stdout, (r.stdout, r.stderr)
    assert "to stderr" in r.stderr


@WINDOWS_ONLY
def test_the_launcher_reports_a_command_that_cannot_start(tmp_path: Path) -> None:
    r = tc.run(_launcher(str(tmp_path / "no_such_java.exe"), "-version"), timeout=120)
    assert r.returncode == 1 and "cannot start" in r.stderr, (r.returncode, r.stderr)


def _alive(pid: int) -> bool:
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = ctypes.c_void_p
    k32.WaitForSingleObject.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
    k32.CloseHandle.argtypes = (ctypes.c_void_p,)
    handle = k32.OpenProcess(0x00100000, False, pid)                   # SYNCHRONIZE
    if not handle:
        return False
    try:
        return k32.WaitForSingleObject(handle, 15000) != 0              # not signalled in 15 s
    finally:
        k32.CloseHandle(handle)


@WINDOWS_ONLY
def test_killing_the_launcher_on_timeout_ends_the_child_tree() -> None:
    sleeper = "import os, time\nprint(os.getpid(), flush=True)\ntime.sleep(300)\n"
    with pytest.raises(subprocess.TimeoutExpired) as caught:
        tc.run(_launcher(sys.executable, "-c", sleeper), timeout=20)
    out = caught.value.stdout or ""
    out = out.decode() if isinstance(out, bytes) else out
    pid = int(out.split()[0])
    assert not _alive(pid), f"the launched child {pid} outlived its launcher"
