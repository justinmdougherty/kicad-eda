#!/usr/bin/env python3
"""Generate an openable, ERC-checkable .kicad_sch (KiCad 10, format 20250114) from
the pad-level netlist.json.

Approach: embed each used symbol into lib_symbols (flattening `extends` symbols so
inherited pins/graphics are present), place components on a grid, and drop a local
net label at every pin (same-named local labels are electrically connected on a
sheet). Power-source nets get a PWR_FLAG so ERC sees them driven. Unused pins get
no_connect flags.

Symbols come from a source (symlib.lib_path, ROADMAP_DECISIONS D1). On the
command line <symbols> is either the stock symbol-library directory of the KiCad
install -- each nickname then resolves through the netlist's ``libraries`` block,
then the project ``sym-lib-table`` beside the netlist, then
``<symbols>/<nick>.kicad_sym`` -- or a ``.json`` library map
(libtable.LibraryResolver.library_map()). build() takes a directory (the naming
convention alone), a LibraryResolver or a library map dict.

Pin coordinates are derived from the flattened symbol itself (symlib.pin_offsets),
so no sidecar is needed. A ``<Lib_Symbol>.pins.json`` file, taken from the first
sidecar directory that has one, overrides the derived offset per key. A pin that
resolves to NO coordinate is an error: the script prints ``{"ok": false,
"missing_pins": ...}`` and exits 1 instead of silently dropping the label. A
sidecar key the symbol itself does not have (a pad id from another KiCad
version) is honoured but listed in the success result's ``warnings``.

Component flags (ROADMAP_DECISIONS D4; booleans, anything else is a
ValueError): a ``"board_only": true`` part is on the board only -- it gets no
symbol (and needs no lib_id), its pads get no label or no_connect flag, and a
net left with only such pads is not in the schematic; the other pins of its
nets keep their labels. ``"dnp": true`` writes ``(dnp yes)`` and
``"in_bom": false`` writes ``(in_bom no)`` on every placement of the part. The
success result lists the skipped parts under ``board_only`` when there are any.

Usage:
  <venv-python> sch_build.py <netlist.json> <out.kicad_sch> <symbols> [sidecar_dirs]

  <symbols>       stock symbols directory, or a library map ending in .json
  [sidecar_dirs]  directories searched in order, joined by os.pathsep; default
                  <netlist dir>/pins then the plugin's central server/pins
                  ("" = no sidecars)
"""
from __future__ import annotations

import json
import os
import sys
import uuid as uuidlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Union

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import libtable  # noqa: E402
import symlib  # noqa: E402,F401
from symlib import (SYM_ONLY_TAGS, S, SymbolLibError, SymbolSource, dumps,  # noqa: E402,F401
                    embed, first, flatten, is_, kids, lib_path, load_lib, pin_etypes,
                    pin_numbers, pin_offsets, unit_pins)

# public re-exports (lint_netlist / design_report / older callers import these)
__all__ = ["S", "SYM_ONLY_TAGS", "SymbolLibError", "dumps", "embed", "first",
           "flatten", "is_", "kids", "lib_path", "load_lib", "pin_etypes", "pin_numbers",
           "pin_offsets", "unit_pins", "uid", "q", "load_sidecars", "default_sidecar_dirs",
           "read_library_map", "symbol_source", "FLAG_KEYS", "component_flag",
           "is_board_only", "board_only_refs", "schematic_design", "build", "main"]

# every schematic gets the power library's flag, written (lib_id "power:PWR_FLAG")
PWR_FLAG_LIB_ID = f"{libtable.POWER_LIBRARY}:PWR_FLAG"
USAGE_EXIT = 2
CENTRAL_PINS_DIR = Path(_HERE).parent / "pins"     # the plugin's sidecars for stock parts
PROJECT_PINS_DIR = "pins"                           # <netlist dir>/pins: project parts (D1)
LIBRARY_MAP_SUFFIX = ".json"
NUL = "\x00"
# the D4 component flags; each is a boolean when present
FLAG_KEYS = ("board_only", "dnp", "in_bom")

# One grid cell per (component, unit); origins and spacings are multiples of
# 1.27 mm so pins land on KiCad's connection grid. PWR_FLAGs go in a column on
# the right.
GRID_COLS, GRID_DX, GRID_DY, GRID_X0, GRID_Y0 = 5, 63.5, 50.8, 50.8, 38.1
FLAG_X, FLAG_Y0, FLAG_DY = 363.22, 38.1, 15.24
FOOTER = ('\t(sheet_instances', '\t\t(path "/" (page "1"))', '\t)', '\t(embedded_fonts no)', ')')

SidecarDirs = Union[str, "os.PathLike[str]", Iterable, None]


def uid() -> str:
    return str(uuidlib.uuid4())


def q(value: object) -> str:
    r"""``value`` (``str(value)``) as a quoted KiCad S-expression string atom.

    The escapes are exactly the ones sexpdata performs (``String.quote``), the
    quoting the embedded lib_symbols block gets too, so every string in the
    file is written one way:

        backslash -> \\          double quote -> \"
        backspace -> \b          form feed -> \f
        newline -> \n            carriage return -> \r
        tab -> \t

    Every other character, other control characters included, is written as
    is. KiCad's lexer decodes those escapes, but a RAW newline inside a quoted
    atom makes the whole schematic fail to load. NUL is refused with a
    ValueError: KiCad reads 'A\x00B' back as 'A'.

    Every netlist- or library-supplied string (lib_id, reference, value,
    footprint, pin number, project name, net name) goes through here; a bare
    f-string would let a value such as ``1/4"`` end the atom early and corrupt
    the file."""
    text = str(value)
    if NUL in text:
        raise ValueError(f"NUL character (\\x00) in {text[:80]!r}: KiCad would read "
                         f"{text.split(NUL, 1)[0][:80]!r} back (the string ends at the NUL)")
    return dumps(text)


# --- pin sidecars ------------------------------------------------------------------
def _sidecar_dir_list(dirs: SidecarDirs) -> list[Path]:
    """A bare str / PathLike is ONE directory (never split); empty entries are skipped."""
    if dirs is None:
        return []
    entries = [dirs] if isinstance(dirs, (str, os.PathLike)) else list(dirs)
    return [Path(d) for d in entries if os.fspath(d)]


def _read_sidecar(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise ValueError(f"malformed pin sidecar {path}: {e}") from e
    if not isinstance(data, dict):
        raise ValueError(f"malformed pin sidecar {path}: it must be a JSON object of "
                         f"pin -> {{x, y}}, got {type(data).__name__}")
    return data


def load_sidecars(sidecar_dirs: SidecarDirs, lib_ids: Iterable[str]) -> dict:
    """{lib_id: {key: {x, y}}} from ``<dir>/<Lib_Symbol>.pins.json``: the whole
    file of the FIRST directory in ``sidecar_dirs`` that has one (a search
    path, e.g. project pins before the central dir; a bare str is one
    directory). A missing file or directory means "no override" (empty map);
    a malformed first hit is an error."""
    dirs = _sidecar_dir_list(sidecar_dirs)
    out: dict = {}
    for lib_id in lib_ids:
        name = f"{lib_id.replace(':', '_')}.pins.json"
        hit = next((d / name for d in dirs if (d / name).is_file()), None)
        out[lib_id] = _read_sidecar(hit) if hit is not None else {}
    return out


def default_sidecar_dirs(netlist_path: str) -> list[str]:
    """The CLI's sidecar search path when none is given (D1): the project's
    ``<netlist dir>/pins``, then the plugin's central ``server/pins``."""
    project = Path(os.path.abspath(netlist_path)).parent / PROJECT_PINS_DIR
    return [str(project), str(CENTRAL_PINS_DIR)]


# --- symbol sources (the CLI's <symbols> argument) ---------------------------------
def read_library_map(path: str) -> dict:
    """A library map JSON file (LibraryResolver.library_map()) as a dict."""
    p = Path(path)
    try:
        data = json.loads(p.read_text(encoding="utf-8-sig"))
    except OSError as e:
        raise SymbolLibError(f"cannot read library map {p}: {e}") from e
    except ValueError as e:
        raise SymbolLibError(f"library map {p} is not valid JSON: {e}") from e
    if not isinstance(data, dict):
        raise SymbolLibError(f"library map {p} must be a JSON object (LibraryResolver."
                             f"library_map()), got {type(data).__name__}")
    version = data.get("version", libtable.MAP_VERSION)
    if version != libtable.MAP_VERSION:
        raise SymbolLibError(f"library map {p} has version {version!r}; this sch_build reads "
                             f"version {libtable.MAP_VERSION}")
    return data


def symbol_source(netlist_path: str, symbols: str) -> SymbolSource:
    """<symbols> as a source: a ``.json`` file is a library map; otherwise it is
    the stock symbols directory behind the netlist's libraries block and the
    project sym-lib-table (LibraryResolver.for_netlist, D1 order)."""
    if symbols.lower().endswith(LIBRARY_MAP_SUFFIX) and not Path(symbols).is_dir():
        return read_library_map(symbols)
    return libtable.LibraryResolver.for_netlist(netlist_path, symbols, None)


# --- D4 component flags: the design as the schematic sees it -----------------------
def component_flag(comp: Mapping, key: str) -> bool | None:
    """A component's D4 flag ``key`` (None when absent). ValueError unless it
    is a boolean (the board builder refuses the same values)."""
    value = comp.get(key)
    if value is not None and not isinstance(value, bool):
        raise ValueError(f"{comp.get('ref', '?')}: '{key}' must be true or false, "
                         f"got {value!r}")
    return value


def is_board_only(comp: Mapping) -> bool:
    """``"board_only": true``: on the board, not in the schematic (D4)."""
    return component_flag(comp, "board_only") is True


def board_only_refs(design: Mapping) -> list[str]:
    """The references of the board-only components, in netlist order."""
    return [c["ref"] for c in design["components"] if is_board_only(c)]


def schematic_design(design: Mapping) -> dict:
    """``design`` as the schematic sees it (D4). Every component's flags are
    checked (ValueError for a non-boolean); board-only components are dropped
    together with their pads in ``nets`` and ``no_connect`` (a board-only pad
    never gets a label or a no_connect flag), and so is a net whose every pad
    was board-only. A new dict: ``design`` itself is not modified. Without
    board-only parts the result equals ``design``."""
    for comp in design["components"]:
        for key in FLAG_KEYS:
            component_flag(comp, key)
    dropped = set(board_only_refs(design))
    if not dropped:
        return dict(design)
    nets = {}
    for net, conns in design.get("nets", {}).items():
        kept = [[ref, pad] for ref, pad in conns if ref not in dropped]
        if kept or not conns:
            nets[net] = kept
    return {**design,
            "components": [c for c in design["components"] if c["ref"] not in dropped],
            "nets": nets,
            "no_connect": [[ref, pad] for ref, pad in design.get("no_connect", [])
                           if ref not in dropped]}


# --- symbols and the power-flag rule -----------------------------------------------
@dataclass(frozen=True)
class _Symbols:
    """Per lib_id: the pin maps the builder needs; ``blocks`` are the embedded
    lib_symbols lines."""
    blocks: tuple[str, ...]
    etypes: Mapping[str, dict]
    units: Mapping[str, dict]
    pin_unit: Mapping[str, dict]
    offsets: Mapping[str, dict]


def _load_symbols(source: SymbolSource, lib_ids: list[str]) -> _Symbols:
    """Flatten and embed every used symbol, plus PWR_FLAG, from ``source``."""
    cache: dict = {}
    blocks: list[str] = []
    etypes, units, pin_unit, offsets = {}, {}, {}, {}
    for lib_id in lib_ids + [PWR_FLAG_LIB_ID]:
        lib, _, name = lib_id.partition(":")
        flat = flatten(cache, source, lib, name)
        etypes[lib_id] = pin_etypes(flat)
        units[lib_id] = unit_pins(flat)
        pin_unit[lib_id] = {p: u for u, ps in units[lib_id].items() for p in ps}
        offsets[lib_id] = pin_offsets(flat)
        blocks.append("\t\t" + dumps(embed(flat, lib_id)))
    return _Symbols(tuple(blocks), etypes, units, pin_unit, offsets)


def _pin_type(lib_of: Mapping[str, str], syms: _Symbols, ref: str, pad: object) -> str:
    """Electrical type of a pad key that may be a pin NUMBER or a pin NAME
    (the type map is number-keyed; a name resolves through its offset)."""
    lib_id = lib_of.get(ref, "")
    key = str(pad)
    types = syms.etypes.get(lib_id, {})
    if key not in types:
        key = str((syms.offsets.get(lib_id, {}).get(key) or {}).get("number") or key)
    return types.get(key, "")


def _power_flag_nets(design: Mapping, syms: _Symbols) -> set[str]:
    """Nets that need a PWR_FLAG: any net with a power-input pin but no
    power-output pin driving it (exactly KiCad's power_pin_not_driven rule).

    A regulator's *output* rail is driven through passives (the inductor /
    divider), so its feedback pin reads as an undriven input to ERC even though
    it is a real supply. Declaring the rail electrically -- in net_specs (a real
    voltage/current) or an explicit power_flags list -- makes it a power source,
    exactly as dropping a PWR_FLAG on the regulator output does in KiCad. Skip
    any rail that already has an output/power_out driver, or the flag and the
    driver both source the net and ERC reports a conflict."""
    lib_of = {c["ref"]: c["lib_id"] for c in design["components"]}
    nets = design.get("nets", {})
    flagged: set[str] = set()
    driven: set[str] = set()      # nets with any output/power_out source pin
    for net, conns in nets.items():
        types = [_pin_type(lib_of, syms, ref, pad) for ref, pad in conns]
        if any(t in ("power_out", "output") for t in types):
            driven.add(net)
        if "power_in" in types and "power_out" not in types:
            flagged.add(net)
    declared = {n for n, s in design.get("net_specs", {}).items()
                if s.get("voltage", 0) or s.get("current", 0)}
    declared |= set(design.get("power_flags", []))
    return flagged | {n for n in declared if n in nets and n not in driven}


# --- layout and component instances ------------------------------------------------
Cell = tuple[str, int]                     # (ref, unit)


def _layout(design: Mapping, units: Mapping[str, dict]) -> tuple[list[Cell], dict]:
    """Placement cells in order -- one per (component, unit): multi-unit parts
    (op-amps, logic) place each unit separately, all sharing one reference --
    and each cell's grid position."""
    cells = [(c["ref"], u) for c in design["components"] for u in sorted(units[c["lib_id"]])]
    pos = {}
    for i, key in enumerate(cells):
        pos[key] = (round(GRID_X0 + (i % GRID_COLS) * GRID_DX, 2),
                    round(GRID_Y0 + (i // GRID_COLS) * GRID_DY, 2))
    return cells, pos


def _header_lines(root: str, blocks: tuple[str, ...]) -> list[str]:
    return ['(kicad_sch', '\t(version 20250114)', '\t(generator "kicad-eda")',
            '\t(generator_version "10.0")', f'\t(uuid "{root}")', '\t(paper "A4")',
            '\t(lib_symbols', *blocks, '\t)']


def _attribute_lines(c: Mapping) -> list[str]:
    """The placement's attribute lines: ``in_bom: false`` -> (in_bom no),
    ``dnp: true`` -> (dnp yes); absent flags keep the defaults."""
    in_bom = "no" if component_flag(c, "in_bom") is False else "yes"
    dnp = "yes" if component_flag(c, "dnp") is True else "no"
    return ['\t\t(exclude_from_sim no)', f'\t\t(in_bom {in_bom})', '\t\t(on_board yes)',
            f'\t\t(dnp {dnp})']


def _instance_lines(c: Mapping, unit: int, xy: tuple[float, float], pins: list,
                    project: str, root: str) -> list[str]:
    """One placement symbol: component ``c``'s ``unit`` at ``xy``."""
    x, y = xy
    head = [
        '\t(symbol',
        f'\t\t(lib_id {q(c["lib_id"])})',
        f'\t\t(at {x} {y} 0)',
        f'\t\t(unit {unit})',
        *_attribute_lines(c),
        f'\t\t(uuid "{uid()}")',
        f'\t\t(property "Reference" {q(c["ref"])} (at {x} {y - 7.62} 0)'
        ' (effects (font (size 1.27 1.27))))',
        f'\t\t(property "Value" {q(c.get("value", ""))} (at {x} {y + 7.62} 0)'
        ' (effects (font (size 1.27 1.27))))',
        f'\t\t(property "Footprint" {q(c.get("footprint", ""))} (at {x} {y} 0)'
        ' (effects (font (size 1.27 1.27)) (hide yes)))',
    ]
    pin_lines = [f'\t\t(pin {q(num)} (uuid "{uid()}"))' for num in pins]
    tail = [f'\t\t(instances (project {q(project)} (path "/{root}"'
            f' (reference {q(c["ref"])}) (unit {unit}))))',
            '\t)']
    return head + pin_lines + tail


def _component_lines(design: Mapping, cells: list[Cell], pos: dict,
                     units: Mapping[str, dict], root: str) -> list[str]:
    by_ref = {c["ref"]: c for c in design["components"]}
    project = design.get("name", "design")
    lines: list[str] = []
    for ref, u in cells:
        c = by_ref[ref]
        lines += _instance_lines(c, u, pos[(ref, u)], units[c["lib_id"]].get(u, []),
                                 project, root)
    return lines


# --- labels and no_connect flags at the pins ---------------------------------------
@dataclass(frozen=True)
class _PinMap:
    """Where a (ref, pad) lands: its own unit's cell plus the sidecar override
    or the symbol-derived offset."""
    by_ref: Mapping[str, Mapping]
    syms: _Symbols
    sidecar: Mapping[str, Mapping]
    pos: Mapping[Cell, tuple[float, float]]

    def locate(self, ref: str, pad: object) -> tuple[tuple[float, float] | None, str]:
        """((x, y) or None, the lib_id when only its sidecar knows the key)."""
        key = str(pad)
        comp = self.by_ref.get(ref)
        if comp is None:
            return None, ""
        lib_id = comp["lib_id"]
        derived = self.syms.offsets[lib_id].get(key)
        override = self.sidecar[lib_id].get(key)
        off = override or derived
        # The unit comes from the pin NUMBER map when the key is a number; a pin
        # NAME (e.g. "V+" on a dual op-amp) carries its unit in the derived
        # offset. Defaulting to unit 1 would drop the label in the wrong cell.
        u = self.syms.pin_unit[lib_id].get(key) or (derived or {}).get("unit", 1)
        cell = self.pos.get((ref, u))
        if cell is None or off is None:
            return None, ""
        xy = (round(cell[0] + off["x"], 2), round(cell[1] + off["y"], 2))
        return xy, (lib_id if override and derived is None else "")


def _pin_lines(design: Mapping, pins: _PinMap) -> tuple[list[str], list[list[str]], list[str]]:
    """(label + no_connect lines, pins without a coordinate, warnings)."""
    lines: list[str] = []
    missing: list[list[str]] = []
    notes: dict[tuple[str, str], str] = {}        # one warning per (lib_id, key)

    def place(ref: str, pad: object) -> tuple[float, float] | None:
        xy, sidecar_only = pins.locate(ref, pad)
        if xy is None:
            missing.append([str(ref), str(pad)])
        elif sidecar_only:
            notes.setdefault((sidecar_only, str(pad)), (
                f"{ref}.{pad}: not a pin of {sidecar_only} in this symbol library; sidecar "
                f"override used (the board stage will report the pad missing if the "
                f"footprint lacks it too)"))
        return xy

    for net, conns in design.get("nets", {}).items():
        for ref, pad in conns:
            xy = place(ref, pad)
            if xy is not None:
                lines.append(f'\t(label {q(net)} (at {xy[0]} {xy[1]} 0)'
                             ' (effects (font (size 1.27 1.27)) (justify left bottom))'
                             f' (uuid "{uid()}"))')
    for ref, pad in design.get("no_connect", []):
        xy = place(ref, pad)
        if xy is not None:
            lines.append(f'\t(no_connect (at {xy[0]} {xy[1]}) (uuid "{uid()}"))')
    return lines, missing, list(notes.values())


def _flag_lines(design: Mapping, flag_nets: set[str], root: str) -> list[str]:
    """A PWR_FLAG symbol plus a label per undriven power net, top to bottom."""
    project = design.get("name", "design")
    nets = design.get("nets", {})
    fx, fy = FLAG_X, FLAG_Y0
    lines: list[str] = []
    for n, net in enumerate(sorted(n for n in flag_nets if n in nets), start=1):
        ref = f"#FLG{n:02d}"
        lines += [
            '\t(symbol',
            f'\t\t(lib_id "{PWR_FLAG_LIB_ID}")',
            f'\t\t(at {fx} {fy} 0)', '\t\t(unit 1)',
            '\t\t(exclude_from_sim no)', '\t\t(in_bom yes)', '\t\t(on_board yes)', '\t\t(dnp no)',
            f'\t\t(uuid "{uid()}")',
            f'\t\t(property "Reference" "{ref}" (at {fx} {fy-2.54} 0)'
            ' (effects (font (size 1.27 1.27)) (hide yes)))',
            f'\t\t(property "Value" "PWR_FLAG" (at {fx} {fy+2.54} 0)'
            ' (effects (font (size 1.27 1.27))))',
            f'\t\t(pin "1" (uuid "{uid()}"))',
            f'\t\t(instances (project {q(project)} (path "/{root}" (reference "{ref}")'
            ' (unit 1))))',
            '\t)',
            f'\t(label {q(net)} (at {fx} {fy} 0) (effects (font (size 1.27 1.27)))'
            f' (uuid "{uid()}"))',
        ]
        fy += FLAG_DY
    return lines


# --- the build -----------------------------------------------------------------------
def build(netlist_path: str, out_path: str, symbols: SymbolSource,
          sidecars: SidecarDirs = ()) -> dict:
    """Write ``out_path`` from ``netlist_path``. ``symbols`` is a directory, a
    LibraryResolver or a library map (symlib.lib_path); ``sidecars`` is a list
    of sidecar directories searched in order (a bare str is one directory).
    Returns {"ok": True, "sch", "components", "nets", "warnings"} (counts of
    what the schematic holds; plus "board_only": [refs] when parts were left
    to the board), or {"ok": False, "error", "missing_pins"} without writing
    anything; an unresolvable library raises SymbolLibError, a non-boolean D4
    flag ValueError."""
    with open(netlist_path, encoding="utf-8") as f:
        netlist = json.load(f)
    design = schematic_design(netlist)
    root = uid()
    lib_ids = list(dict.fromkeys(c["lib_id"] for c in design["components"]))
    syms = _load_symbols(symbols, lib_ids)
    flag_nets = _power_flag_nets(design, syms)
    sidecar = load_sidecars(sidecars, lib_ids)    # overrides the derived offsets per key
    cells, pos = _layout(design, syms.units)
    lines = _header_lines(root, syms.blocks) + _component_lines(design, cells, pos, syms.units,
                                                                root)
    pins = _PinMap({c["ref"]: c for c in design["components"]}, syms, sidecar, pos)
    pin_lines, missing, warnings = _pin_lines(design, pins)
    # a label that silently vanished would leave a pin electrically open in an
    # ERC-"clean" schematic -- refuse to write the file instead.
    if missing:
        return {"ok": False, "error": f"{len(missing)} pins have no coordinates",
                "missing_pins": missing}
    lines += pin_lines + _flag_lines(design, flag_nets, root) + list(FOOTER)
    Path(out_path).write_text("\n".join(lines) + "\n", encoding="utf-8")
    board_only = board_only_refs(netlist)
    return {"ok": True, "sch": out_path,
            "components": len(design["components"]),
            "nets": len(design.get("nets", {})),
            "warnings": warnings,
            **({"board_only": board_only} if board_only else {})}


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else list(argv)
    if len(args) < 3:
        print(__doc__)
        return USAGE_EXIT
    netlist_path, out_path, symbols = args[:3]
    sidecars = (default_sidecar_dirs(netlist_path) if len(args) < 4
                else [d for d in args[3].split(os.pathsep) if d])
    try:
        res = build(netlist_path, out_path, symbol_source(netlist_path, symbols), sidecars)
    except (SymbolLibError, ValueError, KeyError, OSError) as e:
        res = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    print(json.dumps(res))
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
