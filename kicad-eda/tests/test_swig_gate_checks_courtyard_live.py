"""swig_gate_checks main() under the real pcbnew: the T02 courtyard-cache fix.

KiCad 9.0.4's LoadBoard leaves every footprint's courtyard cache EMPTY. main()
used to call check_placement on that board directly, so on 9.0.4
_world_mouth returned None for every J* (the inward-mouth / >3 mm-from-edge
rule never fired) and check_heights measured GetBoundingBox (silk and text
included) instead of the courtyard. main() now loads the board through
board_outline.load_board and builds every footprint's courtyard cache first.
The antenna-keepout getter is version-safe (the 10.x-only
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
  (d) antenna_keepout: board_shapes.add_keepout's rule area (every copper layer,
      no pour) satisfies it on 9 and 10; the bare board gets the issue;
  (e) routed mode runs the checks moved to swig_routed_checks (via ampacity);
  (f) a missing board file -> one JSON issue and rc 1, not a traceback; a missing
      netlist still runs the checks (as before) and now says so in a warning.
Static tier: the split's shape and the contracts other tasks read from the source.

Every assertion reads only the issues of the rule it is about, by the contract
prefix of their text: later owners of swig_gate_checks.py add rules, a parallel
`code` field in the JSON and a '<why>; <nearest edge>' suffix to the connector
texts (F3-13b), and swap the keepout getter (D7-14b), without editing this file.
"""
from __future__ import annotations

import ast
import json
import re
import subprocess
from pathlib import Path

import pytest

import ipc_rules
from fixtures.swig_python import (PREAMBLE, TOOLS, kicad_footprints, require_kicad_python,
                                  run_child)

GATE = TOOLS / "swig_gate_checks.py"
ROUTED = TOOLS / "swig_routed_checks.py"
MOVED = ("check_routed_widths", "check_diff_pairs", "check_via_ampacity")
GATE_TIMEOUT_S = 180

# contract prefixes of the connector rule (the texts may grow a suffix, never change)
INWARD = "J1: mating face points INTO the board interior (unusable in an enclosure)"
FAR = re.compile(r"^J2: mating face is (\d+\.\d) mm from the board edge it faces "
                 r"\(must be at the edge, <=3 mm\)")
MATING = re.compile(r"^(J\d+): mating face ")
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
        out = _last_json(proc.stdout)
    except AssertionError:
        pytest.fail(f"rc {proc.returncode}, no JSON\n--- stdout\n{proc.stdout}\n"
                    f"--- stderr\n{proc.stderr}")
    # the contract keys; later tasks may add fields next to them (F3-13b: issue codes)
    assert {"issues", "warnings"} <= set(out), out
    assert isinstance(out["issues"], list) and isinstance(out["warnings"], list), out
    return proc.returncode, out


def _ok(boards: dict, mode: str, pcb: Path, design: dict, name: str) -> dict:
    rc, out = _gate(boards, mode, pcb, design, name)
    assert rc == 0, out
    return out


def _mating(out: dict) -> list[str]:
    """Refs the connector rule reports (other G3 rules may report the same parts)."""
    return sorted(m.group(1) for m in map(MATING.match, out["issues"]) if m)


def _about(texts: list[str], word: str) -> list[str]:
    return [t for t in texts if word in t]


def _connectors(*interior: str) -> dict:
    return {"components": [{"ref": r, **({"interior": True} if r in interior else {})}
                           for r in ("J1", "J2", "J3")]}


# --- live: the T02 regression -------------------------------------------------------------
def test_the_premise_load_board_courtyard_cache_per_version(boards: dict) -> None:
    """Measured: 9.0.4 hands back an EMPTY courtyard after LoadBoard (so (a)-(c)
    exercise the T02 path there); 10.0.5 builds it lazily."""
    empty_after_load = boards["version"].startswith("9.")
    assert boards["courtyard_outlines_after_load"] == (0 if empty_after_load else 1), boards


def test_a_inward_connector_is_flagged_by_main(boards: dict) -> None:
    out = _ok(boards, "placement", boards["gates"], _connectors(), "connectors")
    assert [i for i in out["issues"] if i.startswith(INWARD)] != [], out
    assert _mating(out).count("J1") == 1, out


def test_b_far_connector_flagged_edge_connector_and_interior_part_not(boards: dict) -> None:
    out = _ok(boards, "placement", boards["gates"], _connectors(), "connectors")
    far = [m for m in map(FAR.match, out["issues"]) if m]
    assert len(far) == 1 and 4.5 < float(far[0].group(1)) < 6.5, out
    assert _mating(out) == ["J1", "J2"], out          # J3 mates through the left edge
    exempt = _ok(boards, "placement", boards["gates"], _connectors("J1"), "interior")
    assert _mating(exempt) == ["J2"], exempt


def test_b_placement_routed_mode_runs_the_same_connector_rule(boards: dict) -> None:
    out = _ok(boards, "placement_routed", boards["gates"], _connectors(), "connectors_routed")
    assert _mating(out) == ["J1", "J2"], out


def test_c_check_heights_measures_the_courtyard_not_the_text(boards: dict) -> None:
    # board frame = Edge.Cuts bbox top-left: R1's courtyard spans ~(16.6..19.5, 5.3..6.8),
    # its reference text sits around (18, 13)
    design = {"components": [{"ref": "R1", "height": 5.0}],
              "height_zones": [{"name": "text_zone", "x": 16, "y": 12, "w": 4, "h": 2,
                                "max_height": 2.0},
                               {"name": "body_zone", "x": 17, "y": 5, "w": 2, "h": 2,
                                "max_height": 2.0}]}
    out = _ok(boards, "placement", boards["gates"], design, "heights")
    assert _about(out["issues"], "_zone") == [
        "R1: 5.0 mm tall inside body_zone with a 2.0 mm ceiling (F side) — move it out, "
        "use a lower package, or exempt it if it IS the standoff"], out
    assert _about(out["warnings"], "_zone") == [], out


def test_d_antenna_keepout_rule_area_is_found_on_every_version(boards: dict) -> None:
    design = {"antenna_keepout": {"x": 25, "y": 2, "w": 10, "h": 8}}
    out = _ok(boards, "placement", boards["gates"], design, "keepout")
    assert _about(out["issues"], "antenna_keepout") == [], out
    bare = _ok(boards, "placement", boards["bare"], design, "keepout_bare")
    assert _about(bare["issues"], "antenna_keepout") == [NO_KEEPOUT], bare


def test_e_routed_mode_runs_the_moved_checks(boards: dict) -> None:
    out = _ok(boards, "routed", boards["gates"], {"net_specs": {"VIN": {"current": 5.0}}},
              "routed")
    cap = round(ipc_rules.via_ampacity_a(0.3), 2)
    assert [i for i in out["issues"] if i.startswith("VIN:")] == [
        f"VIN: 5.0 A crosses layers through 1 via(s) + 0 PTH barrel(s) totaling {cap} A "
        f"(IPC-2221) — add parallel vias at the transition"], out
    assert [w for w in out["warnings"] if w.startswith("VIN:")] == [], out


def test_f_missing_board_is_one_json_issue_not_a_traceback(boards: dict) -> None:
    missing = boards["work"] / "no_such_board.kicad_pcb"
    rc, out = _gate(boards, "placement", missing, {}, "missing")
    assert rc == 1, out
    assert len(out["issues"]) == 1 and "board file not found" in out["issues"][0], out
    assert str(missing) in out["issues"][0] and out["warnings"] == [], out


def test_f_unreadable_netlist_still_checks_and_says_so(boards: dict) -> None:
    rc, out = _gate(boards, "placement", boards["gates"], None, "never_written")
    assert rc == 0 and _mating(out) == ["J1", "J2"], out
    said = [w for w in out["warnings"] if w.startswith("netlist ")]
    assert len(said) == 1 and said[0].startswith(
        f"netlist {boards['work'] / 'never_written.json'} not read (FileNotFoundError"), out


# --- static: the split's shape and the contracts -------------------------------------------
def _body(path: Path) -> list[ast.stmt]:
    return ast.parse(path.read_text(encoding="utf-8")).body


def _defs(path: Path) -> set[str]:
    return {n.name for n in _body(path) if isinstance(n, ast.FunctionDef)}


def _imported(path: Path, module: str | None = None) -> set[str]:
    """Names a top-level `from <module> import ...` binds (any module when None)."""
    return {a.asname or a.name for n in _body(path) if isinstance(n, ast.ImportFrom)
            and (module is None or n.module == module) for a in n.names}


def test_routed_checks_moved_and_re_exported() -> None:
    assert set(MOVED) <= _defs(ROUTED)
    assert not set(MOVED) & _defs(GATE)
    assert set(MOVED) <= _imported(GATE, "swig_routed_checks")


def test_contract_names_and_texts_stay_in_swig_gate_checks() -> None:
    # F5-09's probe imports these from swig_gate_checks (defined there or re-exported)
    assert {"_world_mouth", "is_edge_connector", "interior_refs", "main"} <= (
        _defs(GATE) | _imported(GATE))
    source = GATE.read_text(encoding="utf-8")
    for text in ("mating face points INTO the board interior", "mating face is ",
                 "mm from the board ", "fractured into separate islands",
                 "dissipating exposed-pad part has only"):
        assert text in source, text
    assert len(source.splitlines()) < 700


def test_the_pour_flag_is_never_read_with_a_single_version_getter() -> None:
    source = GATE.read_text(encoding="utf-8")
    assert "GetDoNotAllowZoneFills" not in source and "GetDoNotAllowCopperPour" not in source
