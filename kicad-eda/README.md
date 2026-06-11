# kicad-eda — Napkin to Gerbers

A Claude Code plugin that designs circuit boards **in conversation**. You
describe the board you want — what it does, how it's powered, what it
connects to — and Claude carries it from concept to a fab-ready zip:
schematic with clean ERC, placed and autorouted PCB with clean DRC, Gerbers,
drill, BOM and placement files you can upload straight to JLCPCB.

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

```
/plugin marketplace add justinmdougherty/kicad-eda
/plugin install kicad-eda@dougherty-eda
```

Then run **`/kicad-eda:setup`** once — it checks the three external
dependencies and installs what's missing:

| Dependency | Used for | Windows | macOS |
|---|---|---|---|
| KiCad 10 | kicad-cli, bundled pcbnew Python, stock libraries | `winget install KiCad.KiCad` | `brew install --cask kicad` |
| Java 17–21 | Freerouting 1.9.0 autorouter (jar is bundled) | `winget install EclipseAdoptium.Temurin.21.JRE` | `brew install --cask temurin@21` |
| Python 3.10+ | MCP server + pipeline harness | `winget install Python.Python.3.12` | `brew install python` |

Python *packages* need nothing: the server launcher
(`scripts/launch-server.js`) builds its own venv on first start and keeps it
in the plugin data dir, surviving updates.

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
headless pipeline does the rest — schematic + ERC, SWIG board build
(placement, planes, preroutes, silk), Freerouting autoroute, DRC +
IPC-2221/2141A checks, Gerber/BOM/CPL export. Power users can author or edit
`netlist.json` directly and run `/kicad-eda:pipeline path/to/netlist.json`;
the schema and gate map live in **`docs/PIPELINE.md`** (the run-book).

## What's inside

| Component | What it does |
|---|---|
| `server/` | The `kicad-generator` MCP server + the toolchain it shells into: `sch_build` (ERC-clean schematics), `swig_build_board` (placement/planes/preroutes/silk under KiCad's bundled Python), `netclass_writer` + `ipc_rules` (IPC-2221/2141A math), `swig_gate_checks` (placement/thermal/plane/height/routed-width/via-ampacity reviews), `swig_board_fixes` (auto-remediation incl. a BFS maze router), `silk_place`, `gates` + `remediate` (the review system), and `freerouting-1.9.0.jar`. |
| `pipeline/` | `run_pipeline.py` (one board through all five gates) and `run_all.py` (the developer regression suite over `examples/`). |
| `examples/` | Test netlists used to validate the process (QFN, LQFP, multi-unit op-amps, a 3 A buck, RF boards, an ESP32-C3 node, a two-sided RPi 5 HAT). Useful as schema references; not end-user content. |
| `docs/PIPELINE.md` | The run-book: netlist schema, gate map, env vars, and the known-traps list that costs real hours to rediscover. |
| `skills/` | `netlist-pipeline` (headless flow) and `pcb-design` (GUI flow). |
| `agents/` | Gate reviewers: `parts-selector`, `erc-reviewer`, `placement-reviewer`, `drc-reviewer`, `fab-checker`. |
| `commands/` | `/setup`, `/pipeline`, `/new-board`, `/erc`, `/drc`, `/gerbers`. |
| `hooks/` | Cross-platform (Node): session env report, IPC-API guard for GUI tools, schematic structure check after writes. |

## Requirements

- **Claude Code or Claude Desktop** (local MCP server — not the web app).
- KiCad 10, Java 17–21, Python 3.10+ (see Install). KiCad 9 mostly works
  but the headless pipeline is validated against 10.
- The headless pipeline never needs the KiCad GUI. The GUI flow's PCB tools
  require KiCad running with **Preferences → API → Enable KiCad API server**.

## Developing

```bash
claude plugin validate ./kicad-eda --strict
node scripts/launch-server.js          # run the MCP server by hand
.venv/bin/python pipeline/run_all.py   # regression matrix (routes for many minutes)
docker build -t kicad-eda . && docker run --rm kicad-eda   # suite in the official KiCad container
```

CI runs the same regression suite on every push (`.github/workflows/regression.yml`).

## License

MIT
