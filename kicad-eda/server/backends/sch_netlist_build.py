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
  build returns ok=False WITHOUT writing a file (never a silent gap).
"""
from __future__ import annotations

from typing import Callable

from backends import sch_geometry as geo
from backends.errors import PinNotFoundError, SchematicApiError
from backends import sch_session as sess
from backends.sch_api import BACKEND, SchematicApi

DRIVER_TYPES = frozenset({"power_out", "output"})
PinType = Callable[[str, str], str]


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
    lib_id = comp.get("lib_id")
    if not isinstance(lib_id, str) or ":" not in lib_id:
        raise SchematicApiError(f"components[{index}] ({ref}) needs a 'lib_id' like 'Device:R'")
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


def _build(api: SchematicApi, design: dict) -> dict:
    placements = place_components(api, design["components"])
    labels, no_connects, missing = pin_items(api, design)
    base = {"sch": api.path, "components": len(design["components"]),
            "nets": len(design.get("nets", {})), "placements": len(placements),
            "warnings": [], "backend": BACKEND}
    if missing:
        api.discard()
        return {**base, "ok": False, "error": f"{len(missing)} pins have no coordinates",
                "missing_pins": missing}
    api.add_labels(labels)
    api.add_no_connects(no_connects)
    flags = place_power_flags(api, design, pin_type_lookup(api, design["components"]))
    api.save()
    return {**base, "ok": True, "missing_pins": [], "labels": len(labels),
            "no_connects": len(no_connects), "power_flags": flags}


def build_from_netlist(design: dict, out_path: str, overwrite: bool = True,
                       session: sess.KiCadSession | None = None) -> dict:
    """Build ``out_path`` from a netlist.json dict through the API (headless).
    Returns {ok, sch, components, nets, missing_pins, warnings, backend: "api",
    ...}; ok=False with ``missing_pins`` when any (ref, pad) has no pin."""
    validate_design(design)
    api = SchematicApi.open(out_path, create=True, overwrite=overwrite, mode="headless",
                            session=session)
    try:
        return _build(api, design)
    finally:
        api.close()
