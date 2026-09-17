# dougherty-eda — Claude Code marketplace

Plugins for electronic design automation.

```
/plugin marketplace add justinmdougherty/kicad-eda
/plugin install kicad-eda@dougherty-eda
/kicad-eda:setup        # checks/installs KiCad, Java, Python
```

**New to all of this?** Start with the step-by-step
[setup guide](kicad-eda/docs/SETUP.md) — it assumes nothing and takes you
from a blank machine to your first board.

- **kicad-eda** — design circuit boards by chatting with Claude: describe the
  board, get back an ERC-clean schematic, a placed + autorouted PCB with
  clean DRC, and a fab-ready zip (Gerbers, drill, BOM, CPL). Five
  self-remediating review gates in the IEEE 15288.2 style do the engineering
  discipline; KiCad 10 + the bundled Freerouting autorouter do the file
  work, and an optional KiCad 11 nightly lets KiCad draw the schematic
  itself through its IPC API. See [`kicad-eda/README.md`](kicad-eda/README.md) to get started and
  [`kicad-eda/docs/PIPELINE.md`](kicad-eda/docs/PIPELINE.md) for the
  internals run-book.
