"""The pre-KiCad-11 snapshot (ROADMAP D5) and the Freerouting fallback.

A save through a KiCad >= 10.99 session rewrites X.kicad_pcb, X.kicad_pro and
X.kicad_prl in KiCad 11 form, which KiCad 9/10 pcbnew (Freerouting, the SWIG
gates) cannot read. toolchain copies the pre-11 set to X.k9.* first and can
restore it (the KiCad 11 copy moves to X.agent.*); autoroute_board(...,
from_snapshot=True) routes the restored board with Freerouting, and both
board-rewriting tools refuse / release a board-API session that still holds
the path.

Three tiers: header-only files for the toolchain helpers; faked
subprocesses and a stub registry session for the board_pipeline_tools flow
(no KiCad needed); one live test on the KiCad 11 nightly + KiCad 9 pcbnew +
Freerouting, skipped when any of them is absent.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Callable

import pytest

import board_pipeline_tools as bpt
import board_route_tools as brt
import board_sessions as bs
import kicad_env as env
import kicad_ipc as ipc
import toolchain as tc
from backends.pcb_api import BoardApi
from fixtures.kicad_procs import owned_pids

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"
K9 = ("9.0", 20241229)
K10 = ("10.0", 20260206)
K11 = ("10.99", 20260901)
K9_PRO = '{"net_settings": {"classes": [{"name": "NC_OUT_A", "track_width": 0.302}]}}\n'
K11_PRO = '{"meta": {"version": 3}, "net_settings": {"classes": []}}\n'


def _header(generator_version: str | None, format_version: int) -> str:
    gen = f'\t(generator_version "{generator_version}")\n' if generator_version else ""
    return (f'(kicad_pcb\n\t(version {format_version})\n\t(generator "pcbnew")\n{gen}'
            '\t(general\n\t\t(thickness 1.6)\n\t)\n)\n')


def _write(path: Path, text: str) -> str:
    path.write_text(text, encoding="utf-8")
    return str(path)


def _project(tmp_path: Path, writer: tuple[str, int] = K9, *, pro: str | None = K9_PRO,
             prl: bool = True, dru: bool = False, stem: str = "netlist") -> str:
    """X.kicad_pcb written by `writer` plus the project files a build leaves."""
    pcb = _write(tmp_path / f"{stem}.kicad_pcb", _header(*writer))
    if pro is not None:
        _write(tmp_path / f"{stem}.kicad_pro", pro)
    if prl:
        _write(tmp_path / f"{stem}.kicad_prl", '{"meta": {"version": 5}}\n')
    if dru:
        _write(tmp_path / f"{stem}.kicad_dru", "(version 1)\n")
    _write(tmp_path / f"~{stem}.kicad_pro.lck", '{"hostname": "x"}')    # KiCad 11 leaves it
    return pcb


def _sib(pcb: str, tag: str, ext: str) -> Path:
    return Path(str(Path(pcb).with_suffix("")) + tag + ext)


def _k11_save(pcb: str) -> None:
    """What a KiCad 11 save does to the set: board, project and prl rewritten."""
    _write(Path(pcb), _header(*K11))
    _write(_sib(pcb, "", ".kicad_pro"), K11_PRO)
    _write(_sib(pcb, "", ".kicad_prl"), '{"meta": {"version": 7}}\n')


def _bump_mtime(path: Path, seconds: int = 5) -> None:
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + seconds * 1_000_000_000))


def _listing(folder: Path, skip: str = "\0") -> dict[str, bytes]:
    """{name: bytes} of every file in `folder` whose name does not contain `skip`."""
    return {p.name: p.read_bytes() for p in folder.iterdir() if p.is_file() and skip not in p.name}


class Editors:
    """A fake running KiCad for kicad_ipc.editor_holding: `held` maps a
    normalised path to the editor kind holding it; `asked` records queries."""

    def __init__(self) -> None:
        self.held: dict[str, str] = {}
        self.asked: list[str] = []

    def __call__(self, path: str) -> str | None:
        self.asked.append(path)
        return self.held.get(ipc.norm_path(path))


REAL_EDITOR_HOLDING = ipc.editor_holding


@pytest.fixture(autouse=True)
def editors(monkeypatch) -> Editors:
    """No unit test asks a real KiCad GUI what it holds (the live tests do)."""
    fake = Editors()
    monkeypatch.setattr(ipc, "editor_holding", fake)
    return fake


# ============================================================================
# toolchain: naming, format and session tests
# ============================================================================
def test_snapshot_and_agent_paths_sit_beside_the_board(tmp_path) -> None:
    pcb = str(tmp_path / "netlist.kicad_pcb")
    assert tc.pre_k11_snapshot_path(pcb) == str(tmp_path / "netlist.k9.kicad_pcb")
    assert tc.agent_copy_path(pcb) == str(tmp_path / "netlist.agent.kicad_pcb")
    dotted = str(tmp_path / "a.b.kicad_pcb")
    assert tc.pre_k11_snapshot_path(dotted) == str(tmp_path / "a.b.k9.kicad_pcb")
    assert tc.existing_snapshot(pcb) is None


@pytest.mark.parametrize("writer, expected", [
    (K9, True), (K10, True), (("8.0", 20240108), True), (K11, False), (("11.0", 20270101), False),
    ((None, 20221018), True),                      # a KiCad 7 header without generator_version
    ((None, 20260901), False),
])
def test_is_pre_k11_reads_the_writer_from_the_header(tmp_path, writer, expected) -> None:
    pcb = _write(tmp_path / "b.kicad_pcb", _header(*writer))
    assert tc.is_pre_k11(pcb) is expected


@pytest.mark.parametrize("content", [b"", b"\x00\xff garbage", b"(kicad_sch (version 1))"])
def test_is_pre_k11_is_false_for_what_is_not_a_board(tmp_path, content: bytes) -> None:
    path = tmp_path / "b.kicad_pcb"
    path.write_bytes(content)
    assert tc.is_pre_k11(str(path)) is False
    assert tc.is_pre_k11(str(tmp_path / "missing.kicad_pcb")) is False


def test_snapshot_field_names_the_snapshot_only_when_it_exists(tmp_path) -> None:
    pcb = _project(tmp_path)
    assert tc.snapshot_field(pcb) == {}
    tc.take_snapshot(pcb)
    assert tc.snapshot_field(pcb) == {"pre_k11_snapshot": tc.pre_k11_snapshot_path(pcb)}


@pytest.mark.parametrize("history, since, stalled", [
    ((7,), 0, False), ((7, 6, 5, 4), 0, False), ((5, 5, 5), 2, False), ((5, 5, 5, 5), 3, True),
    ((9, 4, 6, 4, 5), 3, True), ((3, 0, 0, 0, 0), 3, False),     # a clean board is done, not stuck
])
def test_route_progress_counts_rounds_without_a_new_best(history, since, stalled) -> None:
    res = tc.route_progress(history)
    assert res["progress"] == {"rounds": len(history), "score": history[-1],
                               "best": min(history), "rounds_without_improvement": since}
    assert res["stalled"] is stalled and ("stall_note" in res) is stalled


def test_route_progress_needs_a_round() -> None:
    with pytest.raises(ValueError, match="at least one round"):
        tc.route_progress(())


@pytest.mark.parametrize("version, expected", [
    ("10.99.0-4025-gf7ca620278", True), ("11.0.1", True), ("10.99", True),
    ("10.0.5", False), ("9.0.4", False), ("", False), ("garbage", False)])
def test_is_k11_session(version: str, expected: bool) -> None:
    assert tc.is_k11_session(version) is expected


# ============================================================================
# toolchain: take_snapshot / snapshot_current / snapshot_before_k11_write
# ============================================================================
def test_take_snapshot_copies_the_set_with_mtimes_and_skips_the_lock(tmp_path) -> None:
    pcb = _project(tmp_path, dru=True)
    res = tc.take_snapshot(pcb)
    snap = tc.pre_k11_snapshot_path(pcb)
    assert res["snapshot"] == snap and tc.existing_snapshot(pcb) == snap
    assert sorted(Path(f).name for f in res["files"]) == [
        "netlist.k9.kicad_dru", "netlist.k9.kicad_pcb", "netlist.k9.kicad_prl",
        "netlist.k9.kicad_pro"]
    for ext in (".kicad_pcb", ".kicad_pro", ".kicad_prl", ".kicad_dru"):
        src, dst = _sib(pcb, "", ext), _sib(pcb, ".k9", ext)
        assert dst.read_bytes() == src.read_bytes()
        assert dst.stat().st_mtime_ns == src.stat().st_mtime_ns
    assert not (tmp_path / "~netlist.k9.kicad_pro.lck").exists()
    locks = sorted(p.name for p in tmp_path.iterdir() if "lck" in p.name)
    assert locks == ["~netlist.kicad_pro.lck"]
    assert tc.snapshot_current(pcb) is True


def test_take_snapshot_drops_stale_optional_files_of_an_older_snapshot(tmp_path) -> None:
    pcb = _project(tmp_path, dru=True)
    tc.take_snapshot(pcb)
    _sib(pcb, "", ".kicad_dru").unlink()
    _sib(pcb, "", ".kicad_prl").unlink()
    res = tc.take_snapshot(pcb)
    assert sorted(Path(f).name for f in res["files"]) == ["netlist.k9.kicad_pcb",
                                                          "netlist.k9.kicad_pro"]
    assert not _sib(pcb, ".k9", ".kicad_dru").exists()
    assert not _sib(pcb, ".k9", ".kicad_prl").exists()


def test_take_snapshot_of_a_board_without_a_project(tmp_path) -> None:
    pcb = _project(tmp_path, pro=None, prl=False)
    assert [Path(f).name for f in tc.take_snapshot(pcb)["files"]] == ["netlist.k9.kicad_pcb"]
    assert tc.snapshot_current(pcb) is True
    _write(_sib(pcb, "", ".kicad_pro"), K9_PRO)            # a project appeared since: stale
    assert tc.snapshot_current(pcb) is False


def test_take_snapshot_refuses_a_kicad11_or_missing_board(tmp_path) -> None:
    k11 = _project(tmp_path, K11)
    with pytest.raises(ValueError, match="not a pre-KiCad-11 board"):
        tc.take_snapshot(k11)
    with pytest.raises(ValueError, match="not found"):
        tc.take_snapshot(str(tmp_path / "missing.kicad_pcb"))
    assert tc.existing_snapshot(k11) is None


def test_snapshot_goes_stale_when_the_board_or_project_changes(tmp_path) -> None:
    pcb = _project(tmp_path)
    assert tc.snapshot_current(pcb) is False
    tc.take_snapshot(pcb)
    assert tc.snapshot_current(pcb) is True
    _bump_mtime(_sib(pcb, "", ".kicad_pro"))              # netclass_writer rewrote the project
    assert tc.snapshot_current(pcb) is False
    tc.take_snapshot(pcb)
    _bump_mtime(Path(pcb))                                 # a KiCad 9 re-placement
    assert tc.snapshot_current(pcb) is False
    _sib(pcb, ".k9", ".kicad_pcb").unlink()
    assert tc.snapshot_current(pcb) is False


def test_snapshot_before_k11_write_follows_the_d5_rule(tmp_path, monkeypatch) -> None:
    pcb = _project(tmp_path)
    taken: list[str] = []
    real = tc.take_snapshot
    monkeypatch.setattr(tc, "take_snapshot", lambda p: taken.append(p) or real(p))
    assert tc.snapshot_before_k11_write(pcb, "9.0.4") is None              # KiCad 9 writes K9
    assert tc.snapshot_before_k11_write(pcb, "10.0.5") is None
    snap = tc.pre_k11_snapshot_path(pcb)
    assert tc.snapshot_before_k11_write(pcb, "10.99.0-4025-g1") == snap
    assert tc.snapshot_before_k11_write(pcb, "10.99.0-4025-g1") is None    # current already
    assert taken == [pcb]
    _k11_save(pcb)
    assert tc.snapshot_before_k11_write(pcb, "10.99.0-4025-g1") is None    # nothing pre-11 left
    _write(Path(pcb), _header(*K9))                                        # a K9 rebuild
    _bump_mtime(Path(pcb))
    assert tc.snapshot_before_k11_write(pcb, "11.0.0") == snap and taken == [pcb, pcb]
    assert tc.snapshot_before_k11_write(str(tmp_path / "gone.kicad_pcb"), "11.0.0") is None


# ============================================================================
# toolchain: restore_snapshot
# ============================================================================
def _snapshotted_k11_board(tmp_path: Path, *, dru: bool = False) -> str:
    """X saved by KiCad 11 after its pre-11 set was snapshotted to X.k9.*."""
    pcb = _project(tmp_path, dru=dru)
    tc.take_snapshot(pcb)
    _k11_save(pcb)
    return pcb


def test_restore_moves_the_kicad11_copy_aside_and_brings_the_snapshot_back(tmp_path) -> None:
    pcb = _snapshotted_k11_board(tmp_path, dru=True)
    k9_bytes = {ext: _sib(pcb, ".k9", ext).read_bytes()
                for ext in (".kicad_pcb", ".kicad_pro", ".kicad_prl")}
    res = tc.restore_snapshot(pcb)
    assert res["pcb"] == pcb and res["snapshot"] == tc.pre_k11_snapshot_path(pcb)
    assert res["agent_copy"] == tc.agent_copy_path(pcb)
    assert tc.is_pre_k11(pcb) and not tc.is_pre_k11(res["agent_copy"])
    for ext, data in k9_bytes.items():
        assert _sib(pcb, "", ext).read_bytes() == data                   # canonical = snapshot
        assert _sib(pcb, ".k9", ext).read_bytes() == data                # the snapshot stays
    assert _sib(pcb, ".agent", ".kicad_pro").read_text(encoding="utf-8") == K11_PRO
    assert '"version": 7' in _sib(pcb, ".agent", ".kicad_prl").read_text(encoding="utf-8")
    # KiCad 11 never writes the rules: the canonical file stays, the agent pair gets a copy
    assert _sib(pcb, "", ".kicad_dru").is_file() and _sib(pcb, ".agent", ".kicad_dru").is_file()
    assert sorted(Path(p).name for p in res["moved"]) == [
        "netlist.agent.kicad_pcb", "netlist.agent.kicad_prl", "netlist.agent.kicad_pro"]
    assert sorted(Path(p).name for p in res["restored"]) == [
        "netlist.kicad_pcb", "netlist.kicad_prl", "netlist.kicad_pro"]
    assert tc.snapshot_current(pcb) is True                               # copy2 kept the mtimes


def test_restore_refuses_without_a_usable_snapshot(tmp_path) -> None:
    for name in ("a", "b", "c"):
        (tmp_path / name).mkdir()
    no_snap = _project(tmp_path / "a", K11)
    with pytest.raises(ValueError, match="no pre-KiCad-11 snapshot"):
        tc.restore_snapshot(no_snap)
    pre11 = _project(tmp_path / "b")
    tc.take_snapshot(pre11)
    with pytest.raises(ValueError, match="already a KiCad 9/10 board"):
        tc.restore_snapshot(pre11)
    pcb = _snapshotted_k11_board(tmp_path / "c")
    _write(_sib(pcb, ".k9", ".kicad_pcb"), _header(*K11))                 # not pre-11 after all
    with pytest.raises(ValueError, match="is not a pre-KiCad-11 board"):
        tc.restore_snapshot(pcb)
    assert not tc.is_pre_k11(pcb) and not Path(tc.agent_copy_path(pcb)).exists()


def test_restore_undoes_its_moves_when_a_copy_fails(tmp_path, monkeypatch) -> None:
    pcb = _snapshotted_k11_board(tmp_path)
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    real = shutil.copy2
    calls: list[str] = []

    def flaky(src, dst, **kw):
        calls.append(str(dst))
        if len(calls) == 2:
            raise OSError("disk full")
        return real(src, dst, **kw)

    monkeypatch.setattr(shutil, "copy2", flaky)
    with pytest.raises(OSError, match="disk full"):
        tc.restore_snapshot(pcb)
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before


def test_restore_refuses_a_missing_board_and_moves_nothing(tmp_path) -> None:
    pcb = str(tmp_path / "netlist.kicad_pcb")                 # only the snapshot is left
    _write(_sib(pcb, ".k9", ".kicad_pcb"), _header(*K9))
    _write(_sib(pcb, ".k9", ".kicad_pro"), K9_PRO)
    before = _listing(tmp_path)
    with pytest.raises(ValueError, match="netlist.kicad_pcb not found; the snapshot") as exc:
        tc.restore_snapshot(pcb)
    assert "is left as it is" in str(exc.value)
    assert _listing(tmp_path) == before and not Path(tc.agent_copy_path(pcb)).exists()


# ============================================================================
# toolchain: a second restore rotates the first agent copy (keep_agent_copy)
# ============================================================================
SET_EXTS = (".kicad_pcb", ".kicad_pro", ".kicad_prl", ".kicad_dru")


def _set(pcb: str, tag: str = ".agent") -> dict[str, bytes]:
    """{ext: bytes} of the X<tag>.* files that exist (tag "" = the canonical set)."""
    return {ext: _sib(pcb, tag, ext).read_bytes() for ext in SET_EXTS
            if _sib(pcb, tag, ext).is_file()}


def _second_kicad11_save(pcb: str) -> None:
    """After a restore: Freerouting routed the K9 board, the agent saved it
    through KiCad 11 again (a fresh snapshot first, as the board tools do)."""
    _write(Path(pcb), _header(*K9) + "; routed\n")
    _bump_mtime(Path(pcb))
    tc.take_snapshot(pcb)
    _k11_save(pcb)
    _write(Path(pcb), _header(*K11) + "; second agent\n")


def test_a_second_restore_keeps_the_first_agent_copy_as_agent_1(tmp_path) -> None:
    pcb = _snapshotted_k11_board(tmp_path)
    first_agent = _set(pcb, "")                                # board, project, prl of KiCad 11
    first = tc.restore_snapshot(pcb)
    assert first["previous_agent_copy"] is None and _set(pcb) == first_agent
    _second_kicad11_save(pcb)
    second = tc.restore_snapshot(pcb)
    assert second["agent_copy"] == tc.agent_copy_path(pcb)
    assert second["previous_agent_copy"] == str(_sib(pcb, ".agent.1", ".kicad_pcb"))
    assert _set(pcb, ".agent.1") == first_agent                # nothing of the first copy lost
    assert _set(pcb)[".kicad_pcb"].rstrip().endswith(b"; second agent")
    assert Path(pcb).read_text(encoding="utf-8").endswith("; routed\n") and tc.is_pre_k11(pcb)


def test_a_failed_second_restore_puts_every_file_back(tmp_path, monkeypatch) -> None:
    pcb = _snapshotted_k11_board(tmp_path, dru=True)
    tc.restore_snapshot(pcb)
    _second_kicad11_save(pcb)
    before = _listing(tmp_path)
    real = shutil.copy2
    calls: list[str] = []

    def flaky(src, dst, **kw):
        calls.append(str(dst))
        if len(calls) == 3:                    # the rules copy, then the snapshot board, then...
            raise OSError("disk full")
        return real(src, dst, **kw)

    monkeypatch.setattr(shutil, "copy2", flaky)
    with pytest.raises(OSError, match="disk full"):
        tc.restore_snapshot(pcb)
    assert _listing(tmp_path) == before and not _sib(pcb, ".agent.1", ".kicad_pcb").exists()


# ============================================================================
# toolchain: restore_snapshot and the KiCad 11 project lock
# ============================================================================
def test_restore_removes_the_leftover_lock_when_nothing_holds_the_board(tmp_path,
                                                                        editors) -> None:
    pcb = _snapshotted_k11_board(tmp_path)
    lock = tmp_path / "~netlist.kicad_pro.lck"
    res = tc.restore_snapshot(pcb)
    assert res["lock_removed"] is True and not lock.exists() and "lock_note" not in res
    assert ipc.norm_path(pcb) in [ipc.norm_path(p) for p in editors.asked]


def test_restore_keeps_the_lock_while_a_session_or_an_editor_holds_the_board(
        tmp_path, registry, editors) -> None:
    for name in ("session", "pcbnew", "manager"):
        (tmp_path / name).mkdir()
    held = _snapshotted_k11_board(tmp_path / "session")
    registry(held)                                          # this process's board API session
    res = tc.restore_snapshot(held)
    assert res["lock_removed"] is False and "session" in res["lock_note"]
    assert (tmp_path / "session" / "~netlist.kicad_pro.lck").is_file() and editors.asked == []
    gui = _snapshotted_k11_board(tmp_path / "pcbnew")
    editors.held[ipc.norm_path(gui)] = "pcb"                # open in a running PCB editor
    res = tc.restore_snapshot(gui)
    assert res["lock_removed"] is False and "Pcbnew" in res["lock_note"]
    assert (tmp_path / "pcbnew" / "~netlist.kicad_pro.lck").is_file()
    project = _snapshotted_k11_board(tmp_path / "manager")
    editors.held[ipc.norm_path(str(_sib(project, "", ".kicad_pro")))] = "project"
    res = tc.restore_snapshot(project)
    assert res["lock_removed"] is False and "project manager" in res["lock_note"]
    assert (tmp_path / "manager" / "~netlist.kicad_pro.lck").is_file()


def test_restore_without_a_lock_asks_no_editor(tmp_path, editors) -> None:
    pcb = _snapshotted_k11_board(tmp_path)
    (tmp_path / "~netlist.kicad_pro.lck").unlink()
    res = tc.restore_snapshot(pcb)
    assert res["lock_removed"] is False and "lock_note" not in res and editors.asked == []


def test_restore_reports_a_lock_it_cannot_remove(tmp_path, monkeypatch) -> None:
    pcb = _snapshotted_k11_board(tmp_path)
    real = Path.unlink

    def denied(self: Path, missing_ok: bool = False) -> None:
        if self.name.endswith(".lck"):
            raise PermissionError("in use")
        real(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", denied)
    res = tc.restore_snapshot(pcb)
    assert res["lock_removed"] is False and "could not be removed (in use)" in res["lock_note"]
    assert tc.is_pre_k11(pcb) and (tmp_path / "~netlist.kicad_pro.lck").is_file()


# ============================================================================
# the board_pipeline_tools flow over a stub registry session
# ============================================================================
class StubSession:
    """The registry face of a BoardApi: path, mode, dirty flag, close."""

    def __init__(self, path: str, mode: str = "headless", modified: bool = False) -> None:
        self.path, self.mode, self.is_modified = path, mode, modified
        self.closed = False

    def close(self) -> None:
        self.closed = True

    def discard(self) -> None:
        self.closed = True


@pytest.fixture
def registry(monkeypatch) -> Callable[..., StubSession]:
    """An isolated board registry; hold(pcb) puts a stub session on it."""
    monkeypatch.setattr(bs, "_BOARDS", {})
    monkeypatch.setattr(bs, "_PROGRESS", {})

    def hold(pcb: str, **kw) -> StubSession:
        stub = StubSession(pcb, **kw)
        bs._BOARDS[bs._key(pcb)] = stub
        return stub
    return hold


def _install(base: Path, version: str, vt: tuple[int, int, int], *,
             python: bool) -> env.KiCadInstall:
    root = base / version
    return env.KiCadInstall(
        version=version, version_tuple=vt, root=root, kicad_cli=root / "bin" / "kicad-cli.exe",
        python=(root / "bin" / "python.exe") if python else None,
        share=root / "share" / "kicad", has_api_server=vt >= env.API_SERVER_MIN_VERSION)


@pytest.fixture
def machine(monkeypatch, tmp_path) -> tuple[env.KiCadInstall, env.KiCadInstall]:
    """KiCad 9.0.4 (pcbnew Python) + the 10.99 nightly, Java and a jar, no GUI."""
    stable = _install(tmp_path / "kicad", "9.0", (9, 0, 4), python=True)
    nightly = _install(tmp_path / "kicad", "10.99", (10, 99, 0), python=False)
    monkeypatch.setattr(env, "discover", lambda refresh=False: [nightly, stable])
    monkeypatch.setenv("DISPLAY", ":99")          # Freerouting 1.x needs none faked on Linux CI
    monkeypatch.setattr(tc, "find_java", lambda: "java")
    monkeypatch.setattr(tc, "freerouting_jar", lambda explicit="": "freerouting-1.9.0.jar")
    monkeypatch.setattr(ipc, "write_guard", lambda path: None)
    return stable, nightly


def _done(stdout: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")


def _fake_chain(monkeypatch, pcb: str) -> list[dict]:
    """tc.run standing in for DSN export -> Freerouting -> SES import -> DRC.
    Each call records argv and the board header the step saw."""
    seen: list[dict] = []

    def fake_run(args, **kw):
        step = {"args": list(args), "board": tc.board_file_info(pcb)}
        seen.append(step)
        joined = " ".join(map(str, args))
        if "swig_export_dsn.py" in joined:
            _write(Path(args[3]), "(pcb dsn)")
        elif "-jar" in args:
            _write(Path(args[args.index("-do") + 1]), "(session ses)")
        elif "swig_import_ses.py" in joined:
            return _done('{"ok": true, "tracks": 21, "vias": 6, "warnings": []}\n')
        elif "drc" in args:
            _write(Path(args[args.index("-o") + 1]),
                   json.dumps({"violations": [], "unconnected_items": []}))
        return _done()

    monkeypatch.setattr(tc, "run", fake_run)
    return seen


def test_autoroute_from_snapshot_restores_and_routes_the_pre11_board(tmp_path, machine, registry,
                                                                     monkeypatch) -> None:
    stable, _ = machine
    pcb = _snapshotted_k11_board(tmp_path)
    idle = registry(pcb)                                   # the agent's saved headless session
    bs._PROGRESS[bs._key(pcb)] = (4, 4, 4, 4)
    seen = _fake_chain(monkeypatch, pcb)
    res = bpt.autoroute_board(pcb, from_snapshot=True)
    assert res["ok"] is True and res["fallback_from_snapshot"] is True, res
    assert res["pcb"] == pcb and res["agent_copy"] == tc.agent_copy_path(pcb)
    assert res["pre_k11_snapshot"] == tc.pre_k11_snapshot_path(pcb)
    assert res["tracks"] == 21 and res["unconnected"] == 0
    assert res["released_boards"] == [pcb] and idle.closed and bs._PROGRESS == {}
    # every SWIG step and the DRC saw the restored KiCad 9 board, never the KiCad 11 one
    assert {s["board"]["generator_version"] for s in seen} == {"9.0"}
    assert seen[-1]["args"][0] == str(stable.kicad_cli)
    assert not tc.is_pre_k11(res["agent_copy"])
    assert _sib(pcb, "", ".kicad_pro").read_text(encoding="utf-8") == K9_PRO
    assert _sib(pcb, ".agent", ".kicad_pro").read_text(encoding="utf-8") == K11_PRO


def test_autoroute_refuses_a_dirty_session_before_touching_any_file(tmp_path, machine, registry,
                                                                     monkeypatch) -> None:
    pcb = _snapshotted_k11_board(tmp_path)
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    dirty = registry(pcb, modified=True)
    monkeypatch.setattr(tc, "run", lambda args, **kw: pytest.fail(f"ran {args[:2]}"))
    res = bpt.autoroute_board(pcb, from_snapshot=True)
    assert res["ok"] is False and res["blocking_board"] == pcb and not dirty.closed
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before


def test_autoroute_on_a_pre11_board_releases_a_held_session_and_routes_in_place(
        tmp_path, machine, registry, monkeypatch) -> None:
    pcb = _project(tmp_path)
    idle = registry(pcb)
    seen = _fake_chain(monkeypatch, pcb)
    res = bpt.autoroute_board(pcb, from_snapshot=True)
    assert res["ok"] is True and res["fallback_from_snapshot"] is False
    assert "already" in res["fallback_note"] and "agent_copy" not in res
    assert res["released_boards"] == [pcb] and idle.closed
    assert not Path(tc.agent_copy_path(pcb)).exists() and len(seen) == 4
    plain = bpt.autoroute_board(pcb)                        # default: no fallback keys at all
    assert plain["ok"] is True and "fallback_from_snapshot" not in plain


def test_autoroute_of_a_kicad11_board_names_the_snapshot_fallback(tmp_path, machine, registry,
                                                                   monkeypatch) -> None:
    monkeypatch.setattr(tc, "run", lambda args, **kw: pytest.fail(f"ran {args[:2]}"))
    pcb = _snapshotted_k11_board(tmp_path)
    refused = bpt.autoroute_board(pcb)
    assert refused["ok"] is False
    assert refused["reason"].startswith("netlist.kicad_pcb was saved by KiCad 10.99")
    assert "from_snapshot=true" in refused["reason"]
    assert refused["pre_k11_snapshot"] == tc.pre_k11_snapshot_path(pcb)
    bare = tmp_path / "bare"
    bare.mkdir()
    k11 = _project(bare, K11)
    plain = bpt.autoroute_board(k11)
    assert plain["ok"] is False and "from_snapshot" not in plain["reason"]
    no_snap = bpt.autoroute_board(k11, from_snapshot=True)
    assert no_snap["ok"] is False and "no pre-KiCad-11 snapshot" in no_snap["reason"]
    assert not Path(tc.agent_copy_path(k11)).exists()
    k10_dir = tmp_path / "k10"                    # KiCad 10.0 is too new for 9.0's pcbnew, but
    k10_dir.mkdir()                               # it is no KiCad 11 board: nothing to restore
    k10 = _project(k10_dir, K10)
    _write(_sib(k10, ".k9", ".kicad_pcb"), _header(*K9))
    k10_res = bpt.autoroute_board(k10, from_snapshot=True)
    assert k10_res["ok"] is False and "is not a KiCad 11 board" in k10_res["reason"]
    assert "from_snapshot" not in bpt.autoroute_board(k10)["reason"]
    assert _sib(k10, "", ".kicad_pcb").read_text(encoding="utf-8") == _header(*K10)


def test_autoroute_reports_a_failed_restore(tmp_path, machine, registry, monkeypatch) -> None:
    pcb = _snapshotted_k11_board(tmp_path)
    monkeypatch.setattr(tc, "run", lambda args, **kw: pytest.fail(f"ran {args[:2]}"))

    def broken(path: str) -> dict:
        raise OSError("permission denied")

    monkeypatch.setattr(tc, "restore_snapshot", broken)
    res = bpt.autoroute_board(pcb, from_snapshot=True)
    assert res["ok"] is False and res["step"] == "restore"
    assert "permission denied" in res["reason"]


def test_autoroute_refuses_a_snapshot_the_pcbnew_cannot_read_either(tmp_path, machine, registry,
                                                                     monkeypatch) -> None:
    pcb = _project(tmp_path, K11, pro=K11_PRO)
    _write(_sib(pcb, ".k9", ".kicad_pcb"), _header(*K10))    # 9.0.4's pcbnew cannot load 10.0
    before = _listing(tmp_path)
    monkeypatch.setattr(tc, "run", lambda args, **kw: pytest.fail(f"ran {args[:2]}"))
    res = bpt.autoroute_board(pcb, from_snapshot=True)
    assert res["ok"] is False and res["pre_k11_snapshot"] == tc.pre_k11_snapshot_path(pcb)
    assert res["reason"].startswith("the snapshot is unreadable too: netlist.k9.kicad_pcb was "
                                    "saved by KiCad 10.0 (format 20260206)")
    assert "pcbnew Python cannot read it" in res["reason"]
    assert _listing(tmp_path) == before and not Path(tc.agent_copy_path(pcb)).exists()


def test_drc_check_reports_a_report_that_cannot_be_read_back(tmp_path, machine,
                                                              monkeypatch) -> None:
    stable, _ = machine
    pcb = _project(tmp_path)
    _fake_chain(monkeypatch, pcb)                           # kicad-cli writes a clean report
    real = tc.drc_counts

    def counted_then_corrupted(path: str) -> dict:
        counts = real(path)
        Path(counts["report"]).write_text("{not json", encoding="utf-8")
        return counts

    monkeypatch.setattr(tc, "drc_counts", counted_then_corrupted)
    before = _listing(tmp_path, skip="_drc.json")
    res = tc.drc_check(pcb, 10)
    report = str(tmp_path / "netlist_drc.json")
    assert res["ok"] is False and res["reason"].startswith(f"DRC report {report} unreadable: ")
    assert "errors" not in res and res["report"] == report
    assert res["kicad_cli"] == str(stable.kicad_cli)
    assert _listing(tmp_path, skip="_drc.json") == before


def test_build_board_refuses_or_releases_a_held_session(tmp_path, machine, registry,
                                                         monkeypatch) -> None:
    netlist = _write(tmp_path / "netlist.json", json.dumps({"components": [], "nets": {}}))
    out = str(tmp_path / "netlist.kicad_pcb")
    dirty = registry(out, modified=True)
    monkeypatch.setattr(tc, "run", lambda args, **kw: pytest.fail(f"ran {args[:2]}"))
    refused = bpt.build_board_from_netlist(netlist, 30, 20, out_pcb=out,
                                           footprints_dir=str(tmp_path))
    assert refused["ok"] is False and refused["blocking_board"] == out and not dirty.closed
    dirty.is_modified = False
    ran: list[list[str]] = []
    monkeypatch.setattr(tc, "run", lambda args, **kw: ran.append(list(args)) or _done(
        '{"ok": false, "error": "fake builder"}\n'))
    res = bpt.build_board_from_netlist(netlist, 30, 20, out_pcb=out, footprints_dir=str(tmp_path))
    assert res["released_boards"] == [out] and dirty.closed
    assert len(ran) == 1 and ran[0][1].endswith("swig_build_board.py") and res["ok"] is False


# ============================================================================
# live: KiCad 9 build -> KiCad 11 headless route_check -> Freerouting fallback
# ============================================================================
def _skip_unless_live() -> None:
    """Skip unless the KiCad 11 nightly api-server, a KiCad 9/10 pcbnew Python,
    Java and the Freerouting jar are all here (probed only when the live test
    runs, so collecting the unit tier never probes KiCad)."""
    if not BoardApi.available():
        pytest.skip(BoardApi.unavailable_reason() or "board API unavailable")
    if not tc.kicad_python():
        pytest.skip(f"no KiCad pcbnew Python: {tc.kicad_python_error()}")
    if not tc.find_java() or not tc.freerouting_jar():
        pytest.skip("Java or the Freerouting jar is missing")


def _kicad_cli_pids() -> set[int] | None:
    """kicad-cli.exe PIDs this test run owns (Windows; other agents' servers on
    the same machine are not ours); None off Windows."""
    return owned_pids("kicad-cli.exe")


def _java_pids() -> set[int] | None:
    """java.exe PIDs this test run owns (Freerouting); None off Windows."""
    return owned_pids("java.exe")


def _live_lm358(tmp_path: Path, monkeypatch) -> str:
    """A fresh registry, the real GUI probe, and examples/lm358_buffer built by
    KiCad 9's pcbnew in tmp_path (its 1 A outputs get net classes)."""
    _skip_unless_live()
    monkeypatch.setattr(bs, "_BOARDS", {})
    monkeypatch.setattr(bs, "_PROGRESS", {})
    monkeypatch.setattr(ipc, "editor_holding", REAL_EDITOR_HOLDING)
    design = json.loads((EXAMPLES / "lm358_buffer" / "netlist.json").read_text(encoding="utf-8"))
    design["net_specs"] = {"OUT_A": {"voltage": 5, "current": 1.0},
                           "OUT_B": {"voltage": 5, "current": 1.0}}
    netlist = _write(tmp_path / "netlist.json", json.dumps(design, indent=2))
    pcb = str(tmp_path / "netlist.kicad_pcb")
    built = bpt.build_board_from_netlist(netlist, 50, 25, out_pcb=pcb)
    assert built.get("ok"), built
    assert tc.is_pre_k11(pcb) and "NC_OUT_A" in _sib(pcb, "", ".kicad_pro").read_text("utf-8")
    return pcb


def test_live_k11_save_snapshots_then_freerouting_routes_the_snapshot(tmp_path, monkeypatch):
    pcb = _live_lm358(tmp_path, monkeypatch)
    snap = tc.pre_k11_snapshot_path(pcb)
    before = _kicad_cli_pids()
    try:
        opened = brt.open_board(pcb, mode="headless")
        assert opened["ok"] and opened["mode"] == "headless", opened
        assert "pre_k11_snapshot" not in opened and not Path(snap).exists()    # open never writes
        check = brt.route_check(pcb)                        # the refill dirties it: saved
        assert check["ok"] is False and check["saved"] is True, check          # nothing routed
        assert check["pre_k11_snapshot_taken"] == snap and check["pre_k11_snapshot"] == snap
        assert check["progress"]["rounds"] == 1 and check["stalled"] is False
        assert tc.is_pre_k11(snap) and not tc.is_pre_k11(pcb)
        assert "NC_OUT_A" in _sib(pcb, ".k9", ".kicad_pro").read_text("utf-8")
        assert brt.list_open_boards()["boards"][0]["pre_k11_snapshot"] == snap
        again = brt.route_check(pcb)                        # a second save takes no new snapshot
        assert again["saved"] is True and "pre_k11_snapshot_taken" not in again
        routed = bpt.autoroute_board(pcb, route_timeout=600, from_snapshot=True)
        assert routed["ok"] is True and routed["fallback_from_snapshot"] is True, routed
        assert routed["released_boards"] == [pcb] and routed["unconnected"] == 0
        assert brt.list_open_boards()["boards"] == []
    finally:
        brt.close_all()
    assert tc.is_pre_k11(pcb) and tc.swig_readable(pcb) is None
    assert not tc.is_pre_k11(tc.agent_copy_path(pcb))
    drc = tc.drc_counts(pcb)
    assert drc["errors"] == 0 and drc["unconnected"] == 0, drc
    assert "NC_OUT_A" in _sib(pcb, "", ".kicad_pro").read_text("utf-8")
    if before is not None:
        assert _kicad_cli_pids() <= before, "kicad-cli api-server leaked"


def test_live_stall_then_two_fallbacks_keep_the_first_agent_copy(tmp_path, monkeypatch):
    """ROADMAP D5 end to end: headless route_check x5 on the unrouted board
    reports the stall, Freerouting routes the snapshot, the agent saves
    through KiCad 11 again, and a second fallback keeps the first agent copy
    as X.agent.1.*; no kicad-cli api-server or java process is left."""
    pcb = _live_lm358(tmp_path, monkeypatch)
    lock = tmp_path / "~netlist.kicad_pro.lck"
    clis, javas = _kicad_cli_pids(), _java_pids()
    try:
        rounds = [brt.route_check(pcb) for _ in range(5)]          # nothing is routed: no gain
        assert [r["progress"]["rounds"] for r in rounds] == [1, 2, 3, 4, 5], rounds[-1]
        assert [r["stalled"] for r in rounds] == [False, False, False, True, True]
        assert "from_snapshot=true" in rounds[-1]["stall_note"]
        first = bpt.autoroute_board(pcb, route_timeout=600, from_snapshot=True)
        assert first["ok"] is True and first["fallback_from_snapshot"] is True, first
        assert first["released_boards"] == [pcb] and first["unconnected"] == 0
        agent = Path(first["agent_copy"])
        first_agent = agent.read_bytes()
        assert not tc.is_pre_k11(str(agent)) and not lock.exists()
        saved = brt.save_board(pcb) if brt.open_board(pcb, mode="headless")["ok"] else None
        assert saved and saved["saved"] is True, saved            # the routed board, now K11
        assert saved["pre_k11_snapshot_taken"] == tc.pre_k11_snapshot_path(pcb)
        assert not tc.is_pre_k11(pcb)
        second = bpt.autoroute_board(pcb, route_timeout=600, from_snapshot=True)
        assert second["ok"] is True and second["fallback_from_snapshot"] is True, second
        assert second["released_boards"] == [pcb] and second["unconnected"] == 0
    finally:
        brt.close_all()
    kept = _sib(pcb, ".agent.1", ".kicad_pcb")
    assert kept.read_bytes() == first_agent                       # the first agent copy survives
    assert _sib(pcb, ".agent.1", ".kicad_pro").is_file() and agent.read_bytes() != first_agent
    assert tc.is_pre_k11(pcb) and not lock.exists()
    drc = tc.drc_counts(pcb)
    assert drc["errors"] == 0 and drc["unconnected"] == 0, drc
    if clis is not None:
        assert _kicad_cli_pids() <= clis, "kicad-cli api-server leaked"
        assert _java_pids() <= javas, "java (Freerouting) leaked"
