"""Build the kimport fixture board with KiCad 9's pcbnew (run by
make_kimport.py under KiCad's bundled Python; not part of the test run).

Usage: <kicad-python> make_kimport_board.py <pad_nets.json> <out.kicad_pcb> <footprints_dir>

pad_nets.json: {"REF.PAD": "net name"} exactly as the schematic's XML netlist
names them (what KiCad's "Update PCB from schematic" would assign). The board
exercises what the importer must carry: a rounded outline (arcs) with a round
cutout, a slot drawn on Edge.Cuts inside a rotated footprint, an F-side part
at 270 degrees, a B-side part at 45 degrees, board-only mounting holes (one
with a pad on GND), DNP / exclude-from-BOM attributes and an aux origin.
"""
import wx
wx.DisableAsserts()
wx.Log.SetActiveTarget(wx.LogStderr())

import json  # noqa: E402
import sys  # noqa: E402

import pcbnew  # noqa: E402

# ref -> (library, footprint, x, y, rot, side)
PLACEMENT = {
    "U1": ("Package_SO", "SOIC-8_3.9x4.9mm_P1.27mm", 115.0, 95.0, 0.0, "F"),
    "J1": ("Connector_PinHeader_2.54mm", "PinHeader_1x06_P2.54mm_Vertical",
           104.0, 98.0, 270.0, "F"),
    "J2": ("Connector_PinHeader_2.54mm", "PinHeader_1x02_P2.54mm_Vertical",
           134.0, 86.0, 0.0, "F"),
    "C1": ("Capacitor_SMD", "C_0603_1608Metric", 120.5, 101.5, 45.0, "B"),
    "C2": ("Capacitor_SMD", "C_0603_1608Metric", 112.0, 87.5, 0.0, "F"),
    "R1": ("Resistor_SMD", "R_0603_1608Metric", 125.0, 92.0, 90.0, "F"),
    "R2": ("Resistor_SMD", "R_0603_1608Metric", 125.0, 98.5, 0.0, "F"),
    "H1": ("MountingHole", "MountingHole_3.2mm_M3_Pad", 104.5, 105.5, 0.0, "F"),
    "H2": ("MountingHole", "MountingHole_3.2mm_M3", 135.5, 104.0, 30.0, "F"),
}
VALUES = {"U1": "LM358", "J1": "IO", "J2": "AUX", "C1": "100nF", "C2": "1uF", "R1": "1k",
          "R2": "10k", "H1": "MountingHole_3.2mm_M3_Pad", "H2": "MountingHole_3.2mm_M3"}
BOARD_X, BOARD_Y, BOARD_W, BOARD_H, CORNER = 100.0, 80.0, 40.0, 30.0, 2.0


def mm(v):
    return pcbnew.FromMM(float(v))


def pt(x, y):
    return pcbnew.VECTOR2I(mm(x), mm(y))


def edge(parent, kind, *pts):
    shape = pcbnew.PCB_SHAPE(parent)
    shape.SetLayer(pcbnew.Edge_Cuts)
    shape.SetWidth(mm(0.1))
    if kind == "line":
        shape.SetShape(pcbnew.SHAPE_T_SEGMENT)
        shape.SetStart(pts[0])
        shape.SetEnd(pts[1])
    elif kind == "arc":
        shape.SetShape(pcbnew.SHAPE_T_ARC)
        shape.SetArcGeometry(pts[0], pts[1], pts[2])
    else:
        shape.SetShape(pcbnew.SHAPE_T_CIRCLE)
        shape.SetCenter(pts[0])
        shape.SetEnd(pts[1])
    parent.Add(shape)
    return shape


def outline(board):
    x0, y0, x1, y1, r = BOARD_X, BOARD_Y, BOARD_X + BOARD_W, BOARD_Y + BOARD_H, CORNER
    k = r * (1 - 0.5 ** 0.5)
    edge(board, "line", pt(x0 + r, y0), pt(x1 - r, y0))
    edge(board, "arc", pt(x1 - r, y0), pt(x1 - k, y0 + k), pt(x1, y0 + r))
    edge(board, "line", pt(x1, y0 + r), pt(x1, y1 - r))
    edge(board, "arc", pt(x1, y1 - r), pt(x1 - k, y1 - k), pt(x1 - r, y1))
    edge(board, "line", pt(x1 - r, y1), pt(x0 + r, y1))
    edge(board, "arc", pt(x0 + r, y1), pt(x0 + k, y1 - k), pt(x0, y1 - r))
    edge(board, "line", pt(x0, y1 - r), pt(x0, y0 + r))
    edge(board, "arc", pt(x0, y0 + r), pt(x0 + k, y0 + k), pt(x0 + r, y0))
    edge(board, "circle", pt(131.0, 95.0), pt(132.5, 95.0))        # round cutout, r = 1.5


def slot(fp):
    """A 3 x 1 mm rounded slot on Edge.Cuts, 4 mm below the footprint origin
    (footprint frame), drawn while the footprint sits at (0, 0) rot 0."""
    edge(fp, "line", pt(-1.0, 3.5), pt(1.0, 3.5))
    edge(fp, "arc", pt(1.0, 3.5), pt(1.5, 4.0), pt(1.0, 4.5))
    edge(fp, "line", pt(1.0, 4.5), pt(-1.0, 4.5))
    edge(fp, "arc", pt(-1.0, 4.5), pt(-1.5, 4.0), pt(-1.0, 3.5))


def main(argv):
    pad_nets_path, out, fp_dir = argv[1], argv[2], argv[3]
    with open(pad_nets_path, encoding="utf-8") as f:
        pad_nets = json.load(f)
    board = pcbnew.CreateEmptyBoard()
    board.SetCopperLayerCount(2)
    nets = {}
    for name in sorted(set(pad_nets.values())):
        nets[name] = pcbnew.NETINFO_ITEM(board, name)
        board.Add(nets[name])
    outline(board)
    for ref, (lib, name, x, y, rot, side) in PLACEMENT.items():
        fp = pcbnew.FootprintLoad(f"{fp_dir}/{lib}.pretty", name)
        if fp is None:
            raise RuntimeError(f"cannot load {lib}:{name}")
        fp.SetFPID(pcbnew.LIB_ID(lib, name))
        fp.SetReference(ref)
        fp.SetValue(VALUES[ref])
        if ref == "H2":
            slot(fp)
        board.Add(fp)
        fp.SetPosition(pt(x, y))
        if side == "B":
            fp.Flip(fp.GetPosition(), pcbnew.FLIP_DIRECTION_TOP_BOTTOM)
        fp.SetOrientationDegrees(rot)
        if ref.startswith("H"):
            fp.SetBoardOnly(True)
            fp.SetExcludedFromBOM(True)
            fp.SetExcludedFromPosFiles(True)
        for pad in fp.Pads():
            net = pad_nets.get(f"{ref}.{pad.GetNumber()}") or (
                "/GND" if ref == "H1" and pad.GetNumber() else None)
            if net:
                pad.SetNet(nets[net])
    board.FindFootprintByReference("C2").SetDNP(True)
    board.FindFootprintByReference("R2").SetExcludedFromBOM(True)
    board.GetDesignSettings().SetAuxOrigin(pt(BOARD_X, BOARD_Y + BOARD_H))
    if not pcbnew.SaveBoard(out, board):
        raise RuntimeError(f"SaveBoard failed for {out}")
    print(json.dumps({"ok": True, "out": out, "footprints": len(board.GetFootprints())}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
