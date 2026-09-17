#!/usr/bin/env python3
"""kicad-generator — MCP server for the kicad-eda plugin.

Exposes the tool surface the `pcb-design` skill calls by name. Four layers:

  SCHEMATIC (file-based, no KiCad GUI needed)
      create_schematic / add_component(s) / add_wire / add_label / add_power_port /
      wire_by_ref / label_pin / power_pin / save_schematic build an in-memory model
      and write a .kicad_sch through the SAME emitter format as the headless
      pipeline (server/tools/sch_build.py): file version 20250114, every used
      library symbol flattened and embedded in lib_symbols, per-instance pin
      lists, instances / sheet_instances / embedded_fonts. Pin coordinates for
      the *_pin tools come from a pins/ sidecar when one exists, otherwise they
      are derived from the library symbol itself. run_erc / render_sch shell out
      to kicad-cli.

  PCB (KiCad IPC API via kicad-python / `kipy`)
      Live edits of the board OPEN in Pcbnew: move/remove footprints, traces,
      vias, zones, placement checks. KiCad 9/10 cannot instantiate a footprint
      from a library over IPC (only Update PCB from Schematic / F8 does), so
      add_footprint_to_pcb only positions footprints that already exist. When
      the API is unreachable every PCB tool returns {"ipc_connected": false}
      and a PreToolUse hook blocks the call before it reaches the server.
      test_kicad_connection reports version, open documents and the
      version-derived capabilities; server_status reports the whole toolchain.

  HEADLESS PIPELINE (netlist.json -> ERC-clean schematic -> placed board ->
      Freerouting -> DRC -> fab zip). build_schematic_from_netlist,
      build_board_from_netlist, autoroute_board, add_ground_stitching,
      check_design_gates, export_fab_package, drc_pcb, create_pcb shell out to
      server/tools/*.py — sch_build under this venv (sexpdata), the SWIG board
      steps under KiCad's bundled Python (pcbnew), Freerouting under Java.

  OPEN-DOCUMENT GUARD
      Every tool that WRITES a .kicad_sch/.kicad_pcb first asks a running KiCad
      (over IPC) whether that file is open in an editor and refuses to clobber
      it. When IPC is unavailable the guard cannot know and never blocks.

Forward path: KiCad 11 adds a schematic API, footprint placement from a
library and netlist import over IPC. `capabilities` in test_kicad_connection
turns those on by version so the prompt layer can pick the IPC route when it
exists and the file/pipeline route otherwise.
"""

from __future__ import annotations

import json
import os
import sys
import uuid
import glob
import shutil
import tempfile
import zipfile
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("kicad-generator")

# ----------------------------------------------------------------------------
# State
# ----------------------------------------------------------------------------
SCH: dict[str, dict[str, Any]] = {}          # path -> schematic model
FAB_PROFILE = os.environ.get("FAB_PROFILE", "JLCPCB")
WORK_UNITS = os.environ.get("WORK_UNITS", "mm")
SERVER_DIR = Path(__file__).parent
TOOLS_DIR = SERVER_DIR / "tools"
PINS_DIR = SERVER_DIR / "pins"

# schematic file format written by the emitter (KiCad 9/10 stable format)
SCH_FORMAT_VERSION = 20250114
SCH_GENERATOR = "kicad-eda"
SCH_GENERATOR_VERSION = "10.0"
KICAD11_NIGHTLY = (10, 99)      # a KiCad 11 nightly reports itself as 10.99.x
PCBNEW_PROBE_TIMEOUT_S = 30
KICAD_CLI_TIMEOUT_S = 600


def _find_kicad_cli() -> str | None:
    """Locate kicad-cli: PATH first, then $KICAD_PATH (root or bin), then common
    install locations across versions/platforms. KiCad's per-user winget install on
    Windows does not add itself to PATH, so PATH alone is not enough."""
    exe = "kicad-cli.exe" if os.name == "nt" else "kicad-cli"
    found = shutil.which("kicad-cli")
    if found:
        return found
    roots: list[Path] = []
    kp = os.environ.get("KICAD_PATH")
    if kp:
        roots += [Path(kp), Path(kp) / "bin"]
    if os.name == "nt":
        bases = [Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "KiCad",
                 Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "KiCad"]
        for base in bases:
            if base.is_dir():
                # newest version subdir first (e.g. 10.0 before 9.0)
                for ver in sorted((d for d in base.iterdir() if d.is_dir()),
                                  reverse=True):
                    roots.append(ver / "bin")
    else:
        roots += [Path("/usr/bin"), Path("/usr/local/bin"),
                  Path("/Applications/KiCad/KiCad.app/Contents/MacOS")]
    for r in roots:
        cand = r / exe
        if cand.is_file():
            return str(cand)
    return None


KICAD_CLI = _find_kicad_cli()


def _kicad_share() -> Path | None:
    """KiCad's shared data dir (.../share/kicad) holding symbols/ and footprints/."""
    if KICAD_CLI:
        share = Path(KICAD_CLI).parent.parent / "share" / "kicad"
        if share.is_dir():
            return share
    return None


def _symbols_dir(explicit: str = "") -> str | None:
    """The .kicad_sym library dir: an explicit path (must exist) or KiCad's stock one."""
    if explicit:
        if not Path(explicit).is_dir():
            raise ValueError(f"symbols_dir does not exist: {explicit}")
        return explicit
    share = _kicad_share()
    if share and (share / "symbols").is_dir():
        return str(share / "symbols")
    return None


def _version_tuple(ver: str) -> tuple[int, ...]:
    """'9.0.4' -> (9, 0, 4); '10.99.0-rc1-3-gabc' -> (10, 99, 0); junk -> ()."""
    head = ver.strip().split()[0] if ver.strip() else ""
    nums: list[int] = []
    for part in head.split("-")[0].split("."):
        digits = "".join(ch for ch in part if ch.isdigit())
        if not digits or digits != part:
            if digits:
                nums.append(int(digits))
            break
        nums.append(int(digits))
    return tuple(nums)


def _capabilities(ver: tuple[int, ...]) -> dict[str, bool]:
    """Version-derived IPC capabilities: KiCad 11+ (or a 10.99 nightly) gains the
    schematic API, footprint placement from a library and netlist import."""
    is11 = bool(ver) and (ver[0] >= 11 or ver[:2] == KICAD11_NIGHTLY)
    return {"schematic_api": is11,
            "place_footprint_from_library": is11,
            "import_netlist": is11}


# ----------------------------------------------------------------------------
# Symbol libraries (shared with the pipeline: server/tools/sch_build.py)
# ----------------------------------------------------------------------------
_SYM_CACHE: dict[str, dict] = {}            # symbols_dir -> sch_build lib cache


def _sch_build():
    """Import server/tools/sch_build.py — the pipeline's symbol flattener / emitter
    helpers (flatten, embed, unit_pins, dumps, ...). Needs sexpdata in this venv."""
    if str(TOOLS_DIR) not in sys.path:
        sys.path.insert(0, str(TOOLS_DIR))
    try:
        import sch_build  # noqa: E402
    except ImportError as e:
        raise ImportError(f"cannot import {TOOLS_DIR / 'sch_build.py'} ({e}); "
                          "is sexpdata installed in the server venv?") from e
    return sch_build


def _flat_symbol(lib_id: str, symbols_dir: str) -> list:
    """Flattened (extends-resolved) symbol sexp for a Library:Symbol id.
    Raises ValueError when the library or the symbol cannot be found."""
    lib, sep, name = lib_id.partition(":")
    if not sep or not lib or not name:
        raise ValueError(f"lib_id '{lib_id}' must be 'Library:Symbol' (e.g. Device:R)")
    sb = _sch_build()
    cache = _SYM_CACHE.setdefault(symbols_dir, {})
    try:
        return sb.flatten(cache, symbols_dir, lib, name)
    except sb.SymbolLibError as e:  # library missing / unparsable / symbol absent
        raise ValueError(str(e)) from e
    except (FileNotFoundError, KeyError) as e:
        raise ValueError(f"symbol {lib_id} not found under {symbols_dir}: {e}") from e


def _pin_offsets_of(flat: list) -> dict[str, dict[str, float]]:
    """{pin number | pin name: {x, y}} schematic-space offsets (mm, y negated —
    symbol files are +Y up, a placed symbol is +Y down) for rotation 0: the
    pipeline's own symlib.pin_offsets (re-exported by sch_build), i.e. the same
    math build_pin_sidecars.py uses, so sidecar and derived maps agree."""
    sb = _sch_build()
    return {k: {"x": float(v["x"]), "y": float(v["y"])}
            for k, v in sb.pin_offsets(flat).items()}


def _new_sch(path: str) -> dict[str, Any]:
    return {"uuid": str(uuid.uuid4()), "paper": "A4", "project": Path(path).stem,
            "symbols": [], "wires": [], "labels": [], "power": []}


def _pins_sidecar(lib_id: str, near: str) -> dict | None:
    """Load a pin map mapping pin name/number -> {x, y} schematic-space offsets (mm).

    Searched in order: a `pins/` dir next to the schematic, then the server's
    central `pins/` dir (so sidecars built once are reused across projects).
    A sidecar, when present, overrides the symbol-derived offsets."""
    safe = lib_id.replace(":", "_")
    candidates = [Path(near).parent / "pins" / f"{safe}.pins.json",
                  PINS_DIR / f"{safe}.pins.json"]
    for cand in candidates:
        if cand.is_file():
            try:
                return json.loads(cand.read_text(encoding="utf-8"))
            except (OSError, ValueError) as e:
                raise ValueError(f"malformed pin sidecar {cand}: {e}") from e
    return None


def _pin_map(lib_id: str, near: str) -> dict:
    """Pin offsets for a lib_id: the sidecar if one exists, else derived from the
    KiCad symbol library. Raises ValueError when neither source is available."""
    side = _pins_sidecar(lib_id, near)
    if side is not None:
        return side
    syms = _symbols_dir()
    if not syms:
        raise ValueError(
            f"Pin coordinates for {lib_id} are unknown: no sidecar at "
            f"pins/{lib_id.replace(':', '_')}.pins.json and the KiCad symbol "
            f"libraries were not found (install KiCad or set KICAD_PATH), or use "
            f"the explicit-coordinate tools (add_wire/add_label/add_power_port).")
    return _pin_offsets_of(_flat_symbol(lib_id, syms))


def _rotate_offset(dx: float, dy: float, rot: int) -> tuple[float, float]:
    """Rotate a rotation-0 schematic-space pin offset by the symbol's (at ... rot).
    KiCad rotates counter-clockwise on screen; the sheet is +Y down."""
    r = int(rot) % 360
    if r == 0:
        return dx, dy
    if r == 90:
        return dy, -dx
    if r == 180:
        return -dx, -dy
    if r == 270:
        return -dy, dx
    raise ValueError(f"rotation must be one of 0/90/180/270, got {rot}")


# ----------------------------------------------------------------------------
# S-expression emission — the same file format as server/tools/sch_build.py
# ----------------------------------------------------------------------------
def _q(s: Any) -> str:
    """Quote a string atom for a KiCad S-expression."""
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _lib_symbols(model: dict[str, Any], symbols_dir: str | None
                 ) -> tuple[list[str], dict[str, dict[int, list[str]]], list[str]]:
    """Flatten + embed every distinct lib_id in the model.
    Returns (lib_symbols lines, {lib_id: {unit: [pin numbers]}}, unresolved lib_ids)."""
    lib_ids = list(dict.fromkeys(s["lib_id"] for s in model["symbols"]))
    if not symbols_dir or not lib_ids:
        return [], {}, []
    sb = _sch_build()
    blocks: list[str] = []
    units: dict[str, dict[int, list[str]]] = {}
    unresolved: list[str] = []
    for lib_id in lib_ids:
        try:
            flat = _flat_symbol(lib_id, symbols_dir)
        except ValueError:
            unresolved.append(lib_id)
            continue
        blocks.append("\t\t" + sb.dumps(sb.embed(flat, lib_id)))
        units[lib_id] = sb.unit_pins(flat)
    return blocks, units, unresolved


def _symbol_lines(s: dict[str, Any], unit_pins: dict[int, list[str]],
                  root: str, project: str) -> list[str]:
    x, y, rot, unit = float(s["x"]), float(s["y"]), int(s.get("rot", 0)), int(s.get("unit", 1))
    font = "(effects (font (size 1.27 1.27)))"
    lines = [
        "\t(symbol",
        f"\t\t(lib_id {_q(s['lib_id'])})",
        f"\t\t(at {x:.4f} {y:.4f} {rot})",
        f"\t\t(unit {unit})",
        "\t\t(exclude_from_sim no)", "\t\t(in_bom yes)", "\t\t(on_board yes)", "\t\t(dnp no)",
        f"\t\t(uuid \"{s['uuid']}\")",
        f"\t\t(property \"Reference\" {_q(s['ref'])} (at {x:.4f} {y - 7.62:.4f} 0) {font})",
        f"\t\t(property \"Value\" {_q(s.get('value', ''))} (at {x:.4f} {y + 7.62:.4f} 0) {font})",
        f"\t\t(property \"Footprint\" {_q(s.get('footprint', ''))} (at {x:.4f} {y:.4f} 0) "
        "(effects (font (size 1.27 1.27)) (hide yes)))",
    ]
    lines += [f"\t\t(pin {_q(num)} (uuid \"{uuid.uuid4()}\"))"
              for num in unit_pins.get(unit, [])]
    lines += [f"\t\t(instances (project {_q(project)} (path \"/{root}\" "
              f"(reference {_q(s['ref'])}) (unit {unit}))))", "\t)"]
    return lines


def _emit(model: dict[str, Any], symbols_dir: str | None) -> tuple[str, list[str]]:
    """Render the model as a KiCad 9/10-format .kicad_sch (identical structure to
    sch_build.py's output). Returns (text, lib_ids that could not be embedded)."""
    blocks, units, unresolved = _lib_symbols(model, symbols_dir)
    root, project = model["uuid"], model.get("project", "design")
    L = ["(kicad_sch", f"\t(version {SCH_FORMAT_VERSION})",
         f"\t(generator {_q(SCH_GENERATOR)})",
         f"\t(generator_version {_q(SCH_GENERATOR_VERSION)})",
         f"\t(uuid \"{root}\")", f"\t(paper {_q(model['paper'])})",
         "\t(lib_symbols", *blocks, "\t)"]
    for s in model["symbols"]:
        L += _symbol_lines(s, units.get(s["lib_id"], {}), root, project)
    for w in model["wires"]:
        (x1, y1), (x2, y2) = w["a"], w["b"]
        L.append(f"\t(wire (pts (xy {x1:.4f} {y1:.4f}) (xy {x2:.4f} {y2:.4f})) "
                 f"(stroke (width 0) (type default)) (uuid \"{uuid.uuid4()}\"))")
    for lb in model["labels"]:
        L.append(f"\t(label {_q(lb['net'])} (at {lb['x']:.4f} {lb['y']:.4f} {int(lb.get('rot', 0))}) "
                 f"(effects (font (size 1.27 1.27)) (justify left bottom)) (uuid \"{uuid.uuid4()}\"))")
    for p in model["power"]:
        L.append(f"\t(global_label {_q(p['net'])} (shape input) (at {p['x']:.4f} {p['y']:.4f} 0) "
                 f"(effects (font (size 1.27 1.27))) (uuid \"{uuid.uuid4()}\"))")
    L += ["\t(sheet_instances", "\t\t(path \"/\" (page \"1\"))", "\t)",
          "\t(embedded_fonts no)", ")"]
    return "\n".join(L) + "\n", unresolved


def _require(path: str) -> dict[str, Any]:
    if path not in SCH:
        raise ValueError(f"No schematic loaded for '{path}'. Call create_schematic or load_schematic first.")
    return SCH[path]


# ----------------------------------------------------------------------------
# SCHEMATIC TOOLS
# ----------------------------------------------------------------------------
@mcp.tool()
def create_schematic(path: str, paper: str = "A4") -> dict:
    """Create a new, empty schematic in memory for the given .kicad_sch path.
    Nothing touches disk until save_schematic."""
    if not path.endswith(".kicad_sch"):
        raise ValueError(f"path must end in .kicad_sch, got '{path}'")
    m = {**_new_sch(path), "paper": paper}
    SCH[path] = m
    return {"ok": True, "path": path, "uuid": m["uuid"], "project": m["project"]}


@mcp.tool()
def load_schematic(path: str) -> dict:
    """Register an existing .kicad_sch path for editing. NOTE: the file is NOT
    parsed — the in-memory model starts empty and save_schematic will REPLACE the
    file with the model. Use it to rebuild a schematic in place, not to append."""
    if not Path(path).is_file():
        raise ValueError(f"File not found: {path}")
    SCH[path] = _new_sch(path)
    return {"ok": True, "path": path,
            "note": "registered with an EMPTY model; the existing file is not parsed "
                    "and will be overwritten by save_schematic"}


def _save_result(m: dict[str, Any], sch: str, symbols_dir: str | None,
                 unresolved: list[str]) -> dict:
    res = {"ok": True, "path": sch, "symbols": len(m["symbols"]),
           "wires": len(m["wires"]), "labels": len(m["labels"]),
           "power_ports": len(m["power"])}
    if not symbols_dir:
        return {**res, "warning": "lib_symbols empty: KiCad symbol libraries not found; "
                                  "KiCad will rebuild from its configured libraries on open"}
    if unresolved:
        return {**res, "ok": False, "written": True,
                "unresolved_symbols": unresolved,
                "reason": f"file written but lib_symbols is incomplete: {unresolved} not "
                          f"found under {symbols_dir}. Fix the lib_id(s) (Library:Symbol, "
                          f"e.g. Device:R) and call save_schematic again."}
    return res


@mcp.tool()
def save_schematic(sch: str, symbols_dir: str = "") -> dict:
    """Write the in-memory schematic to disk as a .kicad_sch (KiCad 9/10 format,
    library symbols embedded from KiCad's stock libraries or `symbols_dir`).
    Refuses to overwrite a file that is open in a running KiCad editor."""
    m = _require(sch)
    guard = _write_guard(sch)
    if guard:
        return guard
    syms = _symbols_dir(symbols_dir)
    text, unresolved = _emit(m, syms)
    Path(sch).parent.mkdir(parents=True, exist_ok=True)
    Path(sch).write_text(text, encoding="utf-8")
    return _save_result(m, sch, syms, unresolved)


def _symbol_entry(c: dict[str, Any]) -> dict[str, Any]:
    """Validate one component dict and build the model entry."""
    for key in ("ref", "lib_id", "x", "y"):
        if key not in c:
            raise ValueError(f"component is missing '{key}': {c}")
    lib_id = str(c["lib_id"])
    if lib_id.count(":") != 1 or not all(lib_id.split(":")):
        raise ValueError(f"lib_id '{lib_id}' must be 'Library:Symbol' (e.g. Device:R)")
    rot = int(c.get("rot", 0))
    if rot % 90 or not 0 <= rot < 360:
        raise ValueError(f"rot must be one of 0/90/180/270 for {c['ref']}, got {rot}")
    return {"ref": str(c["ref"]), "lib_id": lib_id, "value": str(c.get("value", "")),
            "x": float(c["x"]), "y": float(c["y"]), "rot": rot,
            "unit": int(c.get("unit", 1)), "footprint": str(c.get("footprint", "")),
            "uuid": str(uuid.uuid4())}


@mcp.tool()
def add_component(sch: str, ref: str, lib_id: str, value: str,
                  x: float, y: float, rot: int = 0, footprint: str = "",
                  unit: int = 1) -> dict:
    """Place one component. Coordinates in mm. rot in {0,90,180,270}. `unit` for
    multi-unit parts (op-amps, logic); defaults to 1."""
    m = _require(sch)
    sym = _symbol_entry({"ref": ref, "lib_id": lib_id, "value": value, "x": x, "y": y,
                         "rot": rot, "footprint": footprint, "unit": unit})
    m["symbols"].append(sym)
    return {"ok": True, "ref": ref, "uuid": sym["uuid"]}


@mcp.tool()
def add_components_batch(sch: str, components: list[dict]) -> dict:
    """Place many components in one call. Each dict: ref, lib_id, value, x, y,
    [rot], [footprint], [unit]. Prefer this over repeated add_component calls.
    Validates every entry BEFORE adding any, so a bad entry adds nothing."""
    m = _require(sch)
    syms = [_symbol_entry(c) for c in components]
    m["symbols"].extend(syms)
    placed = [s["ref"] for s in syms]
    return {"ok": True, "placed": placed, "count": len(placed)}


@mcp.tool()
def add_wire(sch: str, x1: float, y1: float, x2: float, y2: float) -> dict:
    """Draw a wire between two explicit points (mm)."""
    m = _require(sch)
    m["wires"].append({"a": (x1, y1), "b": (x2, y2)})
    return {"ok": True}


@mcp.tool()
def add_label(sch: str, net: str, x: float, y: float, rot: int = 0) -> dict:
    """Place a net label at an explicit point (mm)."""
    m = _require(sch)
    m["labels"].append({"net": net, "x": x, "y": y, "rot": rot})
    return {"ok": True, "net": net}


@mcp.tool()
def add_power_port(sch: str, net: str, x: float, y: float) -> dict:
    """Place a power/global-label port (e.g. +3V3, GND) at an explicit point (mm)."""
    m = _require(sch)
    m["power"].append({"net": net, "x": x, "y": y})
    return {"ok": True, "net": net}


def _pin_xy(sch: str, ref: str, pin: str) -> tuple[float, float]:
    """Absolute schematic coordinate (mm) of a component pin, by pin number or
    name: sidecar offsets when present, else derived from the library symbol,
    rotated by the symbol's placement rotation."""
    m = _require(sch)
    sym = next((s for s in m["symbols"] if s["ref"] == ref), None)
    if not sym:
        raise ValueError(f"No component {ref} in {sch}.")
    pins = _pin_map(sym["lib_id"], sch)
    if pin not in pins:
        raise ValueError(
            f"{sym['lib_id']} has no pin '{pin}' (known: {sorted(pins)[:40]}). "
            f"Use the pin number or name as printed in the KiCad symbol.")
    off = pins[pin]
    dx, dy = _rotate_offset(float(off["x"]), float(off["y"]), sym.get("rot", 0))
    return round(sym["x"] + dx, 4), round(sym["y"] + dy, 4)


@mcp.tool()
def wire_by_ref(sch: str, ref_a: str, pin_a: str, ref_b: str, pin_b: str) -> dict:
    """Connect two component pins by reference, resolving coordinates from the pin
    sidecar or, failing that, the KiCad symbol library."""
    ax, ay = _pin_xy(sch, ref_a, pin_a)
    bx, by = _pin_xy(sch, ref_b, pin_b)
    _require(sch)["wires"].append({"a": (ax, ay), "b": (bx, by)})
    return {"ok": True, "from": f"{ref_a}.{pin_a}", "to": f"{ref_b}.{pin_b}"}


@mcp.tool()
def label_pin(sch: str, ref: str, pin: str, net_name: str) -> dict:
    """Place a net label at a component pin, resolving its coordinate from the pin
    sidecar or, failing that, the KiCad symbol library."""
    x, y = _pin_xy(sch, ref, pin)
    _require(sch)["labels"].append({"net": net_name, "x": x, "y": y, "rot": 0})
    return {"ok": True, "at": f"{ref}.{pin}", "net": net_name}


@mcp.tool()
def power_pin(sch: str, ref: str, pin: str, net_name: str) -> dict:
    """Place a power port at a component power pin, resolving its coordinate from
    the pin sidecar or, failing that, the KiCad symbol library."""
    x, y = _pin_xy(sch, ref, pin)
    _require(sch)["power"].append({"net": net_name, "x": x, "y": y})
    return {"ok": True, "at": f"{ref}.{pin}", "net": net_name}


@mcp.tool()
def list_symbols(sch: str) -> dict:
    """List all components currently in the schematic."""
    m = _require(sch)
    return {"symbols": [{"ref": s["ref"], "lib_id": s["lib_id"], "value": s["value"],
                         "at": [s["x"], s["y"], s["rot"]]} for s in m["symbols"]]}


@mcp.tool()
def list_nets(sch: str) -> dict:
    """List net names referenced by labels and power ports."""
    m = _require(sch)
    nets = sorted({lb["net"] for lb in m["labels"]} | {p["net"] for p in m["power"]})
    return {"nets": nets, "count": len(nets)}


# ERC severities that never count as violations: the user excluded them in the
# editor, or the rule is set to "ignore" in the project's ERC settings.
_ERC_SUPPRESSED = frozenset({"exclusion", "ignore"})


def _erc_severity_counts(report: dict) -> dict[str, int]:
    """{severity: count} over every sheet of a kicad-cli ERC JSON report."""
    return dict(Counter(str(v.get("severity", "unknown"))
                        for s in report.get("sheets", [])
                        for v in s.get("violations", [])))


def _erc_json(sch: str, rpt: str, severity_error: bool = True) -> dict:
    """Run kicad-cli ERC with a JSON report and count violations across sheets.

    kicad-cli's --severity-error filters its console summary ("Found N
    violations") but NOT the JSON report — KiCad 9.0.4 still writes every
    warning into it — so the count is taken from each violation's own
    `severity`: errors only when severity_error, else every non-suppressed
    severity. erc_ok is False (with erc_error) whenever the report cannot be
    produced or parsed."""
    args = [KICAD_CLI, "sch", "erc", "--format", "json", "-o", rpt]
    if severity_error:
        args.append("--severity-error")
    e = _run(args + [sch], timeout=KICAD_CLI_TIMEOUT_S)
    try:
        report = json.loads(Path(rpt).read_text(encoding="utf-8"))
    except (OSError, ValueError) as ex:
        return {"erc_ok": False, "erc_report": rpt,
                "erc_error": f"ERC report unreadable ({ex}): {(e.stdout + e.stderr)[-600:]}"}
    by_sev = _erc_severity_counts(report)
    if severity_error:
        n = by_sev.get("error", 0)
    else:
        n = sum(c for sev, c in by_sev.items() if sev not in _ERC_SUPPRESSED)
    return {"erc_ok": n == 0, "erc_violations": n, "erc_by_severity": by_sev,
            "erc_report": rpt, "report": report}


@mcp.tool()
def run_erc(sch: str, errors_only: bool = False) -> dict:
    """Run KiCad's Electrical Rules Check via kicad-cli and return the parsed JSON
    report plus a violation count: errors AND warnings by default, errors only
    with errors_only (the count is filtered by each violation's severity, since
    kicad-cli writes every severity into the JSON report regardless of its
    --severity flags). by_severity holds the full histogram either way."""
    if not KICAD_CLI:
        return {"ok": False, "reason": "kicad-cli not found on PATH; install KiCad 9."}
    if not Path(sch).is_file():
        raise ValueError(f"Schematic not found on disk: {sch} (call save_schematic first)")
    res = _erc_json(sch, str(Path(sch).with_suffix(".erc.json")), severity_error=errors_only)
    if "erc_error" in res:
        return {"ok": False, "reason": res["erc_error"], "report_path": res["erc_report"]}
    return {"ok": True, "violations": res["erc_violations"], "erc_ok": res["erc_ok"],
            "by_severity": res["erc_by_severity"], "report_path": res["erc_report"],
            "report": res["report"]}


@mcp.tool()
def render_sch(sch: str, out_png: str = "") -> dict:
    """Render the schematic to an image via kicad-cli for visual verification."""
    if not KICAD_CLI:
        return {"ok": False, "reason": "kicad-cli not found on PATH; install KiCad 9."}
    out = out_png or str(Path(sch).with_suffix(".svg"))
    r = subprocess.run([KICAD_CLI, "sch", "export", "svg", "-o",
                        str(Path(out).parent), sch], capture_output=True, text=True)
    return {"ok": r.returncode == 0, "output_dir": str(Path(out).parent),
            "stderr": r.stderr[-1000:]}


# ----------------------------------------------------------------------------
# PCB TOOLS (KiCad 9 IPC API via kipy)
# ----------------------------------------------------------------------------
_ENABLE_API_HINT = ("Open KiCad with the API server enabled (Preferences > Plugins and "
                    "Content Manager > Enable KiCad API; KiCad 9: Preferences > API).")
_EDITOR_NAMES = {"pcb": "Pcbnew (PCB editor)", "schematic": "Eeschema (schematic editor)",
                 "project": "project manager"}
# (kind, DocumentType enum name) — queried in this order; a KiCad without a
# schematic API answers the schematic query with an ApiError, which is tolerated.
_DOC_QUERIES = (("pcb", "DOCTYPE_PCB"), ("schematic", "DOCTYPE_SCHEMATIC"),
                ("project", "DOCTYPE_PROJECT"))


def _kicad_version_of(kicad) -> tuple[str, tuple[int, ...]]:
    """(full version string, numeric tuple) from a connected KiCad."""
    v = kicad.get_version()
    full = str(getattr(v, "full_version", "") or v)
    parts = tuple(getattr(v, a, None) for a in ("major", "minor", "patch"))
    if all(isinstance(p, int) for p in parts):
        return full, parts  # type: ignore[return-value]
    return full, _version_tuple(full)


def _doc_path(doc, kind: str) -> str:
    """Absolute file path of an open DocumentSpecifier, best effort."""
    has_project = False
    try:
        has_project = doc.HasField("project")
    except ValueError:
        has_project = False
    pdir = doc.project.path if has_project else ""
    pname = doc.project.name if has_project else ""
    if kind == "pcb":
        fn = doc.board_filename or (f"{pname}.kicad_pcb" if pname else "")
        if fn and not Path(fn).is_absolute() and pdir:
            return str(Path(pdir) / fn)
        return fn
    if kind == "schematic":
        return str(Path(pdir) / f"{pname}.kicad_sch") if pdir and pname else ""
    if kind == "project":
        return str(Path(pdir) / f"{pname}.kicad_pro") if pdir and pname else pdir
    return ""


def _open_documents(kicad) -> tuple[list[dict[str, str]], list[str]]:
    """[{type, path}] for every document open in KiCad, plus probe notes for
    queries KiCad refused (e.g. DOCTYPE_SCHEMATIC on KiCad 9/10)."""
    from kipy.proto.common.types import DocumentType
    docs: list[dict[str, str]] = []
    notes: list[str] = []
    for kind, enum_name in _DOC_QUERIES:
        try:
            found = kicad.get_open_documents(getattr(DocumentType, enum_name))
        except Exception as e:  # kipy ApiError on unsupported doc types
            notes.append(f"{kind}: {type(e).__name__}: {str(e)[:120]}")
            continue
        docs += [{"type": kind, "path": _doc_path(d, kind)} for d in found]
    return docs, notes


def _kipy_connect() -> dict:
    """Connect to a running KiCad (no board required). Returns a dict with
    ipc_connected, kicad_version, open_documents, capabilities and the live
    `kicad` client — or {"ipc_connected": False, "reason": ...}."""
    try:
        from kipy import KiCad  # type: ignore
    except Exception as e:
        return {"ipc_connected": False,
                "reason": f"kicad-python (kipy) not importable in the server venv ({e})."}
    try:
        kicad = KiCad()
        full, ver = _kicad_version_of(kicad)
    except Exception as e:
        return {"ipc_connected": False,
                "reason": f"KiCad IPC API not reachable ({e}). {_ENABLE_API_HINT}"}
    docs, notes = _open_documents(kicad)
    out = {"ipc_connected": True, "kicad": kicad, "kicad_version": full,
           "open_documents": docs, "capabilities": _capabilities(ver)}
    return {**out, "open_documents_note": "; ".join(notes)} if notes else out


def _public(k: dict) -> dict:
    """Strip live client objects so the dict can be returned to the caller."""
    return {kk: v for kk, v in k.items() if kk not in ("kicad", "board")}


def _kipy() -> dict:
    """Connected KiCad + the board open in Pcbnew (keys `kicad`, `board`), or a
    JSON-safe dict describing why not (no `board` key)."""
    k = _kipy_connect()
    if not k.get("ipc_connected"):
        return k
    try:
        board = k["kicad"].get_board()
    except Exception as e:
        return {**_public(k), "ok": False,
                "reason": f"KiCad IPC is reachable but no board is open in Pcbnew ({e}). "
                          "Open the .kicad_pcb in the PCB editor and retry."}
    return {**k, "board": board}


def _holding_kind(k: dict, path: str) -> str | None:
    """Which editor kind ('pcb'/'schematic'/'project') in the connection dict
    `k` (from _kipy_connect) has `path` open; None when it is not open or the
    connection failed (unknown never blocks). One path-resolution rule for
    every guard: absolute + normcase on both sides."""
    if not k.get("ipc_connected"):
        return None
    target = os.path.normcase(os.path.abspath(path))
    for d in k["open_documents"]:
        if d["path"] and os.path.normcase(os.path.abspath(d["path"])) == target:
            return d["type"]
    return None


def _editor_holding(path: str) -> str | None:
    """Which KiCad editor kind has `path` open (fresh IPC connection), or None."""
    return _holding_kind(_kipy_connect(), path)


def _open_in_editor(path: str) -> bool:
    """True when `path` is currently open in a running KiCad editor."""
    return _editor_holding(path) is not None


def _write_refusal(path: str, kind: str) -> dict:
    """The refusal every WRITE tool returns for a file open in a KiCad editor
    (writing it from outside would be lost or corrupt the editor's copy)."""
    return {"ok": False,
            "reason": f"{path} is open in KiCad {_EDITOR_NAMES.get(kind, kind)}; close it "
                      "(or save+close) before writing, then reopen to see the result."}


def _write_guard(path: str) -> dict | None:
    """Refusal dict when `path` is open in a KiCad editor, else None."""
    kind = _editor_holding(path)
    return None if kind is None else _write_refusal(path, kind)


def _live_board_report(k: dict, target: str) -> dict:
    """create_pcb's answer when `target` is the board already open in Pcbnew:
    report it (ipc_connected true) instead of creating anything."""
    res = {"ipc_connected": True, "ok": True, "created": False, "pcb": target,
           "note": "this board is already open in KiCad; nothing was created"}
    try:
        res["footprints"] = len(k["kicad"].get_board().get_footprints())
    except Exception as e:  # kipy ApiError: editor busy / board not yet loaded
        res["footprints_error"] = str(e)[:200]
    return res


@mcp.tool()
def create_pcb(pcb: str, sch: str = "", overwrite: bool = False) -> dict:
    """Create an empty .kicad_pcb (+ sibling .kicad_pro) on disk with KiCad's own
    pcbnew (no hand-written S-expressions). No running KiCad needed. If KiCad has
    THIS file open already, reports the live board instead (ipc_connected true).
    Footprints reach the board from the schematic via Update PCB from Schematic
    (F8) in KiCad; `sch` is recorded for that step. Refuses to overwrite an
    existing file unless `overwrite` is true."""
    if not pcb.endswith(".kicad_pcb"):
        raise ValueError(f"pcb must end in .kicad_pcb, got '{pcb}'")
    target = str(Path(pcb).resolve())
    # one IPC connection, one path-resolution rule (the same _holding_kind the
    # write guards use, so a relative board_filename resolves via its project)
    k = _kipy_connect()
    kind = _holding_kind(k, target)
    if kind == "pcb":
        return _live_board_report(k, target)
    if Path(target).is_file() and not overwrite:
        return {"ok": False, "created": False, "pcb": target,
                "reason": f"{target} already exists; pass overwrite=true to replace it"}
    if kind is not None:
        return _write_refusal(target, kind)
    kpy = _kicad_python()
    if not kpy:
        return {"ok": False, "reason": f"KiCad bundled Python (pcbnew) not found: {_KPY['error']}"}
    Path(target).parent.mkdir(parents=True, exist_ok=True)
    code = ("import sys, pcbnew\n"
            "board = pcbnew.CreateEmptyBoard()\n"
            "pcbnew.SaveBoard(sys.argv[1], board)\n")
    r = _run([kpy, "-c", code, target], timeout=120)
    if r.returncode != 0 or not Path(target).is_file():
        return {"ok": False, "created": False, "pcb": target,
                "reason": "pcbnew failed to create the board", "stderr": r.stderr[-1200:]}
    pro = Path(target).with_suffix(".kicad_pro")
    res = {"ok": True, "pcb": target, "created": True,
           "project": str(pro) if pro.is_file() else None,
           "next": "Open it in KiCad (Pcbnew) with the API enabled, then call test_kicad_connection"}
    if sch:
        res["sch"] = sch
        res["next"] += "; run Tools > Update PCB from Schematic (F8) to import the footprints"
    return res


@mcp.tool()
def get_pcb_summary() -> dict:
    """Summarize the open board: footprint count, nets, layer count, size."""
    k = _kipy()
    if "board" not in k:
        return k
    board = k["board"]
    fps = board.get_footprints()
    return {"ipc_connected": True, "footprints": len(fps),
            "refs": [f.reference_field.text.value for f in fps][:200]}


# --- kipy helpers (resolve layers, nets, geometry) --------------------------
_LAYER_ALIASES = {
    "F.Cu": "BL_F_Cu", "B.Cu": "BL_B_Cu",
    "In1.Cu": "BL_In1_Cu", "In2.Cu": "BL_In2_Cu",
    "In3.Cu": "BL_In3_Cu", "In4.Cu": "BL_In4_Cu",
}


def _layer_value(name: str) -> int:
    """Map a KiCad layer name ('F.Cu', 'In1.Cu', 'B.Cu') to its BoardLayer enum value."""
    from kipy.board_types import BoardLayer
    key = _LAYER_ALIASES.get(name)
    if key is None:
        key = name if name.startswith("BL_") else "BL_" + name.replace(".", "_")
    return BoardLayer.Value(key)


def _resolve_net(board, net_name: str):
    """Return the live board Net matching net_name, or a fresh Net(name=...) fallback.

    Nets are created from the schematic netlist (Update PCB from Schematic). A trace
    on a net the board doesn't know about will be electrically unconnected, so callers
    should prefer names that already exist; we surface that via register_net."""
    from kipy.board_types import Net
    for n in board.get_nets():
        if n.name == net_name:
            return n
    return Net(name=net_name)


def _fp_by_ref(board, ref: str):
    return next((f for f in board.get_footprints()
                 if f.reference_field.text.value == ref), None)


def _box_minmax(bb):
    """Return (xmin, ymin, xmax, ymax) in nm from a kipy Box2, tolerant of shape."""
    pos = getattr(bb, "pos", None)
    size = getattr(bb, "size", None)
    if pos is not None and size is not None:
        return (pos.x, pos.y, pos.x + size.x, pos.y + size.y)
    mn, mx = getattr(bb, "min", None), getattr(bb, "max", None)
    if callable(mn):
        mn, mx = mn(), mx()
    if mn is not None and mx is not None:
        return (mn.x, mn.y, mx.x, mx.y)
    raise ValueError("unrecognized Box2 shape")


def _boxes_overlap(a, b) -> bool:
    ax0, ay0, ax1, ay1 = _box_minmax(a)
    bx0, by0, bx1, by1 = _box_minmax(b)
    return not (ax1 <= bx0 or bx1 <= ax0 or ay1 <= by0 or by1 <= ay0)


@mcp.tool()
def test_kicad_connection() -> dict:
    """Probe the running KiCad over IPC (MANDATORY first PCB step): version, every
    open document ({type: pcb|schematic|project, path}) and the version-derived
    capabilities (schematic_api / place_footprint_from_library / import_netlist
    are KiCad 11+ only). ipc_connected false + reason when unreachable."""
    k = _kipy_connect()
    if not k.get("ipc_connected"):
        # same shape as the connected result so callers can always read
        # capabilities / open_documents without a key check (C1)
        return {**k, "kicad_version": None, "open_documents": [],
                "capabilities": _capabilities(())}
    res = _public(k)
    boards = [d["path"] for d in k["open_documents"] if d["type"] == "pcb"]
    if boards:
        try:
            res["footprints"] = len(k["kicad"].get_board().get_footprints())
        except Exception as e:
            res["footprints_error"] = str(e)[:200]
    return res


@mcp.tool()
def add_footprint_to_pcb(ref: str, lib_id: str, x: float, y: float,
                         rot: float = 0, net_assignments: dict | None = None) -> dict:
    """Place a footprint on the board. NOTE: KiCad 9's IPC API cannot instantiate a
    footprint from a library — footprints reach the board from the schematic netlist
    via Tools > Update PCB from Schematic (F8). Use move_footprint to position one that
    is already on the board."""
    k = _kipy()
    if "board" not in k:
        return k
    existing = _fp_by_ref(k["board"], ref)
    if existing is not None:
        return move_footprint(ref, x, y, rot)
    return {"ipc_connected": True, "ok": False, "ref": ref,
            "reason": "KiCad 9 IPC cannot create a footprint from a library id. "
                      "Run Tools > Update PCB from Schematic (F8) in KiCad to import "
                      "footprints from the netlist, then call move_footprint to place them."}


@mcp.tool()
def add_footprints_batch(footprints: list[dict]) -> dict:
    """Position many already-imported footprints in one pass. Each dict: ref, x, y, [rot].

    Footprints must already be on the board (imported via Update PCB from Schematic);
    this moves each to its target location."""
    k = _kipy()
    if "board" not in k:
        return k
    from kipy.board_types import Vector2, Angle
    board = k["board"]
    moved, missing = [], []
    items = []
    for c in footprints:
        fp = _fp_by_ref(board, c["ref"])
        if fp is None:
            missing.append(c["ref"])
            continue
        fp.position = Vector2.from_xy_mm(float(c["x"]), float(c["y"]))
        fp.orientation = Angle.from_degrees(float(c.get("rot", 0)))
        items.append(fp)
        moved.append(c["ref"])
    if items:
        board.update_items(items)
    return {"ipc_connected": True, "ok": not missing, "moved": moved,
            "missing": missing, "count": len(moved)}


@mcp.tool()
def move_footprint(ref: str, x: float, y: float, rot: float = 0) -> dict:
    """Move a placed footprint to (x,y) mm with rotation (degrees). Requires IPC."""
    k = _kipy()
    if "board" not in k:
        return k
    from kipy.board_types import Vector2, Angle
    board = k["board"]
    fp = _fp_by_ref(board, ref)
    if fp is None:
        return {"ipc_connected": True, "ok": False,
                "reason": f"No footprint {ref} on the board."}
    fp.position = Vector2.from_xy_mm(float(x), float(y))
    fp.orientation = Angle.from_degrees(float(rot))
    board.update_items([fp])
    return {"ipc_connected": True, "ok": True, "ref": ref, "at": [x, y, rot]}


@mcp.tool()
def remove_footprint(ref: str) -> dict:
    """Remove a footprint from the board by reference. Requires IPC."""
    k = _kipy()
    if "board" not in k:
        return k
    board = k["board"]
    fp = _fp_by_ref(board, ref)
    if fp is None:
        return {"ipc_connected": True, "ok": False,
                "reason": f"No footprint {ref} on the board."}
    board.remove_items([fp])
    return {"ipc_connected": True, "ok": True, "ref": ref}


@mcp.tool()
def get_footprint_pads(ref: str) -> dict:
    """Return pad numbers, nets, and absolute positions (mm) for a footprint. Requires IPC."""
    k = _kipy()
    if "board" not in k:
        return k
    board = k["board"]
    fp = _fp_by_ref(board, ref)
    if fp is None:
        return {"ipc_connected": True, "ok": False,
                "reason": f"No footprint {ref} on the board."}
    pads = []
    try:
        fp_pads = fp.definition.pads  # type: ignore[attr-defined]
    except Exception:
        fp_pads = []
    for p in fp_pads:
        pos = getattr(p, "position", None)
        pads.append({"number": getattr(p, "number", "?"),
                     "net": getattr(getattr(p, "net", None), "name", ""),
                     "x_mm": round(pos.x / 1e6, 4) if pos is not None else None,
                     "y_mm": round(pos.y / 1e6, 4) if pos is not None else None})
    return {"ipc_connected": True, "ok": True, "ref": ref, "pads": pads}


@mcp.tool()
def register_net(net_name: str) -> dict:
    """Confirm a net exists on the board before routing it. Nets originate from the
    schematic netlist; this reports presence rather than creating a bare net. Requires IPC."""
    k = _kipy()
    if "board" not in k:
        return k
    exists = any(n.name == net_name for n in k["board"].get_nets())
    return {"ipc_connected": True, "ok": exists, "net": net_name, "exists": exists,
            "note": "" if exists else
            "Net not present yet — import it via Update PCB from Schematic (F8). "
            "A trace placed on an unknown net will be unconnected."}


@mcp.tool()
def add_trace(net_name: str, layer: str, x1: float, y1: float,
              x2: float, y2: float, width: float = 0.25) -> dict:
    """Add a copper trace segment on a net/layer between two points (mm). Requires IPC."""
    k = _kipy()
    if "board" not in k:
        return k
    from kipy.board_types import Track, Vector2, from_mm
    board = k["board"]
    t = Track()
    t.start = Vector2.from_xy_mm(float(x1), float(y1))
    t.end = Vector2.from_xy_mm(float(x2), float(y2))
    t.width = from_mm(float(width))
    t.layer = _layer_value(layer)
    t.net = _resolve_net(board, net_name)
    created = board.create_items(t)
    return {"ipc_connected": True, "ok": bool(created), "net": net_name,
            "layer": layer, "created": len(created)}


@mcp.tool()
def add_via(net_name: str, x: float, y: float,
            drill: float = 0.3, diameter: float = 0.6) -> dict:
    """Add a via on a net at (x,y) mm. Requires IPC."""
    k = _kipy()
    if "board" not in k:
        return k
    from kipy.board_types import Via, Vector2, from_mm
    board = k["board"]
    v = Via()
    v.position = Vector2.from_xy_mm(float(x), float(y))
    v.diameter = from_mm(float(diameter))
    v.drill_diameter = from_mm(float(drill))
    v.net = _resolve_net(board, net_name)
    created = board.create_items(v)
    return {"ipc_connected": True, "ok": bool(created), "net": net_name,
            "created": len(created)}


@mcp.tool()
def add_copper_zone(net_name: str, layer: str, polygon: list[list[float]]) -> dict:
    """Add a copper pour/zone on a net and layer bounded by a polygon (list of [x,y] mm). Requires IPC."""
    k = _kipy()
    if "board" not in k:
        return k
    from kipy.board_types import Zone, Vector2
    from kipy.geometry import PolyLine
    board = k["board"]
    z = Zone()
    z.name = f"{net_name}_zone"
    try:
        z.layers = [_layer_value(layer)]
    except Exception:
        pass
    z.net = _resolve_net(board, net_name)
    outline = PolyLine()
    for pt in polygon:
        outline.append(Vector2.from_xy_mm(float(pt[0]), float(pt[1])))
    try:
        outline.closed = True
    except Exception:
        pass
    z.outline = outline
    created = board.create_items(z)
    board.refill_zones()
    return {"ipc_connected": True, "ok": bool(created), "net": net_name,
            "layer": layer, "created": len(created)}


@mcp.tool()
def validate_placement() -> dict:
    """Check the current placement for courtyard/bounding-box overlaps. Requires IPC."""
    k = _kipy()
    if "board" not in k:
        return k
    board = k["board"]
    fps = board.get_footprints()
    boxes = []
    for f in fps:
        bb = board.get_item_bounding_box(f)
        if bb is not None:
            boxes.append((f.reference_field.text.value, bb))
    overlaps = []
    for i in range(len(boxes)):
        for j in range(i + 1, len(boxes)):
            try:
                if _boxes_overlap(boxes[i][1], boxes[j][1]):
                    overlaps.append([boxes[i][0], boxes[j][0]])
            except Exception:
                pass
    return {"ipc_connected": True, "footprints": len(fps),
            "checked": len(boxes), "overlaps": overlaps, "ok": not overlaps}


def _drc_counts(pcb: str) -> dict:
    """Run kicad-cli DRC (error severity) on a saved board and summarize the JSON
    report: {errors, unconnected, types: {type: count}, report}. When the report
    cannot be produced or parsed the dict carries an `error` key (counts -1)."""
    if not KICAD_CLI:
        raise RuntimeError("kicad-cli not found; install KiCad 9.")
    rpt = str(Path(pcb).with_suffix("")) + "_drc.json"
    r = _run([KICAD_CLI, "pcb", "drc", pcb, "--format", "json", "--severity-error",
              "-o", rpt], timeout=KICAD_CLI_TIMEOUT_S)
    try:
        d = json.loads(Path(rpt).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return {"errors": -1, "unconnected": -1, "types": {}, "report": rpt,
                "error": f"DRC report unreadable ({e}): {(r.stdout + r.stderr)[-600:]}"}
    violations = d.get("violations", [])
    return {"errors": len(violations),
            "unconnected": len(d.get("unconnected_items", [])),
            "types": dict(Counter(v.get("type", "unknown") for v in violations)),
            "report": rpt}


@mcp.tool()
def drc_pcb(pcb: str, fab_profile: str = "") -> dict:
    """Run KiCad DRC (error severity) on a SAVED .kicad_pcb via kicad-cli and
    return the error / unconnected counts, a per-type histogram and the JSON
    report path. Read-only: safe on a board that is open in the editor, but
    unsaved GUI changes are not reflected. The fab profile is echoed; the rules
    themselves live in the board/.kicad_pro."""
    profile = fab_profile or FAB_PROFILE
    if not KICAD_CLI:
        return {"ok": False, "reason": "kicad-cli not found; install KiCad 9.", "profile": profile}
    if not Path(pcb).is_file():
        raise ValueError(f"PCB not found: {pcb}")
    c = _drc_counts(pcb)
    if "error" in c:
        return {"ok": False, "reason": c["error"], "report": c["report"], "profile": profile}
    res = {"ok": c["errors"] == 0 and c["unconnected"] == 0, "errors": c["errors"],
           "unconnected": c["unconnected"], "types": c["types"], "report": c["report"],
           "profile": profile}
    if _open_in_editor(pcb):
        res["note"] = "file is open in the editor; unsaved GUI changes are not reflected"
    return res


@mcp.tool()
def export_gerbers(pcb: str, out_dir: str = "") -> dict:
    """Export Gerbers + drill via kicad-cli. Requires KiCad 9."""
    if not KICAD_CLI:
        return {"ok": False, "reason": "kicad-cli not found; install KiCad 9."}
    out = out_dir or str(Path(pcb).with_suffix("")) + "_gerbers"
    Path(out).mkdir(parents=True, exist_ok=True)
    g = subprocess.run([KICAD_CLI, "pcb", "export", "gerbers", "-o", out, pcb],
                       capture_output=True, text=True)
    d = subprocess.run([KICAD_CLI, "pcb", "export", "drill", "-o", out, pcb],
                       capture_output=True, text=True)
    files = [p.name for p in Path(out).iterdir()] if Path(out).is_dir() else []
    return {"ok": g.returncode == 0 and d.returncode == 0, "out_dir": out,
            "files": files, "stderr": (g.stderr + d.stderr)[-1500:]}


@mcp.tool()
def render_pcb(pcb: str, side: str = "top", out_png: str = "") -> dict:
    """Render the board to a PNG (top/bottom) via kicad-cli for visual review. Requires KiCad 9."""
    if not KICAD_CLI:
        return {"ok": False, "reason": "kicad-cli not found; install KiCad 9."}
    out = out_png or str(Path(pcb).with_suffix(f".{side}.png"))
    r = subprocess.run([KICAD_CLI, "pcb", "render", "--side", side, "-o", out, pcb],
                       capture_output=True, text=True)
    return {"ok": r.returncode == 0, "output": out, "stderr": r.stderr[-1000:]}


# ----------------------------------------------------------------------------
# NET-MAP PIPELINE  (concept → Gerbers, headless; no IPC required)
#
# These tools drive the proven net-map-first flow: a pad-level netlist.json is
# the single source of truth, from which an ERC-clean schematic AND a routed,
# DRC-clean board with copper planes are generated. They shell out to the
# scripts in server/tools/ — sch_build under this server's own Python (needs
# sexpdata), the SWIG board/route steps under KiCad's bundled Python (needs
# pcbnew), and Freerouting under Java.
# ----------------------------------------------------------------------------
_KPY: dict[str, Any] = {"checked": False, "path": None, "error": None}


def _kicad_python_candidates() -> list[Path]:
    if not KICAD_CLI:
        return []
    bin_dir = Path(KICAD_CLI).parent
    if os.name == "nt":
        return [bin_dir / "python.exe"]
    # macOS app bundle keeps its Python in Frameworks/, Linux uses the system one
    return [bin_dir / "python3",
            bin_dir.parent / "Frameworks" / "Python.framework" / "Versions" / "Current" / "bin" / "python3",
            Path(shutil.which("python3") or "")]


def _kicad_python() -> str | None:
    """KiCad's bundled Python, VERIFIED to import `pcbnew` (checked once per
    process; the failure text is kept in _KPY['error'] for server_status)."""
    if _KPY["checked"]:
        return _KPY["path"]
    _KPY["checked"] = True
    if not KICAD_CLI:
        _KPY["error"] = "kicad-cli not found, so KiCad's bundled Python could not be located"
        return None
    errors: list[str] = []
    for cand in _kicad_python_candidates():
        if not cand.is_file():
            continue
        try:
            r = subprocess.run([str(cand), "-c", "import pcbnew"], capture_output=True,
                               text=True, timeout=PCBNEW_PROBE_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            errors.append(f"{cand}: 'import pcbnew' timed out after {PCBNEW_PROBE_TIMEOUT_S} s")
            continue
        except OSError as e:
            errors.append(f"{cand}: cannot start ({e})")
            continue
        if r.returncode == 0:
            _KPY["path"] = str(cand)
            return _KPY["path"]
        errors.append(f"{cand}: cannot import pcbnew: {r.stderr.strip()[-400:]}")
    _KPY["error"] = "; ".join(errors) or f"no bundled Python found beside {KICAD_CLI}"
    return None


def _find_java() -> str | None:
    # a tools-local portable JRE wins — Freerouting 2.2.4+ is compiled for
    # Java 25, newer than any system-installed JDK we can rely on
    for cand in sorted(TOOLS_DIR.glob("jre*/**/bin/java.exe"), reverse=True):
        if cand.is_file():
            return str(cand)
    found = shutil.which("java")
    if found:
        return found
    jh = os.environ.get("JAVA_HOME")
    exe = "java.exe" if os.name == "nt" else "java"
    if jh and (Path(jh) / "bin" / exe).is_file():
        return str(Path(jh) / "bin" / exe)
    if os.name == "nt":
        for base in (r"C:\Program Files\Microsoft", r"C:\Program Files\Java",
                     r"C:\Program Files\Eclipse Adoptium"):
            b = Path(base)
            if b.is_dir():
                for d in sorted(b.iterdir(), reverse=True):
                    cand = d / "bin" / exe
                    if cand.is_file():
                        return str(cand)
    return None


def _freerouting_jar(explicit: str = "") -> str | None:
    cands: list[Path] = []
    if explicit:
        cands.append(Path(explicit))
    env = os.environ.get("FREEROUTING_JAR")
    if env:
        cands.append(Path(env))
    # prefer a versioned jar (e.g. freerouting-1.9.0.jar runs on JDK21)
    cands += sorted(TOOLS_DIR.glob("freerouting*.jar"), reverse=True)
    for c in cands:
        if c.is_file():
            return str(c)
    return None


def _run(args: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, **kw)


def _last_json(stdout: str) -> dict | None:
    for line in reversed(stdout.strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except Exception:
                continue
    return None


@mcp.tool()
def build_schematic_from_netlist(netlist: str, out_sch: str = "",
                                 symbols_dir: str = "", sidecars_dir: str = "",
                                 run_erc: bool = True) -> dict:
    """Generate an ERC-clean .kicad_sch from a pad-level netlist.json.

    Embeds flattened library symbols, lays components on a 1.27 mm grid, and wires
    connectivity with a net label at every pin (+ PWR_FLAGs on undriven power nets,
    no_connect flags on unused pins). Defaults: symbols_dir = KiCad's stock symbol
    library, sidecars_dir = this server's pins/ dir. Optionally runs ERC and reports
    the violation count."""
    script = TOOLS_DIR / "sch_build.py"
    if not script.is_file():
        return {"ok": False, "reason": f"missing {script}"}
    out = out_sch or str(Path(netlist).with_suffix(".kicad_sch"))
    syms = _symbols_dir(symbols_dir)
    sides = sidecars_dir or str(PINS_DIR)
    if not syms:
        return {"ok": False, "reason": "symbols_dir not given and KiCad share dir not found"}
    guard = _write_guard(out)
    if guard:
        return guard
    r = _run([sys.executable, str(script), netlist, out, syms, sides])
    res = _last_json(r.stdout) or {}
    res["ok"] = r.returncode == 0 and res.get("ok", False)
    res["sch"] = out
    if r.stderr:
        res["stderr"] = r.stderr[-1200:]
    if res["ok"] and run_erc and KICAD_CLI:
        # gate on ERRORS only (warnings inform but don't block — e.g. the
        # pin_to_pin warning for an address-strap pin tied to a power net is a
        # known false positive); same severity policy as the DRC gate.
        erc = _erc_json(out, str(Path(out).with_suffix("")) + "_erc.json")
        res.update({k: v for k, v in erc.items() if k != "report"})
    return res


@mcp.tool()
def build_board_from_netlist(netlist: str, board_w: float, board_h: float,
                             out_pcb: str = "", footprints_dir: str = "",
                             placement: str = "", pack_scale: float = 1.0) -> dict:
    """Generate a placed .kicad_pcb with copper planes from the netlist.json.

    Loads every footprint by lib_id, places it (board-relative coords, optionally
    overridden by a placement.json of ref -> [x, y, rot]), centers the board on an
    A4 sheet, assigns every pad to its net (all copper elements of split thermal
    pads), draws the Edge.Cuts outline, sets JLCPCB-class rules, and pours a GND
    plane on In1.Cu + the first power rail on In2.Cu (filled). Runs under KiCad's
    bundled Python. pack_scale (>1) loosens auto-placement spacing to give a
    deterministic router more room on a re-route."""
    kpy = _kicad_python()
    if not kpy:
        return {"ok": False, "reason": "KiCad bundled Python (with pcbnew) not found"}
    script = TOOLS_DIR / "swig_build_board.py"
    if not script.is_file():
        return {"ok": False, "reason": f"missing {script}"}
    out = out_pcb or str(Path(netlist).with_suffix(".kicad_pcb"))
    fps = footprints_dir or (str(_kicad_share() / "footprints") if _kicad_share() else "")
    if not fps:
        return {"ok": False, "reason": "footprints_dir not given and KiCad share dir not found"}
    guard = _write_guard(out)
    if guard:
        return guard
    args = [kpy, str(script), netlist, out, fps, str(board_w), str(board_h)]
    if placement:
        args.append(placement)
    env = {**os.environ, "PACK_SCALE": str(pack_scale)}
    r = _run(args, env=env)
    res = _last_json(r.stdout) or {"ok": False}
    res["ok"] = r.returncode == 0 and res.get("ok", False)
    res["pcb"] = out
    if r.stderr:
        res["stderr"] = r.stderr[-1200:]
    if res["ok"]:
        res.update(_apply_netclasses(netlist, out, res.get("zones", [])))
    return res


def _apply_netclasses(netlist: str, out_pcb: str, zones: list[str]) -> dict:
    """Resolve electrical intent (net_specs + reliability class) into IPC-2221
    net classes and write them into the .kicad_pro so BOTH the Specctra DSN
    export (Freerouting routes power copper wider) and kicad-cli DRC honor
    per-net widths/clearances. Default class 3 (high-reliability) unless the
    netlist overrides "class". A netlist WITH net_specs whose classes cannot be
    written fails the build (ok False) — a board routed at default widths must
    never pass silently. A netlist without net_specs only reports the slip."""
    design = json.loads(Path(netlist).read_text(encoding="utf-8"))
    has_specs = bool(design.get("net_specs"))
    try:
        if str(TOOLS_DIR) not in sys.path:
            sys.path.insert(0, str(TOOLS_DIR))
        import netclass_writer
        pro = str(Path(out_pcb).with_suffix(".kicad_pro"))
        # nets backed by a poured plane ("In1.Cu:GND") or by designer preroutes
        # are exempt from track-width classing — the plane or the prerouted
        # copper carries the current, and a width class would make the net's
        # signal taps unreachable for the router
        planes = tuple(z.split(":", 1)[1] for z in zones if ":" in z)
        planes += tuple(p["net"] for p in design.get("preroutes", []))
        nc = netclass_writer.apply_to_project(
            pro, design, int(design.get("class", 3)), plane_nets=planes)
    except Exception as e:
        nc = {"ok": False, "reason": f"{type(e).__name__}: {e}"}
    summary = {"profile": nc.get("class_profile"), "written": nc.get("classes_written", []),
               "warnings": nc.get("warnings", [])}
    if nc.get("ok"):
        return {"netclasses": summary}
    failure = {"netclasses": {**summary, "error": nc.get("reason", "unknown failure")}}
    if not has_specs:
        return failure
    return {**failure, "ok": False,
            "reason": f"net classes not written: {nc.get('reason', 'unknown failure')}"}


@mcp.tool()
def autoroute_board(pcb: str, max_passes: int = 100, jar: str = "",
                    route_timeout: int = 300) -> dict:
    """Autoroute a board with Freerouting: export Specctra DSN, route, import the
    SES session back, and refill zones. Signal layers route on F.Cu/B.Cu while
    GND/power return to the inner planes. The router is killed after
    route_timeout seconds so a pathological board can never hang the pipeline.
    Returns the resulting track count and unconnected count."""
    kpy = _kicad_python()
    java = _find_java()
    fj = _freerouting_jar(jar)
    if not kpy:
        return {"ok": False, "reason": "KiCad bundled Python not found"}
    if not java:
        return {"ok": False, "reason": "java not found (Freerouting needs a JRE/JDK)"}
    if not fj:
        return {"ok": False, "reason": "freerouting jar not found in server/tools or $FREEROUTING_JAR"}
    guard = _write_guard(pcb)
    if guard:
        return guard
    base = str(Path(pcb).with_suffix(""))
    dsn, ses = base + ".dsn", base + ".ses"
    d = _run([kpy, str(TOOLS_DIR / "swig_export_dsn.py"), pcb, dsn], timeout=120)
    if not Path(dsn).is_file():
        return {"ok": False, "step": "dsn", "stderr": (d.stdout + d.stderr)[-1200:]}
    # remove any stale SES so a router timeout can't import a previous result
    try:
        Path(ses).unlink()
    except FileNotFoundError:
        pass
    # Freerouting 2.x: true headless, internal wall-clock budget (so it
    # finishes and WRITES the SES instead of being killed with nothing),
    # multi-threaded routing, no interactive dialog stalls. 1.x jars only
    # understand the short flags — detect by jar name.
    # Its CLI re-splits arguments on spaces, so paths like "OneDrive - NTS"
    # break it: route in a space-free temp dir and copy results back.
    work = Path(tempfile.mkdtemp(prefix="frt_"))
    w_dsn, w_ses = str(work / "in.dsn"), str(work / "out.ses")
    shutil.copyfile(dsn, w_dsn)
    args = [java, "-Xmx4g", "-jar", fj, "-de", w_dsn, "-do", w_ses,
            "-mp", str(max_passes)]
    # Freerouting 1.x initializes AWT/Swing even in CLI mode — on a
    # display-less Linux box (Docker, CI) it dies with HeadlessException
    # before routing anything. Wrap it in a virtual framebuffer.
    if os.name != "nt" and not os.environ.get("DISPLAY") \
            and "freerouting-2" not in Path(fj).name:
        if shutil.which("xvfb-run"):
            args = ["xvfb-run", "-a"] + args
        else:
            return {"ok": False, "reason":
                    "no display and xvfb-run not found — Freerouting 1.x "
                    "needs one (install xvfb, e.g. apt-get install xvfb)"}
    if "freerouting-2" in Path(fj).name:
        budget = max(60, route_timeout - 60)
        hh, rem = divmod(budget, 3600)
        mm_, ss = divmod(rem, 60)
        args += ["--gui.enabled=false", "-dct", "0",
                 f"--router.job_timeout={hh:02d}:{mm_:02d}:{ss:02d}",
                 "--feature_flags.multi_threading=true"]
    try:
        fr = _run(args, timeout=route_timeout)
        fr_out = (fr.stdout + fr.stderr)[-1500:]
        if Path(w_ses).is_file():
            shutil.copyfile(w_ses, ses)
    except subprocess.TimeoutExpired:
        return {"ok": False, "step": "route",
                "reason": f"Freerouting exceeded {route_timeout}s and was killed"}
    finally:
        shutil.rmtree(work, ignore_errors=True)
    if not Path(ses).is_file():
        return {"ok": False, "step": "route", "stderr": fr_out}
    im = _run([kpy, str(TOOLS_DIR / "swig_import_ses.py"), pcb, ses], timeout=180)
    imp = _parse_ses_import(im.stdout)
    if not imp["ok"]:
        return {"ok": False, "step": "import", "pcb": pcb, "dsn": dsn, "ses": ses,
                "reason": imp.get("error") or "SES import failed",
                "warnings": imp["warnings"], "import_log": (im.stdout + im.stderr)[-600:]}
    # verify route completeness — Freerouting is non-deterministic and may leave a
    # stub; report the unconnected count so the caller can re-run if it isn't zero.
    unconnected = None
    drc_note = None
    if KICAD_CLI:
        c = _drc_counts(pcb)
        unconnected = c["unconnected"] if "error" not in c else None
        drc_note = c.get("error")
    res = {"ok": unconnected in (0, None), "pcb": pcb, "dsn": dsn, "ses": ses,
           "tracks": imp["tracks"], "vias": imp["vias"], "unconnected": unconnected,
           "warnings": imp["warnings"] + ([drc_note] if drc_note else []),
           "import_log": im.stdout.strip()[-300:],
           "note": None if unconnected in (0, None) else
                   f"{unconnected} net(s) left unrouted — re-run autoroute_board to converge"}
    return res


def _parse_ses_import(stdout: str) -> dict:
    """Result of swig_import_ses.py: its final JSON line
    {"ok", "tracks", "vias", "warnings"[, "error"]} when present, else the
    legacy 'ses_import True tracks N' token line."""
    j = _last_json(stdout)
    if j is not None and "ok" in j:
        return {"ok": bool(j.get("ok")), "tracks": j.get("tracks"), "vias": j.get("vias"),
                "warnings": list(j.get("warnings", [])), "error": j.get("error")}
    digits = [int(tok) for tok in stdout.split() if tok.isdigit()]
    return {"ok": "True" in stdout, "tracks": digits[-1] if digits else None,
            "vias": None, "warnings": [],
            "error": None if "True" in stdout else stdout.strip()[-300:]}


@mcp.tool()
def export_fab_package(pcb: str, out_dir: str = "", zip_name: str = "",
                       netlist: str = "") -> dict:
    """Export a complete fabrication + assembly package — Gerbers, drill,
    pick-and-place — then zip it for the fab house. When `netlist` is given,
    also writes the SMT-assembly pair the fab needs to BUILD the board, in
    JLCPCB format: BOM.csv (Comment/Designator/Footprint/LCSC Part #, grouped
    line items; per-component "lcsc"/"mpn" netlist keys are carried through)
    and CPL.csv (Designator/Mid X/Mid Y/Layer/Rotation). Requires kicad-cli."""
    if not KICAD_CLI:
        return {"ok": False, "reason": "kicad-cli not found; install KiCad."}
    out = out_dir or (str(Path(pcb).with_suffix("")) + "_gerbers")
    Path(out).mkdir(parents=True, exist_ok=True)
    g = _run([KICAD_CLI, "pcb", "export", "gerbers", "-o", out, pcb])
    dr = _run([KICAD_CLI, "pcb", "export", "drill", "-o", out, pcb])
    pos = str(Path(out) / (Path(pcb).stem + "-pos.csv"))
    p = _run([KICAD_CLI, "pcb", "export", "pos", "-o", pos,
              "--format", "csv", "--units", "mm", pcb])

    bom_written = cpl_written = False
    if netlist and Path(netlist).is_file():
        import csv
        design = json.loads(Path(netlist).read_text(encoding="utf-8"))
        groups: dict[tuple, list] = {}
        for c in design.get("components", []):
            key = (c.get("value", ""), c.get("footprint", ""),
                   c.get("lcsc", ""), c.get("mpn", ""))
            groups.setdefault(key, []).append(c["ref"])
        with open(Path(out) / "BOM.csv", "w", newline="", encoding="utf-8") as f:
            wr = csv.writer(f)
            wr.writerow(["Comment", "Designator", "Footprint", "LCSC Part #", "MPN"])
            for (val, fpr, lcsc, mpn), refs in sorted(groups.items()):
                wr.writerow([val, ",".join(sorted(refs)),
                             fpr.split(":")[-1], lcsc, mpn])
        bom_written = True
        if Path(pos).is_file():
            with open(pos, newline="", encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
            with open(Path(out) / "CPL.csv", "w", newline="",
                      encoding="utf-8") as f:
                wr = csv.writer(f)
                wr.writerow(["Designator", "Mid X", "Mid Y", "Layer", "Rotation"])
                for r0 in rows:
                    wr.writerow([r0.get("Ref"), r0.get("PosX"), r0.get("PosY"),
                                 r0.get("Side"), r0.get("Rot")])
            cpl_written = True

    zpath = zip_name or (str(Path(pcb).with_suffix("")) + f"_{FAB_PROFILE}.zip")
    files = [f for f in glob.glob(str(Path(out) / "*")) if not f.endswith(".zip")]
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for f in files:
            z.write(f, arcname=Path(f).name)
    return {"ok": g.returncode == 0 and dr.returncode == 0,
            "out_dir": out, "zip": zpath, "fab_profile": FAB_PROFILE,
            "bom": bom_written, "cpl": cpl_written,
            "files": [Path(f).name for f in files],
            "stderr": (g.stderr + dr.stderr + p.stderr)[-1200:]}


@mcp.tool()
def build_pin_sidecars(symbols: str, out_dir: str = "", symbols_dir: str = "") -> dict:
    """Build pin-coordinate sidecars (pin name/number -> schematic-space offset) for
    a comma-separated list of `Lib:Symbol` ids, used by the schematic wiring tools.
    Writes <Lib_Symbol>.pins.json into out_dir (default: this server's pins/)."""
    script = TOOLS_DIR / "build_pin_sidecars.py"
    if not script.is_file():
        return {"ok": False, "reason": f"missing {script}"}
    syms = _symbols_dir(symbols_dir)
    out = out_dir or str(PINS_DIR)
    if not syms:
        return {"ok": False, "reason": "symbols_dir not given and KiCad share dir not found"}
    ids = [s.strip() for s in symbols.split(",") if s.strip()]
    if not ids:
        raise ValueError("symbols must list at least one Library:Symbol id")
    r = _run([sys.executable, str(script), syms, out] + ids)
    summary = _last_json(r.stdout) or {}
    return {"ok": r.returncode == 0 and summary.get("ok", r.returncode == 0),
            "out_dir": out, "written": summary.get("written", []),
            "failed": summary.get("failed", []),
            "stdout": r.stdout[-800:], "stderr": r.stderr[-800:]}


@mcp.tool()
def design_report(netlist: str, symbols_dir: str = "", out_md: str = "") -> dict:
    """Audit a netlist for design intent + assembly (DFA): part-count reduction
    (repeated passives -> arrays), decoupling coverage (every IC power rail has a
    bypass cap), the power tree (each rail's driver + load count), floating pins,
    and nets that likely need a pull resistor. Reads pin electrical types from the
    KiCad symbols. Returns the structured report and writes a Markdown copy next to
    the netlist. This reviews; it does not modify the design."""
    if str(TOOLS_DIR) not in sys.path:
        sys.path.insert(0, str(TOOLS_DIR))
    try:
        import design_report as dr
    except Exception as e:
        return {"ok": False, "reason": f"design_report import failed: {e}"}
    design = json.loads(Path(netlist).read_text(encoding="utf-8"))
    syms = _symbols_dir(symbols_dir) or ""
    rep = dr.analyze(design, syms)
    md = dr.to_markdown(rep)
    md_path = out_md or (str(Path(netlist).with_suffix("")) + "_report.md")
    Path(md_path).write_text(md, encoding="utf-8")
    return {"ok": True, "report": rep, "markdown": md, "markdown_path": md_path}


@mcp.tool()
def check_design_gates(pcb: str, netlist: str, stage: str = "placement") -> dict:
    """Run the formal design-review gate checks on a board (IEEE 15288.2 working
    reviews). stage="placement" (pre-route, G3): connectors must mouth-out at the
    board edge, antenna keepout present if requested. stage="routed" (pre-CDR,
    G4): every net bound to an IPC net class must be routed at >= the class
    width. Returns {"passed": bool, "issues": [...]} — do NOT proceed to the next
    pipeline stage while issues remain."""
    kpy = _kicad_python()
    script = TOOLS_DIR / "swig_gate_checks.py"
    if not kpy:
        return {"ok": False, "reason": "KiCad bundled Python (pcbnew) not found"}
    if not script.is_file():
        return {"ok": False, "reason": f"missing {script}"}
    r = _run([kpy, str(script), stage, pcb, netlist], timeout=120)
    res = _last_json(r.stdout) or {"issues": [f"no result: {r.stderr[-300:]}"]}
    issues = res.get("issues", [])
    return {"ok": True, "stage": stage, "passed": not issues, "issues": issues}


@mcp.tool()
def add_ground_stitching(pcb: str, gnd_net: str = "GND", max_freq_hz: float = 0.0,
                         er: float = 4.2, interior: bool = True,
                         via_diameter_mm: float = 0.6, via_drill_mm: float = 0.3) -> dict:
    """Add a DRC-safe GND via fence + interior stitching grid to a ROUTED board
    (EMC: ties the ground planes/pours, contains edge fields, keeps the return
    path under the signal). Vias land only where they clear all non-GND copper and
    every hole, so the board stays DRC-clean. Pitch is lambda/20 at `max_freq_hz`
    (set it to your highest signal/RF frequency) or a 3 mm default. Run AFTER
    autoroute_board, BEFORE export_fab_package. Runs under KiCad's bundled Python."""
    kpy = _kicad_python()
    if not kpy:
        return {"ok": False, "reason": f"KiCad bundled Python (pcbnew) not found: {_KPY['error']}"}
    script = TOOLS_DIR / "emc_stitch.py"
    if not script.is_file():
        return {"ok": False, "reason": f"missing {script}"}
    if not Path(pcb).is_file():
        raise ValueError(f"PCB not found: {pcb}")
    guard = _write_guard(pcb)
    if guard:
        return guard
    # TRANSACTIONAL: stitching must never make the board worse. Apply, verify
    # with DRC; on any violation/unconnected roll back and retry fence-only
    # (a dense interior grid can fragment a power plane); if even the fence
    # fails, restore the original board and report the open task.
    bak = pcb + ".prestitch"
    shutil.copyfile(pcb, bak)

    # a board that isn't already G4-clean can't be judged after stitching —
    # require a clean baseline before adding anything
    if KICAD_CLI:
        base = _drc_counts(pcb)
        if "error" in base:
            Path(bak).unlink(missing_ok=True)
            return {"ok": False, "reason": f"baseline DRC failed: {base['error']}"}
        if base["errors"] or base["unconnected"]:
            Path(bak).unlink(missing_ok=True)
            return {"ok": False, "reason": f"board not clean before stitching "
                    f"({base['errors']} violations, {base['unconnected']} unconnected) "
                    f"— pass the G4 layout gate first", "types": base["types"]}

    # emc_stitch.py argv: <pcb> <max_freq_hz> <mode> [gnd_net] [er] [via_dia_mm] [via_drill_mm]
    extra = [gnd_net, str(er), str(via_diameter_mm), str(via_drill_mm)]
    res: dict = {"ok": False}
    for mode in (("full", "fence") if interior else ("fence",)):
        r = _run([kpy, str(script), pcb, str(max_freq_hz), mode] + extra, timeout=180)
        res = _last_json(r.stdout) or {"ok": False, "stderr": (r.stdout + r.stderr)[-600:]}
        if not res.get("ok"):
            break
        if not KICAD_CLI:
            Path(bak).unlink(missing_ok=True)
            return {**res, "mode": mode, "note": "kicad-cli not found: stitching NOT DRC-verified"}
        after = _drc_counts(pcb)
        if "error" not in after and after["errors"] == 0 and after["unconnected"] == 0:
            Path(bak).unlink(missing_ok=True)
            return {**res, "mode": mode, "drc_errors": 0, "unconnected": 0}
        shutil.copyfile(bak, pcb)            # roll back, try the next mode
        res = {"ok": False, "mode": mode, "drc_errors": after["errors"],
               "unconnected": after["unconnected"], "types": after["types"],
               **({"drc_error": after["error"]} if "error" in after else {})}
    Path(bak).unlink(missing_ok=True)
    return {**res, "reason": res.get("reason") or res.get("error") or
            "stitching rolled back — it violated DRC or broke plane connectivity "
            "even fence-only; board left unmodified"}


# ----------------------------------------------------------------------------
@mcp.tool()
def server_status() -> dict:
    """Report what this server can do right now: KiCad CLI, bundled Python
    (verified to import pcbnew; kicad_python_error says why not), symbol/footprint
    libraries, Java + Freerouting, IPC (version, open documents, capabilities),
    fab profile. The net-map pipeline needs kicad_cli + kicad_python; autoroute
    additionally needs java + freerouting_jar; the live PCB tools need ipc."""
    k = _kipy_connect()
    ipc = _public(k)
    return {"kicad_cli": bool(KICAD_CLI), "kicad_cli_path": KICAD_CLI,
            "kicad_python": _kicad_python(), "kicad_python_error": _KPY["error"],
            "kicad_share": str(_kicad_share() or ""), "symbols_dir": _symbols_dir(),
            "java": _find_java(), "freerouting_jar": _freerouting_jar(),
            "ipc": bool(ipc.get("ipc_connected")),
            "ipc_reason": ipc.get("reason"),
            "kicad_version": ipc.get("kicad_version"),
            "open_documents": ipc.get("open_documents", []),
            "capabilities": ipc.get("capabilities", _capabilities(())),
            "fab_profile": FAB_PROFILE, "units": WORK_UNITS,
            "schematics_open": list(SCH.keys())}


if __name__ == "__main__":
    mcp.run()
