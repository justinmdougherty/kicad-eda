"""kicad_env — multi-install KiCad discovery: version parsing, search order,
de-duplication, per-path probe caching, the pick_* selectors and the vendored
kipy sys.path helper.

The fake cases build install trees under tmp_path and feed canned probe
answers through kicad_env._run, so they run on any OS with no KiCad present.
The live case re-scans the real machine and skips when no kicad-cli exists.
"""
from __future__ import annotations

import os
import sys
import types
from dataclasses import dataclass, field
from pathlib import Path

import pytest

import kicad_env as env

PCBNEW_MISSING = "ModuleNotFoundError: No module named 'pcbnew'"


# ----------------------------------------------------------------------------
# fakes
# ----------------------------------------------------------------------------
@dataclass
class FakeProbes:
    """Canned answers keyed by kicad-cli / python path; records every call."""
    versions: dict[str, str] = field(default_factory=dict)     # cli -> version text
    api_ok: set[str] = field(default_factory=set)               # cli with api-server
    pcbnew_ok: set[str] = field(default_factory=set)            # python importing pcbnew
    calls: list[list[str]] = field(default_factory=list)

    def run(self, argv: list[str], timeout_s: float = 0) -> env.ProbeResult:
        self.calls.append(list(argv))
        exe, args = env._key(argv[0]), argv[1:]
        if args == ["version"]:
            if exe in self.versions:
                return env.ProbeResult(0, self.versions[exe] + "\n", "")
            return env.ProbeResult(1, "", "not a kicad-cli")
        if args == ["api-server", "--help"]:
            return env.ProbeResult(0 if exe in self.api_ok else 1, "", "unknown command")
        if args == ["-c", "import pcbnew"]:
            return env.ProbeResult(0 if exe in self.pcbnew_ok else 1, "", PCBNEW_MISSING)
        raise AssertionError(f"unexpected probe {argv}")

    def count(self, exe: Path, *args: str) -> int:
        return sum(1 for c in self.calls if env._key(c[0]) == env._key(exe) and c[1:] == list(args))


def make_install(root: Path, *, python: bool, share: bool = True) -> Path:
    """A fake <root>/bin/kicad-cli.exe [+ python.exe] [+ share/kicad/{symbols,footprints}]."""
    (root / "bin").mkdir(parents=True)
    cli = root / "bin" / "kicad-cli.exe"
    cli.write_text("")
    if python:
        (root / "bin" / "python.exe").write_text("")
    if share:
        for sub in ("symbols", "footprints"):
            (root / "share" / "kicad" / sub).mkdir(parents=True)
    return cli


@pytest.fixture
def fake_world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeProbes:
    """A Windows-shaped machine with empty Program Files / LOCALAPPDATA, no
    KICAD_PATH and nothing on PATH; every probe goes through FakeProbes."""
    probes = FakeProbes()
    env.clear_cache()
    monkeypatch.setattr(env, "IS_WINDOWS", True)
    monkeypatch.setattr(env, "CLI_NAME", "kicad-cli.exe")
    monkeypatch.setattr(env, "_run", probes.run)
    monkeypatch.setattr(env, "_which", lambda name: None)
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "pf"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "lad"))
    monkeypatch.delenv("KICAD_PATH", raising=False)
    (tmp_path / "pf" / "KiCad").mkdir(parents=True)
    (tmp_path / "lad" / "Programs" / "KiCad").mkdir(parents=True)
    yield probes
    env.clear_cache()


@pytest.fixture
def two_installs(tmp_path: Path, fake_world: FakeProbes) -> tuple[Path, Path]:
    """Program Files\\KiCad\\9.0 (bundled Python imports pcbnew) and \\10.99
    (no Python, api-server present) -> (cli9, cli11)."""
    cli9 = make_install(tmp_path / "pf" / "KiCad" / "9.0", python=True)
    cli11 = make_install(tmp_path / "pf" / "KiCad" / "10.99", python=False)
    fake_world.versions[env._key(cli9)] = "9.0.4"
    fake_world.versions[env._key(cli11)] = "10.99.0-4025-gf7ca620278"
    fake_world.api_ok.add(env._key(cli11))
    fake_world.pcbnew_ok.add(env._key(cli9.parent / "python.exe"))
    return cli9, cli11


# ----------------------------------------------------------------------------
# parsing
# ----------------------------------------------------------------------------
@pytest.mark.parametrize("text,expected", [
    ("9.0.4", (9, 0, 4)),
    ("9.0.4\r\n", (9, 0, 4)),
    ("10.99.0", (10, 99, 0)),
    ("10.99.0-4025-gf7ca620278", (10, 99, 0)),
    ("9.0", (9, 0, 0)),
    ("Version: 9.0.4", (0, 0, 0)),
    ("", (0, 0, 0)),
    ("\n\n8.0.7\nextra line", (8, 0, 7)),
])
def test_parse_version(text: str, expected: tuple[int, int, int]) -> None:
    assert env.parse_version(text) == expected


def test_install_requires_three_part_version_tuple(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="3 parts"):
        env.KiCadInstall("9.0", (9, 0), tmp_path, tmp_path / "kicad-cli")


# ----------------------------------------------------------------------------
# discovery on fake trees
# ----------------------------------------------------------------------------
def test_discover_newest_first_and_pickers(two_installs: tuple[Path, Path]) -> None:
    cli9, cli11 = two_installs
    installs = env.discover()
    assert [i.version_tuple for i in installs] == [(10, 99, 0), (9, 0, 4)]
    nightly, stable = installs
    assert nightly.kicad_cli == cli11 and nightly.root == cli11.parent.parent
    assert nightly.has_api_server and nightly.python is None
    assert nightly.python_error == f"no bundled Python found beside {cli11}"
    assert stable.python == cli9.parent / "python.exe" and stable.python_error is None
    assert not stable.has_api_server
    assert env.pick_cli() is nightly
    assert env.pick_api() is nightly
    assert env.pick_swig() is stable


def test_share_dirs_and_as_dict(two_installs: tuple[Path, Path]) -> None:
    cli9, cli11 = two_installs
    stable = env.pick_swig()
    assert env.symbols_dir(stable) == cli9.parent.parent / "share" / "kicad" / "symbols"
    assert env.footprints_dir(stable) == cli9.parent.parent / "share" / "kicad" / "footprints"
    assert env.symbols_dir(None) is None and env.footprints_dir(None) is None
    d = stable.as_dict()
    assert d["version"] == "9.0.4" and d["version_tuple"] == [9, 0, 4]
    assert d["python"] == str(cli9.parent / "python.exe") and d["python_error"] is None
    assert d["kicad_cli"] == str(cli9) and d["has_api_server"] is False
    assert Path(d["share"]).name == "kicad"


def test_install_without_share_has_no_library_dirs(tmp_path: Path, fake_world: FakeProbes) -> None:
    cli = make_install(tmp_path / "pf" / "KiCad" / "9.0", python=False, share=False)
    fake_world.versions[env._key(cli)] = "9.0.4"
    (inst,) = env.discover()
    assert inst.share is None
    assert env.symbols_dir(inst) is None and env.footprints_dir(inst) is None


@pytest.mark.parametrize("sub", ["", "bin"])
def test_kicad_path_env_root_or_bin_dedupes_with_program_files(
        sub: str, two_installs: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    cli9, _cli11 = two_installs
    monkeypatch.setenv("KICAD_PATH", str(cli9.parent.parent / sub))
    installs = env.discover(refresh=True)
    assert [i.kicad_cli for i in installs] == [_cli11, cli9]


def test_localappdata_and_path_sources(tmp_path: Path, fake_world: FakeProbes,
                                       monkeypatch: pytest.MonkeyPatch) -> None:
    cli_lad = make_install(tmp_path / "lad" / "Programs" / "KiCad" / "8.0", python=False)
    cli_path = make_install(tmp_path / "elsewhere" / "kicad", python=False)
    fake_world.versions[env._key(cli_lad)] = "8.0.7"
    fake_world.versions[env._key(cli_path)] = "9.0.1"
    monkeypatch.setattr(env, "_which", lambda name: str(cli_path) if name == "kicad-cli" else None)
    assert [i.kicad_cli for i in env.discover()] == [cli_path, cli_lad]


def test_nothing_installed(fake_world: FakeProbes) -> None:
    assert env.discover() == []
    assert env.pick_cli() is None and env.pick_swig() is None and env.pick_api() is None
    assert fake_world.calls == []


# ----------------------------------------------------------------------------
# probes: caching and failure reporting
# ----------------------------------------------------------------------------
def test_probes_run_once_per_path(two_installs: tuple[Path, Path], fake_world: FakeProbes) -> None:
    cli9, cli11 = two_installs
    first = env.discover()
    assert env.discover() == first                    # cached list, no new probes
    calls_after_first = len(fake_world.calls)
    env.discover(refresh=True)                        # re-scan, but probes stay cached
    assert len(fake_world.calls) == calls_after_first
    assert fake_world.count(cli9, "version") == 1
    assert fake_world.count(cli11, "version") == 1
    assert fake_world.count(cli9.parent / "python.exe", "-c", "import pcbnew") == 1
    assert fake_world.count(cli11, "api-server", "--help") == 1
    assert fake_world.count(cli9, "api-server", "--help") == 0   # too old to bother


def test_clear_cache_reprobes(two_installs: tuple[Path, Path], fake_world: FakeProbes) -> None:
    env.discover()
    env.clear_cache()
    env.discover()
    assert fake_world.count(two_installs[0], "version") == 2


def test_version_probe_failure_skips_install(tmp_path: Path, fake_world: FakeProbes) -> None:
    good = make_install(tmp_path / "pf" / "KiCad" / "9.0", python=False)
    bad = make_install(tmp_path / "pf" / "KiCad" / "broken", python=False)
    fake_world.versions[env._key(good)] = "9.0.4"
    installs = env.discover()
    assert [i.kicad_cli for i in installs] == [good]
    assert env.version_probe_error(bad) == f"{bad} version: not a kicad-cli"
    assert env.probe_errors() == [f"{bad} version: not a kicad-cli"]
    assert env.version_probe_error(good) is None


def test_python_probe_failure_keeps_reason(tmp_path: Path, fake_world: FakeProbes) -> None:
    cli = make_install(tmp_path / "pf" / "KiCad" / "9.0", python=True)
    fake_world.versions[env._key(cli)] = "9.0.4"          # python.exe exists, pcbnew does not
    (inst,) = env.discover()
    assert inst.python is None
    assert inst.python_error == f"{cli.parent / 'python.exe'}: cannot import pcbnew: {PCBNEW_MISSING}"
    assert env.pick_swig() is None


def test_api_server_needs_version_and_probe(tmp_path: Path, fake_world: FakeProbes) -> None:
    old = make_install(tmp_path / "pf" / "KiCad" / "9.0", python=False)
    new = make_install(tmp_path / "pf" / "KiCad" / "10.99", python=False)
    fake_world.versions[env._key(old)] = "9.0.4"
    fake_world.versions[env._key(new)] = "10.99.0"        # subcommand probe will fail
    nightly, stable = env.discover()
    assert not nightly.has_api_server and not stable.has_api_server
    assert env.pick_api() is None
    assert fake_world.count(old, "api-server", "--help") == 0
    assert fake_world.count(new, "api-server", "--help") == 1


def test_run_reports_launch_failure_and_timeout(tmp_path: Path) -> None:
    missing = env._run([str(tmp_path / "no-such-kicad-cli.exe"), "version"])
    assert missing.returncode == env.TRANSIENT_RC and not missing.ok and "cannot start" in missing.stderr
    slow = env._run([sys.executable, "-c", "import time; time.sleep(30)"], timeout_s=0.5)
    assert slow.returncode == env.TRANSIENT_RC and "timed out after 0.5 s" in slow.stderr


def test_run_detaches_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    """Children must not inherit the MCP server's stdin pipe: a python.exe
    that does blocks at start-up until the timeout (seen on Windows)."""
    seen: dict = {}

    def fake_run(argv, **kw):
        seen.update(kw)
        return types.SimpleNamespace(returncode=0, stdout="9.0.4", stderr="")
    monkeypatch.setattr(env.subprocess, "run", fake_run)
    assert env._run(["kicad-cli", "version"]).ok
    assert seen["stdin"] is env.subprocess.DEVNULL
    assert seen["capture_output"] is True and seen["timeout"] == env.PROBE_TIMEOUT_S


def test_python_probe_uses_the_longer_timeout(tmp_path: Path, fake_world: FakeProbes,
                                               monkeypatch: pytest.MonkeyPatch) -> None:
    timeouts: list[float] = []

    def run(argv, timeout_s=0):
        timeouts.append(timeout_s)
        return fake_world.run(argv, timeout_s)
    monkeypatch.setattr(env, "_run", run)
    cli = make_install(tmp_path / "pf" / "KiCad" / "9.0", python=True)
    fake_world.versions[env._key(cli)] = "9.0.4"
    fake_world.pcbnew_ok.add(env._key(cli.parent / "python.exe"))
    env.discover()
    assert env.PYTHON_PROBE_TIMEOUT_S in timeouts and env.PYTHON_PROBE_TIMEOUT_S > env.PROBE_TIMEOUT_S


def test_python_probe_timeout_is_not_cached(tmp_path: Path, fake_world: FakeProbes,
                                            monkeypatch: pytest.MonkeyPatch) -> None:
    """A probe that timed out (or could not start) settles nothing: the next
    discovery probes again instead of disabling board builds for the process."""
    cli = make_install(tmp_path / "pf" / "KiCad" / "9.0", python=True)
    py = cli.parent / "python.exe"
    fake_world.versions[env._key(cli)] = "9.0.4"
    slow = {"left": 1}

    def run(argv, timeout_s=0):
        if argv[1:] == ["-c", "import pcbnew"] and slow["left"]:
            slow["left"] -= 1
            fake_world.calls.append(list(argv))
            return env.ProbeResult(env.TRANSIENT_RC, "", f"{argv[0]}: timed out after {timeout_s:g} s")
        return fake_world.run(argv, timeout_s)
    monkeypatch.setattr(env, "_run", run)
    fake_world.pcbnew_ok.add(env._key(py))

    (first,) = env.discover()
    assert first.python is None and "timed out after 90 s" in (first.python_error or "")
    assert env.python_probe_error(cli) and "timed out" in env.python_probe_error(cli)
    (second,) = env.discover(refresh=True)                 # probes again, now answers
    assert second.python == py and second.python_error is None
    assert env.python_probe_error(cli) is None
    assert fake_world.count(py, "-c", "import pcbnew") == 2
    env.discover(refresh=True)                             # a settled answer IS cached
    assert fake_world.count(py, "-c", "import pcbnew") == 2


def test_python_probe_real_failure_is_cached(tmp_path: Path, fake_world: FakeProbes) -> None:
    cli = make_install(tmp_path / "pf" / "KiCad" / "9.0", python=True)
    fake_world.versions[env._key(cli)] = "9.0.4"          # pcbnew import fails for real
    env.discover()
    env.discover(refresh=True)
    assert fake_world.count(cli.parent / "python.exe", "-c", "import pcbnew") == 1
    assert env.python_probe_error(cli) and PCBNEW_MISSING in env.python_probe_error(cli)


# ----------------------------------------------------------------------------
# vendored kipy
# ----------------------------------------------------------------------------
def test_vendor_dir_holds_kipy() -> None:
    assert (env.VENDOR_DIR / "kipy" / "__init__.py").is_file()
    assert (env.VENDOR_DIR / "kipy" / "proto" / "schematic" / "schematic_types_pb2.py").is_file()
    assert (env.VENDOR_DIR / "kipy-LICENSE").is_file()


def test_use_vendored_kipy_puts_vendor_first(monkeypatch: pytest.MonkeyPatch) -> None:
    vendor = str(env.VENDOR_DIR)
    monkeypatch.setattr(sys, "path", ["/somewhere", vendor, "/elsewhere"])
    monkeypatch.delitem(sys.modules, "kipy", raising=False)
    assert env.use_vendored_kipy() == env.VENDOR_DIR
    assert sys.path == [vendor, "/somewhere", "/elsewhere"]
    env.use_vendored_kipy()                              # idempotent
    assert sys.path == [vendor, "/somewhere", "/elsewhere"]


def test_use_vendored_kipy_refuses_foreign_kipy(monkeypatch: pytest.MonkeyPatch) -> None:
    foreign = types.ModuleType("kipy")
    foreign.__file__ = os.path.join("site-packages", "kipy", "__init__.py")
    monkeypatch.setitem(sys.modules, "kipy", foreign)
    with pytest.raises(RuntimeError, match="already imported from"):
        env.use_vendored_kipy()


# ----------------------------------------------------------------------------
# live machine
# ----------------------------------------------------------------------------
@pytest.mark.skipif(not env.candidate_clis(), reason="no KiCad install on this machine")
def test_live_discovery() -> None:
    env.clear_cache()
    installs = env.discover(refresh=True)
    assert installs and installs == env.discover()
    assert [i.version_tuple for i in installs] == sorted(
        (i.version_tuple for i in installs), reverse=True)
    for inst in installs:
        assert inst.kicad_cli.is_file() and inst.version_tuple > (0, 0, 0)
        assert inst.share is None or inst.share.is_dir()
        assert inst.python is None or Path(inst.python).is_file()
        assert (inst.python is None) == (inst.python_error is not None)
        if inst.has_api_server:
            assert inst.version_tuple >= env.API_SERVER_MIN_VERSION
    assert env.pick_cli() is installs[0]
    if env.pick_api():
        assert env.pick_api().has_api_server
    if env.pick_swig():
        assert env.pick_swig().python is not None
    assert env.probe_errors() == []
