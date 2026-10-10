---
description: "Hands-off concept to Gerbers: decide every open question yourself (recorded), run the gated pipeline and the routing handoff, finish with a review report; never ask the user"
argument-hint: "<concept paragraph | path/to/netlist.json>"
---

Build this board hands-off, from napkin to a fab-ready zip: $ARGUMENTS

The input is one of:

- a concept paragraph: what the board does, its interfaces, power and size (anything left
  out is decided by the requirements defaults and recorded as an assumption);
- the path of an existing `netlist.json`: build it as it is, adding `"mode": "napkin"` and
  any missing assumptions;
- an existing open-source design to rebuild: not supported in this version (the napkin-mode
  skill, section (f), says how to end the run).

Load the `napkin-mode` skill (`${CLAUDE_PLUGIN_ROOT}/skills/napkin-mode/SKILL.md`) and follow
it to the end: requirements and their assumptions, parts through the `parts-selector`
subagent, the netlist, lint, the gated pipeline harness and its exit-code loop, the routing
handoff to the `router` subagent, and the review report.

Never ask the user anything, at any point: take each decision yourself, record it with
`record_decision` (or as a netlist assumption), and go on. The D5 hard stops still hold:
never install software, spend money or order, or overwrite a file open in a KiCad editor
(write a sibling and record it). Safety-relevant designs are built and flagged HUMAN REVIEW
REQUIRED. Finish with `review_report(netlist)` and a final summary: verdict, fab zip, review
path, low-confidence items and open items. The final message is a statement, not a question.

For the interactive, GUI-assisted flow use `/kicad-eda:new-board` instead.
