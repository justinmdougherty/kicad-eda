"""One connection to KiCad 11 for the schematic backend.

A ``KiCadSession`` is either a headless ``kicad-cli api-server`` this object
launched and owns (stopped by ``close()``) or a connection to a running KiCad
GUI. KiCad's headless server holds ONE open document at a time, so
``open_document``/``create_document`` first close whatever is open.

Mode selection (``select_mode``) is a pure function so it is unit-tested with
fake open-document lists:

* ``headless`` always launches a private server;
* ``gui`` requires a reachable KiCad 11 GUI that has the file open;
* ``auto`` uses the GUI when the file is open there, else headless.

Libraries (ROADMAP D0/D1, verified on kicad-cli 10.99.0-4025): the api-server
reads the PROJECT ``sym-lib-table`` (``${KIPRJMOD}`` = the schematic's
directory) when a document is created or opened, never later; so every
library cache here is dropped per document. ``check_project_libraries``
turns a project row KiCad could not load into an error naming that table,
``reload_project_libraries`` re-reads edited library files (ReloadLibrary
works; the table-editing RPCs have no handler, so tables are written as
files). With no global tables (an empty config dir) KiCad loads 0 global
symbol libraries and every stock symbol is just "not found": the session
counts the global rows and ``is_missing_global_tables`` recognises the
resulting error.
"""
from __future__ import annotations

import os
import re
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path

import kicad_env as env
from backends import kipy_bridge as kb
from backends.errors import SchematicApiError

if str(env.TOOLS_DIR) not in sys.path:      # server/tools holds libtable (stdlib-only)
    sys.path.insert(0, str(env.TOOLS_DIR))
import libtable  # noqa: E402

MODES = ("auto", "gui", "headless")
HEADLESS_TIMEOUT_MS = 30_000
GUI_TIMEOUT_MS = 2_000
LIBRARY_TIMEOUT_S = 120.0
SCH_SUFFIX = ".kicad_sch"
SYM_TABLE = libtable.TABLE_FILES[libtable.SYMBOL]          # "sym-lib-table"
LIB_TABLE_HINT = ("KiCad 11 headless finds no stock libraries unless its config dir "
                  "(%APPDATA%\\kicad\\10.99 on Windows, ~/.config/kicad/10.99 elsewhere, "
                  "$KICAD_CONFIG_HOME/10.99 when that is set) holds sym-lib-table and "
                  "fp-lib-table; copy them from <install>/share/kicad/template (this plugin "
                  "never edits the KiCad config)")
# The marker every missing-global-tables error carries (is_missing_global_tables).
GLOBAL_TABLES_MISSING = "KiCad 11 has no global symbol library table"
REOPEN_HINT = ("KiCad reads the project table only when a document is created or opened: a "
               "row added since then is seen once the saved document is opened again "
               "(open_document)")
# KiCad's own words when the global tables are absent. The nightly says no more
# than "symbol 'Device:R' not found" -- exactly what a misspelt name gets -- so
# only PWR_FLAG (in every stock power library) is evidence on its own.
_MISSING_GLOBAL_RE = re.compile(
    re.escape(GLOBAL_TABLES_MISSING)
    + rf"|symbol '{re.escape(libtable.POWER_LIBRARY)}:PWR_FLAG' not found")


def library_hint(project_dir: str | os.PathLike | None,
                 global_tables_missing: bool = False) -> str:
    """What to check when KiCad cannot find a library or symbol: the project
    table ``<project_dir>/sym-lib-table`` first; led by GLOBAL_TABLES_MISSING
    when the session saw zero global symbol libraries."""
    table = str(Path(project_dir) / SYM_TABLE) if project_dir else f"the project's {SYM_TABLE}"
    project = (f"a project symbol library must be a row of {table} (URI ${{KIPRJMOD}}/...) "
               "present when the document was created or opened; after editing a library "
               "file call reload_project_libraries")
    if global_tables_missing:
        return (f"{GLOBAL_TABLES_MISSING} (0 global symbol libraries loaded): "
                f"{LIB_TABLE_HINT}; {project}")
    return (f"{project}; stock libraries come from the global {SYM_TABLE} in KiCad 11's "
            f"config dir (a copy of <install>/share/kicad/template/{SYM_TABLE})")


def is_missing_global_tables(message: str) -> bool:
    """True when an error says KiCad 11 has no global library tables: the
    session's GLOBAL_TABLES_MISSING marker (stamped once GetLibraryStatuses
    showed 0 global symbol libraries), or KiCad not finding power:PWR_FLAG.
    A bare "symbol 'Device:R' not found" is NOT enough (a typo reads the same)."""
    return bool(_MISSING_GLOBAL_RE.search(str(message or "")))


def _nicknames(nicks: Iterable[str] | None) -> list[str] | None:
    """Unique nicknames in order; None (= every row) for None or empty."""
    if nicks is None:
        return None
    if isinstance(nicks, (str, bytes)):
        raise SchematicApiError(f"nicks must be a list of nicknames, got the string {nicks!r}")
    names = list(dict.fromkeys(str(n) for n in nicks))
    if any(not n.strip() for n in names):
        raise SchematicApiError(f"library nicknames must be non-empty, got {names}")
    return names or None


def table_rows(table: Path) -> tuple[list[str], list[str]]:
    """(enabled nicknames, problems) of a sym-lib-table file; ([], []) when absent."""
    if not table.is_file():
        return [], []
    try:
        parsed = libtable.read_table(table)
    except (libtable.LibTableError, OSError) as exc:
        return [], [f"{table} cannot be read: {exc}"]
    if parsed.tag != libtable.TABLE_TAGS[libtable.SYMBOL]:
        return [], [f"{table} is a ({parsed.tag} ...) file, not a symbol library table"]
    return [row.name for row in parsed.rows if not row.disabled], []


def project_library_problems(statuses: Iterable, wanted: Sequence[str] | None,
                             rows_on_disk: Sequence[str]) -> tuple[list[str], list[str]]:
    """(loaded nicknames sorted, problems) from KiCad's project symbol-library
    statuses. ``wanted`` narrows the check (None = every row KiCad or the file
    has); a nickname KiCad has no row for, or a row not LLS_LOADED, is a problem."""
    by_nick = {s.nickname: s for s in statuses}
    expected = (list(dict.fromkeys([*rows_on_disk, *by_nick])) if wanted is None
                else list(wanted))
    unseen = [n for n in expected if n not in by_nick]
    missing = ([f"KiCad has no project row {', '.join(repr(n) for n in unseen)} (rows it "
                f"read: {', '.join(repr(n) for n in by_nick) or 'none'})"] if unseen else [])
    failed = [by_nick[n] for n in expected if n in by_nick and by_nick[n].status != kb.LLS_LOADED]
    broken = [f"'{s.nickname}' {kb.library_status_name(s.status)}: "
              f"{s.error_message or 'no error message'}" for s in failed]
    loaded = sorted(n for n in expected if n in by_nick and by_nick[n].status == kb.LLS_LOADED)
    return loaded, [*missing, *broken]


def norm_path(path: str | os.PathLike) -> str:
    """Case-normalised absolute path, the key for 'is this file open in KiCad'."""
    return os.path.normcase(os.path.abspath(str(path)))


def doc_sch_path(doc) -> str:
    """Root .kicad_sch of an open-document specifier (project dir + project
    name), '' when the specifier carries no project."""
    project = getattr(doc, "project", None)
    pdir = str(getattr(project, "path", "") or "")
    name = str(getattr(project, "name", "") or "")
    if not (pdir and name):
        return ""
    return str(Path(pdir) / f"{name}{SCH_SUFFIX}")


NO_HANDLER = "no handler available"    # headless KiCad before any document is open


def open_schematics(kicad) -> list:
    """Open-document specifiers of every schematic; [] when nothing is open
    yet (the headless server has no GetOpenDocuments handler until a document
    exists). Other API errors propagate as SchematicApiError."""
    try:
        return list(kicad.get_open_documents(kb.DocumentType.DOCTYPE_SCHEMATIC))
    except kb.KIPY_EXCEPTIONS as exc:
        if NO_HANDLER in str(exc):
            return []
        raise SchematicApiError(f"listing open schematics failed: {exc}") from exc


def open_schematic_paths(kicad) -> list[str]:
    """Normalised paths of every schematic open in a connected KiCad; [] when
    that KiCad predates the schematic API (KiCad 9/10 refuse the query)."""
    try:
        docs = kicad.get_open_documents(kb.DocumentType.DOCTYPE_SCHEMATIC)
    except kb.KIPY_EXCEPTIONS:
        return []
    return [norm_path(p) for p in (doc_sch_path(d) for d in docs) if p]


def select_mode(requested: str, path: str | os.PathLike, gui_docs: list[str] | None) -> str:
    """Which mode serves ``path``: ``gui_docs`` is the normalised list of
    schematics open in a reachable GUI, None when no GUI answers."""
    if requested not in MODES:
        raise SchematicApiError(f"mode must be one of {MODES}, got {requested!r}")
    if requested == "headless":
        return "headless"
    in_gui = gui_docs is not None and norm_path(path) in gui_docs
    if requested == "auto":
        return "gui" if in_gui else "headless"
    if gui_docs is None:
        raise SchematicApiError(
            "mode='gui' but no KiCad GUI answers on the IPC socket: start KiCad 11, enable "
            "Preferences > Plugins > Enable API server, open the schematic and retry "
            "(or use mode='auto' / 'headless')")
    if not in_gui:
        raise SchematicApiError(
            f"mode='gui' but {path} is not open in the running KiCad (open schematics: "
            f"{gui_docs or 'none'}); open it in the schematic editor or use mode='headless'")
    return "gui"


def _version_string(kicad) -> str:
    """The connected KiCad's version. This is the FIRST request on a new
    connection: kipy's client dials lazily, so a refused/absent socket
    surfaces here, not in the KiCad() constructor."""
    try:
        version = kicad.get_version()
    except kb.KIPY_EXCEPTIONS as exc:
        raise SchematicApiError(f"KiCad answered but GetVersion failed: {exc}") from exc
    return str(getattr(version, "full_version", "") or version)


def _probe_gui(kicad) -> str | None:
    """Version string of a reachable KiCad GUI, or None (connection closed)
    when nothing answers on its socket."""
    try:
        return _version_string(kicad)
    except SchematicApiError:
        kicad.close()
        return None


class KiCadSession:
    """A KiCad connection plus the state the schematic backend needs: the
    project of the current document, whether libraries are loaded, how many
    global symbol libraries KiCad has and which symbols a library offers."""

    def __init__(self, kicad, mode: str, install: env.KiCadInstall | None = None,
                 kicad_version: str | None = None) -> None:
        self.kicad = kicad
        self.mode = mode
        self.install = install
        self.kicad_version = kicad_version or _version_string(kicad)
        self.library_errors: list[str] = []
        self.global_symbol_libraries: int | None = None   # None = not counted (GUI / no load)
        self.project_dir: Path | None = None
        self._libraries_loaded = mode == "gui"     # the GUI preloads on its own
        self._lib_names: dict[str, set[str]] = {}
        self._closed = False

    # --- construction -------------------------------------------------------
    @classmethod
    def headless(cls, install: env.KiCadInstall | None = None,
                 timeout_ms: int = HEADLESS_TIMEOUT_MS) -> KiCadSession:
        """Launch a private `kicad-cli api-server` (newest KiCad 11 found)."""
        kb.require()
        install = install or env.pick_api()
        if install is None:
            seen = [f"{i.version} at {i.root}" for i in env.discover()] or ["none"]
            raise SchematicApiError(
                "no KiCad with `kicad-cli api-server` found (needs a KiCad 11 nightly, "
                f"10.99+); installs seen: {seen}")
        try:
            kicad = kb.KiCad(headless=True, kicad_cli_path=str(install.kicad_cli),
                             timeout_ms=timeout_ms)
        except kb.KIPY_EXCEPTIONS as exc:
            raise SchematicApiError(
                f"could not start `{install.kicad_cli} api-server`: {exc}") from exc
        return cls(kicad, "headless", install)

    @classmethod
    def gui(cls, timeout_ms: int = GUI_TIMEOUT_MS) -> KiCadSession | None:
        """Connect to a running KiCad GUI on the default IPC socket; None when
        nothing answers. The constructor alone proves nothing (the client
        dials on its first request), so the connection is VERIFIED with a
        GetVersion round trip (a refused connection returns in milliseconds,
        a silent socket after ``timeout_ms``)."""
        kb.require()
        try:
            kicad = kb.KiCad(timeout_ms=timeout_ms)
        except kb.KIPY_EXCEPTIONS:
            return None
        version = _probe_gui(kicad)
        if version is None:
            return None
        return cls(kicad, "gui", env.pick_api(), kicad_version=version)

    # --- documents ----------------------------------------------------------
    def open_documents(self) -> list[str]:
        return open_schematic_paths(self.kicad)

    def close_all_documents(self) -> None:
        """Close every open schematic (headless holds one document at a time)."""
        for doc in open_schematics(self.kicad):
            try:
                self.kicad.close_document(doc)
            except kb.KIPY_EXCEPTIONS as exc:
                raise SchematicApiError(
                    f"closing {doc_sch_path(doc)} failed: {exc}") from exc

    def open_document(self, path: str) -> None:
        self._require_headless("open a document by path")
        self._new_project(path)
        self.close_all_documents()
        try:
            self.kicad.open_document(str(path), kb.DocumentType.DOCTYPE_SCHEMATIC)
        except kb.KIPY_EXCEPTIONS as exc:
            raise SchematicApiError(f"KiCad could not open {path}: {exc}") from exc

    def create_document(self, path: str) -> None:
        """A new in-memory schematic (+ project) at path; nothing is written
        until save(). KiCad refuses while another unsaved document is open."""
        self._require_headless("create a document")
        self._new_project(path)
        self.close_all_documents()
        try:
            self.kicad.create_document(str(path), kb.DocumentType.DOCTYPE_SCHEMATIC)
        except kb.KIPY_EXCEPTIONS as exc:
            raise SchematicApiError(f"KiCad could not create {path}: {exc}") from exc

    def _new_project(self, path: str) -> None:
        """Another document means another project whose tables KiCad reads at
        create/open: drop every library cache, so the next use loads again
        (~0.6 s once the stock libraries are in the server's memory)."""
        self.project_dir = Path(os.path.abspath(str(path))).parent
        self.library_errors = []
        self.global_symbol_libraries = None
        self._libraries_loaded = False
        self._lib_names = {}

    def schematic_for(self, path: str):
        """The kipy Schematic bound to the open document at ``path`` (whose
        directory becomes the session's project, for a GUI document too)."""
        docs = open_schematics(self.kicad)
        wanted = norm_path(path)
        for doc in docs:
            if norm_path(doc_sch_path(doc)) == wanted:
                doc.ClearField("sheet_path")         # all sheets, like KiCad.get_schematic
                self.project_dir = Path(os.path.abspath(str(path))).parent
                return kb.Schematic(self.kicad._client, doc)  # noqa: SLF001 (mirrors kipy)
        open_now = [doc_sch_path(d) for d in docs] or ["none"]
        raise SchematicApiError(f"{path} is not open in KiCad (open: {open_now})")

    # --- libraries ----------------------------------------------------------
    @property
    def project_table(self) -> Path | None:
        """``<project>/sym-lib-table`` of the current document (None before one)."""
        return None if self.project_dir is None else self.project_dir / SYM_TABLE

    @property
    def global_tables_missing(self) -> bool:
        """True once a load showed KiCad has zero global symbol libraries."""
        return self.global_symbol_libraries == 0

    def library_hint(self) -> str:
        """The module-level library_hint for this session's project and state."""
        return library_hint(self.project_dir, self.global_tables_missing)

    def load_libraries(self, timeout_s: float = LIBRARY_TIMEOUT_S) -> None:
        """Load every library once per document (headless; the GUI preloads)."""
        if self._libraries_loaded:
            return
        try:
            self.kicad.load_all_libraries()
            statuses = self.kicad.wait_for_libraries(timeout_s=timeout_s)
        except kb.KIPY_EXCEPTIONS as exc:
            raise SchematicApiError(
                f"loading KiCad libraries failed: {exc}. {self.library_hint()}") from exc
        self._record(statuses)
        self._libraries_loaded = True

    def _record(self, statuses: Iterable) -> None:
        statuses = list(statuses)
        self.library_errors = [f"{s.nickname}: {s.error_message}" for s in statuses
                               if s.status != kb.LLS_LOADED and s.error_message]
        self.global_symbol_libraries = sum(
            1 for s in statuses if s.type == kb.LT_SYMBOL and s.scope == kb.LTS_GLOBAL)

    def check_project_libraries(self, nicks: Iterable[str] | None = None,
                                timeout_s: float = LIBRARY_TIMEOUT_S) -> list[str]:
        """Nicknames (sorted) of the project's symbol libraries KiCad loaded,
        narrowed to ``nicks``. Raises SchematicApiError naming
        ``<project>/sym-lib-table`` when a (requested) row failed to load,
        KiCad has no row for a requested nickname or for a row of the table
        file (written after the open), or the status query fails. Without a
        project table and with nothing requested the answer is []."""
        wanted = _nicknames(nicks)
        table = self._require_project_table()
        self.load_libraries(timeout_s)
        try:
            statuses = self.kicad.wait_for_libraries(types=kb.LT_SYMBOL, timeout_s=timeout_s,
                                                     scope=kb.LTS_PROJECT)
        except kb.KIPY_EXCEPTIONS as exc:
            raise SchematicApiError(
                f"reading the load status of the libraries of {table} failed: {exc}") from exc
        rows_on_disk, table_problems = table_rows(table)
        loaded, found = project_library_problems(statuses, wanted, rows_on_disk)
        problems = [*table_problems, *found]
        if problems:
            raise SchematicApiError(
                f"project symbol libraries of {table} are not usable: {'; '.join(problems)}. "
                f"Fix the library file or its row, then call reload_project_libraries; "
                f"{REOPEN_HINT}")
        return loaded

    def reload_project_libraries(self, nicks: Iterable[str] = (),
                                 timeout_s: float = LIBRARY_TIMEOUT_S) -> list[str]:
        """Re-read project symbol libraries from disk (``nicks``, or every
        project row) after their files changed, drop the symbol-name caches and
        return check_project_libraries(nicks). Before any load this is just the
        first load. A nickname KiCad has no row for raises, naming the table."""
        names = _nicknames(nicks)
        table = self._require_project_table()
        if not self._libraries_loaded:
            self.load_libraries(timeout_s)
        else:
            try:
                self.kicad.reload_library(kb.LT_SYMBOL, kb.LTS_PROJECT, names or [])
                statuses = self.kicad.wait_for_libraries(timeout_s=timeout_s)
            except kb.KIPY_EXCEPTIONS as exc:
                raise SchematicApiError(
                    f"reloading {names or 'every project library'} of {table} failed: "
                    f"{exc}; {REOPEN_HINT}") from exc
            self._record(statuses)
        self._lib_names = {}
        return self.check_project_libraries(names, timeout_s)

    def _require_project_table(self) -> Path:
        table = self.project_table
        if table is None:
            raise SchematicApiError(
                "no project document is open: create_document / open_document (or bind a "
                "GUI document with schematic_for) before checking project libraries")
        return table

    def library_symbol_names(self, nickname: str) -> set[str]:
        """Symbol names of one library (cached), e.g. the `power` library."""
        if nickname not in self._lib_names:
            self.load_libraries()
            try:
                items = self.kicad.get_library_symbols([nickname])
            except kb.KIPY_EXCEPTIONS as exc:
                raise SchematicApiError(f"listing library {nickname!r} failed: {exc}") from exc
            self._lib_names[nickname] = {item.name for item in items}
        return self._lib_names[nickname]

    def has_library_symbol(self, lib_id: str) -> bool:
        nickname, _, name = lib_id.partition(":")
        return name in self.library_symbol_names(nickname)

    # --- lifecycle ----------------------------------------------------------
    def close(self) -> None:
        """Drop the connection; stops the headless server this session owns."""
        if self._closed:
            return
        self._closed = True
        self.kicad.close()

    @property
    def closed(self) -> bool:
        return self._closed

    def __enter__(self) -> KiCadSession:
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def _require_headless(self, what: str) -> None:
        if self.mode != "headless":
            raise SchematicApiError(
                f"cannot {what} through a KiCad GUI connection; use mode='headless'")


def connect(mode: str, path: str) -> KiCadSession:
    """A session able to serve ``path`` in the requested mode (see module doc)."""
    if mode not in MODES:
        raise SchematicApiError(f"mode must be one of {MODES}, got {mode!r}")
    if mode == "headless":
        return KiCadSession.headless()
    gui = KiCadSession.gui()
    docs = gui.open_documents() if gui is not None else None
    try:
        chosen = select_mode(mode, path, docs)
    except SchematicApiError:
        if gui is not None:
            gui.close()
        raise
    if chosen == "gui":
        return gui                                   # type: ignore[return-value]
    if gui is not None:
        gui.close()
    return KiCadSession.headless()
