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

Board outline (ROADMAP D2, F3-19): build resolves the netlist ``board``
block with outline_resolve.resolve_board (relative to the netlist directory;
a DXF is read here, in the venv) before anything is written or spawned, and
refuses ``{"ok": False, "reason": "board outline: ..."}`` when it cannot. A
non-legacy board gets the version-1 sidecar ``<pcb stem>.outline.json``
beside the board and ``KICAD_EDA_OUTLINE=<its absolute path>`` in the
builder's environment (swig_build_board then uses the sidecar's board block).
A legacy board (no block, or a plain w x h rectangle) keeps today's argv
exactly, gets no sidecar (a stale one is removed) and never inherits a stray
KICAD_EDA_OUTLINE. board_w / board_h (0 = from the netlist) go to the
builder unchanged; one > 0 that differs from the board block / outline bbox
by more than SIZE_TOL_MM is reported as ignored. Every spawned build reports
``outline`` = the builder's own record + {source, fingerprint, sidecar}.

Stackup (ADR D7.4, D7-23a): board_physics.ensure_artifact right before the
SWIG child (artifact, managed DRU, board-stem mirror; errors refuse) and
KICAD_EDA_STACKUP for it; apply_netclasses takes the plane nets from its roles.

toolchain and kicad_ipc are imported AS MODULES and their functions looked up
at call time, so tests that monkeypatch tc.run / ipc.write_guard reach this
code. server/tools is put on sys.path lazily (apply_netclasses, project_libs,
outline_resolve), never at import time.
"""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING

import board_physics
import kicad_ipc as ipc
import project_libs
import toolchain as tc

if TYPE_CHECKING:                  # server/tools: imported lazily by outline_module()
    from outline_resolve import ResolvedBoard

BUILD_TIMEOUT_S = 600              # the SWIG build child (D0: every KiCad child has one)
MAP_PREFIX = "kicad-eda-libmap-"   # the temp library map handed to the builder as argv[3]
FOOTPRINTS, SYMBOLS = "footprints", "symbols"   # library_map() keys = the D1 block keys
SIZE_TOL_MM = 0.01                 # board_w / board_h vs the outline bbox
SIZE_IGNORED = "board_w/board_h ignored: the board outline defines the board"
OutlinePlan = tuple["ResolvedBoard", list[str]]   # (the resolved board block, result warnings)


def build(netlist: str, board_w: float = 0, board_h: float = 0,
          out_pcb: str = "", footprints_dir: str = "",
          placement: str = "", pack_scale: float = 1.0) -> dict:
    """build_board_from_netlist's body: SWIG build of the placed board, then
    the net classes (see the tool's docstring in board_pipeline_tools and
    the module doc for the project libraries and the board outline)."""
    bad = size_refusal(board_w, board_h)
    if bad:
        return bad
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
    board = resolve_outline(design, netlist, board_w, board_h)
    if isinstance(board, dict):
        return board
    guard = ipc.write_guard(out)
    if guard:
        return guard
    held = release_board_session(out)
    if held.get("ok") is False:
        return held
    argv = [str(script), netlist, out, fps, str(board_w), str(board_h),
            *([placement] if placement else [])]
    return {**build_released(kpy, argv, libs, design, board, pack_scale), **held}


def build_released(kpy: str, argv: list[str], libs: project_libs.ProjectLibs, design: dict,
                   board: OutlinePlan, pack_scale: float) -> dict:
    """build's writes once no editor or session holds the board (argv[1:3] =
    netlist, out): the library tables, the outline sidecar, the stackup
    artifact, the SWIG child, then the net classes. A refusal on the way spawns
    nothing."""
    netlist, out = argv[1], argv[2]
    declared = declared_libraries(libs)
    tables = materialize_libraries(libs, design, out, bool(declared))
    if isinstance(tables, dict):
        return tables
    resolved, notes = board
    placed = place_sidecar(resolved, out)
    if isinstance(placed, dict):
        return placed
    sidecar, stale = placed
    physics = board_physics.ensure_artifact(netlist, out, design=design)     # D7-23a
    if physics.get("ok") is False:
        return physics
    res = run_with_libraries(kpy, argv, libs, FOOTPRINTS in declared,
                             board_physics.builder_env(builder_env(pack_scale, sidecar), physics))
    res = {**res, "pcb": out, "libraries": project_libs.sources(libs, design),
           "library_tables": tables, "outline": outline_record(res, resolved, sidecar), **physics}
    res = with_warnings(res, [*notes, *stale])
    if res["ok"]:
        res = {**res, **apply_netclasses(netlist, out, res.get("zones", []),
                                         physics["physics"]["artifact"])}
    return res


# --- the board outline (ROADMAP D2) ---------------------------------------------------------
def outline_module() -> ModuleType:
    """server/tools/outline_resolve, imported on first use (module doc)."""
    if str(tc.TOOLS_DIR) not in sys.path:
        sys.path.insert(0, str(tc.TOOLS_DIR))
    import outline_resolve as orz
    return orz


def size_refusal(board_w: object, board_h: object) -> dict | None:
    """The refusal for a board_w / board_h that is not a finite number (the
    builder would crash on it or silently drop it), else None."""
    for name, value in (("board_w", board_w), ("board_h", board_h)):
        if isinstance(value, bool) or not isinstance(value, (int, float)) \
                or not math.isfinite(value):
            return {"ok": False, "reason": f"{name} must be a finite number of mm (0 = from "
                                           f"the netlist), got {value!r} (nothing was built)"}
    return None


def resolve_outline(design: dict, netlist: str, board_w: float,
                    board_h: float) -> OutlinePlan | dict:
    """(the resolved board block, its result warnings: the DXF reader's and
    the ignored board_w / board_h), or the refusal {ok: False, reason:
    "board outline: ..."} when outline_resolve cannot resolve the block."""
    orz = outline_module()
    try:
        resolved = orz.resolve_board(design, Path(netlist).absolute().parent)
    except orz.ResolveError as e:
        return {"ok": False, "reason": f"board outline: {e}"}
    notes = [f"board outline: {w}" for w in resolved.warnings]
    if resolved.outline is None:                # source "none": nothing defines the board
        return resolved, notes
    w, h = orz.outline_bbox_dims(resolved)
    off = any(given > 0 and abs(given - have) > SIZE_TOL_MM
              for given, have in ((board_w, w), (board_h, h)))
    if off:
        notes.append(f"{SIZE_IGNORED} ({w:g} x {h:g} mm; {board_w:g} x {board_h:g} mm "
                     "was given)")
    return resolved, notes


def place_sidecar(resolved: ResolvedBoard, out_pcb: str) -> tuple[str | None, list[str]] | dict:
    """(the absolute sidecar path, warnings) for the builder: a non-legacy
    board's sidecar written beside ``out_pcb``; None for a legacy board,
    whose stale sidecar from an earlier outline build is removed (a warning
    when that fails). A sidecar that cannot be written is a refusal."""
    orz = outline_module()
    path = os.path.abspath(orz.sidecar_path(out_pcb))
    if resolved.is_legacy:
        try:
            Path(path).unlink(missing_ok=True)
        except OSError as e:
            return None, [f"could not remove the stale outline sidecar {path} (not used by "
                          f"this build): {e}"]
        return None, []
    try:
        orz.write_sidecar(resolved, path)
    except OSError as e:
        return {"ok": False, "reason": f"cannot write the outline sidecar {path} (nothing "
                                       f"was built): {e}"}
    return path, []


def builder_env(pack_scale: float, sidecar: str | None) -> dict[str, str]:
    """The builder's environment: os.environ with PACK_SCALE, and
    KICAD_EDA_OUTLINE = the sidecar exactly when there is one (an inherited
    value is never passed on)."""
    var = outline_module().ENV_VAR
    base = {k: v for k, v in os.environ.items() if k.upper() != var}
    return {**base, "PACK_SCALE": str(pack_scale), **({var: sidecar} if sidecar else {})}


def outline_record(res: dict, resolved: ResolvedBoard, sidecar: str | None) -> dict:
    """The result's ``outline``: the builder's record (bbox, paper, datum,
    ...) when it reported one, plus {source, fingerprint, sidecar}."""
    built = res.get("outline")
    return {**(built if isinstance(built, dict) else {}), "source": resolved.source,
            "fingerprint": resolved.fingerprint, "sidecar": sidecar}


def with_warnings(res: dict, notes: list[str]) -> dict:
    """``res`` with ``notes`` appended to its warnings (the builder's first)."""
    if not notes:
        return res
    return {**res, "warnings": [*res.get("warnings", []), *notes]}


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
                       use_map: bool, env: dict[str, str]) -> dict:
    """run_builder on ``argv`` (script, netlist, out, stock footprints dir,
    ...) with ``env`` (builder_env). ``use_map``: argv[3] becomes the library
    map (project_libs.write_map) in a temp *.json, deleted in finally; a map
    that cannot be written is a refusal (nothing spawned), one that cannot be
    deleted a result warning."""
    if not use_map:
        return run_builder(kpy, argv, env)
    try:
        lib_map = write_temp_map(libs)
    except OSError as e:
        return {"ok": False, "reason": f"cannot write the library map for the build "
                                       f"(nothing was built): {e}"}
    try:
        res = run_builder(kpy, [*argv[:3], lib_map, *argv[4:]], env)
    finally:
        leftover = remove_temp_map(lib_map)
    return res if leftover is None else with_warnings(res, [leftover])


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


def run_builder(kpy: str, argv: list[str], env: dict[str, str]) -> dict:
    """swig_build_board (``argv`` = the script and its arguments) under
    KiCad's Python with ``env``: its result JSON with ok = rc 0 and the
    script's ok, plus the stderr tail; a child that cannot start or times
    out is a refusal."""
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


def apply_netclasses(netlist: str, out_pcb: str, zones: list[str],
                     artifact: str | None = None) -> dict:
    """Resolve electrical intent (net_specs + reliability class) into IPC-2221
    net classes and write them into the .kicad_pro so BOTH the Specctra DSN
    export (Freerouting routes power copper wider) and kicad-cli DRC honor
    per-net widths/clearances. Default class 3 (high-reliability) unless the
    netlist overrides "class". A netlist WITH net_specs whose classes cannot be
    written fails the build (ok False) — a board routed at default widths must
    never pass silently. A netlist without net_specs only reports the slip.
    With the stackup `artifact` (D7-21) its roles give the plane nets."""
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
        planes = () if artifact else tuple(z.split(":", 1)[1] for z in zones if ":" in z)
        planes += tuple(p["net"] for p in design.get("preroutes", []))
        nc = netclass_writer.apply_to_project(
            pro, design, int(design.get("class", 3)), plane_nets=planes, artifact=artifact)
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
