#!/usr/bin/env python3
"""The 'Mechanical' section of the hands-off review (ROADMAP D2, F3-20 / X-03).

review_report wires ``render(report, design)`` into its SECTIONS after 'Fab
package'. It renders ``report['mechanical']``, the record the outline
feature's G5 hook writes (pipeline/rp_feature_outline.g5_mechanical):

  {ok, dir, files, step, dxf, datum_cpl, hausdorff_mm, step_bbox, warnings}
      the Fusion round-trip exports of a non-legacy board: the board-only
      STEP with its bbox (mech_export.step_bbox: {x, y, z: [min, max]} mm in
      the datum frame), the outline DXF read back against the board's
      Edge.Cuts (Hausdorff distance), the mechanical datum CPL (positions
      relative to board.datum; the fab CPL stays absolute) and the warnings
      the G5 verdict also carries; a failed export leaves its path None
  {'skipped': <why>}
      a legacy board (no board block, or a plain rectangle without cutouts
      or datum): one line
  absent
      the run stopped before G5 ('Not produced'), or reached the fab package
      without the outline feature ('Not recorded')

Everything is read tolerantly: a malformed record or odd value renders a
line naming it, never an exception; every rendered line is one Markdown
line and nothing is mutated. Pure: stdlib only (Python 3.11), no KiCad.
"""
from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

SECTION_TITLE = "Mechanical"
# (record key, what), in export order: rp_feature_outline.EXPORTS
EXPORTS: tuple[tuple[str, str], ...] = (
    ("step", "STEP"), ("dxf", "Outline DXF"), ("datum_cpl", "Datum CPL"))
DATUM_FRAME = "datum frame (Fusion): mm, Y up, origin = board.datum (the aux / drill origin)"
BOARD_FRAME = "board frame (mm, Y down, origin = the outline bbox top-left)"
NOT_PRODUCED = ("- Not produced: the run stopped before G5 (the mechanical exports are made "
                "with the fab package).")
NOT_RECORDED = ("- Not recorded: the pipeline report has no mechanical record (the run "
                "predates the outline feature or ran without it).")
NOT_WRITTEN = "not written (see the warnings)"
LISTED = 10                                   # warnings shown before '(+N more)'


# --- formatting (review_report's conventions) ---------------------------------------
def _flat(value: Any) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return " ".join(text.split())


def _code(value: Any) -> str:
    """Inline code that survives backticks in the text."""
    text = _flat(value)
    ticks = "`" * (max(map(len, re.findall(r"`+", text)), default=0) + 1)
    pad = " " if text[:1] == "`" or text[-1:] == "`" else ""
    return f"{ticks}{pad}{text}{pad}{ticks}"


def _one_line(line: str) -> str:
    return line.replace("\r", " ").replace("\n", " ")


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _strings(value: Any) -> list[str]:
    return [_flat(v) for v in value] if isinstance(value, (list, tuple)) else []


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _g(value: Any) -> str:
    """A number with six significant digits and no trailing zeros."""
    return str(value) if isinstance(value, int) else f"{value:.6g}"


def _path(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


# --- the lines ---------------------------------------------------------------------
def _summary_line(mech: Mapping[str, Any]) -> str:
    written = sum(1 for key, _what in EXPORTS if _path(mech.get(key)))
    where = _code(mech["dir"]) if _path(mech.get("dir")) else "an unrecorded folder"
    state = "all ok" if mech.get("ok") is True else "with problems (G5 warnings)"
    return f"- Exports: {written} of {len(EXPORTS)} written to {where}; {state}"


def _span(box: Mapping[str, Any], axis: str) -> str | None:
    span = box.get(axis)
    if isinstance(span, (list, tuple)) and len(span) == 2 and all(map(_number, span)):
        return f"{axis} [{_g(span[0])}, {_g(span[1])}]"
    return None


def _bbox_text(box: Any) -> str:
    spans = [_span(_mapping(box), axis) for axis in ("x", "y", "z")]
    if not isinstance(box, Mapping) or not all(spans):
        return "bbox not computed"
    basis = {"edges": ", from the B-rep edges", "points": ", from every 3D point"}.get(
        box.get("basis"), "")
    return f"bbox {', '.join(s for s in spans if s)} mm{basis} ({DATUM_FRAME})"


def _step_line(mech: Mapping[str, Any]) -> str:
    path = _path(mech.get("step"))
    where = f"{_code(path)} (board only)" if path else NOT_WRITTEN
    return f"- STEP: {where}; {_bbox_text(mech.get('step_bbox'))}"


def _dxf_line(mech: Mapping[str, Any]) -> str:
    path, dist = _path(mech.get("dxf")), mech.get("hausdorff_mm")
    where = _code(path) if path else NOT_WRITTEN
    back = (f"Hausdorff {_g(dist)} mm" if _number(dist) else
            "not measured" if dist is None else f"not measured ({_flat(dist)})")
    return f"- Outline DXF: {where}; read back against the board's Edge.Cuts: {back}"


def _cpl_line(mech: Mapping[str, Any]) -> str:
    path = _path(mech.get("datum_cpl"))
    if not path:
        return f"- Datum CPL: {NOT_WRITTEN}"
    return (f"- Datum CPL: {_code(path)} (positions relative to board.datum; the fab CPL "
            f"stays absolute)")


def _datum_line(design: Mapping[str, Any]) -> str:
    datum = _mapping(design.get("board")).get("datum")
    if isinstance(datum, (list, tuple)) and len(datum) == 2 and all(map(_number, datum)):
        return f"- Datum: ({_g(datum[0])}, {_g(datum[1])}) in the {BOARD_FRAME}"
    return "- Datum: not declared (board.datum); the exports use the board's aux / drill origin"


def _warning_lines(mech: Mapping[str, Any]) -> list[str]:
    warnings = _strings(mech.get("warnings"))
    if not warnings:
        return ["- Warnings: none"]
    more = len(warnings) - LISTED
    return ([f"- Warnings ({len(warnings)}):"] + [f"  - {w}" for w in warnings[:LISTED]]
            + ([f"  - (+{more} more)"] if more > 0 else []))


def _skipped_line(mech: Mapping[str, Any]) -> str:
    return (f"- Skipped: {_flat(mech.get('skipped'))} (a legacy board has no Fusion round-trip "
            f"exports; the fab package is unaffected)")


# --- the hook ------------------------------------------------------------------------
def render(report: Mapping[str, Any], design: Mapping[str, Any] | None = None) -> list[str]:
    """The section body (Markdown lines without the '## Mechanical' heading)
    of a pipeline report; the datum line needs the ``design`` (netlist).
    A record the harness never wrote renders one 'Not produced' / 'Not
    recorded' line; a legacy board's {'skipped': ...} one line; a malformed
    record one line naming it. Never raises on odd input."""
    rep = _mapping(report)
    if "mechanical" not in rep:
        return [NOT_RECORDED if "fab" in rep else NOT_PRODUCED]
    mech = rep["mechanical"]
    if not isinstance(mech, Mapping):
        return [_one_line(f"- Malformed: the pipeline report's mechanical record is not an "
                          f"object ({_flat(mech)})")]
    if "skipped" in mech:
        return [_one_line(_skipped_line(mech))]
    lines = [_summary_line(mech), _step_line(mech), _dxf_line(mech), _cpl_line(mech)]
    if design is not None:
        lines.append(_datum_line(_mapping(design)))
    return [_one_line(ln) for ln in lines + _warning_lines(mech)]
