#!/usr/bin/env python3
"""The KiCad project importer's checks of its own output (ROADMAP_DECISIONS D4).

``check_design`` holds a converted netlist.json to the contract and to the
board it came from; ``ok`` needs every check to have RUN and passed:

* netlist_schema.validate. A validation that could not run (jsonschema
  missing) is ``schema_validated: false`` and fails ``ok``: skipped is not
  passed.
* lint_netlist.
* net_equivalence.compare(netlist, source board), strict, nothing ignored.
  The import renames footprints whose board reference is empty or repeated
  (tiny_tapeout: 130 stitching vias called STITCH1, 136 renamed), so the source board
  is first given those names where the footprints sit
  (``board_as_imported``, by their place among the board's footprints) and
  then read by net_equivalence.pcb_pad_nets: every renamed part's pads are
  compared with the nets the netlist gives them.
* The paths of the written library files (KiCad on Windows cannot open a
  file past MAX_PATH, kicad_import_fplib.too_long_for_kicad).
"""
from __future__ import annotations

import json
import re
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
for _p in (str(_HERE), str(_HERE.parent)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import kicad_import_fplib as kfp  # noqa: E402
import kicad_import_pcb as kpcb  # noqa: E402
import net_equivalence as ne  # noqa: E402
from kicad_import_sch import child_spans  # noqa: E402

_ATOM = r'("(?:[^"\\]|\\.)*"|[^\s()"]+)'
# the reference as net_equivalence and kicad_import_pcb read it: KiCad 8+
# (property "Reference" "R1"), else KiCad <= 7 (fp_text reference "R1")
_REF_PROPERTY = re.compile(r'(\(\s*property\s+"?[Rr]eference"?\s+)' + _ATOM)
_REF_TEXT = re.compile(r"(\(\s*fp_text\s+reference\s+)" + _ATOM)
_FP_NAME = re.compile(r"\(\s*(?:footprint|module)(?:\s+" + _ATOM + ")?")
_ESCAPED = re.compile(r"\\(.)")


def _unquote(atom: str) -> str:
    return _ESCAPED.sub(r"\1", atom[1:-1]) if atom.startswith('"') else atom


def _quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


# ----------------------------------------------------------------------------
# The source board under the import's references
# ----------------------------------------------------------------------------
def _footprint_spans(text: str) -> list[tuple[int, int]]:
    """(start, end) of every top-level footprint, in file order: the order
    of kicad_import_pcb's BoardFootprint.index."""
    root = len(text) - len(text.lstrip(ne._LEADING))
    return [(a, b) for head, a, b in child_spans(text, root, len(text))
            if head in kpcb.FOOTPRINT_HEADS]


def _rename_footprint(block: str, original: str, ref: str, where: str) -> str:
    """The footprint source `block` carrying reference `ref` instead of
    `original` (kicad_import_pcb's reading: the first Reference property,
    else the KiCad <= 7 reference text; none at all reads as "")."""
    spans = child_spans(block, 0, len(block))
    props = [(a, m) for head, a, _b in spans if head == "property"
             and (m := _REF_PROPERTY.match(block, a))]
    texts = [(a, m) for head, a, _b in spans if head == "fp_text"
             and (m := _REF_TEXT.match(block, a))]
    found = props or texts
    current = _unquote(found[0][1].group(2) if props else found[-1][1].group(2)) if found else ""
    if current != original:
        raise ValueError(f"{where} has reference {current!r}, not the {original!r} the import "
                         "renamed: the rename does not match the board")
    if not found:
        head = _FP_NAME.match(block)
        return f'{block[:head.end()]} (property "Reference" {_quote(ref)}){block[head.end():]}'
    out, pos = [], 0
    for _a, m in found:                      # every reference child, so any reader agrees
        out += [block[pos:m.start(2)], _quote(ref)]
        pos = m.end(2)
    return "".join(out) + block[pos:]


def board_as_imported(board_text: str, renamed_refs: Iterable[Mapping[str, Any]]) -> str:
    """The source board with every footprint the import renamed (rows of the
    report's ``renamed_refs``: ``index`` = its place among the board's
    footprints, ``original`` -> ``ref``) carrying its new reference, so a
    strict ref + pad comparison covers the renamed parts too. Raises
    ValueError when a row does not match the footprint at its index."""
    rows = sorted(renamed_refs, key=lambda r: r["index"], reverse=True)
    if not rows:
        return board_text
    text = ne._restore_teardrop_parens(board_text)       # as kicad_import_pcb scans it
    spans = _footprint_spans(text)
    for row in rows:                                     # from the end: offsets stay valid
        index = row["index"]
        if not 0 <= index < len(spans):
            raise ValueError(f"renamed footprint {index} ({row['ref']}): the board has "
                             f"{len(spans)} footprints")
        a, b = spans[index]
        block = _rename_footprint(text[a:b], row["original"], row["ref"],
                                  f"board footprint {index} ({row.get('footprint', '?')})")
        text = text[:a] + block + text[b:]
    return text


# ----------------------------------------------------------------------------
# check_design
# ----------------------------------------------------------------------------
def _schema(design: Mapping[str, Any]) -> dict[str, Any]:
    try:
        import netlist_schema
        return {"schema_validated": True, "schema_errors": netlist_schema.validate(design)}
    except (ImportError, RuntimeError) as e:
        return {"schema_validated": False, "schema_errors": None,
                "schema_note": f"schema not validated, so the import is not ok: {e}"}


def _netlist_vs_pcb(design: Mapping[str, Any], board_text: str,
                    renamed_refs: list[Mapping[str, Any]]) -> dict[str, Any]:
    renamed = {row["ref"]: row["index"] for row in renamed_refs}
    try:
        board = board_as_imported(board_text, renamed_refs)
    except ValueError as e:
        return {"equal": False, "summary": f"not compared: {e}", "error": str(e),
                "renamed_parts": renamed}
    res = ne.compare(ne.netlist_pad_nets(design), ne.pcb_pad_nets(board))
    return {"summary": res.summary(), "renamed_parts": renamed, **res.as_dict()}


def check_design(design: Mapping[str, Any], board_text: str,
                 renamed_refs: Iterable[Mapping[str, Any]] = (),
                 library_files: Iterable[str] = ()) -> dict:
    """The converted design against the contract and its source board (see
    the module doc). ``renamed_refs``: the import report's rows; their parts
    are compared under their new names, at their place on the board."""
    import lint_netlist
    checks: dict[str, Any] = _schema(design)
    checks["lint"] = lint_netlist.lint(json.loads(json.dumps(design)))
    checks["netlist_vs_pcb"] = _netlist_vs_pcb(design, board_text, list(renamed_refs))
    too_long = kfp.too_long_for_kicad(library_files)
    checks["library_paths"] = {"too_long": too_long}
    if too_long:
        checks["library_paths"]["error"] = (
            f"{len(too_long)} library file path(s) exceed {kfp.KICAD_MAX_PATH} characters, "
            "which KiCad cannot open: convert into a shorter output directory")
    checks["ok"] = bool(checks["schema_validated"] and not checks["schema_errors"]
                        and checks["lint"]["ok"] and checks["netlist_vs_pcb"]["equal"]
                        and not too_long)
    return checks
