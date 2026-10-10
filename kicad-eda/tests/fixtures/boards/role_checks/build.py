"""KiCad-Python builder of the D7-18a layer-role boards (tests/test_swig_layer_role_checks_live.py).

usage: <KiCad python> build.py <out_dir> <server/tools> <Capacitor_SMD.pretty>

Deterministic boards (fixed coordinates, 40 x 30 mm at (100, 100)), each built from a
committed design next to this file: the stackup (swig_stackup.load_or_resolve +
apply_layers), one pour per plane layer on its net, the tracks / vias / capacitor below,
SaveBoard, then swig_stackup.finalize (stackup block spliced, fresh load, fill, save,
verified). Writes <name>.kicad_pcb and <name>.json (its netlist) into out_dir.

  foreign_4l        role4l: exactly one foreign track - SIG_A on In1.Cu, the GND plane
                    (plus own-net GND / +3V3 tracks on their planes, which are not foreign)
  clean_4l          the twin: that SIG_A segment on B.Cu
  transition_cap    role4l: Z50 F.Cu -> via -> B.Cu; F.Cu rests on In1 GND, B.Cu on In2
                    +3V3; C1 (+3V3 / GND) with its nearest pad 1.7 mm from the via
  transition_nocap  the same without C1
  transition_farcap the same with C1 6 mm away
  dual_6l           dual6l: ZS on In2.Cu (a dual stripline under In3.Cu), AUX 8 mm parallel
                    right over it on In3, SHORT 4 mm parallel, XING crossing at 90 deg,
                    OFFSIDE 8 mm parallel 2 mm to the side

The pre-D7 board cp2102n_pre_d7.kicad_pcb beside this file is not built here: it is the
routed board of today's (pre-D7, before D7-19's DSN use_layer) flow, committed as made by
HEAD ebbd57b's pipeline/run_pipeline.py at 50 x 50 mm on KiCad 9.0.4 + Freerouting 1.9.0
(G1-G5 PASS) from examples/cp2102n_uart/netlist.json with J1.SH moved from GND to
no_connect (cp2102n_pre_d7.json): KiCad 9.0.4's HRO TYPE-C-31-M-12 footprint names its shield
pads S1, so the example's J1.SH stops G2/G3 there. Its foreign count is pinned by the test.
Prints one JSON object line: {version, python, boards {name: {pcb, netlist, finalize} |
{error}}}.
"""
import json
import shutil
import sys
import traceback
from pathlib import Path
from typing import Any

sys.path.insert(0, sys.argv[2])
import kicad_quiet  # noqa: E402,F401  (before pcbnew: no modal wx dialogs)
import pcbnew  # noqa: E402

import board_outline as bo  # noqa: E402
import board_shapes  # noqa: E402
import swig_stackup as ss  # noqa: E402

HERE = Path(__file__).resolve().parent
OX, OY, W, H = 100.0, 100.0, 40.0, 30.0
CAP = "C_0603_1608Metric"

Z50_PATH = [("Z50", "F.Cu", (105, 115), (120, 115), 0.2),
            ("Z50", "B.Cu", (120, 115), (135, 115), 0.2)]
Z50_VIA = [("Z50", (120, 115))]
PLANE_TRACKS = [("SIG_A", "F.Cu", (110, 110), (130, 110), 0.2),
                ("GND", "In1.Cu", (112, 125), (128, 125), 0.5),
                ("+3V3", "In2.Cu", (112, 127), (128, 127), 0.5)]

BOARDS = {
    "foreign_4l": {"design": "role4l",
                   "tracks": [*PLANE_TRACKS, ("SIG_A", "In1.Cu", (130, 110), (130, 120), 0.2)]},
    "clean_4l": {"design": "role4l",
                 "tracks": [*PLANE_TRACKS, ("SIG_A", "B.Cu", (130, 110), (130, 120), 0.2)]},
    "transition_cap": {"design": "role4l", "tracks": Z50_PATH, "vias": Z50_VIA,
                       "caps": [("C1", (120, 116.5))]},
    "transition_nocap": {"design": "role4l", "tracks": Z50_PATH, "vias": Z50_VIA},
    "transition_farcap": {"design": "role4l", "tracks": Z50_PATH, "vias": Z50_VIA,
                          "caps": [("C1", (120, 121))]},
    "dual_6l": {"design": "dual6l", "tracks": [
        ("ZS", "In2.Cu", (110, 115), (130, 115), 0.15),
        ("AUX", "In3.Cu", (115, 115), (123, 115), 0.15),
        ("SHORT", "In3.Cu", (124.5, 115), (128.5, 115), 0.15),
        ("XING", "In3.Cu", (113, 105), (113, 125), 0.15),
        ("OFFSIDE", "In3.Cu", (114, 117), (122, 117), 0.15)]},
}


def _pt(xy: tuple) -> Any:
    return pcbnew.VECTOR2I(bo.nm(xy[0]), bo.nm(xy[1]))


def _nets(board: Any, design: dict) -> dict:
    nets = {}
    for name in design["nets"]:
        nets[name] = pcbnew.NETINFO_ITEM(board, name)
        board.Add(nets[name])
    return nets


def _add_tracks(board: Any, nets: dict, tracks: list) -> None:
    for net, layer, a, b, width in tracks:
        t = pcbnew.PCB_TRACK(board)
        t.SetStart(_pt(a))
        t.SetEnd(_pt(b))
        t.SetWidth(bo.nm(width))
        t.SetLayer(board.GetLayerID(layer))
        t.SetNet(nets[net])
        board.Add(t)


def _add_vias(board: Any, nets: dict, vias: list) -> None:
    for net, at in vias:
        v = pcbnew.PCB_VIA(board)
        v.SetPosition(_pt(at))
        v.SetWidth(bo.nm(0.6))
        v.SetDrill(bo.nm(0.3))
        v.SetViaType(pcbnew.VIATYPE_THROUGH)
        v.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
        v.SetNet(nets[net])
        board.Add(v)


def _add_caps(board: Any, nets: dict, caps: list, cap_dir: str) -> None:
    for ref, at in caps:
        fp = pcbnew.FootprintLoad(cap_dir, CAP)
        if fp is None:
            raise RuntimeError(f"FootprintLoad found no {CAP} in {cap_dir}")
        fp.SetReference(ref)
        fp.SetPosition(_pt(at))
        board.Add(fp)
        for pad in fp.Pads():
            pad.SetNet(nets["+3V3" if pad.GetNumber() == "1" else "GND"])


def build(name: str, spec: dict, out_dir: str, cap_dir: str) -> dict:
    design_path = HERE / f"{spec['design']}.json"
    design = json.loads(design_path.read_text(encoding="utf-8"))
    rs = ss.load_or_resolve(design).stackup
    board = pcbnew.CreateEmptyBoard()
    ss.apply_layers(board, rs)
    board_shapes.add_outline(board, W, H, OX, OY)
    nets = _nets(board, design)
    for copper in rs.copper:
        if copper.role == "plane":
            board_shapes.add_plane_outline(board, board.GetLayerID(copper.name),
                                           nets[copper.net])
    _add_tracks(board, nets, spec.get("tracks", []))
    _add_vias(board, nets, spec.get("vias", []))
    _add_caps(board, nets, spec.get("caps", []), cap_dir)
    pcb = Path(out_dir) / f"{name}.kicad_pcb"
    if pcbnew.SaveBoard(str(pcb), board) is False:
        raise RuntimeError(f"SaveBoard failed for {pcb}")
    result = ss.finalize(pcb, rs)
    netlist = Path(out_dir) / f"{name}.json"
    shutil.copyfile(design_path, netlist)
    return {"pcb": str(pcb), "netlist": str(netlist),
            "finalize": {"ok": result["ok"], "problems": result["problems"]}}


def main() -> None:
    out_dir, cap_dir = sys.argv[1], sys.argv[3]
    boards = {}
    for name, spec in BOARDS.items():
        try:
            boards[name] = build(name, spec, out_dir, cap_dir)
        except Exception:
            boards[name] = {"error": traceback.format_exc()}
    print(json.dumps({"version": pcbnew.Version(), "python": sys.version.split()[0],
                      "boards": boards}))


main()
