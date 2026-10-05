"""netlist_schema — the netlist.json JSON Schema contract and validate().

Covers: every example netlist (and the regression-suite variants derived from
them) validates clean; every roadmap key of docs/ROADMAP_DECISIONS.md D1-D5 has
positive and negative cases; error strings carry the JSON path.
"""
from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest

import netlist_schema as ns

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"
EXAMPLE_NAMES = sorted(p.parent.name for p in EXAMPLES.glob("*/netlist.json"))

SHA = "0123456789abcdef" * 4


def _full_design() -> dict:
    """A design that uses every roadmap key (D1-D5) next to the classic ones."""
    return {
        "name": "sps_node", "title": "SPS node", "class": 2,
        "mode": "napkin",
        "copper_layers": 4,
        "libraries": {
            "symbols": {"SPS_parts": "libs/SPS_parts.kicad_sym"},
            "footprints": {"SPS_parts": "${KIPRJMOD}/libs/SPS_parts.pretty",
                           "Shared": "../libs/Shared.pretty"}},
        "board": {
            "outline": {"loop": [[0, 0], [60, 0], [60, 40, 0.4142], [0, 40]]},
            "cutouts": [{"loop": [[20, 10], [30, 10, 1.0], [30, 20], [20, 20, 1.0]]},
                        {"circle": [45.0, 20.0, 1.6]}],
            "datum": [30.0, 20.0],
            "auto_x": 4.0, "auto_y": 4.0, "auto_w": 30.0, "auto_h": 30.0},
        "components": [
            {"ref": "U1", "lib_id": "SPS_parts:LG290P", "value": "LG290P",
             "footprint": "SPS_parts:LGA-79_12.2x16.0mm", "at": [30.0, 20.0, 90]},
            {"ref": "C1", "lib_id": "Device:C", "value": "100nF",
             "footprint": "Capacitor_SMD:C_0603_1608Metric", "side": "B",
             "at": [25.0, 20.0, 180.0]},
            {"ref": "H1", "footprint": "MountingHole:MountingHole_3.2mm_M3",
             "board_only": True, "at": [3.5, 3.5, 0]},
            {"ref": "R1", "lib_id": "Device:R", "value": "10k",
             "footprint": "Resistor_SMD:R_0603_1608Metric"},
            {"ref": "J1", "lib_id": "Connector:USB_C_Receptacle", "value": "USB-C",
             "footprint": "Connector_USB:USB_C_Receptacle_GCT_USB4105-xx-A",
             "x": 10.0, "y": 2.0, "rot": 0},
        ],
        "nets": {"GND": [["U1", "10"], ["C1", "2"], ["R1", 2]],
                 "+3V3": [["U1", "1"], ["C1", "1"], ["R1", "1"]]},
        "no_connect": [["U1", "5"]],
        "source": {"kind": "kicad", "path": "orig/sps_node.kicad_pcb",
                   "sha256": SHA, "tool": "kicad-cli 10.99.0-4025"},
        "rebuild": "keep",
        "assumptions": [
            {"id": "A1", "topic": "input supply", "value": "5 V USB-C",
             "rationale": "the napkin says USB powered", "confidence": "med",
             "override": "net_specs.VBUS.voltage"},
            {"id": "A2", "topic": "board thickness", "value": 1.6,
             "rationale": "JLCPCB default", "confidence": "high"},
        ],
    }


def _errors_at(errors: list[str], path: str) -> list[str]:
    return [e for e in errors if e.startswith(f"{path}: ")]


def _assert_error(errors: list[str], path: str, fragment: str) -> None:
    hits = _errors_at(errors, path)
    assert hits, f"no error at {path}; got {errors}"
    assert any(fragment in e for e in hits), \
        f"no error at {path} mentions {fragment!r}; got {hits}"


def _comp(d: dict, ref: str) -> dict:
    return next(c for c in d["components"] if c["ref"] == ref)


# --- the schema itself -------------------------------------------------------
def test_schema_is_a_valid_draft_2020_12_schema():
    from jsonschema import Draft202012Validator
    Draft202012Validator.check_schema(ns.SCHEMA)
    assert ns.SCHEMA["$schema"] == "https://json-schema.org/draft/2020-12/schema"


def test_component_schema_types_every_known_key():
    props = ns.SCHEMA["$defs"]["component"]["properties"]
    consumed = {"ref", "lib_id", "value", "footprint", "x", "y", "rot", "side",
                "interior", "edge_row", "overhang", "height", "thermal",
                "rds_mohm", "lcsc", "mpn", "at", "board_only"}
    assert consumed <= set(props), consumed - set(props)


def test_top_level_schema_types_every_known_key():
    props = ns.SCHEMA["properties"]
    known = {"name", "title", "class", "board", "mounting_holes", "components",
             "nets", "no_connect", "power_flags", "net_specs", "preroutes",
             "planes", "antenna_keepout", "emc", "height_zones", "thermal",
             "stackup", "require_sourcing", "libraries", "copper_layers",
             "source", "rebuild", "mode", "assumptions"}
    assert known <= set(props), known - set(props)


def _pipeline_section_2() -> str:
    text = (ROOT / "docs" / "PIPELINE.md").read_text(encoding="utf-8")
    start = text.index("## 2. Author the netlist")
    return text[start:text.index("\n## 3.", start)]


def test_pipeline_doc_section_2_documents_every_schema_key():
    """docs/PIPELINE.md section 2 is the human side of this contract: every
    top-level, board, outline, cutout and component key appears there."""
    doc = _pipeline_section_2()
    defs = ns.SCHEMA["$defs"]
    keys = (set(ns.SCHEMA["properties"]) | set(defs["component"]["properties"])
            | set(defs["board"]["properties"]) | set(defs["outline"]["properties"])
            | set(defs["cutout"]["properties"]) | set(defs["assumption"]["properties"])
            | set(defs["source"]["properties"]))
    missing = sorted(k for k in keys if f'"{k}"' not in doc)
    assert not missing, f"PIPELINE.md section 2 does not document {missing}"


def test_pipeline_doc_section_2_names_every_roadmap_value():
    doc = _pipeline_section_2()
    words = (*ns.SOURCE_KINDS, *ns.REBUILD_MODES, *ns.MODES, *ns.CONFIDENCE_LEVELS,
             *ns.DXF_UNITS, "${KIPRJMOD}", "bulge")
    missing = [w for w in words if w not in doc]
    assert not missing, f"PIPELINE.md section 2 does not name {missing}"
    assert "netlist_schema.py" in doc


def test_enum_constants_match_the_decisions():
    assert ns.SOURCE_KINDS == ("kicad", "eagle", "altium", "easyeda", "vision")
    assert ns.REBUILD_MODES == ("keep", "reflow")
    assert ns.MODES == ("napkin",)
    assert ns.CONFIDENCE_LEVELS == ("high", "med", "low")
    assert ns.COPPER_LAYER_COUNTS == (2, 4, 6)


# --- today's designs stay valid ---------------------------------------------
def test_all_ten_examples_are_present():
    assert len(EXAMPLE_NAMES) == 10, EXAMPLE_NAMES


@pytest.mark.parametrize("name", EXAMPLE_NAMES)
def test_every_example_netlist_validates(name, load_design):
    assert ns.validate(load_design(name)) == []


def test_regression_suite_variants_validate(load_design):
    """The negative fixtures pipeline/run_all.py derives from the examples."""
    neg = load_design("buck_5v3a")
    neg["components"] = [c for c in neg["components"] if c["ref"] not in {"C1", "C2"}]
    neg["nets"] = {n: [p for p in conns if p[0] not in {"C1", "C2"}]
                   for n, conns in neg["nets"].items()}
    assert ns.validate(neg) == []

    g3 = load_design("cp2102n_uart")
    g3["board"] = {"w": 54.0, "h": 52.0,
                   "auto_x": 2.0, "auto_y": 2.0, "auto_w": 34.0, "auto_h": 28.0}
    _comp(g3, "J1").update({"x": 45.0, "y": 14.0, "rot": 270})
    g3["components"].append({
        "ref": "J9", "lib_id": "Connector_Generic:Conn_02x20_Odd_Even",
        "value": "WALL",
        "footprint": "Connector_PinSocket_2.54mm:PinSocket_2x20_P2.54mm_Vertical",
        "x": 27.0, "y": 34.0, "rot": 90, "interior": True})
    g3["no_connect"] += [["J9", str(i)] for i in range(1, 41)]
    assert ns.validate(g3) == []


def test_remediated_bypass_cap_validates(buck_design):
    """remediate._add_decoupling appends a part + string pad pairs."""
    import remediate
    fixed, log = remediate.apply(buck_design, [
        {"auto": True, "fix": {"type": "add_decoupling", "rail": "VIN", "ref": "U1"}}])
    assert len(log) == 1 and len(fixed["components"]) == len(buck_design["components"]) + 1
    assert ns.validate(fixed) == []


def test_full_roadmap_design_validates():
    assert ns.validate(_full_design()) == []


def test_unknown_keys_pass_on_components_and_top_level():
    d = _full_design()
    d["components"][0]["datasheet"] = "https://example.com/lg290p.pdf"
    d["components"][0]["unit"] = 1
    d["notes"] = "hand notes are allowed"
    d["source"]["sch"] = "orig/sps_node.kicad_sch"
    d["assumptions"][0]["stage"] = "requirements"
    assert ns.validate(d) == []


def test_validate_does_not_mutate_the_design():
    d = _full_design()
    d["copper_layers"] = 3
    before = copy.deepcopy(d)
    ns.validate(d)
    assert d == before


# --- positive variants of every roadmap key ----------------------------------
def _rect_board(d: dict) -> None:
    d["board"] = {"w": 60, "h": 40, "corner_radius": 3.0,
                  "cutouts": [{"circle": [30, 20, 2]}], "datum": [0, 40]}


def _dxf_outline(d: dict) -> None:
    d["board"]["outline"] = {"dxf": "mech/outline.dxf", "layer": "OUTLINE",
                             "units": "mm"}


def _dxf_outline_bare(d: dict) -> None:
    d["board"]["outline"] = {"dxf": "C:/mech/outline.dxf"}


def _two_vertex_circle_loop(d: dict) -> None:
    d["board"]["cutouts"] = [{"loop": [[10, 10, 1], [20, 10, 1]]}]


def _negative_bulge(d: dict) -> None:
    d["board"]["outline"]["loop"][2] = [60, 40, -2.5]


def _empty_cutouts(d: dict) -> None:
    d["board"]["cutouts"] = []


def _two_layers(d: dict) -> None:
    d["copper_layers"] = 2


def _six_layers(d: dict) -> None:
    d["copper_layers"] = 6


def _reflow(d: dict) -> None:
    d["rebuild"] = "reflow"
    for c in d["components"]:
        c.pop("at", None)


def _vision_source_minimal(d: dict) -> None:
    d["source"] = {"kind": "vision", "path": "scans/sheet1.pdf"}


def _board_only_with_lib_id(d: dict) -> None:
    _comp(d, "H1")["lib_id"] = "Mechanical:MountingHole"


def _board_only_false_with_lib_id(d: dict) -> None:
    _comp(d, "R1")["board_only"] = False


def _override_null(d: dict) -> None:
    d["assumptions"][0]["override"] = None


def _assumption_value_object(d: dict) -> None:
    d["assumptions"][0]["value"] = {"v": 5.0, "i": 3.0}


def _empty_assumptions(d: dict) -> None:
    d["assumptions"] = []


def _libraries_symbols_only(d: dict) -> None:
    d["libraries"] = {"symbols": {"SPS_parts": "/abs/SPS_parts.kicad_sym"}}


def _nullable_blocks(d: dict) -> None:
    d.update({"mounting_holes": None, "planes": None, "antenna_keepout": None,
              "emc": None, "thermal": None, "height_zones": None,
              "stackup": None, "require_sourcing": None})


def _board_null(d: dict) -> None:
    d["board"] = None
    for c in d["components"]:
        c.pop("at", None)


def _board_empty(d: dict) -> None:
    d["board"] = {}


def _mounting_holes_false(d: dict) -> None:
    d["mounting_holes"] = False


def _lowercase_side(d: dict) -> None:
    _comp(d, "C1")["side"] = "b"


def _integer_pads(d: dict) -> None:
    d["nets"]["GND"] = [["U1", 10], ["C1", 2]]


@pytest.mark.parametrize("mutate", [
    _rect_board, _dxf_outline, _dxf_outline_bare, _two_vertex_circle_loop,
    _negative_bulge, _empty_cutouts, _two_layers, _six_layers, _reflow,
    _vision_source_minimal, _board_only_with_lib_id, _board_only_false_with_lib_id,
    _override_null, _assumption_value_object, _empty_assumptions,
    _libraries_symbols_only, _nullable_blocks, _board_null, _board_empty,
    _mounting_holes_false, _lowercase_side, _integer_pads,
], ids=lambda f: f.__name__.lstrip("_"))
def test_roadmap_key_positive_variants(mutate):
    d = _full_design()
    mutate(d)
    assert ns.validate(d) == []


# --- negative cases: (mutation, JSON path, message fragment) -----------------
def _set(path: str, value):
    """Mutator that sets a dotted path (ints index lists) to ``value``."""
    def mutate(d: dict) -> None:
        keys = [int(k) if k.isdigit() else k for k in path.split(".")]
        target = d
        for k in keys[:-1]:
            target = target[k]
        target[keys[-1]] = value
    mutate.__name__ = f"set {path}={value!r}"
    return mutate


def _drop(path: str):
    def mutate(d: dict) -> None:
        keys = [int(k) if k.isdigit() else k for k in path.split(".")]
        target = d
        for k in keys[:-1]:
            target = target[k]
        del target[keys[-1]]
    mutate.__name__ = f"drop {path}"
    return mutate


def _with(**kw):
    """Mutator that merges keys into component 0 (U1, which carries ``at``)."""
    def mutate(d: dict) -> None:
        d["components"][0].update(kw)
    mutate.__name__ = f"U1 + {kw}"
    return mutate


NEGATIVE = [
    # D1 libraries
    (_set("libraries", ["libs/SPS_parts.pretty"]), "$.libraries", "expected object"),
    (_set("libraries.symbols.SPS_parts", 5), "$.libraries.symbols.SPS_parts",
     "expected string"),
    (_set("libraries.footprints.SPS_parts", ""), "$.libraries.footprints.SPS_parts",
     "must not be empty"),
    (_set("libraries.symbols.bad:nick", "x.kicad_sym"), "$.libraries.symbols",
     "nickname"),
    (_set("libraries.symbols.", "x.kicad_sym"), "$.libraries.symbols", "nickname"),
    (_set("libraries.symbol", {}), "$.libraries", "unknown key 'symbol'"),
    (_set("libraries.footprints", ["x.pretty"]), "$.libraries.footprints",
     "expected object"),
    # D2 board: corner radius, outline, cutouts, datum
    (_set("board.corner_radius", "3"), "$.board.corner_radius", "expected number"),
    (_set("board.corner_radius", -1), "$.board.corner_radius", "must be >= 0"),
    (_set("board.outline", {"loop": [[0, 0], [1, 0], [1, 1]],
                            "dxf": "mech/outline.dxf"}),
     "$.board.outline", "exactly one of 'loop' or 'dxf'"),
    (_set("board.outline", {}), "$.board.outline", "exactly one of 'loop' or 'dxf'"),
    (_set("board.outline", [[0, 0], [1, 0], [1, 1]]), "$.board.outline",
     "expected object"),
    (_set("board.outline.loop.0", ["a", 0]), "$.board.outline.loop[0][0]",
     "expected number"),
    (_set("board.outline.loop.1", {"x": 60, "y": 0}), "$.board.outline.loop[1]",
     "[x, y] or [x, y, bulge]"),
    (_set("board.outline.loop.2", [60, 40, 0.4142, 1]), "$.board.outline.loop[2]",
     "[x, y] or [x, y, bulge]"),
    (_set("board.outline.loop.3", [5]), "$.board.outline.loop[3]",
     "[x, y] or [x, y, bulge]"),
    (_set("board.outline.loop.2", [60, 40, "0.4"]), "$.board.outline.loop[2][2]",
     "expected number"),
    (_set("board.outline.loop", [[0, 0]]), "$.board.outline.loop", "at least 2"),
    (_set("board.outline.loop", "0,0 60,0 60,40"), "$.board.outline.loop",
     "expected array"),
    (_set("board.outline", {"dxf": ""}), "$.board.outline.dxf", "must not be empty"),
    (_set("board.outline", {"dxf": "o.dxf", "units": "furlong"}),
     "$.board.outline.units", "must be one of"),
    (_set("board.outline", {"dxf": "o.dxf", "layer": 7}), "$.board.outline.layer",
     "expected string"),
    (_set("board.outline.layer", "OUTLINE"), "$.board.outline",
     "only apply to a 'dxf' outline"),
    (_set("board.outline.polygon", [[0, 0]]), "$.board.outline",
     "unknown key 'polygon'"),
    (_set("board.cutouts", {"circle": [1, 2, 3]}), "$.board.cutouts",
     "expected array"),
    (_set("board.cutouts.0.circle", [1, 2, 3]), "$.board.cutouts[0]",
     "exactly one of 'loop' or 'circle'"),
    (_set("board.cutouts.1", {}), "$.board.cutouts[1]",
     "exactly one of 'loop' or 'circle'"),
    (_set("board.cutouts.1.circle", [45, 20, 0]), "$.board.cutouts[1].circle[2]",
     "must be > 0"),
    (_set("board.cutouts.1.circle", [45, 20]), "$.board.cutouts[1].circle",
     "[cx, cy, r]"),
    (_set("board.cutouts.1.circle", [45, 20, 1, 2]), "$.board.cutouts[1].circle",
     "[cx, cy, r]"),
    (_set("board.cutouts.0.loop.1", [30, 10, 1, 0]), "$.board.cutouts[0].loop[1]",
     "[x, y] or [x, y, bulge]"),
    (_set("board.cutouts.1.slot", [1, 2]), "$.board.cutouts[1]", "unknown key 'slot'"),
    (_set("board.datum", [1, 2, 3]), "$.board.datum", "[x, y]"),
    (_set("board.datum", "0,0"), "$.board.datum", "expected array"),
    (_set("board", {"auto_x": 3.0}), "$.board", "needs 'w' and 'h'"),
    (_set("board", {"w": 60, "auto_x": 3.0}), "$.board", "'w' needs 'h'"),
    (_set("board", {"w": 60, "h": "40"}), "$.board.h", "expected number"),
    # D3 placement anchor
    (_with(x=30.0), "$.components[0]", "never both"),
    (_with(y=20.0), "$.components[0]", "never both"),
    (_with(rot=90), "$.components[0]", "never both"),
    (_with(at=[30.0, 20.0]), "$.components[0].at", "[x, y, rot]"),
    (_with(at=[30.0, 20.0, 90, 0]), "$.components[0].at", "[x, y, rot]"),
    (_with(at=[30.0, "20", 90]), "$.components[0].at[1]", "expected number"),
    (_with(at="30,20,90"), "$.components[0].at", "[x, y, rot]"),
    # D4 imports
    (_set("copper_layers", 3), "$.copper_layers", "must be one of"),
    (_set("copper_layers", 8), "$.copper_layers", "must be one of"),
    (_set("copper_layers", "4"), "$.copper_layers", "expected integer"),
    (_set("copper_layers", 4.0), "$.copper_layers", "expected integer"),
    (_set("components.2.board_only", "yes"), "$.components[2].board_only",
     "expected boolean"),
    (_drop("components.3.lib_id"), "$.components[3]", "lib_id"),
    (_drop("source.kind"), "$.source", "'kind' is a required property"),
    (_drop("source.path"), "$.source", "'path' is a required property"),
    (_set("source.kind", "orcad"), "$.source.kind", "must be one of"),
    (_set("source.sha256", "abc123"), "$.source.sha256", "64 hex"),
    (_set("source.tool", 11), "$.source.tool", "expected string"),
    (_set("source", "orig/board.kicad_pcb"), "$.source", "expected object"),
    (_set("rebuild", "copy"), "$.rebuild", "must be one of"),
    (_set("rebuild", True), "$.rebuild", "must be one of"),
    # D5 napkin mode and assumptions
    (_set("mode", "yolo"), "$.mode", "must be one of"),
    (_set("mode", 1), "$.mode", "must be one of"),
    (_set("assumptions", {"A1": "x"}), "$.assumptions", "expected array"),
    (_set("assumptions.0", "assume 5 V"), "$.assumptions[0]", "expected object"),
    (_drop("assumptions.0.rationale"), "$.assumptions[0]",
     "'rationale' is a required property"),
    (_drop("assumptions.0.id"), "$.assumptions[0]", "'id' is a required property"),
    (_drop("assumptions.0.topic"), "$.assumptions[0]", "'topic' is a required property"),
    (_drop("assumptions.1.value"), "$.assumptions[1]", "'value' is a required property"),
    (_drop("assumptions.1.confidence"), "$.assumptions[1]",
     "'confidence' is a required property"),
    (_set("assumptions.0.confidence", "medium"), "$.assumptions[0].confidence",
     "must be one of"),
    (_set("assumptions.0.id", ""), "$.assumptions[0].id", "must not be empty"),
    (_set("assumptions.0.rationale", ""), "$.assumptions[0].rationale",
     "must not be empty"),
    (_set("assumptions.0.value", None), "$.assumptions[0].value", "got null"),
    (_set("assumptions.0.override", 5), "$.assumptions[0].override",
     "expected string or null"),
    # existing keys keep their documented types
    (_drop("components"), "$", "'components' is a required property"),
    (_set("components", []), "$.components", "at least 1"),
    (_drop("components.3.ref"), "$.components[3]", "'ref' is a required property"),
    (_drop("components.3.footprint"), "$.components[3]",
     "'footprint' is a required property"),
    (_set("components.3.lib_id", "Device_R"), "$.components[3].lib_id",
     "'Library:Name'"),
    (_set("components.4.side", "top"), "$.components[4].side", "must be one of"),
    (_set("components.4.rot", "90"), "$.components[4].rot", "expected number"),
    (_drop("components.4.y"), "$.components[4]", "'x' needs 'y'"),
    (_set("components.4.interior", "true"), "$.components[4].interior",
     "expected boolean"),
    (_set("components.4.thermal", {"p_diss": "3W"}), "$.components[4].thermal.p_diss",
     "expected number"),
    (_set("nets.GND.0", ["U1"]), "$.nets.GND[0]", "[ref, pad]"),
    (_set("nets.GND.0", ["U1", 1.5]), "$.nets.GND[0][1]", "expected string or integer"),
    (_set("nets.GND", {"U1": "10"}), "$.nets.GND", "expected array"),
    (_set("no_connect.0", ["U1", "5", "x"]), "$.no_connect[0]", "[ref, pad]"),
    (_set("class", 4), "$.class", "must be one of"),
    (_set("power_flags", "VBUS"), "$.power_flags", "expected array"),
    (_set("net_specs", {"VBUS": {"current": "3A"}}), "$.net_specs.VBUS.current",
     "expected number"),
    (_set("net_specs", {"USB_DP": {"diff_pair": 7}}), "$.net_specs.USB_DP.diff_pair",
     "expected string"),
    (_set("preroutes", [{"net": "GND", "path": [["xy", 0, 0], ["xy", 1, 0]]}]),
     "$.preroutes[0]", "'width' is a required property"),
    (_set("preroutes", [{"net": "GND", "width": 1, "path": [["via", 0, 0]]}]),
     "$.preroutes[0].path[0][0]", '"pad" or "xy"'),
    (_set("preroutes", [{"net": "GND", "width": 1, "path": [["pad", "U1"]]}]),
     "$.preroutes[0].path[0]", '["pad", ref, pad, (width)]'),
    (_set("preroutes", [{"net": "GND", "width": 1, "path": [["xy", 0, "1"]]}]),
     "$.preroutes[0].path[0][2]", "expected number"),
    (_set("preroutes", [{"net": "GND", "width": 1, "layer": "Top",
                         "path": [["xy", 0, 0]]}]),
     "$.preroutes[0].layer", "copper layer"),
    (_set("preroutes", [{"net": "GND", "width": 1, "path": [["xy", 0, 0]],
                         "via_size": [1.2]}]),
     "$.preroutes[0].via_size", "[diameter, drill]"),
    (_set("planes", {"Top": "GND"}), "$.planes", "copper layer"),
    (_set("mounting_holes", "yes"), "$.mounting_holes",
     "expected boolean or object or null"),
    (_set("mounting_holes", {"positions": [[1, 2, 3]]}),
     "$.mounting_holes.positions[0]", "[x, y]"),
    (_set("antenna_keepout", {"edge": "north"}), "$.antenna_keepout.edge",
     "must be one of"),
    (_set("antenna_keepout", {"rect": [0, 0, 10]}), "$.antenna_keepout.rect",
     "[x, y, w, h]"),
    (_set("emc", {"stitch": "yes"}), "$.emc.stitch", "expected boolean"),
    (_set("height_zones", [{"x": 0, "y": 0, "w": 1, "h": 1, "max_height": 4,
                            "side": "f"}]),
     "$.height_zones[0].side", "must be one of"),
    (_set("height_zones", [{"x": 0, "y": 0, "w": 1, "h": 1}]),
     "$.height_zones[0]", "'max_height' is a required property"),
    (_set("thermal", {"ep_vias_min": 4.5}), "$.thermal.ep_vias_min",
     "expected integer"),
    (_set("stackup", {"er": "4.2"}), "$.stackup.er", "expected number"),
    (_set("require_sourcing", "yes"), "$.require_sourcing", "expected boolean"),
]


@pytest.mark.parametrize("mutate,path,fragment", NEGATIVE,
                         ids=[m.__name__ for m, _p, _f in NEGATIVE])
def test_negative_cases_report_the_path(mutate, path, fragment):
    d = _full_design()
    mutate(d)
    _assert_error(ns.validate(d), path, fragment)


# --- error strings -----------------------------------------------------------
def test_every_error_string_starts_with_its_json_path():
    d = _full_design()
    d["copper_layers"] = "four"
    d["components"][0]["x"] = 1.0
    d["board"]["outline"]["loop"][1] = [60]
    d["assumptions"][0].pop("rationale")
    errors = ns.validate(d)
    assert len(errors) >= 4
    assert all(e.startswith("$") and ": " in e for e in errors), errors


def test_component_errors_name_the_ref():
    d = _full_design()
    _comp(d, "C1")["at"] = [1, 2]
    errs = _errors_at(ns.validate(d), "$.components[1].at")
    assert errs and all(e.endswith("(component C1)") for e in errs), errs


def test_awkward_net_names_are_quoted_in_the_path():
    d = _full_design()
    d["nets"]["+3V3"][0] = ["U1", 1.5]
    _assert_error(ns.validate(d), "$.nets['+3V3'][0][1]", "expected string or integer")


@pytest.mark.parametrize("mutate,path", [
    (_set("board.outline", [[0, 0], [1, 0], [1, 1]]), "$.board.outline"),
    (_set("board.cutouts.0", [[0, 0], [1, 0]]), "$.board.cutouts[0]"),
    (_set("copper_layers", "4"), "$.copper_layers"),
    (_set("class", "2"), "$.class"),
], ids=["outline-array", "cutout-array", "layers-string", "class-string"])
def test_a_wrong_kind_of_value_is_one_type_error(mutate, path):
    """A type error says it all: the enum / exactly-one rules on the same value
    would only repeat it."""
    d = _full_design()
    mutate(d)
    errs = _errors_at(ns.validate(d), path)
    assert len(errs) == 1 and ": expected " in errs[0], errs


def test_unannotated_one_of_reports_its_most_relevant_branch():
    """Safeguard for future schema edits: a oneOf without an x-message is
    replaced by its best branch error instead of jsonschema's whole-instance
    'is not valid under any of the given schemas'."""
    from jsonschema import Draft202012Validator
    v = Draft202012Validator({"type": "object", "properties": {"p": {"oneOf": [
        {"type": "object", "required": ["a"]},
        {"type": "array", "items": {"type": "number"}}]}}})
    (err,) = ns._leaf_errors(v, {"p": [1, "x"]})
    assert err.validator == "type" and list(err.absolute_path)[:1] == ["p"]
    assert "is not valid under any" not in err.message


def test_long_instances_are_truncated():
    d = _full_design()
    d["nets"]["GND"][0] = ["U1", "1", "x" * 500]
    (err,) = _errors_at(ns.validate(d), "$.nets.GND[0]")
    assert len(err) < 250, err


def test_errors_are_sorted_by_path_and_deterministic():
    d = _full_design()
    d["mode"] = "x"
    d["components"][3]["side"] = "z"
    d["components"][1]["side"] = "z"
    d["copper_layers"] = 5
    first = ns.validate(d)
    assert first == ns.validate(copy.deepcopy(d))
    paths = [e.split(": ", 1)[0] for e in first]
    assert paths == ["$.components[1].side", "$.components[3].side",
                     "$.copper_layers", "$.mode"], first


@pytest.mark.parametrize("bad,kind", [([], "array"), ("netlist", "string"),
                                      (None, "null")])
def test_non_object_design_is_one_root_error(bad, kind):
    assert ns.validate(bad) == [f"$: expected object, got {kind}"
                                + ("" if bad is None else f" {json.dumps(bad)}")]


# --- CLI and import hygiene --------------------------------------------------
def test_cli_reports_ok_for_a_valid_file(tmp_path, capsys):
    p = tmp_path / "netlist.json"
    p.write_text(json.dumps(_full_design()), encoding="utf-8")
    assert ns.main([str(p)]) == 0
    assert json.loads(capsys.readouterr().out) == {"ok": True, "errors": []}


def test_cli_reports_errors_and_exit_1(tmp_path, capsys):
    d = _full_design()
    d["rebuild"] = "copy"
    p = tmp_path / "netlist.json"
    p.write_text(json.dumps(d), encoding="utf-8")
    assert ns.main([str(p)]) == 1
    rep = json.loads(capsys.readouterr().out)
    assert rep["ok"] is False and rep["errors"][0].startswith("$.rebuild: ")


def test_cli_unreadable_file_fails_loudly(tmp_path, capsys):
    p = tmp_path / "netlist.json"
    p.write_text("{not json", encoding="utf-8")
    assert ns.main([str(p)]) == 1
    rep = json.loads(capsys.readouterr().out)
    assert rep["ok"] is False and "cannot read" in rep["errors"][0]


def test_cli_dumps_the_schema(capsys):
    assert ns.main(["--schema"]) == 0
    assert json.loads(capsys.readouterr().out) == ns.SCHEMA


def test_cli_usage_error(capsys):
    assert ns.main([]) == 2
    assert "usage" in capsys.readouterr().err.lower()


def test_import_is_stdlib_only_and_validate_fails_loudly_without_jsonschema():
    """SWIG scripts (KiCad's bundled python: no jsonschema) may import the
    constants; validate() then raises a clear error instead of a NameError."""
    code = (
        "import sys; sys.modules['jsonschema'] = None\n"
        f"sys.path.insert(0, {str(ROOT / 'server' / 'tools')!r})\n"
        "import netlist_schema as ns\n"
        "assert ns.COPPER_LAYER_COUNTS == (2, 4, 6)\n"
        "try:\n"
        "    ns.validate({'components': []})\n"
        "except RuntimeError as e:\n"
        "    print('RAISED', e)\n")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                       stdin=subprocess.DEVNULL, timeout=60)
    assert r.returncode == 0, r.stderr
    assert "RAISED" in r.stdout and "jsonschema" in r.stdout, r.stdout
