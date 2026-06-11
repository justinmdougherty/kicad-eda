# dougherty-eda — Claude Code marketplace

Plugins for electronic design automation.

```
/plugin marketplace add justinmdougherty/kicad-eda
/plugin install kicad-eda@dougherty-eda
/kicad-eda:setup        # checks/installs KiCad, Java, Python
```

- **kicad-eda** — design circuit boards by chatting with Claude: describe the
  board, get back an ERC-clean schematic, a placed + autorouted PCB with
  clean DRC, and a fab-ready zip (Gerbers, drill, BOM, CPL). Five
  self-remediating review gates in the IEEE 15288.2 style do the engineering
  discipline; KiCad 10 + the bundled Freerouting autorouter do the file
  work. See [`kicad-eda/README.md`](kicad-eda/README.md) to get started and
  [`kicad-eda/docs/PIPELINE.md`](kicad-eda/docs/PIPELINE.md) for the
  internals run-book.
