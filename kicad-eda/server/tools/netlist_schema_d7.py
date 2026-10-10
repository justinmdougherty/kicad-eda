#!/usr/bin/env python3
"""The D7 fragments of the netlist.json schema (ADR D7.2, D7.7, D7.11; task D7-10a).

netlist_schema merges them (``merge_defs``, ``merge_top_level``); this module holds
the D7 vocabularies and subschemas so the classic schema stays readable:

* ``copper_layers``: 1 (single-sided) or an even number 2..32 (D7.2.1; supersedes
  D4's 2|4|6).
* ``stackup``: exactly one of two shapes (the D7.2.2 oneOf), told apart by the
  legacy keys so an error points into the shape the author meant: the legacy
  shorthand ``{h, er, t}`` (no other key), or the model (fixed shape: unknown keys
  rejected; at most one of ``preset`` / ``layers``, neither = the D7.2.4 default
  geometry; ``roles`` only with ``preset``; ``origin`` designer | layer_policy).
  ``null`` = absent. A layer entry is copper or dielectric (one ply, or ``plies``),
  each fixed-shape.
* ``net_specs`` gain the D7.7 RF keys (``freq_hz`` turns the RF rules on),
  components ``clock_hz`` (Hz: one clock or a list), top-level ``rf`` (chains,
  isolation, antennas, coexistence; fixed-shape entries) and ``layer_policy``
  (napkin hints).

Semantics stay out, as in netlist_schema: alternation, positional names, roles vs
``planes``, references and fab limits are the G1 lint's (lint_rules_stackup,
ST-*). ``D7_KEYS`` lists every property name the fragments define (the docs check
it). Stdlib only, no I/O: KiCad's Python may import it.
"""
from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any

X_MESSAGE = "x-message"              # netlist_schema's annotation keyword (same string)

# --- the D7 vocabularies (stackup_model / stackup_roles / tline read the same) -----
COPPER_LAYER_COUNTS = (1,) + tuple(range(2, 33, 2))                # D7.2.1
MULTILAYER_COUNTS = COPPER_LAYER_COUNTS[1:]                        # layer_policy.max_layers
LEGACY_STACKUP_KEYS = ("h", "er", "t")                             # D7.2.2 (a)
STACKUP_MODEL_KEYS = ("preset", "roles", "layers", "fab", "board_thickness", "finish", "mask",
                      "dk_policy", "design_freq_hz", "impedance_control", "via_spans",
                      "rationale", "origin")                       # D7.2.2 (b)
COPPER_ENTRY_KEYS = ("type", "name", "oz", "thickness", "role", "net")
PLY_KEYS = ("type", "thickness", "material", "construction", "dk", "df", "dk_freq_hz", "label",
            "locked", "dielectric_model")
PLY_VALUE_KEYS = tuple(k for k in PLY_KEYS if k not in ("type", "locked"))
DIELECTRIC_ENTRY_KEYS = PLY_KEYS + ("kind", "plies")
MASK_KEYS = ("material", "dk", "df", "color", "over_substrate", "over_trace", "between_traces")
LAYER_ROLES = ("signal", "plane", "mixed", "unused")
DIELECTRIC_KINDS = ("core", "prepreg")
FINISHES = ("HASL", "LF-HASL", "ENIG", "OSP", "ImAg", "ImSn")
ORIGINS = ("designer", "layer_policy")
DK_POLICIES = ("auto", "fab", "design", "process", "typical")
LINE_KINDS = ("auto", "microstrip", "cpwg", "stripline", "dual_stripline",
              "embedded_microstrip", "cpw")                        # D7.7 net_specs line
PAIR_KINDS = ("edge", "broadside")
RF_ROLES = ("tx", "rx", "trx")
ROUTE_MODES = ("auto", "preroute", "agent")                        # never Freerouting for RF
ANTENNA_KINDS = ("pcb_trace", "chip", "module", "connector")
GENERATOR_FAMILIES = ("ifa", "mifa", "meander", "chip_land")
EDGES = ("top", "bottom", "left", "right")                         # = netlist_schema.BOARD_EDGES
DK_RANGE = (1.0, 15.0)
DF_RANGE = (0.0, 0.1)

_NUM = {"type": "number"}
_POS = {"type": "number", "exclusiveMinimum": 0}
_POS_OR_NULL = {"type": ["number", "null"], "exclusiveMinimum": 0}
_NON_NEG = {"type": "number", "minimum": 0}
_BOOL = {"type": "boolean"}
_NAME = {"type": "string", "minLength": 1}
_NAME_OR_NULL = {"type": ["string", "null"], "minLength": 1}


def _ref(name: str) -> dict:
    return {"$ref": f"#/$defs/{name}"}


def _enum(values: tuple, *, null: bool = False) -> dict:
    return {"enum": [*values, *([None] if null else [])]}


def _range(lo: float, hi: float, what: str) -> dict:
    return {"type": "number", "minimum": lo, "maximum": hi,
            X_MESSAGE: f"{what} is a number in [{lo:g}, {hi:g}]"}


def _array(items: dict, min_items: int = 0) -> dict:
    out = {"type": "array", "items": items}
    return {**out, "minItems": min_items} if min_items else out


def _fixed(properties: dict, *, required: tuple = (), nullable: bool = False) -> dict:
    """A fixed-shape object: unknown keys are rejected."""
    out = {"type": ["object", "null"] if nullable else "object", "properties": properties,
           "additionalProperties": False}
    return {**out, "required": list(required)} if required else out


def _tuple(*items: dict, message: str, nullable: bool = False) -> dict:
    return {"type": ["array", "null"] if nullable else "array", "prefixItems": list(items),
            "minItems": len(items), "maxItems": len(items), X_MESSAGE: message}


def _rect(nullable: bool = False) -> dict:
    return _tuple(_NUM, _NUM, _POS, _POS, nullable=nullable,
                  message="a rect is [x, y, w, h] (mm, board frame; w and h > 0)")


_BAND = _tuple(_POS, _POS, message="a band is [low, high] (Hz)")
_PLY_VALUES = {"thickness": _POS, "material": _NAME, "construction": _NAME,
               "dk": _range(*DK_RANGE, "dk"), "df": _range(*DF_RANGE, "df"),
               "dk_freq_hz": _POS, "label": _NAME, "dielectric_model": _NAME}

# --- stackup (D7.2.2) ------------------------------------------------------------
_STACKUP_DEFS: dict[str, Any] = {
    "stackup_legacy": {
        **_fixed({"h": _POS, "er": _range(*DK_RANGE, "er"), "t": _POS}),
        X_MESSAGE: "the legacy shorthand {h, er, t} cannot be mixed with stackup model keys "
                   "(ADR D7.2.2): drop h/er/t and declare a preset or layers instead"},
    "stackup_model": {
        **_fixed({
            "preset": {"type": "string", "pattern": r"^[^:\s]+:[^:\s]+$",
                       X_MESSAGE: "a preset is '<fab>:<template>', e.g. "
                                  "'jlcpcb:JLC04161H-3313'"},
            "roles": {"type": "object", "propertyNames": _ref("copper_layer"),
                      "additionalProperties": _ref("stackup_role")},
            "layers": _array(_ref("stackup_layer"), 1),
            "fab": _NAME_OR_NULL,
            "board_thickness": _POS,
            "finish": _enum(FINISHES, null=True),
            "mask": _ref("stackup_mask"),
            "dk_policy": _enum(DK_POLICIES),
            "design_freq_hz": _POS,
            "impedance_control": _fixed({"required": _BOOL, "tolerance_pct": _POS}),
            "via_spans": _array(_tuple(_ref("copper_layer"), _ref("copper_layer"),
                                       message="a via span is [top layer, bottom layer], "
                                               "e.g. [\"F.Cu\", \"B.Cu\"]"), 1),
            "rationale": _NAME,
            "origin": _enum(ORIGINS)}),
        "allOf": [{"not": {"required": ["preset", "layers"]},
                   X_MESSAGE: "give at most one of 'preset' and 'layers' (neither = the "
                              "default geometry for copper_layers, ADR D7.2.4)"}],
        "dependentSchemas": {"roles": {
            "required": ["preset"],
            X_MESSAGE: "'roles' overrides a preset's roles, so it needs 'preset'; in 'layers' "
                       "set role / net on each copper entry"}},
    },
    "stackup_role": _fixed({"role": _enum(LAYER_ROLES), "net": _NAME_OR_NULL},
                           required=("role",)),
    "stackup_layer": {
        "type": "object", "required": ["type"],
        "properties": {"type": _enum(("copper", "dielectric"))},
        "allOf": [{"if": {"required": ["type"], "properties": {"type": {"const": kind}}},
                   "then": _ref(f"stackup_{kind}")} for kind in ("copper", "dielectric")]},
    "stackup_copper": _fixed({"type": {"const": "copper"}, "name": _ref("copper_layer"),
                              "oz": _POS, "thickness": _POS, "role": _enum(LAYER_ROLES),
                              "net": _NAME_OR_NULL}),
    "stackup_dielectric": {
        **_fixed({"type": {"const": "dielectric"}, "kind": _enum(DIELECTRIC_KINDS),
                  **_PLY_VALUES, "locked": _BOOL, "plies": _array(_ref("stackup_ply"), 1)}),
        "dependentSchemas": {"plies": {
            "not": {"anyOf": [{"required": [k]} for k in PLY_VALUE_KEYS]},
            X_MESSAGE: "a multi-ply dielectric gives thickness, material, dk, ... per ply in "
                       "'plies', not on the dielectric itself"}},
        "if": {"not": {"required": ["plies"]}},
        "then": {"required": ["thickness"],
                 X_MESSAGE: "a dielectric needs a 'thickness' (mm), or 'plies'"}},
    "stackup_ply": _fixed({"type": {"const": "dielectric"}, **_PLY_VALUES, "locked": _BOOL},
                          required=("thickness",)),
    "stackup_mask": _fixed({"material": _NAME, "dk": _range(*DK_RANGE, "dk"),
                            "df": _range(*DF_RANGE, "df"), "color": _NAME,
                            "over_substrate": _POS, "over_trace": _POS, "between_traces": _POS},
                           nullable=True),
}

# --- RF intent (D7.7) ------------------------------------------------------------
_RF_DEFS: dict[str, Any] = {
    "layer_lists": {"type": "object", "propertyNames": _ref("copper_layer"),
                    "additionalProperties": _array(_ref("copper_layer"))},
    "rf_fence": _fixed({"pitch_mm": _POS_OR_NULL, "offset_mm": _POS_OR_NULL,
                        "via": _tuple(_POS, _POS, message="via is [diameter, drill] (mm)"),
                        "rows": {"type": "integer", "minimum": 1}, "freq_hz": _POS_OR_NULL}),
    "rf_exempt": _fixed({"rect": _rect(), "layers": _array(_ref("copper_layer"), 1),
                         "why": _NAME}, required=("rect", "why")),
    "rf": _fixed({"chains": _array(_ref("rf_chain")), "isolation": _array(_ref("rf_isolation")),
                  "antennas": _array(_ref("rf_antenna")), "coexistence": _ref("rf_coexistence")},
                 nullable=True),
    "rf_chain": _fixed({"name": _NAME, "group": _NAME, "from": _ref("pad_ref"),
                        "to": _ref("pad_ref"), "nets": _array(_NAME, 1),
                        "series": _ref("ref_list"), "shunts": _ref("ref_list"),
                        "match_max_dist_mm": _POS, "max_len_mm": _POS, "tx_power_dbm": _NUM},
                       required=("name", "nets")),
    "rf_isolation": _fixed({"groups": _array(_NAME, 2), "min_mm": _NON_NEG,
                            "edges_differ": _BOOL}, required=("groups",)),
    "rf_antenna": _fixed({
        "ref": _NAME, "kind": _enum(ANTENNA_KINDS), "preset": _NAME_OR_NULL,
        "generator": _ref("antenna_generator"), "feed_net": _NAME_OR_NULL, "ground_net": _NAME,
        "keepout": _ref("antenna_keepout_area"), "ground_under": _BOOL,
        "edge": _enum(EDGES, null=True), "clearance_mm": _NON_NEG, "license_ack": _BOOL,
        "match": _ref("antenna_match"), "sim": _NAME, "human_steps": _array(_NAME)},
        required=("ref", "kind")),
    "antenna_generator": _fixed({"family": _enum(GENERATOR_FAMILIES), "band_hz": _BAND,
                                 "params": {"type": "object"}},
                                required=("family",), nullable=True),
    "antenna_keepout_area": _fixed({
        "layers": {"type": ["string", "array"],
                   "if": {"type": "string"},
                   "then": {"const": "all",
                            X_MESSAGE: "keepout layers is \"all\" or a list of copper layers"},
                   "else": {"items": _ref("copper_layer"), "minItems": 1}},
        "from_footprint": _BOOL, "rect": _rect(nullable=True),
        "polygon": {"type": ["array", "null"], "items": _ref("point"), "minItems": 3}}),
    "antenna_match": _fixed({"topology": _NAME, "refs": _ref("ref_list"), "max_dist_mm": _POS,
                             "fit": {"type": "object",
                                     "propertyNames": {"minLength": 1,
                                                       X_MESSAGE: "a fit ref must not be empty"},
                                     "additionalProperties": _NAME}}),
    "rf_coexistence": _fixed({"check": _BOOL, "tx": _array(_ref("rf_tx")),
                              "rx": _array(_ref("rf_rx")),
                              "isolation_db": _array(_ref("rf_isolation_db")),
                              "margin_db": _NUM}),
    "rf_tx": _fixed({"group": _NAME, "band_hz": _BAND, "power_dbm": _NUM},
                    required=("group", "band_hz")),
    "rf_rx": _fixed({"group": _NAME, "bands_hz": _array(_BAND, 1), "abs_max_dbm": _NUM,
                     "oob_rejection_db": _NUM, "lna_gain_db": _NUM},
                    required=("group", "bands_hz")),
    "rf_isolation_db": _fixed({"from": _NAME, "to": _NAME, "db": _NUM},
                              required=("from", "to", "db")),
    # D7.11 napkin hints (wave 3 scores layer counts with them)
    "layer_policy": _fixed({"max_layers": {"type": "integer", "enum": list(MULTILAYER_COUNTS),
                                           X_MESSAGE: "max_layers is an even number 2..32"},
                            "fab": _NAME}, nullable=True),
}

DEFS: Mapping[str, Any] = {**_STACKUP_DEFS, **_RF_DEFS}

TOP_LEVEL_PROPERTIES: Mapping[str, Any] = {
    "copper_layers": {"type": "integer", "enum": list(COPPER_LAYER_COUNTS),
                      X_MESSAGE: "copper_layers is 1 (single-sided) or an even number 2..32 "
                                 "(ADR D7.2.1)"},
    "stackup": {"type": ["object", "null"],
                "if": {"type": "object",
                       "anyOf": [{"required": [k]} for k in LEGACY_STACKUP_KEYS]},
                "then": _ref("stackup_legacy"),
                "else": {"if": {"type": "object"}, "then": _ref("stackup_model")},
                X_MESSAGE: "stackup is the legacy {h, er, t} shorthand, the stackup model "
                           "object or null (ADR D7.2.2)"},
    "rf": _ref("rf"),
    "layer_policy": _ref("layer_policy"),
}

NET_SPEC_PROPERTIES: Mapping[str, Any] = {
    "freq_hz": _POS, "role": _enum(RF_ROLES), "line": _enum(LINE_KINDS),
    "pair": _enum(PAIR_KINDS),
    "layers": _array(_ref("copper_layer"), 1),
    "ref_layers": _ref("layer_lists"), "void_layers": _ref("layer_lists"),
    "ref_nets": _array(_NAME, 1),
    "width": _POS_OR_NULL, "ground_gap": _POS_OR_NULL, "z_tol_pct": _POS,
    "max_vias": {"type": "integer", "minimum": 0},
    "max_len_mm": _POS_OR_NULL, "stub_max_mm": _POS_OR_NULL,
    "fence": {"type": ["object", "boolean"], "if": {"type": "boolean"},
              "then": {"const": False,
                       X_MESSAGE: "fence is an object or false (omit it for the defaults)"},
              "else": _ref("rf_fence")},
    "isolation_mm": _NON_NEG, "rf_group": _NAME, "ref_plane_margin_h": _POS,
    "ref_plane_exempt": _array(_ref("rf_exempt")),
    "allow_branches": _array(_ref("pad_ref")),
    "route": _enum(ROUTE_MODES),
}

COMPONENT_PROPERTIES: Mapping[str, Any] = {
    "clock_hz": {"type": ["number", "array"], "exclusiveMinimum": 0, "items": _POS,
                 "minItems": 1,
                 X_MESSAGE: "clock_hz is a clock frequency in Hz or a list of them, each > 0"},
}


# --- the merge -------------------------------------------------------------------
def _merged(base: Mapping[str, Any], extra: Mapping[str, Any], what: str) -> dict[str, Any]:
    clash = sorted(set(base) & set(extra))
    if clash:
        raise ValueError(f"netlist_schema_d7: {what} already defines {clash}")
    return {**base, **extra}


def _with_properties(base_defs: Mapping[str, Any], name: str,
                     extra: Mapping[str, Any]) -> dict[str, Any]:
    if name not in base_defs:
        raise ValueError(f"netlist_schema_d7: the base schema has no '{name}' definition")
    schema = base_defs[name]
    return {**schema, "properties": _merged(schema.get("properties", {}), extra,
                                            f"$defs.{name}.properties")}


def _refs(node: Any) -> Iterator[str]:
    if isinstance(node, Mapping):
        if isinstance(node.get("$ref"), str):
            yield node["$ref"]
        for value in node.values():
            yield from _refs(value)
    elif isinstance(node, (list, tuple)):
        for value in node:
            yield from _refs(value)


def merge_defs(base_defs: Mapping[str, Any]) -> dict[str, Any]:
    """New $defs: ``base_defs`` plus the D7 definitions, with the RF keys merged into
    ``net_spec`` and ``clock_hz`` into ``component``. Raises ValueError on a name the
    base already defines or a $ref (also from TOP_LEVEL_PROPERTIES) to no definition."""
    defs = {**_merged(base_defs, DEFS, "$defs"),
            "component": _with_properties(base_defs, "component", COMPONENT_PROPERTIES),
            "net_spec": _with_properties(base_defs, "net_spec", NET_SPEC_PROPERTIES)}
    prefix = "#/$defs/"
    dangling = sorted({r for r in _refs([defs, TOP_LEVEL_PROPERTIES])
                       if not (r.startswith(prefix) and r[len(prefix):] in defs)})
    if dangling:
        raise ValueError(f"netlist_schema_d7: $ref to no definition: {dangling}")
    return defs


def merge_top_level(base_properties: Mapping[str, Any]) -> dict[str, Any]:
    """New top-level properties: ``base_properties`` plus the D7 keys (copper_layers,
    stackup, rf, layer_policy); ValueError if the base already defines one."""
    return _merged(base_properties, TOP_LEVEL_PROPERTIES, "the top-level properties")


def _property_names(node: Any) -> Iterator[str]:
    if isinstance(node, Mapping):
        props = node.get("properties")
        if isinstance(props, Mapping):
            yield from props
        for value in node.values():
            yield from _property_names(value)
    elif isinstance(node, (list, tuple)):
        for value in node:
            yield from _property_names(value)


D7_KEYS: tuple[str, ...] = tuple(sorted(
    {*TOP_LEVEL_PROPERTIES, *NET_SPEC_PROPERTIES, *COMPONENT_PROPERTIES,
     *_property_names([DEFS, TOP_LEVEL_PROPERTIES, NET_SPEC_PROPERTIES])}))
