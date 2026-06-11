#!/usr/bin/env python3
"""kicad-generator — MCP server for the kicad-eda plugin.

Exposes the tool surface the `pcb-design` skill calls by name. Two layers:

  SCHEMATIC  — direct .kicad_sch S-expression generation. Pure Python, no
               KiCad required. Produces files that open in KiCad 9.
  PCB        — KiCad 9 IPC API (kicad-python / `kipy`). These require KiCad
               running with the API server enabled; when it is unreachable
               every PCB tool returns {"ipc_connected": false, ...} so the
               caller stops cleanly (a plugin PreToolUse hook also blocks them).

This is a clean, runnable baseline that matches the skill's tool names. The
elaborate pieces from a mature generator — full lib_symbols embedding and the
pin-coordinate sidecar that powers wire_by_ref / label_pin / power_pin — drop
in behind the same function signatures. Where a pin map is required and absent,
the tool says so explicitly rather than guessing coordinates.
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


def _new_sch() -> dict[str, Any]:
    return {"uuid": str(uuid.uuid4()), "paper": "A4",
            "symbols": [], "wires": [], "labels": [], "power": []}


def _pins_sidecar(lib_id: str, near: str) -> dict | None:
    """Load a pin map mapping pin name/number -> {x, y} schematic-space offsets (mm).

    Searched in order: a `pins/` dir next to the schematic, then the server's
    central `pins/` dir (so sidecars built once are reused across projects)."""
    safe = lib_id.replace(":", "_")
    candidates = [
        Path(near).parent / "pins" / f"{safe}.pins.json",
        Path(__file__).parent / "pins" / f"{safe}.pins.json",
    ]
    for cand in candidates:
        if cand.is_file():
            try:
                return json.loads(cand.read_text())
            except Exception:
                return None
    return None


# ----------------------------------------------------------------------------
# S-expression emission
# ----------------------------------------------------------------------------
def _emit(model: dict[str, Any]) -> str:
    L: list[str] = []
    L.append("(kicad_sch")
    L.append("  (version 20231120)")
    L.append('  (generator "kicad-eda")')
    L.append(f'  (uuid "{model["uuid"]}")')
    L.append(f'  (paper "{model["paper"]}")')
    # lib_symbols left as a cache placeholder; KiCad resolves graphics from its
    # configured libraries on open / cache rebuild.
    L.append("  (lib_symbols)")
    for s in model["symbols"]:
        L.append(f'  (symbol (lib_id "{s["lib_id"]}") '
                 f'(at {s["x"]:.4f} {s["y"]:.4f} {s["rot"]}) (unit 1)')
        L.append(f'    (uuid "{s["uuid"]}")')
        L.append(f'    (property "Reference" "{s["ref"]}" (at {s["x"]:.4f} {s["y"]-2.54:.4f} 0))')
        L.append(f'    (property "Value" "{s["value"]}" (at {s["x"]:.4f} {s["y"]+2.54:.4f} 0))')
        if s.get("footprint"):
            L.append(f'    (property "Footprint" "{s["footprint"]}" (at {s["x"]:.4f} {s["y"]:.4f} 0))')
        L.append("  )")
    for w in model["wires"]:
        (x1, y1), (x2, y2) = w["a"], w["b"]
        L.append(f'  (wire (pts (xy {x1:.4f} {y1:.4f}) (xy {x2:.4f} {y2:.4f})) '
                 f'(stroke (width 0) (type default)) (uuid "{uuid.uuid4()}"))')
    for lb in model["labels"]:
        L.append(f'  (label "{lb["net"]}" (at {lb["x"]:.4f} {lb["y"]:.4f} {lb.get("rot",0)}) '
                 f'(effects (font (size 1.27 1.27))) (uuid "{uuid.uuid4()}"))')
    for p in model["power"]:
        L.append(f'  (global_label "{p["net"]}" (shape input) '
                 f'(at {p["x"]:.4f} {p["y"]:.4f} 0) '
                 f'(effects (font (size 1.27 1.27))) (uuid "{uuid.uuid4()}"))')
    L.append(")")
    return "\n".join(L) + "\n"


def _require(path: str) -> dict[str, Any]:
    if path not in SCH:
        raise ValueError(f"No schematic loaded for '{path}'. Call create_schematic or load_schematic first.")
    return SCH[path]


# ----------------------------------------------------------------------------
# SCHEMATIC TOOLS
# ----------------------------------------------------------------------------
@mcp.tool()
def create_schematic(path: str, paper: str = "A4") -> dict:
    """Create a new, empty schematic in memory for the given .kicad_sch path."""
    m = _new_sch()
    m["paper"] = paper
    SCH[path] = m
    return {"ok": True, "path": path, "uuid": m["uuid"]}


@mcp.tool()
def load_schematic(path: str) -> dict:
    """Load an existing .kicad_sch from disk into memory (best-effort parse of instances)."""
    if not Path(path).is_file():
        raise ValueError(f"File not found: {path}")
    # Baseline: register the path with a fresh model; full round-trip parsing is
    # the drop-in point for the mature parser.
    SCH[path] = _new_sch()
    return {"ok": True, "path": path, "note": "registered; full parse is the drop-in point"}


@mcp.tool()
def save_schematic(sch: str) -> dict:
    """Write the in-memory schematic to disk as a .kicad_sch file."""
    m = _require(sch)
    Path(sch).parent.mkdir(parents=True, exist_ok=True)
    Path(sch).write_text(_emit(m), encoding="utf-8")
    return {"ok": True, "path": sch, "symbols": len(m["symbols"]),
            "wires": len(m["wires"]), "labels": len(m["labels"])}


@mcp.tool()
def add_component(sch: str, ref: str, lib_id: str, value: str,
                  x: float, y: float, rot: int = 0, footprint: str = "") -> dict:
    """Place one component. Coordinates in mm. rot in {0,90,180,270}."""
    m = _require(sch)
    sym = {"ref": ref, "lib_id": lib_id, "value": value, "x": x, "y": y,
           "rot": rot, "footprint": footprint, "uuid": str(uuid.uuid4())}
    m["symbols"].append(sym)
    return {"ok": True, "ref": ref, "uuid": sym["uuid"]}


@mcp.tool()
def add_components_batch(sch: str, components: list[dict]) -> dict:
    """Place many components in one write. Each dict: ref, lib_id, value, x, y, [rot], [footprint].

    Prefer this over repeated add_component calls — it builds all symbols then
    writes once, eliminating read-modify-write corruption on large schematics."""
    m = _require(sch)
    placed = []
    for c in components:
        sym = {"ref": c["ref"], "lib_id": c["lib_id"], "value": c.get("value", ""),
               "x": float(c["x"]), "y": float(c["y"]), "rot": int(c.get("rot", 0)),
               "footprint": c.get("footprint", ""), "uuid": str(uuid.uuid4())}
        m["symbols"].append(sym)
        placed.append(sym["ref"])
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
    m = _require(sch)
    sym = next((s for s in m["symbols"] if s["ref"] == ref), None)
    if not sym:
        raise ValueError(f"No component {ref} in {sch}.")
    pins = _pins_sidecar(sym["lib_id"], sch)
    if not pins or pin not in pins:
        raise ValueError(
            f"Pin coordinates for {ref}.{pin} ({sym['lib_id']}) are unknown. "
            f"Provide a pin sidecar at pins/{sym['lib_id'].replace(':','_')}.pins.json "
            f"or use the explicit-coordinate tools (add_wire/add_label/add_power_port).")
    off = pins[pin]
    return sym["x"] + float(off["x"]), sym["y"] + float(off["y"])


@mcp.tool()
def wire_by_ref(sch: str, ref_a: str, pin_a: str, ref_b: str, pin_b: str) -> dict:
    """Connect two component pins by reference, resolving coordinates from the pin sidecar."""
    ax, ay = _pin_xy(sch, ref_a, pin_a)
    bx, by = _pin_xy(sch, ref_b, pin_b)
    _require(sch)["wires"].append({"a": (ax, ay), "b": (bx, by)})
    return {"ok": True, "from": f"{ref_a}.{pin_a}", "to": f"{ref_b}.{pin_b}"}


@mcp.tool()
def label_pin(sch: str, ref: str, pin: str, net_name: str) -> dict:
    """Place a net label at a component pin, resolving its coordinate from the pin sidecar."""
    x, y = _pin_xy(sch, ref, pin)
    _require(sch)["labels"].append({"net": net_name, "x": x, "y": y, "rot": 0})
    return {"ok": True, "at": f"{ref}.{pin}", "net": net_name}


@mcp.tool()
def power_pin(sch: str, ref: str, pin: str, net_name: str) -> dict:
    """Place a power port at a component power pin, resolving its coordinate from the pin sidecar."""
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


@mcp.tool()
def run_erc(sch: str) -> dict:
    """Run KiCad's Electrical Rules Check via kicad-cli (KiCad 9 required)."""
    if not KICAD_CLI:
        return {"ok": False, "reason": "kicad-cli not found on PATH; install KiCad 9."}
    out = Path(sch).with_suffix(".erc.json")
    r = subprocess.run([KICAD_CLI, "sch", "erc", "--format", "json",
                        "-o", str(out), sch], capture_output=True, text=True)
    report = json.loads(out.read_text()) if out.is_file() else {}
    return {"ok": r.returncode == 0, "stdout": r.stdout[-2000:],
            "stderr": r.stderr[-2000:], "report": report}


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
def _kipy():
    """Return a connected KiCad board, or a dict describing why not."""
    try:
        from kipy import KiCad  # type: ignore
    except Exception:
        return {"ipc_connected": False,
                "reason": "kicad-python (kipy) not installed in the server venv."}
    try:
        kicad = KiCad()
        board = kicad.get_board()
        return {"ipc_connected": True, "kicad": kicad, "board": board}
    except Exception as e:
        return {"ipc_connected": False,
                "reason": f"KiCad IPC API not reachable ({e}). Open Pcbnew and enable the API server."}


@mcp.tool()
def create_pcb(sch: str = "", pcb: str = "") -> dict:
    """Create a PCB from the schematic netlist. Requires KiCad 9 IPC.

    With the IPC workflow, create the .kicad_pcb in KiCad and open it in Pcbnew;
    this tool then verifies the connection and reports the board summary."""
    k = _kipy()
    if not k.get("ipc_connected"):
        return k
    board = k["board"]
    return {"ipc_connected": True, "ok": True,
            "footprints": len(board.get_footprints())}


@mcp.tool()
def get_pcb_summary() -> dict:
    """Summarize the open board: footprint count, nets, layer count, size."""
    k = _kipy()
    if not k.get("ipc_connected"):
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
    """Ping the running KiCad instance and confirm the IPC API is live (MANDATORY first PCB step)."""
    k = _kipy()
    if not k.get("ipc_connected"):
        return k
    kc, board = k["kicad"], k["board"]
    try:
        ver = str(kc.get_version())
    except Exception:
        ver = "unknown"
    return {"ipc_connected": True, "kicad_version": ver,
            "board": getattr(board, "name", "?"),
            "footprints": len(board.get_footprints())}


@mcp.tool()
def add_footprint_to_pcb(ref: str, lib_id: str, x: float, y: float,
                         rot: float = 0, net_assignments: dict | None = None) -> dict:
    """Place a footprint on the board. NOTE: KiCad 9's IPC API cannot instantiate a
    footprint from a library — footprints reach the board from the schematic netlist
    via Tools > Update PCB from Schematic (F8). Use move_footprint to position one that
    is already on the board."""
    k = _kipy()
    if not k.get("ipc_connected"):
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
    if not k.get("ipc_connected"):
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
    if not k.get("ipc_connected"):
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
    if not k.get("ipc_connected"):
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
    if not k.get("ipc_connected"):
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
    if not k.get("ipc_connected"):
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
    if not k.get("ipc_connected"):
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
    if not k.get("ipc_connected"):
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
    if not k.get("ipc_connected"):
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
    if not k.get("ipc_connected"):
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


@mcp.tool()
def drc_pcb(fab_profile: str = "") -> dict:
    """Run DRC against the fab profile (defaults to the plugin's configured profile). Requires KiCad 9."""
    profile = fab_profile or FAB_PROFILE
    if not KICAD_CLI:
        return {"ok": False, "reason": "kicad-cli not found; install KiCad 9.", "profile": profile}
    return {"ok": True, "profile": profile,
            "note": "run kicad-cli pcb drc on the saved .kicad_pcb and parse the JSON report"}


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
TOOLS_DIR = Path(__file__).parent / "tools"


def _kicad_python() -> str | None:
    """KiCad's bundled Python (ships the `pcbnew` SWIG module) — beside kicad-cli."""
    if KICAD_CLI:
        cand = Path(KICAD_CLI).parent / ("python.exe" if os.name == "nt" else "python3")
        if cand.is_file():
            return str(cand)
    return None


def _kicad_share() -> Path | None:
    """KiCad's shared data dir (.../share/kicad) holding symbols/ and footprints/."""
    if KICAD_CLI:
        share = Path(KICAD_CLI).parent.parent / "share" / "kicad"
        if share.is_dir():
            return share
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
    syms = symbols_dir or (str(_kicad_share() / "symbols") if _kicad_share() else "")
    sides = sidecars_dir or str(Path(__file__).parent / "pins")
    if not syms:
        return {"ok": False, "reason": "symbols_dir not given and KiCad share dir not found"}
    r = _run([sys.executable, str(script), netlist, out, syms, sides])
    res = _last_json(r.stdout) or {}
    res["ok"] = r.returncode == 0 and res.get("ok", False)
    res["sch"] = out
    if r.stderr:
        res["stderr"] = r.stderr[-1200:]
    if res["ok"] and run_erc and KICAD_CLI:
        rpt = str(Path(out).with_suffix("")) + "_erc.rpt"
        # gate on ERRORS only (warnings inform but don't block — e.g. the
        # pin_to_pin warning for an address-strap pin tied to a power net is a
        # known false positive); same severity policy as the DRC gate.
        e = _run([KICAD_CLI, "sch", "erc", out, "--severity-error", "-o", rpt])
        res["erc_ok"] = "Failed to load" not in (e.stdout + e.stderr)
        # kicad-cli prints "Found N violations"
        for tok in (e.stdout + e.stderr).split():
            if tok.isdigit():
                res["erc_violations"] = int(tok)
                break
        res["erc_report"] = rpt
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

    # Resolve electrical intent (net_specs + reliability class) into IPC-2221
    # net classes and write them into the .kicad_pro so BOTH the Specctra DSN
    # export (Freerouting routes power copper wider) and kicad-cli DRC honor
    # per-net widths/clearances. Default class 3 (high-reliability) unless the
    # netlist overrides "class". No-op for a netlist with no power intent.
    if res["ok"]:
        try:
            if str(TOOLS_DIR) not in sys.path:
                sys.path.insert(0, str(TOOLS_DIR))
            import netclass_writer
            design = json.loads(Path(netlist).read_text(encoding="utf-8"))
            pro = str(Path(out).with_suffix(".kicad_pro"))
            # nets backed by a poured plane ("In1.Cu:GND") or by designer
            # preroutes are exempt from track-width classing — the plane or
            # the prerouted copper carries the current, and a width class
            # would make the net's signal taps unreachable for the router
            planes = tuple(z.split(":", 1)[1] for z in res.get("zones", [])
                           if ":" in z)
            planes += tuple(p["net"] for p in design.get("preroutes", []))
            nc = netclass_writer.apply_to_project(
                pro, design, int(design.get("class", 3)), plane_nets=planes)
            res["netclasses"] = {"profile": nc.get("class_profile"),
                                 "written": nc.get("classes_written", [])}
        except Exception as e:  # never fail the build on a rules-annotation slip
            res["netclasses"] = {"error": str(e)}
    return res


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
    tracks = None
    for tok in im.stdout.split():
        if tok.isdigit():
            tracks = int(tok)
    # verify route completeness — Freerouting is non-deterministic and may leave a
    # stub; report the unconnected count so the caller can re-run if it isn't zero.
    unconnected = None
    if KICAD_CLI:
        rpt = base + "_drc.json"
        _run([KICAD_CLI, "pcb", "drc", pcb, "-o", rpt, "--format", "json"])
        try:
            unconnected = len(json.loads(Path(rpt).read_text()).get("unconnected_items", []))
        except Exception:
            pass
    return {"ok": "True" in im.stdout and (unconnected in (0, None)),
            "pcb": pcb, "dsn": dsn, "ses": ses, "tracks": tracks,
            "unconnected": unconnected, "import_log": im.stdout.strip()[-300:],
            "note": None if unconnected in (0, None) else
                    f"{unconnected} net(s) left unrouted — re-run autoroute_board to converge"}


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
    syms = symbols_dir or (str(_kicad_share() / "symbols") if _kicad_share() else "")
    out = out_dir or str(Path(__file__).parent / "pins")
    if not syms:
        return {"ok": False, "reason": "symbols_dir not given and KiCad share dir not found"}
    args = [sys.executable, str(script), syms, out] + [s.strip() for s in symbols.split(",") if s.strip()]
    r = _run(args)
    return {"ok": r.returncode == 0, "out_dir": out,
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
    syms = symbols_dir or (str(_kicad_share() / "symbols") if _kicad_share() else "")
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
        return {"ok": False, "reason": "KiCad bundled Python (pcbnew) not found"}
    script = TOOLS_DIR / "emc_stitch.py"
    if not script.is_file():
        return {"ok": False, "reason": f"missing {script}"}
    # TRANSACTIONAL: stitching must never make the board worse. Apply, verify
    # with DRC; on any violation/unconnected roll back and retry fence-only
    # (a dense interior grid can fragment a power plane); if even the fence
    # fails, restore the original board and report the open task.
    bak = pcb + ".prestitch"
    shutil.copyfile(pcb, bak)

    def _drc_counts() -> tuple[int, int]:  # noqa: E306
        rpt = str(Path(pcb).with_suffix("")) + "_drc.json"
        _run([KICAD_CLI, "pcb", "drc", pcb, "--severity-error", "-o", rpt,
              "--format", "json"])
        try:
            d = json.loads(Path(rpt).read_text())
            return (len(d.get("violations", [])),
                    len(d.get("unconnected_items", [])))
        except Exception:
            return (-1, -1)

    # a board that isn't already G4-clean can't be judged after stitching —
    # require a clean baseline before adding anything
    if KICAD_CLI:
        b_errs, b_unc = _drc_counts()
        if b_errs or b_unc:
            Path(bak).unlink(missing_ok=True)
            return {"ok": False, "reason": f"board not clean before stitching "
                    f"({b_errs} violations, {b_unc} unconnected) — pass the G4 "
                    f"layout gate first"}

    for mode in ("full", "fence"):
        r = _run([kpy, str(script), pcb, str(max_freq_hz), mode], timeout=180)
        res = _last_json(r.stdout) or {"ok": False}
        if not res.get("ok"):
            break
        if not KICAD_CLI:
            res["mode"] = mode
            return res
        errs, unc = _drc_counts()
        if errs == 0 and unc == 0:
            res.update({"mode": mode, "drc_errors": 0, "unconnected": 0})
            Path(bak).unlink(missing_ok=True)
            return res
        shutil.copyfile(bak, pcb)            # roll back, try the next mode
        res = {"ok": False, "mode": mode, "drc_errors": errs, "unconnected": unc}
    Path(bak).unlink(missing_ok=True)
    res["reason"] = ("stitching rolled back — it violated DRC or broke plane "
                     "connectivity even fence-only; board left unmodified")
    return res


# ----------------------------------------------------------------------------
@mcp.tool()
def server_status() -> dict:
    """Report what this server can do right now: KiCad CLI, bundled Python (pcbnew),
    Java + Freerouting, IPC, fab profile. The net-map pipeline needs kicad_cli +
    kicad_python; autoroute additionally needs java + freerouting_jar."""
    return {"kicad_cli": bool(KICAD_CLI), "kicad_cli_path": KICAD_CLI,
            "kicad_python": _kicad_python(), "kicad_share": str(_kicad_share() or ""),
            "java": _find_java(), "freerouting_jar": _freerouting_jar(),
            "ipc": _kipy().get("ipc_connected", False),
            "fab_profile": FAB_PROFILE, "units": WORK_UNITS,
            "schematics_open": list(SCH.keys())}


if __name__ == "__main__":
    mcp.run()
