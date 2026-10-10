"""server/tools/swig_export_dsn.py: safe load, the polygon-board DSN outline
rewrite and its one-line JSON report (F3-17).

The script exports a .kicad_pcb to Specctra DSN for Freerouting. A polygon
board (any cutout, any arc, more than 4 outline vertices or a non-axis-aligned
edge) gets dsn_outline.rewrite_outline (F3-02): the boundary shrinks and every
hole keepout grows by delta = copper edge clearance - smallest DSN clearance +
0.02 mm, so Freerouting keeps the copper-to-edge clearance KiCad's DRC wants.
A rectangular board's DSN is KiCad's raw export, byte for byte.

Unit tier (dev venv, no pcbnew): polygon_reasons; main()'s argument and
missing-board refusals; the whole flow with pcbnew's export and board_outline
faked (rewrite written atomically, rect untouched, an inset failure keeps the
raw export with a warning, a frame mismatch / parse error / failed export is
ok false + rc 1 and removes the DSN); the source contract (stdlib + pcbnew,
Python 3.11, small functions).

Live tier (KiCad's bundled Python; KiCad 9.0.4 locally and 10.0.5 in
kicad-eda:wave1, RULES.md "K10"; skipped without a KiCad Python that imports
pcbnew):
  (a) a missing or unreadable board: rc 1 with a JSON error, never a segfault;
  (b) the T10 board (tests/fixtures/outline/t10_board.json, built by
      build_board_from_netlist): the DSN boundary bbox shrinks by 0.12 mm per
      side and the 2.5 mm circle keepout grows by 0.12 mm;
  (c) three T10 builds routed by board_pipeline_tools.autoroute_board
      (Freerouting, dsn_seed 0 / 1 / 2): 0 copper_edge_clearance violations;
      DRC errors / unconnected per build are recorded (skipped without Java or
      the Freerouting jar);
  (d) examples/lm358_buffer (a rectangle): the DSN is byte-identical to
      KiCad's raw export.
"""
from __future__ import annotations

import ast
import json
import math
import os
import shutil
import subprocess
from pathlib import Path

import pytest

import board_outline as bo
import dsn_outline as do
import dsn_sexpr as ds
import outline as ol
import swig_export_dsn as sed
import toolchain as tc
from fixtures import swig_python as sp

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
TOOLS = ROOT / "server" / "tools"
SCRIPT = TOOLS / "swig_export_dsn.py"
FIXTURE = HERE / "fixtures" / "dsn" / "poly_outline_k9.dsn.txt"
T10_NETLIST = HERE / "fixtures" / "outline" / "t10_board.json"
LM358 = ROOT / "examples" / "lm358_buffer" / "netlist.json"
T10 = ol.from_board_block(sp.T10_BOARD)
T10_ORIGIN = (118.5, 85.0)          # the fixture's board-frame origin in sheet mm
EDGE_MM = 0.3                       # FAB_RULES_MM m_CopperEdgeClearance
DELTA_UM = 120.0                    # 0.3 - 0.2 (class clearance) + 0.02 (margin)
HOLE = (30.0, 20.0, 2.5)            # T10's round cutout (board frame, mm)
EXPORT_TIMEOUT_S = 180
ROUTE_TIMEOUT_S = 600
SEGFAULT_RCS = {139, -11, 3221225477}   # Linux signal 11 (both forms), Windows 0xC0000005


# ----------------------------------------------------------------------------
# DSN readers (dsn_sexpr, never regexes)
# ----------------------------------------------------------------------------
def _structure(text: str) -> ds.Node:
    return ds.child(ds.parse_nodes(text)[0], "structure")


def _points(shape: ds.Node) -> list[tuple[float, float]]:
    vals = [float(t.value) for t in shape.items[2:]]
    return list(zip(vals[::2], vals[1::2], strict=True))


def _boundary(text: str) -> list[tuple[float, float]]:
    (b,) = ds.find_all(_structure(text), "boundary")
    return _points(ds.child(b, "path"))


def _keepouts(text: str) -> list[list[tuple[float, float]]]:
    return [_points(ds.child(k, "polygon")) for k in ds.find_all(_structure(text), "keepout")]


def _box(pts: list[tuple[float, float]]) -> tuple[float, float, float, float]:
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return (min(xs), min(ys), max(xs), max(ys))


def _hole_centre_dsn(origin: tuple[float, float]) -> tuple[float, float]:
    return ((HOLE[0] + origin[0]) * 1000.0, -(HOLE[1] + origin[1]) * 1000.0)


def _ring_near(text: str, c: tuple[float, float]) -> list[tuple[float, float]]:
    def centre(ring: list[tuple[float, float]]) -> tuple[float, float]:
        return (sum(p[0] for p in ring) / len(ring), sum(p[1] for p in ring) / len(ring))
    return min(_keepouts(text), key=lambda ring: math.dist(centre(ring), c))


def _fixture_text(newline: str) -> str:
    lf = FIXTURE.read_bytes().decode("utf-8").replace("\r\n", "\n")
    return lf.replace("\n", newline)


def _last_line_json(out: str) -> dict:
    lines = [ln for ln in out.splitlines() if ln.strip()]
    assert len(lines) == 1, f"expected ONE JSON line, got {lines!r}"
    return json.loads(lines[0])


# ----------------------------------------------------------------------------
# Unit tier: which boards are polygon boards
# ----------------------------------------------------------------------------
@pytest.mark.parametrize("block,want", [
    ({"w": 50, "h": 25}, []),
    ({"outline": {"loop": [[0, 0], [40, 0], [40, 30], [0, 30]]}}, []),
    ({"w": 50, "h": 25, "corner_radius": 3}, ["arcs", "8 vertices"]),
    ({"w": 50, "h": 25, "cutouts": [{"circle": [10, 10, 2]}]}, ["1 cutout"]),
    ({"outline": {"loop": [[0, 0], [32, 0], [32, 20], [60, 20], [60, 45], [0, 45]]}},
     ["6 vertices"]),
    ({"outline": {"loop": [[0, 10], [10, 0], [20, 10], [10, 20]]}},
     ["a non-axis-aligned edge"]),
    ({"outline": {"loop": [[0, 0], [40, 0], [20, 30]]}}, ["a non-axis-aligned edge"]),
])
def test_polygon_reasons(block: dict, want: list[str]) -> None:
    assert sed.polygon_reasons(ol.from_board_block(block)) == want


def test_polygon_reasons_of_the_t10_board_and_a_round_board() -> None:
    assert sed.polygon_reasons(T10) == ["2 cutouts", "arcs", "8 vertices"]
    disc = ol.Outline(ol.Circle(10.0, 10.0, 10.0))
    assert sed.polygon_reasons(disc) == ["a circular outline"]


def test_a_rectangle_within_a_nanometre_is_still_a_rectangle() -> None:
    eps = 4e-7                                  # below 1 nm: KiCad's own resolution
    rect = ol.Outline(ol.Loop(((0.0, 0.0, 0.0), (40.0, eps, 0.0), (40.0, 30.0, 0.0),
                               (eps, 30.0, 0.0))))
    assert sed.polygon_reasons(rect) == []


# ----------------------------------------------------------------------------
# Unit tier: main() refusals that need no pcbnew
# ----------------------------------------------------------------------------
@pytest.mark.parametrize("argv", [[], ["only.kicad_pcb"], ["a.kicad_pcb", "b.dsn", "c", "d"]])
def test_usage_is_rc_2_with_one_json_line(argv: list[str], capsys) -> None:
    assert sed.main(argv) == 2
    res = _last_line_json(capsys.readouterr().out)
    assert res["ok"] is False and res["stage"] == "usage" and "usage" in res["error"]


def test_the_dsn_may_not_overwrite_the_board(tmp_path: Path, capsys) -> None:
    pcb = tmp_path / "b.kicad_pcb"
    pcb.write_text("(kicad_pcb)", encoding="utf-8")
    for dsn in (pcb, tmp_path / "other.kicad_pcb"):
        assert sed.main([str(pcb), str(dsn)]) == 2
        assert _last_line_json(capsys.readouterr().out)["stage"] == "usage"
    assert pcb.read_text(encoding="utf-8") == "(kicad_pcb)"


def test_a_missing_board_is_a_json_error_before_pcbnew(tmp_path: Path, capsys) -> None:
    dsn = tmp_path / "out.dsn"
    assert sed.main([str(tmp_path / "missing.kicad_pcb"), str(dsn)]) == 1
    res = _last_line_json(capsys.readouterr().out)
    assert res["ok"] is False and res["stage"] == "load" and "not found" in res["error"]
    assert res["dsn"] == str(dsn) and not dsn.exists()


# ----------------------------------------------------------------------------
# Unit tier: the flow with pcbnew's export and board_outline faked
# ----------------------------------------------------------------------------
class FakePcbnew:
    """pcbnew stand-in: ExportSpecctraDSN writes `text` (None: writes
    nothing) and returns `result`, or raises `raises`."""

    def __init__(self, text: str | None, result: object = True,
                 raises: Exception | None = None) -> None:
        self.text, self.result, self.raises = text, result, raises
        self.calls: list[tuple[object, str]] = []

    def ExportSpecctraDSN(self, board: object, path: str) -> object:  # noqa: N802 (pcbnew API)
        self.calls.append((board, path))
        if self.raises is not None:
            raise self.raises
        if self.text is not None:
            Path(path).write_bytes(self.text.encode("utf-8"))
        return self.result


@pytest.fixture
def fake(monkeypatch, tmp_path: Path, capsys):
    """fake(text, outline=T10, origin=T10_ORIGIN, edge=0.3, **pcbnew_kw) ->
    (rc, JSON line, dsn path, the exported bytes) of main() on a fake board
    whose export writes `text`; `outline` / `edge` may be an exception to
    raise instead."""
    board = object()

    def run(text: str | None, outline: ol.Outline | Exception = T10,
            origin: tuple[float, float] = T10_ORIGIN, edge: float | Exception = EDGE_MM,
            **kw: object) -> tuple[int, dict, Path, bytes]:
        pcbnew = FakePcbnew(text, **kw)

        def from_board(b: object) -> tuple[ol.Outline, tuple[float, float]]:
            assert b is board
            if isinstance(outline, Exception):
                raise outline
            return outline, origin

        def edge_mm(b: object) -> float:
            if isinstance(edge, Exception):
                raise edge
            return edge

        monkeypatch.setattr(sed, "pcbnew", pcbnew)
        monkeypatch.setattr(sed.bo, "load_board", lambda path: board)
        monkeypatch.setattr(sed.bo, "outline_from_board", from_board)
        monkeypatch.setattr(sed.bo, "edge_clearance_mm", edge_mm)
        dsn = tmp_path / "board.dsn"
        rc = sed.main([str(tmp_path / "board.kicad_pcb"), str(dsn)])
        assert pcbnew.calls == [(board, str(dsn))]
        return rc, _last_line_json(capsys.readouterr().out), dsn, (text or "").encode("utf-8")

    return run


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_polygon_board_is_rewritten_atomically(fake, tmp_path: Path, newline) -> None:
    text = _fixture_text(newline)
    rc, res, dsn, _raw = fake(text)
    want, report = do.rewrite_outline(text, T10, T10_ORIGIN, EDGE_MM)
    assert rc == 0 and res["ok"] is True and res["dsn"] == str(dsn)
    assert res["outline_rewrite"] == report and report["changed"] is True
    assert res["outline_rewrite"]["delta"] == pytest.approx(0.12)
    assert res["polygon"] == ["2 cutouts", "arcs", "8 vertices"] and res["warnings"] == []
    assert dsn.read_bytes() == want.encode("utf-8")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["board.dsn"]     # no temp left


def test_rect_board_dsn_is_left_byte_identical(fake) -> None:
    rect = ol.from_board_block({"w": 50, "h": 25})
    rc, res, dsn, raw = fake(_fixture_text("\r\n"), outline=rect, origin=(123.5, 92.5))
    assert rc == 0 and res["ok"] is True
    assert res["outline_rewrite"] == sed.RECT_SKIP == "skipped: rectangular board"
    assert res["polygon"] == [] and res["warnings"] == []
    assert dsn.read_bytes() == raw


def test_an_inset_failure_keeps_the_raw_export_with_a_warning(fake) -> None:
    rc, res, dsn, raw = fake(_fixture_text("\n"), edge=25.0)    # consumes the board
    assert rc == 0 and res["ok"] is True and dsn.read_bytes() == raw
    assert res["outline_rewrite"].startswith("skipped: ")
    (warning,) = res["warnings"]
    assert "consumes the whole board" in warning and "raw export" in warning
    assert "G4" in warning


def test_delta_at_or_below_zero_reports_and_keeps_the_file(fake) -> None:
    rc, res, dsn, raw = fake(_fixture_text("\n"), edge=0.1)
    assert rc == 0 and res["ok"] is True and dsn.read_bytes() == raw
    assert res["outline_rewrite"]["changed"] is False and "<= 0" in res["outline_rewrite"]["reason"]


def test_an_unreadable_outline_keeps_the_raw_export_with_a_warning(fake) -> None:
    err = bo.OutlineBuildError("Edge.Cuts does not form one closed outline: open at (1, 2)")
    rc, res, dsn, raw = fake(_fixture_text("\n"), outline=err)
    assert rc == 0 and res["ok"] is True and dsn.read_bytes() == raw
    assert res["outline_rewrite"] == "skipped: the board outline cannot be read"
    (warning,) = res["warnings"]
    assert "open at (1, 2)" in warning


@pytest.mark.parametrize("case,stage,needle", [
    ("frame", "outline_rewrite", "frame mismatch"),
    ("parse", "outline_rewrite", "DsnError"),
    ("structure", "outline_rewrite", "structure"),
    ("false", "export", "returned False"),
    ("raises", "export", "OSError: disk full"),
    ("nofile", "export", "wrote no file"),
])
def test_failures_are_ok_false_rc_1_and_leave_no_dsn(fake, tmp_path: Path, case: str,
                                                     stage: str, needle: str) -> None:
    text = _fixture_text("\n")
    kwargs = {"frame": {"origin": (0.0, 0.0)}, "parse": {}, "structure": {},
              "false": {"result": False}, "raises": {"raises": OSError("disk full")},
              "nofile": {}}[case]
    body = {"parse": "(pcb x (structure (boundary", "nofile": None,
            "structure": text.replace("(boundary", "(boundory")}.get(case, text)
    rc, res, dsn, _raw = fake(body, **kwargs)
    assert rc == 1 and res["ok"] is False and res["stage"] == stage, res
    assert needle in res["error"], res["error"]
    assert res["dsn_removed"] is (case not in ("nofile", "raises"))      # a file was there
    assert res["pcb"].endswith("board.kicad_pcb")
    assert not dsn.exists() and list(tmp_path.iterdir()) == []


def test_an_unexpected_error_after_the_export_is_internal(fake, tmp_path: Path) -> None:
    rc, res, dsn, _raw = fake(_fixture_text("\n"), edge=AttributeError("no m_CopperEdgeClearance"))
    assert rc == 1 and res["ok"] is False and res["stage"] == "internal", res
    assert res["error"] == "AttributeError: no m_CopperEdgeClearance" and res["dsn_removed"]
    assert not dsn.exists() and list(tmp_path.iterdir()) == []


def test_write_atomic_keeps_the_old_file_when_the_replace_fails(monkeypatch,
                                                               tmp_path: Path) -> None:
    path = tmp_path / "x.dsn"
    path.write_bytes(b"old\r\n")
    sed.write_atomic(str(path), "new\r\n")
    assert path.read_bytes() == b"new\r\n" and [p.name for p in tmp_path.iterdir()] == ["x.dsn"]

    def boom(src: str, dst: str) -> None:
        raise OSError("locked")

    monkeypatch.setattr(sed.os, "replace", boom)
    with pytest.raises(sed.ExportError, match="locked"):
        sed.write_atomic(str(path), "newer")
    assert path.read_bytes() == b"new\r\n" and [p.name for p in tmp_path.iterdir()] == ["x.dsn"]


# ----------------------------------------------------------------------------
# Unit tier: source contract
# ----------------------------------------------------------------------------
def test_script_is_stdlib_plus_pcbnew_py311_and_small() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    tree = ast.parse(source, feature_version=(3, 11))
    assert len(source.splitlines()) < 800
    ours = {"kicad_quiet", "pcbnew", "board_outline", "dsn_outline", "dsn_sexpr", "outline"}
    stdlib = {"__future__", "json", "math", "os", "sys", "tempfile", "traceback",
              "collections", "pathlib", "typing"}
    mods = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import)
            for a in n.names}
    mods |= {n.module.split(".")[0] for n in ast.walk(tree)
             if isinstance(n, ast.ImportFrom) and n.module}
    assert mods <= ours | stdlib, mods - ours - stdlib
    for fn in (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)):
        assert fn.end_lineno - fn.lineno < 50, fn.name
        assert fn.returns is not None, fn.name
    assert "ExportSpecctraDSN(dsn)" not in source           # never the GUI-board overload


# ----------------------------------------------------------------------------
# Live tier
# ----------------------------------------------------------------------------
RAW_EXPORT = sp.PREAMBLE + r"""
import board_outline as bo
board = bo.load_board(sys.argv[1])
print(json.dumps({"ok": bool(pcbnew.ExportSpecctraDSN(board, sys.argv[2]))}))
"""


@pytest.fixture(scope="module")
def kpy() -> str:
    return sp.require_kicad_python()


def run_script(kpy: str, pcb: Path, dsn: Path) -> tuple[int, dict, str]:
    """(rc, its JSON line, stdout + stderr) of swig_export_dsn.py, stdin closed."""
    proc = subprocess.run([kpy, str(SCRIPT), str(pcb), str(dsn)], capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          stdin=subprocess.DEVNULL, timeout=EXPORT_TIMEOUT_S)
    return proc.returncode, tc.last_json(proc.stdout) or {}, proc.stdout + proc.stderr


def raw_and_script(kpy: str, pcb: Path, folder: Path) -> tuple[bytes, bytes, dict]:
    """(KiCad's raw export, the script's DSN, its JSON), both written to the
    SAME path (KiCad puts the file name in the DSN's first line)."""
    dsn = folder / "board.dsn"
    assert sp.run_child(RAW_EXPORT, [pcb, dsn], folder)["ok"] is True
    raw = dsn.read_bytes()
    dsn.unlink()
    rc, res, log = run_script(kpy, pcb, dsn)
    assert rc == 0 and res.get("ok") is True, log
    return raw, dsn.read_bytes(), res


def build(netlist: Path, folder: Path, w: float = 0, h: float = 0) -> Path:
    """build_board_from_netlist on a copy of `netlist` in `folder` (the
    pipeline's own build: footprints and net classes as in a real run)."""
    import board_pipeline_tools as bpt
    folder.mkdir(parents=True, exist_ok=True)
    copy = folder / "netlist.json"
    shutil.copyfile(netlist, copy)
    pcb = folder / "board.kicad_pcb"
    built = bpt.build_board_from_netlist(str(copy), w, h, out_pcb=str(pcb))
    assert built.get("ok"), built
    return pcb


def test_live_a_missing_or_unreadable_board_is_a_json_error(kpy: str, tmp_path: Path) -> None:
    dsn = tmp_path / "out.dsn"
    rc, res, log = run_script(kpy, tmp_path / "missing.kicad_pcb", dsn)
    assert rc not in SEGFAULT_RCS and rc == 1, log
    assert res["ok"] is False and res["stage"] == "load" and "not found" in res["error"], log
    assert not dsn.exists()
    junk = tmp_path / "junk.kicad_pcb"
    junk.write_text("(kicad_pcb (version 20241229) (generator", encoding="utf-8")
    rc, res, log = run_script(kpy, junk, dsn)
    assert rc not in SEGFAULT_RCS and rc == 1, log
    assert res["ok"] is False and res["stage"] == "load" and str(junk) in res["error"], log
    assert not dsn.exists()


@pytest.fixture(scope="module")
def t10_pcb(kpy: str, tmp_path_factory) -> Path:
    return build(T10_NETLIST, tmp_path_factory.mktemp("t10"))


def test_live_b_t10_boundary_shrinks_and_hole_grows_by_delta(kpy: str, t10_pcb: Path,
                                                            tmp_path: Path) -> None:
    raw_b, new_b, res = raw_and_script(kpy, t10_pcb, tmp_path)
    raw, new = raw_b.decode("utf-8"), new_b.decode("utf-8")
    rep = res["outline_rewrite"]
    assert rep["changed"] is True and rep["delta"] == pytest.approx(0.12, abs=1e-9)
    assert (rep["min_clearance_mm"], rep["edge_clearance_mm"]) == (0.2, EDGE_MM)
    assert rep["holes_replaced"] == 2 and rep["holes_unmatched"] == 0, rep
    assert res["polygon"] == ["2 cutouts", "arcs", "8 vertices"] and res["warnings"] == []
    rx0, ry0, rx1, ry1 = _box(_boundary(raw))
    want = (rx0 + DELTA_UM, ry0 + DELTA_UM, rx1 - DELTA_UM, ry1 - DELTA_UM)
    assert _box(_boundary(new)) == pytest.approx(want, abs=0.5)
    origin = (rx0 / 1000.0, -ry1 / 1000.0)
    assert origin == pytest.approx(T10_ORIGIN, abs=1e-3)
    c = _hole_centre_dsn(origin)
    before = [math.dist(p, c) for p in _ring_near(raw, c)]
    after = [math.dist(p, c) for p in _ring_near(new, c)]
    assert min(before) == pytest.approx(2500.0, abs=0.5) and max(before) < 2510.0
    assert min(after) == pytest.approx(2500.0 + DELTA_UM, abs=0.5)
    assert max(after) == pytest.approx(2500.0 + DELTA_UM, abs=0.5)
    for name in ("placement", "library", "network", "wiring"):
        node_raw, node_new = (ds.child(ds.parse_nodes(t)[0], name) for t in (raw, new))
        assert raw[node_raw.start:node_raw.end] == new[node_new.start:node_new.end], name


def _require_freerouting() -> None:
    if not tc.find_java():
        pytest.skip("java not found (Freerouting needs a JRE)")
    if not tc.freerouting_jar(""):
        pytest.skip("Freerouting jar not found in server/tools or $FREEROUTING_JAR")


def test_live_c_three_freerouting_builds_keep_the_edge_clearance(kpy: str, tmp_path: Path,
                                                                record_property) -> None:
    _require_freerouting()
    import board_pipeline_tools as bpt
    rows = []
    for seed in range(3):
        pcb = build(T10_NETLIST, tmp_path / f"build{seed}")
        routed = bpt.autoroute_board(str(pcb), route_timeout=ROUTE_TIMEOUT_S, dsn_seed=seed)
        assert routed.get("step") is None and routed.get("tracks"), routed
        dsn = Path(routed["dsn"]).read_bytes().decode("utf-8")
        x0, y0, x1, y1 = _box(_boundary(dsn))
        assert (x1 - x0, y1 - y0) == pytest.approx((60000 - 2 * DELTA_UM, 40000 - 2 * DELTA_UM),
                                                    abs=1.0)
        drc = tc.drc_counts(str(pcb))
        assert "error" not in drc, drc
        rows.append({"build": seed, "dsn_seed": seed, "dsn_sha256": routed["dsn_sha256"][:12],
                     "tracks": routed["tracks"], "vias": routed["vias"],
                     "drc_errors": drc["errors"], "unconnected": drc["unconnected"],
                     "types": drc["types"]})
    record_property("t10_freerouting", rows)
    print("T10 Freerouting builds:", json.dumps(rows))
    assert [r["types"].get("copper_edge_clearance", 0) for r in rows] == [0, 0, 0], rows


def test_live_d_rect_board_dsn_is_byte_identical_to_the_raw_export(kpy: str,
                                                                  tmp_path: Path) -> None:
    pcb = build(LM358, tmp_path / "lm358", 50, 25)
    raw, new, res = raw_and_script(kpy, pcb, tmp_path)
    assert res["outline_rewrite"] == "skipped: rectangular board" and res["polygon"] == []
    assert new == raw and len(raw) > 1000
    assert os.path.getsize(tmp_path / "board.dsn") == len(raw)
