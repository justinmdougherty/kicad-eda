"""Library footprints on the KiCad 11 board API backend (F2-07, ROADMAP D0/D1).

A headless ``kicad-cli api-server`` loads no library on its own: every
``PlaceFootprintFromLibrary`` answers "footprint ... not found" until
``LoadAllLibraries([LT_FOOTPRINT])`` ran (recorded below). Two tiers:

* unit (fake kipy KiCad + board, no KiCad): ``BoardSession.load_libraries`` is
  idempotent, cached per bound board, a no-op for GUI sessions and loud on
  failure; ``BoardApi.place_footprint`` loads first, places, sets reference and
  value, marks the board modified and names the lib_id and the project
  fp-lib-table in every library error;
* live (KiCad 11 nightly + a KiCad 9/10 pcbnew Python, skipped when absent): a
  SWIG-built board in a temp project whose fp-lib-table (written by
  ``LibraryResolver.materialize``) holds the authored Quectel_LG290P;
  ``place_footprint('P_parts:Quectel_LG290P', 'U9', ...)``, save, reopen ->
  79 pads and FPID 'P_parts:Quectel_LG290P'.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

import libtable
import toolchain as tc
from backends import kipy_bridge as kb
from backends import pcb_api as papi
from backends import pcb_session as sess
from backends.errors import BoardApiError
from backends.pcb_api import BoardApi

requires_kipy = pytest.mark.skipif(bool(kb.IMPORT_ERROR), reason=kb.IMPORT_ERROR or "")
requires_api = pytest.mark.skipif(not BoardApi.available(),
                                  reason=BoardApi.unavailable_reason() or "")

ROOT = Path(__file__).resolve().parents[1]
NICK = "P_parts"
LG290P = f"{NICK}:Quectel_LG290P"
LG290P_PADS = 79
STOCK_R = "Resistor_SMD:R_0603_1608Metric"
KICAD11_FORMAT = 20260901

# --- recorded from kicad-cli 10.99.0-4025-gf7ca620278 `api-server` (2026-10-05), a
# KiCad 9 SWIG-built board preloaded, P_parts a managed row of its project fp-lib-table:
# PlaceFootprintFromLibrary BEFORE LoadAllLibraries (P_parts reports LLS_INVALID then):
RECORDED_NOT_LOADED = "KiCad returned error: footprint 'P_parts:Quectel_LG290P' not found"
# after the load: a footprint the library lacks, a nickname no table has, an inner layer
RECORDED_NOT_FOUND = "KiCad returned error: footprint 'P_parts:Nope' not found"
RECORDED_NO_LIBRARY = "KiCad returned error: footprint 'Nolib:X' not found"
RECORDED_BAD_LAYER = "KiCad returned error: layer must be F_Cu or B_Cu"


# ============================================================================
# fakes
# ============================================================================
@dataclass(frozen=True)
class FakeStatus:
    nickname: str
    status: int
    error_message: str | None = None
    type: int = kb.LT_FOOTPRINT
    scope: int = kb.LTS_PROJECT


def _doc(path: Path) -> SimpleNamespace:
    """An open-board specifier as pcb_session.doc_pcb_path reads it."""
    return SimpleNamespace(board_filename=path.name,
                           project=SimpleNamespace(path=str(path.parent), name=path.stem))


def _statuses(project: int = kb.LLS_LOADED, error: str | None = None,
              global_rows: int = 2) -> list[FakeStatus]:
    stock = [FakeStatus(f"G{i}", kb.LLS_LOADED, scope=kb.LTS_GLOBAL) for i in range(global_rows)]
    return [*stock, FakeStatus("Resistor_SMD", kb.LLS_LOADED, scope=kb.LTS_GLOBAL),
            FakeStatus(NICK, project, error),
            FakeStatus("Device", kb.LLS_LOADED, type=kb.LT_SYMBOL, scope=kb.LTS_GLOBAL)]


class FakeKiCad:
    """The kipy KiCad calls BoardSession makes, recorded in ``calls``."""

    def __init__(self, paths: list[Path], statuses: list[FakeStatus] | None = None) -> None:
        self._client = None                     # kb.Board(kicad._client, doc) in board()
        self.calls: list[tuple] = []
        self.docs = [_doc(p) for p in paths]
        self.statuses = list(_statuses() if statuses is None else statuses)
        self.loaded: set[int] = set()
        self.fail_wait: BaseException | None = None

    def get_open_documents(self, _doc_type) -> list:
        return list(self.docs)

    def load_all_libraries(self, types=None) -> None:
        kinds = tuple(types) if types is not None else (kb.LT_SYMBOL, kb.LT_FOOTPRINT)
        self.calls.append(("load_all", kinds))
        self.loaded |= set(kinds)

    def wait_for_libraries(self, types=None, interval_ms=500, timeout_s=60.0, scope=kb.LTS_BOTH):
        kinds = tuple(types) if types is not None else None
        self.calls.append(("wait", kinds))
        if self.fail_wait is not None:
            raise self.fail_wait
        return [s for s in self.statuses if kinds is None or s.type in kinds]

    def close(self) -> None:
        self.calls.append(("close",))


def _footprint(lib_id: str, pads: int, position, degrees: float, layer: int,
               ref: str = "REF**") -> kb.FootprintInstance:
    """A real kipy FootprintInstance as PlaceFootprintFromLibrary returns one."""
    fp = kb.FootprintInstance()
    fp.id.value = f"id-{ref}-{lib_id}"
    nickname, _, name = lib_id.partition(":")
    fp.definition.id.library, fp.definition.id.name = nickname, name
    for number in range(1, pads + 1):
        pad = kb.Pad()
        pad.number = str(number)
        fp.definition.add_item(pad)
    fp.position = position
    fp.proto.orientation.value_degrees = degrees
    fp.layer = layer
    fp.reference_field.text.value = ref
    fp.value_field.text.value = name
    return fp


class FakeBoard:
    """place_footprint_from_library / update_items / get_footprints as KiCad
    answers them: nothing is found before the footprint libraries loaded."""

    def __init__(self, kicad: FakeKiCad, library: dict[str, int] | None = None,
                 existing: tuple[str, ...] = ()) -> None:
        self.kicad = kicad
        self.library = dict(library or {LG290P: LG290P_PADS, STOCK_R: 2})
        self.calls: list[tuple] = []
        self.footprints = [_footprint(STOCK_R, 2, kb.vec(0, 0), 0, kb.BoardLayer.BL_F_Cu, ref)
                           for ref in existing]
        self.fail_update: BaseException | None = None

    def place_footprint_from_library(self, lib_id, position, orientation=None, layer=None):
        layer = kb.BoardLayer.BL_F_Cu if layer is None else layer
        degrees = orientation.degrees if orientation is not None else 0.0
        self.calls.append(("place", lib_id, position.x, position.y, degrees, layer))
        if kb.LT_FOOTPRINT not in self.kicad.loaded or lib_id not in self.library:
            raise kb.ApiError(f"KiCad returned error: footprint '{lib_id}' not found")
        if layer not in (kb.BoardLayer.BL_F_Cu, kb.BoardLayer.BL_B_Cu):
            raise kb.ApiError(RECORDED_BAD_LAYER)
        placed = _footprint(lib_id, self.library[lib_id], position, degrees, layer)
        self.footprints = [*self.footprints, placed]
        return kb.FootprintInstance(proto=placed.proto)

    def update_items(self, updates):
        self.calls.append(("update", tuple(str(u.reference_field.text.value) for u in updates)))
        if self.fail_update is not None:
            raise self.fail_update
        by_id = {u.id.value: kb.FootprintInstance(proto=u.proto) for u in updates}
        self.footprints = [by_id.get(fp.id.value, fp) for fp in self.footprints]
        return list(by_id.values())

    def get_footprints(self):
        self.calls.append(("get_footprints",))
        return list(self.footprints)

    def get_item_bounding_box(self, item):
        self.calls.append(("bbox",))
        return SimpleNamespace(pos=kb.vec(item.position.x / 1e6 - 1, item.position.y / 1e6 - 1),
                               size=kb.vec(2, 2))


def _session(kicad: FakeKiCad, mode: str = "headless") -> sess.BoardSession:
    return sess.BoardSession(kicad, mode, install=None, kicad_version="10.99.0-fake")


def _loads(kicad: FakeKiCad) -> list[tuple]:
    return [c for c in kicad.calls if c[0] == "load_all"]


@pytest.fixture
def pcb(tmp_path: Path) -> Path:
    return tmp_path / "proj" / "board.kicad_pcb"


@pytest.fixture
def rig(pcb: Path, monkeypatch):
    """(BoardApi over a real BoardSession, FakeKiCad, FakeBoard) for one board."""
    def make(mode: str = "headless", existing: tuple[str, ...] = (),
             statuses: list[FakeStatus] | None = None):
        kicad = FakeKiCad([pcb], statuses)
        board = FakeBoard(kicad, existing=existing)
        if mode == "gui":
            kicad.loaded = {kb.LT_SYMBOL, kb.LT_FOOTPRINT}     # the GUI preloads its libraries
        monkeypatch.setattr(kb, "Board", lambda _client, _doc: board)
        return BoardApi(_session(kicad, mode), str(pcb)), kicad, board
    return make


# ============================================================================
# unit tier: BoardSession.load_libraries
# ============================================================================
@requires_kipy
def test_load_libraries_loads_the_footprint_tables_once(pcb):
    kicad = FakeKiCad([pcb])
    session = _session(kicad)
    session.board(pcb)
    session.load_libraries()
    session.load_libraries()
    session.load_libraries(kb.LT_FOOTPRINT)                    # a bare value works too
    assert _loads(kicad) == [("load_all", (kb.LT_FOOTPRINT,))]
    assert ("wait", (kb.LT_FOOTPRINT,)) in kicad.calls
    session.load_libraries((kb.LT_SYMBOL, kb.LT_FOOTPRINT))    # only the new kind loads
    assert _loads(kicad)[1:] == [("load_all", (kb.LT_SYMBOL,))]
    session.load_libraries((kb.LT_FOOTPRINT, kb.LT_SYMBOL))
    assert len(_loads(kicad)) == 2


@requires_kipy
def test_load_libraries_is_a_noop_for_a_gui_session(pcb):
    kicad = FakeKiCad([pcb])
    session = _session(kicad, mode="gui")
    session.board(pcb)
    session.load_libraries()
    session.load_libraries((kb.LT_SYMBOL, kb.LT_FOOTPRINT))
    assert kicad.calls == []
    with pytest.raises(BoardApiError, match="LibraryType"):     # arguments are still checked
        session.load_libraries(())


@requires_kipy
def test_library_cache_resets_when_the_session_binds_another_board(tmp_path):
    first, second = tmp_path / "a" / "a.kicad_pcb", tmp_path / "b" / "b.kicad_pcb"
    kicad = FakeKiCad([first])
    session = _session(kicad)
    session.board(first)
    assert session.project_dir == first.parent
    assert session.project_table == first.parent / "fp-lib-table"
    session.load_libraries()
    session.board(first)                                       # the same board: cache kept
    session.load_libraries()
    assert len(_loads(kicad)) == 1
    kicad.docs = [_doc(second)]                                # another board opened
    session.board(second)
    assert session.project_dir == second.parent and session.library_errors == []
    session.load_libraries()
    assert len(_loads(kicad)) == 2


@requires_kipy
def test_load_failure_names_the_project_table_and_is_not_cached(pcb):
    kicad = FakeKiCad([pcb])
    kicad.fail_wait = TimeoutError("Timed out waiting for libraries to finish loading")
    session = _session(kicad)
    session.board(pcb)
    with pytest.raises(BoardApiError) as err:
        session.load_libraries()
    text = str(err.value)
    assert "loading KiCad footprint libraries failed: Timed out" in text
    assert str(pcb.parent / "fp-lib-table") in text
    kicad.fail_wait = None
    session.load_libraries()                                   # retried, then cached
    session.load_libraries()
    assert len(_loads(kicad)) == 2


@requires_kipy
@pytest.mark.parametrize("kinds", [(), [], ["footprint"], (True,), (0,), "fp", None])
def test_load_libraries_refuses_bad_kinds(pcb, kinds):
    kicad = FakeKiCad([pcb])
    with pytest.raises(BoardApiError, match="LibraryType"):
        _session(kicad).load_libraries(kinds)
    assert kicad.calls == []


@requires_kipy
def test_load_records_statuses_and_counts_global_footprint_libraries(pcb):
    broken = "Expecting '(' in 'C:\\x\\libs\\P_parts.pretty\\Quectel_LG290P.kicad_mod', line 2"
    kicad = FakeKiCad([pcb], _statuses(project=kb.LLS_ERROR, error=broken, global_rows=0))
    session = _session(kicad)
    session.board(pcb)
    assert session.global_footprint_libraries is None          # unknown before a load
    session.load_libraries()
    assert session.library_errors == [f"{NICK}: {broken}"]
    assert session.global_footprint_libraries == 1             # Resistor_SMD only
    hint = session.footprint_library_hint(NICK)
    assert f"'{NICK}'" in hint and "LLS_ERROR" in hint and broken in hint
    assert str(pcb.parent / "fp-lib-table") in hint


@requires_kipy
def test_zero_global_footprint_libraries_lead_the_hint(pcb):
    kicad = FakeKiCad([pcb], [FakeStatus(NICK, kb.LLS_LOADED)])
    session = _session(kicad)
    session.board(pcb)
    session.load_libraries()
    assert session.global_footprint_libraries == 0 and session.global_footprint_tables_missing
    hint = session.footprint_library_hint("Resistor_SMD")
    assert sess.GLOBAL_FP_TABLES_MISSING in hint and "share/kicad/template" in hint
    assert "KiCad has no footprint library 'Resistor_SMD'" in hint


# ============================================================================
# unit tier: the pure library texts
# ============================================================================
def test_footprint_library_hint_names_the_project_table(tmp_path):
    hint = sess.footprint_library_hint(tmp_path)
    assert f"{tmp_path / 'fp-lib-table'} (absent)" in hint      # no table written yet
    assert "${KIPRJMOD}" in hint and "opened" in hint
    assert sess.GLOBAL_FP_TABLES_MISSING not in hint
    (tmp_path / "fp-lib-table").write_text("(fp_lib_table (version 7))\n", encoding="utf-8")
    assert "(absent)" not in sess.footprint_library_hint(tmp_path)
    assert "the project's fp-lib-table" in sess.footprint_library_hint(None)
    missing = sess.footprint_library_hint(tmp_path, global_tables_missing=True)
    assert missing.startswith(sess.GLOBAL_FP_TABLES_MISSING)
    assert str(tmp_path / "fp-lib-table") in missing


def test_footprint_library_state():
    by_nick = sess.footprint_statuses(_statuses(project=kb.LLS_ERROR, error="bad token"))
    assert sess.footprint_library_state(NICK, by_nick, loaded=False) == ""
    assert sess.footprint_library_state(NICK, by_nick, loaded=True) == (
        "footprint library 'P_parts' (project fp-lib-table) did not load: LLS_ERROR: bad token")
    assert sess.footprint_library_state("Resistor_SMD", by_nick, loaded=True) == (
        "footprint library 'Resistor_SMD' (global fp-lib-table) is loaded: check the "
        "footprint name")
    assert sess.footprint_library_state("Nolib", by_nick, loaded=True).startswith(
        "KiCad has no footprint library 'Nolib'")
    assert "Device" not in by_nick                              # symbol rows are left out


def test_footprint_statuses_let_a_project_row_shadow_a_global_one():
    rows = [FakeStatus(NICK, kb.LLS_LOADED), FakeStatus(NICK, kb.LLS_ERROR, scope=kb.LTS_GLOBAL)]
    assert sess.footprint_statuses(rows)[NICK].scope == kb.LTS_PROJECT
    assert sess.footprint_statuses(list(reversed(rows)))[NICK].scope == kb.LTS_PROJECT


# ============================================================================
# unit tier: BoardApi.place_footprint
# ============================================================================
@requires_kipy
def test_place_footprint_loads_places_names_and_dirties(rig, pcb):
    api, kicad, board = rig()
    assert api.is_modified is False
    placed = api.place_footprint(LG290P, "U9", 20, 15.25, value="LG290P")
    assert placed["ref"] == "U9" and placed["lib_id"] == LG290P
    assert placed["pads"] == LG290P_PADS and placed["value"] == "LG290P"
    assert (placed["x"], placed["y"], placed["rot"], placed["side"]) == (20.0, 15.25, 0.0, "F")
    assert placed["bbox"] == [19.0, 14.25, 21.0, 16.25]
    assert api.is_modified is True
    # the fake board finds nothing before the load, so a placement proves the order
    assert _loads(kicad) == [("load_all", (kb.LT_FOOTPRINT,))]
    assert [c for c in board.calls if c[0] == "place"] == [
        ("place", LG290P, 20_000_000, 15_250_000, 0.0, kb.BoardLayer.BL_F_Cu)]
    assert ("update", ("U9",)) in board.calls
    saved = [fp for fp in board.footprints if fp.reference_field.text.value == "U9"]
    assert len(saved) == 1 and saved[0].value_field.text.value == "LG290P"


@requires_kipy
def test_place_footprint_loads_the_libraries_once_per_session(rig):
    api, kicad, board = rig()
    api.place_footprint(LG290P, "U9", 20, 15)
    second = api.place_footprint(STOCK_R, "R7", 5, 5, rot=90, side="b")
    assert len(_loads(kicad)) == 1
    assert (second["side"], second["rot"], second["pads"]) == ("B", 90.0, 2)
    last_place = [c for c in board.calls if c[0] == "place"][-1]
    assert last_place[4:] == (90.0, kb.BoardLayer.BL_B_Cu)


@requires_kipy
def test_place_footprint_keeps_the_library_value_by_default(rig):
    api, _kicad, _board = rig()
    assert api.place_footprint(LG290P, "U9", 1, 1)["value"] == "Quectel_LG290P"


@requires_kipy
@pytest.mark.parametrize("lib_id,nick_text", [
    (f"{NICK}:Nope", "footprint library 'P_parts' (project fp-lib-table) is loaded: check the "
                     "footprint name"),
    ("Nolib:X", "KiCad has no footprint library 'Nolib'"),
])
def test_place_footprint_not_found_names_the_lib_id_and_the_project_table(rig, pcb, lib_id,
                                                                           nick_text):
    api, _kicad, _board = rig()
    with pytest.raises(BoardApiError) as err:
        api.place_footprint(lib_id, "U9", 1, 1)
    text = str(err.value)
    assert text.startswith(f"placing {lib_id} as U9 on {pcb} failed: KiCad returned error: "
                           f"footprint '{lib_id}' not found")
    assert nick_text in text and str(pcb.parent / "fp-lib-table") in text
    assert api.is_modified is False


@requires_kipy
def test_place_footprint_reports_a_library_that_did_not_load(rig, pcb):
    broken = "Expecting '(' in 'Quectel_LG290P.kicad_mod', line 2"
    api, kicad, board = rig(statuses=_statuses(project=kb.LLS_ERROR, error=broken))
    board.library = {}                                         # KiCad finds nothing in it
    with pytest.raises(BoardApiError) as err:
        api.place_footprint(LG290P, "U9", 1, 1)
    text = str(err.value)
    assert RECORDED_NOT_LOADED in text and "LLS_ERROR" in text and broken in text
    assert str(pcb.parent / "fp-lib-table") in text


@requires_kipy
@pytest.mark.parametrize("kwargs,match", [
    ({"lib_id": "Quectel_LG290P"}, r"'<nickname>:<footprint>'.*fp-lib-table"),
    ({"lib_id": ":Quectel_LG290P"}, "'<nickname>:<footprint>'"),
    ({"lib_id": f"{NICK}:"}, "'<nickname>:<footprint>'"),
    ({"lib_id": 7}, "'<nickname>:<footprint>'"),
    ({"ref": ""}, "reference must be a non-empty string"),
    ({"ref": "U 9"}, "without spaces"),
    ({"x_mm": "left"}, "x must be a number in mm"),
    ({"y_mm": None}, "y must be a number in mm"),
    ({"rot": "ninety"}, "rot must be a number in degrees"),
    ({"side": "T"}, "side must be 'F' or 'B'"),
    ({"side": "In1.Cu"}, "side must be 'F' or 'B'"),
    ({"value": 5}, "value must be a string"),
])
def test_place_footprint_refuses_bad_arguments_before_kicad(rig, pcb, kwargs, match):
    api, kicad, board = rig()
    args = {"lib_id": LG290P, "ref": "U9", "x_mm": 1, "y_mm": 1, **kwargs}
    with pytest.raises(BoardApiError, match=match) as err:
        api.place_footprint(**args)
    if "lib_id" in kwargs:
        assert str(pcb.parent / "fp-lib-table") in str(err.value)
    assert kicad.calls == [] and board.calls == [] and api.is_modified is False


@requires_kipy
def test_place_footprint_refuses_a_reference_already_on_the_board(rig):
    api, kicad, board = rig(existing=("U9",))
    with pytest.raises(BoardApiError, match="reference U9 is already used"):
        api.place_footprint(LG290P, "U9", 1, 1)
    assert not [c for c in board.calls if c[0] == "place"] and api.is_modified is False


@requires_kipy
def test_place_footprint_in_a_gui_session_skips_the_library_load(rig):
    api, kicad, board = rig(mode="gui")
    assert api.place_footprint(LG290P, "U9", 1, 1)["pads"] == LG290P_PADS
    assert _loads(kicad) == [] and api.is_modified is True


@requires_kipy
def test_place_footprint_naming_failure_leaves_the_board_modified(rig):
    api, _kicad, board = rig()
    board.fail_update = kb.ApiError("editor busy")
    with pytest.raises(BoardApiError) as err:
        api.place_footprint(LG290P, "U9", 1, 1)
    assert LG290P in str(err.value) and "U9" in str(err.value) and "editor busy" in str(err.value)
    assert api.is_modified is True                             # KiCad holds the new footprint


@requires_kipy
def test_place_footprint_on_a_closed_board(rig):
    api, kicad, board = rig()
    api.close()
    with pytest.raises(BoardApiError, match="is closed"):
        api.place_footprint(LG290P, "U9", 1, 1)
    assert board.calls == [] and _loads(kicad) == []


def test_pcb_api_stays_under_800_lines():
    for module in (papi, sess):
        assert len(Path(module.__file__).read_text(encoding="utf-8").splitlines()) < 800


# ============================================================================
# live tier: KiCad 11 nightly on a SWIG-built board (KiCad 9/10 pcbnew)
# ============================================================================
def _server_process(api: BoardApi) -> subprocess.Popen:
    """The `kicad-cli api-server` child of a headless BoardApi (kipy's
    KiCad._server._process): this test's own server, not every kicad-cli on a
    machine where other sessions start and stop theirs."""
    process = getattr(getattr(api.session.kicad, "_server", None), "_process", None)
    assert process is not None and process.poll() is None, "no running api-server child"
    return process


def _assert_server_stopped(process: subprocess.Popen) -> None:
    try:
        process.wait(timeout=30)
    except subprocess.TimeoutExpired:
        pytest.fail(f"kicad-cli api-server (pid {process.pid}) outlived its session")


def _footprint_block(text: str, fpid: str) -> str:
    """The board-level (footprint "<fpid>" ...) block of a .kicad_pcb text."""
    start = text.find(f'\n\t(footprint "{fpid}"')
    assert start >= 0, f"no footprint {fpid} on the board"
    end = text.find("\n\t(", start + 1)
    return text[start:end if end > 0 else len(text)]


@pytest.fixture(scope="module")
def project(tmp_path_factory) -> Path:
    """lm358_buffer built by KiCad 9/10's pcbnew in a temp project whose
    fp-lib-table (materialized, kicad-eda managed) holds P_parts = the authored
    LG290P. The table is written BEFORE any api-server opens the board."""
    if not BoardApi.available():
        pytest.skip(BoardApi.unavailable_reason() or "board API unavailable")
    if not tc.kicad_python():
        pytest.skip(f"no KiCad pcbnew Python to build the board: {tc.kicad_python_error()}")
    import board_build
    import fp_author
    import fp_author_templates as fpt
    root = tmp_path_factory.mktemp("f207") / "proj"
    root.mkdir()
    netlist = root / "netlist.json"
    shutil.copyfile(ROOT / "examples" / "lm358_buffer" / "netlist.json", netlist)
    pcb = root / "lm358_buffer.kicad_pcb"
    built = board_build.build(str(netlist), 30, 25, out_pcb=str(pcb))
    assert built.get("ok"), built
    assert tc.board_file_info(str(pcb))["format_version"] in (20241229, 20260206)
    fp_author.write_footprint(fpt.lg290p(), root / "libs" / f"{NICK}.pretty")
    design = {"libraries": {"footprints": {NICK: f"libs/{NICK}.pretty"}}}
    report = libtable.LibraryResolver.for_design(root, design, None, None).materialize(root)
    assert report["fp-lib-table"]["managed"] == [NICK]
    return pcb


@requires_api
def test_live_place_authored_lg290p_save_and_reopen(project):
    table = project.parent / "fp-lib-table"
    with BoardApi.open(project, mode="headless") as api:
        server = _server_process(api)
        placed = api.place_footprint(LG290P, "U9", 15.0, 12.5, value="LG290P")
        assert (placed["ref"], placed["lib_id"], placed["pads"]) == ("U9", LG290P, LG290P_PADS)
        assert (placed["x"], placed["y"], placed["value"]) == (15.0, 12.5, "LG290P")
        assert api.is_modified and api.session.global_footprint_libraries
        for wrong in (f"{NICK}:Nope", "Nolib:X"):
            with pytest.raises(BoardApiError) as err:
                api.place_footprint(wrong, "U10", 1, 1)
            text = str(err.value)
            assert f"KiCad returned error: footprint '{wrong}' not found" in text
            assert wrong in text and str(table) in text
        with pytest.raises(BoardApiError, match="reference U9 is already used"):
            api.place_footprint(LG290P, "U9", 1, 1)
        api.save()
    _assert_server_stopped(server)
    text = project.read_text(encoding="utf-8")
    assert tc.board_file_info(str(project))["format_version"] == KICAD11_FORMAT
    block = _footprint_block(text, LG290P)
    assert len(re.findall(r"\n\t\t\(pad ", block)) == LG290P_PADS
    assert '(property "Reference" "U9"' in block and '(property "Value" "LG290P"' in block
    with BoardApi.open(project, mode="headless") as again:
        server = _server_process(again)
        found = {fp["ref"]: fp for fp in again.footprints()}
        assert found["U9"]["pads"] == LG290P_PADS and found["U9"]["value"] == "LG290P"
        board = again.session.board(project)
        fpids = {str(fp.reference_field.text.value): str(fp.definition.id)
                 for fp in board.get_footprints()}
        assert fpids["U9"] == LG290P
        assert len(again.pads(ref="U9")) == LG290P_PADS
    _assert_server_stopped(server)
