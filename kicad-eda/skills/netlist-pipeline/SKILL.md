---
name: netlist-pipeline
description: >-
  Headless reference for the netlist-first flow: authoring a pad-level
  netlist.json (nets by pad number, net_specs, preroutes, the board block)
  and running the gated harness run_pipeline.py (G1 lint -> G2 schematic +
  ERC -> G3 placement -> G4 routing, Freerouting with a handoff to the router
  subagent -> G5 fab zip) with its flags, exit codes and artifacts, or the
  same steps one MCP tool at a time. Use for a supervised headless build
  where the user stays in the loop and makes the calls, including
  fixed-outline, two-sided and high-current designs. Not for hands-off,
  no-questions or unattended builds (use napkin-mode), and not for the
  interactive, GUI-assisted 10-stage flow (use pcb-design).
---

# Headless netlist-first pipeline

The complete run-book lives at `${CLAUDE_PLUGIN_ROOT}/docs/PIPELINE.md` —
**read it before authoring**. This skill is the working summary.

## The flow

```
netlist.json -> G1 lint -> G2 schematic+ERC -> G3 placement -> G4 routing -> G5 fab zip
```

Run: `<venv-python> ${CLAUDE_PLUGIN_ROOT}/pipeline/run_pipeline.py <netlist.json> [<w> <h>] [placement.json] [--strict] [--route auto|freerouting|agent] [--resume routed|stalled]`
Regression: `<venv-python> ${CLAUDE_PLUGIN_ROOT}/pipeline/run_all.py` (examples/)

- Board dims are optional: the netlist `board` block, else the board is
  auto-sized to its parts (a design whose every part is fixed needs the
  block or the dims).
- Napkin behaviour is the default (ROADMAP D5): the harness turns floating
  IC output / I/O / passive / NC pins, connectors facing the board interior,
  fractured planes and short EP via farms into automatic remedies, one
  decision each (floating inputs stay designer tasks); `--strict` turns
  these policies off (a netlist `"mode": "napkin"` always means napkin). A
  hands-off run follows the napkin-mode skill.
- Exit codes: `0` PASS, `1` FAIL (also a resumed run that cannot continue:
  the report's `error`), `2` usage error, `3` routing handoff (G4 below).
- Artifacts beside the netlist (`<stem>` = its file name without `.json`),
  written on every exit: `<stem>_pipeline_report.json` (gate verdicts,
  remediations, routing, fab package, `error`), `<stem>_review.md` (the human
  review; `review_report(netlist)` re-renders it) and the append-only
  decision log `<stem>.decisions.jsonl` (yours go in with `record_decision`);
  on exit 3 also `<stem>_route_handoff.json`. Then `<stem>.kicad_sch` /
  `<stem>.kicad_pcb` (`<stem>.napkin.*` siblings while the canonical files are
  open in a KiCad editor), the renders `<stem>_final_top.png` /
  `<stem>_final_bottom.png`, the fab zip `<stem>_<FAB_PROFILE>.zip`, and after
  agent routing the pre-KiCad-11 snapshot `<stem>.k9.kicad_pcb`.

Gates self-feed: missing bypass caps are auto-added (G2), courtyard overlap
re-places at a looser pack scale (G3), same-net micro-gaps and plane islands
are healed and under-ampacity transitions get parallel vias (G4, the
unattended Freerouting harness). Only design-intent blockers stop the run —
fix them in the netlist and re-run. Never hand-edit the generated board to
silence a gate.

G4 has two implementations. **Agent routing is the default when you drive
the board yourself:** after G3 you route it net by net through the KiCad
board API (`agent-routing` skill, `router` subagent) — `route_check` gives
you every DRC violation with coordinates and `route_status` is the routed
gate. **The harness** routes with Freerouting and the healers above, and
`--route` decides what happens around it: `auto` (default) hands the board
to the agent when G4 fails — it writes `<stem>_route_handoff.json` (`pcb`,
`why`, `unconnected_items`, `net_classes`, `instructions`, `resume`) and
exits 3; `freerouting` makes a G4 failure final (exit 1); `agent` hands the
placed board off without trying Freerouting. Route a handoff with the
`router` subagent and its `instructions`, `close_board(pcb, save=True)`,
then run the handoff's `resume` line: `--resume routed` judges the agent's
board (G3 carried from the handoff, G4 judges the agent's copper);
`--resume stalled`, after `route_check` reported `stalled`, runs Freerouting
from the pre-KiCad-11 snapshot, then a higher pack_scale, then stops. A
resumed run never hands off again, and a netlist changed since the handoff
needs a fresh run. A pipeline feature may refuse the handoff: G4 then fails
with "routing handoff refused: ...". `autoroute_board` remains the
conversational fallback for a KiCad 9 format board.

## MCP tools for the headless flow

`run_pipeline.py` is primary (it owns the remediation loop). The same steps
exist as `kicad-generator` MCP tools for driving a board one stage at a time:

- `build_schematic_from_netlist(netlist, out_sch, symbols_dir, sidecars_dir, run_erc, backend="auto")` — G2: ERC-clean `.kicad_sch` (a label per net pin, no_connects, PWR_FLAGs). `backend` "api" = KiCad 11 places every symbol through its schematic API and writes a **KiCad 11 file** (ERC with the newest kicad-cli; `symbols_dir` / `sidecars_dir` are ignored), "file" = `sch_build.py` with embedded symbols (KiCad 9/10 format); "auto" takes the API when a KiCad 11 nightly is installed. Fails with `missing_pins` when a (ref, pad) resolves to no pin. The response's `backend` says which ran; same topology either way, so G2 is identical.
- `design_report(netlist)` — G2 design-intent review (decoupling, power tree, floating pins, pulls); writes `<netlist>_report.md`.
- `build_board_from_netlist(netlist, board_w, board_h, out_pcb, footprints_dir, placement, pack_scale)` — G3: placed board, planes, Edge.Cuts, IPC net classes written to the `.kicad_pro`.
- `check_design_gates(pcb, netlist, stage="placement"|"routed")` — the G3 / G4 review; `passed` must be true before the next step. On a board saved by the KiCad 11 board API (below) the SWIG checks cannot read the file: `stage="routed"` then answers with `route_status` (`backend: "api"`), `stage="placement"` is refused.
- **G4, default — agent routing (KiCad board API, `agent-routing` skill):** `open_board(pcb, mode)` / `board_overview(pcb)` (layers, planes, rules, net classes, outline, footprints) → `unrouted_connections(pcb, net, with_obstacles, clearance)` (the work list, each link with the foreign copper near its straight line on F.Cu / B.Cu) → `route_net(pcb, net, path, width, allow_narrow, via_diameter, via_drill)` (polylines + vias in mm; validated, then created) / `add_vias(pcb, net, points, diameter, drill)` / `remove_routing(pcb, net, ids)` → `route_check(pcb, refill, save, max_items)` (refill zones, save, kicad-cli DRC: `violations[*].items[*]{description, x, y}`, `unconnected_items`) → `route_status(pcb, netlist)` (`passed` = every net one cluster + width / via-ampacity checks — the G4 gate) → `save_board(pcb)` / `close_board(pcb, save)`; `list_open_boards()`. Route 3–6 nets, `route_check`, fix, repeat. `board_pads(pcb, net, ref)` gives pad geometry. Headless needs a KiCad 11 nightly (`server_status().board_api`); a board open in a running KiCad GUI (API server enabled) is routed in place instead.
- `autoroute_board(pcb, max_passes, jar, route_timeout)` — G4, fallback: Freerouting DSN→SES round trip, zones refilled; re-run until `unconnected` is 0. **KiCad 9 format boards only** (it runs under KiCad 9/10's pcbnew Python).
- `add_ground_stitching(pcb, gnd_net, max_freq_hz, er, interior, via_diameter_mm, via_drill_mm)` — transactional GND via fence/grid on a DRC-clean routed board (SWIG: KiCad 9 format only; on an API-routed board use `add_vias` on GND).
- `export_fab_package(pcb, out_dir, zip_name, netlist)` — G5: Gerbers, drill, CPL, BOM, zipped.
- `build_pin_sidecars("Lib:Symbol,...")` — optional: both flows derive pin coordinates from the symbol; a sidecar in `server/pins/` overrides for both. Still a valid symbol-id check (exits 1 on an unknown symbol).
- `server_status()` — every installed KiCad (`installs`), the kicad-cli / bundled Python / libraries in use, `schematic_backend` ("api" | "file") with the reason when it is "file", `board_api` (bool) with `board_api_reason`, `boards_open`, Java/Freerouting, IPC, open documents, capabilities.
- `rescan_kicad_installs()` — drop every cached KiCad probe and discover again (after installing/removing a KiCad, or when `kicad_python` is null with a timeout / cannot-start reason); returns the fresh picks.

Every writer above refuses a file that is open in a KiCad editor — close it
first (`test_kicad_connection().open_documents` shows what is open).

**Backends and installs** (`docs/PIPELINE.md` section 8): the newest kicad-cli
runs schematic ERC/render, the newest KiCad with a pcbnew Python (9/10)
builds the board, board jobs (DRC/Gerbers/render) use the kicad-cli of the
KiCad that **wrote** the board file (its header: 9 for a pcbnew-built board,
11 for one saved by the board API), and a KiCad 11 nightly (10.99+, optional)
serves the schematic API and the headless board API. A schematic written by
the API backend needs KiCad 11 to open; pass `backend="file"` for a KiCad
9/10 file. **A board saved by the headless KiCad 11 board session is a
KiCad 11 file** (`version 20260901`): KiCad 9/10, their kicad-cli and pcbnew
Python cannot read it, so `autoroute_board` / `check_design_gates` (placement)
/ `add_ground_stitching` refuse it with the reason, while `drc_pcb`,
`export_fab_package`, `render_pcb` keep working through kicad-cli 11. The
first save snapshots the pre-KiCad-11 board automatically as
`X.k9.kicad_pcb` + `X.k9.kicad_pro` (+ `.kicad_prl` / `.kicad_dru`; the
responses carry `pre_k11_snapshot`), and `autoroute_board(pcb, from_snapshot=True)`
restores it (the KiCad 11 copy moves to `X.agent.kicad_pcb`) and routes it
with Freerouting. A nightly that has never been opened has no library tables
(`symbol Device:R not found`) — open it once or copy `sym-lib-table` /
`fp-lib-table` from its `share/kicad/template`.

## Authoring rules that decide success

1. **Pad-correct nets.** Exposed pads have their own pad number (QFN-24 EP =
   pin 25). `Device:LED` pin 1 = cathode. Verify real pin maps against the
   datasheet, not the symbol name. A generated part's `pin_groups` go on
   their nets pad by pad: every number of `pin_groups["GND"]` (and of any
   other group of one net) on that net — the stacked copies are hidden on
   the symbol and float otherwise; a RESERVED group goes to `no_connect`.
2. **Design intent in `net_specs`**: `{"5V": {"voltage":5, "current":4}}`
   drives IPC-2221/2141A track widths, via sizes, and the G4 ampacity gate.
   Spec CONTINUOUS current — bursts ride the bulk caps (IPC-2152).
3. **Preroute every high-current path** (`"preroutes"`): Freerouting 1.9
   cannot neck down, so a wide width-class into a small pad is THE
   unroutable pattern. Prerouted and plane nets are width-class-exempt; the
   router connects their remaining taps at signal width. (Agent routing can
   neck down — `route_net(..., allow_narrow=True)` for the ≤ 3 mm pad entry —
   but preroutes still pin the fat copper where the designer wants it.)
4. **Hand-place the power chain in current-flow order** with fixed `x/y/rot`
   on those components; let the auto-placer handle the rest. Fixed-outline
   boards declare `"board": {w, h, auto_x/y/w/h, auto_b}` (free fields for
   auto-placed top/bottom parts).
5. `"side": "B"` puts a part on the bottom; `"interior": true` exempts a J*
   part from edge-connector treatment; `"lcsc"/"mpn"` flow into BOM.csv.
6. Both flows (`sch_build` and the GUI-flow `wire_by_ref` / `label_pin` /
   `power_pin`) derive pin coordinates from the symbol itself; a sidecar in
   `server/pins/` (`build_pin_sidecars`, optional) overrides for both. A pad
   that resolves to no coordinate fails the run with `missing_pins` — fix
   the pad number, don't guess.
7. Lint first (`lint_netlist.py`, 1 s) — never debug a 2-minute pipeline run
   for a single-pin net.

## When something fails

`docs/PIPELINE.md` section 7 lists the known traps (deterministic router,
DSN plane abstraction, KiCad 10 SWIG padstack asserts, `.kicad_pro` rule
override...). Check it before debugging from scratch. Post-route fixers
(`silk_place.py`, `swig_board_fixes.py`) run standalone on a routed board and
never require re-routing.
