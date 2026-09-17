---
description: Export fab-ready Gerbers and run the final fab-capability check.
---

Export the manufacturing package for the current board and run the final fabrication gate. Confirm `drc_pcb(pcb)` reports 0 errors / 0 unconnected on the saved board (if `test_kicad_connection().open_documents` lists it, have the user save in Pcbnew first), then delegate to the `fab-checker` subagent, which runs `export_fab_package` (Gerbers + drill + pick-and-place, zipped), renders top and bottom, and checks the design against the configured fab house's capabilities. Report a ship/hold verdict.
