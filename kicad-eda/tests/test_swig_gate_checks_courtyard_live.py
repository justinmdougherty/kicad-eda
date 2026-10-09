"""swig_gate_checks main() under the real pcbnew: the T02 courtyard-cache fix.

KiCad 9.0.4's LoadBoard leaves every footprint's courtyard cache EMPTY. main()
used to call check_placement on that board directly, so on 9.0.4
_world_mouth returned None for every J* (the inward-mouth / >3 mm-from-edge
rule never fired) and check_heights measured GetBoundingBox (silk and text
included) instead of the courtyard. main() now loads the board through
board_outline.load_board and builds every footprint's courtyard cache first.
The antenna-keepout getter goes through board_outline.blocks_pour (the 10.x-only
GetDoNotAllowZoneFills raised AttributeError on 9.0.4).

Live tier: one 40 x 30 mm board (Edge.Cuts by board_outline.draw_outline at
sheet (100, 50), stock footprints by FootprintLoad) plus a bare twin, built by a
child under KiCad's bundled Python. The REAL script runs on them as the harness
runs it (`swig_gate_checks.py <mode> <pcb> <netlist.json>`, stdin closed):
  (a) J1, a right-angle header whose mouth faces the board interior ->
      'J1: mating face points INTO the board interior (unusable in an enclosure)'
      (T02: on 9.0.4 nothing was reported);
  (b) J2 faces the right edge from ~5.5 mm -> 'J2: mating face is <d> mm from the
      board edge it faces (must be at the edge, <=3 mm)'; J3 at the left edge ->
      nothing; "interior": true still exempts J1;
  (c) check_heights uses the courtyard: a height zone over R1's moved reference
      text only does not catch R1, a zone over its body does;
  (d) antenna_keepout: board_shapes.add_keepout's rule area satisfies it on 9 and
      10; the bare board gets the 'no no-fill rule area' issue;
  (e) routed mode runs the checks moved to swig_routed_checks (via ampacity);
  (f) a missing board file -> one JSON issue and rc 1, not a traceback; a missing
      netlist still runs the checks (as before) and now says so in a warning.
Unit tier (no KiCad): the keepout getter on fake 9.x / 10.x zones, and the
split's shape plus the contract texts other tasks read from the source.
"""
from __future__ import annotations

import ast
import importlib
import json
import re
import subprocess
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

import ipc_rules
from fixtures.swig_python import (PREAMBLE, TOOLS, kicad_footprints, require_kicad_python,
                                  run_child)

GATE = TOOLS / "swig_gate_checks.py"
ROUTED = TOOLS / "swig_routed_checks.py"
MOVED = ("check_routed_widths", "check_diff_pairs", "check_via_ampacity")
NM_PER_MM = 1_000_000
GATE_TIMEOUT_S = 180

INWARD = "J1: mating face points INTO the board interior (unusable in an enclosure)"
FAR = re.compile(r"^J2: mating face is (\d+\.\d) mm from the board edge it faces "
                 r"\(must be at the edge, <=3 mm\)$")
NO_KEEPOUT = ("antenna_keepout requested in the netlist but no no-fill rule area exists "
              "on the board")

BUILD = PREAMBLE + """
import board_outline as bo
import board_shapes
import outline as ol

out_a, out_b, header_dir, res_dir = sys.argv[1:5]
OX, OY = 100.0, 50.0


def at(x, y):
    return pcbnew.VECTOR2I(bo.nm(OX + x), bo.nm(OY + y))


def place(board, lib_dir, name, ref, x, y, rot=0.0):
    fp = pcbnew.FootprintLoad(lib_dir, name)
    if fp is None:
        raise SystemExit(f"FootprintLoad returned None for {name} in {lib_dir}")
    fp.SetReference(ref)
    fp.SetPosition(at(x, y))
    fp.SetOrientationDegrees(rot)
    board.Add(fp)
    return fp


def outline_board():
    board = pcbnew.BOARD()
    bo.draw_outline(board, ol.from_board_block({"w": 40, "h": 30}), OX, OY)
    return board


HEADER = "PinHeader_1x04_P2.54mm_Horizontal"
board = outline_board()
place(board, header_dir, HEADER, "J1", 4, 2)            # mouth +X: into the board
place(board, header_dir, HEADER, "J2", 24, 17)          # mouth +X: 5.5 mm short of the edge
place(board, header_dir, HEADER, "J3", 12, 28, 180)     # mouth -X: at the left edge
r1 = place(board, res_dir, "R_0603_1608Metric", "R1", 18, 6)
r1.Reference().SetPosition(at(18, 13))                  # its text 7 mm below its body
board_shapes.add_keepout(board, OX + 25, OY + 2, 10, 8)
net = pcbnew.NETINFO_ITEM(board, "VIN")
board.Add(net)
via = pcbnew.PCB_VIA(board)
via.SetPosition(at(37, 28))
via.SetWidth(bo.nm(0.6))
via.SetDrill(bo.nm(0.3))
via.SetViaType(pcbnew.VIATYPE_THROUGH)
via.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
via.SetNet(net)
board.Add(via)
pcbnew.SaveBoard(out_a, board)
pcbnew.SaveBoard(out_b, outline_board())

loaded = pcbnew.LoadBoard(out_a)
yard = loaded.FindFootprintByReference("J1").GetCourtyard(pcbnew.F_CrtYd)
print(json.dumps({"version": pcbnew.Version(),
                  "courtyard_outlines_after_load": yard.OutlineCount()}))
"""


@pytest.fixture(scope="module")
def boards(tmp_path_factory: pytest.TempPathFactory) -> dict:
    kpy = require_kicad_python()
    work = tmp_path_factory.mktemp("gate_courtyard")
    pcb_a, pcb_b = work / "gates.kicad_pcb", work / "bare.kicad_pcb"
    libs = [kicad_footprints(kpy, lib) for lib in ("Connector_PinHeader_2.54mm.pretty",
                                                   "Resistor_SMD.pretty")]
    res = run_child(BUILD, [pcb_a, pcb_b, *libs], work)
    return {"kpy": kpy, "work": work, "gates": pcb_a, "bare": pcb_b, **res}


def _last_json(stdout: str) -> dict:
    for line in reversed(stdout.splitlines()):
        if line.strip().startswith("{"):
            return json.loads(line)
    raise AssertionError(f"swig_gate_checks printed no JSON line:\n{stdout}")


def _gate(boards: dict, mode: str, pcb: Path, design: dict | None, name: str
          ) -> tuple[int, dict]:
    """Run the real swig_gate_checks.py as rp_kicad.gate_check does (design None:
    the netlist path is handed over but never written)."""
    netlist = boards["work"] / f"{name}.json"
    if design is not None:
        netlist.write_text(json.dumps(design), encoding="utf-8")
    try:
        proc = subprocess.run([boards["kpy"], str(GATE), mode, str(pcb), str(netlist)],
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", stdin=subprocess.DEVNULL,
                              timeout=GATE_TIMEOUT_S)
    except subprocess.TimeoutExpired as e:
        pytest.fail(f"swig_gate_checks hung for {GATE_TIMEOUT_S} s: {e.stdout!r} {e.stderr!r}")
    try:
        return proc.returncode, _last_json(proc.stdout)
    except AssertionError:
        pytest.fail(f"rc {proc.returncode}, no JSON\n--- stdout\n{proc.stdout}\n"
                    f"--- stderr\n{proc.stderr}")


def _ok(boards: dict, mode: str, pcb: Path, design: dict, name: str) -> dict:
    rc, out = _gate(boards, mode, pcb, design, name)
    assert rc == 0, out
    assert set(out) == {"issues", "warnings"}, out
    return out


def _connectors(*interior: str) -> dict:
    return {"components": [{"ref": r, **({"interior": True} if r in interior else {})}
                           for r in ("J1", "J2", "J3")]}


# --- live: the T02 regression -------------------------------------------------------------
def test_the_premise_load_board_courtyard_cache_per_version(boards):
    """Measured: 9.0.4 hands back an EMPTY courtyard after LoadBoard (so (a)-(c)
    exercise the T02 path there); 10.0.5 builds it lazily."""
    empty_after_load = boards["version"].startswith("9.")
    assert boards["courtyard_outlines_after_load"] == (0 if empty_after_load else 1), boards


def test_a_inward_connector_is_flagged_by_main(boards):
    out = _ok(boards, "placement", boards["gates"], _connectors(), "connectors")
    assert INWARD in out["issues"], out


def test_b_far_connector_flagged_edge_connector_and_interior_part_not(boards):
    out = _ok(boards, "placement", boards["gates"], _connectors(), "connectors")
    far = [m for m in map(FAR.match, out["issues"]) if m]
    assert len(far) == 1 and 4.5 < float(far[0].group(1)) < 6.5, out
    assert sorted(i.split(":")[0] for i in out["issues"]) == ["J1", "J2"], out
    exempt = _ok(boards, "placement", boards["gates"], _connectors("J1"), "interior")
    assert [i.split(":")[0] for i in exempt["issues"]] == ["J2"], exempt


def test_b_placement_routed_mode_runs_the_same_connector_rule(boards):
    out = _ok(boards, "placement_routed", boards["gates"], _connectors(), "connectors_routed")
    assert sorted(i.split(":")[0] for i in out["issues"]) == ["J1", "J2"], out


def test_c_check_heights_measures_the_courtyard_not_the_text(boards):
    # board frame = Edge.Cuts bbox top-left: R1's courtyard spans ~(16.6..19.5, 5.3..6.8),
    # its reference text sits around (18, 13)
    design = {"components": [{"ref": "R1", "height": 5.0}],
              "height_zones": [{"name": "text_zone", "x": 16, "y": 12, "w": 4, "h": 2,
                                "max_height": 2.0},
                               {"name": "body_zone", "x": 17, "y": 5, "w": 2, "h": 2,
                                "max_height": 2.0}]}
    out = _ok(boards, "placement", boards["gates"], design, "heights")
    assert [i for i in out["issues"] if i.startswith("R1:")] == [
        "R1: 5.0 mm tall inside body_zone with a 2.0 mm ceiling (F side) — move it out, "
        "use a lower package, or exempt it if it IS the standoff"], out
    assert not [w for w in out["warnings"] if w.startswith("R1:")], out


def test_d_antenna_keepout_rule_area_is_found_on_every_version(boards):
    design = {"antenna_keepout": {"x": 25, "y": 2, "w": 10, "h": 8}}
    out = _ok(boards, "placement", boards["gates"], design, "keepout")
    assert not [i for i in out["issues"] if "antenna_keepout" in i], out
    bare = _ok(boards, "placement", boards["bare"], design, "keepout_bare")
    assert bare["issues"] == [NO_KEEPOUT], bare


def test_e_routed_mode_runs_the_moved_checks(boards):
    out = _ok(boards, "routed", boards["gates"], {"net_specs": {"VIN": {"current": 5.0}}},
              "routed")
    cap = round(ipc_rules.via_ampacity_a(0.3), 2)
    assert out == {"issues": [f"VIN: 5.0 A crosses layers through 1 via(s) + 0 PTH barrel(s) "
                              f"totaling {cap} A (IPC-2221) — add parallel vias at the "
                              f"transition"], "warnings": []}


def test_f_missing_board_is_one_json_issue_not_a_traceback(boards):
    missing = boards["work"] / "no_such_board.kicad_pcb"
    rc, out = _gate(boards, "placement", missing, {}, "missing")
    assert rc == 1, out
    assert len(out["issues"]) == 1 and "board file not found" in out["issues"][0], out
    assert str(missing) in out["issues"][0] and out["warnings"] == [], out


def test_f_unreadable_netlist_still_checks_and_says_so(boards):
    rc, out = _gate(boards, "placement", boards["gates"], None, "never_written")
    assert rc == 0 and sorted(i.split(":")[0] for i in out["issues"]) == ["J1", "J2"], out
    assert len(out["warnings"]) == 1 and out["warnings"][0].startswith(
        f"netlist {boards['work'] / 'never_written.json'} not read (FileNotFoundError"), out


# --- unit: the keepout getter on fake 9.x / 10.x zones -------------------------------------
def _import_gate_checks() -> types.ModuleType:
    """swig_gate_checks; without pcbnew (the venv) a stub is registered for the
    import only, as tests/test_gate_checks_interior.py does."""
    try:
        import pcbnew  # noqa: F401  (KiCad's bundled python only)
    except ImportError:
        pass
    else:
        return importlib.import_module("swig_gate_checks")
    stub = types.ModuleType("pcbnew")
    stub.ToMM = lambda nm: nm / NM_PER_MM
    stub.FromMM = lambda mm: int(round(mm * NM_PER_MM))
    stub.F_CrtYd, stub.B_CrtYd, stub.F_Cu, stub.B_Cu = 46, 47, 0, 2
    sys.modules["pcbnew"] = stub
    try:
        return importlib.import_module("swig_gate_checks")
    finally:
        if sys.modules.get("pcbnew") is stub:
            del sys.modules["pcbnew"]


def _zone(api: str, rule: bool, no_pour: bool) -> SimpleNamespace:
    """A rule area exposing only one pcbnew version's pour getter."""
    getter = {"9": "GetDoNotAllowCopperPour", "10": "GetDoNotAllowZoneFills"}[api]
    return SimpleNamespace(GetIsRuleArea=lambda: rule, **{getter: lambda: no_pour})


def _fake_board(zones: list) -> SimpleNamespace:
    box = SimpleNamespace(GetLeft=lambda: 0, GetTop=lambda: 0, GetRight=lambda: 40 * NM_PER_MM,
                          GetBottom=lambda: 30 * NM_PER_MM)
    return SimpleNamespace(GetBoardEdgesBoundingBox=lambda: box, GetFootprints=lambda: [],
                           Zones=lambda: list(zones))


@pytest.mark.parametrize(("zones", "flagged"), [
    ([_zone("9", True, True)], False),            # KiCad 9.x: GetDoNotAllowCopperPour
    ([_zone("10", True, True)], False),           # KiCad 10.x: GetDoNotAllowZoneFills
    ([_zone("9", True, False)], True),            # a rule area that allows pours
    ([], True),
])
def test_keepout_getter_reads_both_pcbnew_apis(zones: list, flagged: bool):
    gc = _import_gate_checks()
    issues, _ = gc.check_placement(_fake_board(zones), {"antenna_keepout": {"w": 5}})
    assert issues == ([NO_KEEPOUT] if flagged else [])


# --- static: the split's shape and the contract texts --------------------------------------
def _defs(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}


def test_routed_checks_moved_and_re_exported():
    assert set(MOVED) <= _defs(ROUTED)
    assert not set(MOVED) & _defs(GATE)
    tree = ast.parse(GATE.read_text(encoding="utf-8"))
    reexports = [n for n in tree.body if isinstance(n, ast.ImportFrom)
                 and n.module == "swig_routed_checks"]
    assert [sorted(a.name for a in n.names) for n in reexports] == [sorted(MOVED)]


def test_contract_names_and_texts_stay_in_swig_gate_checks():
    source = GATE.read_text(encoding="utf-8")
    assert {"_world_mouth", "is_edge_connector", "interior_refs", "check_placement",
            "check_heights", "check_plane_integrity", "check_ep_via_farms", "main"} <= _defs(GATE)
    for text in ("mating face points INTO the board interior", "mating face is ",
                 "mm from the board ", "fractured into separate islands",
                 "dissipating exposed-pad part has only"):
        assert text in source, text
    assert len(source.splitlines()) < 700


def test_the_pour_flag_is_read_only_through_board_outline():
    source = GATE.read_text(encoding="utf-8")
    assert "GetDoNotAllowZoneFills" not in source and "GetDoNotAllowCopperPour" not in source
    assert "blocks_pour" in source
