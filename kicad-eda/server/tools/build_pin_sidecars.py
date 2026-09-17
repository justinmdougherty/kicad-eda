#!/usr/bin/env python3
"""Build pin-coordinate sidecars from KiCad .kicad_sym libraries.

For each requested ``Library:Symbol`` lib_id, parse the library file and emit
``<out>/<Library_Symbol>.pins.json`` mapping pin name AND pin number to the
pin's connection-point offset in SCHEMATIC space (mm).

Coordinate note: symbol files use +Y = up; a placed schematic symbol uses
+Y = down. So the schematic-space offset of a pin at symbol coord (x, y) is
(x, -y). Sidecars are consumed by the server's wire_by_ref / label_pin /
power_pin when the symbol is placed at rotation 0. (sch_build derives the same
offsets itself via symlib.pin_offsets; a sidecar only overrides them.)

A missing library or symbol is reported per lib_id and does not abort the
batch; the exit code is 1 if anything failed. The LAST stdout line is JSON:
  {"ok": bool, "written": [path, ...], "failed": [{"symbol", "reason"}, ...]}

Usage:
  python build_pin_sidecars.py <symbols_dir> <out_dir> Lib:Sym [Lib:Sym ...]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from symlib import SymbolLibError, flatten, pin_offsets  # noqa: E402

USAGE_EXIT = 2


def sidecar_map(cache: dict, symbols_dir: Path, lib_id: str) -> dict:
    """{key: {x, y}} for one lib_id — the sidecar file format (no unit key)."""
    lib, _, sym = lib_id.partition(":")
    if not lib or not sym:
        raise SymbolLibError(f"'{lib_id}' is not of the form Library:Symbol")
    flat = flatten(cache, symbols_dir, lib, sym)
    return {k: {"x": v["x"], "y": v["y"]} for k, v in pin_offsets(flat).items()}


def build_all(symbols_dir: Path, out_dir: Path, lib_ids: list) -> dict:
    cache: dict = {}
    written, failed = [], []
    for lib_id in lib_ids:
        try:
            pins = sidecar_map(cache, symbols_dir, lib_id)
        except SymbolLibError as e:
            failed.append({"symbol": lib_id, "reason": str(e)})
            print(f"  {lib_id:42s} FAILED  {e}")
            continue
        path = out_dir / f"{lib_id.replace(':', '_')}.pins.json"
        try:
            path.write_text(json.dumps(pins, indent=2), encoding="utf-8")
        except OSError as e:
            failed.append({"symbol": lib_id, "reason": f"cannot write {path}: {e}"})
            print(f"  {lib_id:42s} FAILED  cannot write {path}: {e}")
            continue
        written.append(str(path))
        print(f"  {lib_id:42s} ok                 keys={len(pins)}")
    return {"ok": not failed, "written": written, "failed": failed}


def main() -> int:
    if len(sys.argv) < 4:
        print(__doc__)
        return USAGE_EXIT
    symbols_dir = Path(sys.argv[1])
    out_dir = Path(sys.argv[2])
    lib_ids = sys.argv[3:]
    out_dir.mkdir(parents=True, exist_ok=True)
    summary = build_all(symbols_dir, out_dir, lib_ids)
    print(json.dumps(summary))
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
