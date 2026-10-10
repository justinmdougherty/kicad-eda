"""F3-19: build_board_from_netlist (board_build.build) resolves the board outline.

Before KiCad starts, the build resolves the netlist ``board`` block with
outline_resolve.resolve_board (ROADMAP D2), relative to the netlist directory:

  - a block it cannot resolve (a DXF that is missing, an auto_*-only block,
    ...) refuses with {"ok": False, "reason": "board outline: ..."} before
    anything is spawned or written;
  - a non-legacy board (outline.loop / corner_radius / cutouts / datum /
    outline.dxf) gets the version-1 sidecar <pcb stem>.outline.json beside
    the board and KICAD_EDA_OUTLINE=<its absolute path> in the builder's env;
  - a legacy board (no block, or a plain w x h rectangle) keeps today's call
    exactly: the same argv, no sidecar (a stale one is removed), and no
    KICAD_EDA_OUTLINE in the env (a stray one in os.environ is not passed on);
  - board_w / board_h default to 0 (= from the netlist); a value > 0 that
    differs from the board outline's bbox by more than 0.01 mm is ignored with
    the warning 'board_w/board_h ignored: the board outline defines the board';
  - every spawned build reports "outline": the builder's own record plus
    {source, fingerprint, sidecar}.

Unit tier: tc.kicad_python, tc.footprints_dir, tc.symbols_dir, tc.run,
ipc.write_guard and board_build.release_board_session are monkeypatched (the
X-09 patch target); the fake builder records argv, env and the sidecar it
was handed. Live tier (KiCad 9/10 pcbnew; skipped without it): the T10 board
referenced as a KiCad 9 DXF export builds through the MCP tool with no
board_w/board_h.
"""
from __future__ import annotations

import hashlib
import inspect
import json
import math
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import pytest

import board_build
import board_pipeline_tools as bpt
import kicad_ipc as ipc
import outline_resolve as orz
import toolchain as tc
from conftest import load_example
from fixtures.dxf_builders import Sketch, fusion_board, fusion_doc, save
from fixtures.swig_python import T10_BOARD, require_kicad_python

HERE = Path(__file__).resolve().parent
SERVER = HERE.parent / "server"
SCRIPT = str(tc.TOOLS_DIR / "swig_build_board.py")
ENV = orz.ENV_VAR
IGNORED = "board_w/board_h ignored: the board outline defines the board"
BUILDER_OUTLINE = {"source": "builder", "legacy": True, "paper": "A4",
                   "bbox_mm": [0.0, 0.0, 1.0, 1.0]}
FAKE_RESULT = {"ok": True, "footprints": 1, "nets": 1, "pads_assigned": 2, "zones": [],
               "missing_pads": [], "warnings": ["a builder note"],
               "outline": BUILDER_OUTLINE}
NONE_FINGERPRINT = hashlib.sha256(b"{}").hexdigest()
R0603 = "Resistor_SMD:R_0603_1608Metric"
K9_DXF = HERE / "fixtures" / "dxf" / "k9_mm_nodrill.dxf"
T10_NETLIST = HERE / "fixtures" / "outline" / "t10_board.json"


# --- the rig: a stock install, a project folder and a fake builder ------------------------
@dataclass
class Rig:
    root: Path
    fps: Path
    project: Path
    runs: list[dict] = field(default_factory=list)
    result: dict = field(default_factory=lambda: dict(FAKE_RESULT))
    rc: int = 0

    def netlist(self, design: dict) -> Path:
        path = self.project / "netlist.json"
        path.write_text(json.dumps(design, indent=1), encoding="utf-8")
        return path

    def build(self, netlist: Path, *dims: float, **kw: object) -> dict:
        kw.setdefault("out_pcb", str(self.project / "board.kicad_pcb"))
        return board_build.build(str(netlist), *dims, **kw)

    def fake_run(self, args: list[str], **kw: object) -> subprocess.CompletedProcess:
        env = dict(kw.get("env") or {})
        side = env.get(ENV)
        self.runs.append({"args": [str(a) for a in args], "kw": kw, "env": env,
                          "sidecar": orz.read_sidecar(side) if side else None})
        return subprocess.CompletedProcess(args, self.rc, stdout=json.dumps(self.result) + "\n",
                                           stderr="")


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Rig:
    fps, syms = tmp_path / "stock" / "footprints", tmp_path / "stock" / "symbols"
    fps.mkdir(parents=True)
    syms.mkdir(parents=True)
    project = tmp_path / "proj"
    project.mkdir()
    r = Rig(tmp_path, fps, project)
    monkeypatch.delenv(ENV, raising=False)
    monkeypatch.setattr(tc, "kicad_python", lambda: "kpy")
    monkeypatch.setattr(tc, "footprints_dir", lambda explicit="": str(fps))
    monkeypatch.setattr(tc, "symbols_dir", lambda explicit="": str(syms))
    monkeypatch.setattr(tc, "run", r.fake_run)
    monkeypatch.setattr(ipc, "write_guard", lambda path: None)
    monkeypatch.setattr(board_build, "release_board_session", lambda pcb: {})
    return r


def _design(board: object = None) -> dict:
    design: dict = {"components": [{"ref": "R1", "lib_id": "Device:R", "value": "10k",
                                    "footprint": R0603, "x": 30, "y": 20}],
                    "nets": {"N1": [["R1", "1"]], "N2": [["R1", "2"]]}}
    if board is not None:
        design["board"] = board
    return design


def _fusion_dxf(folder: Path) -> Path:
    """A Fusion-like 60 x 40 r3 sketch (cm) with a hole and a slot."""
    doc, msp = fusion_doc()
    fusion_board(Sketch(msp, k=10.0))
    folder.mkdir(parents=True, exist_ok=True)
    return save(doc, folder, "outline.dxf")


def _sidecar(rig: Rig, name: str = "board") -> Path:
    return Path(os.path.abspath(rig.project / f"{name}.outline.json"))


# --- legacy boards: today's call, exactly ---------------------------------------------------
def test_the_legacy_lm358_call_through_the_tool_has_todays_args_and_env(rig: Rig) -> None:
    netlist = rig.netlist(load_example("lm358_buffer"))
    out = rig.project / "board.kicad_pcb"
    res = bpt.build_board_from_netlist(str(netlist), 30, 20, out_pcb=str(out))
    assert res["ok"] is True, res
    [run] = rig.runs
    assert run["args"] == ["kpy", SCRIPT, str(netlist), str(out), str(rig.fps), "30", "20"]
    assert run["env"] == {**os.environ, "PACK_SCALE": "1.0"} and ENV not in run["env"]
    assert run["sidecar"] is None and not _sidecar(rig).exists()
    assert res["outline"] == {**BUILDER_OUTLINE, "source": "none",
                              "fingerprint": NONE_FINGERPRINT, "sidecar": None}
    assert res["warnings"] == ["a builder note"]


def test_the_tool_defaults_board_w_and_board_h_to_zero(rig: Rig) -> None:
    for fn in (bpt.build_board_from_netlist, board_build.build):
        params = inspect.signature(fn).parameters
        assert (params["board_w"].default, params["board_h"].default) == (0, 0), fn
    netlist = rig.netlist(load_example("lm358_buffer"))
    res = bpt.build_board_from_netlist(str(netlist), placement="p.json")
    assert res["ok"] is True, res
    out = str(netlist.with_suffix(".kicad_pcb"))
    assert rig.runs[0]["args"] == ["kpy", SCRIPT, str(netlist), out, str(rig.fps), "0", "0",
                                   "p.json"]


def test_a_rect_board_is_legacy_and_its_size_mismatch_is_warned(rig: Rig) -> None:
    netlist = rig.netlist(_design({"w": 65.0, "h": 56.5, "auto_x": 8.5}))
    res = rig.build(netlist, 30, 20)
    [run] = rig.runs
    assert run["args"][5:] == ["30", "20"] and ENV not in run["env"]
    assert run["sidecar"] is None and not _sidecar(rig).exists()
    assert (res["outline"]["source"], res["outline"]["sidecar"]) == ("rect", None)
    [note] = [w for w in res["warnings"] if IGNORED in w]
    assert "65 x 56.5" in note and "30 x 20" in note


def test_a_stray_env_var_never_reaches_a_legacy_build(rig: Rig,
                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV, str(rig.root / "elsewhere.outline.json"))
    rig.build(rig.netlist(load_example("lm358_buffer")), 30, 20)
    assert ENV not in rig.runs[0]["env"]
    assert rig.runs[0]["env"]["PACK_SCALE"] == "1.0"


def test_a_legacy_rebuild_removes_a_stale_sidecar(rig: Rig) -> None:
    rig.build(rig.netlist(_design(T10_BOARD)))
    assert _sidecar(rig).is_file()
    res = rig.build(rig.netlist(_design()))
    assert res["ok"] is True and not _sidecar(rig).exists()
    assert rig.runs[-1]["sidecar"] is None and ENV not in rig.runs[-1]["env"]


# --- outline boards: the sidecar and the env var ------------------------------------------
def test_an_outline_board_writes_the_sidecar_and_sets_the_env_var(rig: Rig) -> None:
    design = _design(T10_BOARD)
    netlist = rig.netlist(design)
    out = rig.project / "board.kicad_pcb"
    res = rig.build(netlist)
    assert res["ok"] is True, res
    [run] = rig.runs
    side = _sidecar(rig)
    assert run["args"] == ["kpy", SCRIPT, str(netlist), str(out), str(rig.fps), "0", "0"]
    assert run["env"][ENV] == str(side) and run["env"]["PACK_SCALE"] == "1.0"
    want = orz.resolve_board(design, rig.project)
    sidecar = run["sidecar"]                     # read back while the builder ran
    assert (sidecar["source"], sidecar["board"], sidecar["dxf"]) == ("loop", want.block, None)
    assert sidecar["fingerprint"] == want.fingerprint and side.is_file()
    assert orz.read_sidecar(side) == sidecar     # kept beside the board
    assert res["outline"] == {**BUILDER_OUTLINE, "source": "loop",
                              "fingerprint": want.fingerprint, "sidecar": str(side)}
    assert not any(IGNORED in w for w in res["warnings"])


def test_an_outline_build_overrides_a_stray_env_var(rig: Rig,
                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV, str(rig.root / "elsewhere.outline.json"))
    rig.build(rig.netlist(_design({"w": 40, "h": 30, "corner_radius": 2})))
    assert rig.runs[0]["env"][ENV] == str(_sidecar(rig))
    assert rig.runs[0]["sidecar"]["source"] == "rounded_rect"


def test_a_dxf_board_is_resolved_here_and_handed_over_as_loops(rig: Rig) -> None:
    dxf = _fusion_dxf(rig.project / "mech")
    design = _design({"outline": {"dxf": "mech/outline.dxf"}})
    res = rig.build(rig.netlist(design))
    assert res["ok"] is True, res
    sidecar = rig.runs[0]["sidecar"]
    want = orz.resolve_board(design, rig.project)
    assert sidecar["source"] == "dxf" and Path(sidecar["dxf"]) == dxf.resolve()
    assert "dxf" not in sidecar["board"]["outline"] and sidecar["board"] == want.block
    assert res["outline"]["fingerprint"] == want.fingerprint
    assert res["outline"]["source"] == "dxf"


def test_relative_paths_resolve_the_dxf_and_give_an_absolute_sidecar(
        rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    """The harness passes ctx.netlist / ctx.pcb as given (run_pipeline.py proj/netlist.json)."""
    _fusion_dxf(rig.project / "mech")
    rig.netlist(_design({"outline": {"dxf": "mech/outline.dxf"}}))
    monkeypatch.chdir(rig.root)
    res = board_build.build("proj/netlist.json", out_pcb="proj/board.kicad_pcb")
    assert res["ok"] is True, res
    side = str(rig.root / "proj" / "board.outline.json")
    assert rig.runs[0]["env"][ENV] == side == res["outline"]["sidecar"]
    assert rig.runs[0]["args"][2:4] == ["proj/netlist.json", "proj/board.kicad_pcb"]


def test_a_failed_build_still_reports_the_outline(rig: Rig) -> None:
    rig.rc, rig.result = 1, {"ok": False, "error": "pcbnew could not load footprint"}
    res = rig.build(rig.netlist(_design(T10_BOARD)))
    assert res["ok"] is False and res["error"] == "pcbnew could not load footprint"
    assert res["outline"] == {"source": "loop", "sidecar": str(_sidecar(rig)),
                              "fingerprint": orz.resolve_board(_design(T10_BOARD),
                                                               None).fingerprint}
    assert "netclasses" not in res


# --- board_w / board_h against the outline ---------------------------------------------------
@pytest.mark.parametrize(("dims", "warned"), [
    ((), False), ((0, 0), False), ((60, 40), False), ((60.009, 39.991), False),
    ((30, 20), True), ((60.02, 40), True), ((0, 40.5), True), ((61, 0), True),
], ids=["defaults", "zeros", "equal", "within-0.01", "smaller", "w-off", "h-only", "w-only"])
def test_board_w_h_that_differ_from_the_outline_are_ignored_with_a_warning(
        rig: Rig, dims: tuple[float, ...], warned: bool) -> None:
    res = rig.build(rig.netlist(_design(T10_BOARD)), *dims)
    notes = [w for w in res["warnings"] if IGNORED in w]
    assert len(notes) == int(warned), res["warnings"]
    assert res["warnings"][0] == "a builder note"            # the builder's stay first
    assert rig.runs[0]["args"][5:] == [str(d) for d in (dims or (0, 0))]


def test_no_outline_means_no_size_warning(rig: Rig) -> None:
    res = rig.build(rig.netlist(load_example("lm358_buffer")), 30, 20)
    assert not any(IGNORED in w for w in res["warnings"])


# --- refusals: nothing spawned, nothing written ---------------------------------------------
@pytest.mark.parametrize(("board", "words"), [
    ({"outline": {"dxf": "mech/nope.dxf"}}, ("nope.dxf",)),
    ({"auto_x": 5}, ("no outline",)),
    ({"outline": {"loop": [[0, 0], [10, 0], [10, 10]], "units": "cm"}}, ("only apply",)),
], ids=["missing-dxf", "auto-only", "loop-in-cm"])
def test_an_unresolvable_board_refuses_before_spawning_or_writing(
        rig: Rig, board: dict, words: tuple[str, ...], monkeypatch: pytest.MonkeyPatch) -> None:
    touched: list[str] = []
    monkeypatch.setattr(ipc, "write_guard", lambda path: touched.append(path))
    netlist = rig.netlist(_design(board))
    res = rig.build(netlist)
    assert res["ok"] is False and res["reason"].startswith("board outline: "), res
    assert all(w in res["reason"] for w in words), res["reason"]
    assert set(res) == {"ok", "reason"} and rig.runs == [] and touched == []
    assert sorted(p.name for p in rig.project.iterdir()) == ["netlist.json"]


def test_a_sidecar_that_cannot_be_written_refuses_before_spawning(
        rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    def full_disk(resolved: object, path: object) -> Path:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(orz, "write_sidecar", full_disk)
    monkeypatch.setattr(board_build, "release_board_session",
                        lambda pcb: {"released_boards": [pcb]})
    res = rig.build(rig.netlist(_design(T10_BOARD)))
    assert res["ok"] is False and rig.runs == [], res
    assert "outline sidecar" in res["reason"] and "No space left on device" in res["reason"]
    assert res["released_boards"] == [str(rig.project / "board.kicad_pcb")]


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf, True, "30", None],
                         ids=["nan", "inf", "-inf", "bool", "str", "none"])
def test_board_w_h_must_be_finite_numbers(rig: Rig, bad: object) -> None:
    netlist = rig.netlist(load_example("lm358_buffer"))
    for dims in ((bad, 20), (30, bad)):
        res = rig.build(netlist, *dims)
        name = "board_w" if dims[0] is bad else "board_h"
        assert res["ok"] is False and res["reason"].startswith(name), res
    assert rig.runs == []


# --- the MCP descriptions and the file sizes -------------------------------------------------
def _doc(fn: object) -> str:
    return " ".join((getattr(fn, "__doc__", "") or "").split())


def test_the_build_tool_describes_the_outline_and_the_optional_size() -> None:
    doc = _doc(bpt.build_board_from_netlist)
    for words in ("board_w/board_h", "0", "outline.dxf", "outline.json", IGNORED.split(":")[0]):
        assert words in doc, words


def test_the_autoroute_tool_tells_the_truth_about_layers_and_outlines() -> None:
    doc = _doc(bpt.autoroute_board)
    assert "Signal layers route on F.Cu/B.Cu" not in doc
    assert "inner plane layers" in doc and "non-rectangular" in doc
    assert "dsn_seed" in doc and "deterministic" in doc      # F5-02's contract stays


@pytest.mark.parametrize(("name", "limit"), [("board_pipeline_tools.py", 350),
                                             ("board_build.py", 400)])
def test_the_board_modules_stay_small(name: str, limit: int) -> None:
    lines = (SERVER / name).read_text(encoding="utf-8").count("\n")
    assert lines < limit, f"{name}: {lines} lines"


# --- live: KiCad 9/10 pcbnew builds a DXF board through the tool --------------------------------
def test_live_a_dxf_board_builds_through_the_tool_without_board_w_h(tmp_path: Path) -> None:
    require_kicad_python()
    project = tmp_path / "proj"
    (project / "mech").mkdir(parents=True)
    shutil.copyfile(K9_DXF, project / "mech" / "outline.dxf")
    design = json.loads(T10_NETLIST.read_text(encoding="utf-8"))
    design["board"] = {"outline": {"dxf": "mech/outline.dxf"}}
    netlist = project / "netlist.json"
    netlist.write_text(json.dumps(design, indent=1), encoding="utf-8")
    pcb = project / "board.kicad_pcb"
    res = bpt.build_board_from_netlist(str(netlist), 30, 20, out_pcb=str(pcb))
    assert res.get("ok") is True, res
    outline = res["outline"]
    assert (outline["source"], outline["legacy"], outline["cutouts"]) == ("dxf", False, 2)
    assert outline["bbox_mm"] == pytest.approx([0, 0, 60, 40], abs=1e-3)
    assert outline["datum_board"] == pytest.approx([10, 30], abs=1e-3)
    assert outline["area_mm2"] == pytest.approx(2314.36, abs=0.05)
    assert res["board_mm"] == pytest.approx([60, 40], abs=1e-3)
    assert outline["sidecar"] == str(pcb.with_name("board.outline.json"))
    side = orz.read_sidecar(outline["sidecar"])
    assert side["source"] == "dxf" and side["fingerprint"] == outline["fingerprint"]
    assert any(IGNORED in w for w in res["warnings"]), res["warnings"]
    assert pcb.is_file() and "netclasses" in res
