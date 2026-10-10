"""build_from_netlist: the sch_build.py topology produced through the KiCad 11
API instead of hand-written S-expressions.

Same layout and connectivity as ``server/tools/sch_build.py`` so ERC results
and the downstream pipeline stay identical:

* one grid cell per (ref, unit) in component order, units ascending, 5 columns
  of 63.5 x 50.8 mm from (50.8, 38.1) (``sch_geometry.grid_cell``);
* Value and Footprint set on EVERY unit (KiCad units do not share fields; a
  mismatch is an ERC error) but only AFTER every placement: a field update
  before a later placement of the same lib_id makes KiCad 11 embed a renamed
  private copy of the library symbol (units then lose their ``Library:`` link);
* a local label with the net name at every net pin, a no-connect marker at
  every ``no_connect`` pin;
* a power:PWR_FLAG (+ label) for every net that has a power_in pin and no
  power_out pin, plus nets declared electrically in ``net_specs`` (voltage or
  current) or listed in ``power_flags`` that have no output/power_out driver;
* a (ref, pad) that resolves to no pin is collected in ``missing_pins`` and the
  build returns ok=False WITHOUT writing a file (never a silent gap);
* the D4 component flags as sch_build reads them (``sch_build.schematic_design``):
  a ``board_only`` part is not placed (it needs no lib_id) and its pads get no
  label or no-connect marker; ``dnp: true`` / ``in_bom: false`` set the
  instance attributes ``do_not_populate`` / ``exclude_from_bill_of_materials``
  through kipy after every placement (an update before a later placement of
  the same lib_id would fork the library symbol, see above). A kipy without
  the field, or a KiCad that does not apply it, gives a warning per part --
  the flag is never claimed.
"""
from __future__ import annotations

import sys
from collections.abc import Mapping
from typing import Callable

import kicad_env as env
from backends import kipy_bridge as kb
from backends import sch_geometry as geo
from backends import sch_items as items
from backends.errors import PinNotFoundError, SchematicApiError
from backends import sch_session as sess
from backends.sch_api import BACKEND, SchematicApi

if str(env.TOOLS_DIR) not in sys.path:      # server/tools holds sch_build
    sys.path.insert(0, str(env.TOOLS_DIR))
import sch_build  # noqa: E402

DRIVER_TYPES = frozenset({"power_out", "output"})
PinType = Callable[[str, str], str]
# (netlist flag, the value that turns it on, the kipy SchematicSymbolAttributes field)
ASSEMBLY_ATTRIBUTES = (("dnp", True, "do_not_populate"),
                       ("in_bom", False, "exclude_from_bill_of_materials"))
_FLAG_OF = {attr: flag for flag, _on, attr in ASSEMBLY_ATTRIBUTES}
_EDITOR_HINT = "set it in the schematic editor"


# --- validation (the design dict is external input) -------------------------
def _check_pairs(where: str, conns) -> None:
    if not isinstance(conns, list):
        raise SchematicApiError(f"{where} must be a list of [ref, pad] pairs")
    for pair in conns:
        if not (isinstance(pair, (list, tuple)) and len(pair) == 2):
            raise SchematicApiError(f"{where} entries must be [ref, pad] pairs, got {pair!r}")


def _check_component(index: int, comp, seen: set[str]) -> None:
    ref = comp.get("ref") if isinstance(comp, dict) else None
    if not isinstance(ref, str) or not ref.strip():
        raise SchematicApiError(f"components[{index}] needs a non-empty string 'ref'")
    try:
        flags = {key: sch_build.component_flag(comp, key) for key in sch_build.FLAG_KEYS}
    except ValueError as exc:
        raise SchematicApiError(f"components[{index}]: {exc}") from exc
    lib_id = comp.get("lib_id")
    if flags["board_only"] is not True and (not isinstance(lib_id, str) or ":" not in lib_id):
        raise SchematicApiError(f"components[{index}] ({ref}) needs a 'lib_id' like 'Device:R' "
                                "(only a \"board_only\": true part may omit it)")
    if ref in seen:
        raise SchematicApiError(f"duplicate reference {ref!r} in components")
    seen.add(ref)


def validate_design(design) -> None:
    """Raise SchematicApiError unless ``design`` has the netlist.json shape."""
    if not isinstance(design, dict):
        raise SchematicApiError("design must be a dict (the parsed netlist.json)")
    comps = design.get("components")
    if not isinstance(comps, list) or not comps:
        raise SchematicApiError("design['components'] must be a non-empty list")
    seen: set[str] = set()
    for index, comp in enumerate(comps):
        _check_component(index, comp, seen)
    nets = design.get("nets", {})
    if not isinstance(nets, dict):
        raise SchematicApiError("design['nets'] must be a dict {net: [[ref, pad], ...]}")
    for net, conns in nets.items():
        _check_pairs(f"nets[{net!r}]", conns)
    _check_pairs("no_connect", design.get("no_connect", []))


# --- the sch_build rules, pure ----------------------------------------------
def power_flag_nets(design: dict, pin_type: PinType) -> set[str]:
    """Nets that get a PWR_FLAG (KiCad's power_pin_not_driven rule plus the
    declared rails), exactly as sch_build computes them."""
    nets = design.get("nets", {})
    flagged: set[str] = set()
    driven: set[str] = set()
    for net, conns in nets.items():
        types = [pin_type(ref, pad) for ref, pad in conns]
        if any(t in DRIVER_TYPES for t in types):
            driven.add(net)
        if "power_in" in types and "power_out" not in types:
            flagged.add(net)
    specs = design.get("net_specs", {}) or {}
    declared = {n for n, s in specs.items() if s.get("voltage", 0) or s.get("current", 0)}
    declared |= set(design.get("power_flags", []) or [])
    return flagged | {n for n in declared if n in nets and n not in driven}


def pin_type_lookup(api: SchematicApi, components: list[dict]) -> PinType:
    """pin_type(ref, pad) -> 'power_in' | 'passive' | ... ('' when unknown);
    pad may be a pin number or a pin name (number wins, first name wins)."""
    table: dict[tuple[str, str], str] = {}
    for comp in components:
        ref = comp["ref"]
        for info in api.pin_info(ref):
            table[(ref, info["number"])] = info["type"]
            if info["name"] and info["name"] not in ("~",):
                table.setdefault((ref, info["name"]), info["type"])
    return lambda ref, pad: table.get((str(ref), str(pad)), "")


# --- build steps ------------------------------------------------------------
def place_components(api: SchematicApi, components: list[dict]) -> list[dict]:
    """One placement per (ref, unit) in sch_build's cell order, then the
    Value/Footprint fields of every unit (two passes: see the module doc)."""
    placed: list[dict] = []
    for comp in components:
        first = api.place_symbol(comp["lib_id"], comp["ref"], *geo.grid_cell(len(placed)), unit=1)
        placed.append(first)
        for unit in first["units"]:
            if unit == 1:
                continue
            placed.append(api.place_symbol(comp["lib_id"], comp["ref"],
                                           *geo.grid_cell(len(placed)), unit=unit))
    set_fields(api, components)
    return placed


def set_fields(api: SchematicApi, components: list[dict]) -> None:
    """Value / Footprint on every placed unit of every component that has one."""
    for comp in components:
        value = comp.get("value", "") or ""
        footprint = comp.get("footprint", "") or ""
        if value or footprint:
            api.set_fields(comp["ref"], value=value, footprint=footprint)


def assembly_attributes(comp: Mapping) -> dict[str, bool]:
    """The SchematicSymbolAttributes fields a component's D4 flags turn on
    (``dnp: true`` -> do_not_populate, ``in_bom: false`` ->
    exclude_from_bill_of_materials). Flags only ever turn attributes ON, as
    the board builder's do."""
    return {attr: True for flag, on, attr in ASSEMBLY_ATTRIBUTES if comp.get(flag) is on}


def _unsupported_attributes(names: set[str]) -> set[str]:
    """The attribute fields this kipy cannot set on a symbol instance."""
    inst_cls = getattr(kb.st, "SchematicSymbolInstance", None)
    attrs_cls = getattr(kb.st, "SchematicSymbolAttributes", None)
    if attrs_cls is None or not isinstance(getattr(inst_cls, "attributes", None), property):
        return set(names)
    return {n for n in names if not isinstance(getattr(attrs_cls, n, None), property)}


def _link_to_library(inst, lib_id: str) -> None:
    """Point ``inst.definition`` back at ``lib_id``. GetSymbols reports a unit
    whose embedded symbol KiCad has privately renamed ("C_1", "LM358_1": the
    second user of a lib_id after an UpdateItems) with the id ":C_1"; KiCad
    writes the definition back on UpdateItems, so sending that id unlinks the
    unit from its library (lib_id "C_1", ERC lib_symbol_issues). Verified on
    10.99 with lm358_buffer: restoring the id keeps every unit linked."""
    library, _, name = lib_id.partition(":")
    inst.definition.id.library = library
    inst.definition.id.name = name


def _flag_targets(sch, wanted: Mapping[str, Mapping[str, bool]],
                  lib_ids: Mapping[str, str]) -> list:
    """Every placed unit of a part in ``wanted``, its attributes set and its
    library link restored (not yet sent to KiCad)."""
    targets = []
    for inst in sch.get_symbols():                # fresh copies, owned by this call
        attrs = wanted.get(inst.reference)
        if not attrs:
            continue
        for attr, value in attrs.items():
            setattr(inst.attributes, attr, value)
        _link_to_library(inst, lib_ids[inst.reference])
        items.prime_definition(inst)              # keep the embedded lib symbol intact
        targets.append(inst)
    return targets


def _not_applied(sent: list, echoed: list, wanted: Mapping[str, Mapping[str, bool]]) -> list[str]:
    """A warning per unit whose echo from KiCad lacks a requested attribute."""
    if len(echoed) != len(sent):
        raise SchematicApiError(f"KiCad updated {len(echoed)} of {len(sent)} symbols "
                                "while setting dnp / in_bom")
    return [f"{inst.reference} unit {inst.unit}: KiCad did not apply {attr} "
            f"({_FLAG_OF[attr]}); {_EDITOR_HINT}"
            for inst in echoed for attr, value in wanted.get(inst.reference, {}).items()
            if getattr(inst.attributes, attr) != value]


def apply_assembly_flags(api: SchematicApi, components: list[dict]) -> list[str]:
    """Set the dnp / in_bom attributes of ``components`` on every placed unit
    in one UpdateItems call, then make ``api`` re-read its symbols. Returns
    warnings (a field this kipy lacks, a field KiCad did not apply)."""
    asked = {c["ref"]: attrs for c in components if (attrs := assembly_attributes(c))}
    unsupported = _unsupported_attributes({a for attrs in asked.values() for a in attrs})
    warnings = [f"{ref}: this kipy has no SchematicSymbolAttributes.{attr}; {_FLAG_OF[attr]} "
                f"is not set in the schematic ({_EDITOR_HINT})"
                for ref, attrs in asked.items() for attr in attrs if attr in unsupported]
    wanted = {ref: {a: v for a, v in attrs.items() if a not in unsupported}
              for ref, attrs in asked.items()}
    wanted = {ref: attrs for ref, attrs in wanted.items() if attrs}
    if not wanted:
        return warnings
    sch = api.session.schematic_for(api.path)
    lib_ids = {c["ref"]: c["lib_id"] for c in components if c["ref"] in wanted}
    try:
        sent = _flag_targets(sch, wanted, lib_ids)
        echoed = list(sch.update_items(sent)) if sent else []
    except kb.KIPY_EXCEPTIONS as exc:
        raise SchematicApiError(f"setting dnp / in_bom ({api.path}): {exc}") from exc
    warnings += _not_applied(sent, echoed, wanted)
    api.symbols()                                 # drop api's cached copies of those units
    return warnings


def pin_items(api: SchematicApi, design: dict) -> tuple[list, list, list]:
    """(labels [(net, x, y)], no_connects [(x, y)], missing [[ref, pad]])."""
    labels: list[tuple[str, float, float]] = []
    no_connects: list[tuple[float, float]] = []
    missing: list[list[str]] = []

    def locate(ref, pad):
        try:
            return api.pin_xy(str(ref), str(pad))
        except PinNotFoundError:
            missing.append([str(ref), str(pad)])
            return None

    for net, conns in design.get("nets", {}).items():
        for ref, pad in conns:
            point = locate(ref, pad)
            if point is not None:
                labels.append((net, *point))
    for ref, pad in design.get("no_connect", []):
        point = locate(ref, pad)
        if point is not None:
            no_connects.append(point)
    return labels, no_connects, missing


def place_power_flags(api: SchematicApi, design: dict, pin_type: PinType) -> list[str]:
    nets = sorted(power_flag_nets(design, pin_type))
    for index, net in enumerate(nets):
        api.add_power_flag(net, *geo.flag_position(index))
    return nets


def _build(api: SchematicApi, netlist: dict) -> dict:
    design = sch_build.schematic_design(netlist)       # board_only parts left to the board
    board_only = sch_build.board_only_refs(netlist)
    placements = place_components(api, design["components"])
    labels, no_connects, missing = pin_items(api, design)
    base = {"sch": api.path, "components": len(design["components"]),
            "nets": len(design.get("nets", {})), "placements": len(placements),
            "warnings": [], "backend": BACKEND,
            **({"board_only": board_only} if board_only else {})}
    if missing:
        api.discard()
        return {**base, "ok": False, "error": f"{len(missing)} pins have no coordinates",
                "missing_pins": missing}
    warnings = apply_assembly_flags(api, design["components"])
    api.add_labels(labels)
    api.add_no_connects(no_connects)
    flags = place_power_flags(api, design, pin_type_lookup(api, design["components"]))
    api.save()
    return {**base, "ok": True, "missing_pins": [], "labels": len(labels),
            "no_connects": len(no_connects), "power_flags": flags, "warnings": warnings}


def build_from_netlist(design: dict, out_path: str, overwrite: bool = True,
                       session: sess.KiCadSession | None = None) -> dict:
    """Build ``out_path`` from a netlist.json dict through the API (headless).
    Returns {ok, sch, components, nets, missing_pins, warnings, backend: "api",
    ...} (counts of what the schematic holds; "board_only": [refs] when parts
    were left to the board); ok=False with ``missing_pins`` when any (ref,
    pad) has no pin."""
    validate_design(design)
    api = SchematicApi.open(out_path, create=True, overwrite=overwrite, mode="headless",
                            session=session)
    try:
        return _build(api, design)
    finally:
        api.close()
