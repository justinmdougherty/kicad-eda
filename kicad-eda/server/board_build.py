"""Board build stage of the net-map pipeline: netlist.json -> placed
.kicad_pcb with copper planes (swig_build_board.py under KiCad's bundled
Python), then the IPC-2221 net classes written into the .kicad_pro.

build() is the body of the MCP tool board_pipeline_tools.build_board_from_netlist
(the tool keeps its name, signature and docstring there as a thin wrapper).
Split out of board_pipeline_tools.py by X-09; later owners locate code here by
function name.

toolchain and kicad_ipc are imported AS MODULES and their functions looked up
at call time, so tests that monkeypatch tc.run / ipc.write_guard reach this
code. server/tools is put on sys.path lazily (apply_netclasses), never at
import time.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import kicad_ipc as ipc
import toolchain as tc


def build(netlist: str, board_w: float, board_h: float,
          out_pcb: str = "", footprints_dir: str = "",
          placement: str = "", pack_scale: float = 1.0) -> dict:
    """build_board_from_netlist's body: SWIG build of the placed board, then
    the net classes (see the tool's docstring in board_pipeline_tools)."""
    kpy = tc.kicad_python()
    if not kpy:
        return {"ok": False, "reason": "KiCad bundled Python (with pcbnew) not found"}
    script = tc.TOOLS_DIR / "swig_build_board.py"
    if not script.is_file():
        return {"ok": False, "reason": f"missing {script}"}
    out = out_pcb or str(Path(netlist).with_suffix(".kicad_pcb"))
    fps = tc.footprints_dir(footprints_dir)
    if not fps:
        return {"ok": False, "reason": "footprints_dir not given and KiCad share dir not found"}
    guard = ipc.write_guard(out)
    if guard:
        return guard
    held = release_board_session(out)
    if held.get("ok") is False:
        return held
    args = [kpy, str(script), netlist, out, fps, str(board_w), str(board_h)]
    if placement:
        args.append(placement)
    env = {**os.environ, "PACK_SCALE": str(pack_scale)}
    r = tc.run(args, env=env)
    res = tc.last_json(r.stdout) or {"ok": False}
    res["ok"] = r.returncode == 0 and res.get("ok", False)
    res["pcb"] = out
    if r.stderr:
        res["stderr"] = r.stderr[-1200:]
    if res["ok"]:
        res.update(apply_netclasses(netlist, out, res.get("zones", [])))
    return {**res, **held}


def release_board_session(pcb: str) -> dict:
    """Before this module rewrites `pcb`: a board-API session that still holds
    it would keep the old board in memory and could save it over the new
    file. board_route_tools.release_for_rewrite closes an idle one (its
    released_boards fields go into the response) and refuses a dirty one."""
    import board_route_tools
    return board_route_tools.release_for_rewrite(pcb)


def apply_netclasses(netlist: str, out_pcb: str, zones: list[str]) -> dict:
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
        if str(tc.TOOLS_DIR) not in sys.path:
            sys.path.insert(0, str(tc.TOOLS_DIR))
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
