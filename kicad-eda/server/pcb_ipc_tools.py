"""Live PCB tools (KiCad IPC API via the vendored kipy) + create_pcb.

Edits of the board OPEN in Pcbnew: move/remove footprints, traces, vias,
zones, placement checks. KiCad 9/10 cannot instantiate a footprint from a
library over IPC (only Update PCB from Schematic / F8 does), so
add_footprint_to_pcb only positions footprints that already exist. When the
API is unreachable every tool returns {"ipc_connected": false} and the
check-ipc PreToolUse hook blocks the call before it reaches the server.

Plain functions; kicad_server registers everything in TOOLS with FastMCP.
"""

from __future__ import annotations

from pathlib import Path

import kicad_ipc as ipc
import toolchain as tc

_LAYER_ALIASES = {
    "F.Cu": "BL_F_Cu", "B.Cu": "BL_B_Cu",
    "In1.Cu": "BL_In1_Cu", "In2.Cu": "BL_In2_Cu",
    "In3.Cu": "BL_In3_Cu", "In4.Cu": "BL_In4_Cu",
}


# --- kipy helpers (resolve layers, nets, geometry) --------------------------
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


# --- tools ------------------------------------------------------------------
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
    # one IPC connection, one path-resolution rule (the same holding_kind the
    # write guards use, so a relative board_filename resolves via its project)
    k = ipc.connect()
    kind = ipc.holding_kind(k, target)
    if kind == "pcb":
        return _live_board_report(k, target)
    if Path(target).is_file() and not overwrite:
        return {"ok": False, "created": False, "pcb": target,
                "reason": f"{target} already exists; pass overwrite=true to replace it"}
    if kind is not None:
        return ipc.write_refusal(target, kind)
    kpy = tc.kicad_python()
    if not kpy:
        return {"ok": False, "reason": f"KiCad bundled Python (pcbnew) not found: "
                                       f"{tc.kicad_python_error()}"}
    Path(target).parent.mkdir(parents=True, exist_ok=True)
    # -c code has no script dir on sys.path: argv[2] (server/tools) goes there
    # so kicad_quiet runs before pcbnew (no modal wx dialogs, D0)
    code = ("import sys\n"
            "sys.path.insert(0, sys.argv[2])\n"
            "import kicad_quiet\n"
            "import pcbnew\n"
            "board = pcbnew.CreateEmptyBoard()\n"
            "pcbnew.SaveBoard(sys.argv[1], board)\n")
    r = tc.run([kpy, "-c", code, target, str(tc.TOOLS_DIR)], timeout=120)
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


def get_pcb_summary() -> dict:
    """Summarize the open board: footprint count, nets, layer count, size."""
    k = ipc.board_session()
    if "board" not in k:
        return k
    fps = k["board"].get_footprints()
    return {"ipc_connected": True, "footprints": len(fps),
            "refs": [f.reference_field.text.value for f in fps][:200]}


def add_footprint_to_pcb(ref: str, lib_id: str, x: float, y: float,
                         rot: float = 0, net_assignments: dict | None = None) -> dict:
    """Place a footprint on the board. NOTE: KiCad 9's IPC API cannot instantiate a
    footprint from a library — footprints reach the board from the schematic netlist
    via Tools > Update PCB from Schematic (F8). Use move_footprint to position one that
    is already on the board."""
    k = ipc.board_session()
    if "board" not in k:
        return k
    if _fp_by_ref(k["board"], ref) is not None:
        return move_footprint(ref, x, y, rot)
    return {"ipc_connected": True, "ok": False, "ref": ref,
            "reason": "KiCad 9 IPC cannot create a footprint from a library id. "
                      "Run Tools > Update PCB from Schematic (F8) in KiCad to import "
                      "footprints from the netlist, then call move_footprint to place them."}


def add_footprints_batch(footprints: list[dict]) -> dict:
    """Position many already-imported footprints in one pass. Each dict: ref, x, y, [rot].

    Footprints must already be on the board (imported via Update PCB from Schematic);
    this moves each to its target location."""
    k = ipc.board_session()
    if "board" not in k:
        return k
    from kipy.board_types import Vector2, Angle
    board = k["board"]
    moved, missing, items = [], [], []
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


def move_footprint(ref: str, x: float, y: float, rot: float = 0) -> dict:
    """Move a placed footprint to (x,y) mm with rotation (degrees). Requires IPC."""
    k = ipc.board_session()
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


def remove_footprint(ref: str) -> dict:
    """Remove a footprint from the board by reference. Requires IPC."""
    k = ipc.board_session()
    if "board" not in k:
        return k
    board = k["board"]
    fp = _fp_by_ref(board, ref)
    if fp is None:
        return {"ipc_connected": True, "ok": False,
                "reason": f"No footprint {ref} on the board."}
    board.remove_items([fp])
    return {"ipc_connected": True, "ok": True, "ref": ref}


def get_footprint_pads(ref: str) -> dict:
    """Return pad numbers, nets, and absolute positions (mm) for a footprint. Requires IPC."""
    k = ipc.board_session()
    if "board" not in k:
        return k
    fp = _fp_by_ref(k["board"], ref)
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


def register_net(net_name: str) -> dict:
    """Confirm a net exists on the board before routing it. Nets originate from the
    schematic netlist; this reports presence rather than creating a bare net. Requires IPC."""
    k = ipc.board_session()
    if "board" not in k:
        return k
    exists = any(n.name == net_name for n in k["board"].get_nets())
    return {"ipc_connected": True, "ok": exists, "net": net_name, "exists": exists,
            "note": "" if exists else
            "Net not present yet — import it via Update PCB from Schematic (F8). "
            "A trace placed on an unknown net will be unconnected."}


def add_trace(net_name: str, layer: str, x1: float, y1: float,
              x2: float, y2: float, width: float = 0.25) -> dict:
    """Add a copper trace segment on a net/layer between two points (mm). Requires IPC."""
    k = ipc.board_session()
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


def add_via(net_name: str, x: float, y: float,
            drill: float = 0.3, diameter: float = 0.6) -> dict:
    """Add a via on a net at (x,y) mm. Requires IPC."""
    k = ipc.board_session()
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


def add_copper_zone(net_name: str, layer: str, polygon: list[list[float]]) -> dict:
    """Add a copper pour/zone on a net and layer bounded by a polygon (list of [x,y] mm). Requires IPC."""
    k = ipc.board_session()
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


def validate_placement() -> dict:
    """Check the current placement for courtyard/bounding-box overlaps. Requires IPC."""
    k = ipc.board_session()
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


TOOLS = (create_pcb, get_pcb_summary, add_footprint_to_pcb, add_footprints_batch,
         move_footprint, remove_footprint, get_footprint_pads, register_net, add_trace,
         add_via, add_copper_zone, validate_placement)
