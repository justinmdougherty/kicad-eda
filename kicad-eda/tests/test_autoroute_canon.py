"""F5-02 wiring: autoroute_board canonicalises the exported Specctra DSN before
Freerouting, and add_ground_stitching releases a board-API session first.

autoroute_board: the DSN Freerouting reads is dsn_canon.canonicalize of the
export (two UUID orders route the same bytes), dsn_seed reaches
canonicalize_file, the result carries dsn_sha256 / dsn_seed, a garbage DSN
stops at step "dsn_canon" before Freerouting, a failed export never routes a
stale DSN, a bad dsn_seed is refused before anything runs.
add_ground_stitching: an idle session holding the board is closed
(released_boards), a dirty one refuses before any file is touched.

Faked subprocesses (tc.run) and a stub registry session, as in
tests/test_snapshot.py; no KiCad needed.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Callable

import pytest

import board_pipeline_tools as bpt
import board_route_tools as brt
import dsn_canon as dc
import kicad_env as env
import kicad_ipc as ipc
import toolchain as tc

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "dsn"
K9_BOARD = ('(kicad_pcb\n\t(version 20241229)\n\t(generator "pcbnew")\n'
            '\t(generator_version "9.0")\n\t(general\n\t\t(thickness 1.6)\n\t)\n)\n')


def _raw(name: str) -> str:
    return (FIXTURES / f"{name}.dsn.sexpr").read_text(encoding="utf-8")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _install(base: Path, version: str, vt: tuple[int, int, int], *,
             python: bool) -> env.KiCadInstall:
    root = base / version
    return env.KiCadInstall(
        version=version, version_tuple=vt, root=root, kicad_cli=root / "bin" / "kicad-cli.exe",
        python=(root / "bin" / "python.exe") if python else None,
        share=root / "share" / "kicad", has_api_server=vt >= env.API_SERVER_MIN_VERSION)


@pytest.fixture
def machine(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> env.KiCadInstall:
    """KiCad 9.0.4 with its pcbnew Python, Java and a jar; no GUI."""
    stable = _install(tmp_path / "kicad", "9.0", (9, 0, 4), python=True)
    monkeypatch.setattr(env, "discover", lambda refresh=False: [stable])
    monkeypatch.setenv("DISPLAY", ":99")
    monkeypatch.setattr(tc, "find_java", lambda: "java")
    monkeypatch.setattr(tc, "freerouting_jar", lambda explicit="": "freerouting-1.9.0.jar")
    monkeypatch.setattr(ipc, "write_guard", lambda path: None)
    return stable


@pytest.fixture
def registry(monkeypatch: pytest.MonkeyPatch) -> Callable[..., object]:
    """An isolated board registry; hold(pcb, modified=...) puts a stub session on it."""
    monkeypatch.setattr(brt, "_BOARDS", {})
    monkeypatch.setattr(brt, "_PROGRESS", {})

    class StubSession:
        def __init__(self, path: str, modified: bool) -> None:
            self.path, self.mode, self.is_modified, self.closed = path, "headless", modified, False

        def close(self) -> None:
            self.closed = True

        def discard(self) -> None:
            self.closed = True

    def hold(pcb: str, modified: bool = False) -> StubSession:
        stub = StubSession(pcb, modified)
        brt._BOARDS[brt._key(pcb)] = stub
        return stub
    return hold


@pytest.fixture
def pcb(tmp_path: Path) -> str:
    board = tmp_path / "netlist.kicad_pcb"
    board.write_text(K9_BOARD, encoding="utf-8")
    return str(board)


def _done(stdout: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")


class Chain:
    """tc.run standing in for DSN export -> Freerouting -> SES import -> DRC
    (and emc_stitch). `export` is the DSN text the fake exporter writes (None:
    it writes nothing); `routed` is every DSN text Freerouting was handed."""

    def __init__(self, export: str | None, drc_clean: bool = True, ses: bool = True) -> None:
        self.export, self.drc_clean, self.ses = export, drc_clean, ses
        self.calls: list[list[str]] = []
        self.routed: list[str] = []

    def __call__(self, args: list, **kw: object) -> subprocess.CompletedProcess:
        args = [str(a) for a in args]
        self.calls.append(args)
        joined = " ".join(args)
        if "swig_export_dsn.py" in joined and self.export is not None:
            Path(args[3]).write_text(self.export, encoding="utf-8")
        elif "-jar" in args:
            self.routed.append(Path(args[args.index("-de") + 1]).read_text(encoding="utf-8"))
            if self.ses:
                Path(args[args.index("-do") + 1]).write_text("(session out)", encoding="utf-8")
        elif "swig_import_ses.py" in joined:
            return _done('{"ok": true, "tracks": 21, "vias": 6, "warnings": []}\n')
        elif "emc_stitch.py" in joined:
            return _done('{"ok": true, "vias_added": 12}\n')
        elif "drc" in args:
            unconnected = [] if self.drc_clean else [{"type": "unconnected_items"}]
            Path(args[args.index("-o") + 1]).write_text(
                json.dumps({"violations": [], "unconnected_items": unconnected}),
                encoding="utf-8")
        return _done()

    def steps(self) -> list[str]:
        names = ("swig_export_dsn.py", "-jar", "swig_import_ses.py", "emc_stitch.py", "drc")
        return [next((n for n in names if n in a or any(n in x for x in a)), a[0])
                for a in self.calls]


def _chain(monkeypatch: pytest.MonkeyPatch, export: str | None, **kw: bool) -> Chain:
    chain = Chain(export, **kw)
    monkeypatch.setattr(tc, "run", chain)
    return chain


def _no_run(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tc, "run", lambda args, **kw: pytest.fail(f"ran {list(args)[:2]}"))


# ----------------------------------------------------------------------------
# autoroute_board: the canonical DSN
# ----------------------------------------------------------------------------
def test_freerouting_reads_the_canonical_dsn(machine, pcb, monkeypatch) -> None:
    raw = _raw("buck_r01")
    chain = _chain(monkeypatch, raw)
    res = bpt.autoroute_board(pcb)
    canonical = dc.canonicalize(raw, name="netlist.dsn")
    assert res["ok"] is True and res["tracks"] == 21 and res["unconnected"] == 0, res
    assert chain.routed == [canonical] and canonical != raw
    assert Path(res["dsn"]).read_bytes() == canonical.encode("utf-8")      # on disk too
    assert res["dsn_sha256"] == _sha(canonical) and res["dsn_seed"] == 0
    assert chain.steps() == ["swig_export_dsn.py", "-jar", "swig_import_ses.py", "drc"]


def test_two_uuid_orders_route_identical_bytes(machine, pcb, monkeypatch) -> None:
    hashes, routed = [], []
    for name in ("buck_r01", "buck_r05", "buck_r02"):
        chain = _chain(monkeypatch, _raw(name))
        res = bpt.autoroute_board(pcb, 600, "", 1500)                  # run_pipeline's call
        assert res["ok"] is True, res
        hashes.append(res["dsn_sha256"])
        routed += chain.routed
    assert len(set(hashes)) == 1 and len(set(routed)) == 1 and len(routed) == 3


def test_dsn_seed_reaches_canonicalize(machine, pcb, monkeypatch) -> None:
    raw = _raw("buck_r02")
    seen: list[tuple[str, int]] = []
    real = dc.canonicalize_file

    def spy(path: str, seed: int = 0, name: str | None = None) -> str:
        seen.append((Path(path).name, seed))
        return real(path, seed=seed, name=name)

    monkeypatch.setattr(dc, "canonicalize_file", spy)
    chain = _chain(monkeypatch, raw)
    base = bpt.autoroute_board(pcb, 600, "", 1500)
    alt = bpt.autoroute_board(pcb, 600, "", 1500, dsn_seed=2)
    assert seen == [("netlist.dsn", 0), ("netlist.dsn", 2)]
    assert chain.routed == [dc.canonicalize(raw, name="netlist.dsn"),
                            dc.canonicalize(raw, seed=2, name="netlist.dsn")]
    assert (base["dsn_seed"], alt["dsn_seed"]) == (0, 2)
    assert alt["dsn_sha256"] == _sha(chain.routed[1]) != base["dsn_sha256"]


def test_a_garbage_dsn_stops_at_dsn_canon(machine, pcb, monkeypatch, tmp_path) -> None:
    stale_ses = tmp_path / "netlist.ses"
    stale_ses.write_text("(session old)", encoding="utf-8")
    chain = _chain(monkeypatch, 'not a DSN "at all')
    res = bpt.autoroute_board(pcb)
    assert res["ok"] is False and res["step"] == "dsn_canon", res
    assert "DsnError" in res["reason"] and res["dsn_seed"] == 0
    assert chain.steps() == ["swig_export_dsn.py"]                        # never routed
    assert Path(res["dsn"]).read_text(encoding="utf-8") == 'not a DSN "at all'   # kept as is


def test_a_failed_export_never_routes_a_stale_dsn(machine, pcb, monkeypatch, tmp_path) -> None:
    (tmp_path / "netlist.dsn").write_text(dc.canonicalize(_raw("buck_r01")), encoding="utf-8")
    chain = _chain(monkeypatch, None)
    res = bpt.autoroute_board(pcb)
    assert res["ok"] is False and res["step"] == "dsn", res
    assert chain.steps() == ["swig_export_dsn.py"] and not (tmp_path / "netlist.dsn").exists()


@pytest.mark.parametrize("seed", [-1, True, "2", 1.5, None])
def test_a_bad_dsn_seed_is_refused_before_anything_runs(machine, pcb, monkeypatch,
                                                        seed: object) -> None:
    _no_run(monkeypatch)
    res = bpt.autoroute_board(pcb, dsn_seed=seed)  # type: ignore[arg-type]
    assert res["ok"] is False and "dsn_seed" in res["reason"], res


def test_route_and_import_failures_carry_the_dsn_hash(machine, pcb, monkeypatch) -> None:
    raw = _raw("buck_r05")
    _chain(monkeypatch, raw, ses=False)
    res = bpt.autoroute_board(pcb, dsn_seed=1)
    assert res["ok"] is False and res["step"] == "route", res
    assert res["dsn_sha256"] == _sha(dc.canonicalize(raw, seed=1, name="netlist.dsn"))
    assert res["dsn_seed"] == 1


def test_unrouted_result_keeps_the_dsn_fields(machine, pcb, monkeypatch) -> None:
    _chain(monkeypatch, _raw("buck_r01"), drc_clean=False)
    res = bpt.autoroute_board(pcb)
    assert res["ok"] is False and res["unconnected"] == 1 and res["dsn_seed"] == 0
    assert len(res["dsn_sha256"]) == 64


def test_the_tool_docstring_names_dsn_seed() -> None:
    doc = bpt.autoroute_board.__doc__ or ""
    assert "dsn_seed" in doc and "deterministic" in doc


# ----------------------------------------------------------------------------
# add_ground_stitching: board-API session release
# ----------------------------------------------------------------------------
def test_stitching_releases_an_idle_session(machine, pcb, registry, monkeypatch) -> None:
    idle = registry(pcb)
    brt._PROGRESS[brt._key(pcb)] = (3, 3)
    chain = _chain(monkeypatch, None)
    res = bpt.add_ground_stitching(pcb)
    assert res["ok"] is True and res["mode"] == "full" and res["vias_added"] == 12, res
    assert res["released_boards"] == [pcb] and idle.closed and brt._BOARDS == {}
    assert brt._PROGRESS == {}
    assert chain.steps() == ["drc", "emc_stitch.py", "drc"]


def test_stitching_refuses_a_dirty_session_before_touching_any_file(
        machine, pcb, registry, monkeypatch, tmp_path) -> None:
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir() if p.is_file()}
    dirty = registry(pcb, modified=True)
    _no_run(monkeypatch)
    res = bpt.add_ground_stitching(pcb)
    assert res["ok"] is False and res["blocking_board"] == pcb and not dirty.closed, res
    assert "unsaved changes" in res["reason"]
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir() if p.is_file()} == before


def test_stitching_without_a_session_reports_no_release(machine, pcb, registry,
                                                        monkeypatch) -> None:
    _chain(monkeypatch, None)
    res = bpt.add_ground_stitching(pcb, interior=False)
    assert res["ok"] is True and res["mode"] == "fence" and "released_boards" not in res


def test_a_refused_baseline_still_reports_the_release(machine, pcb, registry,
                                                      monkeypatch) -> None:
    idle = registry(pcb)
    _chain(monkeypatch, None, drc_clean=False)
    res = bpt.add_ground_stitching(pcb)
    assert res["ok"] is False and "not clean before stitching" in res["reason"], res
    assert res["released_boards"] == [pcb] and idle.closed
    assert not Path(pcb + ".prestitch").exists()
