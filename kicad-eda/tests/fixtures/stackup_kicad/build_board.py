"""KiCad-Python builder for tests/test_swig_stackup_live.py (D7-03, ADR D7.5).

usage: <KiCad python> build_board.py <boards.json> <out_dir> <footprints dir> <server/tools>

boards.json is [{"name": ..., "design": {...}}]. Per board: the stackup from the
design (swig_stackup.load_or_resolve), apply_layers, a 40 x 30 mm outline, one pour
per plane / mixed layer on its net (board_shapes.add_plane_outline), two
through-hole pin headers whose pins sit on the plane nets (every pad reaches its
net through a plane, so DRC has nothing unconnected), SaveBoard, finalize, then a
second LoadBoard + SaveBoard to <name>.rt.kicad_pcb (the block must survive it).
A board that raises is reported with its traceback; the others still build.
Prints one JSON object line.
"""
import json
import sys
import traceback

sys.path.insert(0, sys.argv[4])
import kicad_quiet  # noqa: E402,F401  (before pcbnew: no modal wx dialogs)
import pcbnew  # noqa: E402

import board_outline as bo  # noqa: E402
import board_shapes  # noqa: E402
import swig_stackup as ss  # noqa: E402

OX, OY, W, H = 100.0, 100.0, 40.0, 30.0
HEADER = "PinHeader_1x{n:02d}_P2.54mm_Vertical"
NET_ROLES = ("plane", "mixed")


def plane_nets(rs):
    return sorted({c.net for c in rs.copper if c.role in NET_ROLES and c.net})


def add_headers(board, fp_dir, nets):
    for ref, x in (("J1", OX + 10.0), ("J2", OX + 30.0)):
        fp = pcbnew.FootprintLoad(fp_dir, HEADER.format(n=len(nets)))
        if fp is None:
            raise RuntimeError(f"FootprintLoad found no {HEADER.format(n=len(nets))} in {fp_dir}")
        fp.SetReference(ref)
        fp.SetPosition(pcbnew.VECTOR2I(bo.nm(x), bo.nm(OY + 10.0)))
        board.Add(fp)
        for pad in fp.Pads():
            pad.SetNet(nets[int(pad.GetNumber()) - 1])


def build(spec, out_dir, fp_dir):
    rs = ss.load_or_resolve(spec["design"]).stackup
    board = pcbnew.CreateEmptyBoard()
    applied = ss.apply_layers(board, rs)
    board_shapes.add_outline(board, W, H, OX, OY)
    nets = {}
    for name in plane_nets(rs):
        nets[name] = pcbnew.NETINFO_ITEM(board, name)
        board.Add(nets[name])
    for copper in rs.copper:
        if copper.role in NET_ROLES:
            board_shapes.add_plane_outline(board, board.GetLayerID(copper.name), nets[copper.net])
    add_headers(board, fp_dir, [nets[n] for n in plane_nets(rs)])
    pcb = f"{out_dir}/{spec['name']}.kicad_pcb"
    if pcbnew.SaveBoard(pcb, board) is False:
        raise RuntimeError(f"SaveBoard failed for {pcb}")
    result = ss.finalize(pcb, rs)
    rt_path = f"{out_dir}/{spec['name']}.rt.kicad_pcb"
    rt = bo.load_board(pcb)
    pcbnew.SaveBoard(rt_path, rt)
    copper = [rt.GetLayerName(lid) for lid in rt.GetEnabledLayers().CuStack()]
    settings = rt.GetDesignSettings()
    return {"pcb": pcb, "rt": rt_path, "applied": applied, "finalize": result, "copper": copper,
            "layer_types": {n: int(rt.GetLayerType(rt.GetLayerID(n))) for n in copper},
            "has_stackup": bool(settings.m_HasStackup),
            "thickness_mm": pcbnew.ToMM(settings.GetBoardThickness())}


def main():
    with open(sys.argv[1], encoding="utf-8") as f:
        specs = json.load(f)
    boards = {}
    for spec in specs:
        try:
            boards[spec["name"]] = build(spec, sys.argv[2], sys.argv[3])
        except Exception:
            boards[spec["name"]] = {"error": traceback.format_exc()}
    print(json.dumps({"version": pcbnew.Version(), "python": sys.version.split()[0],
                      "lt": {"signal": int(pcbnew.LT_SIGNAL), "power": int(pcbnew.LT_POWER),
                             "mixed": int(pcbnew.LT_MIXED)},
                      "boards": boards}))


main()
