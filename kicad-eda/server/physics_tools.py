"""Stackup, impedance and RF physics tools (D7; docs/ROADMAP_DECISIONS.md D7).

The MCP surface for a design's physical layer, computed from the stackup the
netlist declares (any copper layer count, per-layer copper, per-dielectric
material / thickness / Dk / Df; fab presets only expand into that model).
Registered empty by X-04; **D7-13a** fills TOOLS (then D7-13b, D7-08b):

  * ``resolve_stackup``: resolve the declared stackup, the per-net
    transmission lines and the DRU/DSN rules into ``<stem>.stackup.json``
    (server/tools/physics_resolve.py);
  * ``impedance_calc``: microstrip, embedded microstrip, stripline, CPWG and
    coplanar, single-ended and differential, reference planes taken from the
    stackup;
  * ``list_materials`` / ``list_stackup_presets``: the laminate and fab
    preset data the stackup model accepts;
  * ``check_layer_count``: the layer count as a recorded design decision
    (D7-08b adds its score);
  * ``rf_plan`` / ``coexistence_check`` (D7-13b): RF chains, antennas and
    radio coexistence.

Plain functions; kicad_server registers every function in TOOLS with FastMCP
(this module is listed in kicad_server.TOOL_MODULES). A new tool is added by
appending it to TOOLS — kicad_server.py is not edited.
"""

from __future__ import annotations

from typing import Any, Callable

TOOLS: tuple[Callable[..., Any], ...] = ()
