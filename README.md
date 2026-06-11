# dougherty-eda — Claude Code marketplace

Plugins for electronic design automation.

```
/plugin marketplace add dougherty-labs/kicad-eda
/plugin install kicad-eda@dougherty-eda
/kicad-eda:setup        # checks/installs KiCad, Java, Python
```

- **kicad-eda** — concept-to-Gerbers PCB design for KiCad 9/10: a headless
  gated pipeline (netlist → schematic+ERC → placement → Freerouting → DRC →
  fab zip) with self-remediating IEEE 15288.2-style reviews, plus a
  GUI-assisted 10-stage flow. Bundled MCP server, autorouter jar, gate
  reviewer subagents, cross-platform hooks. See
  [`kicad-eda/docs/PIPELINE.md`](kicad-eda/docs/PIPELINE.md) for the run-book.
