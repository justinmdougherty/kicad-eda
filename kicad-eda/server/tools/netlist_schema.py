#!/usr/bin/env python3
"""The netlist.json contract: a JSON Schema (draft 2020-12) plus ``validate()``.

The schema types every key a pipeline consumer reads today (lint_netlist,
sch_build, swig_build_board, place_engine, swig_gate_checks, swig_board_fixes,
netclass_writer, ipc_rules, impedance, thermal_check, design_report, gates,
run_pipeline) and the roadmap keys of docs/ROADMAP_DECISIONS.md D1-D5:
``libraries``, ``board.corner_radius|outline|cutouts|datum|panel_cutouts``,
component ``at``, ``board_only``, ``dnp``, ``in_bom`` and ``edge``,
``source`` (with the import provenance files), ``rebuild``, ``variant``,
``blocks``, ``mode`` and ``assumptions``. The D7 keys (``copper_layers`` 1 | even
2..32, ``stackup``, the net_specs RF keys, ``rf``, ``layer_policy``, component
``clock_hz``) are merged in from netlist_schema_d7. docs/PIPELINE.md section 2
is the human-readable side.

Policy:
  * Unknown keys pass at the top level, on components, inside the classic
    blocks and on the provenance records (``source``, ``variant``, a block
    entry: hand notes, forward compatibility); every KNOWN key is typed. The
    new fixed-shape blocks (``libraries``, ``board.outline``, a cutout) reject
    unknown keys so a typo cannot silently fall back to a rectangle.
  * ``null`` is accepted exactly where every consumer treats it as absent
    (``board``, ``planes``, ``emc``, ``height_zones``, component ``edge``, a
    block's ``source`` / ``rebuild``, ...).
  * Integers are strict (``4.0`` is not a layer count, ``1.0`` is not pad "1");
    ``thermal.ep_vias_min`` is the exception: its consumers read it via int(),
    so a whole-number float (4.0) passes.
  * Semantics stay out: duplicate refs, single-pin nets, unknown refs in nets
    (or in a block's ref_map / drop_refs) are lint's job; geometry (closed
    loops, radius vs size, loop bbox at the origin) belongs to the outline
    module.

Importing this module is stdlib-only, so KiCad-python (SWIG) scripts may read
the shared constants; ``validate()`` needs ``jsonschema`` (server/requirements).

Usage: <python> netlist_schema.py <netlist.json>   -> {"ok", "errors"}, exit 0/1
       <python> netlist_schema.py --schema         -> the schema as JSON
"""
from __future__ import annotations

import functools
import json
import re
import sys
from pathlib import Path
from typing import Any

import netlist_schema_d7 as d7

# --- shared enums (the decisions' vocabularies; import these, never re-type) --
SOURCE_KINDS = ("kicad", "eagle", "altium", "easyeda", "vision", "gerber")  # D4 (+F4/F6)
REBUILD_MODES = ("keep", "reflow")                                  # D4
COPPER_MODES = ("carry", "reroute")             # source.copper (keep rebuilds, F46-29)
VARIANT_HOLES = ("source", "corners", "none")   # variant.holes (rebuild_variant, F46-10)
BOARD_EDGES = ("top", "bottom", "left", "right")    # component edge (F3-12), antenna_keepout
MODES = ("napkin",)                                                 # D5
CONFIDENCE_LEVELS = ("high", "med", "low")                          # D5
COPPER_LAYER_COUNTS = d7.COPPER_LAYER_COUNTS    # D7.2.1: (1,) + tuple(range(2, 33, 2))
DXF_UNITS = ("mm", "cm", "in", "mil")                               # D2
IPC_CLASSES = (1, 2, 3)

# Non-standard annotation keyword: a validator ignores it, validate() uses it as
# the human message for errors raised by that subschema's own keywords.
X_MESSAGE = "x-message"

_NUM = {"type": "number"}
_POS = {"type": "number", "exclusiveMinimum": 0}
_BOOL = {"type": "boolean"}
_STR = {"type": "string"}
_NAME = {"type": "string", "minLength": 1}


def _ref(name: str) -> dict:
    return {"$ref": f"#/$defs/{name}"}


def _numbers(*keys: str) -> dict:
    return {k: _NUM for k in keys}


def _positive(*keys: str) -> dict:
    return {k: _POS for k in keys}


def _key_name(what: str) -> dict:
    """propertyNames subschema: a non-empty key, named in the message."""
    return {"minLength": 1, X_MESSAGE: f"a {what} must not be empty"}


def _name_map(key: str) -> dict:
    """{<key>: <non-empty string>} with non-empty keys."""
    return {"type": "object", "propertyNames": _key_name(key), "additionalProperties": _NAME}


# D4 provenance written by the importers (kicad_import, import_pipeline, F46-*):
# every file is relative to the netlist.json directory (D1 rule) or absolute.
_SOURCE_FILES = ("reference",      # reference pad nets of the source board as imported
                 "erc_baseline",   # source ERC report: ERC is judged against it
                 "drc_baseline",   # source DRC report (informational)
                 "import_report",  # the import report (default <dir>/import_report.json)
                 "via",            # the intermediate .kicad_pro a foreign import went through
                 "board")          # a source board KiCad 9/10 pcbnew can load (copper carry)
_SOURCE_BODY: dict[str, Any] = {
    "required": ["kind", "path"],
    "properties": {
        "kind": {"enum": list(SOURCE_KINDS)},
        "path": _NAME,
        "sha256": _ref("sha256"),
        "tool": _NAME,
        **{k: _NAME for k in _SOURCE_FILES},
        "files": {"type": "object", "propertyNames": _key_name("file name"),
                  "additionalProperties": _ref("sha256")},
        "copper": {"enum": list(COPPER_MODES)},
    },
}


# The classic definitions; d7.merge_defs adds the D7 ones (stackup, rf, ...) and
# the net_spec RF keys / component clock_hz.
_DEFS: dict[str, Any] = d7.merge_defs({
    # --- geometry primitives (mm, board frame: origin = outline bbox
    # top-left, Y down) ---
    "point": {"type": "array", "prefixItems": [_NUM, _NUM],
              "minItems": 2, "maxItems": 2, X_MESSAGE: "a point is [x, y] (mm)"},
    "vertex": {"type": "array", "items": _NUM, "minItems": 2, "maxItems": 3,
               X_MESSAGE: "a loop vertex is [x, y] or [x, y, bulge] (mm; bulge "
                          "= tan(arc angle / 4) of the arc to the next vertex)"},
    "loop": {"type": "array", "items": _ref("vertex"), "minItems": 2},
    "circle": {"type": "array",
               "prefixItems": [_NUM, _NUM, {"type": "number", "exclusiveMinimum": 0}],
               "minItems": 3, "maxItems": 3,
               X_MESSAGE: "a circle is [cx, cy, r] (mm, r > 0)"},
    "copper_layer": {"type": "string",
                     "pattern": r"^(F|B|In([1-9]|[12][0-9]|30))\.Cu$",
                     X_MESSAGE: "a copper layer name is F.Cu, B.Cu or In1.Cu..In30.Cu"},
    "lib_ref": {"type": "string", "pattern": r"^[^:]+:[^:]+$",
                X_MESSAGE: "must look like 'Library:Name'"},
    "nickname": {"type": "string", "pattern": r"^[^:\x00-\x1f]+$",
                 X_MESSAGE: "a library nickname is non-empty, without ':' or "
                            "control characters"},
    "sha256": {"type": "string", "pattern": "^[0-9a-fA-F]{64}$",
               X_MESSAGE: "sha256 is 64 hex digits"},
    "ref_list": {"type": "array", "items": _NAME},
    # --- connectivity ---
    "pad": {"type": ["string", "integer"], "minLength": 1},
    "pad_ref": {"type": "array", "prefixItems": [_NAME, _ref("pad")],
                "minItems": 2, "maxItems": 2,
                X_MESSAGE: "a pad reference is [ref, pad]"},

    # --- D1 project-local libraries: nickname -> path (relative to the
    # netlist.json directory, ${KIPRJMOD} = that directory, or absolute) ---
    "library_map": {"type": "object", "propertyNames": _ref("nickname"),
                    "additionalProperties": _NAME},
    "libraries": {"type": "object",
                  "properties": {"symbols": _ref("library_map"),
                                 "footprints": _ref("library_map")},
                  "additionalProperties": False},

    # --- D2 board outline ---
    "outline": {
        "type": "object",
        "properties": {"loop": _ref("loop"), "dxf": _NAME, "layer": _NAME,
                       "units": {"enum": list(DXF_UNITS)}},
        "additionalProperties": False,
        "allOf": [{"oneOf": [{"required": ["loop"]}, {"required": ["dxf"]}],
                   X_MESSAGE: "an outline has exactly one of 'loop' or 'dxf'"}],
        "dependentSchemas": {"loop": {
            "not": {"anyOf": [{"required": ["layer"]}, {"required": ["units"]}]},
            X_MESSAGE: "'layer' and 'units' only apply to a 'dxf' outline"}},
    },
    "cutout": {
        "type": "object",
        "properties": {"loop": _ref("loop"), "circle": _ref("circle")},
        "additionalProperties": False,
        "allOf": [{"oneOf": [{"required": ["loop"]}, {"required": ["circle"]}],
                   X_MESSAGE: "a cutout has exactly one of 'loop' or 'circle'"}],
    },
    "auto_field": {"type": ["object", "null"],
                   "properties": {"x": {"type": ["number", "null"]},
                                  **_numbers("y"), **_positive("w", "h")}},
    # Contracts beyond the schema (outline.from_board_block): an outline loop's
    # bbox starts at (0, 0) and w/h then match it; corner_radius only with w/h.
    # A block of auto_* keys alone is rejected on purpose (they are windows in
    # a FIXED board; an auto-sized board has no board block).
    "board": {
        "type": ["object", "null"],
        "properties": {
            **_positive("w", "h", "auto_w", "auto_h"),
            "auto_y": _NUM,
            "auto_x": {"type": ["number", "null"]},
            "auto_b": _ref("auto_field"),
            "corner_radius": {"type": "number", "minimum": 0},
            "outline": _ref("outline"),
            "cutouts": {"type": "array", "items": _ref("cutout")},
            "datum": _ref("point"),
            "panel_cutouts": _BOOL,     # F3-13: a connector may face a cutout edge
        },
        "dependentRequired": {"w": ["h"], "h": ["w"]},
        "allOf": [{
            "if": {"type": "object", "minProperties": 1,
                   "not": {"required": ["outline"]}},
            "then": {"required": ["w", "h"],
                     X_MESSAGE: "a board block needs 'w' and 'h' (mm) unless it "
                                "has an 'outline'"}}],
    },

    # --- components (unknown keys allowed; every known key typed) ---
    "part_thermal": {"type": ["object", "null"],
                     "properties": _numbers("p_diss", "theta_ja", "tj_max",
                                            "i_max", "rds_mohm")},
    "component": {
        "type": "object",
        "required": ["ref", "footprint"],
        "properties": {
            "ref": _NAME,
            "lib_id": _ref("lib_ref"),
            "value": _STR,
            "footprint": _ref("lib_ref"),
            # courtyard-bbox centre placement (fixed when both x and y are set)
            **_numbers("x", "y", "rot", "rds_mohm"),
            # D3: footprint origin + final (post-flip) rotation, board frame
            "at": {"type": "array", "prefixItems": [_NUM, _NUM, _NUM],
                   "minItems": 3, "maxItems": 3,
                   X_MESSAGE: "'at' is [x, y, rot]: the footprint origin (mm, "
                              "board frame) and its final rotation"},
            "side": {"enum": ["F", "B", "f", "b"]},
            "interior": _BOOL,
            "edge_row": _BOOL,
            "overhang": {"type": ["number", "null"]},
            "height": {"type": ["number", "null"]},
            "thermal": _ref("part_thermal"),
            "lcsc": {"type": ["string", "null"]},
            "mpn": {"type": ["string", "null"]},
            "board_only": _BOOL,                                    # D4
            "dnp": _BOOL,                   # D4: in the design, not populated
            "in_bom": _BOOL,                # D4: false = left out of the BOM
            # F3-12: the board edge a connector mates through (null = top)
            "edge": {"enum": [*BOARD_EDGES, None]},
        },
        "dependentRequired": {"x": ["y"], "y": ["x"]},
        "dependentSchemas": {"at": {
            "not": {"anyOf": [{"required": ["x"]}, {"required": ["y"]},
                              {"required": ["rot"]}]},
            X_MESSAGE: "a component is placed by 'at' or by 'x'/'y'/'rot', never "
                       "both: 'at' = [x, y, rot] already places and rotates it"}},
        "if": {"required": ["board_only"],
               "properties": {"board_only": {"const": True}}},
        "else": {"required": ["lib_id"],
                 X_MESSAGE: "needs 'lib_id' ('Library:Symbol'); only a "
                            "\"board_only\": true part may omit it"},
    },

    # --- design intent and copper ---
    "net_spec": {"type": "object",
                 "properties": {**_numbers("voltage", "current", "impedance", "diff",
                                           "gap", "dp_tol_mm", "dp_gap_mm"),
                                "diff_pair": _NAME}},
    "preroute_point": {
        "type": "array", "minItems": 1,
        "prefixItems": [{"enum": ["pad", "xy"],
                         X_MESSAGE: "a preroute point starts with \"pad\" or \"xy\""}],
        "allOf": [
            {"if": {"prefixItems": [{"const": "pad"}]},
             "then": {"prefixItems": [True, _NAME, _ref("pad"), _NUM],
                      "minItems": 3, "maxItems": 4,
                      X_MESSAGE: "a pad point is [\"pad\", ref, pad, (width)]"}},
            {"if": {"prefixItems": [{"const": "xy"}]},
             "then": {"prefixItems": [True, _NUM, _NUM, _NUM],
                      "minItems": 3, "maxItems": 4,
                      X_MESSAGE: "an xy point is [\"xy\", x, y, (width)]"}},
        ],
    },
    "preroute": {
        "type": "object",
        "required": ["net", "width", "path"],
        "properties": {
            "net": _NAME, "width": _NUM, "layer": _ref("copper_layer"),
            "path": {"type": "array", "items": _ref("preroute_point")},
            "vias": {"type": "array", "items": _ref("point")},
            "via_size": {"type": "array", "prefixItems": [_NUM, _NUM],
                         "minItems": 2, "maxItems": 2,
                         X_MESSAGE: "via_size is [diameter, drill] (mm)"},
        },
    },
    "mounting_holes": {
        "type": ["boolean", "object", "null"],
        "properties": {"footprint": _ref("lib_ref"), "inset": _NUM,
                       "positions": {"type": "array", "items": _ref("point")}},
    },
    "antenna_keepout": {
        "type": ["object", "null"],
        "properties": {
            "edge": {"enum": list(BOARD_EDGES)},
            "depth": _NUM, "name": _STR,
            "rect": {"type": "array", "prefixItems": [_NUM, _NUM, _NUM, _NUM],
                     "minItems": 4, "maxItems": 4,
                     X_MESSAGE: "rect is [x, y, w, h] (mm)"},
        },
    },
    "height_zone": {
        "type": "object",
        "required": ["x", "y", "w", "h", "max_height"],
        "properties": {**_numbers("x", "y", "w", "h", "max_height"),
                       "side": {"enum": ["F", "B", "both"]}, "name": _STR,
                       "exempt": {"type": "array", "items": _NAME}},
    },

    # --- D4 / D5 provenance and assumptions ---
    "source": {"type": "object", **_SOURCE_BODY},
    # rebuild_variant (F46-10): how this design was derived from an import
    "variant": {
        "type": "object",
        "required": ["mode"],
        "properties": {"mode": {"enum": list(REBUILD_MODES)}, "from": _NAME,
                       "dropped_refs": _ref("ref_list"),
                       "holes": {"enum": list(VARIANT_HOLES)}},
    },
    # design_blocks.compose (F46-10 / F46-27): one entry per composed block
    "block": {
        "type": "object",
        "required": ["name", "ref_map"],
        "properties": {
            "name": _NAME,
            "source": {"type": ["object", "null"], **_SOURCE_BODY},
            "rebuild": {"enum": [*REBUILD_MODES, None]},
            "ref_map": {**_name_map("reference"), "minProperties": 1,
                        X_MESSAGE: "ref_map lists every block ref, old -> new "
                                   "(identity entries included)"},
            "net_map": _name_map("net name"),
            "drop_refs": _ref("ref_list"),      # BASE refs this compose step removed
        },
    },
    "assumption": {
        "type": "object",
        "required": ["id", "topic", "value", "rationale", "confidence"],
        "properties": {
            "id": _NAME, "topic": _NAME, "rationale": _NAME,
            "value": {"type": ["string", "number", "boolean", "array", "object"]},
            "confidence": {"enum": list(CONFIDENCE_LEVELS)},
            "override": {"type": ["string", "null"]},
        },
    },
})

# Read-only: subschemas are shared objects and validate() compiles it once.
SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "kicad-eda netlist.json",
    "description": "Pad-level design netlist: the source of truth of the gated "
                   "pipeline (docs/PIPELINE.md section 2).",
    "type": "object",
    "required": ["components"],
    # d7.merge_top_level adds copper_layers, stackup, rf and layer_policy
    "properties": d7.merge_top_level({
        "name": _STR,
        "title": _STR,
        "class": {"type": "integer", "enum": list(IPC_CLASSES)},
        "mode": {"enum": list(MODES)},
        "libraries": _ref("libraries"),
        "board": _ref("board"),
        "mounting_holes": _ref("mounting_holes"),
        "components": {"type": "array", "minItems": 1, "items": _ref("component")},
        "nets": {"type": "object", "propertyNames": _key_name("net name"),
                 "additionalProperties": {"type": "array", "items": _ref("pad_ref")}},
        "no_connect": {"type": "array", "items": _ref("pad_ref")},
        "power_flags": {"type": "array", "items": _NAME},
        "net_specs": {"type": "object", "additionalProperties": _ref("net_spec")},
        "preroutes": {"type": "array", "items": _ref("preroute")},
        "planes": {"type": ["object", "null"], "propertyNames": _ref("copper_layer"),
                   "additionalProperties": _NAME},
        "antenna_keepout": _ref("antenna_keepout"),
        "emc": {"type": ["object", "null"],
                "properties": {"stitch": _BOOL, "max_freq_hz": _NUM,
                               "fence_only": _BOOL, "teardrops": _BOOL}},
        "height_zones": {"type": ["array", "null"], "items": _ref("height_zone")},
        "thermal": {"type": ["object", "null"],
                    "properties": {**_numbers("t_ambient", "tj_margin"),
                                   # consumers read it via int(): 4.0 passes
                                   "ep_vias_min": {"type": "number", "multipleOf": 1,
                                                   X_MESSAGE: "ep_vias_min is a whole "
                                                              "number of vias, e.g. 4 or 4.0"}}},
        "require_sourcing": {"type": ["boolean", "null"]},
        "source": _ref("source"),
        "rebuild": {"enum": list(REBUILD_MODES)},
        "variant": _ref("variant"),
        "blocks": {"type": "array", "items": _ref("block")},
        "assumptions": {"type": "array", "items": _ref("assumption")},
    }),
    "$defs": _DEFS,
}


# --- validation ---------------------------------------------------------------
def _is_strict_int(_checker: Any, instance: Any) -> bool:
    return isinstance(instance, int) and not isinstance(instance, bool)


@functools.lru_cache(maxsize=1)
def _validator() -> Any:
    try:
        from jsonschema import Draft202012Validator, validators
    except ImportError as e:
        raise RuntimeError(
            "netlist_schema.validate needs the 'jsonschema' package (listed in "
            f"server/requirements.txt) and it is not importable here: {e}") from e
    Draft202012Validator.check_schema(SCHEMA)
    checker = Draft202012Validator.TYPE_CHECKER.redefine("integer", _is_strict_int)
    strict = validators.extend(Draft202012Validator, type_checker=checker)
    return strict(SCHEMA)


_JSON_TYPES = ((bool, "boolean"), (int, "integer"), (float, "number"),
               (str, "string"), (list, "array"), (tuple, "array"),
               (dict, "object"), (type(None), "null"))
_DEPENDENCY = re.compile(r"^'(.+)' is a dependency of '(.+)'$")
_SHORT = 60


def _json_type(instance: Any) -> str:
    return next((name for py, name in _JSON_TYPES if isinstance(instance, py)),
                type(instance).__name__)


def _short(instance: Any) -> str:
    text = json.dumps(instance, ensure_ascii=False, default=repr)
    return text if len(text) <= _SHORT else text[:_SHORT - 3] + "..."


def _got(instance: Any) -> str:
    """' (got <value>)' for scalars and arrays; an object is named by its path."""
    return "" if isinstance(instance, dict) else f" (got {_short(instance)})"


def _default_message(err: Any) -> str:
    """Messages for the keywords the schema uses without an x-message; any
    other keyword falls back to jsonschema's own text, truncated."""
    kw, want, inst = err.validator, err.validator_value, err.instance
    if kw == "enum":
        return "must be one of " + ", ".join(json.dumps(v) for v in want) + _got(inst)
    if kw == "minItems":
        return f"needs at least {want} item(s), has {len(inst)}"
    if kw == "minLength" and want == 1:
        return "must not be empty"
    if kw in ("minimum", "exclusiveMinimum"):
        return f"must be {'>=' if kw == 'minimum' else '>'} {want}" + _got(inst)
    if kw == "additionalProperties" and isinstance(inst, dict):
        allowed = sorted((err.schema or {}).get("properties", {}))
        extra = sorted(k for k in inst if k not in allowed)
        noun = "unknown key" if len(extra) == 1 else "unknown keys"
        return (f"{noun} {', '.join(repr(k) for k in extra)} "
                f"(allowed: {', '.join(allowed)})")
    if kw == "dependentRequired":
        m = _DEPENDENCY.match(err.message)
        if m:
            return f"{m.group(2)!r} needs {m.group(1)!r} as well"
    msg = err.message
    return msg if len(msg) <= 200 else msg[:197] + "..."


def _message(err: Any) -> str:
    custom = err.schema.get(X_MESSAGE) if isinstance(err.schema, dict) else None
    if err.validator == "type":
        want = err.validator_value
        kinds = " or ".join(want) if isinstance(want, list) else want
        got = _json_type(err.instance)
        if err.instance is not None and not isinstance(err.instance, dict):
            got += f" {_short(err.instance)}"
        return f"expected {kinds}, got {got}" + (f"; {custom}" if custom else "")
    if custom:
        return custom + _got(err.instance)
    return _default_message(err)


def _component_ref(design: Any, path: list) -> str | None:
    """The ref of the component an error path points into (the path came from
    this design, so components[i] exists), else None."""
    if len(path) < 2 or path[0] != "components" or not isinstance(path[1], int):
        return None
    comp = design["components"][path[1]]
    ref = comp.get("ref") if isinstance(comp, dict) else None
    return ref if isinstance(ref, str) and ref else None


def _sort_key(path: list) -> tuple:
    return tuple((0, p, "") if isinstance(p, int) else (1, 0, str(p)) for p in path)


def _leaf_errors(validator: Any, design: Any) -> list:
    """Every error, with an unannotated oneOf/anyOf replaced by its most
    relevant branch error; a value of the wrong JSON type reports only that
    (its enum / exactly-one rules would just repeat it)."""
    from jsonschema.exceptions import best_match

    errors = []
    for err in validator.iter_errors(design):
        while (err.validator in ("oneOf", "anyOf") and err.context
               and not (isinstance(err.schema, dict) and X_MESSAGE in err.schema)):
            err = best_match(err.context)
        errors.append(err)
    mistyped = {tuple(e.absolute_path) for e in errors if e.validator == "type"}
    return [e for e in errors
            if e.validator == "type" or tuple(e.absolute_path) not in mistyped]


def validate(design: Any) -> list[str]:
    """Schema errors as "<JSON path>: <message>" strings ([] = valid), sorted
    by path. A message inside ``components[i]`` ends with "(component <ref>)".
    Never mutates ``design``. Raises RuntimeError when jsonschema is missing."""
    validator = _validator()        # raises the clear error without jsonschema
    found: dict[tuple, None] = {}
    for err in _leaf_errors(validator, design):
        path = list(err.absolute_path)
        text = f"{err.json_path}: {_message(err)}"
        ref = _component_ref(design, path)
        if ref:
            text += f" (component {ref})"
        found[(_sort_key(path), text)] = None
    return [text for _key, text in sorted(found)]


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args == ["--schema"]:
        print(json.dumps(SCHEMA, indent=2))
        return 0
    if len(args) != 1:
        print("usage: netlist_schema.py <netlist.json> | --schema", file=sys.stderr)
        return 2
    path = Path(args[0])
    try:
        design = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        print(json.dumps({"ok": False, "errors": [f"cannot read {path}: {e}"]}, indent=2))
        return 1
    errors = validate(design)
    print(json.dumps({"ok": not errors, "errors": errors}, indent=2))
    return 0 if not errors else 1


if __name__ == "__main__":
    sys.exit(main())
