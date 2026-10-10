#!/usr/bin/env python3
"""Outline e2e check (F3-21a): a board the gated harness built from a netlist with
a board outline (ROADMAP D2), judged against that outline. A run_all.json
post_check ("python": "kicad"): KiCad's bundled Python, stdin closed.

Usage: <kicad-python> pipeline/e2e_outline_check.py <pcb> <netlist> [<report>]
                                                    [--caught <REF>]

The outline is the netlist's board block resolved by outline_resolve (a DXF
outline: the builder's <pcb stem>.outline.json sidecar, as KiCad's Python has
no ezdxf); a legacy board (no outline, corner_radius, cutouts or datum) is not
an outline board and cannot be checked. Checks:
  area        KiCad's Edge.Cuts polygon (board_outline.polygon_outlines, cutouts
              removed) has the resolved outline's area +/- AREA_TOL_MM2
  aux_origin  the aux (drill) origin is the datum (board.datum, else the outline
              bbox bottom-left: build_features.datum_board) plus the sheet offset
              (ox, oy) of the Edge.Cuts bbox top-left, +/- ORIGIN_TOL_MM
  audit       no outline_audit courtyard / pad / copper error on the final board
              (swig_outline_checks' G3 courtyard and pad rules and G4 copper
              containment, re-run after the EMC stitching, which runs after G4:
              kicad-cli DRC misses copper lying wholly inside a cutout)
  mechanical  with <report>: report['mechanical'] (G5, rp_feature_outline) is
              ok; the STEP (mech_export.step_bbox, read from the file) spans the
              outline bbox in the Fusion frame (mm, Y up, origin = the datum)
              +/- STEP_BBOX_TOL_MM; the outline DXF read-back Hausdorff is <=
              mech_export.HAUSDORFF_TOL_MM; the datum CPL has at least one row
--caught REF (an expected G3 failure, e.g. examples/outline_negative_cutout):
  area and aux_origin as above, then the G3 outline rules must give a
  courtyard_outside error naming REF (the stable G3 code: the gate's blocker
  texts carry the message, not the code); audit and mechanical are skipped.

Prints ONE JSON line {ok, pcb, netlist, mode, outline_source, checks, problems}.
Exit 0 when every check passes, 1 when one fails, 2 on a usage error or when
nothing can be checked (no pcbnew, an unreadable board / netlist / report, a
legacy or unresolvable board block).
"""
from __future__ import annotations

import json
import math
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, NamedTuple

TOOLS = Path(__file__).resolve().parents[1] / "server" / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import kicad_quiet  # noqa: E402,F401  (before pcbnew: no modal wx dialogs)

try:
    import pcbnew  # noqa: E402
except ImportError:              # the dev venv: the pure helpers only
    pcbnew = None

import board_outline as bo  # noqa: E402
import build_features as bfe  # noqa: E402
import mech_export as mx  # noqa: E402
import outline as ol  # noqa: E402
import outline_resolve as orz  # noqa: E402
import swig_outline_checks as soc  # noqa: E402

USAGE = "usage: e2e_outline_check.py <pcb> <netlist> [<report>] [--caught <REF>]"
CAUGHT_FLAG = "--caught"
EXIT_OK, EXIT_FAIL, EXIT_USAGE = 0, 1, 2
AREA_TOL_MM2 = 0.05
ORIGIN_TOL_MM = 0.001
STEP_BBOX_TOL_MM = 0.05
AUDIT_CODES = (soc.COURTYARD_OUTSIDE, soc.PAD_EDGE_CLEARANCE, soc.COPPER_OUTSIDE)
Finding = soc.Finding
Span = tuple[float, float]


class UsageError(ValueError):
    """Bad command-line arguments."""


class CheckError(RuntimeError):
    """Nothing can be checked: the reason goes in the JSON's error."""


class Args(NamedTuple):
    pcb: str
    netlist: str
    report: str | None = None
    caught: str | None = None


def parse_args(argv: Sequence[str]) -> Args:
    """<pcb> <netlist> [<report>] [--caught REF], the flag anywhere."""
    rest: list[str] = []
    caught, i = None, 0
    while i < len(argv):
        arg = argv[i]
        if arg == CAUGHT_FLAG:
            if i + 1 >= len(argv) or not argv[i + 1].strip() or argv[i + 1].startswith("-"):
                raise UsageError(f"{CAUGHT_FLAG} needs a reference (e.g. {CAUGHT_FLAG} C9)")
            caught, i = argv[i + 1], i + 2
            continue
        if arg.startswith("--"):
            raise UsageError(f"unknown option {arg}")
        rest, i = [*rest, arg], i + 1
    if len(rest) not in (2, 3):
        raise UsageError(f"expected <pcb> <netlist> [<report>], got {len(rest)} argument(s)")
    return Args(rest[0], rest[1], rest[2] if len(rest) == 3 else None, caught)


# --- the outline the board must have (pure) ---------------------------------------------------
def board_block(design: Mapping, netlist_dir: Path, pcb: str) -> tuple[dict, str]:
    """(the resolved board block, 'netlist' | 'sidecar'): a DXF outline comes from
    the builder's sidecar (KiCad's Python cannot read DXF), anything else from
    outline_resolve. Raises CheckError."""
    board = design.get("board")
    spec = board.get("outline") if isinstance(board, Mapping) else None
    if isinstance(spec, Mapping) and "dxf" in spec:
        path = orz.sidecar_path(pcb)
        if not Path(path).is_file():
            raise CheckError(f"a DXF outline is checked against the builder's sidecar "
                             f"{Path(path).name} beside the board, which is missing")
        try:
            return dict(orz.read_sidecar(path)["board"]), "sidecar"
        except orz.ResolveError as e:
            raise CheckError(str(e)) from e
    try:
        return orz.resolve_board(design, netlist_dir).block, "netlist"
    except orz.ResolveError as e:
        raise CheckError(f"the netlist board block does not resolve: {e}") from e


def resolved_outline(design: Mapping, netlist_dir: Path, pcb: str) -> tuple[ol.Outline, str]:
    """(the board-frame outline, its source); CheckError for a legacy board."""
    block, source = board_block(design, netlist_dir, pcb)
    if bfe.is_legacy_block(block):
        raise CheckError("not an outline board: the board block has no outline, corner_radius, "
                         "cutouts or datum (a legacy w x h rectangle), so there is nothing to "
                         "check")
    try:
        return ol.from_board_block(block), source
    except (TypeError, ValueError) as e:
        raise CheckError(f"the board block is not a valid outline: {e}") from e


def datum_board(outline: ol.Outline) -> tuple[float, float]:
    """The builder's aux-origin rule: board.datum, else the bbox bottom-left."""
    return bfe.datum_board(outline)


def _r(v: float) -> float:
    return round(float(v), 6) + 0.0


def fusion_bbox(outline: ol.Outline) -> tuple[Span, Span]:
    """((x min, x max), (y min, y max)) of the outline in the Fusion frame (mm, Y
    up, origin = the datum): where a --drill-origin STEP must land."""
    x0, y0, x1, y1 = ol.bbox(outline)
    dx, dy = datum_board(outline)
    return ((_r(x0 - dx), _r(x1 - dx)), (_r(dy - y1), _r(dy - y0)))


def area_problem(want_mm2: float, got_mm2: float) -> str | None:
    off = abs(got_mm2 - want_mm2)
    if off <= AREA_TOL_MM2 + 1e-9:
        return None
    return (f"Edge.Cuts polygon area {got_mm2:.4f} mm^2 differs from the netlist outline's "
            f"{want_mm2:.4f} mm^2 by {off:.4f} (tolerance {AREA_TOL_MM2:g})")


def origin_problem(want: Sequence[float], got: Sequence[float]) -> str | None:
    off = max(abs(float(g) - float(w)) for g, w in zip(got, want, strict=True))
    if off <= ORIGIN_TOL_MM + 1e-9:
        return None
    return (f"aux origin ({got[0]:.4f}, {got[1]:.4f}) (sheet mm) is not the datum + the board "
            f"offset ({want[0]:.4f}, {want[1]:.4f}): {off:.4f} mm off (tolerance "
            f"{ORIGIN_TOL_MM:g})")


# --- findings (pure) ---------------------------------------------------------------------------
def audit_problems(findings: Sequence[Finding]) -> list[str]:
    """Every courtyard / pad / copper ERROR of the outline rules, '<code>: <text>'."""
    return [f"{f.code}: {f.text}" for f in findings
            if f.severity == "error" and f.code in AUDIT_CODES]


def caught_problem(findings: Sequence[Finding], ref: str) -> str | None:
    """None when a courtyard_outside error names `ref`, else why not."""
    if any(f.code == soc.COURTYARD_OUTSIDE and f.severity == "error"
           and f.text.startswith(f"{ref}:") for f in findings):
        return None
    seen = sorted({f"{f.code} ({f.text.split(':', 1)[0]})" for f in findings
                   if f.severity == "error"})
    return (f"the G3 outline rules gave no {soc.COURTYARD_OUTSIDE} error naming {ref} "
            f"(errors: {', '.join(seen) or 'none'})")


# --- G5 mechanical (pure; the STEP reader is injected) -----------------------------------------
def _step_problems(path: object, want: tuple[Span, Span],
                   step_bbox: Callable[[str], Mapping]) -> list[str]:
    if not isinstance(path, str) or not Path(path).is_file():
        return [f"no STEP export (report['mechanical']['step'] = {path!r})"]
    try:
        box = step_bbox(path)
        got = ((float(box["x"][0]), float(box["x"][1])), (float(box["y"][0]), float(box["y"][1])))
    except (OSError, ValueError, KeyError, TypeError, IndexError) as e:
        return [f"STEP bbox not read from {path}: {type(e).__name__}: {e}"]
    off = max(abs(g - w) for gs, ws in zip(got, want, strict=True)
              for g, w in zip(gs, ws, strict=True))
    if off <= STEP_BBOX_TOL_MM + 1e-9:
        return []
    return [f"STEP bbox x {list(got[0])} y {list(got[1])} (Fusion frame, mm) is not the "
            f"outline's x {list(want[0])} y {list(want[1])} within {STEP_BBOX_TOL_MM:g} mm "
            f"({off:.4f} off)"]


def _hausdorff_problems(value: object) -> list[str]:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return [f"outline DXF read-back Hausdorff missing from the report (got {value!r})"]
    if value > mx.HAUSDORFF_TOL_MM + 1e-12:
        return [f"outline DXF read-back Hausdorff {value:g} mm > {mx.HAUSDORFF_TOL_MM:g} mm"]
    return []


def _cpl_problems(path: object) -> list[str]:
    if not isinstance(path, str) or not Path(path).is_file():
        return [f"no datum CPL export (report['mechanical']['datum_cpl'] = {path!r})"]
    try:
        rows = mx.csv_rows(path)
    except OSError as e:
        return [f"datum CPL {path} unreadable: {e}"]
    return [] if rows >= 1 else [f"datum CPL {path} has no rows (no placed part)"]


def mechanical_problems(report: Mapping, want: tuple[Span, Span],
                        step_bbox: Callable[[str], Mapping] = mx.step_bbox) -> list[str]:
    """The G5 mechanical exports of a pipeline report against the outline's
    Fusion-frame bbox `want` (fusion_bbox)."""
    mech = report.get("mechanical")
    if not isinstance(mech, Mapping):
        return ["no G5 mechanical exports in the report (report['mechanical'] is missing: the "
                "run never reached G5, or the outline feature did not run)"]
    if mech.get("skipped"):
        return [f"the G5 mechanical exports were skipped ({mech['skipped']}) on an outline board"]
    problems = [] if mech.get("ok") else [
        "report['mechanical'] is not ok: " + ("; ".join(map(str, mech.get("warnings") or []))
                                              or "no reason given")]
    return (problems + _step_problems(mech.get("step"), want, step_bbox)
            + _hausdorff_problems(mech.get("hausdorff_mm")) + _cpl_problems(mech.get("datum_cpl")))


# --- the board (pcbnew) ------------------------------------------------------------------------
def _check(problems: list[str], **data: Any) -> dict:
    return {"ok": not problems, **data, "problems": problems}


def area_check(board: Any, outline: ol.Outline) -> dict:
    try:
        got = bo.polygon_outlines(board).Area() / bo.NM_PER_MM ** 2
    except bo.OutlineBuildError as e:
        return _check([f"KiCad cannot build the Edge.Cuts polygon: {e}"])
    want = ol.area(outline)
    found = area_problem(want, got)
    return _check([found] if found else [], kicad_mm2=_r(got), netlist_mm2=_r(want))


def origin_check(board: Any, outline: ol.Outline, shape: soc.BoardShape) -> dict:
    if shape.origin is None:
        return _check(["the board's Edge.Cuts outline could not be read, so its sheet offset "
                       "is unknown: " + "; ".join(f.text for f in shape.findings)])
    dx, dy = datum_board(outline)
    want = (dx + shape.origin[0], dy + shape.origin[1])
    got = bo.get_aux_origin(board)
    found = origin_problem(want, got)
    return _check([found] if found else [], sheet=[_r(v) for v in got],
                  expected=[_r(v) for v in want], datum_board=[_r(dx), _r(dy)],
                  board_offset=[_r(v) for v in shape.origin])


def _shape_problems(shape: soc.BoardShape) -> list[str]:
    if shape.outline is not None:
        return []
    return ["the outline rules cannot run: " + ("; ".join(f.text for f in shape.findings)
                                                or "no Edge.Cuts outline")]


def audit_check(board: Any, design: Mapping, shape: soc.BoardShape) -> dict:
    """The G3 courtyard / pad rules and the G4 copper containment on the board."""
    broken = _shape_problems(shape)
    if broken:
        return _check(broken)
    found = [*soc.placement_findings(board, design, shape), *soc.routed_findings(board, shape)]
    return _check(audit_problems(found), findings=len(found))


def caught_check(board: Any, design: Mapping, shape: soc.BoardShape, ref: str) -> dict:
    """The G3 outline rules on the placed board must flag `ref` courtyard_outside."""
    broken = _shape_problems(shape)
    if broken:
        return _check(broken, ref=ref)
    found = soc.placement_findings(board, design, shape)
    hit = caught_problem(found, ref)
    return _check([hit] if hit else [], ref=ref, code=soc.COURTYARD_OUTSIDE,
                  texts=[f.text for f in found if f.code == soc.COURTYARD_OUTSIDE])


# --- running it --------------------------------------------------------------------------------
def _read_json(path: str, what: str) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as e:
        raise CheckError(f"cannot read the {what} {path}: {e}") from e
    if not isinstance(data, dict):
        raise CheckError(f"the {what} {path} is not a JSON object")
    return data


def _load_board(pcb: str) -> Any:
    try:
        return bo.load_board(pcb)
    except (FileNotFoundError, RuntimeError) as e:
        raise CheckError(str(e)) from e


def run_checks(args: Args) -> dict:
    """Every check of the module doc for `args`; CheckError when none can run."""
    design = _read_json(args.netlist, "netlist")
    report = _read_json(args.report, "pipeline report") if args.report else None
    outline, source = resolved_outline(design, Path(args.netlist).resolve().parent, args.pcb)
    board = _load_board(args.pcb)
    shape = soc.read_shape(board)
    checks = {"area": area_check(board, outline), "aux_origin": origin_check(board, outline, shape)}
    if args.caught:
        checks["caught"] = caught_check(board, design, shape, args.caught)
    else:
        checks["audit"] = audit_check(board, design, shape)
        if report is not None:
            want = fusion_bbox(outline)
            checks["mechanical"] = _check(mechanical_problems(report, want),
                                          fusion_bbox=[list(want[0]), list(want[1])])
    problems = [f"{name}: {p}" for name, c in checks.items() for p in c["problems"]]
    return {"ok": not problems, "pcb": args.pcb, "netlist": args.netlist,
            "mode": "caught" if args.caught else "outline", "outline_source": source,
            "checks": checks, "problems": problems}


def _emit(result: Mapping) -> None:
    print(json.dumps(result, default=str))


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = parse_args(list(sys.argv[1:] if argv is None else argv))
    except UsageError as e:
        print(f"e2e_outline_check: error: {e}\n{USAGE}", file=sys.stderr)
        _emit({"ok": False, "error": f"usage: {e}"})
        return EXIT_USAGE
    if pcbnew is None:
        _emit({"ok": False, "pcb": args.pcb, "error": "pcbnew is not importable: run this under "
               "KiCad's bundled Python (a run_all.json post_check with \"python\": \"kicad\")"})
        return EXIT_USAGE
    try:
        result = run_checks(args)
    except CheckError as e:
        _emit({"ok": False, "pcb": args.pcb, "netlist": args.netlist, "error": str(e)})
        return EXIT_USAGE
    _emit(result)
    return EXIT_OK if result["ok"] else EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
