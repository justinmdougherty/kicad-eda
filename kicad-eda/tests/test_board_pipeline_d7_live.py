"""Live tier of D7-23a (KiCad 9.0.4 + Java + the bundled Freerouting 1.9.0): the MCP board
tools on a 6-layer board driven by the physical-design artifact.

examples/cp2102n_uart (its J1.SH shield pad dropped, as in tests/test_swig_build_stackup_live.py:
the stock USB-C footprints name it differently per release) with the ADR D7.2.2 6L stackup
(JLC06161H-2116, roles SIG / GND / SIG / SIG / +3V3 / SIG) is built from proj/netlist.json into
out/board.kicad_pcb (another folder and stem: the board-stem mirror), routed, DRC-checked and
packaged through board_pipeline_tools:

- build_board_from_netlist: 6 copper layers, <netlist stem>.stackup.json, the managed
  <board stem>.kicad_dru beside the board, a byte-identical <board stem>.stackup.json;
- autoroute_board: the DSN layer policy (plane nets in their own classes), 0 foreign-net
  tracks on In1.Cu (GND) and In4.Cu (+3V3), 0 unconnected;
- drc_pcb: 0 errors, 0 unconnected (the managed rules included);
- export_fab_package: the zip holds the job file with 16 thickness rows and inner copper
  renumbered L2..L5, <stem>_ipc2581.xml, <stem>_stackup.csv and <stem>_fab_notes.md;
- add_ground_stitching (on a copy): emc_stitch follows the roles and DRC stays 0 / 0.

Skipped without KiCad's bundled Python (pcbnew), kicad-cli, Java or the Freerouting jar.
"""
from __future__ import annotations

import json
import os
import shutil
import zipfile

import pytest

import board_pipeline_tools as bpt
import physics_resolve as pr
import toolchain as tc
from conftest import load_example
from fixtures import swig_python as sp

SIX = {"preset": "jlcpcb:JLC06161H-2116", "impedance_control": {"required": True},
       "rationale": "6L: two inner routing layers for the fan-out"}
PLANES = {"In1.Cu": "GND", "In4.Cu": "+3V3"}
CU6 = ["F.Cu", "In1.Cu", "In2.Cu", "In3.Cu", "In4.Cu", "B.Cu"]
MAX_PASSES, ROUTE_TIMEOUT_S, CHILD_TIMEOUT_S = 600, 1500, 300

COUNT = sp.PREAMBLE + r"""
import collections
board = pcbnew.LoadBoard(sys.argv[1])
if board is None:
    raise SystemExit(f"cannot load {sys.argv[1]}")
planes = json.loads(sys.argv[2])                 # {plane layer: its net}
per_layer, foreign = collections.Counter(), collections.Counter()
for t in board.GetTracks():
    if t.GetClass() == "PCB_VIA":
        continue
    layer = board.GetLayerName(t.GetLayer())
    per_layer[layer] += 1
    if layer in planes and t.GetNetname() != planes[layer]:
        foreign[f"{layer}:{t.GetNetname()}"] += 1
copper = [board.GetLayerName(i) for i in board.GetEnabledLayers().CuStack()]
print(json.dumps({"copper": copper, "per_layer": per_layer, "foreign": foreign}))
"""


def _skip_unless_live() -> None:
    if not tc.kicad_python():
        pytest.skip(f"no KiCad pcbnew Python: {tc.kicad_python_error()}")
    if not tc.board_cli():
        pytest.skip("kicad-cli not found")
    if not tc.find_java() or not tc.freerouting_jar(""):
        pytest.skip("Java or the Freerouting jar is missing")


@pytest.fixture(scope="module")
def six(tmp_path_factory: pytest.TempPathFactory) -> dict:
    """proj/netlist.json -> out/board.kicad_pcb: build, route, DRC, fab package."""
    _skip_unless_live()
    root = tmp_path_factory.mktemp("d7_six")
    design = load_example("cp2102n_uart")
    design = {**design, "stackup": SIX, "nets": {n: [p for p in conns if p != ["J1", "SH"]]
                                                 for n, conns in design["nets"].items()}}
    netlist = root / "proj" / "netlist.json"
    netlist.parent.mkdir()
    netlist.write_text(json.dumps(design, indent=1), encoding="utf-8")
    pcb = root / "out" / "board.kicad_pcb"
    pcb.parent.mkdir()
    built = bpt.build_board_from_netlist(str(netlist), 50, 50, out_pcb=str(pcb))
    assert built.get("ok") is True, built
    routed = bpt.autoroute_board(str(pcb), MAX_PASSES, "", ROUTE_TIMEOUT_S)
    counts = sp.run_child(COUNT, [str(pcb), json.dumps(PLANES)], root, CHILD_TIMEOUT_S)
    drc = bpt.drc_pcb(str(pcb))
    fab = bpt.export_fab_package(str(pcb), str(root / "out" / "fab"), netlist=str(netlist))
    print("d7 six", json.dumps({"routed": {k: routed.get(k) for k in (
        "ok", "tracks", "vias", "unconnected")}, "counts": counts, "drc": drc.get("errors")}))
    return {"root": root, "netlist": netlist, "pcb": pcb, "design": design, "built": built,
            "routed": routed, "counts": counts, "drc": drc, "fab": fab}


def test_the_build_writes_the_artifact_dru_and_mirror(six: dict) -> None:
    built, pcb, netlist = six["built"], six["pcb"], six["netlist"]
    art = pr.artifact_path(netlist)
    assert built["physics"]["artifact"] == os.path.abspath(art) and built["physics"]["errors"] == []
    mirror = pcb.with_name("board.stackup.json")
    assert built["physics"]["mirror"] == os.path.abspath(mirror)
    assert mirror.read_bytes() == art.read_bytes()
    assert pr.staleness(art, six["design"], board_path=pcb) is None     # DRU at the board
    dru = pcb.with_suffix(".kicad_dru").read_text(encoding="utf-8")
    assert "kicad_eda_plane_In1.Cu" in dru and "kicad_eda_plane_In4.Cu" in dru
    assert built["stackup"]["copper"] == CU6 and built["stackup"]["from"] == "artifact"
    assert six["counts"]["copper"] == CU6


def test_the_route_keeps_the_planes_clean(six: dict) -> None:
    routed, counts = six["routed"], six["counts"]
    assert routed["ok"] is True and routed["unconnected"] == 0, routed
    policy = routed["dsn_policy"]
    assert policy["variant"] == "restricted" and policy["pad_layer_excluded"] == {}
    assert policy["nets_moved"] == {"+3V3": "kicad_eda_plane_+3V3", "GND": "kicad_eda_plane_GND"}
    assert routed["physics"]["artifact"] == str(six["pcb"].with_name("board.stackup.json"))
    assert counts["foreign"] == {}, counts
    signal = set(counts["per_layer"]) - set(PLANES)        # Freerouting picks among the four
    assert counts["per_layer"].get("F.Cu") and len(signal) >= 2, counts


def test_drc_is_clean_with_the_managed_rules(six: dict) -> None:
    drc = six["drc"]
    assert (drc["errors"], drc["unconnected"]) == (0, 0), drc


def test_the_zip_carries_the_stackup(six: dict) -> None:
    fab = six["fab"]
    assert fab["ok"] is True and fab["fab_stackup"]["problems"] == [], fab
    job_rec = fab["fab_stackup"]["gbrjob"]
    assert (job_rec["rows"], job_rec["expected_rows"]) == (16, 16)
    with zipfile.ZipFile(fab["zip"]) as z:
        names = set(z.namelist())
        job = json.loads(z.read("board-job.gbrjob"))
        xml = z.read("board_ipc2581.xml")
        notes = z.read("board_fab_notes.md").decode("utf-8")
        table = z.read("board_stackup.csv").decode("utf-8")
    assert {"board_stackup.csv", "board_fab_notes.md", "board_ipc2581.xml"} <= names
    copper = {e["Path"]: e["FileFunction"] for e in job["FilesAttributes"]
              if e["FileFunction"].startswith("Copper,")}
    assert [copper[f"board-In{k}_Cu.g{k}"] for k in range(1, 5)] == [
        f"Copper,L{k},Inr" for k in range(2, 6)]
    rows = [r for r in job["MaterialStackup"] if "Thickness" in r]
    assert len(rows) == 16 and job["GeneralSpecs"]["ImpedanceControlled"] is True
    assert b"IPC-2581" in xml[:4000] and "JLC06161H-2116" in notes and "In4.Cu" in table


def test_stitching_follows_the_roles_and_keeps_drc_clean(six: dict) -> None:
    folder = six["root"] / "stitch"
    shutil.copytree(six["pcb"].parent, folder, ignore=shutil.ignore_patterns("fab", "*.zip"))
    pcb = folder / "board.kicad_pcb"
    res = bpt.add_ground_stitching(str(pcb))
    mirror = str(folder / "board.stackup.json")
    assert res["physics"] == {"artifact": mirror}, res
    if res["ok"]:                                  # emc_stitch ran with --stackup
        assert res["stackup"]["artifact"] == mirror and res["vias_added"] > 0, res
    drc = bpt.drc_pcb(str(pcb))
    assert (drc["errors"], drc["unconnected"]) == (0, 0), (res, drc)
