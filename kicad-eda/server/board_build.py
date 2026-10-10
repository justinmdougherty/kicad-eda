"""Board build stage of the net-map pipeline: netlist.json -> placed
.kicad_pcb with copper planes (swig_build_board.py under KiCad's bundled
Python), then the IPC-2221 net classes written into the .kicad_pro.

build() is the body of the MCP tool board_pipeline_tools.build_board_from_netlist
(the tool keeps its name, signature and docstring there as a thin wrapper).
Split out of board_pipeline_tools.py by X-09; later owners locate code here by
function name.

Project libraries (ROADMAP D1, F2-13): build loads them with project_libs
(netlist ``libraries`` block > project sym-lib-table / fp-lib-table beside the
netlist > stock) and refuses, before spawning or writing anything, when they
do not load. It merges the libraries the design uses into the board
directory's tables (``kicad-eda managed`` rows) with a minimal
``<stem>.kicad_pro``, so kicad-cli DRC resolves the FPIDs, and hands the
builder the library map (a temp *.json, deleted afterwards) as argv[3]. When
no block or project table declares a footprint library, argv[3] stays the
stock footprints directory (dir mode, as before). Every spawned build reports
``libraries`` (project_libs.sources) and ``library_tables`` (files written).
Relative netlist / board paths (the harness passes them as given) work the
same; a board directory that does not exist is refused before anything else.

toolchain and kicad_ipc are imported AS MODULES and their functions looked up
at call time, so tests that monkeypatch tc.run / ipc.write_guard reach this
code. server/tools is put on sys.path lazily (apply_netclasses, project_libs),
never at import time.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import kicad_ipc as ipc
import project_libs
import toolchain as tc

BUILD_TIMEOUT_S = 600              # the SWIG build child (D0: every KiCad child has one)
MAP_PREFIX = "kicad-eda-libmap-"   # the temp library map handed to the builder as argv[3]
FOOTPRINTS, SYMBOLS = "footprints", "symbols"   # library_map() keys = the D1 block keys


def build(netlist: str, board_w: float, board_h: float,
          out_pcb: str = "", footprints_dir: str = "",
          placement: str = "", pack_scale: float = 1.0) -> dict:
    """build_board_from_netlist's body: SWIG build of the placed board, then
    the net classes (see the tool's docstring in board_pipeline_tools and
    the module doc for the project libraries)."""
    kpy = tc.kicad_python()
    if not kpy:
        return {"ok": False, "reason": "KiCad bundled Python (with pcbnew) not found"}
    script = tc.TOOLS_DIR / "swig_build_board.py"
    if not script.is_file():
        return {"ok": False, "reason": f"missing {script}"}
    out = out_pcb or str(Path(netlist).with_suffix(".kicad_pcb"))
    if not Path(out).parent.is_dir():           # the builder never creates it
        return {"ok": False, "reason": f"output directory {Path(out).parent} does not exist "
                                       f"(nothing was built)"}
    fps = tc.footprints_dir(footprints_dir)
    if not fps:
        return {"ok": False, "reason": "footprints_dir not given and KiCad share dir not found"}
    loaded = load_libraries(netlist, fps)
    if isinstance(loaded, dict):
        return loaded
    libs, design = loaded
    guard = ipc.write_guard(out)
    if guard:
        return guard
    held = release_board_session(out)
    if held.get("ok") is False:
        return held
    declared = declared_libraries(libs)
    tables = materialize_libraries(libs, design, out, bool(declared))
    if isinstance(tables, dict):
        return {**tables, **held}
    argv = [str(script), netlist, out, fps, str(board_w), str(board_h),
            *([placement] if placement else [])]
    res = run_with_libraries(kpy, argv, libs, FOOTPRINTS in declared, pack_scale)
    res = {**res, "pcb": out, "libraries": project_libs.sources(libs, design),
           "library_tables": tables}
    if res["ok"]:
        res = {**res, **apply_netclasses(netlist, out, res.get("zones", []))}
    return {**res, **held}


def load_libraries(netlist: str,
                   stock_footprints: str) -> tuple[project_libs.ProjectLibs, dict] | dict:
    """(the project libraries, the design) of ``netlist`` with
    ``stock_footprints`` as the stock footprint dir, or the refusal envelope
    naming the problem (an unreadable netlist, a malformed block or table, a
    declared library that is missing, a block-vs-project-table conflict)."""
    try:
        design = json.loads(Path(netlist).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as e:
        return {"ok": False, "reason": f"cannot read netlist {netlist}: {e}"}
    if not isinstance(design, dict):
        return {"ok": False, "reason": f"netlist {netlist} is not a JSON object"}
    libs = project_libs.load(netlist, design, footprints_dir=stock_footprints)
    if not libs.ok:
        return {"ok": False, "reason": f"the project libraries of {netlist} are not usable "
                                       f"(D1; nothing was built): {libs.error}"}
    return libs, design


def declared_libraries(libs: project_libs.ProjectLibs) -> frozenset[str]:
    """The library kinds (FOOTPRINTS / SYMBOLS) a netlist block entry or a
    project table row declares; empty = every library is stock."""
    lib_map = libs.resolver.library_map() if libs.resolver is not None else {}
    return frozenset(key for key in (FOOTPRINTS, SYMBOLS) if lib_map.get(key))


def materialize_libraries(libs: project_libs.ProjectLibs, design: dict, out_pcb: str,
                          with_project: bool) -> list[str] | dict:
    """Merge the libraries the design uses into the board directory's
    sym-lib-table / fp-lib-table (managed rows; stale ones are cleared) and,
    ``with_project``, write a minimal <stem>.kicad_pro when none exists, so
    kicad-cli reads those tables. The paths written, or the refusal envelope
    (a clashing row in the board directory's table, an unwritable file).
    The project file goes by NAME: project_libs.materialize puts a relative
    kicad_pro in the (absolute) board directory, so a relative ``out_pcb``
    such as ``proj/netlist.kicad_pcb`` must not repeat its directory."""
    pro = Path(out_pcb).with_suffix(".kicad_pro").name if with_project else None
    try:
        return project_libs.materialize(libs, design, Path(out_pcb).parent, kicad_pro=pro)
    except (ValueError, OSError) as e:          # libtable.LibTableError is a ValueError
        return {"ok": False, "reason": f"cannot write the project library tables beside "
                                       f"{out_pcb} (nothing was built): {e}"}


def run_with_libraries(kpy: str, argv: list[str], libs: project_libs.ProjectLibs,
                       use_map: bool, pack_scale: float) -> dict:
    """run_builder on ``argv`` (script, netlist, out, stock footprints dir,
    ...). ``use_map``: argv[3] becomes the library map (project_libs.write_map)
    in a temp *.json, deleted in finally; a map that cannot be written is a
    refusal (nothing spawned), one that cannot be deleted a result warning."""
    if not use_map:
        return run_builder(kpy, argv, pack_scale)
    try:
        lib_map = write_temp_map(libs)
    except OSError as e:
        return {"ok": False, "reason": f"cannot write the library map for the build "
                                       f"(nothing was built): {e}"}
    try:
        res = run_builder(kpy, [*argv[:3], lib_map, *argv[4:]], pack_scale)
    finally:
        leftover = remove_temp_map(lib_map)
    return res if leftover is None else {**res, "warnings": [*res.get("warnings", []), leftover]}


def write_temp_map(libs: project_libs.ProjectLibs) -> str:
    """project_libs.write_map into a new temp *.json; its path (OSError when
    it cannot be created or written; no file is left behind then)."""
    handle, path = tempfile.mkstemp(prefix=MAP_PREFIX, suffix=project_libs.MAP_SUFFIX)
    os.close(handle)
    try:
        return project_libs.write_map(libs, path)
    except BaseException:
        Path(path).unlink(missing_ok=True)
        raise


def remove_temp_map(path: str) -> str | None:
    """Delete the temp library map; the reason when that fails (reported in
    the result: a leftover temp file must not discard a finished build)."""
    try:
        Path(path).unlink(missing_ok=True)
    except OSError as e:
        return f"could not delete the temporary library map {path}: {e}"
    return None


def run_builder(kpy: str, argv: list[str], pack_scale: float) -> dict:
    """swig_build_board (``argv`` = the script and its arguments) under
    KiCad's Python: its result JSON with ok = rc 0 and the script's ok, plus
    the stderr tail; a child that cannot start or times out is a refusal."""
    env = {**os.environ, "PACK_SCALE": str(pack_scale)}
    try:
        r = tc.run([kpy, *argv], env=env, timeout=BUILD_TIMEOUT_S, encoding="utf-8",
                   errors="replace")
    except subprocess.TimeoutExpired:
        return {"ok": False, "reason": f"the board build timed out after {BUILD_TIMEOUT_S} s"}
    except OSError as e:
        return {"ok": False, "reason": f"cannot run KiCad's Python {kpy}: {e}"}
    res = tc.last_json(r.stdout) or {"ok": False}
    res = {**res, "ok": r.returncode == 0 and bool(res.get("ok", False))}
    return {**res, "stderr": r.stderr[-1200:]} if r.stderr else res


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
