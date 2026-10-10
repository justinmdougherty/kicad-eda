---
name: napkin-mode
description: >-
  Hands-off ("napkin") concept-to-Gerbers: take a one-paragraph concept or a netlist.json and
  build the board with no questions at all, deciding every open point yourself and recording
  it (netlist assumptions, the decision log), then running the gated pipeline harness, the
  routing handoff and the review report. Use when the user says hands-off, no questions,
  unattended, napkin sketch, "just build it", "decide for me", or runs /kicad-eda:napkin.
  Not for the interactive GUI flow where the user wants to be consulted at each stage or work
  in the KiCad GUI (use pcb-design), and not for a supervised headless run where the user
  makes the calls (use netlist-pipeline).
---

# Napkin mode: decide, record, build

You turn a concept into a fab-ready zip without a single question. Every judgement a human
would have been asked about is taken by you and written down where a reviewer finds it: the
netlist `assumptions` block and `<stem>.decisions.jsonl`, both rendered into `<stem>_review.md`.
Read `${CLAUDE_PLUGIN_ROOT}/skills/netlist-pipeline/SKILL.md` for netlist authoring and
`${CLAUDE_PLUGIN_ROOT}/docs/PIPELINE.md` for the schema; this skill is the hands-off policy.

## (a) Contract: zero questions

- Never ask. No `AskUserQuestion`, no "shall I", no confirmation, not even at the end. An open
  question becomes a decision you take, with a rationale and a confidence.
- **Requirements-level judgements** (what to build) go into the netlist's top-level
  `assumptions` list: `{"id", "topic", "value", "rationale", "confidence", "override"}`.
  `decisions.validate_assumptions(block)` must return `[]` (G1 blocks otherwise): unique,
  non-blank string `id`s; `topic` and `rationale` non-blank strings; `value` any JSON value
  but null; `confidence` `high` | `med` | `low`; `override` = the netlist path a human edits
  to overrule it (`"net_specs.VBUS.voltage"`), or null.
- **Runtime judgements** (how you fixed or chose something) go to the decision log with
  `record_decision(netlist, stage, question, decision, rationale, confidence, source, alternatives, reversible, override)`.
  Stages (D5): `requirements`, `parts`, `G1`..`G5`, `routing`. `question` = what a human
  would have been asked; `source` = the datasheet URL, `"D5 default"` or the policy-table
  row; `alternatives` = the options you did not take; `override` = the netlist path to change.
- The harness records its own remedies and routing choices; you record yours: the layer
  count (`requirements`), every part (the `parts-selector` subagent does), every netlist edit
  you make to clear a gate (its gate), every routing step you take (`routing`).
- Confidence: `high` = read from a datasheet or a standard; `med` = an engineering default;
  `low` = a guess (the review lists low confidence first). Never inflate it.
- The final message is a summary (section (d), step 10), never a question.

## (b) Hard stops (D5)

These stay stops in napkin mode. Stop the affected step, record why, and carry on with what
does not need it; when nothing can continue, finish with the review and say what is needed.

- **Installing software**: never run an installer, `pip`, `winget`, `apt`, `brew` or a
  download. When `server_status()` or a tool says KiCad, Java / Freerouting or the KiCad 11
  nightly is missing, name what to install (and `/kicad-eda:setup`, which the user runs).
- **Spending money or ordering**: never place an order, upload to a fab, buy parts or open a
  quote. The fab zip on disk is the end of the run; quantity and ordering are human steps.
- **Overwriting a file open in a KiCad editor**: never write over it and never close the
  user's editor. The harness writes `<stem>.napkin.kicad_sch` / `<stem>.napkin.kicad_pcb`
  siblings and records a G1 decision; do the same for any file you write (a sibling,
  recorded). Never edit the user's KiCad configuration (global library tables included).
- **Safety-relevant designs** (mains or > 50 V, Li-ion charging) are built anyway: the
  review opens with **HUMAN REVIEW REQUIRED** and G5 carries the warning. Record the safety
  assumptions with confidence `low` and put the banner first in your final message.

## (c) Requirements defaults

Use a default only when the concept is silent, and record each one you use as an assumption
with this confidence and override. Anything the concept states wins (confidence `high`).

| Topic | Default when the concept says nothing | Confidence | Override |
|---|---|---|---|
| power input | USB-C 5 V sink: 5.1 kΩ pull-downs on CC1 / CC2, no PD, VBUS net spec 5 V | med | `net_specs.VBUS.voltage` |
| operating temperature | 0 to 70 °C ambient (commercial); G2's thermal review at the hot end: `"thermal": {"t_ambient": 70}` | med | `thermal.t_ambient` |
| quantity | 5 prototypes (no netlist key: ordering is a human step) | med | none |
| fab | JLCPCB (the plugin's FAB_PROFILE), 1.6 mm FR-4, 1 oz outer copper | med | `stackup.preset` |
| layer count | a design decision with its rationale (D7): 4 is the usual default, 2 when cheap and simple fits, 6+ for density or RF | med | `copper_layers` |
| regulatory | not assessed (no FCC / CE / UL review); listed as a human item | low | none |
| board size / form factor | auto-sized to the parts unless the concept gives a size or shape (D2 `board` block) | med | `board` |
| sourcing | `"require_sourcing": false`: a part without LCSC / MPN is a G5 warning, not a blocker | med | `require_sourcing` |
| IPC class | class 2 | med | `class` |

**Layer count and stackup (D7).** Never leave the count implicit: declare `copper_layers` and
a stackup with its rationale, and record both.

1. `list_stackup_presets(fab="jlcpcb", copper_layers=4)` lists the builds (2 layers:
   `generic:2L-1.6`; 4: `jlcpcb:JLC04161H-7628`, or `jlcpcb:JLC04161H-3313` when the design has
   impedance or RF nets; 6: `jlcpcb:JLC06161H-2116`). Count: 4 for a solid GND under every
   signal and easy routing; 2 when the board is simple and slow and cost matters most; 6 when
   density, stripline or RF demand it.
2. Write `"copper_layers": 4` and `"stackup": {"preset": "jlcpcb:JLC04161H-7628", "rationale":
   "..."}` plus an assumption with topic `copper_layers`.
3. `check_layer_count(netlist)`: `floors` above your count (impedance, stripline, fine-pitch
   BGA) mean raise it; fix `issues` before the harness runs.
4. `record_decision(netlist, stage="requirements", question="How many copper layers, and which stackup?", decision="4L jlcpcb:JLC04161H-7628", rationale="...", confidence="med", source="D5 default", alternatives=["2L generic:2L-1.6"], override="stackup.preset")`.
5. Impedance nets (`net_specs` `impedance` / `diff`): `impedance_calc(netlist=netlist, net="USB_DP")`
   reports the width the resolver will use on your stackup; `resolve_stackup(netlist, write=False)`
   previews every line and rule without writing. `list_materials(category="rf-laminate")` only
   when a laminate other than FR-4 is needed.

Example block (the first entries of a USB-C node):

```json
"mode": "napkin",
"assumptions": [
  {"id": "A1", "topic": "power input", "value": "USB-C 5 V sink, 5.1 kOhm CC pull-downs",
   "rationale": "concept says USB-C powered, no PD needed", "confidence": "high",
   "override": "net_specs.VBUS.voltage"},
  {"id": "A2", "topic": "operating temperature", "value": "0 to 70 C ambient",
   "rationale": "D5 default, indoor product", "confidence": "med", "override": "thermal.t_ambient"},
  {"id": "A3", "topic": "copper_layers", "value": 4,
   "rationale": "GND plane under the module and the I2C bus, easy routing", "confidence": "med",
   "override": "copper_layers"}
]
```

## (d) Flow

1. **Workspace.** A netlist path as the input: work beside it. A concept: create
   `<cwd>/<board-slug>/` and its `netlist.json` (`name`, `title`, `"mode": "napkin"`,
   `assumptions`) first, so every tool and the decision log share one stem from the start.
2. **Toolchain.** `server_status()`: a KiCad with a pcbnew Python is required (else hard stop);
   Java + Freerouting route in the harness; `board_api` true means the `router` subagent can
   route a handoff. With `board_api` false, run the harness with `--route freerouting` (no
   handoff is possible) and record that `routing` decision.
3. **Requirements.** Fill the assumptions block from the concept and section (c); declare the
   layer count and stackup (section (c) steps 1-5).
4. **Parts.** Start the `parts-selector` subagent (`kicad-eda:parts-selector`) with the
   requirements, the netlist path and the words "napkin mode: unattended, never ask". It picks
   stock parts first, generates missing ones into `<netlist dir>/libs/` with the library tools
   (`author_footprint`, `author_symbol`, checked with `validate_library` and
   `resolve_libraries`; `compare_footprints(a, b, netlist=netlist)` when a stock land pattern
   and the datasheet's disagree) and records one `parts` decision per part. Never ask the user
   to draw a symbol or footprint (D1). Library guide: `${CLAUDE_PLUGIN_ROOT}/docs/LIBRARIES.md`.
   - Stacked pins: put every pad of a returned `pin_groups` entry on its net (all of
     `pin_groups["GND"]` on GND; hidden stacked pins float otherwise).
   - LGA modules (`author_footprint(netlist, preset="lg290p")`, also `lc29h`): fix the module's
     placement with `at` first (front side), then apply the returned `netlist_hints` (the inner-pad GND
     escape vias as `preroutes`) with the plugin's `lga_strategy.apply_hints(design, hints, ref="U3")`
     (stdlib module in `${CLAUDE_PLUGIN_ROOT}/server/tools`, run with `<venv-python>`; it
     returns the new netlist to write back). Record a `parts` decision naming the strategy.
5. **Netlist.** Author `netlist.json` per the netlist-pipeline skill: pad-level nets,
   `net_specs` (voltage / continuous current / impedance), preroutes for every high-current
   path, connectors with an `edge` (or fixed `x` / `y` / `rot`), modules with antennas as
   `edge_row` + `overhang` and an `antenna_keepout`, `mounting_holes`, a `board` block only
   when the concept gives a size, `no_connect` for unused outputs, pulls / rails for inputs.
6. **Lint.** `<venv-python> "${CLAUDE_PLUGIN_ROOT}/server/tools/lint_netlist.py" <netlist.json>`
   until it reports 0 errors (policy table, G1 rows). `<venv-python>` is the venv the MCP server
   runs in: `<plugin data dir>/.venv/Scripts/python.exe` on Windows (`.venv/bin/python`
   elsewhere), else the `.venv` in the plugin root when there is no plugin data dir.
7. **Harness.** `<venv-python> "${CLAUDE_PLUGIN_ROOT}/pipeline/run_pipeline.py" <netlist.json>`
   (no board dims: the `board` block or auto-size), in the background (Bash `run_in_background`);
   routing can take tens of minutes. Wait for its exit, then act on the exit code. At most 5 harness runs in total
   (fresh and resumed); after the fifth, stop and finish with the review.

   | Exit | Meaning | Next |
   |---|---|---|
   | `0` | PASS: every gate passed, fab package written | step 10 |
   | `1` | FAIL: a gate failed, or a resumed run could not continue (report `error`) | step 8 |
   | `2` | usage error: the command line or the netlist cannot start a run (stderr says why) | fix the netlist / command line, run again |
   | `3` | routing handoff: Freerouting did not finish the board (`--route auto`, the default) or `--route agent` was given | step 9 |

8. **Fix loop (exit 1).** Read `<stem>_pipeline_report.json` (`gates[*].blockers`, `tasks`,
   `remediation`, `error`) and the open items of `<stem>_review.md`. Apply the policy table
   (e) to every designer task, edit only the netlist (never the generated `.kicad_sch` /
   `.kicad_pcb`), lint, record one decision per fix with its gate as the stage, run again.
   An `error` from a resumed run (the netlist changed since the handoff, a G2 remediation,
   a missing handoff) means a fresh run without `--resume`.
9. **Routing handoff (exit 3).** Read `<stem>_route_handoff.json`: `pcb`, `why`,
   `unconnected_items`, `net_classes`, `instructions`, `resume`.
   - Placement first when it is the likely cause (the `why` names congestion, or G4 failed
     twice): start the `placement-reviewer-headless` subagent with `pcb` and the netlist; a
     FAIL with BLOCKERs means apply its netlist edits, record each as a `G3` decision and run
     a fresh harness instead of routing.
   - Otherwise start the `router` subagent (`kicad-eda:router`) with `pcb`, the netlist path and
     the handoff's `instructions` verbatim.
   - `route_status` passed: check `list_open_boards()` and `close_board(pcb, save=True)` any
     board still open, then run `resume.routed` (the handoff's `--resume routed` line).
   - `route_check` reported `stalled`: `close_board(pcb, save=True)`, then run
     `resume.stalled` (`--resume stalled`: Freerouting from the pre-KiCad-11 snapshot, then a
     higher pack_scale).
   - Record a `routing` decision for each step. A resumed run never hands off again: exit 1
     after `--resume routed` means the router fixes the board and you resume again (at most
     twice); a stalled chain that ends in exit 1 goes back to step 8 with placement edits.
10. **Finish.** `review_report(netlist)` re-renders `<stem>_review.md` with every decision you
    recorded after the last run. Final message: the verdict; the fab zip (`fab.zip` in the
    report, `<stem>_<FAB_PROFILE>.zip` beside the board); the review path; every
    low-confidence assumption and decision; open items; and, when `safety_flags` is not empty,
    **HUMAN REVIEW REQUIRED** as its first line. A statement, never a question.

## (e) Policy table for gate tasks

Auto tasks (a fix type) are applied by the harness itself in napkin mode, one decision each;
you only check them in the review. Designer tasks are yours: do the edit in the row, lint,
record a decision with the gate as its stage. Task texts are shown as gates.py writes them.

| Gate | Task (fix type / policy, or the task text) | Napkin action |
|---|---|---|
| G1 | lint `E1`: a pad on two nets | keep the net the datasheet pin function needs; remove the pad from the other |
| G1 | lint `E2`: a pad both netted and `no_connect` | drop it from `no_connect` (the net wins) unless the net was the mistake |
| G1 | lint `E3`: a net with fewer than 2 pads | connect the missing end (datasheet); an unused output or NC pin goes to `no_connect`; an input gets its pull / rail |
| G1 | lint `E4`: a net or `no_connect` naming an unknown ref | add the missing part or fix the reference |
| G1 | lint `E5`: duplicate component ref | renumber the later part and its pads in `nets` |
| G1 | "netlist has no nets" | author the nets |
| G1 | any other lint code (libraries, outline, stackup, pin mux) | do what its message says; library codes: `resolve_libraries(netlist)`, then fix the `libraries` block or author the part |
| G1 | malformed `assumptions` entry | fix it per section (a) |
| G1 | "the netlist board block cannot be resolved" | fix the outline / DXF path, or drop the block to auto-size |
| G1 | "hard stop (ROADMAP D5)": the files and their `.napkin` siblings are all open in KiCad | a D5 hard stop: end the run; the final message names the files to close |
| G2 | `add_decoupling` (auto): "add 100nF bypass" | harness adds the cap and records it; check value and voltage rating in the review |
| G2 | `declare_no_connect` / `net_same_name_pin` (auto, `D5.floating_pin`) | harness no-connects an unused IC output / I/O / passive / NC pin, or joins a stacked supply pin to its same-name net; review the low-confidence ones |
| G2 | "net pin {pin} or declare it no_connect in the netlist" | an input / power_in pin (or a non-IC part): net it, never no-connect it: power_in to its rail; enable / reset / boot inputs to a pull-up (10 kΩ) or their driver; unused logic inputs per the datasheet |
| G2 | "resolve thermal violation: {b}" | bigger package or its `_ThermalVias` variant; an LDO that burns too much becomes a buck; a declared current above the real continuous draw is derated, with the rationale |
| G2 | "resolve {erc} ERC violation(s)" | read the ERC items in the report; an undriven power_in rail fed by a real source gets `power_flags`; conflicting pins are re-netted |
| G2 | "fix schematic generation error" | `missing_pins` names each (ref, pad) the symbol lacks: use the symbol's own pin numbers (the sidecar `build_pin_sidecars(symbols="Lib:Symbol", netlist=netlist)` writes to `<netlist dir>/pins/` lists them) or author the symbol |
| G2 | "make the symbol readable so the design review covers it: {e}" | fix the `lib_id`, author the symbol (`author_symbol`) or fix the `libraries` block (`resolve_libraries`) |
| G2 | warnings (pull resistor, test point, DFA arrays) | not blockers: add pull-ups on open-drain buses (I2C) and record; the rest are accepted |
| G3 | `loosen_placement` (auto: courtyard overlap; `D5.fractured_plane` for a fractured plane) | harness re-places at the next pack_scale; when the ladder runs out, enlarge or drop the `board` block or move fixed parts apart |
| G3 | `face_connector_out` (auto, `D5.face_connector_out`) | harness turns / moves the connector to face out at its nearest edge; an OPEN item left over: set its `edge` (or `x` / `y` / `rot`), or `"interior": true` for a header that is not an edge connector |
| G3 | `swap_thermal_footprint` (auto, `D5.thermal_vias`) | harness swaps to the `...ThermalVias` footprint when one exists; else pick a package that has one, or accept the EP via farm the builder drops |
| G3 | "correct netlist pad reference {m}" | re-map from the footprint's pad list when unambiguous (EP "25" vs "EP"); else choose a footprint that matches the symbol |
| G3 | "fix board generation error: {why}" | a missing footprint: fix the id or `author_footprint`; an outline problem: fix the `board` block |
| G3 | "fix placement: {issue}" | height zone: move the part to side B or out of the zone, or use a lower part; antenna keepout: declare `antenna_keepout` and the module's `edge_row` + `overhang`; outside the outline / in a cutout: move it or grow the board; anything else: fixed `x` / `y` / `rot` (the placement review says where) |
| G4 | `reroute` (auto) | harness re-places at a looser pack_scale; in `--route auto` a board Freerouting cannot finish is handed off (exit 3) |
| G4 | "fix autoroute failure: {why}" / "clear DRC violations: {types}" / "fix net-class width violation: {w}" / unrouted nets | handled by the routing modes: exit 3, the `router` subagent, `--resume routed` or `--resume stalled`; after a resumed run failed: placement review, then placement edits (more room, parts apart, preroutes for wide nets) and a fresh run |
| G4 | "routing handoff refused: {reason}" | impedance-controlled / RF boards cannot be handed off in this version: placement review, more room or preroutes, re-run with `--route freerouting`; record it |
| G5 | sourcing warning: parts with neither an LCSC number nor an MPN | accepted (`require_sourcing` false); record one `G5` decision listing them |
| G5 | "HUMAN REVIEW REQUIRED" warning | accepted (D5): the design is built; the banner leads your final message |
| G5 | export failed / too few files / BOM.csv or CPL.csv missing | a toolchain problem: `server_status()`, run once more, else stop with the review |

## (f) Existing designs

Rebuilding an existing open-source design (goal 2: a KiCad, Eagle, Altium or EasyEDA project,
or a PDF schematic, optionally in a new form factor) is not part of this version of napkin
mode: its import tools are not served yet. When `$ARGUMENTS` points at such a design instead
of a concept or a netlist.json, do not convert it by hand: end with a final message that names
the design and states that its rebuild is a human step for now. A concept that only mentions
an existing board as inspiration is an ordinary concept: design it natively.

## (g) Artifacts

Beside the netlist (`<stem>` = the netlist's file name without `.json`):

- `netlist.json`: the design, with `"mode": "napkin"` and its `assumptions`
- `<stem>.decisions.jsonl`: every runtime decision (harness and yours), append-only
- `<stem>_pipeline_report.json`: gate verdicts, remediations, routing, fab package (every exit)
- `<stem>_review.md`: the human review (`review_report(netlist)` re-renders it)
- `<stem>_route_handoff.json`: the routing handoff of an exit-3 run (removed when superseded)
- `<stem>.kicad_sch`, `<stem>.kicad_pcb` (+ `<stem>.k9.kicad_pcb`, the pre-KiCad-11 snapshot,
  after agent routing), renders, and the fab zip with Gerbers, drill, BOM.csv and CPL.csv
- `libs/` and `pins/`: the parts generated for this design
