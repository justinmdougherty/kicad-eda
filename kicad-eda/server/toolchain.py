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
    the KiCad that WROTE the board file (board_cli(pcb) / pick_board_cli(pcb)
    read its `generator_version` header): a pcbnew-built board keeps its
    stable release's DRC semantics and Gerber output, and a board saved by the
    KiCad 11 API session (which KiCad 9/10 cannot load at all) is checked and
    exported by kicad-cli 11. No install of that release -> the oldest install
    that can read the file (a KiCad N cli reads files up to its own version);
    none can -> the newest, and board_cli_note(pcb) says so. No file, or a
    header that does not parse -> the pcbnew install, else the newest.
  * SWIG board tools (Freerouting, gate checks, stitching) refuse a board
    newer than the pcbnew install can read: swig_readable(pcb) is the reason.
  * symbol libraries come from the newest install (pick_cli).

Also here: the subprocess helpers and the kicad-cli ERC / DRC report readers
the schematic, board and pipeline tools all share.
"""

from __future__ import annotations

import json
import os
import re
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
                    "the KiCad that WROTE the board file (its generator_version header): a "
                    "pcbnew-built board keeps its stable release's DRC semantics and fab output, "
                    "so a nightly never touches a stable-built board, and a board saved by the "
                    "KiCad 11 API session is checked by kicad-cli 11; without that release the "
                    "oldest kicad-cli that can read the file is used, and without a readable "
                    "board file the pcbnew install's kicad-cli, else the newest")
# A .kicad_pcb header -- (version N) (generator "pcbnew") (generator_version "9.0") -- sits
# in the first few hundred bytes; this is how much of a board file board_file_info reads.
HEADER_BYTES = 1024
_HEADER_VERSION_RE = re.compile(r"\(version\s+(\d+)\s*\)")
_HEADER_GENERATOR_RE = re.compile(r"\(generator\s+\"?([^\s\")]+)\"?\s*\)")
_HEADER_GENERATOR_VERSION_RE = re.compile(r"\(generator_version\s+\"?([^\s\")]+)\"?\s*\)")


# ----------------------------------------------------------------------------
# KiCad installs (kicad_env does the discovery; these are the per-job picks)
# ----------------------------------------------------------------------------
def kicad_cli() -> str | None:
    """SCHEMATIC kicad-cli: the newest on the machine (reads files from every
    KiCad version; ERC and SVG render). Boards use board_cli()."""
    cli = env.pick_cli()
    return str(cli.kicad_cli) if cli else None


def pick_board_cli(pcb: str = "") -> env.KiCadInstall | None:
    """The install whose kicad-cli runs BOARD jobs on `pcb`: the release that
    WROTE the file (its generator_version header, "9.0" -> 9.0.4, "10.99" ->
    the 11 nightly), else the oldest install that can read it, else the newest
    (board_cli_note(pcb) explains). Without a pcb, or when its header does
    not parse: the pcbnew install (pick_swig), else the newest (pick_cli)."""
    return _choose_board_install(pcb)[0]


def board_cli(pcb: str = "") -> str | None:
    """BOARD kicad-cli path (DRC, Gerbers, drill, pos, PNG render) for `pcb`:
    the kicad-cli of the KiCad that wrote the file, so check and export
    semantics match the writer (pick_board_cli has the fallbacks). None when
    no KiCad is installed: install one or set KICAD_PATH."""
    inst = pick_board_cli(pcb)
    return str(inst.kicad_cli) if inst else None


def board_cli_note(pcb: str = "") -> str | None:
    """Why board_cli(pcb) is a best-effort fallback (the file was written by a
    KiCad newer than every installed kicad-cli), or None when the pick can
    read the file. Surface it next to the job's result."""
    return _choose_board_install(pcb)[1]


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
# Board file headers: which KiCad wrote the file, and who can read it
# ----------------------------------------------------------------------------
def board_file_info(pcb: str) -> dict:
    """{"format_version": int | None, "generator": str | None,
    "generator_version": str | None} from the head of a .kicad_pcb, e.g.
    20260901 / "pcbnew" / "10.99" for a board saved by the KiCad 11 API
    session, 20241229 / "pcbnew" / "9.0" for one built by KiCad 9's pcbnew.
    A missing, unreadable or unparsable file gives None for every field;
    this never raises."""
    head = _read_head(pcb)
    version = _HEADER_VERSION_RE.search(head)
    generator = _HEADER_GENERATOR_RE.search(head)
    generator_version = _HEADER_GENERATOR_VERSION_RE.search(head)
    return {"format_version": int(version.group(1)) if version else None,
            "generator": generator.group(1) if generator else None,
            "generator_version": generator_version.group(1) if generator_version else None}


def _read_head(path: str) -> str:
    """The first HEADER_BYTES of a file as text; '' when it cannot be read."""
    if not path:
        return ""
    try:
        with open(path, "rb") as f:
            return f.read(HEADER_BYTES).decode("utf-8", errors="replace")
    except OSError:
        return ""


def _writer_version(info: dict) -> tuple[int, int, int] | None:
    """(major, minor, patch) of the KiCad that wrote a board, from
    board_file_info's generator_version ("9.0" -> (9, 0, 0), "10.99" ->
    (10, 99, 0)); None when the header has none or it does not parse."""
    generator_version = info.get("generator_version")
    vt = env.parse_version(str(generator_version)) if generator_version else (0, 0, 0)
    return vt if vt != (0, 0, 0) else None


def _release_matches(version: str, generator_version: str) -> bool:
    """True when an install's `kicad-cli version` string is a release of the
    writer's generator_version: "9.0.4" for "9.0", "10.99.0-4025-g.." for
    "10.99" (token-safe: "10.9" never matches "10.99.0")."""
    return (version == generator_version
            or version.startswith(generator_version + ".")
            or version.startswith(generator_version + "-"))


def _choose_board_install(pcb: str) -> tuple[env.KiCadInstall | None, str | None]:
    """(install, note) for a board job on `pcb`: the install of the release
    that wrote the file, else the oldest install that can read it (a KiCad N
    cli reads files up to its own version), else the newest with a note
    saying it may fail. No pcb, or a header that does not parse: the pcbnew
    install, else the newest (no note)."""
    info = board_file_info(pcb) if pcb else {}
    writer = _writer_version(info)
    found = env.discover()
    if writer is None or not found:
        return env.pick_swig() or env.pick_cli(), None
    generator_version = str(info["generator_version"])
    same_release = [i for i in found if _release_matches(i.version, generator_version)]
    if same_release:
        return same_release[0], None
    readers = [i for i in found if i.version_tuple >= writer]
    if readers:
        return min(readers, key=lambda i: i.version_tuple), None
    newest = max(found, key=lambda i: i.version_tuple)
    note = (f"{Path(pcb).name} was saved by KiCad {generator_version} "
            f"(format {info['format_version']}) and no installed kicad-cli is that new; "
            f"using the newest, KiCad {newest.version}, which may fail to load the board "
            f"(install KiCad {generator_version} or newer)")
    return newest, note


def _release_label(vt: tuple[int, int, int]) -> str:
    """The release a version belongs to: an x.99 nightly is the next major
    ((10, 99, 0) -> "11"), anything else its major ((9, 0, 4) -> "9")."""
    return str(vt[0] + 1) if vt[1] == 99 else str(vt[0])


def swig_readable(pcb: str) -> str | None:
    """The refusal reason when `pcb` was written by a KiCad release newer than
    the pcbnew install (pick_swig) can load (KiCad 9/10's pcbnew cannot read
    a board saved by the KiCad 11 API session), else None. Also None when the
    header does not parse or no install has a pcbnew Python: the caller's
    kicad_python check speaks for that case. Every SWIG-based board tool
    returns the reason as {ok: False, reason}; the fix it names is to use the
    board API tools (route_check / route_status) or a KiCad 9 GUI session."""
    info = board_file_info(pcb)
    writer = _writer_version(info)
    swig = env.pick_swig()
    if writer is None or swig is None or writer[:2] <= swig.version_tuple[:2]:
        return None
    return (f"{Path(pcb).name} was saved by KiCad {info['generator_version']} "
            f"(format {info['format_version']}); KiCad {swig.version}'s pcbnew Python cannot "
            f"read it -- use route_check / route_status for a KiCad {_release_label(writer)} "
            f"board, or route in a KiCad {swig.version_tuple[0]} GUI session")


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
    """Run kicad-cli DRC (error severity) on a saved board with the kicad-cli
    of the KiCad that wrote it (board_cli(pcb)) and summarize the JSON report:
    {errors, unconnected, types: {type: count}, report, kicad_cli}, plus
    `kicad_cli_note` when that cli is a fallback (board_cli_note). When the
    report cannot be produced or parsed the dict carries an `error` key
    (counts -1). Raises RuntimeError when no KiCad is installed."""
    inst, note = _choose_board_install(pcb)
    if inst is None:
        raise RuntimeError("kicad-cli not found; install KiCad.")
    cli = str(inst.kicad_cli)
    rpt = str(Path(pcb).with_suffix("")) + "_drc.json"
    r = run([cli, "pcb", "drc", pcb, "--format", "json", "--severity-error",
             "-o", rpt], timeout=KICAD_CLI_TIMEOUT_S)
    base = {"report": rpt, "kicad_cli": cli, **({"kicad_cli_note": note} if note else {})}
    try:
        d = json.loads(Path(rpt).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return {"errors": -1, "unconnected": -1, "types": {}, **base,
                "error": f"DRC report unreadable ({e}): {(r.stdout + r.stderr)[-600:]}"}
    violations = d.get("violations", [])
    return {"errors": len(violations),
            "unconnected": len(d.get("unconnected_items", [])),
            "types": dict(Counter(v.get("type", "unknown") for v in violations)),
            **base}


def _drc_item(item: dict) -> dict:
    pos = item.get("pos") or {}
    return {"description": item.get("description"), "x": pos.get("x"), "y": pos.get("y")}


def _drc_entry(entry: dict) -> dict:
    return {"type": entry.get("type"), "severity": entry.get("severity"),
            "description": entry.get("description"),
            "items": [_drc_item(i) for i in entry.get("items", [])]}


def drc_check(pcb: str, max_items: int) -> dict:
    """drc_counts(pcb) for a repair loop, plus the report's first max_items
    violations and unconnected items as [{type, severity, description,
    items: [{description, x, y}]}]: {ok: errors == 0 and unconnected == 0,
    errors, unconnected, types, violations, unconnected_items, kicad_cli,
    report[, kicad_cli_note]}. {ok: False, reason, kicad_cli, report} (no
    counts) when DRC wrote no readable report. Raises RuntimeError when no
    KiCad is installed."""
    counts = drc_counts(pcb)
    base = {key: counts[key] for key in ("kicad_cli", "report", "kicad_cli_note") if counts.get(key)}
    if "error" in counts:
        return {**base, "ok": False, "reason": counts["error"]}
    try:
        report = json.loads(Path(counts["report"]).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {**base, "ok": False, "reason": f"DRC report {counts['report']} unreadable: {exc}"}
    violations = [_drc_entry(v) for v in report.get("violations", [])]
    unconnected = [_drc_entry(v) for v in report.get("unconnected_items", [])]
    return {**base, "ok": counts["errors"] == 0 and counts["unconnected"] == 0,
            "errors": counts["errors"], "unconnected": counts["unconnected"],
            "types": counts["types"], "violations": violations[:max_items],
            "unconnected_items": unconnected[:max_items]}


# ----------------------------------------------------------------------------
# Pre-KiCad-11 snapshot (ROADMAP D5)
# ----------------------------------------------------------------------------
# The first save through a KiCad >= 10.99 session (headless api-server or a
# KiCad 11 GUI) rewrites X.kicad_pcb (format 20260901), X.kicad_pro and
# X.kicad_prl in KiCad 11 form; KiCad 9/10 pcbnew (Freerouting, the SWIG gates,
# EMC stitching) cannot load that board any more. Right before it, the pre-11
# set is copied to X.k9.* in the same directory (project library tables with
# ${KIPRJMOD} stay valid). Board and project travel together: restored without
# its project, the board loses its net classes and Freerouting lays power copper
# at the default width while DRC still passes (spike T09). KiCad 11 never
# writes .kicad_dru; the ~X.kicad_pro.lck lock it leaves behind is never copied.
K11_SESSION = (10, 99)                  # the KiCad 11 nightly and every release after it
K10_LAST_FORMAT = 20260206              # KiCad 10.0's board format: the newest pre-11 file
SNAPSHOT_TAG = ".k9"
AGENT_TAG = ".agent"
BOARD_EXT = ".kicad_pcb"
PROJECT_EXT = ".kicad_pro"
LOCAL_EXT = ".kicad_prl"                # per-user UI state
RULES_EXT = ".kicad_dru"                # custom DRC rules
SNAPSHOT_EXTRAS = (PROJECT_EXT, LOCAL_EXT, RULES_EXT)   # copied beside the board when present
K11_REWRITES = (BOARD_EXT, PROJECT_EXT, LOCAL_EXT)      # what a KiCad 11 save rewrites
STALL_ROUNDS = 3                       # D5: route_check rounds without a new best = a stall
STALL_NOTE = (f"{STALL_ROUNDS} route_check rounds without fewer errors + unconnected: stop "
              "agent routing and fall back (ROADMAP D5): autoroute_board(pcb, "
              "from_snapshot=true) routes the pre-KiCad-11 snapshot with Freerouting; if that "
              "fails, re-place at a higher pack_scale; then stop with a report")


def route_progress(history: tuple[int, ...]) -> dict:
    """The D5 stall verdict over a board's route_check scores (errors +
    unconnected per round, oldest first): {"progress": {rounds, score, best,
    rounds_without_improvement}, "stalled": bool[, "stall_note"]}. A round
    improves when its score is below every earlier one; stalled = STALL_ROUNDS
    rounds without improvement while the board is not clean yet."""
    if not history:
        raise ValueError("route_progress needs at least one round")
    best, since = history[0], 0
    for score in history[1:]:
        best, since = (score, 0) if score < best else (best, since + 1)
    stalled = since >= STALL_ROUNDS and history[-1] > 0
    return {"progress": {"rounds": len(history), "score": history[-1], "best": best,
                         "rounds_without_improvement": since},
            "stalled": stalled, **({"stall_note": STALL_NOTE} if stalled else {})}


def _sibling(pcb: str, tag: str, ext: str) -> Path:
    """X<tag><ext> beside X.kicad_pcb (tag "" = the canonical project file)."""
    return Path(str(Path(pcb).with_suffix("")) + tag + ext)


def pre_k11_snapshot_path(pcb: str) -> str:
    """X.k9.kicad_pcb beside X.kicad_pcb: where the pre-KiCad-11 snapshot of
    `pcb` lives, whether or not it exists yet."""
    return str(_sibling(pcb, SNAPSHOT_TAG, BOARD_EXT))


def agent_copy_path(pcb: str) -> str:
    """X.agent.kicad_pcb: where restore_snapshot moves the KiCad 11 copy."""
    return str(_sibling(pcb, AGENT_TAG, BOARD_EXT))


def existing_snapshot(pcb: str) -> str | None:
    """X.k9.kicad_pcb when the snapshot exists, else None."""
    path = pre_k11_snapshot_path(pcb)
    return path if Path(path).is_file() else None


def snapshot_field(pcb: str) -> dict:
    """{"pre_k11_snapshot": X.k9.kicad_pcb} while the snapshot exists, else
    {}: the key the board tools add to their responses."""
    found = existing_snapshot(pcb)
    return {"pre_k11_snapshot": found} if found else {}


def is_k11_session(kicad_version: str) -> bool:
    """True for a KiCad session (`kicad-cli version` / GUI version string)
    whose saves write the KiCad 11 board format: 10.99 nightlies and later."""
    return env.parse_version(kicad_version or "")[:2] >= K11_SESSION


def is_pre_k11(pcb: str) -> bool:
    """True when the board file on disk was written by a KiCad older than
    10.99 (its generator_version; without one, a format up to KiCad 10.0's),
    i.e. KiCad 9/10 pcbnew can load it. False for a KiCad 11 file and for a
    missing, unparsable or non-board file (nothing to protect)."""
    if not _read_head(pcb).lstrip().startswith("(kicad_pcb"):
        return False
    info = board_file_info(pcb)
    writer = _writer_version(info)
    if writer is not None:
        return writer[:2] < K11_SESSION
    fmt = info["format_version"]
    return fmt is not None and fmt <= K10_LAST_FORMAT


def snapshot_current(pcb: str) -> bool:
    """True when X.k9.* already holds what is on disk: the snapshot board and
    project exist exactly when X.kicad_pcb / X.kicad_pro do and are at least
    as new (take_snapshot copies with the source mtimes). A K9 re-placement
    or a project rewrite after the snapshot makes it stale."""
    for ext in (BOARD_EXT, PROJECT_EXT):
        src, snap = _sibling(pcb, "", ext), _sibling(pcb, SNAPSHOT_TAG, ext)
        if src.is_file() != snap.is_file() or (ext == BOARD_EXT and not src.is_file()):
            return False
        if src.is_file() and snap.stat().st_mtime_ns < src.stat().st_mtime_ns:
            return False
    return True


def take_snapshot(pcb: str) -> dict:
    """Copy X.kicad_pcb + X.kicad_pro (+ .kicad_prl / .kicad_dru when present)
    to X.k9.* in the same directory, keeping their mtimes. A snapshot file
    whose source is gone is removed, so the set is always one snapshot, and
    the board is copied last: X.k9.kicad_pcb exists only beside a complete
    set. Returns {"snapshot": X.k9.kicad_pcb, "files": [every file written]}.
    Raises ValueError when `pcb` is missing or not a pre-KiCad-11 board, and
    OSError when a copy fails."""
    if not Path(pcb).is_file():
        raise ValueError(f"{pcb} not found; nothing to snapshot")
    if not is_pre_k11(pcb):
        raise ValueError(f"{Path(pcb).name} is not a pre-KiCad-11 board "
                         f"({board_file_info(pcb)}); there is no KiCad 9/10 copy to keep")
    snap = _sibling(pcb, SNAPSHOT_TAG, BOARD_EXT)
    snap.unlink(missing_ok=True)                 # incomplete until the board copy lands
    written: list[str] = []
    for ext in SNAPSHOT_EXTRAS:
        src, dst = _sibling(pcb, "", ext), _sibling(pcb, SNAPSHOT_TAG, ext)
        if src.is_file():
            shutil.copy2(src, dst)
            written.append(str(dst))
        else:
            dst.unlink(missing_ok=True)
    shutil.copy2(pcb, snap)
    return {"snapshot": str(snap), "files": [str(snap), *written]}


def snapshot_before_k11_write(pcb: str, kicad_version: str) -> str | None:
    """The D5 rule, run right before a KiCad session may write `pcb`: when the
    session is KiCad >= 10.99, the file on disk is pre-11 and no current
    snapshot exists, take_snapshot(pcb) and return X.k9.kicad_pcb; else None.
    A failed copy raises RuntimeError saying why: the caller must not save."""
    if not is_k11_session(kicad_version) or not is_pre_k11(pcb) or snapshot_current(pcb):
        return None
    try:
        return take_snapshot(pcb)["snapshot"]
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"the pre-KiCad-11 snapshot of {pcb} failed ({exc}): a KiCad 11 save "
                           "would leave no KiCad 9/10 copy for Freerouting; fix the disk or "
                           "permissions and retry") from exc


def _check_restorable(pcb: str) -> Path:
    """The snapshot board restore_snapshot would bring back, or ValueError."""
    snap = _sibling(pcb, SNAPSHOT_TAG, BOARD_EXT)
    if not snap.is_file():
        raise ValueError(f"no pre-KiCad-11 snapshot {snap.name} beside {Path(pcb).name}: it is "
                         "taken before the first KiCad 11 save, so this board was never saved "
                         "by the board API from a KiCad 9/10 file (rebuild it with "
                         "build_board_from_netlist)")
    if not is_pre_k11(str(snap)):
        raise ValueError(f"{snap.name} is not a pre-KiCad-11 board ({board_file_info(str(snap))}); "
                         "it cannot be routed with Freerouting")
    if is_pre_k11(pcb):
        raise ValueError(f"{Path(pcb).name} is already a KiCad 9/10 board; nothing to restore "
                         "(route it as it is)")
    if not Path(pcb).is_file():
        raise ValueError(f"{pcb} not found; the snapshot {snap} is left as it is")
    return snap


def _swap_in_snapshot(pcb: str, moved: list[tuple[Path, Path]], restored: list[Path]) -> None:
    """Move the KiCad 11 files to X.agent.* and copy the snapshot back,
    recording each step in `moved` / `restored` for the caller's rollback."""
    for ext in K11_REWRITES:
        src = _sibling(pcb, "", ext)
        if src.is_file():
            dst = _sibling(pcb, AGENT_TAG, ext)
            os.replace(src, dst)
            moved.append((src, dst))
    for ext in K11_REWRITES:
        snap = _sibling(pcb, SNAPSHOT_TAG, ext)
        if snap.is_file():
            dst = _sibling(pcb, "", ext)
            shutil.copy2(snap, dst)
            restored.append(dst)


def restore_snapshot(pcb: str) -> dict:
    """The D5 Freerouting fallback's file step on a board KiCad 11 has saved:
    X.kicad_pcb / .kicad_pro / .kicad_prl move to X.agent.* (the agent's
    KiCad 11 copy, reopenable with its classes), X.kicad_dru is copied to
    X.agent.kicad_dru (KiCad 11 never writes it, the canonical one stays),
    and X.k9.{kicad_pcb, kicad_pro, kicad_prl} are copied back to X.* (the
    snapshot stays). Release every KiCad session on `pcb` first. Returns
    {"pcb", "snapshot", "agent_copy", "moved": [...], "restored": [...]}.
    Raises ValueError when there is no usable snapshot or `pcb` is not a
    KiCad 11 file, and OSError when a file operation fails (the moves done
    so far are undone first)."""
    snap = _check_restorable(pcb)
    moved: list[tuple[Path, Path]] = []
    restored: list[Path] = []
    try:
        _swap_in_snapshot(pcb, moved, restored)
        rules = _sibling(pcb, "", RULES_EXT)
        if rules.is_file():
            shutil.copy2(rules, _sibling(pcb, AGENT_TAG, RULES_EXT))
    except OSError:
        for path in restored:
            path.unlink(missing_ok=True)
        for src, dst in reversed(moved):
            os.replace(dst, src)
        raise
    return {"pcb": pcb, "snapshot": str(snap), "agent_copy": agent_copy_path(pcb),
            "moved": [str(dst) for _src, dst in moved], "restored": [str(p) for p in restored]}
