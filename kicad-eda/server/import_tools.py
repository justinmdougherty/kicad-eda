"""Design import tools (F4/F6; docs/ROADMAP_DECISIONS.md D4).

The MCP surface for rebuilding an existing board from its sources:

  * convert a KiCad project (schematic + board) to a netlist.json carrying
    ``source`` (kind, path, sha256, tool) and ``rebuild: "keep"|"reflow"``,
    plus an import report listing every board/schematic mismatch (the board
    wins for pad nets, footprints and placement; the schematic for symbols,
    values and fields);
  * bring foreign designs (Eagle, Altium, EasyEDA) in through KiCad's own
    importers first — KiCad 11 ``kicad-cli import`` / ``pcb import`` /
    ``sch import``, KiCad 9 SWIG ``LoadBoard`` for Altium/EasyEDA only (never
    Eagle via SWIG) — then through the KiCad converter;
  * check net equivalence as a partition comparison of ``(ref, pad)`` sets
    (server/tools/net_equivalence.py), never by net name;
  * extract a source board's footprints to ``libs/<name>_imported.pretty``
    when they are needed for the rebuild;
  * PDF/image schematics: two independent vision extractions compared with
    the equivalence checker, disagreements listed for review.

A tool's ``rc=0`` is not proof of success: stdout/stderr ``Error:`` lines
are scanned and ERC/parity run (D0).

Plain functions; kicad_server registers every function in TOOLS with FastMCP
(this module is listed in kicad_server.TOOL_MODULES). A new tool is added by
appending it to TOOLS — kicad_server.py is not edited.
"""

from __future__ import annotations

from typing import Any, Callable

TOOLS: tuple[Callable[..., Any], ...] = ()
