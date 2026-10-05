"""Project library tables on the KiCad 11 schematic API backend (F2-06, ROADMAP D0/D1/D5).

Two tiers:

* unit (fakes, no KiCad): the session's library caches reset per document,
  ``check_project_libraries`` / ``reload_project_libraries`` against a fake
  kipy KiCad, the project-aware ``library_hint``, ``is_missing_global_tables``
  on the messages recorded from the nightly, the kipy_bridge library enums;
* live (KiCad 11 nightly, skipped when absent): an authored LG290P in a project
  ``sym-lib-table`` written by ``LibraryResolver.materialize``; a malformed
  library reported against the project table, then fixed and reloaded; an
  api-server whose ``KICAD_CONFIG_HOME`` is an empty directory.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

import libtable
import sym_author
from backends import kipy_bridge as kb
from backends import sch_session as sess
from backends.errors import SchematicApiError
from backends.sch_api import PWR_FLAG_LIB_ID, SchematicApi

requires_kipy = pytest.mark.skipif(bool(kb.IMPORT_ERROR), reason=kb.IMPORT_ERROR or "")
requires_api = pytest.mark.skipif(not SchematicApi.available(),
                                  reason=SchematicApi.unavailable_reason() or "")

NICK = "X_parts"
LIB_REL = f"libs/{NICK}.kicad_sym"
TP2 = {"name": "TP2", "reference": "TP", "value": "TP2",
       "pins": [{"number": "1", "name": "A", "type": "passive"},
                {"number": "2", "name": "B", "type": "passive"}]}

# --- recorded from kicad-cli 10.99.0-4025-gf7ca620278 `api-server` started with
# KICAD_CONFIG_HOME = an empty directory (2026-10-05). KiCad creates
# <home>/10.99/{kicad.json,kicad_common.json,colors} but no sym-lib-table /
# fp-lib-table; GetLibraryStatuses(LT_SYMBOL, LTS_GLOBAL) then returns 0 rows,
# LoadAllLibraries succeeds, and every stock symbol fails exactly like a
# misspelt one:
RECORDED_RAW_DEVICE = "KiCad returned error: symbol 'Device:R' not found"
RECORDED_RAW_POWER = "KiCad returned error: symbol 'power:PWR_FLAG' not found"
RECORDED_RELOAD_GLOBAL = "Library 'Device' not found"       # ReloadLibrary(LTS_GLOBAL)
# What SchematicApi.place_symbol raised in that session, verbatim (the live test
# below re-records it and compares, with RECORDED_TABLE replaced by its project's).
RECORDED_TABLE = r"C:\Users\justi\AppData\Local\Temp\f206rec_be3l_fn3\proj\sym-lib-table"
RECORDED_API_ERROR = (
    "placing Device:R as R1: KiCad returned error: symbol 'Device:R' not found; KiCad 11 has "
    "no global symbol library table (0 global symbol libraries loaded): KiCad 11 headless "
    "finds no stock libraries unless its config dir (%APPDATA%\\kicad\\10.99 on Windows, "
    "~/.config/kicad/10.99 elsewhere, $KICAD_CONFIG_HOME/10.99 when that is set) holds "
    "sym-lib-table and fp-lib-table; copy them from <install>/share/kicad/template (this "
    "plugin never edits the KiCad config); a project symbol library must be a row of "
    "C:\\Users\\justi\\AppData\\Local\\Temp\\f206rec_be3l_fn3\\proj\\sym-lib-table (URI "
    "${KIPRJMOD}/...) present when the document was created or opened; after editing a "
    "library file call reload_project_libraries")
# a misspelt symbol / an unknown project nickname in a normal session
RECORDED_TYPO = "KiCad returned error: symbol 'X_parts:NOPE' not found"
RECORDED_RELOAD_UNKNOWN = "Library 'Y_parts' not found"    # row added after the open
RECORDED_MALFORMED = ("Expecting ''('' in 'C:\\Users\\justi\\AppData\\Local\\Temp\\f206\\B\\libs"
                      "\\X_parts.kicad_sym', line 2, offset 59.")


# ============================================================================
# fakes
# ============================================================================
@dataclass(frozen=True)
class FakeStatus:
    nickname: str
    status: int
    error_message: str | None = None
    type: int = kb.LT_SYMBOL
    scope: int = kb.LTS_PROJECT


def _doc(path: Path) -> SimpleNamespace:
    """An open-document specifier as KiCadSession reads it (schematic_for
    clears its sheet_path before binding a kipy Schematic to it)."""
    return SimpleNamespace(project=SimpleNamespace(path=str(path.parent), name=path.stem),
                           ClearField=lambda _field: None)


class FakeKiCad:
    """The kipy KiCad calls KiCadSession makes, recorded in ``calls``."""

    def __init__(self, project: list[FakeStatus] | None = None, global_symbols: int = 2,
                 symbols: dict[str, list[str]] | None = None) -> None:
        self._client = None                     # kb.Schematic(kicad._client, doc) in schematic_for
        self.calls: list[tuple] = []
        self.project = list(project or [])
        self.global_rows = [FakeStatus(f"G{i}", kb.LLS_LOADED, scope=kb.LTS_GLOBAL)
                            for i in range(global_symbols)]
        self.symbols = dict(symbols or {})
        self.docs: list[SimpleNamespace] = []
        self.fail_wait: Exception | None = None

    def get_open_documents(self, _doc_type):
        return list(self.docs)

    def close_document(self, doc) -> None:
        self.calls.append(("close", doc.project.name))
        self.docs = [d for d in self.docs if d is not doc]

    def create_document(self, path: str, _doc_type) -> None:
        self.calls.append(("create", path))
        self.docs = [_doc(Path(path))]

    def open_document(self, path: str, _doc_type) -> None:
        self.calls.append(("open", path))
        self.docs = [_doc(Path(path))]

    def load_all_libraries(self, types=None) -> None:
        self.calls.append(("load_all", types))

    def get_library_statuses(self, types=None, scope=kb.LTS_BOTH) -> list[FakeStatus]:
        rows = {kb.LTS_PROJECT: self.project, kb.LTS_GLOBAL: self.global_rows}.get(
            scope, self.global_rows + self.project)
        return [r for r in rows if types is None or r.type == types]

    def wait_for_libraries(self, types=None, interval_ms=500, timeout_s=60.0,
                           scope=kb.LTS_BOTH) -> list[FakeStatus]:
        self.calls.append(("wait", types, scope))
        if self.fail_wait is not None:
            raise self.fail_wait
        return self.get_library_statuses(types, scope)

    def get_library_symbols(self, nicknames):
        self.calls.append(("symbols", tuple(nicknames)))
        return [SimpleNamespace(name=n) for n in self.symbols.get(nicknames[0], [])]

    def reload_library(self, type, scope, nicknames=()) -> None:  # noqa: A002 (kipy's name)
        self.calls.append(("reload", type, scope, tuple(nicknames)))
        known = {r.nickname for r in self.project}
        for nick in nicknames:
            if nick not in known:
                raise kb.LibraryCommandError(f"Library '{nick}' not found")

    def close(self) -> None:
        self.calls.append(("close_kicad",))


def _session(kicad: FakeKiCad, mode: str = "headless") -> sess.KiCadSession:
    return sess.KiCadSession(kicad, mode, install=None, kicad_version="10.99.0-fake")


def _write_table(project: Path, nicks: list[str], disabled: tuple[str, ...] = ()) -> Path:
    rows = "".join(f'  (lib (name "{n}")(type "KiCad")(uri "${{KIPRJMOD}}/libs/{n}.kicad_sym")'
                   f'(options "")(descr ""){"(disabled)" if n in disabled else ""})\n'
                   for n in nicks)
    path = project / "sym-lib-table"
    path.write_text(f"(sym_lib_table\n  (version 7)\n{rows})\n", encoding="utf-8")
    return path


def _loads(kicad: FakeKiCad) -> int:
    return sum(1 for c in kicad.calls if c[0] == "load_all")


# ============================================================================
# unit tier: kipy_bridge library enums
# ============================================================================
def test_library_enum_fallbacks_are_the_proto_wire_values():
    assert (kb.LT_SYMBOL, kb.LT_FOOTPRINT) == (1, 2)
    assert (kb.LTS_GLOBAL, kb.LTS_PROJECT, kb.LTS_BOTH) == (1, 2, 3)
    assert (kb.LLS_INVALID, kb.LLS_LOADING, kb.LLS_LOADED, kb.LLS_ERROR) == (1, 2, 3, 4)
    for name in ("LT_SYMBOL", "LT_FOOTPRINT", "LTS_GLOBAL", "LTS_PROJECT", "LTS_BOTH",
                 "LLS_LOADED", "LLS_ERROR", "LibraryType", "LibraryTableScope",
                 "LibraryLoadStatus", "LibraryCommandError", "library_status_name"):
        assert name in kb.__all__, name


@requires_kipy
def test_library_enums_match_kipy():
    from kipy.proto.common.types import library_types_pb2 as lt
    assert kb.LibraryType is lt.LibraryType and kb.LibraryTableScope is lt.LibraryTableScope
    assert kb.LT_SYMBOL == lt.LibraryType.LT_SYMBOL
    assert kb.LT_FOOTPRINT == lt.LibraryType.LT_FOOTPRINT
    assert kb.LTS_PROJECT == lt.LibraryTableScope.LTS_PROJECT
    assert kb.LTS_GLOBAL == lt.LibraryTableScope.LTS_GLOBAL
    assert kb.LTS_BOTH == lt.LibraryTableScope.LTS_BOTH
    assert kb.LLS_ERROR == lt.LibraryLoadStatus.LLS_ERROR
    assert kb.LLS_LOADED == lt.LibraryLoadStatus.LLS_LOADED


def test_library_status_name():
    assert kb.library_status_name(kb.LLS_ERROR) == "LLS_ERROR"
    assert kb.library_status_name(kb.LLS_INVALID) == "LLS_INVALID"
    assert kb.library_status_name(99) == "status 99"


def test_pwr_flag_lib_id_derives_from_the_power_library():
    assert PWR_FLAG_LIB_ID == f"{libtable.POWER_LIBRARY}:PWR_FLAG"


# ============================================================================
# unit tier: caches per document
# ============================================================================
@requires_kipy
@pytest.mark.parametrize("verb", ["create_document", "open_document"])
def test_new_document_resets_the_library_caches(tmp_path, verb):
    kicad = FakeKiCad(symbols={"power": ["GND"]})
    session = _session(kicad)
    assert session.has_library_symbol("power:GND")
    assert session.has_library_symbol("power:GND")              # cached
    assert _loads(kicad) == 1 and kicad.calls.count(("symbols", ("power",))) == 1
    kicad.symbols = {"power": ["GND", "VCC"]}
    getattr(session, verb)(str(tmp_path / "b" / "b.kicad_sch"))
    assert session.project_dir == tmp_path / "b"
    assert session.has_library_symbol("power:VCC")              # re-listed ...
    assert _loads(kicad) == 2                                   # ... after a fresh load
    assert kicad.calls.count(("symbols", ("power",))) == 2


@requires_kipy
def test_schematic_for_records_the_project_dir(tmp_path):
    kicad = FakeKiCad()
    path = tmp_path / "gui" / "board.kicad_sch"
    kicad.docs = [_doc(path)]
    session = _session(kicad, mode="gui")
    session.schematic_for(str(path))
    assert session.project_dir == path.parent
    assert session.project_table == path.parent / "sym-lib-table"


@requires_kipy
def test_load_libraries_counts_global_symbol_rows(tmp_path):
    session = _session(FakeKiCad(global_symbols=0))
    assert session.global_tables_missing is False                # unknown before a load
    session.load_libraries()
    assert session.global_symbol_libraries == 0 and session.global_tables_missing is True
    normal = _session(FakeKiCad(global_symbols=3))
    normal.load_libraries()
    assert normal.global_symbol_libraries == 3 and normal.global_tables_missing is False


# ============================================================================
# unit tier: check_project_libraries
# ============================================================================
@requires_kipy
def test_check_returns_the_loaded_project_libraries(tmp_path):
    _write_table(tmp_path, [NICK, "Y_parts"])
    kicad = FakeKiCad(project=[FakeStatus("Y_parts", kb.LLS_LOADED),
                               FakeStatus(NICK, kb.LLS_LOADED)])
    session = _session(kicad)
    session.create_document(str(tmp_path / "t.kicad_sch"))
    assert session.check_project_libraries() == [NICK, "Y_parts"]
    assert session.check_project_libraries([NICK]) == [NICK]
    assert ("wait", kb.LT_SYMBOL, kb.LTS_PROJECT) in kicad.calls


@requires_kipy
def test_check_raises_on_a_failed_row_naming_the_project_table(tmp_path):
    table = _write_table(tmp_path, [NICK])
    kicad = FakeKiCad(project=[FakeStatus(NICK, kb.LLS_ERROR, RECORDED_MALFORMED)])
    session = _session(kicad)
    session.create_document(str(tmp_path / "t.kicad_sch"))
    with pytest.raises(SchematicApiError) as err:
        session.check_project_libraries()
    text = str(err.value)
    assert str(table) in text and NICK in text and "LLS_ERROR" in text
    assert RECORDED_MALFORMED in text and "reload_project_libraries" in text


@requires_kipy
def test_check_narrows_to_the_requested_nicknames(tmp_path):
    _write_table(tmp_path, [NICK, "Bad"])
    kicad = FakeKiCad(project=[FakeStatus(NICK, kb.LLS_LOADED),
                               FakeStatus("Bad", kb.LLS_INVALID)])
    session = _session(kicad)
    session.create_document(str(tmp_path / "t.kicad_sch"))
    assert session.check_project_libraries([NICK]) == [NICK]
    with pytest.raises(SchematicApiError, match="'Bad' LLS_INVALID: no error message"):
        session.check_project_libraries()


@requires_kipy
def test_check_raises_for_a_requested_nickname_kicad_does_not_have(tmp_path):
    table = _write_table(tmp_path, [NICK])
    session = _session(FakeKiCad(project=[FakeStatus(NICK, kb.LLS_LOADED)]))
    session.create_document(str(tmp_path / "t.kicad_sch"))
    with pytest.raises(SchematicApiError) as err:
        session.check_project_libraries(["Y_parts"])
    text = str(err.value)
    assert "'Y_parts'" in text and str(table) in text and "opened again" in text


@requires_kipy
def test_check_raises_when_kicad_never_read_the_table_on_disk(tmp_path):
    """A table written after create/open is invisible to the api-server:
    loud, never an empty list."""
    table = _write_table(tmp_path, [NICK, "Off"], disabled=("Off",))
    session = _session(FakeKiCad(project=[]))
    session.create_document(str(tmp_path / "t.kicad_sch"))
    with pytest.raises(SchematicApiError) as err:
        session.check_project_libraries()
    text = str(err.value)
    assert f"'{NICK}'" in text and str(table) in text and "'Off'" not in text


@requires_kipy
def test_check_without_a_project_table_is_empty(tmp_path):
    session = _session(FakeKiCad(project=[]))
    session.create_document(str(tmp_path / "t.kicad_sch"))
    assert session.check_project_libraries() == []


@requires_kipy
def test_check_reports_an_unreadable_table_file(tmp_path):
    (tmp_path / "sym-lib-table").write_text("(sym_lib_table (lib (name \"A\"", encoding="utf-8")
    session = _session(FakeKiCad(project=[]))
    session.create_document(str(tmp_path / "t.kicad_sch"))
    with pytest.raises(SchematicApiError, match="sym-lib-table"):
        session.check_project_libraries()


@requires_kipy
def test_check_wraps_api_failures_and_needs_a_document(tmp_path):
    kicad = FakeKiCad()
    session = _session(kicad)
    with pytest.raises(SchematicApiError, match="no project document"):
        session.check_project_libraries()
    session.create_document(str(tmp_path / "t.kicad_sch"))
    kicad.fail_wait = kb.ApiError("no handler available")
    with pytest.raises(SchematicApiError) as err:              # the load itself fails
        session.check_project_libraries()
    assert "loading KiCad libraries failed: no handler available" in str(err.value)
    assert str(tmp_path / "sym-lib-table") in str(err.value)
    kicad.fail_wait = None
    session.load_libraries()
    kicad.fail_wait = kb.ApiError("no handler available")
    with pytest.raises(SchematicApiError) as err:              # the status query fails
        session.check_project_libraries()
    assert str(err.value).startswith(
        f"reading the load status of the libraries of {tmp_path / 'sym-lib-table'} failed")


@requires_kipy
def test_check_rejects_a_bare_string_and_empty_nicknames(tmp_path):
    session = _session(FakeKiCad())
    session.create_document(str(tmp_path / "t.kicad_sch"))
    with pytest.raises(SchematicApiError, match="list of nicknames"):
        session.check_project_libraries(NICK)
    with pytest.raises(SchematicApiError, match="must be non-empty"):
        session.check_project_libraries([NICK, " "])


def test_table_rows_rejects_a_footprint_table(tmp_path):
    table = tmp_path / "sym-lib-table"
    table.write_text('(fp_lib_table (version 7) (lib (name "A")(type "KiCad")(uri "a.pretty")'
                     '(options "")(descr "")))\n', encoding="utf-8")
    assert sess.table_rows(table) == ([], [f"{table} is a (fp_lib_table ...) file, not a "
                                           "symbol library table"])
    assert sess.table_rows(tmp_path / "absent") == ([], [])


# ============================================================================
# unit tier: reload_project_libraries
# ============================================================================
@requires_kipy
def test_reload_rereads_the_project_rows_and_clears_the_caches(tmp_path):
    _write_table(tmp_path, [NICK])
    kicad = FakeKiCad(project=[FakeStatus(NICK, kb.LLS_LOADED)], symbols={NICK: ["LG290P"]})
    session = _session(kicad)
    session.create_document(str(tmp_path / "t.kicad_sch"))
    assert session.has_library_symbol(f"{NICK}:LG290P")
    kicad.symbols = {NICK: ["LG290P", "TP2"]}
    assert session.reload_project_libraries([NICK]) == [NICK]
    assert ("reload", kb.LT_SYMBOL, kb.LTS_PROJECT, (NICK,)) in kicad.calls
    assert session.has_library_symbol(f"{NICK}:TP2")             # cache cleared
    assert _loads(kicad) == 1                                    # a reload, not a full load


@requires_kipy
def test_reload_before_any_load_does_the_first_load(tmp_path):
    _write_table(tmp_path, [NICK])
    kicad = FakeKiCad(project=[FakeStatus(NICK, kb.LLS_LOADED)])
    session = _session(kicad)
    session.create_document(str(tmp_path / "t.kicad_sch"))
    assert session.reload_project_libraries() == [NICK]
    assert _loads(kicad) == 1 and not [c for c in kicad.calls if c[0] == "reload"]


@requires_kipy
def test_reload_of_a_row_kicad_never_read_names_the_table(tmp_path):
    _write_table(tmp_path, [NICK, "Y_parts"])
    kicad = FakeKiCad(project=[FakeStatus(NICK, kb.LLS_LOADED)])
    session = _session(kicad)
    session.create_document(str(tmp_path / "t.kicad_sch"))
    session.load_libraries()
    with pytest.raises(SchematicApiError) as err:
        session.reload_project_libraries(["Y_parts"])
    text = str(err.value)
    assert RECORDED_RELOAD_UNKNOWN in text and str(tmp_path / "sym-lib-table") in text
    assert "open_document" in text


@requires_kipy
def test_reload_reports_a_library_still_failing(tmp_path):
    _write_table(tmp_path, [NICK])
    kicad = FakeKiCad(project=[FakeStatus(NICK, kb.LLS_ERROR, RECORDED_MALFORMED)])
    session = _session(kicad)
    session.create_document(str(tmp_path / "t.kicad_sch"))
    session.load_libraries()
    with pytest.raises(SchematicApiError, match="LLS_ERROR"):
        session.reload_project_libraries([NICK])


# ============================================================================
# unit tier: hints and the missing-global-tables test
# ============================================================================
def test_library_hint_names_the_project_table(tmp_path):
    hint = sess.library_hint(tmp_path)
    assert str(tmp_path / "sym-lib-table") in hint
    assert "${KIPRJMOD}" in hint and "reload_project_libraries" in hint
    assert sess.GLOBAL_TABLES_MISSING not in hint
    assert "share/kicad/template" in hint                         # where stock tables come from
    generic = sess.library_hint(None)
    assert "the project's sym-lib-table" in generic


def test_library_hint_leads_with_missing_global_tables(tmp_path):
    hint = sess.library_hint(tmp_path, global_tables_missing=True)
    assert hint.startswith(sess.GLOBAL_TABLES_MISSING)
    assert sess.is_missing_global_tables(hint)
    assert str(tmp_path / "sym-lib-table") in hint


@pytest.mark.skipif(sys.platform != "win32", reason="the message was recorded on Windows")
def test_recorded_message_is_kicads_words_then_the_session_hint():
    kicad_part, sep, hint = RECORDED_API_ERROR.partition(" not found; ")
    assert sep and f"{kicad_part} not found" == f"placing Device:R as R1: {RECORDED_RAW_DEVICE}"
    assert hint == sess.library_hint(Path(RECORDED_TABLE).parent, global_tables_missing=True)


@pytest.mark.parametrize("message,expected", [
    (RECORDED_API_ERROR, True),
    (RECORDED_RAW_POWER, True),                  # PWR_FLAG is in every stock power library
    (f"placing {RECORDED_RAW_POWER}", True),
    (RECORDED_RAW_DEVICE, False),                # identical to a misspelt symbol: no evidence
    (RECORDED_TYPO, False),
    (RECORDED_RELOAD_GLOBAL, False),
    (RECORDED_MALFORMED, False),
    ("", False),
])
def test_is_missing_global_tables(message, expected):
    assert sess.is_missing_global_tables(message) is expected


@requires_kipy
def test_session_hint_and_load_failure_carry_the_project_table(tmp_path):
    kicad = FakeKiCad(global_symbols=0)
    session = _session(kicad)
    session.create_document(str(tmp_path / "t.kicad_sch"))
    session.load_libraries()
    hint = session.library_hint()
    assert sess.is_missing_global_tables(hint) and str(tmp_path / "sym-lib-table") in hint
    other = _session(FakeKiCad())
    other.create_document(str(tmp_path / "t.kicad_sch"))
    other.kicad.fail_wait = TimeoutError("Timed out waiting for libraries to finish loading")
    with pytest.raises(SchematicApiError) as err:
        other.load_libraries()
    assert "Timed out" in str(err.value) and str(tmp_path / "sym-lib-table") in str(err.value)


@requires_kipy
@pytest.mark.parametrize("missing", [False, True])
def test_place_symbol_not_found_uses_the_project_hint(tmp_path, missing):
    path = tmp_path / "proj" / "t.kicad_sch"

    def refuse(lib_id, *_a, **_kw):
        raise kb.ApiError(f"KiCad returned error: symbol '{lib_id}' not found")

    session = SimpleNamespace(mode="headless", global_tables_missing=missing,
                              load_libraries=lambda: None,
                              schematic_for=lambda _p: SimpleNamespace(
                                  place_symbol_from_library=refuse))
    api = SchematicApi(session, str(path), owns_session=False)
    with pytest.raises(SchematicApiError) as err:
        api.place_symbol("Device:R", "R1", 50.8, 50.8)
    text = str(err.value)
    assert text.startswith("placing Device:R as R1: KiCad returned error: symbol 'Device:R'")
    assert str(path.parent / "sym-lib-table") in text
    assert sess.is_missing_global_tables(text) is missing


# ============================================================================
# live tier (KiCad 11 nightly)
# ============================================================================
@pytest.fixture(scope="module")
def live():
    if not SchematicApi.available():
        pytest.skip(SchematicApi.unavailable_reason() or "API backend unavailable")
    session = sess.KiCadSession.headless()
    yield session
    session.close()


def _project(root: Path, malformed: bool = False) -> Path:
    """<root>/libs/X_parts.kicad_sym (the authored LG290P, or garbage) and the
    sym-lib-table LibraryResolver.materialize writes for a netlist block."""
    lib = root / LIB_REL
    lib.parent.mkdir(parents=True, exist_ok=True)
    if malformed:
        lib.write_text('(kicad_symbol_lib (version 20241209) (symbol "BROKEN" (pin \n',
                       encoding="utf-8")
    else:
        sym_author.author_symbol(sym_author.lg290p_spec(), lib)
    design = {"libraries": {"symbols": {NICK: LIB_REL}}}
    report = libtable.LibraryResolver.for_design(root, design, None, None).materialize(root)
    assert report["sym-lib-table"]["managed"] == [NICK]
    return root


@requires_api
def test_live_authored_lg290p_places_from_the_materialized_table(live, tmp_path):
    project = _project(tmp_path / "proj")
    api = SchematicApi.open(project / "gnss.kicad_sch", create=True, overwrite=True,
                            session=live)
    try:
        assert live.check_project_libraries() == [NICK]
        placed = api.place_symbol(f"{NICK}:LG290P", "U1", 101.6, 101.6)
        assert placed["lib_id"] == f"{NICK}:LG290P" and len(placed["pins"]) == 79
        api.save()
        assert f'(lib_id "{NICK}:LG290P")' in Path(api.path).read_text(encoding="utf-8")
    finally:
        api.discard()


@requires_api
def test_live_malformed_library_then_fixed_and_reloaded(live, tmp_path):
    project = _project(tmp_path / "bad", malformed=True)
    table = project / "sym-lib-table"
    api = SchematicApi.open(project / "bad.kicad_sch", create=True, overwrite=True, session=live)
    try:
        with pytest.raises(SchematicApiError) as err:
            live.check_project_libraries([NICK])
        text = str(err.value)
        assert str(table) in text and "LLS_ERROR" in text and "X_parts.kicad_sym" in text
        with pytest.raises(SchematicApiError, match=f"symbol '{NICK}:LG290P' not found") as err:
            api.place_symbol(f"{NICK}:LG290P", "U1", 101.6, 101.6)
        assert str(table) in str(err.value)
        (project / LIB_REL).unlink()                                             # fixed:
        sym_author.author_symbol(sym_author.lg290p_spec(), project / LIB_REL)   # rewritten
        assert live.reload_project_libraries([NICK]) == [NICK]
        assert len(api.place_symbol(f"{NICK}:LG290P", "U1", 101.6, 101.6)["pins"]) == 79
        sym_author.author_symbol(TP2, project / LIB_REL)                         # one more
        assert live.reload_project_libraries([NICK]) == [NICK]
        assert live.has_library_symbol(f"{NICK}:TP2")
        assert api.place_symbol(f"{NICK}:TP2", "TP1", 50.8, 50.8)["lib_id"] == f"{NICK}:TP2"
    finally:
        api.discard()


@requires_api
def test_live_row_added_after_open_needs_a_reopen(live, tmp_path):
    project = _project(tmp_path / "late")
    api = SchematicApi.open(project / "late.kicad_sch", create=True, overwrite=True,
                            session=live)
    try:
        sym_author.author_symbol(TP2, project / "libs" / "Y_parts.kicad_sym")
        design = {"libraries": {"symbols": {NICK: LIB_REL, "Y_parts": "libs/Y_parts.kicad_sym"}}}
        libtable.LibraryResolver.for_design(project, design, None, None).materialize(project)
        with pytest.raises(SchematicApiError) as err:
            live.check_project_libraries()
        assert "'Y_parts'" in str(err.value) and str(project / "sym-lib-table") in str(err.value)
        with pytest.raises(SchematicApiError, match=RECORDED_RELOAD_UNKNOWN):
            live.reload_project_libraries(["Y_parts"])
    finally:
        api.discard()


@requires_api
def test_live_empty_config_home_is_missing_global_tables(tmp_path, monkeypatch):
    """KICAD_CONFIG_HOME = an empty dir: no global tables. The project library
    still places; a stock symbol fails with an error is_missing_global_tables
    recognises (the shape recorded in RECORDED_API_ERROR)."""
    home = tmp_path / "config_home"
    home.mkdir()
    monkeypatch.setenv("KICAD_CONFIG_HOME", str(home))
    project = _project(tmp_path / "proj")
    with sess.KiCadSession.headless() as session:
        api = SchematicApi.open(project / "p.kicad_sch", create=True, overwrite=True,
                                session=session)
        try:
            assert session.check_project_libraries() == [NICK]
            assert session.global_symbol_libraries == 0 and session.global_tables_missing
            assert len(api.place_symbol(f"{NICK}:LG290P", "U1", 101.6, 101.6)["pins"]) == 79
            with pytest.raises(SchematicApiError) as err:
                api.place_symbol("Device:R", "R1", 50.8, 50.8)
            message = str(err.value)
            table = str(project / "sym-lib-table")
            assert message == RECORDED_API_ERROR.replace(RECORDED_TABLE, table), message
            assert sess.is_missing_global_tables(message)
            with pytest.raises(SchematicApiError) as err:
                api.add_power_flag("GND", 50.8, 101.6)
            assert RECORDED_RAW_POWER in str(err.value)
            assert sess.is_missing_global_tables(str(err.value))
        finally:
            api.discard()
    assert not (home / "10.99" / "sym-lib-table").exists()        # nothing wrote the config
