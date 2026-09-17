---
description: Run the ERC / Critical Design Review gate on the current schematic.
---

Run the Electrical Rules Check and Critical Design Review gate on the current schematic. Make sure the file on disk is current (`save_schematic(sch)` if the in-memory model changed), then delegate to the `erc-reviewer` subagent — it runs `run_erc(sch)` — and report its pass/fail verdict and findings. Do not advance to PCB layout while any BLOCKER stands.
