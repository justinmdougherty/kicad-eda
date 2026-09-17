"""KiCad installation discovery shared by the MCP server, the pipeline and tests.

More than one KiCad can be installed at once (a 9.0 stable for the SWIG board
builder, a 10.99 nightly for the IPC schematic API). This module finds them all,
probes what each one can do, and picks the right one per job.

Usage (drop-in for kicad_server's discovery helpers)::

    import kicad_env as env
    cli  = env.pick_cli()    # newest install: schematic ERC / render (reads any file)
    swig = env.pick_swig()   # newest install whose bundled Python imports pcbnew
    api  = env.pick_api()    # newest install whose kicad-cli has `api-server`

    schematic_cli = str(cli.kicad_cli)  if cli  else None
    board_cli     = str(swig.kicad_cli) if swig else schematic_cli   # toolchain.board_cli()
    kicad_python  = str(swig.python)    if swig else None
    symbols      = env.symbols_dir(cli)       # newest symbol libraries
    footprints   = env.footprints_dir(swig)   # footprints for the SWIG board builder
    status["installs"] = [i.as_dict() for i in env.discover()]

    env.use_vendored_kipy()   # server/vendor first on sys.path, THEN `import kipy`

Search order for kicad-cli (every hit is probed, duplicates collapse, the result
is sorted newest first): $KICAD_PATH (root or bin), %ProgramFiles%\\KiCad\\*,
%LOCALAPPDATA%\\Programs\\KiCad\\*, PATH, /usr/bin, /usr/local/bin and the macOS
app bundle. Each probe (`kicad-cli version`, the bundled Python's
`import pcbnew`, `kicad-cli api-server --help`) runs once per path per process.

A file written by KiCad N can only be read by kicad-cli >= N, so schematics are
always ERC'd and rendered with pick_cli() (the newest — the API backend writes
KiCad 11 files). Boards are the other way round: they are built by pick_swig()'s
pcbnew, so their DRC and fab exports use THAT install's kicad-cli
(toolchain.board_cli), never a newer nightly whose rules or Gerber output may
differ from the release that wrote the board.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parent
VENDOR_DIR = SERVER_DIR / "vendor"
PROBE_TIMEOUT_S = 30
API_SERVER_MIN_VERSION = (10, 99, 0)      # a KiCad 11 nightly reports itself as 10.99.x
IS_WINDOWS = os.name == "nt"               # tests patch these two to fake a platform
CLI_NAME = "kicad-cli.exe" if IS_WINDOWS else "kicad-cli"
_which = shutil.which                      # patched by tests (PATH lookups)
_VERSION_RE = re.compile(r"^(\d+)\.(\d+)(?:\.(\d+))?")


@dataclass(frozen=True)
class ProbeResult:
    """Outcome of one subprocess probe; a launch failure or timeout is a
    returncode of -1 with the reason in stderr (never an exception)."""
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


@dataclass(frozen=True)
class KiCadInstall:
    version: str                        # as printed by `kicad-cli version`
    version_tuple: tuple[int, int, int]
    root: Path                          # the dir holding bin/ and share/
    kicad_cli: Path
    python: Path | None = None          # bundled Python verified to import pcbnew
    share: Path | None = None           # .../share/kicad (symbols/, footprints/)
    has_api_server: bool = False        # `kicad-cli api-server` exists (KiCad 11+)

    def __post_init__(self) -> None:
        if len(self.version_tuple) != 3:
            raise ValueError(f"version_tuple must have 3 parts: {self.version_tuple!r}")

    @property
    def python_error(self) -> str | None:
        """Why the bundled Python probe failed (None when it succeeded)."""
        return python_probe_error(self.kicad_cli)

    def as_dict(self) -> dict:
        """JSON-friendly view for server_status()['installs']."""
        return {"version": self.version,
                "version_tuple": list(self.version_tuple),
                "root": str(self.root),
                "kicad_cli": str(self.kicad_cli),
                "python": str(self.python) if self.python else None,
                "python_error": self.python_error,
                "share": str(self.share) if self.share else None,
                "has_api_server": self.has_api_server}


# ----------------------------------------------------------------------------
# Probing (one subprocess per question per kicad-cli path, cached per process)
# ----------------------------------------------------------------------------
_VERSION: dict[str, tuple[str | None, str | None]] = {}   # key -> (version, error)
_PYTHON: dict[str, tuple[Path | None, str | None]] = {}   # key -> (python, error)
_API: dict[str, bool] = {}
_INSTALLS: tuple[KiCadInstall, ...] | None = None


def clear_cache() -> None:
    """Forget every probe result and the discovered list (tests; re-scan)."""
    global _INSTALLS
    _VERSION.clear()
    _PYTHON.clear()
    _API.clear()
    _INSTALLS = None


def _key(path: Path | str) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def _run(argv: list[str], timeout_s: float = PROBE_TIMEOUT_S) -> ProbeResult:
    """Run a probe; tests monkeypatch this to feed canned answers."""
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        return ProbeResult(-1, "", f"{argv[0]}: timed out after {timeout_s:g} s")
    except OSError as e:
        return ProbeResult(-1, "", f"{argv[0]}: cannot start ({e})")
    return ProbeResult(r.returncode, r.stdout, r.stderr)


def parse_version(text: str) -> tuple[int, int, int]:
    """'9.0.4' -> (9, 0, 4); '10.99.0-4025-gf7ca62' -> (10, 99, 0); '9.0' ->
    (9, 0, 0); anything unparseable -> (0, 0, 0)."""
    first = text.strip().splitlines()[0].strip() if text.strip() else ""
    m = _VERSION_RE.match(first)
    if not m:
        return (0, 0, 0)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3) or 0))


def cli_version(kicad_cli: Path) -> str | None:
    """First line of `kicad-cli version`, or None when the probe fails (the
    reason is kept for version_probe_error). Cached per path."""
    key = _key(kicad_cli)
    if key not in _VERSION:
        r = _run([str(kicad_cli), "version"])
        line = r.stdout.strip().splitlines()[0].strip() if r.stdout.strip() else ""
        if r.ok and line:
            _VERSION[key] = (line, None)
        else:
            why = r.stderr.strip() or r.stdout.strip() or f"exit code {r.returncode}"
            _VERSION[key] = (None, f"{kicad_cli} version: {why[-400:]}")
    return _VERSION[key][0]


def version_probe_error(kicad_cli: Path) -> str | None:
    return _VERSION.get(_key(kicad_cli), (None, None))[1]


def python_candidates(kicad_cli: Path) -> list[Path]:
    """Where a KiCad install keeps its Python: beside kicad-cli on Windows; the
    macOS bundle keeps it in Frameworks/; Linux packages use the system one."""
    bin_dir = kicad_cli.parent
    if IS_WINDOWS:
        return [bin_dir / "python.exe"]
    framework = bin_dir.parent / "Frameworks" / "Python.framework" / "Versions" / "Current"
    system = _which("python3")
    return [bin_dir / "python3", framework / "bin" / "python3"] + \
        ([Path(system)] if system else [])


def bundled_python(kicad_cli: Path) -> Path | None:
    """The install's Python, VERIFIED to import pcbnew; None otherwise (why is
    kept for python_probe_error). Cached per path."""
    key = _key(kicad_cli)
    if key not in _PYTHON:
        _PYTHON[key] = _probe_python(kicad_cli)
    return _PYTHON[key][0]


def _probe_python(kicad_cli: Path) -> tuple[Path | None, str | None]:
    errors: list[str] = []
    for cand in python_candidates(kicad_cli):
        if not cand.is_file():
            continue
        r = _run([str(cand), "-c", "import pcbnew"])
        if r.ok:
            return (cand, None)
        errors.append(f"{cand}: cannot import pcbnew: {r.stderr.strip()[-400:]}")
    return (None, "; ".join(errors) or f"no bundled Python found beside {kicad_cli}")


def python_probe_error(kicad_cli: Path) -> str | None:
    return _PYTHON.get(_key(kicad_cli), (None, None))[1]


def has_api_server(kicad_cli: Path, version_tuple: tuple[int, int, int]) -> bool:
    """True when this kicad-cli offers the headless `api-server` subcommand:
    version >= 10.99.0 AND `api-server --help` exits 0. Cached per path."""
    if version_tuple < API_SERVER_MIN_VERSION:
        return False
    key = _key(kicad_cli)
    if key not in _API:
        _API[key] = _run([str(kicad_cli), "api-server", "--help"]).ok
    return _API[key]


# ----------------------------------------------------------------------------
# Candidate locations
# ----------------------------------------------------------------------------
def _env_bin_dirs() -> list[Path]:
    kp = os.environ.get("KICAD_PATH", "").strip()
    return [Path(kp), Path(kp) / "bin"] if kp else []


def _versioned_bin_dirs(base: Path) -> list[Path]:
    """<base>/<version>/bin for every version dir under a KiCad install base."""
    if not base.is_dir():
        return []
    return [d / "bin" for d in sorted(base.iterdir(), reverse=True) if d.is_dir()]


def _windows_bin_dirs() -> list[Path]:
    program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
    local = os.environ.get("LOCALAPPDATA", "").strip()
    bases = [Path(program_files) / "KiCad"]
    if local:
        bases.append(Path(local) / "Programs" / "KiCad")
    return [d for base in bases for d in _versioned_bin_dirs(base)]


def _posix_bin_dirs() -> list[Path]:
    return [Path("/usr/bin"), Path("/usr/local/bin"),
            Path("/Applications/KiCad/KiCad.app/Contents/MacOS")]


def _path_clis() -> list[Path]:
    found = _which("kicad-cli")
    return [Path(found)] if found else []


def candidate_clis() -> list[Path]:
    """Every existing kicad-cli in search order, duplicates removed (the same
    binary reached via PATH and via Program Files counts once)."""
    bin_dirs = _env_bin_dirs()
    bin_dirs += _windows_bin_dirs() if IS_WINDOWS else _posix_bin_dirs()
    found = [d / CLI_NAME for d in bin_dirs] + _path_clis()
    seen: set[str] = set()
    unique: list[Path] = []
    for cli in found:
        if not cli.is_file():
            continue
        resolved = Path(os.path.abspath(cli))
        if _key(resolved) not in seen:
            seen.add(_key(resolved))
            unique.append(resolved)
    return unique


# ----------------------------------------------------------------------------
# Discovery
# ----------------------------------------------------------------------------
def _share_for(root: Path) -> Path | None:
    """share/kicad on Windows and Linux; the macOS bundle uses SharedSupport."""
    for cand in (root / "share" / "kicad", root / "SharedSupport"):
        if cand.is_dir():
            return cand
    return None


def probe(kicad_cli: Path) -> KiCadInstall | None:
    """Describe one kicad-cli, or None when `kicad-cli version` fails (the
    reason stays in version_probe_error)."""
    version = cli_version(kicad_cli)
    if version is None:
        return None
    vt = parse_version(version)
    root = kicad_cli.parent.parent
    return KiCadInstall(version=version, version_tuple=vt, root=root, kicad_cli=kicad_cli,
                        python=bundled_python(kicad_cli), share=_share_for(root),
                        has_api_server=has_api_server(kicad_cli, vt))


def discover(refresh: bool = False) -> list[KiCadInstall]:
    """Every usable KiCad install, newest first. Cached per process; refresh
    re-scans the locations (probe results themselves stay cached per path)."""
    global _INSTALLS
    if _INSTALLS is None or refresh:
        installs = [i for i in map(probe, candidate_clis()) if i is not None]
        _INSTALLS = tuple(sorted(installs, key=lambda i: i.version_tuple, reverse=True))
    return list(_INSTALLS)


def probe_errors() -> list[str]:
    """Why candidate kicad-cli paths were rejected (empty when all probed fine)."""
    return [err for _ver, err in _VERSION.values() if err]


def pick_cli() -> KiCadInstall | None:
    """Newest install: schematic ERC / render (reads any file). Board jobs use
    pick_swig()'s kicad-cli instead (toolchain.board_cli)."""
    installs = discover()
    return installs[0] if installs else None


def pick_swig() -> KiCadInstall | None:
    """Newest install whose bundled Python imports pcbnew (KiCad 9/10 only;
    KiCad 11 dropped the SWIG bindings)."""
    return next((i for i in discover() if i.python), None)


def pick_api() -> KiCadInstall | None:
    """Newest install whose kicad-cli can run the headless IPC api-server."""
    return next((i for i in discover() if i.has_api_server), None)


def symbols_dir(install: KiCadInstall | None) -> Path | None:
    """<share>/symbols of an install (None when absent or no install)."""
    return _share_sub(install, "symbols")


def footprints_dir(install: KiCadInstall | None) -> Path | None:
    """<share>/footprints of an install (None when absent or no install)."""
    return _share_sub(install, "footprints")


def _share_sub(install: KiCadInstall | None, name: str) -> Path | None:
    if install is None or install.share is None:
        return None
    sub = install.share / name
    return sub if sub.is_dir() else None


# ----------------------------------------------------------------------------
# Vendored kipy (server/vendor/kipy)
# ----------------------------------------------------------------------------
def use_vendored_kipy() -> Path:
    """Put server/vendor at the FRONT of sys.path so the vendored kipy (built
    against the KiCad 11 protos) always wins over any pip-installed one. Call
    before the first `import kipy`; raises if a foreign kipy is already loaded."""
    loaded = sys.modules.get("kipy")
    loaded_file = getattr(loaded, "__file__", None) if loaded else None
    if loaded_file and _key(VENDOR_DIR) not in _key(loaded_file):
        raise RuntimeError(
            f"kipy was already imported from {loaded_file}; call "
            "kicad_env.use_vendored_kipy() before importing kipy")
    entry = str(VENDOR_DIR)
    if sys.path[:1] != [entry]:
        sys.path[:] = [entry] + [p for p in sys.path if _key(p) != _key(entry)]
    return VENDOR_DIR


if __name__ == "__main__":
    for inst in discover():
        print(inst)
    print("cli:", pick_cli() and pick_cli().kicad_cli)
    print("swig:", pick_swig() and pick_swig().python)
    print("api:", pick_api() and pick_api().kicad_cli)
    for err in probe_errors():
        print("rejected:", err)
