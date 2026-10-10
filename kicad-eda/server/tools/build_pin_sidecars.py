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

Symbol sources (ROADMAP_DECISIONS D1): sidecar_map / build_all take a
symlib.SymbolSource -- a directory (``<dir>/<nick>.kicad_sym``), a
libtable.LibraryResolver (netlist ``libraries`` block, project
``sym-lib-table``, then stock) or a library map dict
(LibraryResolver.library_map()).

A missing library or symbol is reported per lib_id and does not abort the
batch; the exit code is 1 if anything failed. Progress lines (one per lib_id)
go to stderr, so in-process callers (a stdio MCP server) never write to stdout;
the CLI's stdout is the JSON summary alone (the usage text on a usage error):
  {"ok": bool, "written": [path, ...], "failed": [{"symbol", "reason"}, ...],
   "pins": {lib_id: pin count}, "out_dir": path}
(--netlist adds "netlist", and "note" when the netlist has no project parts).

Usage:
  python build_pin_sidecars.py <symbols> <out_dir> Lib:Sym [Lib:Sym ...]
  python build_pin_sidecars.py --netlist <netlist.json> [--symbols <stock_dir>]
                               [--out <out_dir>] [Lib:Sym ...]

  <symbols>   a symbols directory (<dir>/<Lib>.kicad_sym), or a library map .json
  --netlist   resolve through the netlist's libraries block, then the project
              sym-lib-table beside it, then --symbols (the stock symbols dir;
              without it only project libraries resolve). Default --out:
              <netlist dir>/pins (D1: project parts' sidecars live there). Default
              ids: every lib_id of the netlist whose library is a project library
              (and, with --symbols, any that resolves nowhere). A project-library
              sidecar is never written into the plugin's central server/pins.
"""
from __future__ import annotations

import json
import os
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import libtable  # noqa: E402
# the sidecar dirs are sch_build's (it searches <netlist dir>/pins, then server/pins)
from sch_build import (  # noqa: E402
    CENTRAL_PINS_DIR,
    LIBRARY_MAP_SUFFIX,
    PROJECT_PINS_DIR,
    read_library_map,
)
from symlib import SymbolLibError, SymbolSource, flatten, pin_numbers, pin_offsets  # noqa: E402

USAGE_EXIT = 2
PROJECT_SOURCES = (libtable.NETLIST, libtable.PROJECT)
_OPTIONS = ("--netlist", "--symbols", "--out")


def _progress(line: str) -> None:
    """One progress line on stderr (stdout carries only the JSON summary)."""
    print(line, file=sys.stderr)


def _flat(cache: dict, source: SymbolSource, lib_id: str) -> list:
    lib, _, sym = lib_id.partition(":")
    if not lib or not sym:
        raise SymbolLibError(f"'{lib_id}' is not of the form Library:Symbol")
    return flatten(cache, source, lib, sym)


def _sidecar(flat: list) -> dict:
    return {k: {"x": v["x"], "y": v["y"]} for k, v in pin_offsets(flat).items()}


def sidecar_map(cache: dict, source: SymbolSource, lib_id: str) -> dict:
    """{key: {x, y}} for one lib_id — the sidecar file format (no unit key).
    ``source`` is a directory, a LibraryResolver or a library map
    (symlib.lib_path); ``cache`` is symlib.flatten's {lib: symbols} cache."""
    return _sidecar(_flat(cache, source, lib_id))


def sidecar_path(out_dir: Path, lib_id: str) -> Path:
    """``<out_dir>/<Lib_Symbol>.pins.json``, the name sch_build and
    sch_file_model look up."""
    return Path(out_dir) / f"{lib_id.replace(':', '_')}.pins.json"


def _build_one(cache: dict, source: SymbolSource, out_dir: Path,
               lib_id: str) -> tuple[str | None, int, str]:
    """(written path or None, pin count, failure reason) for one lib_id."""
    try:
        flat = _flat(cache, source, lib_id)
        pins, count = _sidecar(flat), len(pin_numbers(flat))
    except (SymbolLibError, ValueError, IndexError) as e:
        return None, 0, str(e)
    path = sidecar_path(out_dir, lib_id)
    try:
        path.write_text(json.dumps(pins, indent=2), encoding="utf-8")
    except OSError as e:
        return None, 0, f"cannot write {path}: {e}"
    _progress(f"  {lib_id:42s} ok                 keys={len(pins)} pins={count}")
    return str(path), count, ""


def build_all(source: SymbolSource, out_dir: Path, lib_ids: Iterable[str]) -> dict:
    """Write one sidecar per lib_id from ``source`` into ``out_dir`` (which
    must exist). Returns {"ok", "written", "failed", "pins"}."""
    cache: dict = {}
    written, failed, pins = [], [], {}
    for lib_id in lib_ids:
        path, count, reason = _build_one(cache, source, out_dir, lib_id)
        if path is None:
            failed.append({"symbol": lib_id, "reason": reason})
            _progress(f"  {lib_id:42s} FAILED  {reason}")
            continue
        written.append(path)
        pins[lib_id] = count
    return {"ok": not failed, "written": written, "failed": failed, "pins": pins}


# --- --netlist: the netlist's resolver ---------------------------------------------------
def _read_netlist(path: Path) -> dict:
    try:
        design = json.loads(path.read_text(encoding="utf-8-sig"))   # Notepad's BOM too
    except (OSError, ValueError) as e:
        raise libtable.LibTableError(f"cannot read netlist {path}: {e}") from e
    if not isinstance(design, dict):
        raise libtable.LibTableError(f"netlist {path} is not a JSON object")
    return design


def _project_hit(resolver: libtable.LibraryResolver,
                 lib_id: str) -> libtable.ResolvedLib | None:
    """The library declaration (netlist block / project table) of lib_id's
    nickname, None for a stock or unresolvable one."""
    hit = resolver.resolve(libtable.SYMBOL, lib_id.partition(":")[0])
    return hit if hit is not None and hit.source in PROJECT_SOURCES else None


def project_lib_ids(design: Mapping, resolver: libtable.LibraryResolver) -> list[str]:
    """The netlist's lib_ids (component order, once each) whose library is a
    project library; with stock symbols configured, also those resolving
    nowhere (so a mistyped nickname fails loudly instead of being skipped)."""
    stock_known = resolver.stock_dirs[libtable.SYMBOL] is not None
    out: list[str] = []
    for comp in design.get("components") or []:
        lib_id = comp.get("lib_id") if isinstance(comp, Mapping) else None
        if not isinstance(lib_id, str) or ":" not in lib_id or lib_id in out:
            continue
        nick = lib_id.partition(":")[0]
        if _project_hit(resolver, lib_id) is not None or (
                stock_known and resolver.resolve(libtable.SYMBOL, nick) is None):
            out.append(lib_id)
    return out


def _same_dir(a: Path, b: Path) -> bool:
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def _refused(resolver: libtable.LibraryResolver, lib_ids: list[str], out_dir: Path,
             project_pins: Path) -> list[dict]:
    """Project-library lib_ids that would land in the central server/pins (D1)."""
    if not _same_dir(out_dir, CENTRAL_PINS_DIR):
        return []
    refused = []
    for lib_id in lib_ids:
        hit = _project_hit(resolver, lib_id)
        if hit is not None:
            refused.append({"symbol": lib_id, "reason": (
                f"{lib_id} is a project library part ({hit.origin}): its sidecar belongs in "
                f"{project_pins} (D1), never in the plugin's central {CENTRAL_PINS_DIR}")})
    return refused


def run_netlist(netlist: str, symbols: str = "", out: str = "",
                lib_ids: Iterable[str] = ()) -> dict:
    """The --netlist mode as a function: the summary (see the module doc).
    LibTableError (a ValueError) when the netlist or its libraries block
    cannot be read."""
    path = Path(os.path.abspath(netlist))
    design = _read_netlist(path)
    resolver = libtable.LibraryResolver.for_design(path.parent, design, symbols or None, None)
    project_pins = path.parent / PROJECT_PINS_DIR
    out_dir = Path(out) if out else project_pins
    ids = list(lib_ids) or project_lib_ids(design, resolver)
    refused = _refused(resolver, ids, out_dir, project_pins)
    todo = [i for i in ids if i not in {r["symbol"] for r in refused}]
    summary = {"ok": True, "written": [], "failed": [], "pins": {}}
    if todo:
        out_dir.mkdir(parents=True, exist_ok=True)
        summary = build_all(resolver, out_dir, todo)
    failed = summary["failed"] + refused
    extra = {} if ids else {"note": f"no project-library symbols in {path}: nothing to write "
                                    f"(stock parts use the central {CENTRAL_PINS_DIR})"}
    return {**summary, "ok": not failed, "failed": failed, "out_dir": str(out_dir),
            "netlist": str(path), **extra}


# --- command line ------------------------------------------------------------------------
def _parse_options(args: list[str]) -> tuple[dict[str, str], list[str]] | None:
    """({option: value}, lib_ids) of the --netlist form; None = usage error."""
    opts: dict[str, str] = {}
    ids: list[str] = []
    it = iter(args)
    for arg in it:
        if arg in _OPTIONS:
            value = next(it, None)
            if value is None or arg in opts:
                return None
            opts[arg] = value
        elif arg.startswith("--"):
            return None
        else:
            ids.append(arg)
    return (opts, ids) if opts.get("--netlist") else None


def _symbol_source(symbols: str) -> SymbolSource:
    """The legacy <symbols> argument: a library map .json, else a directory.
    (sch_build.symbol_source turns a directory into a netlist's resolver; the
    legacy form has no netlist, so a directory stays a plain directory.)"""
    if symbols.lower().endswith(LIBRARY_MAP_SUFFIX) and not Path(symbols).is_dir():
        return read_library_map(symbols)
    return Path(symbols)


def _main_legacy(args: list[str]) -> dict:
    out_dir = Path(args[1])
    source = _symbol_source(args[0])
    out_dir.mkdir(parents=True, exist_ok=True)
    return {**build_all(source, out_dir, args[2:]), "out_dir": str(out_dir)}


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else list(argv)
    netlist_form = any(a.startswith("--") for a in args)
    parsed = _parse_options(args) if netlist_form else None
    if (netlist_form and parsed is None) or (not netlist_form and len(args) < 3):
        print(__doc__)
        return USAGE_EXIT
    try:
        if parsed is None:
            summary = _main_legacy(args)
        else:
            opts, ids = parsed
            summary = run_netlist(opts["--netlist"], opts.get("--symbols", ""),
                                  opts.get("--out", ""), ids)
    except (SymbolLibError, ValueError, OSError) as e:      # LibTableError is a ValueError
        summary = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    print(json.dumps(summary))
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
