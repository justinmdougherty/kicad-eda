#!/usr/bin/env python3
"""Export a .kicad_pcb to Specctra DSN (Freerouting input). KiCad bundled python.

    swig_export_dsn.py <board.kicad_pcb> <out.dsn>

Called by board_autoroute.export_dsn (the MCP tool autoroute_board), which
treats an existing <out.dsn> as a successful export. Prints ONE JSON line:

    {"ok": true, "dsn": path, "outline_rewrite": report | "skipped: ...",
     "polygon": [reasons], "warnings": [...]}
    {"ok": false, "dsn": path, "pcb": path, "stage": "usage" | "load" |
     "export" | "outline_rewrite" | "internal", "error": msg, "dsn_removed": bool}

rc 0 = ok, 1 = failed, 2 = usage. Steps (F3-17):
  1. board_outline.load_board: a missing or unreadable board is a JSON error
     (pcbnew.LoadBoard returns None for it and ExportSpecctraDSN(None, ...)
     segfaults).
  2. pcbnew.ExportSpecctraDSN(board, dsn) must return True and write the file
     (it returns False for an unwritable path).
  3. A polygon board (any cutout, any arc, more than 4 outline vertices or a
     non-axis-aligned edge; polygon_reasons) gets dsn_outline.rewrite_outline
     (F3-02): the boundary deflates and every hole keepout inflates by delta =
     copper edge clearance - smallest DSN clearance + 0.02 mm, because
     Freerouting keeps only its class clearance from the DSN outline while
     KiCad's DRC wants the edge clearance. Written atomically (temp file in
     the same directory + os.replace), newlines kept. A rectangular board's
     DSN stays byte-identical to KiCad's raw export.

Failures:
  * outline.inset cannot shrink the outline by delta (a narrow neck, ...):
    KiCad's raw export is kept, ok true with a warning; DRC and G4 copper
    containment still check the routed copper.
  * The board's Edge.Cuts cannot be read as one outline (open, missing, an
    unsupported shape): KiCad exports a fallback boundary; it is kept, ok true
    with a warning (DRC reports the malformed outline).
  * A frame mismatch, a structure error or a DSN that does not parse (bugs):
    ok false, rc 1, and the DSN is removed so no caller routes it. The same
    for a failed export or any other error after it.

Later (D7-19): the DSN layer policy post-processes the text after the outline
rewrite, in run(), before the one write.

Stdlib + pcbnew only, Python 3.11 (KiCad 9.0.4's bundled Python). Without
pcbnew (the dev venv) the module imports and load / export refuse.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import traceback
from pathlib import Path

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import kicad_quiet  # noqa: E402,F401  (before pcbnew: no modal wx dialogs)

try:
    import pcbnew  # noqa: E402
except ImportError:          # the dev venv: load / export refuse, the rest is testable
    pcbnew = None

import board_outline as bo  # noqa: E402
import dsn_outline as do  # noqa: E402
import dsn_sexpr  # noqa: E402
import outline as ol  # noqa: E402

USAGE = "usage: swig_export_dsn.py <board.kicad_pcb> <out.dsn>"
RECT_SKIP = "skipped: rectangular board"
UNREADABLE_SKIP = "skipped: the board outline cannot be read"
INSET_SKIP = "skipped: the outline cannot be shrunk by delta"
AXIS_TOL_MM = 1e-6           # an edge is axis-aligned when dx or dy is below 1 nm
RC_OK, RC_FAILED, RC_USAGE = 0, 1, 2


class ExportError(RuntimeError):
    """The export failed at `stage`; `removed` = the DSN was deleted."""

    def __init__(self, stage: str, message: str, removed: bool = False) -> None:
        super().__init__(message)
        self.stage = stage
        self.removed = removed

    def with_removed(self, removed: bool) -> ExportError:
        return ExportError(self.stage, str(self), removed)


# --- which boards get the rewrite -------------------------------------------------------
def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _axis_aligned(a: tuple, b: tuple) -> bool:
    return abs(b[0] - a[0]) < AXIS_TOL_MM or abs(b[1] - a[1]) < AXIS_TOL_MM


def polygon_reasons(outline: ol.Outline) -> list[str]:
    """Why `outline` is a polygon board (its DSN outline is rewritten): any
    cutout, a circular outline, any arc, more than 4 vertices, a straight
    edge that is not axis-aligned. Empty = a rectangle (DSN left as is)."""
    reasons = [_plural(len(outline.cutouts), "cutout")] if outline.cutouts else []
    if isinstance(outline.outer, ol.Circle):
        return [*reasons, "a circular outline"]
    v = outline.outer.vertices
    if any(abs(b) > ol.BULGE_EPS for _x, _y, b in v):
        reasons.append("arcs")
    if len(v) > 4:
        reasons.append(f"{len(v)} vertices")
    straight = [(v[i], v[(i + 1) % len(v)]) for i in range(len(v)) if abs(v[i][2]) <= ol.BULGE_EPS]
    if any(not _axis_aligned(a, b) for a, b in straight):
        reasons.append("a non-axis-aligned edge")
    return reasons


# --- load, export, read, write -------------------------------------------------------------
def load(pcb: str) -> object:
    """The board, or ExportError('load') for a missing / unreadable file
    (never pcbnew's None)."""
    try:
        return bo.load_board(pcb)
    except (OSError, RuntimeError) as e:
        raise ExportError("load", str(e)) from e


def export(board: object, dsn: str) -> None:
    """pcbnew.ExportSpecctraDSN(board, dsn): ExportError('export') unless it
    returns True AND the file exists."""
    if pcbnew is None:
        raise ExportError("export", "pcbnew is not importable: run this script with KiCad's "
                                    "bundled Python")
    try:
        ok = pcbnew.ExportSpecctraDSN(board, dsn)
    except Exception as e:
        raise ExportError("export", f"pcbnew.ExportSpecctraDSN raised "
                                    f"{type(e).__name__}: {e}") from e
    if not ok:
        raise ExportError("export", f"pcbnew.ExportSpecctraDSN returned {ok!r} for {dsn} "
                                    "(an unwritable path?)")
    if not os.path.isfile(dsn):
        raise ExportError("export", f"pcbnew.ExportSpecctraDSN returned True but wrote no file "
                                    f"at {dsn}")


def read_dsn(dsn: str) -> str:
    """The DSN text exactly as written (no newline translation)."""
    try:
        with open(dsn, "rb") as f:
            return f.read().decode("utf-8")
    except (OSError, UnicodeDecodeError) as e:
        raise ExportError("outline_rewrite", f"cannot read {dsn}: {type(e).__name__}: {e}") from e


def write_atomic(path: str, text: str) -> None:
    """Replace `path` with `text` (UTF-8, newlines as given) through a temp
    file in the same directory; on failure the old file stays and no temp is
    left (ExportError('outline_rewrite'))."""
    folder, name = os.path.split(os.path.abspath(path))
    handle, tmp = tempfile.mkstemp(prefix=f".{name}.", suffix=".tmp", dir=folder)
    try:
        with os.fdopen(handle, "wb") as f:
            f.write(text.encode("utf-8"))
        os.replace(tmp, path)
    except OSError as e:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise ExportError("outline_rewrite", f"cannot write {path}: {e}") from e


def discard(dsn: str) -> bool:
    """Remove the DSN a failed run leaves (no caller may route it)."""
    try:
        os.remove(dsn)
    except FileNotFoundError:
        return False
    except OSError as e:
        print(f"swig_export_dsn: could not remove {dsn}: {e}", file=sys.stderr)
        return False
    return True


# --- the outline rewrite -------------------------------------------------------------------
def rewrite_text(text: str, outline: ol.Outline, origin: tuple[float, float],
                 edge_clearance_mm: float) -> tuple[str, dict | str, list[str]]:
    """(new text, the report or a 'skipped: ...' string, warnings).
    An inset failure keeps `text` with a warning; a frame / structure / parse
    error (a bug) raises ExportError('outline_rewrite')."""
    try:
        new, report = do.rewrite_outline(text, outline, origin, edge_clearance_mm)
    except do.DsnOutlineError as e:
        if e.kind != do.INSET:
            raise ExportError("outline_rewrite", f"{e.kind}: {e}") from e
        return text, INSET_SKIP, [
            f"the DSN outline was not rewritten ({e}); KiCad's raw export is kept, so "
            "Freerouting may route closer to the board edge than the edge clearance: DRC and "
            "the G4 copper containment check still judge the routed copper"]
    except (dsn_sexpr.DsnError, ol.OutlineError, ValueError, TypeError) as e:
        raise ExportError("outline_rewrite", f"{type(e).__name__}: {e}") from e
    return new, report, []


def outline_rewrite(board: object, text: str) -> tuple[str, dict]:
    """(the DSN text after the outline step, {outline_rewrite, polygon,
    warnings}); `text` itself comes back when nothing changes."""
    try:
        outline, origin = bo.outline_from_board(board)
    except (bo.OutlineBuildError, ol.OutlineError) as e:
        return text, {"outline_rewrite": UNREADABLE_SKIP, "polygon": [], "warnings": [
            f"the DSN outline was not rewritten: {e}; KiCad's raw export (with its own "
            "fallback boundary) is kept and DRC reports the outline"]}
    reasons = polygon_reasons(outline)
    if not reasons:
        return text, {"outline_rewrite": RECT_SKIP, "polygon": [], "warnings": []}
    new, report, warnings = rewrite_text(text, outline, origin, bo.edge_clearance_mm(board))
    return new, {"outline_rewrite": report, "polygon": reasons, "warnings": warnings}


# --- entry points --------------------------------------------------------------------------
def run(pcb: str, dsn: str) -> dict:
    """Load, export, rewrite a polygon board's outline; the ok-true result.
    Raises ExportError; once the export was attempted it has removed the DSN
    (a load failure never touches it: swapped arguments must not delete a
    board)."""
    board = load(pcb)
    try:
        export(board, dsn)
        raw = read_dsn(dsn)
        text, info = outline_rewrite(board, raw)
        # D7-19: the DSN layer policy post-processes `text` here, before the one write
        if text is not raw:
            write_atomic(dsn, text)
    except Exception as e:
        err = e if isinstance(e, ExportError) else ExportError(
            "internal", f"{type(e).__name__}: {e}")
        if not isinstance(e, ExportError):
            traceback.print_exc(file=sys.stderr)
        raise err.with_removed(discard(dsn)) from e
    return {"ok": True, "dsn": dsn, **info}


def usage_error(argv: list[str]) -> str | None:
    """Why `argv` is not <board.kicad_pcb> <out.dsn>, or None."""
    if len(argv) != 2:
        return f"{USAGE} (got {len(argv)} argument(s))"
    pcb, dsn = argv
    if os.path.abspath(pcb) == os.path.abspath(dsn) or dsn.lower().endswith(".kicad_pcb"):
        return f"{USAGE}: the DSN path {dsn!r} would overwrite a board"
    return None


def main(argv: list[str]) -> int:
    """argv = [board.kicad_pcb, out.dsn]; prints ONE JSON line; rc 0 / 1 / 2."""
    why = usage_error(argv)
    if why:
        print(json.dumps({"ok": False, "stage": "usage", "error": why}))
        return RC_USAGE
    pcb, dsn = argv
    try:
        result = run(pcb, dsn)
    except Exception as e:
        if not isinstance(e, ExportError):          # before the export: nothing to remove
            traceback.print_exc(file=sys.stderr)
            e = ExportError("internal", f"{type(e).__name__}: {e}")
        print(json.dumps({"ok": False, "dsn": dsn, "pcb": pcb, "stage": e.stage,
                          "error": str(e), "dsn_removed": e.removed}))
        return RC_FAILED
    print(json.dumps(result))
    return RC_OK


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
