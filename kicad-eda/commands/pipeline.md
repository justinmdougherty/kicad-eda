---
description: Run the headless gated pipeline (netlist.json -> Gerbers) on a board, with self-remediating IEEE 15288.2-style gates.
---

Drive a board through the full headless pipeline. Input: $ARGUMENTS
(either a path to an existing `netlist.json`, or a board concept to author
one for — load the `netlist-pipeline` skill for the schema and authoring
rules).

1. If the input is a concept, author `netlist.json` first per the
   `netlist-pipeline` skill (pad-level nets, `net_specs` design intent,
   preroutes for every high-current path, fixed placement for connectors).
2. Lint: `<venv-python> "${CLAUDE_PLUGIN_ROOT}/server/tools/lint_netlist.py" <netlist.json>`
   — fix every error before proceeding.
3. Run the gates:
   `<venv-python> "${CLAUDE_PLUGIN_ROOT}/pipeline/run_pipeline.py" <netlist.json> <w> <h>`
   (`<venv-python>` is the venv the server launcher built — on Windows
   `%CLAUDE_PLUGIN_DATA%/.venv/Scripts/python.exe`, else `.venv/bin/python`.
   Routing can take many minutes on dense boards; run it in the background.)
4. Read the gate report. PASS -> announce the fab zip path, render top/bottom
   with `kicad-cli pcb render`, and show the user. FAIL -> the report names
   every blocker and which remediations were auto-applied; fix the
   designer-level tasks in the netlist and re-run. Never hand-edit the
   generated .kicad_pcb to silence a gate.
5. Full reference: `${CLAUDE_PLUGIN_ROOT}/docs/PIPELINE.md` (schema, env
   vars, known traps). Example boards: `${CLAUDE_PLUGIN_ROOT}/examples/`.
