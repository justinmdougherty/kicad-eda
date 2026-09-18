"""kipy item readers and factories for the board API backend.

Everything that touches a kipy board wrapper type lives here so ``pcb_api``
reads as board operations. Readers turn Pad / Track / ArcTrack / Via / Zone /
FootprintInstance / edge shapes into plain JSON-friendly dicts (millimetres
rounded to 4 decimals, ids as the KIID string); factories take millimetres
and build nanometre protos (rounded, never truncated). Nothing here talks to
KiCad: every function is pure over the objects it is given, so the unit tier
drives it with hand-built fakes that carry the same attributes.

Verified against the KiCad 10.99 (11 nightly) headless server: a through-hole
padstack lists ALL 32 inner copper layers KiCad knows (enabled or not), so a
through-hole pad reports the board's enabled copper layers instead; an SMD
padstack's layer set holds its copper layer plus mask/paste. Enabled layers
come back in enum order (F, B, In1, ...), so ``copper_layer_names`` re-sorts
them top to bottom.
"""
from __future__ import annotations

import math
from typing import Any, Iterable, Sequence

from backends import kipy_bridge as kb

MM_DECIMALS = 4
NM_PER_MM = 1_000_000
UNKNOWN = "unknown"
EXAMPLE_COPPER = ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]
CLASS_FIELDS = ("clearance", "track_width", "via_diameter", "via_drill")
# rules() key -> kipy MinimumConstraints attribute
RULE_FIELDS = {"min_clearance": "min_clearance", "min_track_width": "min_track_width",
               "min_via_diameter": "min_via_size", "min_via_drill": "min_through_drill",
               "min_via_annular": "min_via_annular_width",
               "copper_edge_clearance": "copper_edge_clearance",
               "hole_clearance": "hole_clearance", "hole_to_hole": "hole_to_hole_min"}
_SHAPE_NAMES = {"PSS_CIRCLE": "circle", "PSS_RECTANGLE": "rect", "PSS_OVAL": "oval",
                "PSS_TRAPEZOID": "trapezoid", "PSS_ROUNDRECT": "roundrect",
                "PSS_CHAMFEREDRECT": "chamfered", "PSS_CUSTOM": "custom"}
Point = tuple[float, float]


# --- units --------------------------------------------------------------------
def mm(value_nm: int | float) -> float:
    """Nanometres -> millimetres, rounded to MM_DECIMALS."""
    return round(float(value_nm) / NM_PER_MM, MM_DECIMALS)


def nm(value_mm: float) -> int:
    """Millimetres -> nanometres, rounded (kipy's from_mm truncates: 0.35 mm
    would become 349999 nm)."""
    return int(round(float(value_mm) * NM_PER_MM))


def point(vector) -> list[float]:
    return [mm(vector.x), mm(vector.y)]


def box(box2) -> list[float]:
    """[x0, y0, x1, y1] mm of a kipy Box2 (pos + size)."""
    x0, y0 = box2.pos.x, box2.pos.y
    return [mm(x0), mm(y0), mm(x0 + box2.size.x), mm(y0 + box2.size.y)]


def item_id(item) -> str:
    return str(item.id.value)


def net_name(net) -> str:
    """The net's name; '' for no net (Zone.net can be None)."""
    return str(getattr(net, "name", "") or "")


# --- layers -------------------------------------------------------------------
def layer_name(value: int) -> str:
    """'F.Cu' for BoardLayer.BL_F_Cu: KiCad's canonical name, falling back to
    the enum name with ``BL_`` stripped ('Edge.Cuts') for layers the
    canonical table lacks."""
    name = kb.board_layers.canonical_name(value)
    if name != "Unknown":
        return name
    return kb.BoardLayer.Name(value)[3:].replace("_", ".")


def layer_value(name: str) -> int:
    """BoardLayer value for 'F.Cu' / 'In1.Cu' / 'B.Cu' / 'Edge.Cuts'; a name
    KiCad does not know raises ValueError naming the copper layer scheme."""
    key = str(name)
    value = kb.board_layers.layer_from_canonical_name(key)
    if value == kb.BoardLayer.BL_UNKNOWN:
        try:
            value = kb.BoardLayer.Value("BL_" + key.replace(".", "_"))
        except ValueError:
            value = kb.BoardLayer.BL_UNKNOWN
    if value in (kb.BoardLayer.BL_UNKNOWN, kb.BoardLayer.BL_UNDEFINED):
        raise ValueError(f"unknown layer {name!r}; copper layers are named like {EXAMPLE_COPPER}")
    return value


def copper_layer_names(enabled: Iterable[int]) -> list[str]:
    """Enabled copper layers top to bottom ('F.Cu', 'In1.Cu', ..., 'B.Cu').
    KiCad reports enabled layers in enum order (F, B, In1, ...); this sorts
    them by stackup position."""
    order = {layer: index for index, layer in enumerate(kb.board_layers.COPPER_LAYERS)}
    copper = sorted((layer for layer in enabled if layer in order), key=order.__getitem__)
    return [layer_name(layer) for layer in copper]


# --- pads ---------------------------------------------------------------------
def is_through_hole(pad) -> bool:
    return pad.pad_type in (kb.PadType.PT_PTH, kb.PadType.PT_NPTH)


def pad_layers(pad, copper: Sequence[str]) -> list[str]:
    """Copper layers the pad has copper on: every enabled copper layer for a
    through-hole pad, the copper members of its layer set for an SMD pad
    (falling back to its padstack's copper entries)."""
    if is_through_hole(pad):
        return list(copper)
    stack = pad.padstack
    on_layers = [layer_name(layer) for layer in stack.layers
                 if kb.board_layers.is_copper_layer(layer)]
    if on_layers:
        return on_layers
    return [layer_name(entry.layer) for entry in stack.copper_layers]


def _first_copper(padstack) -> Any | None:
    """The padstack's first copper entry (PadStackLayer), None when it has none."""
    entries = list(padstack.copper_layers)
    return entries[0] if entries else None


def pad_shape(pad) -> str:
    """'rect' | 'circle' | 'oval' | 'roundrect' | 'chamfered' | 'trapezoid' |
    'custom' | 'unknown' from the padstack's first copper entry."""
    first = _first_copper(pad.padstack)
    if first is None:
        return UNKNOWN
    try:
        return _SHAPE_NAMES.get(kb.PadStackShape.Name(first.shape), UNKNOWN)
    except ValueError:
        return UNKNOWN


def pad_dict(pad, refs: dict[str, str], copper: Sequence[str]) -> dict:
    """{id, ref, pad, net, x, y, layers, through_hole, shape, w, h, rotation,
    drill}; ``refs`` maps footprint KIID -> reference (pads carry only the
    parent's id) and, for a KiCad before 10.0.6 whose pads report no parent,
    pad KIID -> reference; ``copper`` is the board's enabled copper layers."""
    first = _first_copper(pad.padstack)
    through = is_through_hole(pad)
    parent = pad.parent
    owner = str(parent.value) if parent is not None else item_id(pad)
    return {"id": item_id(pad), "ref": refs.get(owner, ""),
            "pad": str(pad.number), "net": net_name(pad.net),
            "x": mm(pad.position.x), "y": mm(pad.position.y),
            "layers": pad_layers(pad, copper), "through_hole": through,
            "shape": pad_shape(pad),
            "w": mm(first.size.x) if first is not None else 0.0,
            "h": mm(first.size.y) if first is not None else 0.0,
            "rotation": round(float(pad.padstack.angle.degrees), MM_DECIMALS),
            "drill": mm(pad.padstack.drill.diameter.x) if through else None}


# --- copper -------------------------------------------------------------------
def track_dict(track) -> dict:
    """{id, net, layer, start, end, width, length} (+ arc: true, mid for an
    ArcTrack, whose length KiCad's wrapper computes)."""
    out = {"id": item_id(track), "net": net_name(track.net), "layer": layer_name(track.layer),
           "start": point(track.start), "end": point(track.end), "width": mm(track.width)}
    if hasattr(track, "mid"):
        return {**out, "arc": True, "mid": point(track.mid), "length": mm(track.length())}
    straight = math.hypot(track.end.x - track.start.x, track.end.y - track.start.y)
    return {**out, "length": mm(straight)}


def via_dict(via) -> dict:
    return {"id": item_id(via), "net": net_name(via.net),
            "x": mm(via.position.x), "y": mm(via.position.y),
            "diameter": mm(via.diameter), "drill": mm(via.drill_diameter)}


def zone_dict(zone) -> dict:
    """{id, name, net, layers, filled, rule_area, bbox}; ``is_rule_area`` is a
    method on the vendored kipy (an attribute on older ones)."""
    rule_area = zone.is_rule_area
    if callable(rule_area):
        rule_area = rule_area()
    return {"id": item_id(zone), "name": str(zone.name), "net": net_name(zone.net),
            "layers": [layer_name(layer) for layer in zone.layers],
            "filled": bool(zone.filled), "rule_area": bool(rule_area),
            "bbox": box(zone.bounding_box())}


def footprint_dict(footprint, bbox, pad_count: int) -> dict:
    """{id, ref, value, x, y, rot, side, bbox, pads}; ``bbox`` is KiCad's
    Box2 for the footprint (None when KiCad has none)."""
    return {"id": item_id(footprint),
            "ref": str(footprint.reference_field.text.value),
            "value": str(footprint.value_field.text.value),
            "x": mm(footprint.position.x), "y": mm(footprint.position.y),
            "rot": round(float(footprint.orientation.degrees), MM_DECIMALS),
            "side": "B" if footprint.layer == kb.BoardLayer.BL_B_Cu else "F",
            "bbox": box(bbox) if bbox is not None else None, "pads": int(pad_count)}


def _control_polygon_box(shape) -> list[float] | None:
    """[x0, y0, x1, y1] mm spanned by a bezier's start / control1 / control2 /
    end (the curve lies inside its control polygon); None for another shape."""
    points = [getattr(shape, name, None) for name in ("start", "control1", "control2", "end")]
    if any(p is None for p in points):
        return None
    xs, ys = [p.x for p in points], [p.y for p in points]
    return [mm(min(xs)), mm(min(ys)), mm(max(xs)), mm(max(ys))]


def _shape_box(shape) -> list[float] | None:
    """The shape's box in mm, or None when this kipy cannot box it: kipy's
    Bezier.bounding_box raises NotImplementedError (its control polygon is
    used instead) and a geometry kipy has no wrapper for (ellipse, ellipse
    arc) arrives as a bare BoardShape without bounding_box."""
    try:
        return box(shape.bounding_box())
    except NotImplementedError:
        return _control_polygon_box(shape)
    except AttributeError:
        return None


def outline(shapes: Iterable) -> dict:
    """{bbox, width, height, segments} of the Edge.Cuts shapes (arcs, circles
    and polygons enter through their bounding boxes, a bezier through its
    control polygon); bbox None without any. A shape this kipy cannot box
    (an ellipse) is left out and counted in ``skipped``, so a DXF-imported
    outline still reports the rest instead of failing the board."""
    edges = [shape for shape in shapes if shape.layer == kb.BoardLayer.BL_Edge_Cuts]
    if not edges:
        return {"bbox": None, "width": 0.0, "height": 0.0, "segments": 0}
    boxes = [b for b in (_shape_box(shape) for shape in edges) if b is not None]
    skipped = len(edges) - len(boxes)
    if not boxes:
        out = {"bbox": None, "width": 0.0, "height": 0.0, "segments": len(edges)}
    else:
        x0, y0 = min(b[0] for b in boxes), min(b[1] for b in boxes)
        x1, y1 = max(b[2] for b in boxes), max(b[3] for b in boxes)
        out = {"bbox": [x0, y0, x1, y1], "width": round(x1 - x0, MM_DECIMALS),
               "height": round(y1 - y0, MM_DECIMALS), "segments": len(edges)}
    return {**out, "skipped": skipped} if skipped else out


# --- rules / classes ----------------------------------------------------------
def rules_dict(constraints) -> dict:
    """Board minimums in mm keyed as rules(); None for a field this kipy /
    KiCad does not report."""
    out: dict[str, float | None] = {}
    for key, attr in RULE_FIELDS.items():
        value = getattr(constraints, attr, None)
        out[key] = mm(value) if value is not None else None
    return out


def empty_rules() -> dict:
    return {key: None for key in RULE_FIELDS}


def netclass_dict(netclass) -> dict:
    """{name, clearance, track_width, via_diameter, via_drill} in mm; a field
    KiCad reports as unset stays None (pcb_api fills it from the project)."""
    out: dict = {"name": str(getattr(netclass, "name", "") or "")}
    for field in CLASS_FIELDS:
        value = getattr(netclass, field, None)
        out[field] = mm(value) if value is not None else None
    return out


# --- factories ----------------------------------------------------------------
def track_item(net, layer: int, start: Point, end: Point, width_mm: float) -> kb.Track:
    """A kipy Track on ``net`` (a Net from get_nets) and ``layer`` (BoardLayer value)."""
    track = kb.Track()
    track.start = kb.vec(*start)
    track.end = kb.vec(*end)
    track.width = nm(width_mm)
    track.layer = layer
    track.net = net
    return track


def via_item(net, x_mm: float, y_mm: float, diameter_mm: float, drill_mm: float) -> kb.Via:
    """A through via on ``net`` (a Net from get_nets)."""
    via = kb.Via()
    via.position = kb.vec(x_mm, y_mm)
    via.diameter = nm(diameter_mm)
    via.drill_diameter = nm(drill_mm)
    via.net = net
    return via
