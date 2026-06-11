#!/usr/bin/env python3
"""Build pin-coordinate sidecars from KiCad .kicad_sym libraries.

For each requested ``Library:Symbol`` lib_id, parse the library file and emit
``<out>/<Library_Symbol>.pins.json`` mapping pin name AND pin number to the
pin's connection-point offset in SCHEMATIC space (mm).

Coordinate note: symbol files use +Y = up; a placed schematic symbol uses
+Y = down. So the schematic-space offset of a pin at symbol coord (x, y) is
(x, -y). Sidecars are consumed by the server's wire_by_ref / label_pin /
power_pin when the symbol is placed at rotation 0.

Usage:
  python build_pin_sidecars.py <symbols_dir> <out_dir> Lib:Sym [Lib:Sym ...]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import sexpdata

S = sexpdata.Symbol


def _is(node, tag: str) -> bool:
    return isinstance(node, list) and node and isinstance(node[0], S) and node[0].value() == tag


def _children(node, tag: str):
    return [c for c in node if _is(c, tag)]


def _first(node, tag: str):
    for c in node:
        if _is(c, tag):
            return c
    return None


def _load_lib(symbols_dir: Path, lib: str):
    path = symbols_dir / f"{lib}.kicad_sym"
    if not path.is_file():
        raise FileNotFoundError(path)
    data = sexpdata.loads(path.read_text(encoding="utf-8"))
    # top-level (kicad_symbol_lib (symbol "Name" ...) ...)
    return {c[1]: c for c in _children(data, "symbol")}


def _collect_pins(symbol_node, lib_symbols: dict) -> dict:
    """Return {key: {x, y}} for a symbol, resolving 'extends' and child units."""
    pins: dict[str, dict] = {}

    ext = _first(symbol_node, "extends")
    if ext is not None:
        base = lib_symbols.get(ext[1])
        if base is not None:
            pins.update(_collect_pins(base, lib_symbols))

    # pins live in child sub-symbols (Name_unit_style); also tolerate direct pins
    def harvest(node):
        for pin in _children(node, "pin"):
            at = _first(pin, "at")
            name = _first(pin, "name")
            number = _first(pin, "number")
            if at is None:
                continue
            x, y = float(at[1]), float(at[2])
            off = {"x": round(x, 4), "y": round(-y, 4)}  # +Y up -> schematic +Y down
            if number is not None and isinstance(number[1], str):
                pins[number[1]] = off
            if name is not None and isinstance(name[1], str) and name[1] not in ("~", ""):
                pins.setdefault(name[1], off)

    harvest(symbol_node)
    for child in _children(symbol_node, "symbol"):
        harvest(child)
    return pins


def main() -> int:
    if len(sys.argv) < 4:
        print(__doc__)
        return 2
    symbols_dir = Path(sys.argv[1])
    out_dir = Path(sys.argv[2])
    lib_ids = sys.argv[3:]
    out_dir.mkdir(parents=True, exist_ok=True)

    cache: dict[str, dict] = {}
    summary = []
    for lib_id in lib_ids:
        lib, _, sym = lib_id.partition(":")
        if lib not in cache:
            cache[lib] = _load_lib(symbols_dir, lib)
        lib_symbols = cache[lib]
        if sym not in lib_symbols:
            summary.append((lib_id, "SYMBOL NOT FOUND", 0))
            continue
        pins = _collect_pins(lib_symbols[sym], lib_symbols)
        safe = lib_id.replace(":", "_")
        (out_dir / f"{safe}.pins.json").write_text(json.dumps(pins, indent=2))
        summary.append((lib_id, "ok", len(pins)))

    for lib_id, status, n in summary:
        print(f"  {lib_id:42s} {status:18s} keys={n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
