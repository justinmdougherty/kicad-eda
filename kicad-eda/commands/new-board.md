---
description: Start a new KiCad board design from a one-line concept and drive it through the 10-stage workflow.
---

Begin a new PCB design. The concept is: $ARGUMENTS

Load the `pcb-design` skill and start at Stage 1 (Concept & Requirements). Ask the scoping questions, then proceed stage by stage, honoring every gate. Use the `parts-selector` subagent for Stage 2, and the gate-reviewer subagents (`erc-reviewer`, `placement-reviewer`, `drc-reviewer`, `fab-checker`) at their stages. Do not skip a gate. Persist the schematic with `save_schematic` (never by asking for Ctrl+S), and never write a file that is open in a KiCad editor — check `test_kicad_connection().open_documents` and ask the user to close it first.

This is the interactive flow: the user is consulted at each stage. For a hands-off build (no questions, every decision recorded) use `/kicad-eda:napkin` instead.
