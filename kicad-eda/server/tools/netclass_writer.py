#!/usr/bin/env python3
"""Inject IPC-2221 / reliability-class net classes into a KiCad project file.

The board generator pours copper and assigns pads, but every net then routes at
the single "Default" width. That is wrong for a real board: a power rail carrying
amps needs a conductor sized by IPC-2221, and a high-voltage net needs the
IPC-2221 Table 6-1 spacing. This module resolves each net's electrical intent
(via `ipc_rules`) into a concrete (track_width, clearance, via) rule and writes it
into the `.kicad_pro` `net_settings` as named net classes plus the patterns that
bind nets to them.

Why the project file and not the board: `pcbnew.LoadBoard` reads the sibling
`.kicad_pro`, so the classes written here reach BOTH consumers of the design with
no further work — kicad-cli DRC (which reads `board.design_settings.rules` +
`net_settings`) and the Specctra DSN export (which `LoadBoard`s the board, so
`ExportSpecctraDSN` emits per-class `(rule (width ...) (clearance ...))` that
Freerouting honors, routing power copper visibly wider).

Pure standard-library; no KiCad/pcbnew needed. Idempotent — re-running replaces
the classes it manages and leaves "Default" and any hand-authored class intact.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import ipc_rules
import impedance

# net classes this module owns (so a re-run replaces, never duplicates). Default
# is bumped in place; everything we add carries this prefix in its name.
_MANAGED_PREFIX = "NC_"


def _class_name(net: str) -> str:
    """A KiCad-safe, human-readable class name derived from a net name:
    '+3V3' -> 'NC_P3V3', 'VBUS_5V' -> 'NC_VBUS_5V'."""
    s = net.replace("+", "P").replace("-", "N")
    s = re.sub(r"[^0-9A-Za-z_]", "_", s)
    return f"{_MANAGED_PREFIX}{s}"


def _via_for(width: float, current: float, floor_via_d: float,
             floor_via_drill: float) -> tuple[float, float, float, int]:
    """Size a via for a net: geometric (via shouldn't bottleneck the trace) AND
    thermal (IPC-2221 barrel ampacity). Grows the drill until one barrel carries
    the net current or the drill hits 0.8 mm; beyond that the answer is a via
    CLUSTER, reported as vias_needed for the layout gate to verify.
    Returns (diameter, drill, single_via_ampacity_a, vias_needed)."""
    via_d, via_drill = floor_via_d, floor_via_drill
    if width > 0.30:
        via_d = round(min(1.2, max(floor_via_d, width + 0.30)), 2)
        via_drill = round(max(floor_via_drill, via_d - 0.30), 2)
    while (current > 0 and ipc_rules.via_ampacity_a(via_drill) < current
           and via_drill < 0.80):
        via_drill = round(via_drill + 0.10, 2)
        via_d = round(max(via_d, via_drill + 0.30), 2)
    amp = ipc_rules.via_ampacity_a(via_drill)
    needed = ipc_rules.vias_for_current(current, via_drill)
    return via_d, via_drill, amp, needed


def build_classes(design: dict, cls: int = 3, copper_oz: float = 1.0,
                  temp_rise_c: float = 10.0, template: dict | None = None,
                  plane_nets: tuple = ()) -> dict:
    """Resolve the netlist into KiCad net-class dicts + binding patterns.

    Returns {"classes": [class_dict, ...], "patterns": [{netclass, pattern}, ...],
    "default": {track_width, clearance}, "summary": {net: rule}}. Only nets whose
    IPC-resolved width or clearance EXCEEDS the class floor get a dedicated class;
    everything else stays on the floor-sized Default (no clutter)."""
    rules = ipc_rules.resolve_netclasses(design, cls, copper_oz, temp_rise_c)
    imp = impedance.resolve_impedance(design)   # {net: controlled-Z geometry}
    floor = ipc_rules.CLASS_PROFILES[cls]
    base = dict(template or {})
    fl_track = round(floor["min_track"], 3)
    fl_clear = round(floor["min_clearance"], 3)
    fl_via_d = float(base.get("via_diameter", 0.6))
    fl_via_drill = float(base.get("via_drill", 0.3))

    classes, patterns, summary, warnings = [], [], {}, []
    for net, rule in rules.items():
        if net.startswith("_"):
            continue
        # a net with a dedicated copper PLANE carries its current in the
        # plane; every surface segment is a short pad-to-via stub. Forcing
        # the IPC width onto those stubs (on the highest-fanout nets, no
        # less) starves the router for nothing — the plane IS the conductor.
        # Via ampacity into the plane is still enforced by the G4 gate.
        if net in plane_nets:
            summary[net] = {"plane": True, "voltage": rule["voltage"],
                            "current": rule["current"]}
            continue
        w = max(round(rule["track_width"], 3), fl_track)
        clr = max(round(rule["clearance"], 3), fl_clear)
        entry = {"track_width": w, "clearance": clr,
                 "voltage": rule["voltage"], "current": rule["current"]}

        # controlled impedance overrides current-sizing for this net: the trace
        # MUST be the impedance width (a wider current rule would detune it). Such
        # a net ALWAYS gets its own class even if the width is at the floor.
        zi = imp.get(net)
        forced = False
        dp_w = dp_g = None
        if zi:
            forced = True
            w = round(zi["track_width"], 3)
            entry["impedance"] = zi
            if zi["mode"] == "diff":
                dp_w, dp_g = round(zi["diff_pair_width"], 3), round(zi["diff_pair_gap"], 3)
            if w < fl_track:
                warnings.append(
                    f"{net}: {zi.get('z0', zi.get('zdiff'))}-ohm width {w}mm is below the "
                    f"Class floor {fl_track}mm — thin the dielectric or relax the class")
        summary[net] = entry

        # nets that neither exceed the floor nor demand impedance stay on Default
        if not forced and w <= fl_track and clr <= fl_clear:
            continue
        via_d, via_drill, via_amp, vias_needed = _via_for(
            w, rule["current"], fl_via_d, fl_via_drill)
        entry["via"] = {"diameter": via_d, "drill": via_drill,
                        "ampacity_a": via_amp, "vias_needed": vias_needed}
        if vias_needed > 1:
            warnings.append(
                f"{net}: {rule['current']} A exceeds one {via_drill} mm via barrel "
                f"({via_amp} A at 10C rise) — every layer transition on this net "
                f"needs {vias_needed} parallel vias (or keep it on one layer)")
        c = dict(base)
        c.update({"name": _class_name(net), "track_width": w, "clearance": clr,
                  "via_diameter": via_d, "via_drill": via_drill,
                  "diff_pair_width": dp_w if dp_w else w, "priority": len(classes)})
        if dp_g is not None:
            c["diff_pair_gap"] = dp_g
        classes.append(c)
        patterns.append({"netclass": _class_name(net), "pattern": net})

    return {"classes": classes, "patterns": patterns,
            "default": {"track_width": fl_track, "clearance": fl_clear},
            "summary": summary, "class_name": floor["name"], "warnings": warnings}


def apply_to_project(pro_path: str, design: dict, cls: int = 3,
                     copper_oz: float = 1.0, temp_rise_c: float = 10.0,
                     plane_nets: tuple = ()) -> dict:
    """Write the resolved net classes into a `.kicad_pro`. Bumps Default to the
    class floor, replaces any previously-managed classes/patterns, preserves
    hand-authored ones, and tightens the board rule floor to match. Returns a
    report (classes written, per-net summary)."""
    p = Path(pro_path)
    if not p.is_file():
        return {"ok": False, "reason": f"project file not found: {pro_path}"}
    proj = json.loads(p.read_text(encoding="utf-8"))
    ns = proj.setdefault("net_settings", {})
    existing = ns.get("classes") or []
    default_tmpl = next((c for c in existing if c.get("name") == "Default"),
                        existing[0] if existing else {})

    built = build_classes(design, cls, copper_oz, temp_rise_c, default_tmpl,
                          plane_nets=plane_nets)

    # rebuild the class list: Default (floored) + hand-authored + ours
    kept = [c for c in existing
            if not str(c.get("name", "")).startswith(_MANAGED_PREFIX)]
    for c in kept:
        if c.get("name") == "Default":
            c["track_width"] = max(c.get("track_width", 0), built["default"]["track_width"])
            c["clearance"] = max(c.get("clearance", 0), built["default"]["clearance"])
    ns["classes"] = kept + built["classes"]

    # rebuild patterns: drop ours, keep hand-authored, append ours
    ours = {c["name"] for c in built["classes"]}
    hand = [pt for pt in (ns.get("netclass_patterns") or [])
            if not str(pt.get("netclass", "")).startswith(_MANAGED_PREFIX)]
    ns["netclass_patterns"] = hand + built["patterns"]

    # Keep the DRC rule floor at or below the tightest class so DRC never fights a
    # legitimately-narrow class width (it still enforces the per-class minimum).
    # IMPORTANT: do NOT raise the mechanical floors (edge clearance, drill, annular)
    # to the reliability-class target here — those are fab-CAPABILITY limits, and
    # forcing Class-3 reliability margins as hard DRC minimums shrinks the routable
    # area and breaks otherwise-routable boards. The class reliability targets are
    # reported as advisory by design_report; the board keeps the fab's real floors.
    rules = proj.setdefault("board", {}).setdefault("design_settings", {}).setdefault("rules", {})
    floor = ipc_rules.CLASS_PROFILES[cls]
    rules["min_track_width"] = min(rules.get("min_track_width", 1.0), floor["min_track"])
    rules["min_clearance"] = min(rules.get("min_clearance", 1.0), floor["min_clearance"])

    p.write_text(json.dumps(proj, indent=2), encoding="utf-8")
    return {"ok": True, "project": pro_path, "class_profile": built["class_name"],
            "classes_written": [c["name"] for c in built["classes"]],
            "default": built["default"], "summary": built["summary"],
            "warnings": built.get("warnings", [])}


if __name__ == "__main__":
    import sys
    netlist = sys.argv[1]
    pro = sys.argv[2] if len(sys.argv) > 2 else str(Path(netlist).with_suffix(".kicad_pro"))
    cls = int(sys.argv[3]) if len(sys.argv) > 3 else 3
    design = json.loads(Path(netlist).read_text(encoding="utf-8"))
    print(json.dumps(apply_to_project(pro, design, cls), indent=2))
