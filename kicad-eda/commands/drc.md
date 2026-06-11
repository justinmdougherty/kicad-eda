---
description: Run the DRC gate on the current routed board against the configured fab profile.
---

Run the Design Rules Check gate on the current, fully routed board. Confirm KiCad 9 is running with the IPC API enabled, then delegate to the `drc-reviewer` subagent and report the prioritized punch list. Do not export Gerbers while any BLOCKER stands.
