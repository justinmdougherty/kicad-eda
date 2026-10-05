"""Build the faithful-rebuild DRC fixture board with KiCad's pcbnew (run under
KiCad's bundled Python; not part of the test run).

Usage: <kicad-python> make_drc_board.py <out.kicad_pcb> [<share/kicad/footprints>]
(tests/test_faithful.py also runs it live and checks the DRC split.)

Then (KiCad 9.0.4, stdin closed):
  kicad-cli pcb drc <out.kicad_pcb> --severity-error --format json -o drc_mixed.json
  kicad-cli pcb drc <out.kicad_pcb> --severity-all --format json -o drc_mixed_all.json
The board is committed as mixed_board.sexpr (no .kicad_pcb suffix, which
.gitignore drops; DRC needs no .kicad_pro: the default rules apply).

A 40 x 30 mm two-layer board with three kinds of DRC error and one
unconnected pair, the cases faithful.split_drc must tell apart:

* JP1, a "normally closed" trace jumper: two pads on nets A and B joined by
  a copper segment drawn INSIDE the footprint (no net-tie group). Its
  violations involve JP1's items only (intrinsic to the footprint).
* R1 / R2 placed with overlapping courtyards (pads still clear): a
  footprint-to-footprint violation, source placement in keep mode.
* A track on SIG passing 0.05 mm from R3's pad 1 (OTHER): a routing
  violation, ours in every mode.
* N1 on R1.1 and R2.1, not routed: one unconnected item.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "server" / "tools"))
import kicad_quiet  # noqa: E402,F401  (before pcbnew: no modal wx dialogs)
import pcbnew  # noqa: E402

DEFAULT_FOOTPRINTS = Path(r"C:\Program Files\KiCad\9.0\share\kicad\footprints")
X0, Y0, W, H = 100.0, 80.0, 40.0, 30.0
R0603 = ("Resistor_SMD", "R_0603_1608Metric")


def mm(v: float) -> int:
    return pcbnew.FromMM(float(v))


def pt(x: float, y: float) -> pcbnew.VECTOR2I:
    return pcbnew.VECTOR2I(mm(X0 + x), mm(Y0 + y))


def outline(board: pcbnew.BOARD) -> None:
    corners = [(0, 0), (W, 0), (W, H), (0, H)]
    for a, b in zip(corners, corners[1:] + corners[:1], strict=True):
        seg = pcbnew.PCB_SHAPE(board)
        seg.SetShape(pcbnew.SHAPE_T_SEGMENT)
        seg.SetLayer(pcbnew.Edge_Cuts)
        seg.SetWidth(mm(0.1))
        seg.SetStart(pt(*a))
        seg.SetEnd(pt(*b))
        board.Add(seg)


def net(board: pcbnew.BOARD, name: str) -> pcbnew.NETINFO_ITEM:
    item = pcbnew.NETINFO_ITEM(board, name)
    board.Add(item)
    return item


def place(board: pcbnew.BOARD, libs: Path, ref: str, x: float, y: float,
          nets: dict) -> pcbnew.FOOTPRINT:
    lib, name = R0603
    fp = pcbnew.FootprintLoad(str(libs / f"{lib}.pretty"), name)
    if fp is None:
        raise SystemExit(f"footprint {lib}:{name} not found")
    fp.SetReference(ref)
    fp.SetPosition(pt(x, y))
    board.Add(fp)
    for pad in fp.Pads():
        if pad.GetNumber() in nets:
            pad.SetNet(nets[pad.GetNumber()])
    return fp


def trace_jumper(board: pcbnew.BOARD, ref: str, x: float, y: float,
                 net_a: pcbnew.NETINFO_ITEM, net_b: pcbnew.NETINFO_ITEM) -> pcbnew.FOOTPRINT:
    """Two 1.0 x 1.5 mm SMD pads 1.3 mm apart joined by a 0.3 mm copper
    segment that belongs to the footprint (an NC trace jumper)."""
    fp = pcbnew.FOOTPRINT(board)
    fp.SetReference(ref)
    fp.SetValue("NC_TRACE")
    fp.SetFPID(pcbnew.LIB_ID("Fixture", "TraceJumper_NC"))
    board.Add(fp)
    fp.SetPosition(pt(x, y))
    for number, dx, item in (("1", -0.65, net_a), ("2", 0.65, net_b)):
        pad = pcbnew.PAD(fp)
        pad.SetNumber(number)
        pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
        pad.SetShape(pcbnew.PAD_SHAPE_RECT)
        pad.SetSize(pcbnew.VECTOR2I(mm(1.0), mm(1.5)))
        pad.SetLayerSet(pcbnew.PAD.SMDMask())
        fp.Add(pad)
        pad.SetPosition(pt(x + dx, y))
        pad.SetNet(item)
    bridge = pcbnew.PCB_SHAPE(fp)
    bridge.SetShape(pcbnew.SHAPE_T_SEGMENT)
    bridge.SetLayer(pcbnew.F_Cu)
    bridge.SetWidth(mm(0.3))
    fp.Add(bridge)
    bridge.SetStart(pt(x - 0.65, y))
    bridge.SetEnd(pt(x + 0.65, y))
    return fp


def track(board: pcbnew.BOARD, item: pcbnew.NETINFO_ITEM, a: tuple, b: tuple,
          width: float) -> None:
    t = pcbnew.PCB_TRACK(board)
    t.SetLayer(pcbnew.F_Cu)
    t.SetWidth(mm(width))
    t.SetStart(pt(*a))
    t.SetEnd(pt(*b))
    t.SetNet(item)
    board.Add(t)


def main() -> None:
    out = sys.argv[1]
    libs = Path(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_FOOTPRINTS
    board = pcbnew.BOARD()
    outline(board)
    a, b, sig, other, n1 = (net(board, n) for n in ("A", "B", "SIG", "OTHER", "N1"))
    n2, n3 = net(board, "N2"), net(board, "N3")
    trace_jumper(board, "JP1", 8.0, 8.0, a, b)
    # R_0603 courtyard is 2.96 x 1.46 mm: 1.3 mm apart overlaps it, pads stay 0.35 mm apart
    place(board, libs, "R1", 20.0, 8.0, {"1": n1, "2": n2})
    place(board, libs, "R2", 20.0, 9.3, {"1": n1, "2": n3})
    r3 = place(board, libs, "R3", 30.0, 20.0, {"1": other})
    pad = next(p for p in r3.Pads() if p.GetNumber() == "1")
    top = pcbnew.ToMM(pad.GetBoundingBox().GetTop()) - Y0
    # 0.25 mm track whose edge passes 0.05 mm above R3.1 (default clearance 0.2 mm)
    track(board, sig, (26.0, top - 0.175), (34.0, top - 0.175), 0.25)
    pcbnew.SaveBoard(out, board)
    if not Path(out).is_file():
        raise SystemExit(f"could not save {out}")
    print(out)


if __name__ == "__main__":
    main()
