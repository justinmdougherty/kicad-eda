#!/usr/bin/env python3
"""Mechanical exports for the Fusion round trip (ROADMAP_DECISIONS D2): the
kicad-cli command lines, a stdlib STEP bounding-box scan and the outline
comparison behind the mechanical MCP tools (server/mechanical_tools.py).

kicad-cli jobs (flags verified on KiCad 9.0.4 and 10.99; with the aux / drill
origin on ``board.datum`` all three land in the Fusion frame, mm, Y up,
origin = the datum: the T10 board, 60 x 40 with datum (10, 30), spans
x -10..50, y -10..30):
  outline DXF  pcb export dxf --layers Edge.Cuts --use-drill-origin --ou mm
               --drill-shape-opt 0 --mode-single -o <out> <pcb>
               NEVER --use-contours (it turns arcs into polylines). A flag the
               install's ``pcb export dxf --help`` does not list is dropped
               with a warning (dxf_command).
  STEP         pcb export step --drill-origin --force [--board-only] -o <out> <pcb>
  datum CPL    pcb export pos --use-drill-file-origin --units mm --format csv
               -o <out> <pcb>
Every child goes through the caller's runner (server/toolchain.run: stdin
closed) with encoding='utf-8', errors='replace' and a timeout. Success is rc 0
AND no ``Error:`` line (a wx timestamp prefix allowed) AND a non-empty output
file written by this run (D0: rc 0 alone is not success): run_cli moves a
previous output aside first (never an mtime comparison, which a coarse FAT
clock can fool) and puts it back when the job fails. The output may not be a
directory or the job's board itself.

step_bbox: the bounding box of a STEP file's B-rep edges, read as text: every
VERTEX_POINT's CARTESIAN_POINT plus the axis extremes of each CIRCLE edge
between its vertices (a round board's edge bulges past its vertices). Without
vertices it falls back to every 3D CARTESIAN_POINT. Lengths are scaled to mm
from the file's SI_UNIT / INCH unit. Assembly transforms are NOT applied:
component models live in their own product frames, so the box is the board's
only for a --board-only export.

Outline comparison: board_file_outline reads a .kicad_pcb's Edge.Cuts (board
and footprint shapes, arcs exact) and its aux origin as text through
kicad_import_pcb (any KiCad 6..11 file, no KiCad needed); frame_hausdorff
compares two outlines in the external frame (each moved so its datum is the
origin) by the Hausdorff distance of their edges.

Netlist edits (pure, never mutating their inputs): dxf_board (the board block
a DXF import writes, with notes on what it replaced), fusion_anchors (Fusion
part positions -> D3 ``at``) and with_anchors (the new design).
component_list turns a ``components`` value that is not a list into a
ValueError; fusion_anchors / with_anchors leave a component without a str
``ref`` untouched.

Fixed-part checks (outline_audit Issues, board frame): part_issues (an
``at`` / x-y anchor inside a cutout or off the board is an error, within
NEAR_EDGE_MM of an edge a warning, ``overhang`` allows hanging over the outer
edge) and hole_issues (mounting holes judged by their drill circle through
outline_audit.mounting_hole_issues; default bbox-corner holes need a
rectangular board; a malformed ``inset`` or position is a
``mounting_holes_spec`` error, the builder could not place it).

Venv only (kicad_import_pcb needs sexpdata); never imported by KiCad's Python.
"""
from __future__ import annotations

import contextlib
import csv
import math
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import outline as ol  # noqa: E402
import outline_audit as oa  # noqa: E402
from outline_geom import closest_point  # noqa: E402
from outline_model import PrimIndex, all_prims  # noqa: E402

__all__ = ["CLI_TIMEOUT_S", "DXF_FLAGS", "HAUSDORFF_TOL_MM", "NEAR_EDGE_MM", "CliFlag",
           "CliRun", "StepScanError", "board_file_outline", "cli_help", "component_list",
           "csv_rows", "dxf_board", "dxf_command", "error_lines", "frame_hausdorff",
           "fusion_anchors", "hausdorff", "hole_issues", "log_warnings", "part_issues",
           "pos_command", "run_cli", "step_bbox", "step_command", "with_anchors"]

CLI_TIMEOUT_S = 600.0            # one export (a STEP with every 3D model can take minutes)
HELP_TIMEOUT_S = 60.0
HAUSDORFF_TOL_MM = 0.01          # exported DXF vs the board's Edge.Cuts
SAMPLE_MM = 0.05                 # edge sample spacing for the Hausdorff distance
MAX_SAMPLES = 20000              # per outline; the spacing grows on very long outlines
NEAR_MM = 1.0                    # nearest-edge search box before a full scan
NEAR_EDGE_MM = 1.0               # a fixed part's anchor this close to an edge: warning
DEFAULT_HOLE_FOOTPRINT = "MountingHole:MountingHole_3.2mm_M3"   # swig_build_board's default
DEFAULT_HOLE_DRILL_MM = 3.2
DEFAULT_HOLE_INSET_MM = 4.0      # swig_build_board's corner inset
_GEOMETRY_KEYS = ("w", "h", "corner_radius", "outline", "cutouts", "datum")
_HOLE_DRILL = re.compile(r"MountingHole_(\d+(?:\.\d+)?)mm")
_LOG_WARNING = re.compile(r"(?i)\b(warning|could not|cannot|not found)\b")
_TAIL = 600

Runner = Callable[..., subprocess.CompletedProcess]
Point = tuple[float, float]
Vec = tuple[float, float, float]


# --- kicad-cli command lines --------------------------------------------------------------
@dataclass(frozen=True)
class CliFlag:
    """``args`` go on the command line when ``--help`` lists any of ``names``."""
    args: tuple[str, ...]
    names: tuple[str, ...]


DXF_FLAGS = (
    CliFlag(("--layers", "Edge.Cuts"), ("--layers", "-l")),
    CliFlag(("--use-drill-origin",), ("--use-drill-origin", "--udo")),
    CliFlag(("--ou", "mm"), ("--ou", "--output-units")),
    CliFlag(("--drill-shape-opt", "0"), ("--drill-shape-opt",)),
    CliFlag(("--mode-single",), ("--mode-single",)),
)


def _listed(help_text: str, names: Iterable[str]) -> bool:
    return any(re.search(rf"(?<![\w-]){re.escape(n)}(?![\w-])", help_text) for n in names)


def dxf_command(cli: str, pcb: str, out: str, help_text: str,
                why: str | None = None) -> tuple[list[str], list[str], bool]:
    """(args, warnings, mm): the outline DXF export with every DXF_FLAGS entry
    the help text lists (an unreadable help keeps them all, with a warning
    that gives ``why``, cli_help's reason); ``mm`` is False when --ou was
    dropped (the DXF then has kicad-cli's default unit, inches, and must be
    read by its header)."""
    if not help_text.strip():
        args = [a for flag in DXF_FLAGS for a in flag.args]
        because = f" ({why})" if why else ""
        return ([cli, "pcb", "export", "dxf", *args, "-o", out, pcb],
                [f"could not read 'kicad-cli pcb export dxf --help'{because}; ran with every "
                 "flag"], True)
    args, warnings, mm = [cli, "pcb", "export", "dxf"], [], True
    for flag in DXF_FLAGS:
        if _listed(help_text, flag.names):
            args += flag.args
            continue
        warnings.append(f"this kicad-cli's 'pcb export dxf --help' does not list {flag.args[0]}: "
                        f"exported without '{' '.join(flag.args)}'")
        mm = mm and flag.args[0] != "--ou"
    return [*args, "-o", out, pcb], warnings, mm


def step_command(cli: str, pcb: str, out: str, board_only: bool) -> list[str]:
    """STEP in the datum frame: --drill-origin (the aux origin), --force overwrites."""
    return [cli, "pcb", "export", "step", "--drill-origin", "--force",
            *(["--board-only"] if board_only else []), "-o", out, pcb]


def pos_command(cli: str, pcb: str, out: str) -> list[str]:
    """The mechanical datum CPL (positions relative to the drill / aux origin)."""
    return [cli, "pcb", "export", "pos", "--use-drill-file-origin", "--units", "mm",
            "--format", "csv", "-o", out, pcb]


# --- running kicad-cli ----------------------------------------------------------------------
_ERROR_LINE = re.compile(r"(?:^|\s)Error:")


def error_lines(text: str) -> list[str]:
    """kicad-cli's ``Error:`` lines (a wx timestamp prefix is allowed)."""
    return [line.strip() for line in text.splitlines() if _ERROR_LINE.search(line)]


@dataclass(frozen=True)
class CliRun:
    """One kicad-cli job: its command, rc (None: never ran), output, the
    reason it failed (None = success) and warnings about its files."""
    command: tuple[str, ...]
    returncode: int | None
    output: str
    reason: str | None = None
    warnings: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.reason is None


def _tail(text: str) -> str:
    return text.strip()[-_TAIL:] or "(no output)"


def _call(runner: Runner, command: Sequence[str], timeout: float
          ) -> tuple[subprocess.CompletedProcess | None, str | None]:
    try:
        return runner(list(command), encoding="utf-8", errors="replace", timeout=timeout), None
    except subprocess.TimeoutExpired:
        return None, f"kicad-cli timed out after {timeout:g} s: {' '.join(command)}"
    except OSError as exc:
        return None, f"kicad-cli could not be started ({exc}): {' '.join(command)}"


def cli_help(runner: Runner, cli: str, verb: Sequence[str],
             timeout: float = HELP_TIMEOUT_S) -> tuple[str, str | None]:
    """(text, why): ``kicad-cli <verb> --help`` as text and None, or '' and
    why it could not be read (a timeout, a failed start, no output). KiCad 9
    exits 1 after printing the help, so the rc is ignored."""
    proc, reason = _call(runner, [cli, *verb, "--help"], timeout)
    if proc is None:
        return "", reason
    text = (proc.stdout or "") + (proc.stderr or "")
    return (text, None) if text.strip() else ("", f"it printed nothing (rc {proc.returncode})")


def _size(path: Path) -> int:
    try:
        return path.stat().st_size if path.is_file() else 0
    except OSError:
        return 0


def _same_file(a: Path, b: Path) -> bool:
    try:
        return a.exists() and b.exists() and os.path.samefile(a, b)
    except OSError:
        return False


def _refusal(out: Path, what: str, board: str | Path | None) -> str | None:
    """Why ``out`` must not be written (None: it may)."""
    if out.is_dir():
        return f"the {what} path {out} is a directory"
    if board is not None and _same_file(out, Path(board)):
        return f"the {what} path {out} is the board itself: give the {what} another path"
    return None


def _set_aside(out: Path) -> Path | None:
    """Move a previous ``out`` to a sibling temp file, so only a file this run
    writes can count (no mtime / size comparison, which a coarse clock can
    fool); None when there is none. Raises OSError when it cannot be moved."""
    if not out.exists():
        return None
    fd, tmp = tempfile.mkstemp(prefix=out.name + ".", suffix=".prev", dir=out.parent)
    os.close(fd)
    try:
        os.replace(out, tmp)
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    return Path(tmp)


def _settle(out: Path, prev: Path | None, ok: bool, what: str) -> str | None:
    """Drop the previous file after a good run, put it back over any partial
    output after a failed one; the problem when that is not possible."""
    try:
        if prev is not None and ok:
            prev.unlink()
        elif prev is not None:
            os.replace(prev, out)
    except OSError as exc:
        return f"the previous {what} is left at {prev} ({exc})"
    return None


def _failure(proc: subprocess.CompletedProcess, output: str, out: Path, what: str) -> str | None:
    errors = error_lines(output)
    if proc.returncode != 0:
        return f"kicad-cli exited {proc.returncode}: {_tail(output)}"
    if errors:
        return "kicad-cli reported errors (rc 0 is not success): " + "; ".join(errors[:5])
    if _size(out) == 0:
        return f"kicad-cli wrote no {what} at {out}: {_tail(output)}"
    return None


def run_cli(runner: Runner, command: Sequence[str], expect: str | Path, what: str,
            timeout: float = CLI_TIMEOUT_S, board: str | Path | None = None) -> CliRun:
    """Run one export job; ok only with rc 0, no ``Error:`` line and a
    non-empty ``expect`` file written by this run. A previous ``expect`` is
    moved aside first and put back when the job fails; ``expect`` may not be
    a directory or ``board`` (the job's input) itself."""
    out, cmd = Path(expect), tuple(command)
    if (refused := _refusal(out, what, board)) is not None:
        return CliRun(cmd, None, "", refused)
    try:
        prev = _set_aside(out)
    except OSError as exc:
        return CliRun(cmd, None, "", f"could not move the previous {what} at {out} aside "
                                     f"({exc}); is it open in another program?")
    try:
        proc, reason = _call(runner, command, timeout)
    except BaseException:                       # an unexpected error: the previous file back
        _settle(out, prev, False, what)
        raise
    output = "" if proc is None else (proc.stdout or "") + (proc.stderr or "")
    reason = reason or _failure(proc, output, out, what)
    left = _settle(out, prev, reason is None, what)
    if left is not None and reason is not None:
        reason = f"{reason}; {left}"
    return CliRun(cmd, None if proc is None else proc.returncode, output, reason,
                  () if left is None or reason is not None else (left,))


def log_warnings(output: str, limit: int = 10) -> list[str]:
    """kicad-cli log lines worth passing on (warnings, missing 3D models)."""
    lines = [ln.strip() for ln in output.splitlines() if _LOG_WARNING.search(ln)]
    return [f"kicad-cli: {ln}" for ln in lines[:limit]]


def csv_rows(path: str | Path) -> int:
    """Data rows of a kicad-cli CSV (the header and blank lines not counted)."""
    with open(path, newline="", encoding="utf-8", errors="replace") as f:
        rows = [r for r in csv.reader(f) if any(cell.strip() for cell in r)]
    return max(0, len(rows) - 1)


# --- STEP bounding box ----------------------------------------------------------------------
class StepScanError(ValueError):
    """The file is not a STEP file this scan can measure."""


_NAME = r"(?:'(?:[^']|'')*'|\$)"
_NUM = r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[Ee][-+]?\d+)?"


def _entity(kind: str, body: str) -> re.Pattern:
    return re.compile(rf"#(\d+)\s*=\s*{kind}\s*\(\s*{_NAME}\s*,\s*{body}\s*\)\s*;")


_POINT_RE = _entity("CARTESIAN_POINT", r"\(([^()]*)\)")
_DIRECTION_RE = _entity("DIRECTION", r"\(([^()]*)\)")
_VERTEX_RE = _entity("VERTEX_POINT", r"#(\d+)")
_AXIS_RE = _entity("AXIS2_PLACEMENT_3D", r"#(\d+)\s*,\s*(#\d+|\$)\s*,\s*(#\d+|\$)")
_CIRCLE_RE = _entity("CIRCLE", rf"#(\d+)\s*,\s*({_NUM})")
_EDGE_RE = _entity("EDGE_CURVE", r"#(\d+)\s*,\s*#(\d+)\s*,\s*#(\d+)\s*,\s*\.([TF])\.")
_SI_LENGTH_RE = re.compile(r"SI_UNIT\s*\(\s*(\.[A-Z]+\.|\$)\s*,\s*\.METRE\.\s*\)")
_INCH_RE = re.compile(r"CONVERSION_BASED_UNIT\s*\(\s*'INCH'")
_SI_PREFIX_MM = {"$": 1000.0, ".KILO.": 1e6, ".DECI.": 100.0, ".CENTI.": 10.0,
                 ".MILLI.": 1.0, ".MICRO.": 1e-3, ".NANO.": 1e-6}


def _coords(text: str) -> tuple[float, ...] | None:
    try:
        return tuple(float(v) for v in text.split(","))
    except ValueError:
        return None


def _length_scale(text: str, warnings: list[str]) -> float:
    """mm per file length unit (the first one when several are declared)."""
    found = [_SI_PREFIX_MM.get(m.group(1)) for m in _SI_LENGTH_RE.finditer(text)]
    found += [25.4 for _ in _INCH_RE.finditer(text)]
    known = [s for s in found if s is not None]
    if not known:
        warnings.append("no length unit found in the STEP file: read as mm")
        return 1.0
    if len(set(known)) > 1:
        warnings.append(f"the STEP file declares several length units {sorted(set(known))} "
                        f"(mm per unit): scaled by the first, {known[0]:g}")
    return known[0]


def _sub(a: Vec, b: Vec) -> Vec:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _dot(a: Vec, b: Vec) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _unit(a: Vec) -> Vec:
    n = math.sqrt(_dot(a, a))
    if n <= 1e-15:
        raise StepScanError(f"zero-length direction {a!r}")
    return (a[0] / n, a[1] / n, a[2] / n)


def _frame(axis: tuple, points: dict, dirs: dict) -> tuple[Vec, Vec, Vec]:
    """(origin, x, y) of an AXIS2_PLACEMENT_3D (defaults Z / X when '$')."""
    loc, zref, xref = axis
    z = _unit(dirs.get(zref, (0.0, 0.0, 1.0))[:3])
    x0 = dirs.get(xref, (1.0, 0.0, 0.0) if abs(z[0]) < 0.9 else (0.0, 1.0, 0.0))[:3]
    x = _unit(_sub(x0, tuple(_dot(x0, z) * c for c in z)))
    y = (z[1] * x[2] - z[2] * x[1], z[2] * x[0] - z[0] * x[2], z[0] * x[1] - z[1] * x[0])
    return points[loc][:3], x, y


def _arc_extremes(c: Vec, x: Vec, y: Vec, r: float, v1: Vec, v2: Vec,
                  same_sense: bool) -> list[Vec]:
    """The arc's points where a world coordinate is extreme (the circle
    P(t) = c + r (cos t x + sin t y), from v1 to v2 along the edge)."""
    def angle(p: Vec) -> float:
        d = _sub(p, c)
        return math.atan2(_dot(d, y), _dot(d, x))

    if math.dist(v1, v2) <= 1e-9 * max(1.0, r):
        start, sweep = 0.0, math.tau
    else:
        a1, a2 = angle(v1), angle(v2)
        start, end = (a1, a2) if same_sense else (a2, a1)
        sweep = (end - start) % math.tau
    out = []
    for k in range(3):
        if math.hypot(x[k], y[k]) < 1e-12:
            continue
        phi = math.atan2(y[k], x[k])
        for t in (phi, phi + math.pi):
            if (t - start) % math.tau <= sweep + 1e-12:
                out.append(tuple(c[i] + r * (math.cos(t) * x[i] + math.sin(t) * y[i])
                                 for i in range(3)))
    return out


def _ref_id(token: str) -> int | None:
    """'#12' -> 12; '$' (an omitted optional reference) -> None."""
    return None if token == "$" else int(token[1:])


def _edge_points(text: str, points: dict, verts: dict) -> tuple[list[Vec], int]:
    """Arc extremes of every CIRCLE edge (and the count of such edges)."""
    dirs = {int(m.group(1)): c for m in _DIRECTION_RE.finditer(text)
            if (c := _coords(m.group(2))) is not None and len(c) == 3}
    axes = {int(m.group(1)): (int(m.group(2)), _ref_id(m.group(3)), _ref_id(m.group(4)))
            for m in _AXIS_RE.finditer(text)}
    circles = {int(m.group(1)): (int(m.group(2)), float(m.group(3)))
               for m in _CIRCLE_RE.finditer(text)}
    out: list[Vec] = []
    arcs = 0
    for m in _EDGE_RE.finditer(text):
        a, b = verts.get(int(m.group(2))), verts.get(int(m.group(3)))
        axis_id, r = circles.get(int(m.group(4)), (None, 0.0))
        axis = axes.get(axis_id)
        if a is None or b is None or axis is None or len(points.get(axis[0], ())) != 3:
            continue                            # not a circle edge, or unresolved references
        try:
            c, x, y = _frame(axis, points, dirs)
        except StepScanError:                   # a degenerate placement: vertices only
            continue
        out += _arc_extremes(c, x, y, r, a, b, m.group(5) == "T")
        arcs += 1
    return out, arcs


def _span(values: Iterable[float], scale: float) -> list[float]:
    vals = list(values)
    return [round(min(vals) * scale, 6) + 0.0, round(max(vals) * scale, 6) + 0.0]


def step_bbox(path: str | Path) -> dict[str, Any]:
    """{x, y, z: [min, max] (mm), unit, scale, basis, vertices, arcs, points,
    warnings} of a STEP file (see the module doc). Raises StepScanError when
    the file is not ISO-10303-21 or has no 3D point, OSError when unreadable."""
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    if "ISO-10303-21" not in text[:4096]:
        raise StepScanError(f"{path} is not a STEP (ISO-10303-21) file")
    warnings: list[str] = []
    points = {int(m.group(1)): c for m in _POINT_RE.finditer(text)
              if (c := _coords(m.group(2))) is not None}
    verts = {int(m.group(1)): points[int(m.group(2))][:3] for m in _VERTEX_RE.finditer(text)
             if len(points.get(int(m.group(2)), ())) == 3}
    pts: list[Vec] = list(verts.values())
    arcs = 0
    if pts:
        extra, arcs = _edge_points(text, points, verts)
        pts += extra
        basis = "edges"
    else:
        pts = [c[:3] for c in points.values() if len(c) == 3]
        basis = "points"
        warnings.append("no VERTEX_POINT in the STEP file: the bbox spans every 3D point")
    if not pts:
        raise StepScanError(f"{path} has no 3D CARTESIAN_POINT to measure")
    scale = _length_scale(text, warnings)
    return {"x": _span((p[0] for p in pts), scale), "y": _span((p[1] for p in pts), scale),
            "z": _span((p[2] for p in pts), scale), "unit": "mm", "scale": scale,
            "basis": basis, "vertices": len(verts), "arcs": arcs, "points": len(points),
            "warnings": warnings}


# --- outline comparison ---------------------------------------------------------------------
def board_file_outline(pcb: str | Path) -> ol.Outline:
    """The .kicad_pcb's Edge.Cuts (board and footprint shapes) in the board
    frame with ``datum`` = its aux origin (the sheet origin when unset), read
    as text. Raises ValueError when the shapes do not form a board outline,
    OSError when the file cannot be read."""
    import kicad_import_pcb as kip

    name = Path(pcb).name
    board = kip.read_board(Path(pcb).read_text(encoding="utf-8", errors="replace"))
    try:
        frame = kip.board_frame(board)
    except ValueError as exc:
        raise ValueError(f"{name} has no Edge.Cuts outline ({exc})") from exc
    if frame.problem or frame.info.get("kind") != "edge_cuts":
        raise ValueError(f"{name}: the Edge.Cuts shapes do not form a board outline: "
                         f"{frame.problem or 'no Edge.Cuts shapes on the board'}")
    return ol.from_board_block(frame.block)


def _samples(o: ol.Outline, step: float) -> list[Point]:
    prims = [p for _k, _i, p in all_prims(o)]
    step = max(step, sum(p.length for p in prims) / MAX_SAMPLES)
    out: list[Point] = []
    for prim in prims:
        n = max(1, math.ceil(prim.length / step))
        out += [prim.point_at(i / n) for i in range(n)]
    return out


def _directed(a: ol.Outline, b: ol.Outline, step: float) -> float:
    """max over a's edge samples of the exact distance to b's edges."""
    index = PrimIndex(b)
    worst = 0.0
    for p in _samples(a, step):
        near = index.near((p[0] - NEAR_MM, p[1] - NEAR_MM, p[0] + NEAR_MM, p[1] + NEAR_MM))
        d = min((closest_point(prim, p)[1] for _k, _i, prim in near), default=math.inf)
        worst = max(worst, d if d <= NEAR_MM else ol.distance_to_edge(b, p))
    return worst


def hausdorff(a: ol.Outline, b: ol.Outline, step: float = SAMPLE_MM) -> float:
    """Hausdorff distance (mm) between the edges of two outlines in one frame
    (every ring; edge samples ``step`` mm apart, distances exact)."""
    return max(_directed(a, b, step), _directed(b, a, step))


def frame_hausdorff(reference: ol.Outline, other: ol.Outline) -> float:
    """hausdorff in the external frame: ``other`` moved so its datum lands on
    the reference's (both outlines need a datum)."""
    if reference.datum is None or other.datum is None:
        raise ValueError("comparing outlines in the external frame needs both datums")
    dx, dy = (reference.datum[i] - other.datum[i] for i in (0, 1))
    return hausdorff(reference, ol.translate(other, dx, dy))


# --- netlist edits for the Fusion round trip --------------------------------------------------
def finite(v: object) -> bool:
    """A real, finite number (bool is not one)."""
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _r6(v: float) -> float:
    return round(float(v), 6) + 0.0


def component_list(value: object) -> list:
    """A netlist's ``components`` value as a list (None: no components).
    Raises ValueError for anything else, so a malformed netlist is reported
    instead of failing somewhere inside a check."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("the netlist's components must be a list of component objects, got "
                         f"{type(value).__name__} {value!r:.60}")
    return value


def _ref(comp: object) -> str | None:
    """A component's reference when it is one (a str), else None."""
    ref = comp.get("ref") if isinstance(comp, Mapping) else None
    return ref if isinstance(ref, str) else None


def dxf_board(board: object, geometry: Mapping[str, Any]) -> tuple[dict, list[str]]:
    """The new board block: `geometry` (a {"outline": {"dxf": ...}} reference
    or a BoardOutline.to_board_block()) plus the old block's other keys
    (auto_*, panel_cutouts, ...), and notes naming the old geometry keys (w,
    h, corner_radius, outline, cutouts, datum) it removed or replaced."""
    if board is not None and not isinstance(board, Mapping):
        raise ValueError(f"the netlist's board must be an object, got {board!r}")
    old = dict(board or {})
    keep = {k: v for k, v in old.items() if k not in _GEOMETRY_KEYS}
    replaced = [k for k in _GEOMETRY_KEYS if k in old and k in geometry]
    removed = [k for k in _GEOMETRY_KEYS if k in old and k not in geometry]
    notes = []
    if removed:
        notes.append(f"removed board.{', board.'.join(removed)}: the DXF defines the board shape, "
                     "size, cutouts and datum")
    if replaced:
        notes.append(f"replaced board.{', board.'.join(replaced)} with the DXF's")
    return {**geometry, **keep}, notes


def _current_rot(comp: Mapping) -> float:
    at = comp.get("at")
    if isinstance(at, (list, tuple)) and len(at) == 3 and finite(at[2]):
        return float(at[2])
    return float(comp["rot"]) if finite(comp.get("rot")) else 0.0


def fusion_anchors(parts: Mapping, datum: Sequence[float],
                   components: object) -> dict[str, list[float]]:
    """{ref: [x, y, rot]} D3 ``at`` anchors (board frame) from Fusion-frame
    positions {ref: [x, y, rot]} (mm, Y up, origin = the datum): x/y through
    outline.external_to_board, rot unchanged (Fusion CCW-positive in a Y-up
    frame is KiCad's displayed sense); [x, y] keeps the part's current
    rotation. ``components`` (the netlist's value) is read only when there
    are parts. Raises ValueError for an unknown ref, a malformed entry or
    components that are not a list."""
    if not parts:
        return {}
    comps = {ref: c for c in component_list(components) if (ref := _ref(c)) is not None}
    unknown = sorted(str(r) for r in parts if r not in comps)
    if unknown:
        raise ValueError(f"parts names refs that are not netlist components: {', '.join(unknown)}")
    out = {}
    for ref, raw in parts.items():
        if not isinstance(raw, (list, tuple)) or len(raw) not in (2, 3) \
                or not all(finite(v) for v in raw):
            raise ValueError(f"parts[{ref!r}] must be [x, y, rot] (finite numbers), got {raw!r}")
        x, y = ol.external_to_board(raw[:2], datum)
        out[ref] = [_r6(x), _r6(y), float(raw[2]) if len(raw) == 3 else _current_rot(comps[ref])]
    return out


def _placed(comp: Mapping, at: list[float]) -> dict:
    """A new component with ``at`` where its x / y / rot / at keys were."""
    out: dict = {}
    for key, value in comp.items():
        if key in ("x", "y", "rot", "at"):
            out.setdefault("at", at)
        else:
            out[key] = value
    out.setdefault("at", at)
    return out


def with_anchors(design: Mapping, board: dict, anchors: Mapping[str, list[float]]) -> dict:
    """A new design: ``board`` in place of the old block (same key position)
    and every anchored component placed by ``at`` (x / y / rot removed)."""
    out = {k: (board if k == "board" else v) for k, v in design.items()}
    out.setdefault("board", board)
    if anchors:
        out["components"] = [_placed(c, list(anchors[ref]))
                             if (ref := _ref(c)) is not None and ref in anchors else c
                             for c in component_list(design.get("components"))]
    return out


# --- fixed parts and mounting holes against the outline ---------------------------------------
def _fixed_point(comp: Mapping) -> Point | None:
    at = comp.get("at")
    if isinstance(at, (list, tuple)) and len(at) >= 2 and finite(at[0]) and finite(at[1]):
        return (float(at[0]), float(at[1]))
    if finite(comp.get("x")) and finite(comp.get("y")):
        return (float(comp["x"]), float(comp["y"]))
    return None


def _hole_drill(footprint: object) -> float | None:
    m = _HOLE_DRILL.search(footprint) if isinstance(footprint, str) else None
    return float(m.group(1)) if m else None


def _point_issue(o: ol.Outline, ref: str, p: Point, comp: Mapping) -> oa.Issue | None:
    oh = float(comp["overhang"]) if finite(comp.get("overhang")) else 0.0
    anchor = "footprint origin (at)" if "at" in comp else "courtyard centre (x/y)"
    where = f"{ref}: {anchor} ({p[0]:.3f}, {p[1]:.3f})"
    if ol.contains(o, p):
        d = ol.distance_to_edge(o, p)
        if d < NEAR_EDGE_MM and oh <= 0 and not comp.get("edge"):
            return oa.Issue("part_near_edge", "warning", ref, f"{where} is {d:.3f} mm from the "
                            f"board edge, under {NEAR_EDGE_MM:g} mm ({oa.FRAME})", p)
        return None
    hit = ol.nearest_edge(o, p)
    if hit.ring > 0:
        return oa.Issue("part_in_cutout", "error", ref,
                        f"{where} lies in cutout {hit.ring} ({oa.FRAME})", p)
    if 0 < oh and hit.distance <= oh + oa.TOL_MM:
        return None
    why = (f"beyond its overhang {oh:g} mm" if oh > 0 else
           "give it an 'overhang' (mm) if it is meant to hang over the edge")
    return oa.Issue("part_outside", "error", ref, f"{where} is {hit.distance:.3f} mm off the "
                    f"board outline; {why} ({oa.FRAME})", p)


def part_issues(o: ol.Outline, components: Iterable[Any]) -> list[oa.Issue]:
    """Every fixed part's anchor (``at`` = footprint origin, else x/y =
    courtyard centre) on the board: inside a cutout or off the board is an
    error (a positive ``overhang`` allows that far past the OUTER edge),
    within NEAR_EDGE_MM of an edge a warning (not for overhang / ``edge``
    parts). Mounting-hole footprints are left to hole_issues."""
    out = []
    for comp in components:
        point = _fixed_point(comp) if isinstance(comp, Mapping) else None
        if point is None or _hole_drill(comp.get("footprint")) is not None:
            continue
        issue = _point_issue(o, str(comp.get("ref", "?")), point, comp)
        if issue is not None:
            out.append(issue)
    return out


def _spec_issue(message: str) -> oa.Issue:
    """A malformed ``mounting_holes`` value (the builder could not place it)."""
    return oa.Issue("mounting_holes_spec", "error", None, f"mounting_holes.{message}")


def _is_point(p: object) -> bool:
    return isinstance(p, (list, tuple)) and len(p) == 2 and all(finite(v) for v in p)


def _spec_positions(given: object) -> tuple[list[Point], list[oa.Issue]]:
    """mounting_holes.positions as points, plus one issue naming every
    malformed entry (a position is [x, y], finite numbers, board frame)."""
    if not isinstance(given, (list, tuple)):
        return [], [_spec_issue(f"positions must be a list of [x, y] points (mm), got "
                                f"{given!r:.60}")]
    bad = [f"positions[{k}] = {p!r:.40}" for k, p in enumerate(given) if not _is_point(p)]
    issues = [_spec_issue(f"{', '.join(bad)}: each position must be [x, y], finite numbers "
                          f"({oa.FRAME})")] if bad else []
    return [(float(p[0]), float(p[1])) for p in given if _is_point(p)], issues


def _spec_holes(o: ol.Outline, spec: Mapping, source: str
                ) -> tuple[list[dict], list[oa.Issue]]:
    drill = _hole_drill(spec.get("footprint") or DEFAULT_HOLE_FOOTPRINT) or DEFAULT_HOLE_DRILL_MM
    inset = spec.get("inset", DEFAULT_HOLE_INSET_MM)
    issues = [] if finite(inset) else [_spec_issue(f"inset must be a number (mm), got "
                                                   f"{inset!r:.40}")]
    if spec.get("positions"):
        positions, more = _spec_positions(spec["positions"])
        issues = [*issues, *more]
    elif source not in ("rect", "rounded_rect"):
        return [], [*issues, oa.Issue(
            "mounting_holes_positions", "error", None, "mounting_holes without positions puts "
            "four holes at the bbox corners, which assumes a rectangle: give "
            f"mounting_holes.positions for this {source} outline ({oa.FRAME})")]
    elif issues:
        return [], issues
    else:
        x0, y0, x1, y1 = ol.bbox(o)
        i = float(inset)
        positions = [(x0 + i, y0 + i), (x1 - i, y0 + i), (x1 - i, y1 - i), (x0 + i, y1 - i)]
    return [{"ref": f"H{k}", "center": list(p), "drill": drill}
            for k, p in enumerate(positions, 1)], issues


def hole_issues(o: ol.Outline, design: Mapping, source: str,
                profile: str = "JLCPCB") -> list[oa.Issue]:
    """Mounting holes judged by their drill circle (outline_audit
    mounting_hole_issues): mounting-hole footprints placed by at / x-y, and
    ``mounting_holes`` (its positions, else swig_build_board's bbox-corner
    default, an error on a non-rectangular outline; a malformed inset or
    position is a mounting_holes_spec error). The drill is read from a
    'MountingHole_<d>mm' footprint name, else DEFAULT_HOLE_DRILL_MM. Raises
    ValueError when the components are not a list."""
    holes = [{"ref": str(c.get("ref")), "center": list(p), "drill": d}
             for c in component_list(design.get("components")) if isinstance(c, Mapping)
             and (d := _hole_drill(c.get("footprint"))) is not None
             and (p := _fixed_point(c)) is not None]
    spec, out = design.get("mounting_holes"), []
    if spec:
        more, out = _spec_holes(o, spec if isinstance(spec, Mapping) else {}, source)
        holes = [*holes, *more]
    return out + oa.mounting_hole_issues(o, holes, profile)
