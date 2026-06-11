---
description: Start a new KiCad board design from a one-line concept and drive it through the 10-stage workflow.
---

Begin a new PCB design. The concept is: $ARGUMENTS

Load the `pcb-design` skill and start at Stage 1 (Concept & Requirements). Ask the scoping questions, then proceed stage by stage, honoring every gate. Use the `parts-selector` subagent for Stage 2, and the gate-reviewer subagents (`erc-reviewer`, `placement-reviewer`, `drc-reviewer`, `fab-checker`) at their stages. Do not skip a gate.
