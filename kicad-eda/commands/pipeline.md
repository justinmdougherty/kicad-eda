---
description: Run the headless gated pipeline (netlist.json -> Gerbers) on a board, with self-remediating IEEE 15288.2-style gates, napkin policies by default (--strict turns them off) and a routing handoff to the router subagent.
---

Drive a board through the full headless pipeline. Input: $ARGUMENTS
(either a path to an existing `netlist.json`, or a board concept to author
one for — load the `netlist-pipeline` skill for the schema and authoring
rules; harness options such as `--strict` or `--route freerouting` in the
input are passed on to the run).

**Napkin behaviour is the default** (ROADMAP D5): the harness applies the
napkin policies itself (each remedy is a decision in `<stem>.decisions.jsonl`),
and you clear the designer tasks it leaves by the policy table of the
napkin-mode skill (`${CLAUDE_PLUGIN_ROOT}/skills/napkin-mode/SKILL.md`, section
(e)), recording one decision per fix, without asking. `--strict` turns the
policies off: every policy task stays a designer task, and you report the
tasks that depend on the user's design intent instead of deciding them. A
concept whose requirements and parts are still open is a hands-off build
from the start: `/kicad-eda:napkin`.

1. If the input is a concept, author `netlist.json` first per the
   `netlist-pipeline` skill (pad-level nets, `net_specs` design intent,
   preroutes for every high-current path, fixed placement for connectors).
2. Lint: `<venv-python> "${CLAUDE_PLUGIN_ROOT}/server/tools/lint_netlist.py" <netlist.json>`
   — fix every error before proceeding.
3. Run the gates:
   `<venv-python> "${CLAUDE_PLUGIN_ROOT}/pipeline/run_pipeline.py" <netlist.json> [<w> <h>] [--strict] [--route auto|freerouting|agent]`
   (`<venv-python>` is the venv the server launcher built — on Windows
   `%CLAUDE_PLUGIN_DATA%/.venv/Scripts/python.exe`, else `.venv/bin/python`.
   Board dims are optional: the netlist `board` block, else the board is
   auto-sized to its parts. `--route auto` (the default) routes with
   Freerouting first and hands the board to the agent when G4 fails;
   `freerouting` makes a G4 failure final; `agent` hands the placed board off
   without trying Freerouting; `--resume routed|stalled` continues a run that
   handed routing off (step 4). Routing can take many minutes on dense boards;
   run it in the background. Without a shell, the `netlist-pipeline` skill
   lists the MCP tools that run the same steps one at a time, minus the
   auto-remediation loop.)
4. Act on the exit code (every exit writes `<stem>_pipeline_report.json` and
   `<stem>_review.md` beside the netlist; `<stem>` = its file name without
   `.json`):
   - `0` PASS: announce the fab zip (`fab.zip` in the report,
     `<stem>_<FAB_PROFILE>.zip` beside the board) and show the renders the
     harness wrote (`<stem>_final_top.png` / `<stem>_final_bottom.png`).
   - `1` FAIL: read the report (`gates[*].blockers`, `tasks`, `remediation`,
     `error`) and the open items of the review; fix each designer task in
     the netlist by the policy table, lint, record it with the failing gate
     as the stage, e.g.
     `record_decision(netlist, stage="G3", question="...", decision="...", rationale="...", confidence="med", source="napkin-mode policy table")`,
     and re-run. Stop after 5 runs and report what is left. An `error` from
     a resumed run means a fresh run without `--resume`. Never hand-edit the
     generated .kicad_pcb to silence a gate.
   - `2` usage error: the command line or the netlist cannot start a run
     (stderr says why): fix it and run again.
   - `3` routing handoff: read `<stem>_route_handoff.json` and start the
     `router` subagent with its `pcb`, the netlist and its `instructions`
     verbatim. When its `why` names congestion, or G4 failed twice, the
     `placement-reviewer-headless` subagent reviews the placement first: its
     BLOCKER edits go into the netlist (one `G3` decision each) and a fresh
     run replaces the routing.
     `route_status` passed: `close_board(pcb, save=True)`, then run the
     handoff's `resume.routed` line (`--resume routed`). `route_check`
     reported `stalled`: `close_board(pcb, save=True)`, then `resume.stalled`
     (`--resume stalled`: Freerouting from the pre-KiCad-11 snapshot, then a
     higher pack_scale). A resumed run never hands off again.
5. Finish with `review_report(netlist)` (it re-renders `<stem>_review.md`
   with the decisions recorded after the last run) and report the verdict,
   the fab zip, the review path and the low-confidence decisions.
6. Full reference: `${CLAUDE_PLUGIN_ROOT}/docs/PIPELINE.md` (schema, env
   vars, known traps). Example boards: `${CLAUDE_PLUGIN_ROOT}/examples/`.
