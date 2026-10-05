"""One connection to KiCad for the board backend.

A ``BoardSession`` is either a headless ``kicad-cli api-server`` (KiCad 11)
this object launched with the board PRELOADED and owns (stopped by
``close()``), or a connection to a running KiCad GUI whose PCB editor has
the board open. A headless server holds ONE board: a second board means a
second session (the tool registry keeps one headless board at a time).

GUI VERSIONS: the board API grew over KiCad 9/10, so a GUI session degrades
by version (``BoardApi`` reports each degradation in ``notes``): the
enabled-layer query needs KiCad 9.0.5 (older GUIs get the copper layers
from the file's ``(layers ...)`` table), pad -> footprint parents need 10.0.6
(older GUIs resolve pad references through the footprint definitions),
connectivity (GetConnectedItems: the unrouted list, route_status.complete)
needs 10.0.1 (``connectivity_available`` is false below it) and the design
rules need KiCad 11 (older GUIs report None and fall back to the project's
net classes). Laying copper works on every KiCad 9+ GUI.

FILE FORMAT: a board saved through a headless session is written by KiCad 11
(``(version 20260901)``, ``(generator_version "10.99")``). KiCad 9/10, their
kicad-cli and their pcbnew Python cannot load such a file again ("Failed to
load board"), so from the first save on only KiCad 11 tools work on it:
kicad-cli 11 DRC / Gerbers and this backend; the Freerouting / SWIG tools
refuse it. A board edited in a GUI session keeps that GUI's format.

Mode selection (``select_mode``) is a pure function, unit-tested with fake
open-document lists:

* ``headless`` always launches a private server (needs KiCad 11);
* ``gui`` requires a reachable KiCad GUI whose PCB editor has the board open;
* ``auto`` uses the GUI when the board is open there, else headless.

LIBRARIES (ROADMAP D0/D1, verified on kicad-cli 10.99.0-4025): a headless
api-server loads no library on its own, so PlaceFootprintFromLibrary answers
"footprint '<lib_id>' not found" until ``load_libraries`` ran (once per bound
board; a GUI preloads its own). KiCad reads the PROJECT ``fp-lib-table``
(``${KIPRJMOD}`` = the board's directory) when the board is opened, never
later: write it (``libtable.LibraryResolver.materialize``) before opening.
``footprint_library_hint`` says what the last load reported for a nickname.
"""
from __future__ import annotations

import os
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import kicad_env as env
from backends import kipy_bridge as kb
from backends.errors import BoardApiError
from backends.sch_session import LIB_TABLE_HINT, NO_HANDLER, norm_path

if str(env.TOOLS_DIR) not in sys.path:      # server/tools holds libtable (stdlib-only)
    sys.path.insert(0, str(env.TOOLS_DIR))
import libtable  # noqa: E402

MODES = ("auto", "gui", "headless")
HEADLESS_TIMEOUT_MS = 60_000        # zone refills on a dense board take a while
GUI_TIMEOUT_MS = 2_000
LIBRARY_TIMEOUT_S = 120.0           # ~8 s for the 155 stock footprint libraries
PCB_SUFFIX = ".kicad_pcb"
FP_TABLE = libtable.TABLE_FILES[libtable.FOOTPRINT]          # "fp-lib-table"
NO_API_HINT = ("no KiCad with `kicad-cli api-server` found (needs a KiCad 11 nightly, "
               "10.99+)")
# Leads the hint once a load showed zero global footprint libraries (empty config dir).
GLOBAL_FP_TABLES_MISSING = "KiCad 11 has no global footprint library table"
_KIND_NAMES = {kb.LT_SYMBOL: "symbol", kb.LT_FOOTPRINT: "footprint"}

__all__ = ["MODES", "BoardSession", "connect", "select_mode", "resolve_auto", "doc_pcb_path",
           "open_boards", "open_board_paths", "no_api_reason", "norm_path", "FP_TABLE",
           "GLOBAL_FP_TABLES_MISSING", "footprint_library_hint", "footprint_library_state",
           "footprint_statuses"]


def doc_pcb_path(doc) -> str:
    """Absolute .kicad_pcb path of an open-document specifier. KiCad reports
    ``board_filename`` RELATIVE to the project directory (headless and GUI
    alike); '' when the specifier names no board and no project."""
    name = str(getattr(doc, "board_filename", "") or "")
    project = getattr(doc, "project", None)
    pdir = str(getattr(project, "path", "") or "")
    pname = str(getattr(project, "name", "") or "")
    if not name:
        return str(Path(pdir) / f"{pname}{PCB_SUFFIX}") if pdir and pname else ""
    if Path(name).is_absolute() or not pdir:
        return name
    return str(Path(pdir) / name)


def open_boards(kicad) -> list:
    """Open-document specifiers of every board; [] on a headless server that
    has no document yet. Other API errors propagate as BoardApiError."""
    try:
        return list(kicad.get_open_documents(kb.DocumentType.DOCTYPE_PCB))
    except kb.KIPY_EXCEPTIONS as exc:
        if NO_HANDLER in str(exc):
            return []
        raise BoardApiError(f"listing open boards failed: {exc}") from exc


def open_board_paths(kicad) -> list[str]:
    """Normalised paths of every board open in a connected KiCad; [] when the
    query is refused."""
    try:
        docs = kicad.get_open_documents(kb.DocumentType.DOCTYPE_PCB)
    except kb.KIPY_EXCEPTIONS:
        return []
    return [norm_path(p) for p in (doc_pcb_path(d) for d in docs) if p]


def select_mode(requested: str, path: str | os.PathLike, gui_docs: list[str] | None) -> str:
    """Which mode serves ``path``: ``gui_docs`` is the normalised list of
    boards open in a reachable GUI, None when no GUI answers."""
    if requested not in MODES:
        raise BoardApiError(f"mode must be one of {MODES}, got {requested!r}")
    if requested == "headless":
        return "headless"
    in_gui = gui_docs is not None and norm_path(path) in gui_docs
    if requested == "auto":
        return "gui" if in_gui else "headless"
    if gui_docs is None:
        raise BoardApiError(
            "mode='gui' but no KiCad GUI answers on the IPC socket: start KiCad, enable "
            "Preferences > Plugins > Enable API server, open the board in the PCB editor and "
            "retry (or use mode='auto' / 'headless')")
    if not in_gui:
        raise BoardApiError(
            f"mode='gui' but {path} is not open in the running KiCad PCB editor (open boards: "
            f"{gui_docs or 'none'}); open it there or use mode='headless'")
    return "gui"


def resolve_auto(path: str | os.PathLike) -> str:
    """What mode 'auto' would use for ``path`` RIGHT NOW: 'gui' when a
    reachable KiCad GUI has it open in its PCB editor, else 'headless'. The
    probe connection is closed again; a caller uses this to decide on its
    headless housekeeping before it opens anything."""
    gui = BoardSession.gui()
    if gui is None:
        return "headless"
    try:
        docs = gui.open_boards()
    finally:
        gui.close()
    return select_mode("auto", path, docs)


def no_api_reason() -> str:
    """Why no headless session can start: the installs seen, none with api-server."""
    seen = [f"{i.version} at {i.root}" for i in env.discover()] or ["none"]
    return f"{NO_API_HINT}; installs seen: {seen}"


# --- footprint libraries ------------------------------------------------------
def footprint_library_hint(project_dir: str | os.PathLike | None,
                           global_tables_missing: bool = False) -> str:
    """Where a footprint library must be for KiCad to place from it: a row of
    ``<project_dir>/fp-lib-table`` (marked absent when there is no such file)
    present when the board was opened; led by GLOBAL_FP_TABLES_MISSING when a
    load saw zero global footprint libraries."""
    if project_dir:
        table = Path(project_dir) / FP_TABLE
        where = str(table) if table.is_file() else f"{table} (absent)"
    else:
        where = f"the project's {FP_TABLE}"
    project = (f"a project footprint library must be a row of {where} (URI ${{KIPRJMOD}}/...) "
               "written before the board was opened: KiCad reads that table when it opens "
               "the board, never later (write it, then open the board again)")
    if global_tables_missing:
        return (f"{GLOBAL_FP_TABLES_MISSING} (0 global footprint libraries loaded): "
                f"{LIB_TABLE_HINT}; {project}")
    return (f"{project}; stock footprints come from the global {FP_TABLE} in KiCad 11's "
            f"config dir (a copy of <install>/share/kicad/template/{FP_TABLE})")


def footprint_statuses(statuses: Iterable) -> dict[str, Any]:
    """{nickname: library status} of the footprint rows of a library-status
    list; a project row shadows a global row of the same nickname (as KiCad
    resolves a nickname)."""
    rows = [s for s in statuses if s.type == kb.LT_FOOTPRINT]
    ordered = ([s for s in rows if s.scope != kb.LTS_PROJECT]
               + [s for s in rows if s.scope == kb.LTS_PROJECT])
    return {s.nickname: s for s in ordered}


def footprint_library_state(nickname: str, statuses: Mapping[str, Any], loaded: bool) -> str:
    """What the last load said about footprint library ``nickname`` ('' when
    nothing was loaded: a GUI session, or before the first load)."""
    if not loaded:
        return ""
    status = statuses.get(nickname)
    if status is None:
        return (f"KiCad has no footprint library {nickname!r} (no row of that name in the "
                f"project or the global {FP_TABLE})")
    where = f"{'project' if status.scope == kb.LTS_PROJECT else 'global'} {FP_TABLE}"
    if status.status == kb.LLS_LOADED:
        return f"footprint library {nickname!r} ({where}) is loaded: check the footprint name"
    reason = status.error_message or "no error message"
    return (f"footprint library {nickname!r} ({where}) did not load: "
            f"{kb.library_status_name(status.status)}: {reason}")


def _library_kinds(kinds: int | Iterable[int]) -> list[int]:
    """Unique kipy LibraryType values, in order, from one value or several."""
    try:
        values = [kinds] if isinstance(kinds, int) else list(kinds)
    except TypeError:
        values = []
    if not values or any(isinstance(k, bool) or not isinstance(k, int) or k <= 0
                         for k in values):
        raise BoardApiError("kinds must be kipy LibraryType values (kb.LT_FOOTPRINT, "
                            f"kb.LT_SYMBOL), got {kinds!r}")
    return list(dict.fromkeys(values))


def _version_string(kicad) -> str:
    """The connected KiCad's version: the FIRST request on a new connection
    (kipy dials lazily, so an absent socket surfaces here)."""
    try:
        version = kicad.get_version()
    except kb.KIPY_EXCEPTIONS as exc:
        raise BoardApiError(f"KiCad answered but GetVersion failed: {exc}") from exc
    return str(getattr(version, "full_version", "") or version)


def _probe_gui(kicad) -> str | None:
    """Version string of a reachable KiCad GUI, or None (connection closed)
    when nothing answers on its socket."""
    try:
        return _version_string(kicad)
    except BoardApiError:
        kicad.close()
        return None


class BoardSession:
    """A KiCad connection that can hand out the kipy Board for a path, plus
    the footprint-library state of the board it serves (see LIBRARIES)."""

    def __init__(self, kicad, mode: str, install: env.KiCadInstall | None = None,
                 kicad_version: str | None = None) -> None:
        self.kicad = kicad
        self.mode = mode
        self.install = install
        self.kicad_version = kicad_version or _version_string(kicad)
        self.project_dir: Path | None = None             # the bound board's directory
        self.library_errors: list[str] = []
        self.global_footprint_libraries: int | None = None   # None = not loaded / GUI
        self._footprint_libraries: dict[str, Any] = {}
        self._loaded_kinds: frozenset[int] = frozenset()
        self._bound: str | None = None
        self._closed = False

    # --- construction -------------------------------------------------------
    @classmethod
    def headless(cls, pcb_path: str | os.PathLike, install: env.KiCadInstall | None = None,
                 timeout_ms: int = HEADLESS_TIMEOUT_MS) -> BoardSession:
        """Launch a private `kicad-cli api-server` (newest KiCad 11 found)
        with ``pcb_path`` preloaded; ``board()`` is usable at once."""
        kb.require(BoardApiError)
        install = install or env.pick_api()
        if install is None:
            raise BoardApiError(no_api_reason())
        try:
            kicad = kb.KiCad(headless=True, kicad_cli_path=str(install.kicad_cli),
                             file_path=str(pcb_path), timeout_ms=timeout_ms)
        except kb.KIPY_EXCEPTIONS as exc:
            raise BoardApiError(
                f"could not start `{install.kicad_cli} api-server` for {pcb_path}: {exc}") from exc
        return cls(kicad, "headless", install)

    @classmethod
    def gui(cls, timeout_ms: int = GUI_TIMEOUT_MS) -> BoardSession | None:
        """Connect to a running KiCad GUI on the default IPC socket; None when
        nothing answers. VERIFIED with a GetVersion round trip (a refused
        connection returns in milliseconds, a silent socket after ``timeout_ms``)."""
        kb.require(BoardApiError)
        try:
            kicad = kb.KiCad(timeout_ms=timeout_ms)
        except kb.KIPY_EXCEPTIONS:
            return None
        version = _probe_gui(kicad)
        if version is None:
            return None
        return cls(kicad, "gui", env.pick_api(), kicad_version=version)

    # --- documents ----------------------------------------------------------
    def open_boards(self) -> list[str]:
        return open_board_paths(self.kicad)

    def board(self, path: str | os.PathLike) -> kb.Board:
        """The kipy Board bound to ``path``: the preloaded board of a headless
        server (which holds exactly one, so a single open board is it), or the
        matching PCB-editor document of a GUI. Binding another board than
        before (a new open/create) drops the library state."""
        doc = self._document(path)
        self._bind(path)
        return kb.Board(self.kicad._client, doc)  # noqa: SLF001 (mirrors kipy.get_board)

    def _document(self, path: str | os.PathLike) -> Any:
        docs = open_boards(self.kicad)
        wanted = norm_path(path)
        for doc in docs:
            if norm_path(doc_pcb_path(doc)) == wanted:
                return doc
        if self.mode == "headless" and len(docs) == 1:
            return docs[0]
        open_now = [doc_pcb_path(d) for d in docs] or ["none"]
        if self.mode == "headless":
            raise BoardApiError(
                f"the headless KiCad did not load {path} (open: {open_now}); check that it is "
                "a readable .kicad_pcb this KiCad version can open")
        raise BoardApiError(
            f"{path} is not open in the running KiCad PCB editor (open: {open_now}); open it "
            "there or use mode='headless'")

    def _bind(self, path: str | os.PathLike) -> None:
        """Serve the board at ``path``. Another board means another project
        whose fp-lib-table KiCad read when it opened that board: every library
        cache is dropped, so the next use loads again."""
        key = norm_path(path)
        if key == self._bound:
            return
        self._bound = key
        self.project_dir = Path(os.path.abspath(str(path))).parent
        self.library_errors = []
        self.global_footprint_libraries = None
        self._footprint_libraries = {}
        self._loaded_kinds = frozenset()

    # --- libraries ----------------------------------------------------------
    @property
    def project_table(self) -> Path | None:
        """``<project>/fp-lib-table`` of the bound board (None before one)."""
        return None if self.project_dir is None else self.project_dir / FP_TABLE

    @property
    def global_footprint_tables_missing(self) -> bool:
        """True once a load showed KiCad has zero global footprint libraries."""
        return self.global_footprint_libraries == 0

    def load_libraries(self, kinds: int | Iterable[int] = (kb.LT_FOOTPRINT,),
                       timeout_s: float = LIBRARY_TIMEOUT_S) -> None:
        """Load the library tables of ``kinds`` (kipy LibraryType values,
        footprints by default) once per bound board: a headless KiCad 11 has
        none loaded and cannot place from a library before this. A no-op for a
        GUI session (the GUI preloads). A failed load raises BoardApiError
        naming the project fp-lib-table and is not cached."""
        wanted = _library_kinds(kinds)
        missing = [kind for kind in wanted if kind not in self._loaded_kinds]
        if self.mode != "headless" or not missing:
            return
        what = " and ".join(_KIND_NAMES.get(kind, f"type-{kind}") for kind in missing)
        try:
            self.kicad.load_all_libraries(missing)
            statuses = self.kicad.wait_for_libraries(types=missing, timeout_s=timeout_s)
        except kb.KIPY_EXCEPTIONS as exc:
            raise BoardApiError(f"loading KiCad {what} libraries failed: {exc}; "
                                f"{self.footprint_library_hint()}") from exc
        self._record(list(statuses), missing)
        self._loaded_kinds = self._loaded_kinds | frozenset(missing)

    def _record(self, statuses: list, kinds: list[int]) -> None:
        failed = [f"{s.nickname}: {s.error_message}" for s in statuses
                  if s.status != kb.LLS_LOADED and s.error_message]
        self.library_errors = [*self.library_errors, *failed]
        if kb.LT_FOOTPRINT in kinds:
            self._footprint_libraries = footprint_statuses(statuses)
            self.global_footprint_libraries = sum(
                1 for s in statuses if s.type == kb.LT_FOOTPRINT and s.scope == kb.LTS_GLOBAL)

    def footprint_library_hint(self, nickname: str = "") -> str:
        """What the last load said about ``nickname`` (when given and known),
        then where the project's footprint libraries must be."""
        loaded = kb.LT_FOOTPRINT in self._loaded_kinds
        state = (footprint_library_state(nickname, self._footprint_libraries, loaded)
                 if nickname else "")
        hint = footprint_library_hint(self.project_dir, self.global_footprint_tables_missing)
        return f"{state}; {hint}" if state else hint

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

    def __enter__(self) -> BoardSession:
        return self

    def __exit__(self, *_exc) -> None:
        self.close()


def connect(mode: str, path: str) -> BoardSession:
    """A session able to serve ``path`` in the requested mode (see module doc)."""
    if mode not in MODES:
        raise BoardApiError(f"mode must be one of {MODES}, got {mode!r}")
    if mode == "headless":
        return BoardSession.headless(path)
    gui = BoardSession.gui()
    docs = gui.open_boards() if gui is not None else None
    try:
        chosen = select_mode(mode, path, docs)
    except BoardApiError:
        if gui is not None:
            gui.close()
        raise
    if chosen == "gui":
        return gui                                   # type: ignore[return-value]
    if gui is not None:
        gui.close()
    return BoardSession.headless(path)
