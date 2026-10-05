#!/usr/bin/env python3
"""Save a board's placed footprints as library footprints, through pcbnew
SWIG (ROADMAP_DECISIONS D4: a rebuild uses the source board's own
footprints). Runs under KiCad's bundled Python; kicad_import.py drives it
and names, de-duplicates and installs the result.

Every footprint listed in the plan is copied and normalised the way KiCad's
own "save to library" expects a library footprint: a B-side copy is flipped
back (``FLIP_DIRECTION_TOP_BOTTOM`` about its origin, which undoes either
flip direction exactly: KiCad stores every flipped footprint as its library
geometry mirrored in local Y and rotated by the stored orientation), then its
orientation is set to 0 and its origin to (0, 0). Instance data goes: the
lock and (for parts that come from the schematic) the DNP and
exclude-from-BOM flags, which KiCad copies from the symbol; Reference becomes
``REF**``. The rest of the instance data stays in the saved file and
kicad_import_fplib.strip_instance_fields removes it from the text: fields
other than Reference / Value / Footprint / Datasheet / Description (MPN,
LCSC, Sim.*, the bare ``ki_fp_filters``) and the symbol path, sheet name and
sheet file (pcbnew 10 writes the last two into a library copy, pcbnew 9 does
not; see library_copy). The copy is written with the KiCad s-expression plugin of
THIS pcbnew, so the file is in its dialect (KiCad 9: version 20241229, D0;
KiCad 10: 20260206).

Each footprint is saved into its OWN library directory ``<out>/<key>.pretty``:
the plugin re-reads a whole library on every save, so one growing library
makes extraction quadratic (1508 footprints: about 9 minutes; one directory
each: seconds). The caller reads ``<out>/<key>.pretty/<key>.kicad_mod`` back.

The caller runs this with stdin=DEVNULL; kicad_quiet (imported before
pcbnew) keeps wx from opening a modal dialog.

Usage: <kicad-python> kicad_import_swig.py <board.kicad_pcb> <plan.json> <out_dir>
plan.json: {"footprints": [{"key": "fp0001", "uuid": "...", "ref": "R1",
            "x_nm": 1000, "y_nm": 2000, "keep_instance_attrs": false}, ...]}
Output: the LAST stdout line is JSON {"ok": bool, "pcbnew": version,
  "saved": [{"key", "ref"}], "unmatched": [key, ...], "warnings": [...],
  ["error": str]}. Exit 0 when every planned footprint was saved, 1 otherwise,
  2 on a usage error.
"""
import faulthandler
import json
import os
import sys

import kicad_quiet  # noqa: F401  (before pcbnew: no modal wx dialogs)
import pcbnew

POSITION_TOL_NM = 2
USAGE_EXIT, FAIL_EXIT = 2, 1


def emit(ok, code, **fields):
    """Print the result line (always the last stdout line) and return `code`."""
    print(json.dumps({"ok": ok, **fields}), flush=True)
    return code


def _match(fp, by_uuid, by_ref):
    """The plan entry of a board footprint: by UUID, else by reference and
    origin (a board whose UUIDs KiCad regenerated on load)."""
    entry = by_uuid.get(fp.m_Uuid.AsString())
    if entry is not None:
        return entry
    pos = fp.GetPosition()
    for cand in by_ref.get(fp.GetReference(), ()):
        if (abs(cand["x_nm"] - pos.x) <= POSITION_TOL_NM
                and abs(cand["y_nm"] - pos.y) <= POSITION_TOL_NM):
            return cand
    return None


def _flip_direction():
    return getattr(pcbnew, "FLIP_DIRECTION_TOP_BOTTOM", True)     # KiCad 8: a bool


def sexpr_plugin():
    """KiCad's s-expression footprint plugin: ``PCB_IO_MGR.PluginFind``
    (KiCad 9) was renamed ``FindPlugin`` (KiCad 10). pcbnew's module-level
    ``FootprintSave`` is no substitute: KiCad 10's cannot guess the plugin
    of a new, empty library directory."""
    mgr = pcbnew.PCB_IO_MGR
    find = getattr(mgr, "FindPlugin", None) or getattr(mgr, "PluginFind", None)
    if find is None:
        raise RuntimeError(f"pcbnew {pcbnew.Version()}: PCB_IO_MGR has neither FindPlugin "
                           "nor PluginFind")
    plug = find(mgr.KICAD_SEXP)
    if plug is None:
        raise RuntimeError(f"pcbnew {pcbnew.Version()}: no KiCad s-expression plugin")
    return plug


def library_copy(fp, entry, warnings):
    """A normalised copy of `fp`, ready for FootprintSave under entry['key'].
    Instance fields stay in it: kicad_import_fplib strips them from the saved
    text, because pcbnew 10 has no FOOTPRINT.RemoveField and its
    FOOTPRINT.Remove(field) corrupts SWIG's type data within a few
    footprints (vme-wren: the board's own footprints then fail)."""
    copy = pcbnew.FOOTPRINT(fp)
    if copy.IsFlipped():
        copy.Flip(copy.GetPosition(), _flip_direction())
    copy.SetOrientationDegrees(0)
    copy.SetPosition(pcbnew.VECTOR2I(0, 0))
    copy.SetReference("REF**")
    copy.SetValue(entry["key"])
    if hasattr(copy, "SetLocked"):
        copy.SetLocked(False)
    if not entry.get("keep_instance_attrs"):
        for setter in ("SetDNP", "SetExcludedFromBOM"):
            if hasattr(copy, setter):
                getattr(copy, setter)(False)
            else:
                warnings.append(f"{entry['ref']}: pcbnew {pcbnew.Version()} has no "
                                f"FOOTPRINT.{setter}; the flag stays in the library copy")
    copy.SetFPID(pcbnew.LIB_ID("", entry["key"]))
    return copy


def main(argv):
    faulthandler.enable()        # a native crash still leaves a traceback on stderr
    if len(argv) != 4:
        print(__doc__, file=sys.stderr)
        return emit(False, USAGE_EXIT, error="usage: kicad_import_swig.py <board.kicad_pcb> "
                                             "<plan.json> <out_dir>")
    board_path, plan_path, out = (os.path.abspath(a) for a in argv[1:4])
    base = {"pcbnew": pcbnew.Version()}
    try:
        with open(plan_path, encoding="utf-8") as f:
            plan = json.load(f)["footprints"]
    except (OSError, ValueError, KeyError, TypeError) as e:
        return emit(False, USAGE_EXIT, **base, error=f"cannot read the plan {plan_path}: {e}")
    board = pcbnew.LoadBoard(board_path)
    if board is None:
        return emit(False, FAIL_EXIT, **base, error=f"pcbnew could not load {board_path}")
    by_uuid = {e["uuid"]: e for e in plan if e.get("uuid")}
    by_ref = {}
    for e in plan:
        by_ref.setdefault(e["ref"], []).append(e)
    saved, warnings, done = [], [], set()
    try:
        plug = sexpr_plugin()
        for fp in board.GetFootprints():
            entry = _match(fp, by_uuid, by_ref)
            if entry is None or entry["key"] in done:
                continue
            lib = os.path.join(out, entry["key"] + ".pretty")     # one library each, see doc
            os.makedirs(lib, exist_ok=True)
            plug.FootprintSave(lib, library_copy(fp, entry, warnings))
            done.add(entry["key"])
            saved.append({"key": entry["key"], "ref": entry["ref"]})
    except Exception as e:      # SWIG raises assorted exception types
        return emit(False, FAIL_EXIT, **base, saved=saved, warnings=warnings,
                    error=f"{type(e).__name__}: {e}")
    unmatched = [e["key"] for e in plan if e["key"] not in done]
    return emit(not unmatched, 0 if not unmatched else FAIL_EXIT, **base, saved=saved,
                unmatched=unmatched, warnings=warnings)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
