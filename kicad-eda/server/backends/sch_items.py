"""kipy item factories and readers for the schematic API backend.

Everything that touches a kipy wrapper type lives here so ``sch_api`` reads as
schematic operations. Factories take millimetres and build nanometre protos
(rounded, never truncated); readers turn instances/pins/labels into plain
JSON-friendly dicts.
"""
from __future__ import annotations

from collections.abc import Mapping

from backends import kipy_bridge as kb
from backends import sch_geometry as geo

SheetKey = tuple[str, ...]                 # a sheet path: its KIID strings, root first
PinKey = tuple[SheetKey, str]              # (sheet path, pin KIID)
PinRef = tuple[str, str]                   # (reference, pin number)
InstanceKey = tuple[SheetKey, str]         # (sheet path, symbol KIID)

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


# --- sheet paths and the netlist ----------------------------------------------
# A sheet file used several times is ONE screen: its symbols and their pins
# keep the same KIIDs in every instance, while each instance has its own sheet
# path and its own reference (demos/multichannel on 10.99: channel_strip four
# times, 80 pin KIIDs shared). A pin is therefore (sheet path, KIID).
def sheet_key(path) -> SheetKey:
    """A kipy SheetPath as a hashable tuple of KIID strings, root first."""
    return tuple(kiid.value for kiid in path.path)


def sheet_text(path) -> str:
    """'/CH1/ (/<root>/<sheet>)' when KiCad names the path, else the KIIDs."""
    ids = "/" + "/".join(sheet_key(path))
    human = getattr(path, "path_human_readable", "")
    return f"{human} ({ids})" if human else ids


def instance_sheet(inst) -> SheetKey:
    """The sheet path a symbol instance sits on. 10.99 reports the sheet's
    own path; one that ends with the symbol's KIID (a SheetPath may also name
    a symbol instance) is cut back to its sheet."""
    key = sheet_key(inst.path)
    return key[:-1] if key and key[-1] == inst.id.value else key


def instance_key(inst) -> InstanceKey:
    """(sheet path, symbol KIID): what tells the instances of a reused sheet apart."""
    return instance_sheet(inst), inst.id.value


def pin_map(instances) -> dict[PinKey, PinRef]:
    """(sheet path, pin KIID) -> (reference, pin number) for every pin of every
    instance; GetSchematicNetlist reports a net's pins by KIID per sheet path."""
    return {(instance_sheet(inst), pin.id.value): (inst.reference, pin.number)
            for inst in instances for pin, _unit in pins_with_units(inst)}


def kiid_index(pins: Mapping[PinKey, PinRef]) -> dict[str, frozenset[PinRef]]:
    """Pin KIID -> every (reference, pin number) it stands for, on any sheet."""
    found: dict[str, set[PinRef]] = {}
    for (_sheet, kiid), ref in pins.items():
        found.setdefault(kiid, set()).add(ref)
    return {kiid: frozenset(refs) for kiid, refs in found.items()}


def _net_pin(pins: Mapping[PinKey, PinRef], by_kiid: Mapping[str, frozenset[PinRef]],
             sheet: SheetKey, kiid: str) -> tuple[PinRef | None, frozenset[PinRef]]:
    """(pin, ambiguous): the pin at (sheet, KIID), else the one pin the KIID
    stands for in the whole schematic (a KiCad that reports no or another
    sheet path); a KIID standing for several pins gives (None, those pins)."""
    exact = pins.get((sheet, kiid))
    if exact is not None:
        return exact, frozenset()
    candidates = by_kiid.get(kiid, frozenset())
    if len(candidates) == 1:
        return next(iter(candidates)), frozenset()
    return None, candidates                      # empty: a wire, label, junction ...


def net_members(net, name: str, pins: Mapping[PinKey, PinRef],
                by_kiid: Mapping[str, frozenset[PinRef]]) -> tuple[list[list[str]], list[str]]:
    """([[ref, pin], ...] sorted and unique, warnings) for one SchematicNet.
    Non-pin items are skipped; a pin KIID with no instance on the net's sheet
    that stands for several pins elsewhere is left out with a warning, never
    guessed."""
    members: set[PinRef] = set()
    warnings: list[str] = []
    for sheet in net.sheets:
        where = sheet_key(sheet.path)
        for kiid in (item.value for item in sheet.items):
            pin, candidates = _net_pin(pins, by_kiid, where, kiid)
            if pin is not None:
                members.add(pin)
            elif candidates:
                listed = ", ".join(f"{ref}.{number}" for ref, number in sorted(candidates))
                warnings.append(f"net {name!r}: pin KIID {kiid} on sheet {sheet_text(sheet.path)} "
                                f"has no symbol instance on that sheet and stands for "
                                f"{len(candidates)} pins elsewhere ({listed}); left out")
    return sorted(list(pin) for pin in members), warnings


def label_dict(item) -> dict:
    kind = _LABEL_KINDS.get(type(item).__name__, type(item).__name__.lower())
    return {"net": item.text.value, "x_mm": geo.to_mm(item.position.x),
            "y_mm": geo.to_mm(item.position.y), "kind": kind}
