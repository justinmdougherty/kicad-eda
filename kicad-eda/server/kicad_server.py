#!/usr/bin/env python3
"""kicad-generator — MCP server for the kicad-eda plugin.

Exposes the tool surface the `pcb-design` / `netlist-pipeline` skills call by
name. This module is the entry point, the schematic SESSION REGISTRY and the
status tools; the other tool families live beside it and are registered here.

  SCHEMATIC tools (this module) run on one of two backends through ONE
      dispatch path, chosen per session at create/load ("auto" = api when
      available); every response carries "backend":
        "api"  — KiCad 11's IPC schematic API (server/backends/sch_api.py on
                 the vendored kipy): KiCad places the symbols, wires, labels
                 and writes the file. Headless (kicad-cli api-server, ONE
                 document at a time) or IN a running KiCad 11 GUI that has
                 the file open (mode "gui").
        "file" — the in-memory file model (server/sch_file_model.py), the
                 fallback whenever the API is unavailable.
  PCB: live IPC tools (pcb_ipc_tools.py) and the headless board pipeline
      (board_pipeline_tools.py: kicad-cli, KiCad's bundled Python,
      Freerouting). build_schematic_from_netlist (here) uses the API backend
      when available, else server/tools/sch_build.py.
  TOOLCHAIN (kicad_env + toolchain.py): newest kicad-cli for schematics, the
      pcbnew install's kicad-cli for boards (KICAD_CLI_POLICY), KiCad 11 api-server.
  OPEN-DOCUMENT GUARD (kicad_ipc.py): writes refuse a file open in a KiCad
      editor, except a schematic in a KiCad 11 GUI (edited there in place).

pipeline/run_pipeline.py imports this module as `k` and uses k._kicad_python(),
k._kicad_share() and the build_* / autoroute_board / export_fab_package tools;
those names are kept here as thin wrappers (k.KICAD_CLI = the BOARD kicad-cli).
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SERVER_DIR = Path(__file__).resolve().parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import kicad_env as env                     # noqa: E402

env.use_vendored_kipy()                     # server/vendor FIRST, before any `import kipy`

import board_pipeline_tools                 # noqa: E402
import kicad_ipc as ipc                     # noqa: E402
import pcb_ipc_tools                        # noqa: E402
import sch_file_model as sfm                # noqa: E402
import toolchain as tc                      # noqa: E402
from board_pipeline_tools import (          # noqa: E402,F401  (re-exported for the pipeline)
    add_ground_stitching, autoroute_board, build_board_from_netlist,
    check_design_gates, drc_pcb, export_fab_package, export_gerbers, render_pcb)
from mcp.server.fastmcp import FastMCP     # noqa: E402
from pcb_ipc_tools import create_pcb        # noqa: E402,F401

mcp = FastMCP("kicad-generator")

TOOLS_DIR = tc.TOOLS_DIR
PINS_DIR = tc.PINS_DIR
FAB_PROFILE = tc.FAB_PROFILE
WORK_UNITS = tc.WORK_UNITS
BACKENDS = ("auto", "api", "file")
KICAD_CLI_POLICY = tc.KICAD_CLI_POLICY      # schematics: newest cli; boards: the pcbnew install's


# ----------------------------------------------------------------------------
# Toolchain wrappers (kicad_env / toolchain do the work; the names are kept
# for pipeline/run_pipeline.py and run_all.py)
# ----------------------------------------------------------------------------
def _find_kicad_cli() -> str | None:
    """Newest kicad-cli (schematic ERC / render); boards use tc.board_cli()."""
    return tc.kicad_cli()


def _kicad_python() -> str | None:
    """KiCad's bundled Python, VERIFIED to import pcbnew (newest install that has one)."""
    return tc.kicad_python()


def _kicad_share() -> Path | None:
    """The newest install's share/kicad (symbols/ under it are the ones used)."""
    return tc.kicad_share()


def _symbols_dir(explicit: str = "") -> str | None:
    return tc.symbols_dir(explicit)


def __getattr__(name: str):
    """k.KICAD_CLI (kept for older callers) is the BOARD kicad-cli — every use of
    it drives DRC / render on a pcbnew-built board. Lazy (PEP 562): importing
    never probes the installed KiCads."""
    if name == "KICAD_CLI":
        return tc.board_cli()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# ----------------------------------------------------------------------------
# Schematic API backend (server/backends/sch_api.py), imported lazily so the
# server starts — on the file backend — when the module or kipy is missing
# ----------------------------------------------------------------------------
_API: dict[str, Any] = {"checked": False, "module": None, "reason": None}


def _api_module():
    if not _API["checked"]:
        _API["checked"] = True
        try:
            import backends.sch_api as mod  # noqa: E402
            _API["module"] = mod
        except Exception as e:  # ImportError, or a dependency of kipy missing
            _API["reason"] = f"schematic API backend not importable: {type(e).__name__}: {e}"
    return _API["module"]


def _api_available() -> bool:
    """KiCad 11 api-server present AND the vendored kipy imports. Tests patch this."""
    mod = _api_module()
    if mod is None:
        return False
    try:
        ok = bool(mod.SchematicApi.available())
        why = getattr(mod.SchematicApi, "unavailable_reason", lambda: None)()
    except Exception as e:
        _API["reason"] = f"SchematicApi.available() failed: {type(e).__name__}: {e}"
        return False
    if not ok:
        _API["reason"] = why or "no KiCad 11 install with `kicad-cli api-server` found"
    return ok


def _api_kicad_version() -> str | None:
    inst = env.pick_api()
    return inst.version if inst else None


def _backend_errors() -> tuple[type[BaseException], ...]:
    """Actionable backend failures -> {"ok": false, "reason"}; anything else propagates."""
    mod = _api_module()
    api_err = getattr(mod, "SchematicApiError", None) if mod else None
    return (ValueError, api_err) if api_err else (ValueError,)


def _open_api(path: str, **kw) -> tuple[Any, dict | None]:
    """SchematicApi.open(path, **kw): (session, None) or (None, error response)."""
    try:
        return _api_module().SchematicApi.open(path, **kw), None
    except Exception as e:  # SchematicApiError (actionable) or a launch failure
        return None, {"ok": False, "backend": "api", "path": path,
                      "reason": f"{type(e).__name__}: {e}"}


def _pick_backend(requested: str) -> str:
    """'auto' -> api when available else file; 'api' insists (ValueError); 'file' forces."""
    if requested not in BACKENDS:
        raise ValueError(f"backend must be one of {BACKENDS}, got '{requested}'")
    if requested == "file":
        return "file"
    if _api_available():
        return "api"
    if requested == "api":
        raise ValueError(f"schematic API backend unavailable: {_API['reason']}")
    return "file"


# ----------------------------------------------------------------------------
# Session registry: one backend object per schematic path, one dispatch path
# ----------------------------------------------------------------------------
@dataclass
class Session:
    path: str           # as given by the caller
    backend: str        # "api" | "file"
    impl: Any           # SchematicApi | sch_file_model.FileSchematic
    dirty: bool = False  # mutated since the last save

    @property
    def mode(self) -> str:
        """'headless' | 'gui' for the API backend, 'file' for the file model."""
        return str(getattr(self.impl, "mode", self.backend))


_SESSIONS: dict[str, Session] = {}      # normcase(abspath) -> Session
_MUTATORS = frozenset({"place_symbol", "set_fields", "add_wire", "wire_pins", "add_label",
                       "label_pin", "add_global_label", "power_pin", "no_connect", "add_junction"})


def _key(path: str) -> str:
    return ipc.norm_path(path)


def _session(sch: str) -> Session:
    sess = _SESSIONS.get(_key(sch))
    if sess is None:
        raise ValueError(f"No schematic loaded for '{sch}'. Call create_schematic or "
                         "load_schematic first.")
    return sess


def _forget(key: str) -> str | None:
    """Drop a session and release its backend; the close error text, if any."""
    sess = _SESSIONS.pop(key, None)
    if sess is None:
        return None
    try:
        sess.impl.close()
    except Exception as e:  # a dead headless server must not wedge the registry
        return f"{type(e).__name__}: {e}"
    return None


def _fail(sess: Session, reason: str) -> dict:
    return {"ok": False, "backend": sess.backend, "path": sess.path, "reason": reason}


def _call(sess: Session, method: str, *args, **kw) -> tuple[Any, dict | None]:
    """One backend call: (result, None) or (None, error response); tracks unsaved edits."""
    fn = getattr(sess.impl, method, None)
    if fn is None:
        return None, _fail(sess, f"{method} is not supported by the {sess.backend} backend")
    try:
        result = fn(*args, **kw)
    except _backend_errors() as e:
        return None, _fail(sess, str(e))
    if method in _MUTATORS:
        sess.dirty = True
    elif method in ("save", "save_as"):
        sess.dirty = False
    return result, None


def _dispatch(sch: str, method: str, *args, **kw) -> dict:
    """Every schematic tool's path: lookup, call, backend stamp (non-dict -> `result`)."""
    sess = _session(sch)
    result, err = _call(sess, method, *args, **kw)
    if err:
        return err
    fields = result if isinstance(result, dict) else {"result": result}
    return {"ok": True, "backend": sess.backend, **fields}


def _close_error_fields(error: str | None) -> dict:
    """A session that did not stop cleanly is reported, never dropped silently."""
    return {"previous_session_close_error": error} if error else {}


@dataclass(frozen=True)
class Release:
    """What _release_headless did: the refusal (a dirty session), the paths
    whose idle headless sessions were dropped, and any close errors; fields()
    are the extra keys for the response of the tool that triggered it."""
    blocked: dict | None = None
    released: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()

    def fields(self) -> dict:
        out: dict = {}
        if self.released:
            out["released_sessions"] = list(self.released)
            out["released_note"] = ("the headless api-server holds one schematic at a time; "
                                    "load_schematic a released path again to keep editing it")
        if self.errors:
            out["released_session_close_errors"] = list(self.errors)
        return out


def _release_headless(keep: str | None = None) -> Release:
    """The headless api-server holds ONE document at a time: drop idle headless
    sessions before opening another; refuse (naming it) while one is dirty."""
    released: list[str] = []
    errors: list[str] = []
    for key, sess in list(_SESSIONS.items()):
        if key == keep or sess.backend != "api" or sess.mode != "headless":
            continue
        if sess.dirty:
            blocked = _fail(sess, f"{sess.path} is open in the headless KiCad session with "
                                  "unsaved changes; save_schematic or close_schematic it "
                                  "first (the api-server holds one schematic at a time)")
            return Release(blocked=blocked, released=tuple(released), errors=tuple(errors))
        error = _forget(key)
        released.append(sess.path)
        if error:
            errors.append(f"{sess.path}: {error}")
    return Release(released=tuple(released), errors=tuple(errors))


def _backend_of(sch: str) -> str:
    sess = _SESSIONS.get(_key(sch))
    return sess.backend if sess else "file"


# ----------------------------------------------------------------------------
# SCHEMATIC TOOLS
# ----------------------------------------------------------------------------
@mcp.tool()
def create_schematic(path: str, paper: str = "A4", backend: str = "auto") -> dict:
    """Start a new, empty schematic for the given .kicad_sch path. backend
    "auto" uses KiCad 11's schematic API when available (KiCad owns the
    document; save_schematic writes it), else the in-memory file model
    (nothing touches disk until save_schematic); "api" / "file" force one.
    Refuses a path that is open in a KiCad editor (load_schematic edits a
    schematic open in a KiCad 11 GUI in place). The headless API holds ONE
    schematic: an idle (saved) session of another path is dropped and named in
    `released_sessions` (load_schematic it again later); a dirty one blocks."""
    if not path.endswith(".kicad_sch"):
        raise ValueError(f"path must end in .kicad_sch, got '{path}'")
    chosen = _pick_backend(backend)
    kind = ipc.editor_holding(path)
    if kind is not None:
        return {**ipc.write_refusal(path, kind), "backend": chosen,
                "hint": "load_schematic(path) connects to a schematic open in a KiCad 11 GUI"}
    key = _key(path)
    notes = _close_error_fields(_forget(key))   # re-creating replaces any earlier session
    if chosen == "file":
        sess = Session(path, "file", sfm.FileSchematic(path, paper))
    else:
        release = _release_headless()
        if release.blocked:
            return release.blocked
        notes = {**notes, **release.fields()}
        impl, err = _open_api(path, create=True, overwrite=True, mode="headless")
        if err:
            return {**err, **notes}
        sess = Session(path, "api", impl)
    _SESSIONS[key] = sess
    res = {"ok": True, "path": path, "backend": chosen, "mode": sess.mode,
           "paper": paper, "project": Path(path).stem, **notes}
    if chosen == "api" and paper != "A4":
        res["note"] = "paper size is not settable through the API backend (KiCad's default applies)"
    return res


@mcp.tool()
def load_schematic(path: str, backend: str = "auto") -> dict:
    """Open an existing .kicad_sch for editing. With the API backend KiCad
    reads the file (a real round-trip: list_symbols shows its contents) —
    headless, or inside a running KiCad 11 GUI that has it open. The file
    backend does NOT parse the file: its model starts empty and save_schematic
    REPLACES the file (rebuild in place, not append). The headless API holds
    ONE schematic: an idle (saved) session of another path is dropped and named
    in `released_sessions` (load_schematic it again later); a dirty one blocks."""
    if not Path(path).is_file():
        raise ValueError(f"File not found: {path}")
    chosen = _pick_backend(backend)
    key = _key(path)
    existing = _SESSIONS.get(key)
    if existing and existing.dirty:
        return _fail(existing, f"{path} already has a session with unsaved changes; "
                               "save_schematic or close_schematic it first")
    notes = _close_error_fields(_forget(key))
    if chosen == "file":
        _SESSIONS[key] = Session(path, "file", sfm.FileSchematic(path))
        return {"ok": True, "path": path, "backend": "file", "mode": "file",
                "note": "registered with an EMPTY model; the existing file is not parsed "
                        "and will be overwritten by save_schematic", **notes}
    k = ipc.connect()
    kind = ipc.holding_kind(k, path)
    if kind is not None and not (kind == "schematic" and ipc.gui_can_edit_schematic(k)):
        return {**ipc.write_refusal(path, kind), "backend": "api", **notes}
    release = _release_headless()
    if release.blocked:
        return release.blocked
    notes = {**notes, **release.fields()}
    impl, err = _open_api(path, mode="auto")
    if err:
        return {**err, **notes}
    sess = Session(path, "api", impl)
    _SESSIONS[key] = sess
    symbols, err = _call(sess, "symbols")
    if err:
        return {**err, **notes}
    return {"ok": True, "path": path, "backend": "api", "mode": sess.mode,
            "kicad_version": getattr(impl, "kicad_version", None),
            "symbols": len(symbols),
            "note": "opened in KiCad; every edit round-trips through the schematic API",
            **notes}


@mcp.tool()
def save_schematic(sch: str, symbols_dir: str = "") -> dict:
    """Write the schematic to disk. API backend: KiCad saves it (KiCad 11
    format). File backend: KiCad 9/10 format with library symbols embedded
    from KiCad's stock libraries or `symbols_dir`. Refuses to overwrite a file
    that is open in a KiCad editor unless this session IS that editor."""
    sess = _session(sch)
    if sess.mode != "gui":
        guard = ipc.write_guard(sch)
        if guard:
            return {**guard, "backend": sess.backend}
    args = (symbols_dir,) if sess.backend == "file" else ()
    res = _dispatch(sch, "save", *args)
    if sess.backend == "api" and symbols_dir:
        res["note"] = "symbols_dir is ignored by the API backend (KiCad's library tables apply)"
    return {**res, "path": sch}


@mcp.tool()
def close_schematic(sch: str, save: bool = False) -> dict:
    """Close a schematic session (stops a headless KiCad api-server; releases
    the file model). Unsaved edits are DISCARDED unless save is true; the
    response says whether anything was discarded."""
    sess = _session(sch)
    res = {"ok": True, "backend": sess.backend, "path": sess.path, "saved": False}
    if save:
        saved = save_schematic(sch)
        if not saved.get("ok"):
            return saved
        res["saved"] = True
    res["discarded_changes"] = sess.dirty
    close_error = _forget(_key(sch))
    if close_error:
        res["close_error"] = close_error
    return res


@mcp.tool()
def add_component(sch: str, ref: str, lib_id: str, value: str,
                  x: float, y: float, rot: int = 0, footprint: str = "",
                  unit: int = 1) -> dict:
    """Place one library symbol (Library:Symbol, e.g. Device:R). Coordinates in
    mm, rot in {0,90,180,270}, `unit` for multi-unit parts. Returns the pins
    with their absolute sheet coordinates."""
    return _dispatch(sch, "place_symbol", lib_id, ref, x, y, rot=rot, unit=unit,
                     value=value, footprint=footprint)


@mcp.tool()
def add_components_batch(sch: str, components: list[dict]) -> dict:
    """Place many components in one call. Each dict: ref, lib_id, value, x, y,
    [rot], [footprint], [unit]. Prefer this over repeated add_component calls.
    Validates every entry BEFORE placing any; stops at the first placement the
    backend rejects and reports what was placed. (The API backend applies
    Value/Footprint after the placements, on the next read or save, so repeated
    and multi-unit symbols stay linked to their library.)"""
    sess = _session(sch)
    entries = [sfm.validate_component(c) for c in components]
    placed: list[str] = []
    for e in entries:
        _result, err = _call(sess, "place_symbol", e["lib_id"], e["ref"], e["x"], e["y"],
                             rot=e["rot"], unit=e["unit"], value=e["value"],
                             footprint=e["footprint"])
        if err:
            return {**err, "placed": placed, "failed": e["ref"]}
        placed.append(e["ref"])
    return {"ok": True, "backend": sess.backend, "placed": placed, "count": len(placed)}


@mcp.tool()
def add_wire(sch: str, x1: float, y1: float, x2: float, y2: float) -> dict:
    """Draw a wire between two explicit points (mm)."""
    return _dispatch(sch, "add_wire", x1, y1, x2, y2)


@mcp.tool()
def add_label(sch: str, net: str, x: float, y: float, rot: int = 0) -> dict:
    """Place a net label at an explicit point (mm). `rot` applies to the file
    backend only (the API places labels unrotated)."""
    sess = _session(sch)
    res = _dispatch(sch, "add_label", net, x, y, **({"rot": rot} if sess.backend == "file" else {}))
    if rot and sess.backend == "api":
        res["note"] = "rot ignored by the API backend"
    return res


@mcp.tool()
def add_power_port(sch: str, net: str, x: float, y: float) -> dict:
    """Place a power/global-label port (e.g. +3V3, GND) at an explicit point (mm)."""
    return _dispatch(sch, "add_global_label", net, x, y)


@mcp.tool()
def wire_by_ref(sch: str, ref_a: str, pin_a: str, ref_b: str, pin_b: str) -> dict:
    """Connect two component pins by reference: straight when aligned, else an
    L (horizontal then vertical). Pin coordinates come from KiCad (API) or the
    pin sidecar / symbol library (file)."""
    return _dispatch(sch, "wire_pins", ref_a, pin_a, ref_b, pin_b)


@mcp.tool()
def wire_pins(sch: str, ref_a: str, pin_a: str, ref_b: str, pin_b: str) -> dict:
    """Same as wire_by_ref (connect two pins by reference and pin number/name)."""
    return _dispatch(sch, "wire_pins", ref_a, pin_a, ref_b, pin_b)


@mcp.tool()
def label_pin(sch: str, ref: str, pin: str, net_name: str) -> dict:
    """Place a net label on a component pin (by pin number or name)."""
    return _dispatch(sch, "label_pin", ref, pin, net_name)


@mcp.tool()
def power_pin(sch: str, ref: str, pin: str, net_name: str) -> dict:
    """Place a power port on a component power pin: the power:<net> symbol when
    that library symbol exists (API), else a global label."""
    return _dispatch(sch, "power_pin", ref, pin, net_name)


@mcp.tool()
def no_connect_pin(sch: str, ref: str, pin: str) -> dict:
    """Put a no-connect flag on an intentionally unused pin (ERC then accepts it)."""
    return _dispatch(sch, "no_connect", ref, pin)


@mcp.tool()
def add_junction(sch: str, x: float, y: float) -> dict:
    """Place a wire junction at an explicit point (mm)."""
    return _dispatch(sch, "add_junction", x, y)


@mcp.tool()
def list_symbols(sch: str) -> dict:
    """List every placed symbol: [{ref, lib_id, value, unit, at: [x, y, rot]}]."""
    sess = _session(sch)
    symbols, err = _call(sess, "symbols")
    if err:
        return err
    return {"ok": True, "backend": sess.backend, "symbols": symbols, "count": len(symbols)}


def _netlist_response(sess: Session) -> dict:
    nets, err = _call(sess, "netlist")
    if err:
        return err
    res = {"ok": True, "backend": sess.backend, "nets": nets, "count": len(nets)}
    warnings = getattr(sess.impl, "netlist_warnings", [])
    return {**res, "warnings": list(warnings)} if warnings else res


@mcp.tool()
def list_nets(sch: str) -> dict:
    """Net names in the schematic (API: KiCad's own netlist; file: read off the
    model's labels, wires and pin coordinates)."""
    res = _netlist_response(_session(sch))
    if not res.get("ok"):
        return res
    names = sorted(res["nets"])
    return {**res, "nets": names, "count": len(names)}


@mcp.tool()
def schematic_netlist(sch: str) -> dict:
    """Connectivity {net: [[ref, pin], ...]} — the authority is KiCad's netlist
    (API backend); the file backend derives it from its own geometry."""
    return _netlist_response(_session(sch))


@mcp.tool()
def run_erc(sch: str, errors_only: bool = False) -> dict:
    """Run KiCad's Electrical Rules Check on the SAVED file with the newest
    kicad-cli and return the parsed JSON report plus a violation count: errors
    AND warnings by default, errors only with errors_only (counted by each
    violation's own severity — kicad-cli writes every severity into the JSON
    report regardless of its flags). by_severity holds the full histogram."""
    backend = _backend_of(sch)
    if not tc.kicad_cli():
        return {"ok": False, "backend": backend,
                "reason": "kicad-cli not found; install KiCad or set KICAD_PATH."}
    if not Path(sch).is_file():
        raise ValueError(f"Schematic not found on disk: {sch} (call save_schematic first)")
    res = tc.erc_json(sch, str(Path(sch).with_suffix(".erc.json")), severity_error=errors_only)
    if "erc_error" in res:
        return {"ok": False, "backend": backend, "reason": res["erc_error"],
                "report_path": res["erc_report"]}
    out = {"ok": True, "backend": backend, "violations": res["erc_violations"],
           "erc_ok": res["erc_ok"], "by_severity": res["erc_by_severity"],
           "report_path": res["erc_report"], "report": res["report"]}
    sess = _SESSIONS.get(_key(sch))
    if sess and sess.dirty:
        out["warning"] = "the session has unsaved edits; ERC ran on the file on disk"
    return out


@mcp.tool()
def render_sch(sch: str, out_png: str = "") -> dict:
    """Render the schematic to SVG for visual verification (into out_png's
    directory, default beside the file). API sessions render their live
    document; otherwise the newest kicad-cli renders the saved file."""
    out_dir = str(Path(out_png).parent) if out_png else str(Path(sch).parent)
    sess = _SESSIONS.get(_key(sch))
    if sess and sess.backend == "api":
        files, err = _call(sess, "render_svg", out_dir)
        if err:
            return err
        return {"ok": True, "backend": "api", "output_dir": out_dir, "files": list(files)}
    cli = tc.kicad_cli()
    if not cli:
        return {"ok": False, "backend": "file",
                "reason": "kicad-cli not found; install KiCad or set KICAD_PATH."}
    if not Path(sch).is_file():
        raise ValueError(f"Schematic not found on disk: {sch} (call save_schematic first)")
    r = tc.run([cli, "sch", "export", "svg", "-o", out_dir, sch], timeout=tc.KICAD_CLI_TIMEOUT_S)
    return {"ok": r.returncode == 0, "backend": "file", "output_dir": out_dir,
            "stderr": r.stderr[-1000:]}


# ----------------------------------------------------------------------------
# NET-MAP PIPELINE, schematic stage (netlist.json -> ERC-clean .kicad_sch)
# ----------------------------------------------------------------------------
def _build_via_api(netlist: str, out: str, symbols_dir: str, sidecars_dir: str) -> dict:
    """backends.sch_api.build_from_netlist: KiCad 11 places every symbol from
    its libraries and writes the file (the build REPLACES `out`)."""
    guard = ipc.write_guard(out)
    if guard:
        return guard
    release = _release_headless()
    if release.blocked:
        return release.blocked
    notes = {**release.fields(), **_close_error_fields(_forget(_key(out)))}
    try:
        design = json.loads(Path(netlist).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return {"ok": False, "sch": out, "reason": f"netlist unreadable: {e}", **notes}
    try:
        res = _api_module().build_from_netlist(design, out, overwrite=True)
    except Exception as e:  # SchematicApiError carries the actionable message
        return {"ok": False, "sch": out, "reason": f"{type(e).__name__}: {e}", **notes}
    res = {**res, "sch": out, **notes}
    if symbols_dir or sidecars_dir:
        res["note"] = ("symbols_dir / sidecars_dir are ignored by the API backend "
                       "(KiCad's own library tables and symbol geometry are used)")
    return res


def _build_via_sch_build(netlist: str, out: str, symbols_dir: str, sidecars_dir: str) -> dict:
    """server/tools/sch_build.py under this venv (sexpdata)."""
    script = TOOLS_DIR / "sch_build.py"
    if not script.is_file():
        return {"ok": False, "reason": f"missing {script}"}
    syms = tc.symbols_dir(symbols_dir)
    sides = sidecars_dir or str(PINS_DIR)
    if not syms:
        return {"ok": False, "reason": "symbols_dir not given and KiCad share dir not found"}
    guard = ipc.write_guard(out)
    if guard:
        return guard
    r = tc.run([sys.executable, str(script), netlist, out, syms, sides])
    res = tc.last_json(r.stdout) or {}
    res["ok"] = r.returncode == 0 and res.get("ok", False)
    res["sch"] = out
    if r.stderr:
        res["stderr"] = r.stderr[-1200:]
    return res


@mcp.tool()
def build_schematic_from_netlist(netlist: str, out_sch: str = "",
                                 symbols_dir: str = "", sidecars_dir: str = "",
                                 run_erc: bool = True, backend: str = "auto") -> dict:
    """Generate an ERC-clean .kicad_sch from a pad-level netlist.json.

    Lays components on a grid (one cell per unit), wires connectivity with a
    net label at every pin, adds PWR_FLAGs on undriven power nets and
    no_connect flags on unused pins. backend "auto" lets KiCad 11's schematic
    API place the symbols and write the file when available (response
    backend "api"), else server/tools/sch_build.py embeds the symbols itself
    ("file"; defaults: symbols_dir = KiCad's stock library, sidecars_dir =
    this server's pins/). Optionally runs ERC (newest kicad-cli, errors only)
    and reports the violation count."""
    out = out_sch or str(Path(netlist).with_suffix(".kicad_sch"))
    chosen = _pick_backend(backend)
    if chosen == "api":
        res = _build_via_api(netlist, out, symbols_dir, sidecars_dir)
    else:
        res = _build_via_sch_build(netlist, out, symbols_dir, sidecars_dir)
    res["backend"] = chosen
    if res.get("ok") and run_erc and tc.kicad_cli():
        # gate on ERRORS only (warnings inform but don't block — e.g. the
        # pin_to_pin warning for an address-strap pin tied to a power net is a
        # known false positive); same severity policy as the DRC gate.
        erc = tc.erc_json(out, str(Path(out).with_suffix("")) + "_erc.json")
        res.update({k: v for k, v in erc.items() if k != "report"})
    return res


@mcp.tool()
def build_pin_sidecars(symbols: str, out_dir: str = "", symbols_dir: str = "") -> dict:
    """Build pin-coordinate sidecars (pin name/number -> schematic-space offset) for
    a comma-separated list of `Lib:Symbol` ids, used by the file-backend wiring
    tools. Writes <Lib_Symbol>.pins.json into out_dir (default: this server's pins/)."""
    script = TOOLS_DIR / "build_pin_sidecars.py"
    if not script.is_file():
        return {"ok": False, "reason": f"missing {script}"}
    syms = tc.symbols_dir(symbols_dir)
    out = out_dir or str(PINS_DIR)
    if not syms:
        return {"ok": False, "reason": "symbols_dir not given and KiCad share dir not found"}
    ids = [s.strip() for s in symbols.split(",") if s.strip()]
    if not ids:
        raise ValueError("symbols must list at least one Library:Symbol id")
    r = tc.run([sys.executable, str(script), syms, out] + ids)
    summary = tc.last_json(r.stdout) or {}
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
    the netlist. This reviews; it does not modify the design. ok is False (with
    `errors`) when a symbol could not be read: the pin-type checks did not cover
    that part, so the review is incomplete rather than clean."""
    if str(TOOLS_DIR) not in sys.path:
        sys.path.insert(0, str(TOOLS_DIR))
    try:
        import design_report as dr
    except Exception as e:
        return {"ok": False, "reason": f"design_report import failed: {e}"}
    design = json.loads(Path(netlist).read_text(encoding="utf-8"))
    syms = tc.symbols_dir(symbols_dir) or ""
    rep = dr.analyze(design, syms)
    md = dr.to_markdown(rep)
    md_path = out_md or (str(Path(netlist).with_suffix("")) + "_report.md")
    Path(md_path).write_text(md, encoding="utf-8")
    errors = list(rep.get("errors", []))
    res = {"ok": not errors, "errors": errors, "report": rep, "markdown": md,
           "markdown_path": md_path}
    if errors:
        res["reason"] = (f"design review could not run for {len(errors)} symbol(s): "
                         + "; ".join(errors))
    return res


# ----------------------------------------------------------------------------
# STATUS
# ----------------------------------------------------------------------------
def _backend_status() -> dict:
    api_ok = _api_available()
    return {"installs": tc.installs(),
            "schematic_backend": "api" if api_ok else "file",
            "schematic_backend_reason": None if api_ok else _API["reason"],
            "api_kicad_version": _api_kicad_version()}


@mcp.tool()
def test_kicad_connection() -> dict:
    """Probe the running KiCad GUI over IPC (MANDATORY first PCB step): version,
    every open document ({type: pcb|schematic|project, path}) and the
    version-derived capabilities (schematic_api / place_footprint_from_library
    / import_netlist are KiCad 11+ only); plus the installed KiCads and which
    schematic backend ("api" | "file") the schematic tools will use.
    ipc_connected false + reason when no GUI answers."""
    k = ipc.connect()
    if not k.get("ipc_connected"):
        # same shape as the connected result so callers can always read
        # capabilities / open_documents without a key check (C1)
        return {**k, "kicad_version": None, "open_documents": [],
                "capabilities": ipc.capabilities(()), **_backend_status()}
    res = {**ipc.public(k), **_backend_status()}
    boards = [d["path"] for d in k["open_documents"] if d["type"] == "pcb"]
    if boards:
        try:
            res["footprints"] = len(k["kicad"].get_board().get_footprints())
        except Exception as e:
            res["footprints_error"] = str(e)[:200]
    return res


@mcp.tool()
def server_status() -> dict:
    """Report what this server can do right now: every installed KiCad, the
    kicad-cli / bundled Python (verified to import pcbnew; kicad_python_error
    says why not) / libraries in use, the schematic backend ("api" needs a
    KiCad 11 api-server), Java + Freerouting, GUI IPC (version, open
    documents, capabilities), fab profile and the open schematic sessions."""
    cli = tc.kicad_cli()
    gui = ipc.public(ipc.connect())
    return {"kicad_cli": bool(cli), "kicad_cli_path": cli, "kicad_cli_schematic": cli,
            "kicad_cli_board": tc.board_cli(), "kicad_cli_policy": KICAD_CLI_POLICY,
            "kicad_python": tc.kicad_python(), "kicad_python_error": tc.kicad_python_error(),
            "kicad_share": str(tc.kicad_share() or ""), "symbols_dir": tc.symbols_dir(),
            "footprints_dir": tc.footprints_dir(),
            "install_probe_errors": env.probe_errors(),
            **_backend_status(),
            "java": tc.find_java(), "freerouting_jar": tc.freerouting_jar(),
            "ipc": bool(gui.get("ipc_connected")), "ipc_reason": gui.get("reason"),
            "kicad_version": gui.get("kicad_version"),
            "open_documents": gui.get("open_documents", []),
            "capabilities": gui.get("capabilities", ipc.capabilities(())),
            "fab_profile": FAB_PROFILE, "units": WORK_UNITS,
            "schematics_open": [{"path": s.path, "backend": s.backend, "mode": s.mode,
                                 "unsaved_changes": s.dirty} for s in _SESSIONS.values()]}


# ----------------------------------------------------------------------------
# Tool registration for the modules beside this one
# ----------------------------------------------------------------------------
for _fn in pcb_ipc_tools.TOOLS + board_pipeline_tools.TOOLS:
    mcp.tool()(_fn)


if __name__ == "__main__":
    mcp.run()
