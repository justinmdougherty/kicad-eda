"""The board-API session registry behind board_route_tools, and its policies.

One BoardApi per board path (key: kicad_ipc.norm_path), opened ON DEMAND by
every board tool through backends.pcb_api.BoardApi: a headless KiCad 11
``kicad-cli api-server`` with the file preloaded, or a running KiCad GUI whose
PCB editor has it open (the mode is resolved before anything else, so a board
the GUI holds is served by the GUI). A headless server holds ONE board:
opening another releases an idle (saved) headless board, named in
``released_boards``, and refuses while one has unsaved changes
(``blocking_board`` names it). _with_board turns a BoardApiError /
ValueError, and any other exception inside a tool, into ``{ok: false,
reason}``: the tools never raise. Every open board is closed at interpreter
exit (atexit), so a normal shutdown leaves no api-server behind.

ROADMAP D5 policies kept here: _save copies a pre-KiCad-11 file to X.k9.*
before the first KiCad 11 write (a KiCad 11 GUI session: at open), and
_record_round keeps each board's route_check scores (errors + unconnected) for
the stall verdict (toolchain.route_progress). A round that judged a stale disk
copy (unsaved copper) or whose DRC failed is not counted, but the verdict so
far is still reported, so a stall never disappears from a response.
release_for_rewrite frees a board before Freerouting / a rebuild rewrites it
and restarts its stall history.

Split out of board_route_tools.py (F5-04); board_route_tools keeps every MCP
tool and re-exports release_for_rewrite, close_all and status.
"""
from __future__ import annotations

import atexit
from typing import Any, Callable, Mapping

import kicad_ipc as ipc
import toolchain as tc
from backends.errors import BoardApiError
from backends.pcb_api import BoardApi
from session_release import Release, close_error_fields

BACKEND = "api"
_ERRORS = (BoardApiError, ValueError)
STALE_ROUND_NOTE = ("not counted toward stall detection: DRC judged the file on disk, not the "
                    "unsaved board")
FAILED_ROUND_NOTE = ("not counted toward stall detection: DRC produced no usable report (see "
                     "reason)")

_BOARDS: dict[str, BoardApi] = {}      # kicad_ipc.norm_path(pcb) -> BoardApi
_PROGRESS: dict[str, tuple[int, ...]] = {}   # same key -> score of each judged route_check


# ----------------------------------------------------------------------------
# registry: one BoardApi per board path, opened on demand
# ----------------------------------------------------------------------------
def _key(pcb: str) -> str:
    return ipc.norm_path(pcb)


def _note(value: Any, key: str = "note") -> dict:
    return {key: value} if value else {}


def _fail(api: BoardApi, reason: str) -> dict:
    return {"ok": False, "backend": BACKEND, "mode": api.mode, "path": api.path, "reason": reason}


def _registered(pcb: str) -> BoardApi | None:
    """The session that holds ``pcb``, or None."""
    return _BOARDS.get(_key(pcb))


def holds(pcb: str) -> bool:
    """True while a session of this process holds ``pcb`` (toolchain asks
    before it deletes a KiCad project lock)."""
    return _key(pcb) in _BOARDS


def _forget(key: str, discard: bool = False) -> str | None:
    """Drop a board and release its session (``discard`` reverts unsaved
    headless changes first); the close error text, if any."""
    api = _BOARDS.pop(key, None)
    if api is None:
        return None
    try:
        if discard:
            api.discard()
        else:
            api.close()
    except Exception as exc:  # a dead headless server must not wedge the registry
        return f"{type(exc).__name__}: {exc}"
    return None


def _take_snapshot(api: BoardApi, consequence: str) -> dict:
    """{"pre_k11_snapshot_taken": path} when the pre-11 file was copied now (D5)."""
    try:
        return _note(tc.snapshot_before_k11_write(api.path, api.kicad_version),
                     "pre_k11_snapshot_taken")
    except RuntimeError as exc:
        raise BoardApiError(f"{exc} ({consequence})") from exc


def _save(api: BoardApi) -> dict:
    """The one way the board tools save: snapshot first (ROADMAP D5), then write."""
    taken = _take_snapshot(api, "the board was not saved")
    api.save()
    return taken


def _blocked_by(api: BoardApi, pcb: str,
                why: str = "the api-server holds one board at a time") -> dict:
    """The refusal for ``pcb`` while the dirty board ``api`` is in the way."""
    return {"ok": False, "backend": BACKEND, "path": pcb, "blocking_board": api.path,
            "mode": api.mode,
            "reason": f"{api.path} is open in the {api.mode} KiCad session with unsaved changes; "
                      f"save_board or close_board it first ({why})"}


def _release_headless(pcb: str) -> Release:
    """The headless api-server holds ONE board at a time: drop idle (saved)
    headless boards before opening ``pcb`` there; refuse (naming the dirty
    board in ``blocking_board``) while one has unsaved changes."""
    released: list[str] = []
    errors: list[str] = []
    for key, api in list(_BOARDS.items()):
        if key == _key(pcb) or api.mode != "headless":
            continue
        if api.is_modified:
            return Release(_blocked_by(api, pcb), tuple(released), tuple(errors), kind="board")
        error = _forget(key)
        released.append(api.path)
        if error:
            errors.append(f"{api.path}: {error}")
    return Release(released=tuple(released), errors=tuple(errors), kind="board")


def _open(pcb: str, mode: str) -> tuple[BoardApi | None, dict]:
    """Open ``pcb`` into the registry: (api, notes) or (None, refusal). The
    mode is resolved FIRST, so the one-board-at-a-time sweep of the headless
    server runs only for a headless open and a board the running GUI holds
    is served there even while another headless board is dirty."""
    try:
        resolved = BoardApi.resolve_mode(pcb, mode)
    except Exception as exc:  # BoardApiError (bad mode) or a GUI probe failure
        return None, {"ok": False, "backend": BACKEND, "path": pcb,
                      "reason": f"{type(exc).__name__}: {exc}"}
    notes: dict = {}
    if resolved == "headless":
        release = _release_headless(pcb)
        if release.blocked:
            return None, release.blocked
        notes = release.fields()
    try:
        api = BoardApi.open(pcb, mode=resolved)
    except Exception as exc:  # BoardApiError (actionable) or a launch failure
        return None, {"ok": False, "backend": BACKEND, "path": pcb,
                      "reason": f"{type(exc).__name__}: {exc}", **notes}
    _BOARDS[_key(pcb)] = api
    if resolved == "gui":           # its user may save at any time: snapshot now (D5)
        try:
            notes = {**notes, **_take_snapshot(api, "the GUI session was not kept open")}
        except BoardApiError as exc:
            return None, {**_fail(api, str(exc)), **_note(_forget(_key(pcb)), "close_error")}
    return api, notes


def _board(pcb: str, mode: str = "auto") -> tuple[BoardApi | None, dict]:
    """The registered BoardApi for ``pcb``, opened on demand: (api, notes) or
    (None, refusal)."""
    api = _registered(pcb)
    if api is not None:
        return api, {}
    return _open(pcb, mode)


def _with_board(pcb: str, fn: Callable[[BoardApi], dict]) -> dict:
    """Every tool's path: open on demand, run ``fn(api)``, stamp the response;
    a BoardApiError / ValueError from either step is {ok: false, reason}, and
    so is any other exception (a kipy wrapper surprise on an unusual board):
    a tool answers, it never raises out of the MCP server."""
    api, notes = _board(pcb)
    if api is None:
        return notes
    try:
        result = fn(api)
    except _ERRORS as exc:
        return {**_fail(api, str(exc)), **notes}
    except Exception as exc:  # noqa: BLE001 - reported with its type, never swallowed
        return {**_fail(api, f"unexpected {type(exc).__name__}: {exc} (the board stays open; "
                             "report this with the board file)"), **notes}
    return {"ok": True, "backend": BACKEND, "mode": api.mode, "path": api.path, **result, **notes}


def _not_open(pcb: str) -> dict:
    return {"ok": False, "backend": BACKEND, "path": pcb,
            "reason": f"{pcb} is not open; open boards: "
                      f"{[api.path for api in _BOARDS.values()] or 'none'}"}


def _open_list() -> list[dict]:
    return [{"path": api.path, "mode": api.mode, "unsaved_changes": api.is_modified,
             **tc.snapshot_field(api.path)} for api in _BOARDS.values()]


# ----------------------------------------------------------------------------
# lifecycle bodies of open_board / save_board / close_board
# ----------------------------------------------------------------------------
def _reopen(pcb: str, mode: str, opened: Callable[[BoardApi], dict]) -> dict:
    """open_board: refuse a dirty registered board, else drop the old session
    (its close error is reported) and open ``pcb`` again in ``mode``."""
    existing = _registered(pcb)
    if existing is not None and existing.is_modified:
        return _fail(existing, f"{existing.path} is already open with unsaved changes; "
                               "save_board or close_board it before re-opening")
    notes = close_error_fields(_forget(_key(pcb)))
    api, more = _open(pcb, mode)
    if api is None:
        return {**more, **notes}
    return {**_with_board(pcb, opened), **more, **notes}


def _save_open(pcb: str) -> dict:
    """save_board: write a registered board (snapshot first), else refuse."""
    if _registered(pcb) is None:
        return _not_open(pcb)
    return _with_board(pcb, lambda a: {**_save(a), "saved": True, "path": a.path,
                                       **tc.snapshot_field(a.path)})


def _close(pcb: str, save: bool) -> dict:
    """close_board: save a dirty board first (refused: it stays open), report
    what happens to unsaved copper, then release the session."""
    api = _registered(pcb)
    if api is None:
        return _not_open(pcb)
    res = {"ok": True, "backend": BACKEND, "mode": api.mode, "path": api.path, "saved": False,
           "discarded_changes": False}
    if save and api.is_modified:
        try:
            res.update(_save(api))
        except _ERRORS as exc:
            return _fail(api, f"save failed, board left open: {exc}")
        res["saved"] = True
    if api.is_modified and api.mode == "headless":
        res["discarded_changes"] = True
    elif api.is_modified:
        res["note"] = (f"unsaved copper stays in the KiCad editor ({api.mode} session): save "
                       "or undo it there")
    return {**res, "closed": True, **tc.snapshot_field(api.path),
            **_note(_forget(_key(pcb), discard=not save), "close_error")}


# ----------------------------------------------------------------------------
# stall history (ROADMAP D5)
# ----------------------------------------------------------------------------
def _verdict(key: str) -> dict:
    """The stall verdict over the stored rounds ({} before the first one)."""
    history = _PROGRESS.get(key, ())
    return tc.route_progress(history) if history else {}


def _record_round(api: BoardApi, drc: Mapping[str, Any]) -> dict:
    """Score this round (errors + unconnected) into the board's history and
    judge it (tc.route_progress). A DRC that failed ('reason') or judged a
    stale disk copy (unsaved copper) is not counted: the verdict so far comes
    back with a progress_note, so a stall stays visible."""
    key = _key(api.path)
    if "reason" in drc:
        return {**_verdict(key), "progress_note": FAILED_ROUND_NOTE}
    if api.is_modified:
        return {**_verdict(key), "progress_note": STALE_ROUND_NOTE}
    _PROGRESS[key] = _PROGRESS.get(key, ()) + (int(drc["errors"]) + int(drc["unconnected"]),)
    return tc.route_progress(_PROGRESS[key])


# ----------------------------------------------------------------------------
# entry points other modules call (re-exported by board_route_tools)
# ----------------------------------------------------------------------------
def status() -> dict:
    """server_status's board-API keys: board_api (a headless KiCad 11 board
    session can run here), board_api_reason (why not), boards_open."""
    reason = BoardApi.unavailable_reason()
    return {"board_api": reason is None, "board_api_reason": reason, "boards_open": _open_list()}


def release_for_rewrite(pcb: str) -> dict:
    """Before another tool rewrites `pcb` (a session holding it could save its
    OLD board over the new file): close an idle session ({released_boards}),
    refuse a dirty one ({ok: false, blocking_board}); reset stall history."""
    key = _key(pcb)
    api = _BOARDS.get(key)
    if api is not None and api.is_modified:
        return _blocked_by(api, pcb, "it would save its old copy over the rewritten file")
    _PROGRESS.pop(key, None)
    if api is None:
        return {}
    error = _forget(key)
    return Release(released=(api.path,), errors=(f"{api.path}: {error}",) if error else (),
                   kind="board").fields()


def close_all() -> list[str]:
    """Release every open board without saving (registered with atexit: a
    normal interpreter exit must not leave a kicad-cli api-server running);
    the close errors, if any."""
    errors: list[str] = []
    for key in list(_BOARDS):
        error = _forget(key)
        if error:
            errors.append(error)
    return errors


atexit.register(close_all)
