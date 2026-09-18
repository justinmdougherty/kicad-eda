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
"""
from __future__ import annotations

import os
from pathlib import Path

import kicad_env as env
from backends import kipy_bridge as kb
from backends.errors import BoardApiError
from backends.sch_session import NO_HANDLER, norm_path

MODES = ("auto", "gui", "headless")
HEADLESS_TIMEOUT_MS = 60_000        # zone refills on a dense board take a while
GUI_TIMEOUT_MS = 2_000
PCB_SUFFIX = ".kicad_pcb"
NO_API_HINT = ("no KiCad with `kicad-cli api-server` found (needs a KiCad 11 nightly, "
               "10.99+)")

__all__ = ["MODES", "BoardSession", "connect", "select_mode", "resolve_auto", "doc_pcb_path",
           "open_boards", "open_board_paths", "no_api_reason", "norm_path"]


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
    """A KiCad connection that can hand out the kipy Board for a path."""

    def __init__(self, kicad, mode: str, install: env.KiCadInstall | None = None,
                 kicad_version: str | None = None) -> None:
        self.kicad = kicad
        self.mode = mode
        self.install = install
        self.kicad_version = kicad_version or _version_string(kicad)
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
        matching PCB-editor document of a GUI."""
        docs = open_boards(self.kicad)
        wanted = norm_path(path)
        for doc in docs:
            if norm_path(doc_pcb_path(doc)) == wanted:
                return kb.Board(self.kicad._client, doc)  # noqa: SLF001 (mirrors kipy.get_board)
        if self.mode == "headless" and len(docs) == 1:
            return kb.Board(self.kicad._client, docs[0])  # noqa: SLF001
        open_now = [doc_pcb_path(d) for d in docs] or ["none"]
        if self.mode == "headless":
            raise BoardApiError(
                f"the headless KiCad did not load {path} (open: {open_now}); check that it is "
                "a readable .kicad_pcb this KiCad version can open")
        raise BoardApiError(
            f"{path} is not open in the running KiCad PCB editor (open: {open_now}); open it "
            "there or use mode='headless'")

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
