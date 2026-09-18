# kicad-eda — Napkin to Gerbers

A Claude Code plugin that designs circuit boards **in conversation**. You
describe the board you want — what it does, how it's powered, what it
connects to — and Claude carries it from concept to a fab-ready zip:
schematic with clean ERC, placed and routed PCB with clean DRC (Claude
routes it net by net through KiCad's board API, or Freerouting does),
Gerbers, drill, BOM and placement files you can upload straight to JLCPCB.

```
You:    I need a USB-C powered 3-channel LED driver, 1.5 A per channel.
Claude: <asks the questions an engineer would, then designs it>
        … G1 requirements ✓  G2 schematic+ERC ✓  G3 placement ✓
        G4 routed, DRC 0/0 ✓  G5 fab package ✓ → led_driver_JLCPCB.zip
```

The work runs through **five formal review gates** (IEEE 15288.2 style) that
**self-remediate**: missing bypass caps get added, placement loosens itself
when routing is congested, routing micro-gaps and split power planes are
healed, under-ampacity layer transitions get parallel vias, dissipating
power packages get IPC-7093 thermal via farms. You only get asked when a
decision genuinely needs design intent.

## Install

> **First time? Never used Claude Code or cloned a repo?** Follow
> **[docs/SETUP.md](docs/SETUP.md)** — a step-by-step guide from a blank
> machine to your first board, no prior knowledge assumed.

```
/plugin marketplace add justinmdougherty/kicad-eda
/plugin install kicad-eda@dougherty-eda
```

Then run **`/kicad-eda:setup`** once — it checks the three external
dependencies and installs what's missing:

| Dependency | Used for | Windows | macOS |
|---|---|---|---|
| KiCad 10 | kicad-cli, bundled pcbnew Python, stock libraries | `winget install KiCad.KiCad` | `brew install --cask kicad` |
| KiCad 11 nightly *(optional)* | schematic API backend: KiCad draws the schematic itself, headless or live in its editor | [nightly installer](https://downloads.kicad.org/kicad/windows/explore/nightlies) (lands in `Program Files\KiCad\10.99`) | nightly `.dmg` from the same site |
| Java 17–21 | Freerouting 1.9.0 autorouter (jar is bundled) | `winget install EclipseAdoptium.Temurin.21.JRE` | `brew install --cask temurin@21` |
| Python 3.10+ | MCP server + pipeline harness | `winget install Python.Python.3.12` | `brew install python` |

Python *packages* need nothing: the server launcher
(`scripts/launch-server.js`) builds its own venv on first start and keeps it
in the plugin data dir, surviving updates. `setup` only reports the KiCad 11
nightly; it never installs it.

## Usage

Just talk to Claude about the board you want, or jump straight in:

```
/kicad-eda:pipeline a USB-C powered 3-channel LED driver, 1.5 A per channel
/kicad-eda:new-board ...     # GUI-assisted flow in the KiCad editor instead
```

You get back the full project: `netlist.json` (the design's source of
truth), `.kicad_sch`, `.kicad_pcb` (open them in KiCad), top/bottom renders
to eyeball, a design-intent report, and the fab zip.

Under the hood the conversation produces a pad-level `netlist.json`, and the
headless pipeline does the rest — schematic + ERC (through KiCad 11's
schematic API when a nightly is installed, else the plugin writes the file
itself), SWIG board build (placement, planes, preroutes, silk), routing,
DRC + IPC-2221/2141A checks, Gerber/BOM/CPL export. Power users can
author or edit `netlist.json` directly and run
`/kicad-eda:pipeline path/to/netlist.json`; the schema, gate map and the
schematic-backend rules live in **`docs/PIPELINE.md`** (the run-book).

**Routing is Claude's job, not an autorouter's.** With a KiCad 11 nightly
installed (or the board open in a running KiCad with its API server on),
Claude routes the placed board itself through KiCad's board API: it reads
pads, net classes, planes and the foreign copper near every unrouted link,
lays tracks and vias net by net, and clears real kicad-cli DRC in a repair
loop (`agent-routing` skill, `router` subagent). The tools:
`open_board`, `board_overview`, `board_pads`, `unrouted_connections`,
`route_net`, `add_vias`, `remove_routing`, `route_check`, `route_status`,
`save_board`, `close_board`, `list_open_boards`. Freerouting
(`autoroute_board`) stays as the fallback and is what the unattended
`run_pipeline.py` harness runs. One consequence to know: a board saved by the
headless KiCad 11 session becomes a **KiCad 11 file** — KiCad 9/10, their
kicad-cli/pcbnew and the Freerouting/SWIG tools cannot read it afterwards
(DRC and Gerbers still work through kicad-cli 11), so open it in KiCad 11.

## What's inside

| Component | What it does |
|---|---|
| `server/` | The `kicad-generator` MCP server (`kicad_env` finds every installed KiCad and picks one per job — board jobs follow the KiCad that wrote the file; `backends/` is the KiCad 11 IPC schematic backend and the board API backend (`pcb_api`) on the vendored `vendor/kipy`; `route_planner` + `board_route_tools` are the agent-routing geometry and tools; `sch_file_model` is the schematic file-writing fallback) + the toolchain it shells into: `sch_build` (ERC-clean schematics without KiCad 11), `swig_build_board` (placement/planes/preroutes/silk under KiCad's bundled Python), `netclass_writer` + `ipc_rules` (IPC-2221/2141A math), `swig_gate_checks` (placement/thermal/plane/height/routed-width/via-ampacity reviews), `swig_board_fixes` (auto-remediation incl. a BFS maze router), `silk_place`, `gates` + `remediate` (the review system), and `freerouting-1.9.0.jar`. |
| `pipeline/` | `run_pipeline.py` (one board through all five gates) and `run_all.py` (the developer regression suite over `examples/`). |
| `examples/` | Test netlists used to validate the process (QFN, LQFP, multi-unit op-amps, a 3 A buck, RF boards, an ESP32-C3 node, a two-sided RPi 5 HAT). Useful as schema references; not end-user content. |
| `docs/PIPELINE.md` | The run-book: netlist schema, gate map, env vars, and the known-traps list that costs real hours to rediscover. |
| `skills/` | `netlist-pipeline` (headless flow), `pcb-design` (GUI flow) and `agent-routing` (how Claude routes a board through the KiCad board API). |
| `agents/` | Gate reviewers: `parts-selector`, `erc-reviewer`, `placement-reviewer`, `drc-reviewer`, `fab-checker`; and `router`, the routing specialist that runs the route / DRC / repair loop. |
| `commands/` | `/setup`, `/pipeline`, `/new-board`, `/erc`, `/drc`, `/gerbers`. |
| `hooks/` | Cross-platform (Node): session env report, IPC-API guard for GUI tools, schematic structure check after writes. |

## Requirements

- **Claude Code or Claude Desktop** (local MCP server — not the web app).
- KiCad 10, Java 17–21, Python 3.10+ (see Install). KiCad 9 mostly works
  but the headless pipeline is validated against 10.
- **Optional: a KiCad 11 nightly** from
  <https://downloads.kicad.org/kicad/windows/explore/nightlies>. It installs
  beside KiCad 10 as `Program Files\KiCad\10.99` and enables the schematic
  API backend (`server_status().schematic_backend == "api"`) and the headless
  board API for agent routing (`server_status().board_api`). Open it once
  after installing — or copy `sym-lib-table` and `fp-lib-table` from
  `<install>\share\kicad\template` into `%APPDATA%\kicad\10.99` — or the
  headless server finds no libraries. Keep KiCad 10 for the board build (the
  nightly has no bundled pcbnew Python). Schematics and boards it saves are
  KiCad 11 files; KiCad 9/10 cannot open them (board DRC/Gerbers then run
  through the nightly's kicad-cli automatically).
- The headless pipeline never needs the KiCad GUI. The GUI flow's PCB tools
  require KiCad running with **Preferences → API → Enable KiCad API server**;
  with KiCad 11 the schematic tools also work live in the open editor.

## Developing

```bash
claude plugin validate ./kicad-eda --strict
node scripts/launch-server.js          # run the MCP server by hand
.venv/bin/python -m pytest -q          # unit tier (+ live KiCad 11 API tests when a nightly is present)
.venv/bin/python pipeline/run_all.py   # regression matrix (routes for many minutes)
docker build -t kicad-eda . && docker run --rm kicad-eda   # suite in the official KiCad container
```

CI runs the same regression suite on every push (`.github/workflows/regression.yml`).

## License

MIT
