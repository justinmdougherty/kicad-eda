#!/usr/bin/env python3
"""Load a foreign board with KiCad's own importer through pcbnew SWIG and save
it as a .kicad_pcb (ROADMAP_DECISIONS D4: the KiCad 9 board fallback when no
kicad-cli has `pcb import`). Runs under KiCad's bundled Python.

Only the importers verified headless on pcbnew 9.0.4 are allowed:
``pcbnew.LoadBoard(path, PCB_IO_MGR.ALTIUM_DESIGNER)`` (Altium .PcbDoc) and
``PCB_IO_MGR.EASYEDA`` (EasyEDA Std .json). The board is saved by the same
pcbnew, so it is in that release's format (KiCad 9: version 20241229, the D0
dialect the SWIG/Freerouting path reads).

Never Eagle: its importer needs a layer-mapping callback SWIG cannot register
and aborts the whole process ("Fatal Python error: Aborted"); EAGLE is refused
here before pcbnew loads anything. Never ``PCB_IO_MGR.Load`` + ``SaveBoard``
(segfaults, "no project in list"). EasyEDA Pro is not offered either:
``LoadBoard(path, EASYEDAPRO)`` returns None on 9.0.4.

The caller runs this with stdin=DEVNULL; kicad_quiet (imported before pcbnew)
turns wx asserts off (the Altium importer asserts "Unexpected Layer ID") and
sends wx log messages to stderr, so no modal dialog can block the child.

Usage: <kicad-python> swig_load_foreign.py <input> <output.kicad_pcb> <PLUGIN>
       PLUGIN is ALTIUM_DESIGNER or EASYEDA.
Output: the LAST stdout line is JSON
  {"ok": bool, "plugin", "input", "output", "pcbnew", "footprints", "tracks",
   "vias", "nets", "zones", "copper_layers", ["error": str]}
Exit codes: 0 saved, 1 load/save failed, 2 usage or refused plugin.
"""
import faulthandler
import json
import os
import sys

import kicad_quiet  # noqa: F401  (before pcbnew: no modal wx dialogs)
import pcbnew

ALLOWED_PLUGINS = ("ALTIUM_DESIGNER", "EASYEDA")
NEVER_PLUGINS = {"EAGLE": "the Eagle importer aborts the process under SWIG (no layer-mapping "
                          "callback); import Eagle with kicad-cli `pcb import` (KiCad 10+)"}
USAGE_EXIT, FAIL_EXIT = 2, 1


def emit(ok: bool, code: int, **fields) -> int:
    """Print the result line (always the last stdout line) and return `code`."""
    print(json.dumps({"ok": ok, **fields}), flush=True)
    return code


def plugin_type(name: str) -> int:
    """PCB_IO_MGR.<name>, for an allowed plugin only (ValueError otherwise)."""
    if name in NEVER_PLUGINS:
        raise ValueError(f"plugin {name} refused: {NEVER_PLUGINS[name]}")
    if name not in ALLOWED_PLUGINS:
        raise ValueError(f"plugin {name!r} is not one of {', '.join(ALLOWED_PLUGINS)}")
    return getattr(pcbnew.PCB_IO_MGR, name)


def board_counts(board) -> dict:
    """What the loaded board holds (track/arc segments and vias apart)."""
    tracks = list(board.GetTracks())
    vias = sum(1 for t in tracks if t.GetClass() == "PCB_VIA")
    return {"footprints": len(board.GetFootprints()), "tracks": len(tracks) - vias,
            "vias": vias, "nets": board.GetNetCount(), "zones": board.GetAreaCount(),
            "copper_layers": board.GetCopperLayerCount()}


def main(argv: list) -> int:
    faulthandler.enable()        # a native crash still leaves a traceback on stderr
    if len(argv) != 4:
        print(__doc__, file=sys.stderr)
        return emit(False, USAGE_EXIT, error="usage: swig_load_foreign.py <input> "
                                             "<output.kicad_pcb> <PLUGIN>")
    src, out, name = os.path.abspath(argv[1]), os.path.abspath(argv[2]), argv[3]
    base = {"plugin": name, "input": src, "output": out, "pcbnew": pcbnew.Version()}
    try:
        kind = plugin_type(name)
    except ValueError as e:
        return emit(False, USAGE_EXIT, **base, error=str(e))
    if not os.path.isfile(src):
        return emit(False, FAIL_EXIT, **base, error=f"input not found: {src}")
    if not out.endswith(".kicad_pcb"):
        return emit(False, USAGE_EXIT, **base, error=f"output must be a .kicad_pcb: {out}")
    try:
        board = pcbnew.LoadBoard(src, kind)
    except Exception as e:  # pcbnew raises IOError (and friends) for unreadable files
        return emit(False, FAIL_EXIT, **base,
                    error=f"LoadBoard({name}) raised {type(e).__name__}: {e}")
    if board is None:
        return emit(False, FAIL_EXIT, **base,
                    error=f"LoadBoard({name}) returned None: not a {name} board, or "
                          f"this pcbnew cannot import it")
    counts = board_counts(board)
    try:
        saved = pcbnew.SaveBoard(out, board)
    except Exception as e:
        return emit(False, FAIL_EXIT, **base, **counts,
                    error=f"SaveBoard raised {type(e).__name__}: {e}")
    if saved is False or not os.path.isfile(out):
        return emit(False, FAIL_EXIT, **base, **counts,
                    error=f"SaveBoard did not write {out}")
    return emit(True, 0, **base, **counts)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
