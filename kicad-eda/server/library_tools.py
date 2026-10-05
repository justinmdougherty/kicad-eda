"""Project-local library tools (F2; docs/ROADMAP_DECISIONS.md D1).

The MCP surface for parts that are not in KiCad's stock libraries:

  * author a footprint / symbol in the D0 dialect (footprint
    ``(version 20241229)``, symbol ``(version 20241209)``) into the project's
    ``<netlist dir>/libs/<name>_parts.pretty`` / ``<name>_parts.kicad_sym``
    (nickname ``<name>_parts``), one symbol pin per pad with extra GND pins
    stacked, and return ``pin_groups`` so a netlist can name 55 GND pads at
    once (first target: Quectel LG290P, LGA-79);
  * validate what was written by loading it (kicad-cli ``fp upgrade`` /
    ``sym upgrade`` into a temp dir as a parse check);
  * materialise a netlist's ``libraries`` block into project
    ``sym-lib-table`` / ``fp-lib-table`` rows tagged
    ``(descr "kicad-eda managed")`` plus a minimal ``<stem>.kicad_pro`` —
    table FILES only, never the AddLibraryTableEntry RPC, never the user's
    KiCad config;
  * write pin sidecars for project parts to ``<project>/pins/``.

Plain functions; kicad_server registers every function in TOOLS with FastMCP
(this module is listed in kicad_server.TOOL_MODULES). A new tool is added by
appending it to TOOLS — kicad_server.py is not edited.
"""

from __future__ import annotations

from typing import Any, Callable

TOOLS: tuple[Callable[..., Any], ...] = ()
