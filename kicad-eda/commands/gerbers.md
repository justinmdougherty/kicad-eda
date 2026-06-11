---
description: Export fab-ready Gerbers and run the final fab-capability check.
---

Export the manufacturing package for the current board and run the final fabrication gate. Delegate to the `fab-checker` subagent, which exports Gerbers + drill, renders top and bottom, and checks the design against the configured fab house's capabilities. Report a ship/hold verdict.
