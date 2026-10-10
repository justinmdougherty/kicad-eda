"""Board outline and mechanical tools (F3; docs/ROADMAP_DECISIONS.md D2/D3).

The MCP surface for boards whose shape comes from a mechanical design (the
Fusion 360 round trip). Frames: the board frame is mm, Y down, origin = the
outline bbox top-left; the external (Fusion / DXF) frame is mm, Y up, origin
= ``board.datum``, which the builder makes the KiCad aux / drill origin.

  * ``import_outline_dxf``: an outline DXF into the netlist's ``board``
    block, as a reference (``outline.dxf``) or embedded loops / cutouts /
    datum, plus fixed parts given in the Fusion frame as ``at`` anchors (D3);
  * ``write_outline_dxf``: the resolved outline as a DXF for Fusion, before
    any board exists (default datum: the bbox bottom-left);
  * ``export_outline_dxf`` / ``export_step`` / ``export_pos_datum``: kicad-cli
    exports of a built board in the datum frame (outline DXF read back and
    compared with the board's Edge.Cuts, STEP with its bbox, the mechanical
    datum CPL; the fab CPL of export_fab_package stays absolute);
  * ``validate_board_outline``: resolve + outline.check + fab rules + the
    outline summary + fixed-part and mounting-hole checks (outline_audit).

Every tool returns a dict with ``ok``; a bad input becomes {"ok": False,
"reason"}. Relative paths resolve against the netlist's (or board's) folder.
kicad-cli wrappers, STEP bbox, outline comparison and part checks: tools/
mech_export.py, imported inside the tools (never at import time) like the
other server/tools modules; toolchain is imported AS A MODULE so tests that
monkeypatch tc.run / tc.board_cli reach it.

Plain functions; kicad_server registers every function in TOOLS with FastMCP
(this module is listed in kicad_server.TOOL_MODULES). A new tool is added by
appending it to TOOLS — kicad_server.py is not edited.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Callable

import toolchain as tc

MODES = ("reference", "embed")
FRAME_NOTE = "board frame: mm, Y down, origin = the outline bbox top-left"
EXTERNAL_NOTE = "datum frame (Fusion): mm, Y up, origin = board.datum (the aux / drill origin)"
POS_NOTE = ("mechanical datum CPL: positions relative to the drill / aux origin (board.datum) in "
            "the Fusion frame, for the mechanical round trip only; the fab CPL written by "
            "export_fab_package stays in absolute coordinates to match the Gerbers")
STEP_ASSEMBLY_NOTE = ("board_only=False: component models sit in their own STEP products placed "
                      "by assembly transforms this text scan does not apply, so the bbox is the "
                      "board body's only with board_only=True")
_ERRORS = (ValueError, TypeError, OSError, ImportError)  # DxfOutlineError etc. are ValueErrors


class ToolError(ValueError):
    """A bad argument or input file; the tool returns it as its reason."""


# --- shared helpers ------------------------------------------------------------------------
def _tools_on_path() -> None:
    if str(tc.TOOLS_DIR) not in sys.path:
        sys.path.insert(0, str(tc.TOOLS_DIR))


def _fail(exc: BaseException) -> dict:
    if isinstance(exc, ImportError):
        return {"ok": False, "reason": f"a Python dependency is missing ({exc}); install "
                                       "server/requirements.txt into the server's Python"}
    prefix = "malformed netlist or argument: " if isinstance(exc, TypeError) else ""
    return {"ok": False, "reason": prefix + (str(exc) or type(exc).__name__)}


def _r(v: float) -> float:
    return round(float(v), 6) + 0.0


def _load_netlist(netlist: str) -> tuple[Path, dict]:
    if not isinstance(netlist, str) or not netlist.strip():
        raise ToolError(f"netlist must be the path of a netlist.json, got {netlist!r}")
    path = Path(netlist)
    if not path.is_file():
        raise ToolError(f"netlist not found: {path}")
    try:
        design = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise ToolError(f"{path} is not a JSON netlist: {exc}") from exc
    if not isinstance(design, dict):
        raise ToolError(f"{path} is not a JSON object netlist (got {type(design).__name__})")
    return path, design


def _board_file(pcb: str) -> Path:
    if not isinstance(pcb, str) or not pcb.strip():
        raise ToolError(f"pcb must be the path of a .kicad_pcb, got {pcb!r}")
    if not (path := Path(pcb)).is_file():
        raise ToolError(f"board not found: {path}")
    return path


def _beside(base: Path, name: str, what: str) -> Path:
    """`name` as given when absolute, else relative to `base`."""
    if not isinstance(name, str) or not name.strip():
        raise ToolError(f"{what} must be a file path, got {name!r}")
    path = Path(name.strip())
    return path if path.is_absolute() else base / path


def _existing(base: Path, name: str, what: str) -> Path:
    if not (path := _beside(base, name, what)).is_file():
        raise ToolError(f"{what} not found: {path}")
    return path


def _cli(pcb: Path) -> tuple[str, dict]:
    cli = tc.board_cli(str(pcb))
    if not cli:
        raise ToolError("kicad-cli not found; install KiCad (or set KICAD_PATH)")
    note = tc.board_cli_note(str(pcb))
    return cli, ({"kicad_cli_note": note} if note else {})


def _profile() -> tuple[str, list[str]]:
    import outline as ol
    name = str(tc.FAB_PROFILE).upper()
    if name in ol.FAB_LIMITS:
        return name, []
    return "JLCPCB", [f"fab profile {tc.FAB_PROFILE!r} has no outline limits; judged as JLCPCB"]


# --- import_outline_dxf ----------------------------------------------------------------------
def _relative(path: Path, base: Path) -> str:
    try:
        return Path(os.path.relpath(path.resolve(), base.resolve())).as_posix()
    except ValueError:                          # another drive on Windows
        return path.resolve().as_posix()


def _load_parts(parts: object, base: Path) -> dict:
    """{ref: [x, y(, rot)]} from an object, JSON text or a JSON file path."""
    if parts is None or (isinstance(parts, str) and not parts.strip()):
        return {}
    if isinstance(parts, str):
        text = parts.strip()
        if text[0] not in "{[":
            text = _existing(base, text, "parts file").read_text(encoding="utf-8")
        try:
            parts = json.loads(text)
        except ValueError as exc:
            raise ToolError(f"parts is not JSON: {exc}") from exc
    if not isinstance(parts, Mapping):
        raise ToolError("parts must be a JSON object {ref: [x, y, rot]} (Fusion frame: mm, Y up, "
                        f"origin = the datum), got {type(parts).__name__}")
    return dict(parts)


def _atomic_write(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _write_netlist(path: Path, design: dict, backup: bool) -> str | None:
    """<netlist>.bak first (with backup), then the new netlist atomically."""
    text = json.dumps(design, indent=2, ensure_ascii=False) + "\n"
    bak = Path(str(path) + ".bak") if backup else None
    if bak is not None:
        shutil.copy2(path, bak)
    _atomic_write(path, text)
    return None if bak is None else str(bak)


def _import(netlist: str, dxf: str, layer: str, units: str, mode: str, parts: object,
            backup: bool) -> dict:
    if mode not in MODES:
        raise ToolError(f"mode must be one of {', '.join(MODES)}, got {mode!r}")
    path, design = _load_netlist(netlist)
    base = path.absolute().parent
    dxf_path = _existing(base, dxf, "DXF")
    _tools_on_path()
    import dxf_io
    import mech_export as mx
    import outline as ol
    import outline_audit as oa
    import outline_resolve as orv
    read = dxf_io.read_board_outline(dxf_path, layer=layer or None, units=units or None)
    spec = {"dxf": _relative(dxf_path, base),
            **({"layer": layer.strip()} if layer and layer.strip() else {}),
            **({"units": read.source.units} if units and units.strip() else {})}
    geometry = {"outline": spec} if mode == "reference" else read.to_board_block()
    board, notes = mx.dxf_board(design.get("board"), geometry)
    anchors = mx.fusion_anchors(_load_parts(parts, base), read.datum, design.get("components"))
    new = mx.with_anchors(design, board, anchors)
    resolved = orv.resolve_board(new, base)         # validated before anything is written
    profile, profile_notes = _profile()
    fab = [i.to_dict() for i in oa.fab_issues(resolved.outline, profile=profile)]
    bak = _write_netlist(path, new, backup)
    box = ol.bbox(resolved.outline)
    return {"ok": True, "netlist": str(path), "backup": bak, "mode": mode,
            "source": resolved.source, "dxf": str(dxf_path), "board": board,
            "bbox": [_r(v) for v in box], "size": [_r(box[2] - box[0]), _r(box[3] - box[1])],
            "datum": [_r(v) for v in read.datum], "parts": anchors, "notes": notes,
            "warnings": list(dict.fromkeys([*read.warnings, *resolved.warnings,
                                            *profile_notes])),
            "fab_profile": profile, "fab_issues": fab, "fingerprint": resolved.fingerprint,
            "frame": FRAME_NOTE}


def import_outline_dxf(netlist: str, dxf: str, layer: str = "", units: str = "",
                       mode: str = "reference", parts: str | dict = "",
                       backup: bool = True) -> dict:
    """Bring a board outline DXF (e.g. a Fusion 360 sketch export) into a
    netlist.json's ``board`` block, rewriting the netlist atomically (with
    backup, <netlist>.bak keeps the previous file first). dxf: path, relative
    to the netlist's folder or absolute. layer: the DXF layer to read ('' =
    every layer). units: mm | cm | in | mil ('' = the DXF header: $INSUNITS,
    then $MEASUREMENT). mode "reference" writes board.outline = {"dxf": <path
    relative to the netlist>, layer?, units?} (re-read at every build); mode
    "embed" writes the loops, cutouts and datum (board.outline.loop,
    board.cutouts, board.datum, w/h = the bbox). Conflicting board keys (w, h,
    corner_radius, cutouts, datum) are removed or replaced, with a note. parts:
    optional {ref: [x, y, rot]} (a JSON object, JSON text or a JSON file path)
    of fixed parts in the Fusion frame (mm, Y up, origin = the sketch origin =
    the datum); each becomes the component's "at" (footprint origin in the
    board frame, rotation unchanged; [x, y] keeps its current rotation) and
    its x / y / rot are removed. The result is validated (outline_resolve)
    before anything is written. Returns {ok, netlist, backup, mode, source,
    board, bbox, size, datum, parts, notes, warnings, fab_issues,
    fingerprint}, or {ok: false, reason} with the netlist unchanged."""
    try:
        return _import(netlist, dxf, layer, units, mode, parts, backup)
    except _ERRORS as exc:
        return _fail(exc)


# --- write_outline_dxf -----------------------------------------------------------------------
def _write_dxf(netlist: str, out_dxf: str) -> dict:
    path, design = _load_netlist(netlist)
    base = path.absolute().parent
    out = _beside(base, out_dxf, "out_dxf")
    _tools_on_path()
    import dxf_io
    import outline as ol
    import outline_resolve as orv
    resolved = orv.resolve_board(design, base)
    if resolved.outline is None:
        raise ToolError(f"{path.name} has no board block (the builder sizes the board to its "
                        "parts): there is no outline to write; give board w/h or an outline")
    written = dxf_io.write_outline_dxf(out, resolved.block)
    x0, y0, x1, y1 = ol.bbox(resolved.outline)
    if resolved.outline.datum is not None:
        datum, how = resolved.outline.datum, "board.datum"
    else:
        datum, how = (x0, y1), "default: the outline bbox bottom-left (the board has no datum)"
    return {"ok": True, "dxf": str(written), "layer": dxf_io.DEFAULT_LAYER,
            "datum": [_r(v) for v in datum], "datum_source": how, "source": resolved.source,
            "size": [_r(x1 - x0), _r(y1 - y0)], "warnings": list(resolved.warnings),
            "frame": "DXF: mm, Y up, origin = the datum"}


def write_outline_dxf(netlist: str, out_dxf: str) -> dict:
    """Write the netlist's resolved board outline (w/h, corner_radius, loop,
    cutouts or a referenced DXF) as an outline DXF for Fusion 360 BEFORE the
    board is built: R2010, mm, Y up, origin = board.datum (default: the
    outline bbox bottom-left), closed LWPOLYLINEs with exact arcs (bulges) and
    CIRCLEs on layer OUTLINE. out_dxf: relative to the netlist's folder or
    absolute. Returns {ok, dxf, layer, datum (board frame), datum_source,
    source, size, warnings}, or {ok: false, reason}."""
    try:
        return _write_dxf(netlist, out_dxf)
    except _ERRORS as exc:
        return _fail(exc)


# --- kicad-cli exports -----------------------------------------------------------------------
def _job(pcb: str, out_name: str, what: str) -> tuple[Path, Path, str, dict, Any]:
    """(board, out, cli, response base, mech_export) for one export job."""
    board = _board_file(pcb)
    cli, note = _cli(board)
    out = _beside(board.parent, out_name, what)
    out.parent.mkdir(parents=True, exist_ok=True)
    _tools_on_path()
    import mech_export as mx
    return board, out, cli, {"kicad_cli": cli, **note}, mx


def _export_dxf(pcb: str, out_dxf: str) -> dict:
    board, out, cli, base, mx = _job(pcb, out_dxf, "out_dxf")
    import dxf_io
    reference = mx.board_file_outline(board)
    help_text, why = mx.cli_help(tc.run, cli, ("pcb", "export", "dxf"))
    args, warnings, mm = mx.dxf_command(cli, str(board), str(out), help_text, why)
    run = mx.run_cli(tc.run, args, out, "DXF", board=board)
    base = {"dxf": str(out), **base, "command": args}
    if not run.ok:
        return {"ok": False, "reason": run.reason, **base, "warnings": warnings}
    back = dxf_io.read_board_outline(out, units="mm" if mm else None)
    dist, tol = mx.frame_hausdorff(reference, back.model), mx.HAUSDORFF_TOL_MM
    res = {"ok": dist <= tol, "hausdorff_mm": _r(dist), "tolerance_mm": tol, **base,
           "warnings": [*warnings, *run.warnings, *back.warnings], "frame": EXTERNAL_NOTE}
    if dist > tol:
        res["reason"] = (f"the exported DXF differs from the board's Edge.Cuts by {dist:.4f} mm "
                         f"in the datum frame (aux origin), more than the {tol:g} mm tolerance")
    return res


def export_outline_dxf(pcb: str, out_dxf: str) -> dict:
    """Export a built board's outline as a DXF for Fusion 360 in the datum
    frame: kicad-cli pcb export dxf --layers Edge.Cuts --use-drill-origin --ou
    mm --drill-shape-opt 0 --mode-single (never --use-contours, which loses
    arcs; a flag this kicad-cli's --help does not list is dropped with a
    warning), with the kicad-cli of the KiCad that wrote the board. The DXF
    is read back and compared with the board's Edge.Cuts in the datum frame:
    ok only when the Hausdorff distance is <= 0.01 mm. out_dxf: relative to
    the board's folder or absolute. Returns {ok, dxf, hausdorff_mm,
    tolerance_mm, warnings, kicad_cli, command}, plus reason when not ok."""
    try:
        return _export_dxf(pcb, out_dxf)
    except _ERRORS as exc:
        return _fail(exc)


def _export_step(pcb: str, out_step: str, board_only: bool) -> dict:
    board, out, cli, base, mx = _job(pcb, out_step, "out_step")
    args = mx.step_command(cli, str(board), str(out), bool(board_only))
    run = mx.run_cli(tc.run, args, out, "STEP file", board=board)
    base = {"step": str(out), "board_only": bool(board_only), **base, "command": args}
    if not run.ok:
        return {"ok": False, "reason": run.reason, **base}
    warnings = [*run.warnings, *mx.log_warnings(run.output)]
    try:
        box = mx.step_bbox(out)
        warnings += box.pop("warnings")
    except (mx.StepScanError, OSError) as exc:
        box = None
        warnings.append(f"STEP bbox not computed: {exc}")
    if not board_only:
        warnings.append(STEP_ASSEMBLY_NOTE)
    return {"ok": True, **base, "bbox": box, "frame": EXTERNAL_NOTE, "warnings": warnings}


def export_step(pcb: str, out_step: str, board_only: bool = False) -> dict:
    """Export a built board as STEP for Fusion 360 in the datum frame: kicad-cli
    pcb export step --drill-origin --force [--board-only], with the kicad-cli
    of the KiCad that wrote the board (the aux / drill origin is board.datum,
    so the board lands where the Fusion sketch had it). out_step: relative to
    the board's folder or absolute. Returns {ok, step, bbox: {x, y, z: [min,
    max] mm, basis, vertices, arcs, ...} | None, warnings, kicad_cli,
    command}; the bbox (a text scan of the STEP's B-rep edges) is the board
    body's with board_only=True (component models are not transformed).
    {ok: false, reason} when the export fails."""
    try:
        return _export_step(pcb, out_step, board_only)
    except _ERRORS as exc:
        return _fail(exc)


def _export_pos(pcb: str, out_csv: str) -> dict:
    board, out, cli, base, mx = _job(pcb, out_csv, "out_csv")
    args = mx.pos_command(cli, str(board), str(out))
    run = mx.run_cli(tc.run, args, out, "position file", board=board)
    base = {"csv": str(out), **base, "command": args}
    if not run.ok:
        return {"ok": False, "reason": run.reason, **base}
    return {"ok": True, **base, "rows": mx.csv_rows(out), "units": "mm", "frame": EXTERNAL_NOTE,
            "note": POS_NOTE, "warnings": [*run.warnings, *mx.log_warnings(run.output)]}


def export_pos_datum(pcb: str, out_csv: str) -> dict:
    """Export the MECHANICAL datum CPL of a built board: kicad-cli pcb export
    pos --use-drill-file-origin --units mm --format csv, so every part
    position is relative to board.datum (the Fusion frame). This is not the
    fab CPL: export_fab_package's pick-and-place stays in absolute coordinates
    to match the Gerbers. An empty board gives a header-only CSV. out_csv:
    relative to the board's folder or absolute. Returns {ok, csv, rows,
    units, note, warnings, kicad_cli, command}, or {ok: false, reason}."""
    try:
        return _export_pos(pcb, out_csv)
    except _ERRORS as exc:
        return _fail(exc)


# --- validate_board_outline ------------------------------------------------------------------
def _verdict(path: Path, issues: list, **extra: Any) -> dict:
    errors = [i.to_dict() for i in issues if i.severity == "error"]
    warnings = [i.to_dict() for i in issues if i.severity != "error"]
    return {"ok": not errors, "netlist": str(path), **extra, "errors": errors,
            "warnings": warnings}


def _validate(netlist: str) -> dict:
    path, design = _load_netlist(netlist)
    _tools_on_path()
    import mech_export as mx
    import outline as ol
    import outline_audit as oa
    import outline_resolve as orv
    profile, profile_notes = _profile()
    try:
        resolved = orv.resolve_board(design, path.absolute().parent)
        o = None if resolved.outline is None else ol.check(resolved.outline)
    except ValueError as exc:                   # ResolveError, OutlineError
        return _verdict(path, [oa.Issue("outline_unresolved", "error", None, str(exc))],
                        source=None, fab_profile=profile)
    if o is None:
        return _verdict(path, [oa.Issue("no_board_outline", "warning", None, "the netlist has "
                                        "no board block: the builder sizes the board to its "
                                        "parts, so there is no outline to validate")],
                        source="none", fab_profile=profile)
    issues = [*oa.fab_issues(o, profile=profile),
              *mx.part_issues(o, mx.component_list(design.get("components"))),
              *mx.hole_issues(o, design, resolved.source, profile),
              *(oa.Issue("dxf_warning", "warning", None, w) for w in resolved.warnings),
              *(oa.Issue("fab_profile", "warning", None, n) for n in profile_notes)]
    return _verdict(path, issues, source=resolved.source, is_legacy=resolved.is_legacy,
                    fab_profile=profile, summary=oa.outline_summary(o), frame=FRAME_NOTE)


def validate_board_outline(netlist: str) -> dict:
    """Validate a netlist's board outline without building anything: resolve
    the board block (w/h, corner_radius, outline.loop or a referenced DXF,
    cutouts, datum), outline.check (closed simple rings, cutouts inside and
    apart), the fab profile's outline rules (slot width, inner corner radius,
    feature width, board size; ASSUMPTION-based rules are warnings), the
    outline summary (bbox, area, cutouts, datum, narrowest web), fixed parts
    (an 'at' or x/y anchor inside a cutout or off the board is an error,
    within 1 mm of an edge a warning; 'overhang' allows hanging over the outer
    edge) and mounting holes judged by their drill circle (positions, or the
    default bbox-corner holes, which need a rectangular board). Issues are
    {code, severity, ref, message, at} in the board frame. Returns {ok,
    errors, warnings, source, summary, fab_profile}, or {ok: false, reason}
    when the netlist cannot be read."""
    try:
        return _validate(netlist)
    except _ERRORS as exc:
        return _fail(exc)


TOOLS: tuple[Callable[..., Any], ...] = (
    import_outline_dxf, write_outline_dxf, export_outline_dxf, export_step, export_pos_datum,
    validate_board_outline)
