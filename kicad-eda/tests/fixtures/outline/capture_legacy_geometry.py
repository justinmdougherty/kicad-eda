#!/usr/bin/env python3
"""Capture a board's legacy-builder geometry as JSON (F3-11 live test 1).

Runs under KiCad's bundled Python (9.0.4 locally, /usr/bin/python3 in the
kicad-eda:wave1 image). It builds a netlist (default examples/lm358_buffer)
with server/tools/swig_build_board.py as a child of THIS interpreter (stdin
closed, UTF-8, a timeout), loads the saved board and dumps:

  footprints  sorted [ref, x_nm, y_nm, orientation_deg, side]
  edge_cuts   sorted [shape, [x_nm, y_nm], [x_nm, y_nm]] of every board Edge.Cuts drawing
  zones       sorted {layers, net, rule_area, outlines: [[outer pts], [hole pts], ...]}

plus the pcbnew build version and its major. The goldens
tests/fixtures/outline/legacy_lm358_geometry_k9.json / _k10.json were captured
with the builder as it was BEFORE F3-11 edited it; live test 1 runs this script
with the current builder and compares the result with the golden of the
running pcbnew major.

Usage:
  <kicad-python> capture_legacy_geometry.py <out.json | -> [--netlist N]
                 [--footprints DIR] [--builder swig_build_board.py] [--work DIR]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
TOOLS = REPO / "server" / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import kicad_quiet  # noqa: E402,F401  (before pcbnew: no modal wx dialogs)
import pcbnew  # noqa: E402

DEFAULT_NETLIST = REPO / "examples" / "lm358_buffer" / "netlist.json"
BUILD_TIMEOUT_S = 600


def default_footprints() -> Path:
    """The stock footprints of the SAME install as this interpreter."""
    root = Path(sys.executable).resolve().parent.parent
    for share in (root / "share" / "kicad" / "footprints", Path("/usr/share/kicad/footprints")):
        if share.is_dir():
            return share
    raise SystemExit(f"no stock footprints beside {sys.executable}; pass --footprints")


def build(builder: Path, netlist: Path, footprints: Path, pcb: Path) -> dict:
    """Run the builder on `netlist` with argv identical to the legacy pipeline
    call (no board size, no placement), stdin closed; returns its result JSON."""
    env = {k: v for k, v in os.environ.items() if k not in ("KICAD_EDA_OUTLINE", "PACK_SCALE")}
    proc = subprocess.run([sys.executable, str(builder), str(netlist), str(pcb), str(footprints)],
                          capture_output=True, text=True, encoding="utf-8", errors="replace",
                          stdin=subprocess.DEVNULL, timeout=BUILD_TIMEOUT_S, env=env)
    last = next((ln for ln in reversed(proc.stdout.splitlines()) if ln.startswith("{")), "{}")
    result = json.loads(last)
    if proc.returncode != 0 or not result.get("ok") or not pcb.is_file():
        raise SystemExit(f"builder failed (rc {proc.returncode}):\n{proc.stdout}\n{proc.stderr}")
    return result


def _xy(v: object) -> list[int]:
    return [int(v.x), int(v.y)]


def _chain(chain: object) -> list[list[int]]:
    return [_xy(chain.CPoint(j)) for j in range(chain.PointCount())]


def zone_record(board: object, zone: object) -> dict:
    """A zone's layers, net, kind and outline (outer + holes per polygon, nm)."""
    ps = zone.Outline()
    outlines = [[_chain(ps.COutline(i)), *(_chain(ps.CHole(i, h)) for h in range(ps.HoleCount(i)))]
                for i in range(ps.OutlineCount())]
    return {"layers": sorted(board.GetLayerName(lid) for lid in zone.GetLayerSet().Seq()),
            "net": zone.GetNetname(), "rule_area": bool(zone.GetIsRuleArea()),
            "outlines": outlines}


def geometry(pcb: Path) -> dict:
    """The footprint placement, Edge.Cuts drawings and zone outlines of `pcb`."""
    if not pcb.is_file():
        raise SystemExit(f"board file not found: {pcb}")
    board = pcbnew.LoadBoard(str(pcb))
    if board is None:
        raise SystemExit(f"pcbnew.LoadBoard returned None for {pcb}")
    fps = sorted([fp.GetReference(), *_xy(fp.GetPosition()),
                  round(float(fp.GetOrientationDegrees()), 6) % 360.0,
                  "B" if fp.IsFlipped() else "F"] for fp in board.GetFootprints())
    edges = sorted([str(d.ShowShape()), _xy(d.GetStart()), _xy(d.GetEnd())]
                   for d in board.GetDrawings()
                   if isinstance(d, pcbnew.PCB_SHAPE) and d.GetLayer() == pcbnew.Edge_Cuts)
    zones = sorted((zone_record(board, z) for z in board.Zones()),
                   key=lambda r: json.dumps(r, sort_keys=True))
    version = str(pcbnew.GetBuildVersion())
    return {"pcbnew": version, "major": int(version.lstrip("(").split(".")[0]),
            "footprints": fps, "edge_cuts": edges, "zones": zones}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("out", help="output JSON path, or - for stdout")
    ap.add_argument("--netlist", default=str(DEFAULT_NETLIST))
    ap.add_argument("--footprints", default=None)
    ap.add_argument("--builder", default=str(TOOLS / "swig_build_board.py"))
    ap.add_argument("--work", default=None, help="directory for the built board (default: temp)")
    args = ap.parse_args(argv)
    footprints = Path(args.footprints) if args.footprints else default_footprints()
    work = Path(args.work) if args.work else Path(tempfile.mkdtemp(prefix="legacy_geom_"))
    work.mkdir(parents=True, exist_ok=True)
    pcb = work / (Path(args.netlist).parent.name + ".kicad_pcb")
    result = build(Path(args.builder), Path(args.netlist), footprints, pcb)
    data = {**geometry(pcb), "netlist": Path(args.netlist).parent.name,
            "board_mm": result.get("board_mm")}
    text = json.dumps(data, indent=1, sort_keys=True) + "\n"
    if args.out == "-":
        sys.stdout.write(text)
    else:
        Path(args.out).write_text(text, encoding="utf-8", newline="\n")
        print(json.dumps({"ok": True, "out": args.out, "pcbnew": data["pcbnew"],
                          "footprints": len(data["footprints"]),
                          "edge_cuts": len(data["edge_cuts"]), "zones": len(data["zones"])}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
