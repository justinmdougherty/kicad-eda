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
"""
from __future__ import annotations

import os
from pathlib import Path

import kicad_env as env
from backends import kipy_bridge as kb
from backends.errors import SchematicApiError

MODES = ("auto", "gui", "headless")
HEADLESS_TIMEOUT_MS = 30_000
GUI_TIMEOUT_MS = 2_000
LIBRARY_TIMEOUT_S = 120.0
SCH_SUFFIX = ".kicad_sch"
LIB_TABLE_HINT = ("KiCad 11 headless finds no libraries unless its config dir (on Windows "
                  "%APPDATA%\\kicad\\10.99) holds sym-lib-table and fp-lib-table; copy them "
                  "from <install>/share/kicad/template")


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
    """A KiCad connection plus the state the schematic backend needs: whether
    libraries are loaded and which symbols the power library offers."""

    def __init__(self, kicad, mode: str, install: env.KiCadInstall | None = None,
                 kicad_version: str | None = None) -> None:
        self.kicad = kicad
        self.mode = mode
        self.install = install
        self.kicad_version = kicad_version or _version_string(kicad)
        self.library_errors: list[str] = []
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
        self.close_all_documents()
        try:
            self.kicad.open_document(str(path), kb.DocumentType.DOCTYPE_SCHEMATIC)
        except kb.KIPY_EXCEPTIONS as exc:
            raise SchematicApiError(f"KiCad could not open {path}: {exc}") from exc

    def create_document(self, path: str) -> None:
        """A new in-memory schematic (+ project) at path; nothing is written
        until save(). KiCad refuses while another unsaved document is open."""
        self._require_headless("create a document")
        self.close_all_documents()
        try:
            self.kicad.create_document(str(path), kb.DocumentType.DOCTYPE_SCHEMATIC)
        except kb.KIPY_EXCEPTIONS as exc:
            raise SchematicApiError(f"KiCad could not create {path}: {exc}") from exc

    def schematic_for(self, path: str):
        """The kipy Schematic bound to the open document at ``path``."""
        docs = open_schematics(self.kicad)
        wanted = norm_path(path)
        for doc in docs:
            if norm_path(doc_sch_path(doc)) == wanted:
                doc.ClearField("sheet_path")         # all sheets, like KiCad.get_schematic
                return kb.Schematic(self.kicad._client, doc)  # noqa: SLF001 (mirrors kipy)
        open_now = [doc_sch_path(d) for d in docs] or ["none"]
        raise SchematicApiError(f"{path} is not open in KiCad (open: {open_now})")

    # --- libraries ----------------------------------------------------------
    def load_libraries(self, timeout_s: float = LIBRARY_TIMEOUT_S) -> None:
        """Load every symbol library once (headless only; the GUI preloads)."""
        if self._libraries_loaded:
            return
        try:
            self.kicad.load_all_libraries()
            statuses = self.kicad.wait_for_libraries(timeout_s=timeout_s)
        except kb.KIPY_EXCEPTIONS as exc:
            raise SchematicApiError(
                f"loading KiCad libraries failed: {exc}. {LIB_TABLE_HINT}") from exc
        self.library_errors = [
            f"{s.nickname}: {s.error_message}" for s in statuses
            if s.status != kb.LibraryLoadStatus.LLS_LOADED and s.error_message]
        self._libraries_loaded = True

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
