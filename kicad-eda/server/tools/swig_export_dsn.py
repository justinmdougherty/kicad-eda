#!/usr/bin/env python3
"""Export a .kicad_pcb to Specctra .dsn (Freerouting input). KiCad bundled python."""
import sys
import pcbnew

board = pcbnew.LoadBoard(sys.argv[1])
dsn = sys.argv[2]
ok = None
for attempt in (lambda: pcbnew.ExportSpecctraDSN(board, dsn),
                lambda: pcbnew.ExportSpecctraDSN(dsn)):
    try:
        ok = attempt()
        break
    except TypeError:
        continue
print("dsn_export", dsn, ok)
