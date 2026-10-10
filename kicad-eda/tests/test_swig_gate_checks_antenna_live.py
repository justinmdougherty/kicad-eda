"""swig_gate_checks: the G3 antenna-keepout check on KiCad 9 and 10 (D7-14b; ADR D7.1, D7.7,
D7.8 R20, D7.10, D7.19).

The check first read the 10.x-only ``ZONE.GetDoNotAllowZoneFills`` (an AttributeError on
9.0.4, where the flag is CopperPour), then board_outline.blocks_pour; either way it accepted
ANY board rule area with the flag (one on F.Cu only passed) and ignored footprint keep-outs
(a stock module's own all-copper area failed). It now asks
``rule_areas.has_rule_area(board, None, "all", no_pour=True, include_footprints=True)``:
ONE no-pour rule area, the board's or a footprint's, on every enabled copper layer. The
issue text is unchanged (the G3 task, napkin_policy and the review match it).

venv tier: keepout_findings with rule_areas.has_rule_area recorded (the exact call, both
verdicts, no call without an antenna_keepout) and the source contract.

live tier (KiCad 9.0.4 locally, 10.0.5 under the K10 recipe): the REAL swig_gate_checks.py
main() (stdin closed, as rp_kicad.gate_check runs it) on
  - a board built by the real swig_build_board.py from a netlist with an antenna_keepout
    (default 4L) -> no antenna_keepout issue, rc 0, no traceback / AttributeError, in the
    placement and placement_routed modes;
  - that board with the area deleted (BOARD.Remove; see VARIANT) -> the issue;
  - the area replaced by one on F.Cu only, on the outer layers only, or on every layer
    but allowing pour -> the issue;
  - 2L and 6L boards with the builder's all-copper area -> no issue; a 6L board with the
    pre-D7 F/In1/In2/B layer set -> the issue;
  - a stock RF_Module:ESP32-C3-WROOM-02 (its own all-copper keep-out) and no board area ->
    no issue; RF_Antenna:Texas_SWRA416_868MHz_915MHz (F.Cu only) -> the issue.
"""
from __future__ import annotations

import ast
import importlib
import json
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

import rule_areas
import swig_stackup as ss
import toolchain as tc
from fixtures import swig_python as sp

GATE = sp.TOOLS / "swig_gate_checks.py"
BUILDER = sp.TOOLS / "swig_build_board.py"
GATE_TIMEOUT_S = 180
BUILD_TIMEOUT_S = 600
ENV_DROP = ("KICAD_EDA_OUTLINE", "PACK_SCALE", ss.ARTIFACT_ENV)
NO_KEEPOUT = ("antenna_keepout requested in the netlist but no no-fill rule area exists "
              "on the board")
KEEPOUT = {"edge": "top", "depth": 4.0}
CU2, CU4 = ["F.Cu", "B.Cu"], ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]
CU6 = ["F.Cu", "In1.Cu", "In2.Cu", "In3.Cu", "In4.Cu", "B.Cu"]
HEADER = "Connector_PinHeader_2.54mm:PinHeader_1x03_P2.54mm_Vertical"


# ---------------------------------------------------------------------------
# venv tier
# ---------------------------------------------------------------------------
def _import_gate_checks() -> types.ModuleType:
    """swig_gate_checks; without pcbnew (the dev venv) a stub is registered for this import
    only (as tests/test_gate_checks_interior.py does)."""
    try:
        import pcbnew  # noqa: F401  (KiCad's bundled python only)
    except ImportError:
        pass
    else:
        return importlib.import_module("swig_gate_checks")
    stub = types.ModuleType("pcbnew")
    stub.ToMM = lambda nm: nm / 1_000_000
    stub.F_CrtYd, stub.B_CrtYd, stub.F_Cu, stub.B_Cu = 46, 47, 0, 2
    sys.modules["pcbnew"] = stub
    try:
        return importlib.import_module("swig_gate_checks")
    finally:
        if sys.modules.get("pcbnew") is stub:
            del sys.modules["pcbnew"]


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch):
    """keepout_findings with rule_areas.has_rule_area answering `verdict` and recording calls."""
    gc = _import_gate_checks()
    calls: list[tuple[tuple, dict]] = []

    def run(design: dict, verdict: bool) -> list:
        def has_rule_area(*args, **kwargs) -> bool:
            calls.append((args, kwargs))
            return verdict
        monkeypatch.setattr(rule_areas, "has_rule_area", has_rule_area)
        return gc.keepout_findings(BOARD, design)
    return run, calls


BOARD = object()          # has_rule_area is recorded: the board is only handed through


def test_one_all_copper_no_pour_area_board_or_footprint_is_asked_for(recorded) -> None:
    run, calls = recorded
    assert run({"antenna_keepout": KEEPOUT}, True) == []
    assert calls == [((BOARD, None, "all"), {"no_pour": True, "include_footprints": True})]


def test_no_such_area_is_the_unchanged_issue(recorded) -> None:
    run, _ = recorded
    found = run({"antenna_keepout": {"rect": [0, 0, 5, 5]}}, False)
    assert [(f.code, f.severity, f.text, f.detail) for f in found] == [
        ("antenna_keepout", "error", NO_KEEPOUT, None)]


@pytest.mark.parametrize("design", [{}, {"antenna_keepout": None}, {"antenna_keepout": {}}])
def test_no_keepout_requested_reads_no_areas(recorded, design: dict) -> None:
    run, calls = recorded
    assert run(design, False) == [] and calls == []


def _keepout_def() -> ast.FunctionDef:
    tree = ast.parse(GATE.read_text(encoding="utf-8"))
    return next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                and n.name == "keepout_findings")


def test_source_contract() -> None:
    source = GATE.read_text(encoding="utf-8")
    body = ast.get_source_segment(source, _keepout_def())
    assert "rule_areas.has_rule_area(" in body and "blocks_pour" not in body
    # rule_areas binds pcbnew (via board_outline) when first imported: never at import time
    top = ast.parse(source).body
    assert not [n for n in top if isinstance(n, (ast.Import, ast.ImportFrom))
                and "rule_areas" in ast.unparse(n)]
    assert [n for n in ast.walk(_keepout_def()) if isinstance(n, ast.Import)
            and [a.name for a in n.names] == ["rule_areas"]]
    assert "GetDoNotAllowZoneFills" not in source and "GetDoNotAllowCopperPour" not in source
    assert "antenna_keepout requested in the netlist " in body
    assert "but no no-fill rule area exists on the board" in body


# ---------------------------------------------------------------------------
# live tier: boards
# ---------------------------------------------------------------------------
LEGACY = {"no_pour": True, "no_vias": True, "no_pads": True}      # build_features' antenna area
# the built board's area replaced: None = deleted only; else (layers, flags) of the new area
VARIANTS = {"deleted": None, "fcu_only": (["F.Cu"], LEGACY),
            "outer_only": (["F.Cu", "B.Cu"], LEGACY),
            "pour_allowed": ("all", {"no_vias": True, "no_pads": True})}

# The area is deleted with BOARD.Remove, then thisown = False: pcbnew's Python Remove hands
# the ZONE to its proxy, and once a proxy has deleted a removed ZONE every object a later
# pcbnew call returns in that process (LoadBoard's BOARD, a BOX2I) is a bare SwigPyObject
# (9.0.4 and 10.0.5) and 10.0.5 may segfault at exit. rule_areas.remove_rule_areas does
# exactly that (reported to D7-14), so it is not used here. One process per variant;
# INSPECT reads every saved file back in a process of its own.
VARIANT = sp.PREAMBLE + r"""
import board_outline as bo
import rule_areas as ra

built, out, spec = sys.argv[1], sys.argv[2], json.loads(sys.argv[3])
board = bo.load_board(built)
removed = []
for zone in [z for z in board.Zones() if z.GetIsRuleArea()]:
    if str(zone.GetZoneName()) == "ANTENNA":
        board.Remove(zone)
        zone.thisown = False                # C++ keeps it: no delete by the proxy
        removed.append("ANTENNA")
if spec is not None:
    layers, flags = spec
    bb = board.GetBoardEdgesBoundingBox()
    strip = ra.rect_points(pcbnew.ToMM(bb.GetLeft()), pcbnew.ToMM(bb.GetTop()),
                           pcbnew.ToMM(bb.GetWidth()), 4.0)
    ra.add_rule_area(board, strip, layers, name="KEEPOUT", **flags)
pcbnew.SaveBoard(out, board)
print(json.dumps({"removed": removed}))
"""

FRESH = sp.PREAMBLE + r"""
import os
import board_outline as bo
import outline as ol
import rule_areas as ra

work, module_dir, antenna_dir, legacy = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
LEGACY = json.loads(legacy)
OX, OY = 100.0, 50.0
paths = {}


def save(board, name):
    paths[name] = os.path.join(work, name + ".kicad_pcb")
    pcbnew.SaveBoard(paths[name], board)


def fresh(n):
    board = pcbnew.CreateEmptyBoard()
    board.SetCopperLayerCount(n)
    bo.draw_outline(board, ol.from_board_block({"w": 60, "h": 40}), OX, OY)
    return board


def with_area(n, name, layers):
    board = fresh(n)
    ra.add_rule_area(board, ra.rect_points(OX + 5, OY + 2, 30, 6), layers, name="ANTENNA",
                     **LEGACY)
    save(board, name)


def with_footprint(lib_dir, fp_name, name):
    board = fresh(4)
    fp = pcbnew.FootprintLoad(lib_dir, fp_name)
    if fp is None:
        raise SystemExit(f"FootprintLoad returned None for {fp_name} in {lib_dir}")
    fp.SetReference("AE1")
    fp.SetPosition(pcbnew.VECTOR2I(bo.nm(OX + 30), bo.nm(OY + 20)))
    board.Add(fp)
    save(board, name)


with_area(2, "two_layer", "all")
with_area(6, "six_layer", "all")
with_area(6, "six_layer_pre_d7", ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"])
with_footprint(module_dir, "ESP32-C3-WROOM-02", "esp32_module")
with_footprint(antenna_dir, "Texas_SWRA416_868MHz_915MHz", "ti_antenna")
print(json.dumps({"paths": paths}))
"""

INSPECT = sp.PREAMBLE + r"""
import board_outline as bo
import rule_areas as ra

boards = json.loads(sys.argv[1])
areas = {name: ra.find_rule_areas(bo.load_board(pcb), include_footprints=True)
         for name, pcb in boards.items()}
print(json.dumps({"areas": areas, "version": str(pcbnew.Version())}))
"""


def _build(kpy: str, footprints: Path, work: Path) -> tuple[Path, dict]:
    """The real builder on a one-header netlist with an antenna_keepout (default 4L)."""
    design = {"name": "antenna_gate", "antenna_keepout": KEEPOUT,
              "components": [{"ref": "J1", "value": "HDR", "footprint": HEADER}],
              "nets": {"GND": [["J1", "1"]], "+3V3": [["J1", "2"]], "SIG": [["J1", "3"]]}}
    netlist, pcb = work / "antenna_gate.json", work / "antenna_gate.kicad_pcb"
    netlist.write_text(json.dumps(design), encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k not in ENV_DROP}
    proc = subprocess.run([kpy, str(BUILDER), str(netlist), str(pcb), str(footprints)],
                          capture_output=True, text=True, encoding="utf-8", errors="replace",
                          stdin=subprocess.DEVNULL, timeout=BUILD_TIMEOUT_S, env=env)
    res = tc.last_json(proc.stdout) or {}
    assert proc.returncode == 0 and res.get("ok"), (proc.returncode, res, proc.stderr[-2000:])
    return pcb, res


def _gate(kpy: str, mode: str, pcb: str, netlist: Path) -> dict:
    """The real swig_gate_checks.py main(), as rp_kicad.gate_check runs it."""
    try:
        proc = subprocess.run([kpy, str(GATE), mode, pcb, str(netlist)], capture_output=True,
                              text=True, encoding="utf-8", errors="replace",
                              stdin=subprocess.DEVNULL, timeout=GATE_TIMEOUT_S)
    except subprocess.TimeoutExpired as e:
        pytest.fail(f"swig_gate_checks hung for {GATE_TIMEOUT_S} s: {e.stdout!r} {e.stderr!r}")
    out = tc.last_json(proc.stdout)
    assert out is not None, f"rc {proc.returncode}, no JSON\n{proc.stdout}\n{proc.stderr}"
    return {**out, "rc": proc.returncode, "stderr": proc.stderr}


@pytest.fixture(scope="module")
def live(tmp_path_factory: pytest.TempPathFactory) -> dict:
    kpy = sp.require_kicad_python()
    module_dir = sp.kicad_footprints(kpy, "RF_Module.pretty")
    antenna_dir = sp.kicad_footprints(kpy, "RF_Antenna.pretty")
    work = tmp_path_factory.mktemp("antenna_gate")
    built, res = _build(kpy, module_dir.parent, work)
    boards, removed = {"built": str(built)}, {}
    for name, spec in VARIANTS.items():
        boards[name] = str(work / f"{name}.kicad_pcb")
        removed[name] = sp.run_child(VARIANT, [built, boards[name], json.dumps(spec)],
                                     work)["removed"]
    boards |= sp.run_child(FRESH, [work, module_dir, antenna_dir, json.dumps(LEGACY)],
                           work)["paths"]
    seen = sp.run_child(INSPECT, [json.dumps(boards)], work)
    netlist = work / "keepout.json"
    netlist.write_text(json.dumps({"antenna_keepout": KEEPOUT}), encoding="utf-8")
    runs = {name: _gate(kpy, "placement", pcb, netlist) for name, pcb in boards.items()}
    for name in ("built", "deleted"):
        runs[f"{name}_routed"] = _gate(kpy, "placement_routed", boards[name], netlist)
    return {**seen, "build": res, "removed": removed, "runs": runs}


def _keepout_issues(run: dict) -> list[str]:
    assert run["rc"] == 0, run
    assert "Traceback" not in run["stderr"] and "AttributeError" not in run["stderr"], run
    assert len(run["issues"]) == len(run["codes"]), run
    hits = [t for t in run["issues"] if "antenna_keepout" in t]
    assert hits == [t for t, c in zip(run["issues"], run["codes"], strict=True)
                    if c == "antenna_keepout"], run
    return hits


def _areas(live: dict, name: str) -> list[tuple]:
    return [(a["name"], a["footprint"], a["layers"], a["flags"]["no_pour"])
            for a in live["areas"][name]]


# ---------------------------------------------------------------------------
# live tier: verdicts
# ---------------------------------------------------------------------------
def test_live_built_antenna_keepout_board_has_no_issue(live: dict) -> None:
    assert live["build"]["antenna_keepout"] == KEEPOUT
    assert _areas(live, "built") == [("ANTENNA", None, CU4, True)]
    assert _keepout_issues(live["runs"]["built"]) == []
    assert _keepout_issues(live["runs"]["built_routed"]) == []


def test_live_area_deleted_is_the_issue(live: dict) -> None:
    assert live["removed"]["deleted"] == ["ANTENNA"] and _areas(live, "deleted") == []
    assert _keepout_issues(live["runs"]["deleted"]) == [NO_KEEPOUT]
    assert _keepout_issues(live["runs"]["deleted_routed"]) == [NO_KEEPOUT]


@pytest.mark.parametrize("name,layers,no_pour", [
    ("fcu_only", ["F.Cu"], True), ("outer_only", ["F.Cu", "B.Cu"], True),
    ("pour_allowed", CU4, False)])
def test_live_an_area_missing_a_layer_or_the_pour_flag_is_the_issue(
        live: dict, name: str, layers: list[str], no_pour: bool) -> None:
    assert live["removed"][name] == ["ANTENNA"]
    assert _areas(live, name) == [("KEEPOUT", None, layers, no_pour)]
    assert _keepout_issues(live["runs"][name]) == [NO_KEEPOUT]


@pytest.mark.parametrize("name,copper", [("two_layer", CU2), ("six_layer", CU6)])
def test_live_every_enabled_copper_layer_passes_on_any_count(
        live: dict, name: str, copper: list[str]) -> None:
    assert _areas(live, name) == [("ANTENNA", None, copper, True)]
    assert _keepout_issues(live["runs"][name]) == []


def test_live_pre_d7_four_layer_set_on_a_six_layer_board_is_the_issue(live: dict) -> None:
    assert _areas(live, "six_layer_pre_d7") == [("ANTENNA", None, CU4, True)]
    assert _keepout_issues(live["runs"]["six_layer_pre_d7"]) == [NO_KEEPOUT]


def test_live_stock_esp32_c3_wroom_02_keepout_counts_as_covering(live: dict) -> None:
    assert [(fp, layers, no_pour) for _, fp, layers, no_pour in _areas(live, "esp32_module")
            if layers == CU4] == [("AE1", CU4, True)]                 # its own area, no board one
    assert [a for a in live["areas"]["esp32_module"] if a["footprint"] is None] == []
    assert _keepout_issues(live["runs"]["esp32_module"]) == []


def test_live_stock_ti_antenna_f_cu_only_keepout_is_the_issue(live: dict) -> None:
    assert {(a["footprint"], tuple(a["layers"])) for a in live["areas"]["ti_antenna"]} == {
        ("AE1", ("F.Cu",))}
    assert _keepout_issues(live["runs"]["ti_antenna"]) == [NO_KEEPOUT]


def test_live_pcbnew_version_is_recorded(live: dict, record_property) -> None:
    assert live["version"].split(".")[0] in ("9", "10"), live["version"]
    record_property("pcbnew_version", live["version"])
    print(f"pcbnew {live['version']}")
