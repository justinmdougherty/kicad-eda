#!/usr/bin/env python3
"""IPC-2221 design-rule engine: turn per-net electrical intent (working voltage,
current) plus a reliability class (IPC-2221 / IPC-A-610 Class 1/2/3) into the
geometric rules the copper must obey — conductor spacing, conductor width, and a
class-driven floor for clearance / track / annular ring / drill.

Pure functions, no KiCad needed. Verify against the standard:
  - clearance: IPC-2221B Table 6-1 (B1 internal, B2 external uncoated <=3050 m,
    B4 external with permanent polymer coating). >500 V uses the per-volt slope.
  - width: IPC-2221 conductor-current chart, A = (I / (k * dT^0.44))^(1/0.725),
    k_external = 0.048, k_internal = 0.024; width = A / copper_thickness.

This is intentionally conservative (the >3050 m and coated columns are stricter);
callers pass the column that matches the board's coating/altitude.
"""
from __future__ import annotations

import math

# --- IPC-2221B Table 6-1: minimum conductor spacing (mm) by peak working voltage.
# Each row: (V_upper_inclusive, B1_internal, B2_external_uncoated, B4_external_coated)
_CLEARANCE_TABLE = [
    (15,   0.05, 0.10, 0.05),
    (30,   0.05, 0.10, 0.05),
    (50,   0.10, 0.60, 0.13),
    (100,  0.10, 0.60, 0.13),
    (150,  0.20, 0.60, 0.40),
    (170,  0.20, 1.25, 0.40),
    (250,  0.20, 1.25, 0.40),
    (300,  0.20, 1.25, 0.40),
    (500,  0.25, 2.50, 0.80),
]
# >500 V: base value at 500 V plus a per-volt slope (mm per volt over 500).
_CLEARANCE_OVER_500 = {"B1": (0.25, 0.0025), "B2": (2.50, 0.005), "B4": (0.80, 0.00305)}
_COL = {"B1": 1, "B2": 2, "B4": 3}

# copper foil thickness (mils) by weight (oz/ft^2)
_COPPER_MILS = {0.5: 0.68, 1.0: 1.37, 2.0: 2.74, 3.0: 4.11}

# IPC-2221 / IPC-6012 reliability-class floors (mm). Class 3 = high-reliability
# (defense/aerospace/medical): larger annular ring, tighter process tolerance.
CLASS_PROFILES = {
    1: {"min_clearance": 0.10, "min_track": 0.15, "min_annular_ring": 0.05,
        "min_drill": 0.20, "edge_clearance": 0.25, "name": "Class 1 (general)"},
    2: {"min_clearance": 0.13, "min_track": 0.15, "min_annular_ring": 0.10,
        "min_drill": 0.25, "edge_clearance": 0.30, "name": "Class 2 (dedicated service)"},
    3: {"min_clearance": 0.20, "min_track": 0.20, "min_annular_ring": 0.15,
        "min_drill": 0.30, "edge_clearance": 0.50, "name": "Class 3 (high reliability)"},
}


def clearance_mm(voltage_peak: float, column: str = "B2") -> float:
    """Minimum conductor spacing for a peak working voltage. `column` selects the
    Table 6-1 case: B1 internal, B2 external uncoated (default, worst common case),
    B4 external permanently coated."""
    v = abs(voltage_peak)
    idx = _COL[column]
    if v <= 500:
        # the last row's bound IS 500, so the loop always returns here
        for vmax, *cols in _CLEARANCE_TABLE:
            if v <= vmax:
                return cols[idx - 1]
    base, slope = _CLEARANCE_OVER_500[column]
    return round(base + (v - 500) * slope, 4)


def trace_width_mm(current_a: float, temp_rise_c: float = 10.0,
                   copper_oz: float = 1.0, layer: str = "external") -> float:
    """Minimum conductor width to carry `current_a` at `temp_rise_c` (deg C rise),
    per the IPC-2221 chart. Internal layers derate (k halved)."""
    if current_a <= 0:
        return 0.0
    k = 0.048 if layer == "external" else 0.024
    area_mils2 = (current_a / (k * (temp_rise_c ** 0.44))) ** (1.0 / 0.725)
    thickness_mils = _COPPER_MILS.get(copper_oz, 1.37)
    width_mils = area_mils2 / thickness_mils
    return round(width_mils * 0.0254, 4)  # mils -> mm


def via_ampacity_a(drill_mm: float, plating_um: float = 25.0,
                   temp_rise_c: float = 10.0) -> float:
    """Current one plated via barrel can carry. The barrel is treated as an
    internal conductor (IPC-2221 k=0.024 — no convective cooling inside the
    board) whose cross-section is the plating annulus: pi * drill * plating.
    25 um plating is the IPC-6012 Class 2/3 average barrel thickness."""
    if drill_mm <= 0:
        return 0.0
    drill_mils = drill_mm / 0.0254
    plating_mils = plating_um / 25.4
    area_mils2 = math.pi * drill_mils * plating_mils
    return round(0.024 * (temp_rise_c ** 0.44) * (area_mils2 ** 0.725), 3)


def vias_for_current(current_a: float, drill_mm: float, plating_um: float = 25.0,
                     temp_rise_c: float = 10.0) -> int:
    """How many parallel vias a layer transition needs to carry `current_a`.
    Conservative: assumes equal current sharing (valid when the vias sit in a
    tight cluster on the same copper)."""
    if current_a <= 0:
        return 1
    cap = via_ampacity_a(drill_mm, plating_um, temp_rise_c)
    if cap <= 0:
        return 1
    return max(1, math.ceil(current_a / cap))


def net_rule(voltage_peak: float = 3.3, current_a: float = 0.0,
             cls: int = 2, column: str = "B2", copper_oz: float = 1.0,
             temp_rise_c: float = 10.0) -> dict:
    """Resolve one net's geometry: clearance = max(IPC-2221 voltage spacing, class
    floor); width = max(IPC-2221 current width, class floor)."""
    prof = CLASS_PROFILES[cls]
    clr = max(clearance_mm(voltage_peak, column), prof["min_clearance"])
    w = max(trace_width_mm(current_a, temp_rise_c, copper_oz, "external"),
            prof["min_track"])
    return {"clearance": round(clr, 3), "track_width": round(w, 3),
            "voltage": voltage_peak, "current": current_a}


def resolve_netclasses(design: dict, cls: int = 2, copper_oz: float = 1.0,
                       temp_rise_c: float = 10.0) -> dict:
    """Build per-net rules from a netlist whose nets MAY carry electrical intent.
    A net is annotated by adding the design key "net_specs": {netname: {"voltage":
    V, "current": A}}. Unannotated nets default to 3.3 V / signal current and fall
    back to the class floor. Returns {netname: net_rule(...)} plus a 'default'."""
    specs = design.get("net_specs", {})
    out = {"_default": net_rule(3.3, 0.0, cls, copper_oz=copper_oz,
                                temp_rise_c=temp_rise_c),
           "_class": CLASS_PROFILES[cls]["name"]}
    for net in design.get("nets", {}):
        s = specs.get(net, {})
        out[net] = net_rule(s.get("voltage", 3.3), s.get("current", 0.0),
                            cls, copper_oz=copper_oz, temp_rise_c=temp_rise_c)
    return out


if __name__ == "__main__":
    import json
    import sys
    if len(sys.argv) > 1:  # lint a netlist's resolved rules
        design = json.load(open(sys.argv[1], encoding="utf-8"))
        cls = int(sys.argv[2]) if len(sys.argv) > 2 else 3
        print(json.dumps(resolve_netclasses(design, cls), indent=2))
    else:  # self-test table
        print("IPC-2221 clearance (B2 external uncoated):")
        for v in (5, 12, 24, 48, 100, 250, 400, 600):
            print(f"  {v:>4} V -> {clearance_mm(v):.3f} mm")
        print("IPC-2221 trace width (external, 1oz, dT=10C):")
        for i in (0.1, 0.5, 1.0, 2.0, 3.0):
            print(f"  {i:>4} A -> {trace_width_mm(i):.3f} mm")
        print("Via ampacity (25um plating, dT=10C) and count for 5A:")
        for d in (0.3, 0.4, 0.5, 0.6, 0.8):
            print(f"  drill {d} mm -> {via_ampacity_a(d):.2f} A "
                  f"({vias_for_current(5.0, d)} vias for 5 A)")
        print("Class floors:")
        for c in (1, 2, 3):
            p = CLASS_PROFILES[c]
            print(f"  Class {c}: clr>={p['min_clearance']} track>={p['min_track']} "
                  f"ring>={p['min_annular_ring']} drill>={p['min_drill']}")
