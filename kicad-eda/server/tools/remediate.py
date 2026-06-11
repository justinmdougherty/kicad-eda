#!/usr/bin/env python3
"""Corrective-action engine: apply a gate's auto-resolvable tasks to the design.

A failing gate emits tasks (see gates.py). The ones with an unambiguous,
standard remedy are applied here — the design is modified, the stage re-runs,
and the gate is re-tested. This closes the loop the review process demands:
a gate failure becomes completed work, not a dead stop.

Currently auto-applied:
  add_decoupling   add a 100nF/0603 bypass between the named rail and GND,
                   associated with the named IC (functional placement will
                   cluster it at that IC automatically)

Never mutates the input design — returns a modified copy plus a change log.
"""
from __future__ import annotations

import copy
import re

_BYPASS = {"lib_id": "Device:C", "value": "100nF",
           "footprint": "Capacitor_SMD:C_0603_1608Metric"}


def _next_ref(design: dict, prefix: str = "C") -> str:
    pat = re.compile(rf"^{prefix}(\d+)$")
    nums = [int(m.group(1)) for c in design["components"]
            if (m := pat.match(c["ref"]))]
    return f"{prefix}{max(nums, default=0) + 1}"


def _add_decoupling(design: dict, fix: dict) -> str:
    rail = fix["rail"]
    ref = _next_ref(design, "C")
    design["components"].append({"ref": ref, **_BYPASS})
    design["nets"].setdefault(rail, []).append([ref, "1"])
    design["nets"].setdefault("GND", []).append([ref, "2"])
    return (f"added {ref} (100nF 0603) bypass: {rail} -> GND, serving "
            f"{fix.get('ref', '?')}")


_APPLIERS = {"add_decoupling": _add_decoupling}


def apply(design: dict, tasks: list) -> tuple[dict, list]:
    """Apply every auto task with a known applier. Returns (new_design, log).
    Tasks whose fix type has no applier are skipped (they stay open)."""
    d = copy.deepcopy(design)
    log = []
    for t in tasks:
        if not t.get("auto"):
            continue
        fn = _APPLIERS.get(t.get("fix", {}).get("type"))
        if fn:
            log.append(fn(d, t["fix"]))
    return d, log
