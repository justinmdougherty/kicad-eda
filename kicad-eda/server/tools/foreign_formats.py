#!/usr/bin/env python3
"""What a foreign design file is, which KiCad install can import it, and what
its importer printed (ROADMAP_DECISIONS D4, F6). foreign_import.py drives the
imports on top of this.

  * ``classify``: extension first, then a content sniff where the extension
    lies (Eagle XML vs binary / Allegro ``.brd``, EasyEDA Std ``docType``, the
    EasyEDA Pro zip layout, legacy ``EESchema-LIBRARY``). GUI-only inputs say
    so in ``gui_only``: EasyEDA Pro boards (kicad-cli 10.99 ``pcb import``
    crashes, pcbnew 9 ``LoadBoard(EASYEDAPRO)`` returns None) and Altium
    ``.PrjPcb`` (the CLI rejects it; its ``DocumentPath`` entries are listed).
  * ``capabilities``: ``kicad-cli <cmd> --help`` per install for ``import``,
    ``pcb import``, ``sch import``, ``fp upgrade`` and ``sym upgrade``, plus
    the options and format hints the help lists. The exit code proves nothing
    (KiCad 9 prints ``fp upgrade`` help with rc 1), so the usage line decides.
    Cached per kicad-cli path; a probe that timed out or could not start is
    not cached. Verified: KiCad 9.0.4 has only the upgrades, 10.0.5 adds
    ``pcb import`` (no ``--layer-map``), the 10.99 nightly has all five.
  * ``scan_console``: the ``Error:`` lines (and crash / refusal lines) of an
    importer's stdout+stderr. rc=0 and the importer's own JSON report are not
    proof of success (D0): the Altium SchDoc importer prints 51 ``Error:``
    lines with rc=0 and ``"errors": []``.
  * ``run_child``: every KiCad child runs with stdin=DEVNULL (D0).

Venv-side only (stdlib + kicad_env + libtable constants).
"""
from __future__ import annotations

import os
import re
import subprocess
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Iterable, Sequence

import kicad_env as env
import libtable

PROBE_TIMEOUT_S = env.PROBE_TIMEOUT_S
SNIFF_BYTES = 4096

EAGLE, ALTIUM, EASYEDA, KICAD = "eagle", "altium", "easyeda", "kicad"
SOURCE_KINDS = (EAGLE, ALTIUM, EASYEDA, KICAD)      # all in netlist_schema.SOURCE_KINDS (D4)
BOARD, SCHEMATIC, PROJECT, LIBRARY = "board", "schematic", "project", "library"
FOOTPRINT, SYMBOL = libtable.FOOTPRINT, libtable.SYMBOL

EASYEDA_PRO_BOARD_GUI = ("EasyEDA Pro boards import only in the KiCad GUI: kicad-cli 10.99 "
                         "`pcb import` crashes on them (rc 127, std::bad_function_call) and "
                         "KiCad 9 pcbnew LoadBoard(EASYEDAPRO) returns None")
ALTIUM_PROJECT_GUI = ("kicad-cli rejects .PrjPcb (\"No schematic importer recognizes the file "
                      "format\"); a whole Altium project imports only in the KiCad GUI")


class ForeignFormatError(ValueError):
    """A file this module cannot classify, or one it does not import that way."""


# ----------------------------------------------------------------------------
# Classification
# ----------------------------------------------------------------------------
@dataclass(frozen=True)
class ForeignFile:
    """One classified input. ``pcb_format`` / ``sch_format`` are kicad-cli
    ``--format`` hints ("" = not that role); ``swig_plugin`` names the
    PCB_IO_MGR type of the KiCad 9 fallback ("" = none, always for Eagle);
    ``gui_only`` says why (part of) it needs the KiCad GUI; ``documents`` lists
    what an Altium project references."""
    path: Path
    kind: str
    role: str
    label: str
    pcb_format: str = ""
    sch_format: str = ""
    swig_plugin: str = ""
    library_kinds: tuple[str, ...] = ()
    gui_only: str = ""
    documents: tuple[str, ...] = ()


def _head(path: Path, size: int = SNIFF_BYTES) -> bytes:
    with open(path, "rb") as f:
        return f.read(size)


def _eagle(path: Path, role: str, label: str, **kw) -> ForeignFile:
    if b"<eagle" not in _head(path):
        raise ForeignFormatError(f"{path.name}: not an Eagle 6+ XML file (no <eagle> root); "
                                 f"binary Eagle (< 6) files and Allegro .brd boards are not "
                                 f"imported headless")
    return ForeignFile(path, EAGLE, role, label, **kw)


def _legacy_lib(path: Path) -> ForeignFile:
    if not _head(path).startswith(b"EESchema-LIBRARY"):
        raise ForeignFormatError(f"{path.name}: not a legacy KiCad symbol library "
                                 f"(no EESchema-LIBRARY header)")
    return ForeignFile(path, KICAD, LIBRARY, "legacy KiCad symbol library",
                       library_kinds=(SYMBOL,))


_DOCTYPE_RE = re.compile(rb'"docType"\s*:\s*"?(\d+)')


def _easyeda_std(path: Path) -> ForeignFile:
    m = _DOCTYPE_RE.search(_head(path))
    doctype = m.group(1).decode() if m else None
    if doctype == "3":      # pcb import --format auto handles it ("easyeda" is no hint)
        return ForeignFile(path, EASYEDA, BOARD, "EasyEDA Std board", pcb_format="auto",
                           swig_plugin="EASYEDA")
    if doctype in ("1", "5"):
        return ForeignFile(path, EASYEDA, SCHEMATIC, "EasyEDA Std schematic",
                           sch_format="easyeda")
    raise ForeignFormatError(f"{path.name}: not an EasyEDA Std board (docType 3) or schematic "
                             f"(docType 1/5): " + (f"docType {doctype}" if doctype else
                                                   "no docType"))


def _easyeda_pro(path: Path) -> ForeignFile:
    try:
        with zipfile.ZipFile(path) as z:
            names = [n for n in z.namelist() if not n.endswith("/")]
    except zipfile.BadZipFile as e:
        raise ForeignFormatError(f"{path.name}: not an EasyEDA Pro project ({e})") from e
    sheets = any(n.startswith("SHEET/") for n in names)
    board = any(n.startswith("PCB/") for n in names)
    if not (sheets or board):
        raise ForeignFormatError(f"{path.name}: not an EasyEDA Pro project "
                                 f"(no SHEET/ or PCB/ entries)")
    gui = EASYEDA_PRO_BOARD_GUI if board else ""
    if sheets:
        return ForeignFile(path, EASYEDA, SCHEMATIC, "EasyEDA Pro project",
                           sch_format="easyedapro", gui_only=gui)
    return ForeignFile(path, EASYEDA, BOARD, "EasyEDA Pro project", gui_only=gui)


_DOCUMENT_RE = re.compile(r"^DocumentPath=(.+?)\s*$", re.M)


def _altium_project(path: Path) -> ForeignFile:
    text = path.read_text(encoding="utf-8", errors="replace")
    return ForeignFile(path, ALTIUM, PROJECT, "Altium project", gui_only=ALTIUM_PROJECT_GUI,
                       documents=tuple(m.group(1) for m in _DOCUMENT_RE.finditer(text)))


_SIMPLE: dict[str, dict] = {
    ".pcbdoc": dict(kind=ALTIUM, role=BOARD, label="Altium Designer board",
                    pcb_format="altium", swig_plugin="ALTIUM_DESIGNER"),
    ".schdoc": dict(kind=ALTIUM, role=SCHEMATIC, label="Altium schematic sheet",
                    sch_format="altium"),
    ".pcblib": dict(kind=ALTIUM, role=LIBRARY, label="Altium footprint library",
                    library_kinds=(FOOTPRINT,)),
    ".intlib": dict(kind=ALTIUM, role=LIBRARY, label="Altium integrated library",
                    library_kinds=(FOOTPRINT,)),
    ".elibz": dict(kind=EASYEDA, role=LIBRARY, label="EasyEDA Pro library",
                   library_kinds=(FOOTPRINT,)),
    ".elibz2": dict(kind=EASYEDA, role=LIBRARY, label="EasyEDA Pro library",
                    library_kinds=(FOOTPRINT,)),
}
_SNIFFED: dict[str, Callable[[Path], ForeignFile]] = {
    ".brd": lambda p: _eagle(p, BOARD, "Eagle board", pcb_format="eagle"),
    ".sch": lambda p: _eagle(p, SCHEMATIC, "Eagle schematic", sch_format="eagle"),
    ".lbr": lambda p: _eagle(p, LIBRARY, "Eagle library", library_kinds=(FOOTPRINT, SYMBOL)),
    ".lib": _legacy_lib,
    ".json": _easyeda_std,
    ".epro": _easyeda_pro,
    ".zip": _easyeda_pro,
    ".prjpcb": _altium_project,
}
SUPPORTED_SUFFIXES = tuple(sorted({*_SIMPLE, *_SNIFFED}))


def classify(path: str | os.PathLike) -> ForeignFile:
    """What a foreign file is and how KiCad imports it. Raises
    FileNotFoundError, or ForeignFormatError for anything unsupported."""
    p = Path(os.path.abspath(path))
    if not p.is_file():
        raise FileNotFoundError(f"foreign design file not found: {p}")
    suffix = p.suffix.lower()
    if suffix in _SIMPLE:
        return ForeignFile(p, **_SIMPLE[suffix])
    if suffix in _SNIFFED:
        return _SNIFFED[suffix](p)
    raise ForeignFormatError(f"{p.name}: unsupported format {suffix or '(no extension)'}; "
                             f"supported: {', '.join(SUPPORTED_SUFFIXES)}")


# ----------------------------------------------------------------------------
# Children (stdin detached)
# ----------------------------------------------------------------------------
def _decode(data: bytes | str | None) -> str:
    return data.decode("utf-8", "replace") if isinstance(data, bytes) else (data or "")


def run_child(argv: Sequence[str], timeout_s: float, cwd: Path | None = None
              ) -> env.ProbeResult:
    """Run a KiCad child with stdin DETACHED (D0: a python child that inherits
    an MCP server's stdin hangs, and nobody could answer a dialog). A timeout
    or launch failure is returncode TRANSIENT_RC with the reason in stderr.
    Output is decoded as UTF-8. Tests monkeypatch this."""
    try:
        r = subprocess.run(list(argv), capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout_s, stdin=subprocess.DEVNULL,
                           cwd=str(cwd) if cwd else None)
    except subprocess.TimeoutExpired as e:
        return env.ProbeResult(env.TRANSIENT_RC, _decode(e.stdout),
                               f"{argv[0]}: timed out after {timeout_s:g} s")
    except OSError as e:
        return env.ProbeResult(env.TRANSIENT_RC, "", f"{argv[0]}: cannot start ({e})")
    return env.ProbeResult(r.returncode, r.stdout or "", r.stderr or "")


# ----------------------------------------------------------------------------
# Capability probe: `kicad-cli <cmd> --help` per install, cached per path
# ----------------------------------------------------------------------------
_PROBES: dict[str, tuple[str, ...]] = {
    "project_import": ("import",), "pcb_import": ("pcb", "import"),
    "sch_import": ("sch", "import"), "fp_upgrade": ("fp", "upgrade"),
    "sym_upgrade": ("sym", "upgrade")}
_USAGE_RE = re.compile(r"^Usage:\s+([^\[\n]*)", re.M)
_FORMATS_RE = re.compile(r"Input format hint:\s*([^()\n]+?)\s*\(default")


@dataclass(frozen=True)
class ImportCapabilities:
    """What one install can import: its kicad-cli commands and options, and its
    bundled pcbnew Python (the SWIG fallback; None on KiCad 11)."""
    version: str
    version_tuple: tuple[int, int, int]
    kicad_cli: Path
    python: Path | None = None
    project_import: bool = False
    pcb_import: bool = False
    sch_import: bool = False
    fp_upgrade: bool = False
    sym_upgrade: bool = False
    pcb_report_json: bool = False
    sch_report_json: bool = False
    layer_map: bool = False
    pcb_formats: tuple[str, ...] = ()
    sch_formats: tuple[str, ...] = ()
    probe_errors: tuple[str, ...] = ()

    @property
    def tool(self) -> str:
        return f"kicad-cli {self.version}"

    def as_dict(self) -> dict:
        return {**asdict(self), "kicad_cli": str(self.kicad_cli),
                "python": str(self.python) if self.python else None}


_CAPS: dict[str, ImportCapabilities] = {}


def clear_cache() -> None:
    """Forget every probed install (tests; a KiCad was installed or removed)."""
    _CAPS.clear()


def help_supports(text: str, words: Sequence[str]) -> bool:
    """True when `kicad-cli <words> --help` printed the help OF that command.
    The exit code proves nothing: KiCad 9 prints `fp upgrade` help with rc 1
    (a missing positional), an unknown command prints "Failed to parse" (9)
    or its parent's usage with rc 0 (10.0 `sch import`)."""
    if "Failed to parse" in text:
        return False
    m = _USAGE_RE.search(text)
    names = m.group(1).split() if m else []
    if not names or names[-1] != words[-1]:
        return False
    parent = words[-2] if len(words) > 1 else "kicad-cli"
    return len(names) == 1 or names[-2] == parent


def _formats(text: str) -> tuple[str, ...]:
    m = _FORMATS_RE.search(text)
    return tuple(f.strip() for f in m.group(1).split(",") if f.strip()) if m else ()


def capabilities(install: env.KiCadInstall) -> ImportCapabilities:
    """Probe one install (five `--help` runs). Cached per kicad-cli path,
    except when a probe timed out or could not start (probe_errors)."""
    key = os.path.normcase(os.path.abspath(str(install.kicad_cli)))
    if key in _CAPS:
        return _CAPS[key]
    found: dict[str, bool] = {}
    texts: dict[str, str] = {}
    errors: list[str] = []
    for name, words in _PROBES.items():
        r = run_child([str(install.kicad_cli), *words, "--help"], PROBE_TIMEOUT_S)
        if r.returncode == env.TRANSIENT_RC:
            errors.append(f"{' '.join(words)} --help: {r.stderr.strip()}")
        texts[name] = f"{r.stdout}\n{r.stderr}"
        found[name] = r.returncode != env.TRANSIENT_RC and help_supports(texts[name], words)
    pcb = texts["pcb_import"] if found["pcb_import"] else ""
    sch = texts["sch_import"] if found["sch_import"] else ""
    caps = ImportCapabilities(
        version=install.version, version_tuple=install.version_tuple,
        kicad_cli=install.kicad_cli, python=install.python, **found,
        pcb_report_json="--report-format" in pcb, sch_report_json="--report-format" in sch,
        layer_map="--layer-map" in pcb, pcb_formats=_formats(pcb), sch_formats=_formats(sch),
        probe_errors=tuple(errors))
    if not errors:
        _CAPS[key] = caps
    return caps


def discover_capabilities(installs: Iterable[env.KiCadInstall] | None = None
                          ) -> list[ImportCapabilities]:
    """Capabilities of `installs` (default: every discovered KiCad), newest first."""
    found = env.discover() if installs is None else list(installs)
    return sorted((capabilities(i) for i in found), key=lambda c: c.version_tuple, reverse=True)


def first_with(caps: Sequence[ImportCapabilities], attr: str) -> ImportCapabilities | None:
    """The first (newest, for discover_capabilities order) install with `attr`."""
    return next((c for c in caps if getattr(c, attr)), None)


# ----------------------------------------------------------------------------
# Console scanning
# ----------------------------------------------------------------------------
_TIMESTAMP_RE = re.compile(r"^\s*\d{1,2}:\d{2}:\d{2}(?:\s*[AP]M)?:\s*")
_ERROR_RE = re.compile(r"\bError:\s*(.*\S)")
_WARNING_RE = re.compile(r"\bWarning:\s*(.*\S)")
_FATAL_RE = re.compile(r"Unhandled exception|Fatal Python error|Segmentation fault|"
                       r"No (?:PCB|schematic) importer recognizes|Unable to convert library|"
                       r"Output path must not conflict")
_NOTICE_RE = re.compile(r"conflicting placed definitions|not found; substituting|"
                        r"has no KiCad equivalent")


@dataclass(frozen=True)
class ConsoleScan:
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


def scan_console(*texts: str) -> ConsoleScan:
    """The error and warning lines of an importer's stdout/stderr: ``Error:``
    lines (wx log timestamp stripped, the text after the label kept), crash /
    refusal lines, ``Warning:`` lines and known lossy-import notices. Progress
    chatter ("Importing", "Setting schematic symbol ...") is skipped."""
    errors: list[str] = []
    warnings: list[str] = []
    for raw in "\n".join(texts).splitlines():
        line = _TIMESTAMP_RE.sub("", raw).strip()
        if m := _ERROR_RE.search(line):
            errors.append(m.group(1))
        elif _FATAL_RE.search(line):
            errors.append(line)
        elif m := _WARNING_RE.search(line):
            warnings.append(m.group(1))
        elif _NOTICE_RE.search(line):
            warnings.append(line)
    return ConsoleScan(tuple(errors), tuple(warnings))
