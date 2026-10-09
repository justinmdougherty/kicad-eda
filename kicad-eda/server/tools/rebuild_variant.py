#!/usr/bin/env python3
"""Keep / reflow variants of an imported design (ROADMAP_DECISIONS D4 rebuild).

An import (kicad_import, import_pipeline) is a ``keep`` design: every part at
its source origin (``at``). ``make_variant`` turns it into the design a
rebuild runs:

* ``keep``: checks that every schematic (non ``board_only``) part has ``at``
  (``errors`` name the parts without one) and returns the design unchanged.
* ``reflow``: a new form factor. The ``board`` block is replaced by `board`
  (a D2 block: w/h/corner_radius, outline loop, cutouts, datum, or a
  ``{"dxf": ...}`` outline passed through for the F3 loader; None keeps the
  source board); ``at`` / ``x`` / ``y`` / ``rot`` are removed from every part
  but `keep_refs` (each kept origin must lie inside the new outline);
  ``rebuild`` becomes "reflow". Board-only parts without copper pads
  (logos, artwork) are dropped (`drop_graphics`). Hole parts (only NPTH /
  unnumbered pads, or a MountingHole footprint) follow `holes`: "source" =
  ``mounting_holes: {"footprint": <their footprint>, "inset": 3.5}``,
  "corners" = ``mounting_holes: true``, "none" = just dropped. A dropped
  pad on a net with other pads is removed from it with a note and an
  assumption; a net left with one pad turns that pad into a ``no_connect``
  (D4) with an assumption; a net of dropped pads only disappears (with its
  net_specs / power_flags entries). Preroutes (source-frame copper) are
  dropped. The design gains ``variant: {mode, from, dropped_refs, holes}``
  (dropped_refs also keeps an earlier variant's) and D5 assumptions
  ``{id "variant-<n>", topic, value, rationale, confidence "med"}``.

Footprints are classified from their pads (design_paths.footprint_pad_summary
through the libraries block, a project fp-lib-table or `stock_dir`), or by a
caller's `pad_summary` ("Nick:Name" -> summary, None when not found; it may
raise OSError / ValueError for a file it cannot read). A board-only part whose
footprint is not found, and any part whose footprint file cannot be read, is
kept with a note (the note gives the reason). Malformed ``nets`` /
``no_connect`` / ``assumptions`` (each an object with an ``id``) / ``variant``
raise ValueError in both modes (design_paths.check_records).

Pure (returns new dicts, never mutates its input). Stdlib + outline (D2
geometry); outline_resolve / dxf_io only to check keep_refs against a DXF.

CLI: ``rebuild_variant.py <netlist.json> <out_dir> [--mode reflow|keep]
[--board <json | file>] [--keep-refs J1,J2] [--holes source|corners|none]
[--keep-graphics] [--footprints-dir DIR]`` writes ``<out_dir>/netlist.json``
with its paths relocated (design_paths.relocate_libraries) and prints a JSON
summary; exit 0 written, 1 variant errors (nothing written), 2 usage/IO error.
A relative DXF path in --board is relative to the board file (inline: cwd).
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import sys
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import design_paths as dp  # noqa: E402
import outline as ol  # noqa: E402
from sexpr_read import natural_key  # noqa: E402

MODES = ("keep", "reflow")                      # netlist_schema.REBUILD_MODES
HOLE_POLICIES = ("source", "corners", "none")   # netlist_schema.VARIANT_HOLES
HOLE_INSET_MM = 3.5
PLACEMENT_KEYS = ("at", "x", "y", "rot")
ASSUMPTION_PREFIX = "variant-"
CONFIDENCE = "med"
NETLIST_FILE = "netlist.json"
MOUNTING_HOLE_WORD = "mountinghole"             # KiCad's MountingHole:* naming, any case
FRAME_KEYS = ("height_zones", "antenna_keepout")   # coordinates in the source board frame
# read by shape (design_paths.check_records); net_specs / power_flags are type-guarded
RECORD_KEYS = ("nets", "no_connect", "assumptions", "variant")

PadSummary = Callable[[str], "Mapping[str, Any] | None"]
Draft = tuple[str, str, str]                    # assumption (topic, value, rationale)


@dataclass(frozen=True)
class VariantResult:
    """make_variant's result; ``design`` is a new dict (written only when ``ok``)."""
    design: dict[str, Any]
    assumptions: tuple[dict[str, Any], ...] = ()
    dropped_refs: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.errors

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "design": copy.deepcopy(self.design),
                "assumptions": [dict(a) for a in self.assumptions],
                "dropped_refs": list(self.dropped_refs), "notes": list(self.notes),
                "errors": list(self.errors)}


@dataclass(frozen=True)
class _NetStrip:
    """The nets without the dropped parts' pads."""
    nets: dict[str, list]
    no_connect: list
    removed: tuple[str, ...]
    lost: Mapping[str, list[str]] = field(default_factory=dict)   # ref -> ["H1.1 on GND"]
    lone: tuple[tuple[list, str, tuple[str, ...]], ...] = ()       # (pad, net, by refs)


# --- inputs -------------------------------------------------------------------------------
def _check_args(mode: str, holes: str, keep_refs: object) -> tuple[str, ...]:
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}, got {mode!r}")
    if holes not in HOLE_POLICIES:
        raise ValueError(f"holes must be one of {HOLE_POLICIES}, got {holes!r}")
    if isinstance(keep_refs, (str, bytes)) or not isinstance(keep_refs, Iterable):
        raise TypeError(f"keep_refs must be a list of references, not {keep_refs!r}")
    refs = tuple(keep_refs)
    if not all(isinstance(r, str) and r for r in refs):
        raise TypeError(f"keep_refs must be a list of references (strings), not {refs!r}")
    return refs


def _components(design: object) -> list[Mapping[str, Any]]:
    """The design's parts; ValueError when they, the nets or no_connect are malformed."""
    comps = design.get("components") if isinstance(design, Mapping) else None
    if not isinstance(comps, list) or not all(isinstance(c, Mapping)
                                              and isinstance(c.get("ref"), str) for c in comps):
        raise ValueError("the design needs 'components': a list of objects with a 'ref'")
    dp.check_records(design, "design", RECORD_KEYS)
    return comps


def _natural(refs: Iterable[str]) -> tuple[str, ...]:
    return tuple(sorted(set(refs), key=natural_key))


def _origin(comp: Mapping[str, Any]) -> tuple[float, float] | None:
    """The placed point of a part: its ``at`` origin, else x/y; None if unplaced."""
    raw = comp.get("at")
    pair = raw[:2] if isinstance(raw, list) and len(raw) >= 2 else (comp.get("x"), comp.get("y"))
    if all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in pair):
        return float(pair[0]), float(pair[1])
    return None


# --- classification ---------------------------------------------------------------------
def classify_part(comp: Mapping[str, Any], summary: Mapping[str, Any] | None) -> str | None:
    """"hole" (a MountingHole footprint, or drilled pads none of which can
    carry a net: NPTH / unnumbered), "artwork" (board-only, no copper pad) or
    None (a real part, or unknown: `summary` None)."""
    if MOUNTING_HOLE_WORD in str(comp.get("footprint", "")).lower():
        return "hole"
    if summary is None:
        return None
    if summary.get("pads") and summary.get("drilled") and not summary.get("electrical"):
        return "hole"
    if comp.get("board_only") is True and not summary.get("copper"):
        return "artwork"
    return None


def default_pad_summary(design: Mapping[str, Any], netlist_dir: dp.PathArg,
                        stock_dir: dp.PathArg | None = None) -> PadSummary:
    """"Nick:Name" -> design_paths.footprint_pad_summary of its file, cached;
    None when the footprint is not found. Raises ValueError with the reason
    when its file cannot be read or is not a footprint (every call), and at
    once on a malformed libraries block."""
    locate = dp.footprint_locator(design, netlist_dir, stock_dir)
    cache: dict[str, Mapping[str, Any] | str | None] = {}     # a str is the read error

    def summary(fp_id: str) -> Mapping[str, Any] | None:
        if fp_id not in cache:
            path = locate(fp_id)
            try:
                cache[fp_id] = None if path is None else dp.footprint_pad_summary(path)
            except (OSError, ValueError) as e:
                cache[fp_id] = str(e) or type(e).__name__
        hit = cache[fp_id]
        if isinstance(hit, str):
            raise ValueError(hit)
        return hit

    return summary


def _no_summary(_fp_id: str) -> None:
    """The pad summary when footprints cannot be resolved at all."""
    return None


def _summary_of(summary: PadSummary, fp: str) -> tuple[Mapping[str, Any] | None, str | None]:
    """(the pad summary of `fp` or None, why its file cannot be read or None)."""
    try:
        return summary(fp), None
    except (OSError, ValueError) as e:
        return None, str(e) or type(e).__name__


def _unclassified_note(comp: Mapping[str, Any], fp: str, error: str | None) -> list[str]:
    if error is not None:
        return [f"{comp['ref']}: footprint {fp} cannot be read ({error}), so it cannot be told "
                "from artwork or a hole; kept"]
    if comp.get("board_only") is True:
        return [f"{comp['ref']}: footprint {fp} not found, so it cannot be told from artwork; "
                "kept"]
    return []


def _classify_all(comps: list[Mapping[str, Any]], keep: frozenset[str], summary: PadSummary,
                  drop_graphics: bool) -> tuple[list, list, list[str]]:
    """(hole parts, artwork parts, notes) among the parts not kept in place."""
    holes: list[Mapping[str, Any]] = []
    artwork: list[Mapping[str, Any]] = []
    notes: list[str] = []
    for comp in comps:
        if comp["ref"] in keep:
            continue
        fp = str(comp.get("footprint", ""))
        info, error = _summary_of(summary, fp)
        kind = classify_part(comp, info)
        if kind == "hole":
            holes.append(comp)
        elif kind == "artwork" and drop_graphics:
            artwork.append(comp)
        elif kind is None and info is None:
            notes += _unclassified_note(comp, fp, error)
    return holes, artwork, notes


# --- board ----------------------------------------------------------------------------------
def _choose_board(design: Mapping[str, Any], board: object) -> tuple[object, list[str]]:
    """(the variant's board block or None for none, notes)."""
    if board is None:
        if design.get("board") is not None:
            return copy.deepcopy(design["board"]), ["reflow keeps the source board (no new "
                                                    "board given)"]
        return None, ["no board block: the pipeline sizes the board to its parts"]
    return copy.deepcopy(board), []


def _outline(board: object, netlist_dir: dp.PathArg,
             need: bool) -> tuple[ol.Outline | None, list[str], list[str]]:
    """(outline, notes, errors). A DXF outline is only read when keep_refs
    must be checked against it (`need`)."""
    if not board:
        return None, [], (["keep_refs need a board outline to be checked against; give "
                           "the variant a board"] if need else [])
    spec = board.get("outline") if isinstance(board, Mapping) else None
    if isinstance(spec, Mapping) and "dxf" in spec:
        if not need:
            return None, ["board.outline is a DXF: passed through unchecked for the F3 loader"], []
        try:
            import outline_resolve
            return outline_resolve.resolve_board({"board": board}, netlist_dir).outline, [], []
        except (ImportError, ValueError) as e:
            return None, [], [f"board: cannot read the DXF outline to check keep_refs: {e}"]
    try:
        return ol.from_board_block(board), [], []
    except (TypeError, ValueError) as e:             # OutlineError is a ValueError
        return None, [], [f"board: {e}"]


def _keep_ref_errors(comps: list[Mapping[str, Any]], keep_refs: tuple[str, ...],
                     outline: ol.Outline | None) -> list[str]:
    by_ref = {c["ref"]: c for c in comps}
    unknown = [r for r in keep_refs if r not in by_ref]
    errors = [f"keep_refs: unknown reference(s) {', '.join(unknown)}"] if unknown else []
    for ref in (r for r in keep_refs if r in by_ref):
        origin = _origin(by_ref[ref])
        if origin is None:
            errors.append(f"keep_refs: {ref} has no placement ('at' or x/y) to keep")
        elif outline is not None and not ol.contains(outline, origin):
            errors.append(f"keep_refs: {ref} at ({origin[0]:g}, {origin[1]:g}) is outside the "
                          "new board outline")
    return errors


# --- holes and artwork ----------------------------------------------------------------------
def _hole_plan(holes: list[Mapping[str, Any]], policy: str
               ) -> tuple[object, list[str], list[Draft]]:
    """(the mounting_holes value or None for unchanged, notes, drafts)."""
    if not holes:
        return None, [], []
    refs = ", ".join(_natural(c["ref"] for c in holes))
    if policy == "none":
        return None, [], [("mounting holes", "none", f"the source's hole parts ({refs}) are "
                           "dropped without replacement (holes='none')")]
    if policy == "corners":
        return True, [], [("mounting holes", "corner mounting holes (the pipeline default)",
                            f"the source's hole parts ({refs}) cannot keep their positions in "
                            "the new outline; the pipeline puts its holes in the corners")]
    counts = Counter(str(c.get("footprint", "")) for c in holes)
    footprint = counts.most_common(1)[0][0]
    others = [fp for fp in counts if fp != footprint]
    notes = [f"hole parts use several footprints; mounting_holes uses {footprint}, not "
             f"{', '.join(others)}"] if others else []
    return ({"footprint": footprint, "inset": HOLE_INSET_MM}, notes,
            [("mounting holes", f"{footprint}, inset {HOLE_INSET_MM:g} mm",
              f"the source's hole parts ({refs}) cannot keep their positions in the new "
              "outline; the pipeline places the same footprint in the corners")])


def _artwork_drafts(artwork: list[Mapping[str, Any]]) -> list[Draft]:
    if not artwork:
        return []
    return [("artwork", "dropped " + ", ".join(_natural(c["ref"] for c in artwork)),
             "board-only graphics without copper pads (logos, artwork) are drawn for the "
             "source outline; re-add them by hand where wanted")]


# --- nets ---------------------------------------------------------------------------------
def _strip_nets(nets: Mapping[str, list], no_connect: list, dropped: frozenset[str]) -> _NetStrip:
    kept: dict[str, list] = {}
    removed: list[str] = []
    lost: dict[str, list[str]] = {}
    lone: list[tuple[list, str, tuple[str, ...]]] = []
    for name, pads in nets.items():
        stay = [list(p) for p in pads if p[0] not in dropped]
        gone = [p for p in pads if p[0] in dropped]
        if stay and gone:
            for ref, pad in gone:
                lost.setdefault(ref, []).append(f"{ref}.{pad} on {name}")
        if len(stay) >= 2 or not gone:
            kept[name] = stay
            continue
        removed.append(name)
        if stay:
            lone.append((stay[0], name, _natural(r for r, _p in gone)))
    nc = [list(p) for p in no_connect if p[0] not in dropped]
    nc += [pad for pad, _n, _r in lone if pad not in nc]
    return _NetStrip(kept, nc, tuple(removed), lost, tuple(lone))


def _net_drafts(strip: _NetStrip) -> tuple[list[Draft], list[str]]:
    drafts: list[Draft] = [("dropped connection", f"{ref}: {', '.join(where)}",
                            "the variant dropped this part; the rest of each net stays "
                            "connected without it") for ref, where in strip.lost.items()]
    drafts += [("no_connect", f"{pad[0]}.{pad[1]}",
                f"net {net} lost its other pad(s) with {', '.join(refs)}; the pad is left "
                "unconnected (D4)") for pad, net, refs in strip.lone]
    notes = [f"dropped part {ref} was connected: {', '.join(where)}"
             for ref, where in strip.lost.items()]
    notes += [f"net {net} lost its other pad(s) with {', '.join(refs)}: {pad[0]}.{pad[1]} is "
              "now no_connect" for pad, net, refs in strip.lone]
    if strip.removed:
        notes.append("nets removed with the dropped parts: " + ", ".join(strip.removed))
    return drafts, notes


def _without_nets(design: Mapping[str, Any], removed: tuple[str, ...]) -> dict[str, Any]:
    """net_specs / power_flags without the removed nets (only keys the design has)."""
    out: dict[str, Any] = {}
    specs = design.get("net_specs")
    if isinstance(specs, Mapping):
        out["net_specs"] = {n: copy.deepcopy(s) for n, s in specs.items() if n not in removed}
    flags = design.get("power_flags")
    if isinstance(flags, list):
        out["power_flags"] = [f for f in flags if f not in removed]
    return out


def _numbered(existing: object, drafts: list[Draft]) -> list[dict[str, Any]]:
    taken = {a.get("id") for a in existing if isinstance(a, Mapping)} \
        if isinstance(existing, list) else set()
    out: list[dict[str, Any]] = []
    n = 0
    for topic, value, rationale in drafts:
        n += 1
        while f"{ASSUMPTION_PREFIX}{n}" in taken:
            n += 1
        out.append({"id": f"{ASSUMPTION_PREFIX}{n}", "topic": topic, "value": value,
                    "rationale": rationale, "confidence": CONFIDENCE})
    return out


# --- make_variant -------------------------------------------------------------------------
def _keep(design: Mapping[str, Any], comps: list[Mapping[str, Any]]) -> VariantResult:
    missing = [c["ref"] for c in comps if c.get("board_only") is not True and c.get("at") is None]
    errors = (f"keep: {', '.join(missing)} {'has' if len(missing) == 1 else 'have'} no 'at' "
              "(a keep rebuild places every schematic part at its source origin)",) \
        if missing else ()
    return VariantResult(copy.deepcopy(dict(design)), errors=errors)


def _previous_dropped(design: Mapping[str, Any]) -> list[str]:
    variant = design.get("variant")
    refs = variant.get("dropped_refs") if isinstance(variant, Mapping) else None
    return [r for r in refs if isinstance(r, str)] if isinstance(refs, list) else []


def _frame_notes(design: Mapping[str, Any]) -> list[str]:
    notes = [f"{key}: coordinates refer to the source board frame; check them against the "
             "new outline" for key in FRAME_KEYS if design.get(key)]
    if design.get("preroutes"):
        notes.append("preroutes dropped: their copper was drawn in the source board frame")
    return notes


def _reflowed(design: Mapping[str, Any], comps: list[Mapping[str, Any]], keep: frozenset[str],
              dropped: frozenset[str], strip: _NetStrip) -> dict[str, Any]:
    out = {k: copy.deepcopy(v) for k, v in design.items() if k != "preroutes"}
    out["rebuild"] = "reflow"
    out["components"] = [{k: copy.deepcopy(v) for k, v in c.items()
                          if c["ref"] in keep or k not in PLACEMENT_KEYS}
                         for c in comps if c["ref"] not in dropped]
    if "nets" in design or strip.nets:
        out["nets"] = strip.nets
    if "no_connect" in design or strip.no_connect:
        out["no_connect"] = strip.no_connect
    return {**out, **_without_nets(design, strip.removed)}


def make_variant(design: Mapping[str, Any], netlist_dir: dp.PathArg, *, mode: str,
                 board: Mapping[str, Any] | None = None, keep_refs: Iterable[str] = (),
                 holes: str = "source", drop_graphics: bool = True,
                 pad_summary: PadSummary | None = None, stock_dir: dp.PathArg | None = None,
                 source_name: str = NETLIST_FILE) -> VariantResult:
    """The keep / reflow variant of `design` (module doc). `netlist_dir` is
    the design's directory (relative paths, footprints); `source_name` the
    netlist file recorded as ``variant.from``. Raises ValueError / TypeError
    on bad arguments; design problems land in ``errors``."""
    keep_refs = _check_args(mode, holes, keep_refs)
    comps = _components(design)
    if mode == "keep":
        return _keep(design, comps)
    new_board, notes = _choose_board(design, board)
    outline, outline_notes, errors = _outline(new_board, netlist_dir, need=bool(keep_refs))
    errors += _keep_ref_errors(comps, keep_refs, outline)
    keep = frozenset(keep_refs)
    try:
        summary = pad_summary or default_pad_summary(design, netlist_dir, stock_dir)
    except ValueError as e:
        notes.append(f"footprints cannot be resolved ({e}); board-only parts are kept")
        summary = _no_summary
    hole_parts, artwork, class_notes = _classify_all(comps, keep, summary, drop_graphics)
    dropped = _natural(c["ref"] for c in (*hole_parts, *artwork))
    strip = _strip_nets(design.get("nets") or {}, design.get("no_connect") or [],
                        frozenset(dropped))
    holes_value, hole_notes, drafts = _hole_plan(hole_parts, holes)
    net_drafts, net_notes = _net_drafts(strip)
    out = _reflowed(design, comps, keep, frozenset(dropped), strip)
    if new_board is not None:
        out["board"] = new_board
    if holes_value is not None:
        if design.get("mounting_holes") is not None:
            notes.append(f"mounting_holes {design['mounting_holes']!r} replaced by the holes "
                         f"policy '{holes}'")
        out["mounting_holes"] = holes_value
    assumptions = _numbered(design.get("assumptions"),
                            drafts + _artwork_drafts(artwork) + net_drafts)
    out["variant"] = {"mode": "reflow", "from": source_name,
                      "dropped_refs": list(_natural([*_previous_dropped(design), *dropped])),
                      "holes": holes}
    if assumptions or "assumptions" in design:
        out["assumptions"] = [*copy.deepcopy(design.get("assumptions") or []), *assumptions]
    return VariantResult(out, tuple(assumptions), dropped,
                         tuple(notes + outline_notes + class_notes + hole_notes + net_notes
                               + _frame_notes(design)), tuple(errors))


# --- CLI ----------------------------------------------------------------------------------
def _board_arg(raw: str | None, netlist_dir: Path) -> dict[str, Any] | None:
    """--board: inline JSON or a JSON file; a relative DXF path inside is
    rebased from the file's directory (inline: cwd) onto `netlist_dir`."""
    if raw is None:
        return None
    text = raw.strip()
    inline = text.startswith(("{", "["))
    data = json.loads(text if inline else Path(text).read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError(f"--board must be a JSON object (a netlist board block), got {data!r}")
    base = Path.cwd() if inline else Path(text).parent
    return dp.relocate_libraries({"board": data}, base, netlist_dir)["board"]


def _same_dir(a: Path, b: Path) -> bool:
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def _write_json(path: Path, data: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8",
                    newline="\n")


def _run(args: argparse.Namespace) -> tuple[VariantResult, Path | None]:
    src, out_dir = Path(args.netlist), Path(args.out_dir)
    if _same_dir(out_dir, src.parent):
        raise ValueError(f"refusing to write into the source directory {src.parent}: the "
                         "variant would overwrite or sit beside the source; pick another out_dir")
    design = json.loads(src.read_text(encoding="utf-8-sig"))
    if not isinstance(design, dict):
        raise ValueError(f"{src} is not a netlist (a JSON object)")
    res = make_variant(design, src.parent, mode=args.mode,
                       board=_board_arg(args.board, src.parent),
                       keep_refs=[r.strip() for r in args.keep_refs.split(",") if r.strip()],
                       holes=args.holes, drop_graphics=not args.keep_graphics,
                       stock_dir=args.footprints_dir, source_name=src.name)
    if not res.ok:
        return res, None
    target = out_dir / NETLIST_FILE
    _write_json(target, dp.relocate_libraries(res.design, src.parent, out_dir))
    return res, target


def main(argv: list[str] | None = None) -> int:
    """Exit 0: variant written; 1: variant errors (nothing written); 2: usage/IO."""
    ap = argparse.ArgumentParser(description="Make a keep / reflow variant of an imported "
                                 "netlist.json (D4) and write it, paths relocated.")
    ap.add_argument("netlist", help="the imported netlist.json")
    ap.add_argument("out_dir", help="where the variant's netlist.json goes (not the source dir)")
    ap.add_argument("--mode", choices=MODES, default="reflow")
    ap.add_argument("--board", help="the new board block: inline JSON or a JSON file")
    ap.add_argument("--keep-refs", default="", help="comma-separated refs that keep placement")
    ap.add_argument("--holes", choices=HOLE_POLICIES, default="source")
    ap.add_argument("--keep-graphics", action="store_true", help="keep board-only artwork")
    ap.add_argument("--footprints-dir", help="stock footprint directory for stock nicknames")
    args = ap.parse_args(argv)
    try:
        res, target = _run(args)
    except (OSError, ValueError, TypeError) as e:     # json.JSONDecodeError is a ValueError
        print(f"rebuild_variant: {e}", file=sys.stderr)
        return 2
    print(json.dumps({"ok": res.ok, "netlist": None if target is None else str(target),
                      "mode": args.mode, "dropped_refs": list(res.dropped_refs),
                      "assumptions": len(res.assumptions), "notes": list(res.notes),
                      "errors": list(res.errors)}, indent=1))
    return 0 if res.ok else 1


if __name__ == "__main__":
    sys.exit(main())
