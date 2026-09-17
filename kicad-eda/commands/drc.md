---
description: Run the DRC gate on the current routed board against the configured fab profile.
---

Run the Design Rules Check gate on the current, fully routed board. Call `test_kicad_connection()` first — if the board is listed in `open_documents`, ask the user to save it in Pcbnew (`drc_pcb` reads the saved file). Then delegate to the `drc-reviewer` subagent and report the prioritized punch list. Do not export Gerbers while any BLOCKER stands.
