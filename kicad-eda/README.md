# kicad-eda — Napkin to Gerbers

A Claude Code plugin for end-to-end PCB design in **KiCad 9/10**, two ways:

1. **Headless gated pipeline** (the flagship): author a pad-level
   `netlist.json` → schematic + ERC → SWIG board build (placement, planes,
   preroutes, silk) → Freerouting autoroute → DRC + IPC-2221/2141A checks →
   fab-ready zip (Gerbers, drill, BOM.csv, CPL.csv). Five formal review gates
   in the IEEE 15288.2 style that **self-remediate**: missing bypass caps are
   added, placement loosens itself, routing micro-gaps and plane islands are
   healed, under-ampacity layer transitions get parallel vias. Proven on a
   10-board regression suite up to a 68-part two-sided Raspberry Pi HAT.
   **Read `docs/PIPELINE.md` — it is the run-book.**
2. **GUI-assisted 10-stage flow** (`skills/pcb-design`): concept → parts →
   schematic → ERC → CDR → placement → routing → DRC → Gerbers, with gate
   reviews delegated to bundled subagents.

## Install

```
/plugin marketplace add dougherty-labs/kicad-eda
/plugin install kicad-eda@dougherty-eda
```

Then run **`/kicad-eda:setup`** once — it checks the three external
dependencies and installs what's missing:

| Dependency | Used for | Windows | macOS |
|---|---|---|---|
| KiCad 9/10 | kicad-cli, bundled pcbnew Python, stock libraries | `winget install KiCad.KiCad` | `brew install --cask kicad` |
| Java 17–21 | Freerouting 1.9.0 autorouter (jar is bundled) | `winget install EclipseAdoptium.Temurin.21.JRE` | `brew install --cask temurin@21` |
| Python 3.10+ | MCP server + pipeline harness | `winget install Python.Python.3.12` | `brew install python` |

Python *packages* need nothing: the server launcher
(`scripts/launch-server.js`) builds its own venv on first start and keeps it
in the plugin data dir, surviving updates.

## What's inside

| Component | What it does |
|---|---|
| `server/` | The `kicad-generator` MCP server + the toolchain it shells into: `sch_build` (ERC-clean schematics), `swig_build_board` (placement/planes/preroutes/silk under KiCad's bundled Python), `netclass_writer` + `ipc_rules` (IPC-2221/2141A math), `swig_gate_checks` (routed-width + via-ampacity review), `swig_board_fixes` (post-route healing incl. a BFS maze router), `silk_place`, `gates` + `remediate` (the review system), and `freerouting-1.9.0.jar`. |
| `pipeline/` | `run_pipeline.py` (one board through all five gates) and `run_all.py` (regression suite over `examples/`, including a negative fixture that must fail-then-self-heal). |
| `examples/` | Ten netlists: QFN, LQFP, multi-unit op-amps, a 3 A buck, RF coupon + LNA, an ESP32-C3 node with antenna keepout, and the RPi 5 battery-emulator HAT (fixed outline, two-sided, preroutes). |
| `docs/PIPELINE.md` | The run-book: netlist schema, gate map, env vars, and the known-traps list that costs real hours to rediscover. |
| `skills/` | `netlist-pipeline` (headless flow) and `pcb-design` (GUI flow). |
| `agents/` | Gate reviewers: `parts-selector`, `erc-reviewer`, `placement-reviewer`, `drc-reviewer`, `fab-checker`. |
| `commands/` | `/setup`, `/pipeline`, `/new-board`, `/erc`, `/drc`, `/gerbers`. |
| `hooks/` | Cross-platform (Node): session env report, IPC-API guard for GUI tools, schematic structure check after writes. |

## Usage

```
/kicad-eda:pipeline a USB-C powered 3-channel LED driver, 1.5 A per channel
/kicad-eda:pipeline path/to/netlist.json        # run an existing netlist
/kicad-eda:new-board ...                        # GUI-assisted flow instead
```

## Requirements

- **Claude Code or Claude Desktop** (local MCP server — not the web app).
- KiCad 9/10, Java 17–21, Python 3.10+ (see Install).
- The headless pipeline never needs the KiCad GUI. The GUI flow's PCB tools
  require KiCad running with **Preferences → API → Enable KiCad API server**.

## Developing

```bash
claude plugin validate ./kicad-eda --strict
node scripts/launch-server.js          # run the MCP server by hand
.venv/bin/python pipeline/run_all.py   # regression matrix (routes for many minutes)
```

## License

MIT
