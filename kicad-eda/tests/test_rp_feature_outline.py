"""pipeline/rp_feature_outline.py (F3-20): the outline feature's route-hash
inputs, outline board dims and the G5 mechanical exports, through the rp_hooks
hooks only (the G4 board-text copper check is in tests/test_board_text_copper.py).
mechanical_tools is faked here; its kicad-cli exports have their own tests."""
from __future__ import annotations

import copy
import json
import re
from pathlib import Path

import pytest

import dxf_io
import mechanical_tools as mt
import outline_resolve as orv
import rp_cli
import rp_feature_outline as rfo
import rp_hooks
import rp_route_hash
from fixtures import pipeline_world
from fixtures.swig_python import T10_BOARD

world = pipeline_world.world            # the fixture (an assignment: see pipeline_world)

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
EXAMPLE_NETLISTS = sorted(EXAMPLES.glob("*/netlist.json"))
HEX64 = re.compile(r"^[0-9a-f]{64}$")
GEOMETRY_KEYS = ("outline", "corner_radius", "cutouts", "datum")


@pytest.fixture
def use(monkeypatch):
    """use(*features): exactly these features; discovery comes back at teardown."""
    monkeypatch.setattr(rp_hooks, "_override", rp_hooks.selection())
    return lambda *features: rp_hooks.set_features(list(features))


def _design(board: dict | None = None, **extra) -> dict:
    out = copy.deepcopy(pipeline_world.DESIGN)
    if board is not None:
        out["board"] = copy.deepcopy(board)
    return {**out, **extra}


def _example(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _declares_geometry(design: dict) -> bool:
    board = design.get("board") or {}
    return any(board.get(key) for key in GEOMETRY_KEYS)


# --- the module ------------------------------------------------------------------------

def test_the_feature_is_discovered_with_its_order_keys_and_hooks():
    assert "outline" in rp_hooks.available()
    assert rp_hooks.load("outline") is rfo
    assert rfo.ORDER == 20 and rfo.ROUTE_HASH_COMPONENT_KEYS == ("edge",)
    hooks = {name for name in rp_hooks.HOOKS if callable(getattr(rfo, name, None))}
    assert hooks == {"route_hash_extra", "board_dims", "g4_inputs", "g5_verdict"}


def test_the_module_stays_small():
    lines = Path(rfo.__file__).read_text(encoding="utf-8").splitlines()
    assert len(lines) < 300


# --- route_hash_extra ------------------------------------------------------------------

@pytest.mark.parametrize("netlist", EXAMPLE_NETLISTS, ids=lambda p: p.parent.name)
def test_route_hash_extra_is_empty_for_every_legacy_example(netlist):
    design = _example(netlist)
    extra = rfo.route_hash_extra(design, netlist.parent)
    if _declares_geometry(design):        # an outline example (F3-21a/b) is fingerprinted
        assert HEX64.match(extra["outline_fp"])
    else:
        assert extra == {}


def test_every_example_today_is_legacy():
    assert EXAMPLE_NETLISTS
    assert {p.parent.name: rfo.route_hash_extra(_example(p), p.parent)
            for p in EXAMPLE_NETLISTS if not _declares_geometry(_example(p))} == \
        {p.parent.name: {} for p in EXAMPLE_NETLISTS if not _declares_geometry(_example(p))}


def test_route_hash_extra_fingerprints_an_outline_board(tmp_path):
    design = _design(T10_BOARD)
    want = orv.resolve_board(design, tmp_path).fingerprint
    assert rfo.route_hash_extra(design, tmp_path) == {"outline_fp": want}
    assert rfo.route_hash_extra(_design({"w": 40, "h": 30, "corner_radius": 2}), None) != {}
    assert rfo.route_hash_extra(_design({"w": 40, "h": 30}), None) == {}


def _dxf_netlist(tmp_path: Path) -> tuple[Path, dict]:
    dxf = dxf_io.write_outline_dxf(tmp_path / "mech" / "outline.dxf", T10_BOARD)
    return dxf, _design({"outline": {"dxf": "mech/outline.dxf"}})


def test_route_hash_extra_changes_when_the_dxf_bytes_change(tmp_path, use):
    dxf, design = _dxf_netlist(tmp_path)
    first = rfo.route_hash_extra(design, tmp_path)
    use(rfo)
    hash_first = rp_route_hash.route_hash(design, tmp_path)
    geometry = orv.resolve_board(design, tmp_path).block
    dxf.write_bytes(b"999\nre-exported by Fusion\n" + dxf.read_bytes())   # a DXF comment
    assert orv.resolve_board(design, tmp_path).block == geometry          # same geometry
    second = rfo.route_hash_extra(design, tmp_path)
    assert HEX64.match(first["outline_fp"]) and first != second
    assert rp_route_hash.route_hash(design, tmp_path) != hash_first


def test_route_hash_extra_never_raises_for_an_unresolvable_board(tmp_path):
    design = _design({"outline": {"dxf": "mech/missing.dxf"}})
    extra = rfo.route_hash_extra(design, tmp_path)
    assert set(extra) == {"outline_error"} and "missing.dxf" in extra["outline_error"]
    assert rfo.route_hash_extra(design, tmp_path) == extra             # deterministic
    assert "outline_error" in rfo.route_hash_extra(_design({"auto_x": 5}), tmp_path)


def test_changing_a_components_edge_changes_the_route_hash(use):
    design = _design()
    use(rfo)
    hashes = set()
    for edge in (None, "left", "right"):
        moved = copy.deepcopy(design)
        if edge:
            moved["components"][0]["edge"] = edge
        hashes.add(rp_route_hash.route_hash(moved))
    assert len(hashes) == 3
    use()
    without = {rp_route_hash.route_hash({**design, "components": [
        {**design["components"][0], "edge": e}, design["components"][1]]}) for e in ("l", "r")}
    assert len(without) == 1                     # the core registry does not hash 'edge'


@pytest.mark.parametrize("netlist", EXAMPLE_NETLISTS, ids=lambda p: p.parent.name)
def test_a_legacy_example_hashes_as_without_the_feature(use, netlist):
    design = _example(netlist)
    if _declares_geometry(design) or any("edge" in c for c in design.get("components", [])):
        pytest.skip("an outline / edge example: its hash moves on purpose")
    use()
    plain = rp_route_hash.route_hash(design, netlist.parent)
    use(rfo)
    assert rp_route_hash.route_hash(design, netlist.parent) == plain


# --- board dims ------------------------------------------------------------------------

def test_board_dims_outline_for_the_t10_block(tmp_path):
    assert rfo.board_dims_outline(_design(T10_BOARD), tmp_path) == \
        (60.0, 40.0, "netlist board outline")
    assert rfo.board_dims_outline(_design({"w": 65, "h": 56.5}), tmp_path) == \
        (65.0, 56.5, "netlist board outline")
    assert rfo.board_dims_outline(_design(), tmp_path) is None
    with pytest.raises(orv.ResolveError):
        rfo.board_dims_outline(_design({"auto_x": 5}), tmp_path)


def test_the_cli_labels_an_outline_board_honestly(use, tmp_path):
    design = _design(T10_BOARD)
    use()
    assert rp_cli.board_dims(design, None, None, netlist_dir=tmp_path)[2].startswith(
        "auto-sized")                                        # the T06 finding, unfixed
    use(rfo)
    assert rp_cli.board_dims(design, None, None, netlist_dir=tmp_path) == \
        (60.0, 40.0, "netlist board outline")
    assert rp_cli.board_dims(_design(), None, None, netlist_dir=tmp_path)[:2] == \
        rp_cli.AUTO_BOARD_DIMS


def test_the_cli_sizes_a_dxf_board_and_refuses_a_missing_dxf(use, tmp_path):
    _dxf, design = _dxf_netlist(tmp_path)
    netlist = tmp_path / "netlist.json"
    netlist.write_text(json.dumps(design), encoding="utf-8")
    use(rfo)
    w, h, label = rp_cli.cli_dims(rp_cli.parse_cli([str(netlist)]))
    assert (round(w, 6), round(h, 6), label) == (60.0, 40.0, "netlist board outline")
    netlist.write_text(json.dumps(_design({"outline": {"dxf": "gone.dxf"}})), encoding="utf-8")
    with pytest.raises(rp_cli.UsageError, match="gone.dxf"):
        rp_cli.cli_dims(rp_cli.parse_cli([str(netlist)]))


# --- G5 mechanical exports -------------------------------------------------------------

STEP_BBOX = {"x": [-10.0, 50.0], "y": [-10.0, 30.0], "z": [0.0, 1.51], "unit": "mm"}


class FakeMech:
    """mechanical_tools' three exports, scripted: each writes its output file
    and returns the shape the real tool returns (or `fail[name]`)."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, **fail: object) -> None:
        self.calls: list[tuple] = []
        self.fail = fail
        monkeypatch.setattr(mt, "export_step", self.step)
        monkeypatch.setattr(mt, "export_outline_dxf", self.dxf)
        monkeypatch.setattr(mt, "export_pos_datum", self.pos)

    def _out(self, name: str, out: str, ok: dict) -> dict:
        failure = self.fail.get(name)
        if isinstance(failure, Exception):
            raise failure
        if failure is not None:
            return failure
        Path(out).write_text(name, encoding="utf-8")
        return ok

    def step(self, pcb: str, out: str, board_only: bool = False) -> dict:
        self.calls.append(("step", pcb, out, board_only))
        return self._out("step", out, {"ok": True, "step": out, "bbox": STEP_BBOX,
                                       "warnings": []})

    def dxf(self, pcb: str, out: str) -> dict:
        self.calls.append(("dxf", pcb, out))
        return self._out("dxf", out, {"ok": True, "dxf": out, "hausdorff_mm": 0.0,
                                      "tolerance_mm": 0.01, "warnings": []})

    def pos(self, pcb: str, out: str) -> dict:
        self.calls.append(("pos", pcb, out))
        return self._out("pos", out, {"ok": True, "csv": out, "rows": 14, "warnings": []})


def _board(tmp_path: Path) -> tuple[str, str]:
    pcb = tmp_path / "t10.kicad_pcb"
    pcb.write_text(pipeline_world.K9_HEADER + ")\n", encoding="utf-8")
    return str(pcb), str(tmp_path / "t10.json")


def test_g5_mechanical_writes_the_three_exports_into_the_mech_folder(tmp_path, monkeypatch):
    fake = FakeMech(monkeypatch)
    pcb, netlist = _board(tmp_path)
    mech = tmp_path / "t10_mech"
    res = rfo.g5_mechanical(pcb, netlist, _design(T10_BOARD))
    assert fake.calls == [("step", pcb, str(mech / "t10.step"), True),
                          ("dxf", pcb, str(mech / "t10_outline.dxf")),
                          ("pos", pcb, str(mech / "t10_datum_cpl.csv"))]
    assert res == {"ok": True, "dir": str(mech),
                   "files": [str(mech / n) for n in ("t10.step", "t10_outline.dxf",
                                                     "t10_datum_cpl.csv")],
                   "step": str(mech / "t10.step"), "dxf": str(mech / "t10_outline.dxf"),
                   "datum_cpl": str(mech / "t10_datum_cpl.csv"), "hausdorff_mm": 0.0,
                   "step_bbox": STEP_BBOX, "warnings": []}
    assert sorted(p.name for p in mech.iterdir()) == \
        ["t10.step", "t10_datum_cpl.csv", "t10_outline.dxf"]


def test_g5_mechanical_turns_tool_failures_into_warnings(tmp_path, monkeypatch):
    FakeMech(monkeypatch, step={"ok": False, "reason": "no 3D models"},
             dxf={"ok": False, "dxf": "x", "hausdorff_mm": 0.2, "reason": "differs by 0.2 mm",
                  "warnings": ["flag --ou dropped"]},
             pos=RuntimeError("kicad-cli crashed"))
    pcb, netlist = _board(tmp_path)
    res = rfo.g5_mechanical(pcb, netlist, _design(T10_BOARD))
    assert res["ok"] is False and res["files"] == [] and res["hausdorff_mm"] == 0.2
    assert res["step_bbox"] is None and res["step"] is None and res["datum_cpl"] is None
    assert res["warnings"] == [
        "STEP export failed: no 3D models", "outline DXF export failed: differs by 0.2 mm",
        "outline DXF: flag --ou dropped",
        "datum CPL export failed: RuntimeError: kicad-cli crashed"]


def test_g5_mechanical_replaces_stale_exports(tmp_path, monkeypatch):
    FakeMech(monkeypatch, step={"ok": False, "reason": "failed"})
    pcb, netlist = _board(tmp_path)
    stale = tmp_path / "t10_mech" / "t10.step"
    stale.parent.mkdir()
    stale.write_text("an older run", encoding="utf-8")
    rfo.g5_mechanical(pcb, netlist, _design(T10_BOARD))
    assert not stale.exists()


def test_g5_mechanical_skips_a_legacy_board_and_reports_an_unresolved_one(tmp_path,
                                                                         monkeypatch):
    fake = FakeMech(monkeypatch)
    pcb, netlist = _board(tmp_path)
    for board in (None, {"w": 40, "h": 30}):
        assert rfo.g5_mechanical(pcb, netlist, _design(board)) == \
            {"skipped": "rectangular board without datum"}
    res = rfo.g5_mechanical(pcb, netlist, _design({"outline": {"dxf": "gone.dxf"}}))
    assert res["ok"] is False and res["files"] == []
    assert "gone.dxf" in res["warnings"][0]
    assert fake.calls == [] and not (tmp_path / "t10_mech").exists()


def _gate(rep: dict, name: str) -> dict:
    return next(g for g in rep["gates"] if g["gate"] == name)


def test_g5_verdict_stores_the_mechanical_report_and_never_fails_g5(world, monkeypatch):
    FakeMech(monkeypatch, step={"ok": False, "reason": "no 3D models"})
    world.write_design(_design(T10_BOARD))
    world.use_features("outline")
    rep = world.run()
    g5 = _gate(rep, "G5")
    assert rep["PASS"] is True and g5["passed"] is True
    assert "mechanical: STEP export failed: no 3D models" in g5["warnings"]
    assert rep["mechanical"]["files"] == [str(world.dir / "netlist_mech" / n)
                                          for n in ("netlist_outline.dxf",
                                                    "netlist_datum_cpl.csv")]
    saved = json.loads((world.dir / "netlist_pipeline_report.json").read_text("utf-8"))
    assert saved["mechanical"] == rep["mechanical"]


def test_g5_verdict_on_a_legacy_board_only_records_the_skip(world, monkeypatch):
    fake = FakeMech(monkeypatch)
    world.use_features("outline")
    rep = world.run()
    world.use_features()
    plain = world.run()
    assert rep["mechanical"] == {"skipped": "rectangular board without datum"}
    assert _gate(rep, "G5") == _gate(plain, "G5") and fake.calls == []
    assert "mechanical" not in plain
