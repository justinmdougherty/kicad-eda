#!/usr/bin/env python3
"""JLCPCB SMT-assembly pair (BOM.csv + CPL.csv) from a netlist design and the
kicad-cli pick-and-place file, honouring the ROADMAP D4 component flags.

* ``bom_rows(design)``: one BOM line per (value, footprint, lcsc, mpn) group,
  groups in key order, designators natural-sorted (R2 before R10). Parts with
  ``dnp: true``, ``in_bom: false`` or ``board_only: true`` (JSON booleans, as
  netlist_schema types them) are left out and returned as ``excluded``
  {ref: reason}; a part with several flags gets every reason.
* ``read_pos_csv(path)``: the ``kicad-cli pcb export pos --format csv`` file
  (Ref,Val,Package,PosX,PosY,Rot,Side) as a list of dicts of text.
* ``cpl_rows(design, pos_rows)``: a placement row, in pos-file order, for every
  pos row whose ref is a BOM part; pos refs missing from the design, BOM parts
  missing from the pos file, duplicate rows of a BOM part and ref-less pos rows
  are warnings. Excluded parts' rows (duplicates included) are dropped
  silently (``excluded`` already names them).
* ``write_assembly_csvs(design, out_dir, pos_csv)`` writes both files (UTF-8,
  no BOM, csv ``\\r\\n`` rows; headers exactly as the legacy writer's) and
  returns ``{bom, cpl, bom_lines, cpl_rows, excluded, warnings}``. No or an
  unreadable pos file: the BOM only (``cpl`` False) plus a warning.

For a design without those flags the bytes equal the legacy writer's
(board_fab.write_assembly_csvs at X-09) whenever every group's designators
sort the same naturally and lexicographically; texts such as ``4.7µF`` pass
through unchanged. Footprint = the name after the library nickname.

The filtering is ours, not kicad-cli's (no ``--exclude-dnp``), so it also holds
for boards whose footprints lack the DNP / exclude-from-BOM attributes.

Pure apart from the two files written; never mutates its inputs, and importing
it touches nothing (no sys.path change). Stdlib only, Python 3.11 compatible.

CLI: ``assembly_files.py <netlist.json> <out_dir> [<pos.csv>]`` prints the
summary as JSON; exit 0 written, 2 on an unreadable or malformed netlist.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

BOM_FILE, CPL_FILE = "BOM.csv", "CPL.csv"
BOM_HEADER = ("Comment", "Designator", "Footprint", "LCSC Part #", "MPN")
CPL_HEADER = ("Designator", "Mid X", "Mid Y", "Layer", "Rotation")
POS_COLUMNS = ("Ref", "Val", "Package", "PosX", "PosY", "Rot", "Side")
POS_REQUIRED = ("Ref", "PosX", "PosY", "Rot", "Side")
# CPL column <- pos column (the legacy writer's mapping: Layer = kicad-cli Side)
CPL_FROM_POS = ("Ref", "PosX", "PosY", "Side", "Rot")
GROUP_KEYS = ("value", "footprint", "lcsc", "mpn")

REASON_DNP = "dnp"
REASON_NOT_IN_BOM = "in_bom false"
REASON_BOARD_ONLY = "board_only"
# (component key, the JSON boolean that excludes the part, reason)
EXCLUSION_FLAGS = (("dnp", True, REASON_DNP),
                   ("in_bom", False, REASON_NOT_IN_BOM),
                   ("board_only", True, REASON_BOARD_ONLY))

BomRow = tuple[str, str, str, str, str]
CplRow = tuple[str, str, str, str, str]

_DIGIT_RUNS = re.compile(r"([0-9]+)")


# ----------------------------------------------------------------------------
# design side
# ----------------------------------------------------------------------------
def _natural_key(ref: str) -> tuple[str | int, ...]:
    """Text and digit runs alternate (split keeps the runs at odd indices), so
    two keys compare str with str and int with int: 'R10' after 'R2'."""
    return tuple(int(part) if i % 2 else part
                 for i, part in enumerate(_DIGIT_RUNS.split(ref)))


def natural_sorted(refs: Iterable[str]) -> list[str]:
    """Designators in natural order (R2 before R10; R01 before R1 on a tie)."""
    return sorted(refs, key=lambda ref: (_natural_key(ref), ref))


def _text(value: Any) -> str:
    """A CSV cell: None -> "", text unchanged, anything else str()."""
    if value is None:
        return ""
    return value if isinstance(value, str) else str(value)


def _components(design: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """The design's components, validated: objects, each with a text ref."""
    if not isinstance(design, Mapping):
        raise ValueError("design must be a JSON object")
    comps = design.get("components") or []
    if not isinstance(comps, list):
        raise ValueError("design 'components' must be a list")
    for i, comp in enumerate(comps):
        if not isinstance(comp, Mapping):
            raise ValueError(f"component #{i} is not an object")
        ref = comp.get("ref")
        if not isinstance(ref, str) or not ref:
            raise ValueError(f"component #{i} has no 'ref'")
    return comps


def exclusion_reason(comp: Mapping[str, Any]) -> str:
    """Why `comp` is left out of the BOM and CPL ("" = it is assembled)."""
    return ", ".join(reason for key, flag, reason in EXCLUSION_FLAGS
                     if comp.get(key) is flag)


def _partition(design: Mapping[str, Any]
               ) -> tuple[list[Mapping[str, Any]], dict[str, str]]:
    """(BOM components, {excluded ref: reason} natural-sorted)."""
    included: list[Mapping[str, Any]] = []
    excluded: dict[str, str] = {}
    for comp in _components(design):
        reason = exclusion_reason(comp)
        if reason:
            excluded[comp["ref"]] = reason
        else:
            included.append(comp)
    return included, {ref: excluded[ref] for ref in natural_sorted(excluded)}


def bom_rows(design: Mapping[str, Any]) -> tuple[list[BomRow], dict[str, str]]:
    """BOM lines in BOM_HEADER order and the excluded parts {ref: reason}."""
    included, excluded = _partition(design)
    groups: dict[tuple[str, ...], list[str]] = {}
    for comp in included:
        key = tuple(_text(comp.get(k)) for k in GROUP_KEYS)
        groups.setdefault(key, []).append(comp["ref"])
    rows = [(value, ",".join(natural_sorted(refs)), footprint.split(":")[-1], lcsc, mpn)
            for (value, footprint, lcsc, mpn), refs in sorted(groups.items())]
    return rows, excluded


# ----------------------------------------------------------------------------
# pick-and-place side
# ----------------------------------------------------------------------------
def read_pos_csv(path: str | os.PathLike[str]) -> list[dict[str, str]]:
    """The kicad-cli pos CSV as dicts of text (missing cells ""). A UTF-8 BOM
    is tolerated. Raises ValueError when the file is not a pos CSV, OSError
    when it cannot be opened."""
    try:
        with open(path, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            header = list(reader.fieldnames or [])
            rows = list(reader)
    except (UnicodeDecodeError, csv.Error) as exc:
        raise ValueError(f"{os.fspath(path)} is not a readable kicad-cli pos CSV: "
                         f"{exc}") from exc
    if not header:
        raise ValueError(f"{os.fspath(path)} has no header row")
    missing = [col for col in POS_REQUIRED if col not in header]
    if missing:
        raise ValueError(f"{os.fspath(path)} is missing column(s) {', '.join(missing)} "
                         f"(kicad-cli pos header: {','.join(POS_COLUMNS)})")
    return [{col: _text(row.get(col)) for col in header} for row in rows]


def _disagreements(counts: Mapping[str, int], design_refs: set[str],
                   bom_refs: set[str]) -> list[str]:
    """Warnings for duplicate pos rows of a BOM part (an excluded part's rows
    are all dropped), pos refs the design does not have and BOM parts the pos
    file does not place (each list natural-sorted)."""
    refs = natural_sorted(counts)
    return [
        *(f"pos ref {ref} appears {counts[ref]} times; the first placement is used"
          for ref in refs if counts[ref] > 1 and ref in bom_refs),
        *(f"pos ref {ref} is not in the design; no CPL row"
          for ref in refs if ref not in design_refs),
        *(f"BOM part {ref} has no placement in the pos file; no CPL row"
          for ref in natural_sorted(bom_refs - set(counts))),
    ]


def cpl_rows(design: Mapping[str, Any], pos_rows: Sequence[Mapping[str, Any]]
             ) -> tuple[list[CplRow], list[str]]:
    """Placement rows (CPL_HEADER order, pos-file order) for the BOM parts,
    and the warnings about pos / design disagreements (ref-less rows first)."""
    included, excluded = _partition(design)
    bom_refs = {comp["ref"] for comp in included}
    rows: list[CplRow] = []
    warnings: list[str] = []
    counts: Counter[str] = Counter()
    for n, pos in enumerate(pos_rows, start=1):
        ref = _text(pos.get("Ref"))
        if not ref:
            warnings.append(f"pos row {n} has no Ref; skipped")
            continue
        counts[ref] += 1
        if counts[ref] == 1 and ref in bom_refs:
            rows.append(tuple(_text(pos.get(col)) for col in CPL_FROM_POS))
    return rows, warnings + _disagreements(counts, bom_refs | set(excluded), bom_refs)


# ----------------------------------------------------------------------------
# files
# ----------------------------------------------------------------------------
def _write_csv(path: Path, header: Sequence[str], rows: Iterable[Sequence[str]]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        writer.writerows(rows)


def write_assembly_csvs(design: Mapping[str, Any], out_dir: str | os.PathLike[str],
                        pos_csv: str | os.PathLike[str] | None) -> dict[str, Any]:
    """Write <out_dir>/BOM.csv and, when `pos_csv` is a readable kicad-cli pos
    file, <out_dir>/CPL.csv. Returns {bom, cpl, bom_lines, cpl_rows,
    excluded {ref: reason}, warnings} (JSON-ready)."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    bom, excluded = bom_rows(design)
    _write_csv(out / BOM_FILE, BOM_HEADER, bom)
    result: dict[str, Any] = {"bom": True, "cpl": False, "bom_lines": len(bom),
                              "cpl_rows": 0, "excluded": excluded, "warnings": []}
    shown = os.fspath(pos_csv) if pos_csv else ""
    if not shown or not Path(shown).is_file():
        return {**result,
                "warnings": [f"no pick-and-place file {shown!r}; CPL.csv not written"]}
    try:
        pos = read_pos_csv(shown)
    except (OSError, ValueError) as exc:
        return {**result,
                "warnings": [f"pick-and-place file unreadable: {exc}; CPL.csv not written"]}
    cpl, warnings = cpl_rows(design, pos)
    _write_csv(out / CPL_FILE, CPL_HEADER, cpl)
    return {**result, "cpl": True, "cpl_rows": len(cpl), "warnings": warnings}


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="JLCPCB BOM.csv + CPL.csv from a netlist.json "
                                             "and a kicad-cli pos CSV (D4 flags honoured).")
    ap.add_argument("netlist", help="netlist.json")
    ap.add_argument("out_dir", help="directory for BOM.csv / CPL.csv")
    ap.add_argument("pos", nargs="?", default="", help="kicad-cli pos CSV (optional)")
    args = ap.parse_args(argv)
    try:
        design = json.loads(Path(args.netlist).read_text(encoding="utf-8"))
        result = write_assembly_csvs(design, args.out_dir, args.pos)
    except (OSError, ValueError) as exc:
        print(f"assembly_files: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
