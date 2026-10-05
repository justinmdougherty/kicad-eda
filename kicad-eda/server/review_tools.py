"""Napkin-mode review tools (F5; docs/ROADMAP_DECISIONS.md D5).

The MCP surface that lets a hands-off run explain itself afterwards:

  * ``record_decision``: append one runtime decision to the append-only
    ``<stem>.decisions.jsonl`` beside the netlist (entry ``{id, ts, stage,
    question, decision, rationale, alternatives, confidence, source,
    reversible, override}``, stage one of requirements|parts|G1..G5|routing)
    — the same file the pipeline harness writes;
  * the review report for the conversation path: ``<stem>_review.md`` and
    ``<stem>_pipeline_report.json`` (assumptions, decisions by stage with low
    confidence first, part library sources, gate verdicts, remediations,
    accepted risks, routing method, board format + snapshot, renders, fab
    zip, open items), with **HUMAN REVIEW REQUIRED** at the top for
    safety-relevant designs (mains or > 50 V, Li-ion charging).

Plain functions; kicad_server registers every function in TOOLS with FastMCP
(this module is listed in kicad_server.TOOL_MODULES). A new tool is added by
appending it to TOOLS — kicad_server.py is not edited.
"""

from __future__ import annotations

from typing import Any, Callable

TOOLS: tuple[Callable[..., Any], ...] = ()
