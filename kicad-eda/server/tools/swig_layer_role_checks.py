#!/usr/bin/env python3
"""G4 layer-role checks on a routed board (ADR D7.8 R8, R27, R28; task D7-18a).

usage: <KiCad python> swig_layer_role_checks.py <pcb> <netlist.json> [stackup.json]

Loads the board with pcbnew and extracts its copper as plain dicts (mm): tracks and arcs
(layer via board.GetLayerName(t.GetLayer())), vias (top / bottom layer, diameter), pads
(reference, number, net, centre, copper layers) and zones (swig_stackup.zone_layers: layers
via GetLayerSet(), never ZONE.GetLayerName()). The layer roles come from the stackup:
swig_stackup.load_or_resolve(design, stackup.json) - the artifact when it is fresh, else the
netlist resolved (the note says why); without a stackup.json argument $KICAD_EDA_STACKUP is
consulted. The lines come from layer_role_checks.line_layers (tline reference resolution
on that stackup). layer_role_checks.run_checks judges them.

Prints one JSON object as the last stdout line:
  {ok: true, issues (R8 blockers), warnings (R27 / R28), metrics {plane_layers,
   ref_transitions, dual_stripline}, stackup {source, artifact, note}, board_copper,
   stackup_copper, pcbnew}
A board whose enabled copper differs from the stackup's gets an R8 blocker (its roles
cannot be judged). Exit 0 when the checks ran (whatever they found), 1 with
{ok: false, error} when they could not (unreadable board or netlist, a stackup that does
not resolve, no pcbnew), 2 for a usage error. stdlib + pcbnew only (KiCad 9.0.4 ships
Python 3.11; the K10 image runs /usr/bin/python3).
"""
from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import kicad_quiet  # noqa: E402,F401  (before pcbnew: no modal wx dialogs)

try:
    import pcbnew  # noqa: E402
except ImportError:          # the dev venv: the CLI reports the missing pcbnew
    pcbnew = None

import board_outline as bo  # noqa: E402
import layer_role_checks as lrc  # noqa: E402
import swig_stackup as ss  # noqa: E402

USAGE = "usage: swig_layer_role_checks.py <pcb> <netlist.json> [stackup.json]"
EXIT_OK, EXIT_FAILED, EXIT_USAGE = 0, 1, 2
TRACK, ARC, VIA = "PCB_TRACK", "PCB_ARC", "PCB_VIA"


def _pcb() -> Any:
    if pcbnew is None:
        raise RuntimeError("pcbnew is not importable: run this script with KiCad's bundled "
                           "Python")
    return pcbnew


def _mm(value: int) -> float:
    return round(_pcb().ToMM(value), 6)


def _xy(point: Any) -> list[float]:
    return [_mm(point.x), _mm(point.y)]


def _copper(board: Any, layer_set: Any) -> list[str]:
    return [board.GetLayerName(lid) for lid in layer_set.CuStack()]


# ---------------------------------------------------------------- extraction
def _track(board: Any, t: Any, arc: bool) -> dict[str, Any]:
    item = {"net": t.GetNetname(), "layer": board.GetLayerName(t.GetLayer()),
            "start": _xy(t.GetStart()), "end": _xy(t.GetEnd()), "width": _mm(t.GetWidth())}
    return {**item, "mid": _xy(t.GetMid())} if arc else item


def _via_diameter(via: Any) -> float:
    """The via pad width: per layer first (KiCad 10's padstack API asserts on GetWidth()
    without one; a through via is uniform), then the 9.x call (swig_copper.via_w)."""
    try:
        return _mm(via.GetWidth(_pcb().F_Cu))
    except TypeError:
        return _mm(via.GetWidth())


def _via(board: Any, v: Any) -> dict[str, Any]:
    return {"net": v.GetNetname(), "at": _xy(v.GetPosition()),
            "layers": [board.GetLayerName(v.TopLayer()), board.GetLayerName(v.BottomLayer())],
            "diameter": _via_diameter(v)}


def _pads(board: Any) -> list[dict[str, Any]]:
    return [{"ref": fp.GetReference(), "pad": pad.GetNumber(), "net": pad.GetNetname(),
             "at": _xy(pad.GetPosition()), "layers": _copper(board, pad.GetLayerSet())}
            for fp in board.GetFootprints() for pad in fp.Pads()]


def extract(board: Any) -> dict[str, Any]:
    """The board dict of layer_role_checks: tracks, vias, pads, zones (plain JSON, mm)."""
    tracks, vias = [], []
    for t in board.GetTracks():
        kind = t.GetClass()
        if kind in (TRACK, ARC):
            tracks.append(_track(board, t, kind == ARC))
        elif kind == VIA:
            vias.append(_via(board, t))
    return {"tracks": tracks, "vias": vias, "pads": _pads(board),
            "zones": ss.zone_layers(board)}


# ---------------------------------------------------------------- the run
def _load_json(path: str) -> Mapping[str, Any]:
    with open(path, encoding="utf-8") as f:
        design = json.load(f)
    if not isinstance(design, Mapping):
        raise ValueError(f"{path} is not a netlist object")
    return design


def _copper_issue(board_copper: Sequence[str], stackup_copper: Sequence[str]) -> list[str]:
    if list(board_copper) == list(stackup_copper):
        return []
    return [f"R8: the board's copper ({', '.join(board_copper)}) is not the stackup's "
            f"({', '.join(stackup_copper)}): the layer roles cannot be judged on this board - "
            "rebuild it from the netlist (build_board_from_netlist) or fix the stackup"]


def run(pcb: str, netlist: str, artifact: str | None = None) -> dict[str, Any]:
    """The checks on one board (module docstring); raises on an unusable input."""
    design = _load_json(netlist)
    loaded = ss.load_or_resolve(design, artifact)
    rs = loaded.stackup
    board = bo.load_board(pcb)
    board_copper = _copper(board, board.GetEnabledLayers())
    result = lrc.run_checks(extract(board), lrc.roles_of(rs), lrc.line_layers(rs, design))
    return {"ok": True,
            "issues": _copper_issue(board_copper, rs.copper_names()) + result["issues"],
            "warnings": result["warnings"], "metrics": result["metrics"],
            "stackup": {"source": loaded.source, "artifact": loaded.artifact,
                        "note": loaded.note},
            "board_copper": board_copper, "stackup_copper": list(rs.copper_names()),
            "pcbnew": _pcb().Version()}


def main(argv: Sequence[str]) -> int:
    if len(argv) not in (2, 3):
        print(json.dumps({"ok": False, "error": USAGE}))
        return EXIT_USAGE
    pcb, netlist = argv[0], argv[1]
    artifact = argv[2] if len(argv) == 3 else None
    try:
        _pcb()
        result = run(os.fspath(pcb), os.fspath(netlist), artifact)
    except (OSError, ValueError, RuntimeError, KeyError) as exc:
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}))
        return EXIT_FAILED
    print(json.dumps(result, sort_keys=True))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
