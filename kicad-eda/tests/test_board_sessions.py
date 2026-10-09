"""board_sessions — the board-API session registry and its ROADMAP D5 policies.

The registry moved out of board_route_tools (F5-04); the MCP tools still
drive it, so these tests call them over the fake BoardApi of
tests/fixtures/fake_board_api.py (no KiCad needed). Covers the pre-KiCad-11
snapshot hook (first KiCad 11 save, KiCad 11 GUI open, a failed copy refuses
the save), route_check's stall detection (rounds, best score, the stall
verdict on counted AND uncounted rounds, a DRC that wrote nothing is never a
round), the history surviving a reopen and restarting after a rewrite, and the
compatibility names board_route_tools keeps for the registry.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import board_route_tools as brt
import board_sessions as bs
import toolchain as tc
from fixtures import fake_board_api as fba
from fixtures.fake_board_api import KICAD9_HEADER, KICAD11_HEADER, PWR_PATH, SIG_PATH


@pytest.fixture(autouse=True)
def fake_api(monkeypatch) -> type[fba.FakeBoardApi]:
    """The board tools run over FakeBoardApi with an empty registry."""
    return fba.install(monkeypatch)


@pytest.fixture
def pcb(tmp_path) -> str:
    return fba.board_file(tmp_path)


@pytest.fixture
def kicad11(monkeypatch, tmp_path):
    """The only install: a 10.99 nightly (kicad-cli 11 reads the KiCad 11 file)."""
    return fba.kicad11(monkeypatch, tmp_path)


def _done():
    return fba.done()


def _drc_writer(monkeypatch, report: dict) -> list[list[str]]:
    return fba.drc_writer(monkeypatch, report)


def _api() -> fba.FakeBoardApi:
    return fba.last()


# ============================================================================
# the pre-KiCad-11 snapshot (ROADMAP D5)
# ============================================================================
@pytest.fixture
def k9_pcb(tmp_path) -> str:
    """A board built by KiCad 9's pcbnew, with its project: what agent routing gets."""
    path = tmp_path / "board.kicad_pcb"
    path.write_text(KICAD9_HEADER, encoding="utf-8")
    (tmp_path / "board.kicad_pro").write_text('{"net_settings": {"classes": []}}\n',
                                              encoding="utf-8")
    return str(path)


@pytest.fixture
def k9_gui(tmp_path) -> str:
    """A KiCad 9-format board the (fake) running GUI has open."""
    path = tmp_path / "gui.kicad_pcb"
    path.write_text(KICAD9_HEADER, encoding="utf-8")
    return str(path)


def _taken(monkeypatch) -> list[str]:
    """Every toolchain.take_snapshot call (the real copy still runs)."""
    taken: list[str] = []
    real = tc.take_snapshot
    monkeypatch.setattr(tc, "take_snapshot", lambda path: taken.append(path) or real(path))
    return taken


def _clean_drc(monkeypatch) -> None:
    _drc_writer(monkeypatch, {"violations": [], "unconnected_items": []})


def test_first_headless_save_snapshots_once_before_writing(k9_pcb, kicad11, monkeypatch):
    taken = _taken(monkeypatch)
    _clean_drc(monkeypatch)
    snap = tc.pre_k11_snapshot_path(k9_pcb)
    opened = brt.open_board(k9_pcb)                         # headless: opening writes nothing
    assert opened["ok"] and "pre_k11_snapshot" not in opened and taken == []
    res = brt.route_check(k9_pcb)                           # the refill dirties it: saved
    assert res["saved"] is True and res["pre_k11_snapshot_taken"] == snap
    assert res["pre_k11_snapshot"] == snap and taken == [k9_pcb]
    assert _api().snapshot_at_save == [True]                # the copy preceded the save
    assert Path(snap).read_text(encoding="utf-8") == KICAD9_HEADER
    assert Path(k9_pcb).read_text(encoding="utf-8") == KICAD11_HEADER
    assert Path(tc.pre_k11_snapshot_path(k9_pcb)).with_suffix(".kicad_pro").is_file()
    brt.route_net(k9_pcb, "SIG", SIG_PATH)
    saved = brt.save_board(k9_pcb)
    assert saved["ok"] and saved["pre_k11_snapshot"] == snap
    assert "pre_k11_snapshot_taken" not in saved
    brt.route_net(k9_pcb, "PWR", PWR_PATH)
    closed = brt.close_board(k9_pcb)
    assert closed["saved"] is True and closed["pre_k11_snapshot"] == snap
    assert "pre_k11_snapshot_taken" not in closed and taken == [k9_pcb]


def test_save_board_and_close_board_snapshot_an_untouched_kicad9_file(k9_pcb, tmp_path,
                                                                      monkeypatch):
    taken = _taken(monkeypatch)
    brt.open_board(k9_pcb)
    saved = brt.save_board(k9_pcb)                          # save_board always writes
    assert saved["pre_k11_snapshot_taken"] == tc.pre_k11_snapshot_path(k9_pcb)
    other = tmp_path / "other.kicad_pcb"
    other.write_text(KICAD9_HEADER, encoding="utf-8")
    brt.route_net(str(other), "SIG", SIG_PATH)              # releases the idle k9_pcb session
    closed = brt.close_board(str(other))
    assert closed["saved"] is True
    assert closed["pre_k11_snapshot_taken"] == tc.pre_k11_snapshot_path(str(other))
    assert taken == [k9_pcb, str(other)]


def test_kicad11_gui_open_snapshots_and_later_saves_do_not(k9_gui, monkeypatch):
    taken = _taken(monkeypatch)
    opened = brt.open_board(k9_gui)                         # the user may Ctrl+S at any time
    snap = tc.pre_k11_snapshot_path(k9_gui)
    assert opened["ok"] and opened["mode"] == "gui"
    assert opened["pre_k11_snapshot_taken"] == snap and opened["pre_k11_snapshot"] == snap
    assert taken == [k9_gui]
    brt.route_net(k9_gui, "SIG", SIG_PATH)
    assert brt.save_board(k9_gui)["pre_k11_snapshot"] == snap
    brt.close_board(k9_gui)
    again = brt.open_board(k9_gui)                          # the file is KiCad 11 now
    assert again["ok"] and "pre_k11_snapshot_taken" not in again and taken == [k9_gui]


def test_kicad9_sessions_never_snapshot(k9_gui, k9_pcb, kicad11, monkeypatch):
    fba.FakeBoardApi.version = "9.0.4"
    taken = _taken(monkeypatch)
    _clean_drc(monkeypatch)
    assert brt.open_board(k9_gui)["ok"]
    brt.route_net(k9_gui, "SIG", SIG_PATH)
    assert "pre_k11_snapshot" not in brt.save_board(k9_gui)
    assert brt.route_check(k9_pcb)["saved"] is True
    assert taken == [] and Path(k9_pcb).read_text(encoding="utf-8") == KICAD9_HEADER


def test_a_failed_snapshot_refuses_the_save(k9_pcb, kicad11, monkeypatch):
    def full(path: str) -> dict:
        raise OSError("No space left on device")

    monkeypatch.setattr(tc, "take_snapshot", full)
    monkeypatch.setattr(tc, "run", lambda args, **kw: pytest.fail("DRC ran on an unsaved board"))
    res = brt.route_check(k9_pcb)
    assert res["ok"] is False and "pre-KiCad-11 snapshot" in res["reason"]
    assert "No space left" in res["reason"] and "not saved" in res["reason"]
    assert ("save",) not in _api().calls and _api().is_modified is True
    closed = brt.close_board(k9_pcb)
    assert closed["ok"] is False and "board left open" in closed["reason"]
    assert Path(k9_pcb).read_text(encoding="utf-8") == KICAD9_HEADER


def test_a_failed_snapshot_at_gui_open_refuses_the_session(k9_gui, monkeypatch, fake_api):
    def denied(path: str) -> dict:
        raise PermissionError("access denied")

    monkeypatch.setattr(tc, "take_snapshot", denied)
    res = brt.board_pads(k9_gui)
    assert res["ok"] is False and "pre-KiCad-11 snapshot" in res["reason"]
    assert "access denied" in res["reason"] and res["mode"] == "gui"
    assert fake_api.opened[0].closed and brt.list_open_boards()["boards"] == []


def test_the_snapshot_path_is_reported_by_the_read_and_list_tools(k9_pcb, monkeypatch):
    brt.open_board(k9_pcb)
    assert "pre_k11_snapshot" not in brt.board_overview(k9_pcb)
    brt.save_board(k9_pcb)
    snap = tc.pre_k11_snapshot_path(k9_pcb)
    assert brt.board_overview(k9_pcb)["pre_k11_snapshot"] == snap
    assert brt.list_open_boards()["boards"] == [{"path": k9_pcb, "mode": "headless",
                                                 "unsaved_changes": False,
                                                 "pre_k11_snapshot": snap}]


# ============================================================================
# route_check stall detection (ROADMAP D5)
# ============================================================================
def _round(monkeypatch, pcb: str, errors: int, unconnected: int, **kw) -> dict:
    fba.drc_round(monkeypatch, errors, unconnected)
    return brt.route_check(pcb, **kw)


def _no_report(monkeypatch) -> None:
    """The next kicad-cli DRC exits 0 and writes nothing."""
    monkeypatch.setattr(tc, "run", lambda args, **kw: _done())


def test_route_check_tracks_progress_and_flags_a_stall(pcb, kicad11, monkeypatch):
    first = _round(monkeypatch, pcb, 3, 2)
    assert first["progress"] == {"rounds": 1, "score": 5, "best": 5,
                                 "rounds_without_improvement": 0}
    assert first["stalled"] is False and "stall_note" not in first
    assert _round(monkeypatch, pcb, 2, 2)["progress"]["best"] == 4      # improved
    flat = [_round(monkeypatch, pcb, 4, 1), _round(monkeypatch, pcb, 2, 2),
            _round(monkeypatch, pcb, 3, 3)]                              # 5, 4, 6: none below 4
    assert [r["progress"]["rounds_without_improvement"] for r in flat] == [1, 2, 3]
    assert [r["stalled"] for r in flat] == [False, False, True]
    note = flat[-1]["stall_note"]
    assert "3 route_check rounds" in note and "autoroute_board" in note
    assert "from_snapshot=true" in note and "pack_scale" in note
    assert flat[-1]["progress"] == {"rounds": 5, "score": 6, "best": 4,
                                    "rounds_without_improvement": 3}
    better = _round(monkeypatch, pcb, 1, 0)
    assert better["stalled"] is False and better["progress"]["best"] == 1
    assert better["progress"]["rounds_without_improvement"] == 0


def test_a_clean_board_is_never_stalled(pcb, kicad11, monkeypatch):
    rounds = [_round(monkeypatch, pcb, 0, 0) for _ in range(5)]
    assert rounds[-1]["progress"]["rounds_without_improvement"] == 4
    assert rounds[-1]["ok"] is True and not any(r["stalled"] for r in rounds)


def test_rounds_that_judged_a_stale_file_do_not_count(pcb, kicad11, monkeypatch):
    brt.route_net(pcb, "SIG", SIG_PATH)                      # unsaved copper, no history yet
    first = _round(monkeypatch, pcb, 2, 0, refill=False, save=False)
    assert "progress" not in first and "stalled" not in first
    assert "file on disk" in first["progress_note"]
    assert _round(monkeypatch, pcb, 2, 0)["progress"]["rounds"] == 1      # saved: counted
    brt.route_net(pcb, "PWR", PWR_PATH)                      # unsaved copper again
    stale = _round(monkeypatch, pcb, 1, 0, refill=False, save=False)
    assert "file on disk" in stale["progress_note"]
    assert stale["progress"] == {"rounds": 1, "score": 2, "best": 2,
                                 "rounds_without_improvement": 0}    # the verdict so far
    assert stale["stalled"] is False
    assert _round(monkeypatch, pcb, 2, 0)["progress"]["rounds"] == 2


def test_a_drc_that_writes_nothing_is_no_round_even_beside_an_old_report(pcb, kicad11,
                                                                         monkeypatch):
    assert _round(monkeypatch, pcb, 2, 0)["progress"]["rounds"] == 1
    report = Path(pcb).with_name("board_drc.json")           # the last round's report stays
    assert report.is_file()
    _no_report(monkeypatch)
    failed = brt.route_check(pcb)
    assert failed["ok"] is False and "wrote no DRC report" in failed["reason"]
    assert "errors" not in failed and not report.exists()    # never re-judged as current
    assert failed["progress"]["rounds"] == 1 and "no usable report" in failed["progress_note"]
    assert _round(monkeypatch, pcb, 2, 0)["progress"]["rounds"] == 2


def test_an_uncounted_round_still_reports_the_stall(pcb, kicad11, monkeypatch):
    for _ in range(4):
        last = _round(monkeypatch, pcb, 3, 1)
    assert last["stalled"] is True
    brt.route_net(pcb, "SIG", SIG_PATH)                      # unsaved copper: not judged
    unsaved = _round(monkeypatch, pcb, 0, 0, refill=False, save=False)
    assert unsaved["stalled"] is True and "autoroute_board" in unsaved["stall_note"]
    assert unsaved["progress"]["rounds"] == 4 and "file on disk" in unsaved["progress_note"]
    _no_report(monkeypatch)
    failed = brt.route_check(pcb)                            # DRC failed: not judged either
    assert failed["ok"] is False and failed["saved"] is True
    assert failed["stalled"] is True and "from_snapshot=true" in failed["stall_note"]
    assert failed["progress"] == {"rounds": 4, "score": 4, "best": 4,
                                  "rounds_without_improvement": 3}
    assert "no usable report" in failed["progress_note"]
    assert bs._PROGRESS[bs._key(pcb)] == (4, 4, 4, 4)        # nothing was appended


def test_progress_survives_a_reopen_and_restarts_after_a_rewrite(pcb, tmp_path, kicad11,
                                                                 monkeypatch):
    for _ in range(4):
        last = _round(monkeypatch, pcb, 1, 1)
    assert last["stalled"] is True
    brt.board_pads(str(tmp_path / "other.kicad_pcb"))        # releases the idle board
    assert _round(monkeypatch, pcb, 1, 1)["progress"]["rounds"] == 5
    assert brt.release_for_rewrite(pcb)["released_boards"] == [pcb]  # Freerouting / a rebuild
    fresh = _round(monkeypatch, pcb, 1, 1)
    assert fresh["progress"]["rounds"] == 1 and fresh["stalled"] is False


# ============================================================================
# the registry module and the names board_route_tools keeps for it
# ============================================================================
def test_board_route_tools_reexports_the_session_entry_points():
    assert brt.release_for_rewrite is bs.release_for_rewrite
    assert brt.close_all is bs.close_all and brt.status is bs.status
    assert brt._key is bs._key


def test_the_old_registry_names_read_and_rebind_board_sessions(pcb):
    """Tests written before the split monkeypatch board_route_tools._BOARDS /
    _PROGRESS: both names are board_sessions' registry, read and rebound."""
    assert brt._BOARDS is bs._BOARDS and brt._PROGRESS is bs._PROGRESS
    original = bs._BOARDS
    with pytest.MonkeyPatch.context() as mp:
        boards: dict = {}
        mp.setattr(brt, "_BOARDS", boards)
        mp.setattr(brt, "_PROGRESS", {bs._key(pcb): (5, 5)})
        assert bs._BOARDS is boards and bs._PROGRESS == {bs._key(pcb): (5, 5)}
        brt.open_board(pcb)
        assert list(boards) == [bs._key(pcb)] and brt._BOARDS is boards
    assert bs._BOARDS is original and brt._BOARDS is original
    assert "_BOARDS" not in vars(brt) and "_PROGRESS" not in vars(brt)


def test_holds_names_only_a_registered_board(pcb, tmp_path):
    assert bs.holds(pcb) is False
    brt.open_board(pcb)
    assert bs.holds(pcb) is True and bs.holds(str(Path(pcb).parent / "." / "board.kicad_pcb"))
    assert bs.holds(str(tmp_path / "other.kicad_pcb")) is False
    brt.close_board(pcb)
    assert bs.holds(pcb) is False


# ============================================================================
# release_for_rewrite (Freerouting, a rebuild or stitching rewrites the file)
# ============================================================================
class StubSession:
    """The registry face of a BoardApi: path, mode, dirty flag, close."""

    def __init__(self, path: str, modified: bool = False) -> None:
        self.path, self.mode, self.is_modified = path, "headless", modified
        self.closed = False

    def close(self) -> None:
        self.closed = True

    def discard(self) -> None:
        self.closed = True


def _hold(pcb: str, modified: bool = False) -> StubSession:
    stub = StubSession(pcb, modified)
    bs._BOARDS[bs._key(pcb)] = stub
    return stub


def test_release_for_rewrite_closes_idle_and_refuses_dirty_sessions(tmp_path) -> None:
    pcb = str(tmp_path / "x.kicad_pcb")
    assert brt.release_for_rewrite(pcb) == {}
    bs._PROGRESS[bs._key(pcb)] = (5, 5, 5)
    dirty = _hold(pcb, modified=True)
    refused = brt.release_for_rewrite(pcb)
    assert refused["ok"] is False and refused["blocking_board"] == pcb
    assert "unsaved changes" in refused["reason"]
    assert "save_board or close_board" in refused["reason"]
    assert not dirty.closed and bs._PROGRESS[bs._key(pcb)] == (5, 5, 5)
    dirty.is_modified = False
    released = brt.release_for_rewrite(pcb)
    assert released["released_boards"] == [pcb] and dirty.closed
    assert bs._BOARDS == {} and bs._PROGRESS == {}                       # stall history restarts


def test_release_for_rewrite_reports_a_close_error(tmp_path) -> None:
    pcb = str(tmp_path / "x.kicad_pcb")
    stub = _hold(pcb)

    def broken() -> None:
        raise RuntimeError("api-server did not stop")

    stub.close = broken
    res = brt.release_for_rewrite(pcb)
    assert res["released_boards"] == [pcb]
    assert res["released_board_close_errors"] == [f"{pcb}: RuntimeError: api-server did not stop"]
