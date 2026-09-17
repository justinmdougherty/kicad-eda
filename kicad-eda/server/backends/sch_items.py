"""kipy item factories and readers for the schematic API backend.

Everything that touches a kipy wrapper type lives here so ``sch_api`` reads as
schematic operations. Factories take millimetres and build nanometre protos
(rounded, never truncated); readers turn instances/pins/labels into plain
JSON-friendly dicts.
"""
from __future__ import annotations

from backends import kipy_bridge as kb
from backends import sch_geometry as geo

COMMON_UNITS = (None, 0)          # a pin shared by every unit of a symbol
DESCRIPTION_FIELD = "Description"
_LABEL_KINDS = {"LocalLabel": "local", "GlobalLabel": "global",
                "HierarchicalLabel": "hierarchical", "DirectiveLabel": "directive"}


# --- update round-trip -------------------------------------------------------
def prime_definition(inst) -> None:
    """Fill the Description field KiCad 11 (10.99) leaves EMPTY in
    ``inst.definition`` from the instance's own Description field, right before
    an UpdateItems. KiCad writes the definition back into the embedded library
    symbol, so an empty field there blanks that symbol's description: ERC then
    reports lib_symbol_mismatch on every updated symbol and a later placement
    of the same lib_id embeds a renamed copy ("R_1") instead of sharing it.
    Verified against kicad-cli 10.99 ERC at every severity. When KiCad reports
    the field itself the definition is left untouched."""
    field = inst.definition.description_field
    if field.text.value:
        return
    field.name = DESCRIPTION_FIELD
    field.text.value = inst.description_field.text.value
    field.visible = False
    field.allow_auto_place = True


# --- factories --------------------------------------------------------------
def _text(value: str):
    text = kb.ct.Text()
    text.value = value
    return text


def wire_item(x1: float, y1: float, x2: float, y2: float):
    wire = kb.st.SchematicLine()
    wire.start = kb.vec(x1, y1)
    wire.end = kb.vec(x2, y2)
    wire.type = kb.st.SchematicLineType.SLT_WIRE
    return wire


def label_item(net: str, x: float, y: float):
    label = kb.st.LocalLabel()
    label.position = kb.vec(x, y)
    label.text = _text(net)
    return label


def global_label_item(net: str, x: float, y: float, shape):
    label = kb.st.GlobalLabel()
    label.position = kb.vec(x, y)
    label.text = _text(net)
    label.shape = shape
    return label


def junction_item(x: float, y: float):
    junction = kb.st.Junction()
    junction.position = kb.vec(x, y)
    return junction


def no_connect_item(x: float, y: float):
    marker = kb.st.NoConnectMarker()
    marker.position = kb.vec(x, y)
    return marker


# --- readers ----------------------------------------------------------------
def instance_transform(inst) -> tuple[int, bool, bool]:
    """(degrees, mirror_x, mirror_y) as KiCad reports them (SSO_UNKNOWN -> 0)."""
    tr = inst.transform
    return kb.ORIENT_TO_DEG.get(tr.orientation, 0), bool(tr.mirror_x), bool(tr.mirror_y)


def pins_with_units(inst) -> list[tuple]:
    """[(SchematicPin, unit | None)] of the instance's symbol definition; the
    definition lists every unit's pins, not only the placed unit's."""
    return [(child.item, child.unit) for child in inst.definition.items
            if child.kind == "SchematicPin"]


def owns_pin(inst, unit) -> bool:
    return unit in COMMON_UNITS or unit == inst.unit


def own_pins(inst) -> list:
    return [pin for pin, unit in pins_with_units(inst) if owns_pin(inst, unit)]


def units_with_pins(inst) -> list[int]:
    """Units that carry pins (common-only symbols count as unit 1)."""
    return sorted({unit for _pin, unit in pins_with_units(inst) if unit}) or [1]


def pin_world(inst, pin) -> tuple[float, float]:
    """Absolute sheet position (mm) of a definition pin under the instance's
    read-back transform; exact integer nm arithmetic underneath."""
    deg, mirror_x, mirror_y = instance_transform(inst)
    x_nm, y_nm = geo.pin_world_nm((inst.position.x, inst.position.y),
                                  (pin.position.x, pin.position.y), deg, mirror_x, mirror_y)
    return geo.to_mm(x_nm), geo.to_mm(y_nm)


def pin_type_name(pin) -> str:
    return kb.PIN_TYPE_NAMES.get(pin.electrical_type, "unknown")


def lib_id_of(inst) -> str:
    ident = inst.definition.id
    return f"{ident.library}:{ident.name}"


def pin_dict(inst, pin) -> dict:
    x, y = pin_world(inst, pin)
    return {"number": pin.number, "name": pin.name, "x_mm": x, "y_mm": y,
            "type": pin_type_name(pin)}


def pin_info(inst) -> list[dict]:
    return [{"number": pin.number, "name": pin.name, "unit": unit, "type": pin_type_name(pin)}
            for pin, unit in pins_with_units(inst)]


def symbol_dict(inst, with_pins: bool = False) -> dict:
    deg, mirror_x, mirror_y = instance_transform(inst)
    out = {"ref": inst.reference, "lib_id": lib_id_of(inst), "value": inst.value,
           "footprint": inst.footprint_field.text.value, "unit": inst.unit,
           "unit_count": inst.definition.unit_count, "units": units_with_pins(inst),
           "at": [geo.to_mm(inst.position.x), geo.to_mm(inst.position.y), deg],
           "mirror": "x" if mirror_x else "y" if mirror_y else ""}
    if with_pins:
        out["pins"] = [pin_dict(inst, pin) for pin in own_pins(inst)]
    return out


def pin_map(instances) -> dict[str, tuple[str, str]]:
    """KIID string -> (reference, pin number) for every pin of every instance;
    GetSchematicNetlist reports pins by these ids."""
    return {pin.id.value: (inst.reference, pin.number)
            for inst in instances for pin, _unit in pins_with_units(inst)}


def label_dict(item) -> dict:
    kind = _LABEL_KINDS.get(type(item).__name__, type(item).__name__.lower())
    return {"net": item.text.value, "x_mm": geo.to_mm(item.position.x),
            "y_mm": geo.to_mm(item.position.y), "kind": kind}
