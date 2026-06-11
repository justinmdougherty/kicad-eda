#!/usr/bin/env python3
"""PostToolUse sanity check for kicad-eda schematic writes.

Reads the hook event JSON from stdin, finds the schematic path touched by the
tool call, and runs a fast structural check on the .kicad_sch S-expression:
balanced parens, valid top-level token, non-empty. This catches the
read-modify-write corruption that bit sequential edits before it compounds.

Non-blocking by design: prints a warning to stderr and exits 2 only on a
clear structural break, so a real corruption is surfaced to the model while
benign tool calls pass silently.
"""
import json
import sys
import os
import re


def find_sch_path(event: dict):
    """Pull a .kicad_sch path out of the tool input, wherever it lives."""
    ti = event.get("tool_input", {}) or {}
    for key in ("sch", "schematic", "path", "file", "filename", "output"):
        v = ti.get(key)
        if isinstance(v, str) and v.endswith(".kicad_sch"):
            return v
    # Last resort: scan the serialized input for a path-looking token.
    m = re.search(r"[\w./\\-]+\.kicad_sch", json.dumps(ti))
    return m.group(0) if m else None


def check(path: str):
    if not os.path.isfile(path):
        return None  # nothing written yet / not on this machine — skip
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        text = f.read()
    if not text.strip():
        return f"{path} is empty after write."
    if "(kicad_sch" not in text[:200]:
        return f"{path} does not start with a (kicad_sch ...) block."
    bal = 0
    for ch in text:
        if ch == "(":
            bal += 1
        elif ch == ")":
            bal -= 1
            if bal < 0:
                return f"{path} has unbalanced parentheses (extra ')')."
    if bal != 0:
        return f"{path} has unbalanced parentheses ({bal:+d})."
    return None


def main():
    try:
        event = json.load(sys.stdin)
    except Exception:
        sys.exit(0)
    path = find_sch_path(event)
    if not path:
        sys.exit(0)
    problem = check(path)
    if problem:
        print(
            f"SCHEMATIC SANITY: {problem} The last write may have corrupted the "
            f"file. Re-generate it from a clean state (reload, then re-apply the "
            f"change) rather than continuing to edit the broken file.",
            file=sys.stderr,
        )
        sys.exit(2)
    sys.exit(0)


if __name__ == "__main__":
    main()
