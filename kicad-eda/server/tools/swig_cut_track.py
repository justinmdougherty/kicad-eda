#!/usr/bin/env python3
"""Test fixture helper: cut the longest signal track on a routed board to
manufacture a same-net disconnect, so the regression suite can prove the G4
healer repairs it. Never used in production flows.

Usage: kicad-python swig_cut_track.py <board.kicad_pcb>
Prints {"cut_net": ..., "length_mm": ...}.
"""
import json
import sys

import kicad_quiet  # noqa: F401  (before pcbnew: no modal wx dialogs)
import pcbnew


def main():
    pcb = sys.argv[1]
    board = pcbnew.LoadBoard(pcb)
    to = pcbnew.ToMM
    best = None
    for t in board.GetTracks():
        if t.GetClass() != "PCB_TRACK" or t.GetNetname() in ("GND", ""):
            continue
        if best is None or t.GetLength() > best.GetLength():
            best = t
    if best is None:
        print(json.dumps({"cut_net": None}))
        return
    net, ln = best.GetNetname(), to(best.GetLength())
    board.Remove(best)
    board.BuildConnectivity()
    pcbnew.SaveBoard(pcb, board)
    print(json.dumps({"cut_net": net, "length_mm": round(ln, 2)}))


if __name__ == "__main__":
    main()
