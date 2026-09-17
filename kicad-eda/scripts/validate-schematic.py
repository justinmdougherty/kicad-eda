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


PATH_KEYS = ("sch", "out_sch", "schematic", "path", "file", "filename", "output")
# top-level blocks every schematic the plugin writes must carry (KiCad refuses
# a file without sheet_instances; an empty lib_symbols is legal but noted)
REQUIRED_BLOCKS = ("(lib_symbols", "(sheet_instances")


def find_sch_path(event: dict):
    """Pull a .kicad_sch path out of the tool input / response, wherever it lives."""
    ti = event.get("tool_input", {}) or {}
    tr = event.get("tool_response", {}) or {}
    if not isinstance(tr, dict):
        tr = {}
    for src in (ti, tr):
        for key in PATH_KEYS:
            v = src.get(key)
            if isinstance(v, str) and v.endswith(".kicad_sch"):
                return v
    # build_schematic_from_netlist without out_sch writes <netlist>.kicad_sch
    nl = ti.get("netlist")
    if isinstance(nl, str) and nl.endswith(".json"):
        return nl[: -len(".json")] + ".kicad_sch"
    # Last resort: scan the serialized input for a path-looking token.
    m = re.search(r"[\w./\\-]+\.kicad_sch", json.dumps(ti))
    return m.group(0) if m else None


def paren_balance(text: str):
    """Running paren depth; returns the final depth or a negative number as
    soon as a stray ')' closes more than was opened."""
    bal, in_str, escape = 0, False, False
    for ch in text:
        if in_str:  # parens inside quoted strings (e.g. a "(NC)" value) don't count
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "(":
            bal += 1
        elif ch == ")":
            bal -= 1
            if bal < 0:
                return bal
    return bal


def check(path: str):
    if not os.path.isfile(path):
        return None  # nothing written yet / not on this machine — skip
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        text = f.read()
    if not text.strip():
        return f"{path} is empty after write."
    if "(kicad_sch" not in text[:200]:
        return f"{path} does not start with a (kicad_sch ...) block."
    bal = paren_balance(text)
    if bal < 0:
        return f"{path} has unbalanced parentheses (extra ')')."
    if bal != 0:
        return f"{path} has unbalanced parentheses ({bal:+d})."
    missing = [b for b in REQUIRED_BLOCKS if b not in text]
    if missing:
        return f"{path} is missing required top-level block(s): {', '.join(missing)}."
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
