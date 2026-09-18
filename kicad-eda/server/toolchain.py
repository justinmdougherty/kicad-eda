"""Toolchain resolution shared by every kicad-generator tool module.

One place answers "which kicad-cli / bundled Python / library dir / Java /
Freerouting jar do we use?". Installs come from kicad_env (every KiCad on the
machine, probed once per process) and the rules are:

  * SCHEMATIC jobs (ERC, SVG render) use the NEWEST kicad-cli (kicad_cli /
    pick_cli): a file written by KiCad N can only be read by kicad-cli >= N,
    and the API backend writes KiCad 11 files.
  * the SWIG board builder uses the newest install whose bundled Python
    imports pcbnew (pick_swig; KiCad 11 dropped the SWIG bindings) together
    with THAT install's footprint libraries, so board and libraries agree.
  * BOARD jobs (DRC, Gerbers, drill, pos, PNG render) use the kicad-cli of
    that SAME install (board_cli / pick_board_cli): the stable release whose
    pcbnew wrote the board also checks and exports it, so a nightly can never
    change DRC semantics or Gerber output behind a stable-built board. Only
    when no install has a pcbnew Python does board_cli fall back to the newest.
  * symbol libraries come from the newest install (pick_cli).

Also here: the subprocess helpers and the kicad-cli ERC / DRC report readers
the schematic, board and pipeline tools all share.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections import Counter
from pathlib import Path

import kicad_env as env

SERVER_DIR = Path(__file__).resolve().parent
TOOLS_DIR = SERVER_DIR / "tools"
PINS_DIR = SERVER_DIR / "pins"
FAB_PROFILE = os.environ.get("FAB_PROFILE", "JLCPCB")
WORK_UNITS = os.environ.get("WORK_UNITS", "mm")
KICAD_CLI_TIMEOUT_S = 600
# ERC severities that never count as violations: the user excluded them in the
# editor, or the rule is set to "ignore" in the project's ERC settings.
ERC_SUPPRESSED = frozenset({"exclusion", "ignore"})
# Reported by server_status()["kicad_cli_policy"]; the rule the pickers below implement.
KICAD_CLI_POLICY = ("schematic jobs (ERC, SVG render) use the NEWEST installed kicad-cli: a file "
                    "written by KiCad N needs kicad-cli >= N and the API backend writes KiCad 11 "
                    "files; board jobs (DRC, Gerbers, drill, pos, PNG render) use the kicad-cli of "
                    "the SAME install whose bundled Python (pcbnew) built the board, so a nightly "
                    "never changes DRC semantics or fab output of a stable-built board; the newest "
                    "kicad-cli is used for boards only when no install has a pcbnew Python")


# ----------------------------------------------------------------------------
# KiCad installs (kicad_env does the discovery; these are the per-job picks)
# ----------------------------------------------------------------------------
def kicad_cli() -> str | None:
    """SCHEMATIC kicad-cli: the newest on the machine (reads files from every
    KiCad version; ERC and SVG render). Boards use board_cli()."""
    cli = env.pick_cli()
    return str(cli.kicad_cli) if cli else None


def pick_board_cli() -> env.KiCadInstall | None:
    """The install whose kicad-cli runs BOARD jobs: the one whose bundled
    Python built the board (pick_swig), else the newest (pick_cli)."""
    return env.pick_swig() or env.pick_cli()


def board_cli() -> str | None:
    """BOARD kicad-cli (DRC, Gerbers, drill, pos, PNG render): the kicad-cli
    shipped with the install whose pcbnew built the board, so check and
    export semantics match the writer; the newest kicad-cli only when no
    install has a pcbnew Python."""
    inst = pick_board_cli()
    return str(inst.kicad_cli) if inst else None


def kicad_python() -> str | None:
    """KiCad's bundled Python VERIFIED to import pcbnew (newest install that has one)."""
    swig = env.pick_swig()
    return str(swig.python) if swig else None


def kicad_python_error() -> str | None:
    """Why no bundled Python qualified (None when one did)."""
    if env.pick_swig() is not None:
        return None
    found = env.discover()
    if not found:
        return "kicad-cli not found, so KiCad's bundled Python could not be located"
    return "; ".join(f"{i.version}: {i.python_error}" for i in found if i.python_error) \
        or "no install has a bundled Python that imports pcbnew"


def kicad_share() -> Path | None:
    """The newest install's share/kicad (symbols/ and footprints/ live under it)."""
    cli = env.pick_cli()
    return cli.share if cli else None


def symbols_dir(explicit: str = "") -> str | None:
    """The .kicad_sym library dir: an explicit path (must exist) or the newest
    KiCad's stock one."""
    if explicit:
        if not Path(explicit).is_dir():
            raise ValueError(f"symbols_dir does not exist: {explicit}")
        return explicit
    found = env.symbols_dir(env.pick_cli())
    return str(found) if found else None


def footprints_dir(explicit: str = "") -> str | None:
    """The .pretty library dir: an explicit path (must exist) or the one shipped
    with the install whose Python builds the board (pick_swig)."""
    if explicit:
        if not Path(explicit).is_dir():
            raise ValueError(f"footprints_dir does not exist: {explicit}")
        return explicit
    found = env.footprints_dir(env.pick_swig())
    return str(found) if found else None


def installs() -> list[dict]:
    """JSON view of every discovered install, newest first."""
    return [i.as_dict() for i in env.discover()]


# ----------------------------------------------------------------------------
# Java + Freerouting
# ----------------------------------------------------------------------------
def find_java() -> str | None:
    # a tools-local portable JRE wins — Freerouting 2.2.4+ is compiled for
    # Java 25, newer than any system-installed JDK we can rely on
    for cand in sorted(TOOLS_DIR.glob("jre*/**/bin/java.exe"), reverse=True):
        if cand.is_file():
            return str(cand)
    found = shutil.which("java")
    if found:
        return found
    jh = os.environ.get("JAVA_HOME")
    exe = "java.exe" if os.name == "nt" else "java"
    if jh and (Path(jh) / "bin" / exe).is_file():
        return str(Path(jh) / "bin" / exe)
    if os.name == "nt":
        for base in (r"C:\Program Files\Microsoft", r"C:\Program Files\Java",
                     r"C:\Program Files\Eclipse Adoptium"):
            b = Path(base)
            if b.is_dir():
                for d in sorted(b.iterdir(), reverse=True):
                    cand = d / "bin" / exe
                    if cand.is_file():
                        return str(cand)
    return None


def freerouting_jar(explicit: str = "") -> str | None:
    cands: list[Path] = []
    if explicit:
        cands.append(Path(explicit))
    env_jar = os.environ.get("FREEROUTING_JAR")
    if env_jar:
        cands.append(Path(env_jar))
    # prefer a versioned jar (e.g. freerouting-1.9.0.jar runs on JDK21)
    cands += sorted(TOOLS_DIR.glob("freerouting*.jar"), reverse=True)
    for c in cands:
        if c.is_file():
            return str(c)
    return None


# ----------------------------------------------------------------------------
# Subprocess helpers
# ----------------------------------------------------------------------------
def run(args: list[str], **kw) -> subprocess.CompletedProcess:
    """subprocess.run with captured output and stdin DETACHED. Inside an MCP
    stdio server the parent's stdin is the protocol pipe (with a read pending);
    a python.exe child that inherits it blocks at start-up until the timeout
    (kicad_env._run has the details). Callers may still pass stdin= explicitly."""
    kw.setdefault("stdin", subprocess.DEVNULL)
    return subprocess.run(args, capture_output=True, text=True, **kw)


def last_json(stdout: str) -> dict | None:
    """The last line of stdout that parses as a JSON object (tool scripts print
    their result last), or None."""
    for line in reversed(stdout.strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except ValueError:
                continue
    return None


# ----------------------------------------------------------------------------
# kicad-cli ERC / DRC report readers
# ----------------------------------------------------------------------------
def erc_severity_counts(report: dict) -> dict[str, int]:
    """{severity: count} over every sheet of a kicad-cli ERC JSON report."""
    return dict(Counter(str(v.get("severity", "unknown"))
                        for s in report.get("sheets", [])
                        for v in s.get("violations", [])))


def erc_json(sch: str, rpt: str, severity_error: bool = True) -> dict:
    """Run kicad-cli ERC (newest KiCad) with a JSON report and count violations.

    kicad-cli's --severity-error filters its console summary ("Found N
    violations") but NOT the JSON report — KiCad 9.0.4 still writes every
    warning into it — so the count is taken from each violation's own
    `severity`: errors only when severity_error, else every non-suppressed
    severity. erc_ok is False (with erc_error) whenever the report cannot be
    produced or parsed."""
    cli = kicad_cli()
    if not cli:
        return {"erc_ok": False, "erc_report": rpt,
                "erc_error": "kicad-cli not found (install KiCad or set KICAD_PATH)"}
    args = [cli, "sch", "erc", "--format", "json", "-o", rpt]
    if severity_error:
        args.append("--severity-error")
    e = run(args + [sch], timeout=KICAD_CLI_TIMEOUT_S)
    try:
        report = json.loads(Path(rpt).read_text(encoding="utf-8"))
    except (OSError, ValueError) as ex:
        return {"erc_ok": False, "erc_report": rpt,
                "erc_error": f"ERC report unreadable ({ex}): {(e.stdout + e.stderr)[-600:]}"}
    by_sev = erc_severity_counts(report)
    if severity_error:
        n = by_sev.get("error", 0)
    else:
        n = sum(c for sev, c in by_sev.items() if sev not in ERC_SUPPRESSED)
    return {"erc_ok": n == 0, "erc_violations": n, "erc_by_severity": by_sev,
            "erc_report": rpt, "report": report}


def drc_counts(pcb: str) -> dict:
    """Run kicad-cli DRC (error severity, board_cli) on a saved board and
    summarize the JSON report: {errors, unconnected, types: {type: count},
    report, kicad_cli}. When the report cannot be produced or parsed the dict
    carries an `error` key (counts -1)."""
    cli = board_cli()
    if not cli:
        raise RuntimeError("kicad-cli not found; install KiCad.")
    rpt = str(Path(pcb).with_suffix("")) + "_drc.json"
    r = run([cli, "pcb", "drc", pcb, "--format", "json", "--severity-error",
             "-o", rpt], timeout=KICAD_CLI_TIMEOUT_S)
    try:
        d = json.loads(Path(rpt).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return {"errors": -1, "unconnected": -1, "types": {}, "report": rpt, "kicad_cli": cli,
                "error": f"DRC report unreadable ({e}): {(r.stdout + r.stderr)[-600:]}"}
    violations = d.get("violations", [])
    return {"errors": len(violations),
            "unconnected": len(d.get("unconnected_items", [])),
            "types": dict(Counter(v.get("type", "unknown") for v in violations)),
            "report": rpt, "kicad_cli": cli}
