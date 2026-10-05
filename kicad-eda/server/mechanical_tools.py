"""Board outline and mechanical tools (F3; docs/ROADMAP_DECISIONS.md D2/D3).

The MCP surface for boards whose shape comes from a mechanical design:

  * read an outline DXF (Fusion 360, Y up) with ezdxf in the venv and
    normalise it to the board frame (mm, Y down, origin = outline bbox
    top-left), keeping the source origin as ``board.datum``;
  * validate an outline, its cutouts and the placement against it with
    server/tools/outline.py (stdlib-only geometry: arcs, containment with
    edge clearance, largest inscribed rectangle, fab rules);
  * the Fusion round trip out: STEP with ``--drill-origin`` (the datum is the
    aux/drill origin) and the outline DXF via ``kicad-cli pcb export dxf``;
    fixed parts come back in through per-component ``at`` anchors (D3).

Plain functions; kicad_server registers every function in TOOLS with FastMCP
(this module is listed in kicad_server.TOOL_MODULES). A new tool is added by
appending it to TOOLS — kicad_server.py is not edited.
"""

from __future__ import annotations

from typing import Any, Callable

TOOLS: tuple[Callable[..., Any], ...] = ()
