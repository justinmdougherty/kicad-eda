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

With the physical-design artifact (ADR D7.4, `<netlist stem>.stackup.json` from
physics_resolve; task D7-21) passed as `artifact` (the dict or its path):
  - plane nets come from the stackup roles: every plane- or mixed-role layer's net
    (plus any `plane_nets` the caller adds, e.g. prerouted nets); no zone strings;
  - an impedance net's class is its primary layer's line: track width, diff-pair
    width and gap (a broadside pair has no same-layer gap); a diff pair's partner
    (`diff_pair`) gets the same class;
  - its clearance is the `ground_gap` of a coplanar line, else the net clearance
    (ADR D7.6 'Realising the gap', corrected by D7-21's KiCad 9.0.4 measurements):
    KiCad pours at max(the track's class clearance, the pour net's class
    clearance), so the line's reference net (e.g. GND, a plane net otherwise left
    on Default) also gets a class whose clearance is the smallest coplanar gap next
    to it; every other pair still resolves to the larger class. A microstrip's
    `pour_clearance` is reported and warned about but never written as the class
    clearance: a class clearance also binds the pads of the parts on the line (an
    0402 DC block's pads are 0.40 mm apart, a SOT-89's 0.35 mm), which fails DRC;
    the RF<->GND DRU rule (ADR D7.8 R10) is where it belongs;
  - a ground gap below the fab's minimum clearance is a warning, and so is a ground
    gap below the net's IPC-2221 voltage spacing (the class clearance stays the gap:
    the line's impedance needs it); the board rule floor (min track / clearance) is
    lowered to the narrowest class value, never below the fab minimum, so DRC does
    not fight a correctly sized line;
  - current-sized widths use the stackup's outer copper weight (an explicit
    `copper_oz` wins) and ipc_rules.layer_warnings adds the inner-layer warnings;
  - a malformed, error-carrying or stale artifact (design digest) raises
    ArtifactError (apply_to_project returns ok False and leaves the file alone).
Without an artifact the classes are today's: impedance.resolve_impedance(design).

Pure standard-library (+ the repo's impedance module; the artifact-only modules
physics_resolve and dru_writer are imported only when an artifact is given); no
KiCad/pcbnew needed. Idempotent — re-running replaces the classes it manages
(prefix NC_) and leaves "Default" and any hand-authored class intact. The project
is written with LF line ends and a trailing newline.
"""
from __future__ import annotations

import json
import math
import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import fab_presets
import impedance
import ipc_rules

# net classes this module owns (so a re-run replaces, never duplicates). Default
# is bumped in place; everything we add carries this prefix in its name.
_MANAGED_PREFIX = "NC_"
ARTIFACT_SCHEMA = "kicad-eda/physical@1"   # = physics_resolve.SCHEMA (pinned by the tests)
POUR_ROLES = ("plane", "mixed")
SOURCE_DESIGN, SOURCE_ARTIFACT = "design", "artifact"
DEFAULT_COPPER_OZ = 1.0
DEFAULT_LAYERS = 2              # copper count when an artifact stackup names none
VOLTAGE_COLUMN = "B2"           # ipc_rules.resolve_netclasses' Table 6-1 column
_EPS = 1e-9


class ArtifactError(ValueError):
    """The physical-design artifact cannot give the net classes (the message says why)."""


def _classed(net: str) -> bool:
    """Whether a net may get a class at all ('_' nets are internal: never classed)."""
    return not net.startswith("_")


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


# ---------------------------------------------------------------- the artifact
def _positive(value: Any) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value > 0)


def _mm(value: Any) -> float | None:
    return round(float(value), 6) if _positive(value) else None


def plane_nets_from_artifact(artifact: Any) -> tuple[str, ...]:
    """The nets poured on the artifact stackup's plane- and mixed-role copper, top to
    bottom, each once; () without a resolved stackup."""
    stackup = artifact.get("stackup") if isinstance(artifact, Mapping) else None
    copper = stackup.get("copper") if isinstance(stackup, Mapping) else None
    nets = [c["net"] for c in copper if isinstance(c, Mapping) and c.get("role") in POUR_ROLES
            and isinstance(c.get("net"), str) and c["net"]] if isinstance(copper, list) else []
    return tuple(dict.fromkeys(nets))


def _read_artifact(path: str | os.PathLike[str]) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise ArtifactError(f"cannot read the artifact {path}: {exc}") from exc


def load_artifact(artifact: Any, design: Mapping[str, Any]) -> Mapping[str, Any]:
    """The artifact (a dict, or a path to <netlist stem>.stackup.json) once it is a
    resolved, fresh physical@1 artifact of `design`; ArtifactError otherwise."""
    import physics_resolve      # artifact path only (D7-13a); the design path never needs it
    doc = _read_artifact(artifact) if isinstance(artifact, (str, os.PathLike)) else artifact
    if not isinstance(doc, Mapping) or doc.get("schema") != ARTIFACT_SCHEMA:
        got = (f"schema {doc.get('schema')!r}" if isinstance(doc, Mapping)
               else type(doc).__name__)
        raise ArtifactError(f"the artifact must be a {ARTIFACT_SCHEMA} artifact "
                            f"(physics_resolve), got {got}")
    if not isinstance(doc.get("stackup"), Mapping):
        errors = "; ".join(str(e) for e in doc.get("errors") or []) or "no errors recorded"
        raise ArtifactError(f"the artifact has no resolved stackup ({errors}): fix the "
                            "stackup and re-run physics_resolve")
    if doc.get("design_digest") != physics_resolve.design_digest(design):
        raise ArtifactError("the artifact is stale (its design_digest is not the netlist's): "
                            "re-run physics_resolve on this netlist")
    return doc


# ---------------------------------------------------------------- impedance lines
@dataclass(frozen=True)
class _Line:
    """One controlled-impedance net's class geometry and where it came from."""

    width: float
    z: Any
    info: Mapping[str, Any]                 # summary[net]['impedance']
    diff: bool = False
    pair_gap: float | None = None           # KiCad diff_pair_gap (same-layer pairs only)
    ground_gap: float | None = None         # coplanar: the class clearance
    pour_clearance: float | None = None     # microstrip: reported, never the class clearance
    ref_nets: tuple[str, ...] = ()          # the primary layer's reference nets
    from_artifact: bool = False

    def clearance(self, net_clearance: float) -> tuple[float, str]:
        """(class clearance, its source) for a net whose IPC clearance is `net_clearance`."""
        if self.ground_gap is not None:
            return self.ground_gap, "ground_gap"
        return net_clearance, "floor"


def _design_lines(design: Mapping[str, Any]) -> dict[str, _Line]:
    """Today's lines: the impedance facade on the design (no artifact)."""
    out = {}
    for net, zi in impedance.resolve_impedance(design).items():
        diff = zi["mode"] == "diff"
        gap = zi.get("diff_pair_gap") if diff else None
        out[net] = _Line(round(zi["track_width"], 3), zi.get("z0", zi.get("zdiff")), zi,
                         diff=diff, pair_gap=None if gap is None else round(gap, 3))
    return out


def _impedance_nets(design: Mapping[str, Any]) -> list[str]:
    specs = design.get("net_specs") or {}
    return [net for net, spec in specs.items()
            if isinstance(spec, Mapping) and ("impedance" in spec or "diff" in spec)]


def _line_issues(rec: Any) -> str:
    issues = rec.get("issues") if isinstance(rec, Mapping) else None
    found = [f"{i.get('id')} {i.get('message')}" for i in issues or []
             if isinstance(i, Mapping) and i.get("severity") == "error"]
    return "; ".join(found) or f"status {rec.get('status') if isinstance(rec, Mapping) else rec!r}"


def _artifact_line(net: str, rec: Any) -> _Line:
    """The class geometry of one artifact line record (its primary layer)."""
    if not isinstance(rec, Mapping) or rec.get("status") == "error":
        raise ArtifactError(f"{net}: the artifact could not size the line ({_line_issues(rec)})")
    layers, primary = rec.get("layers"), rec.get("primary_layer")
    layer = layers.get(primary) if isinstance(layers, Mapping) else None
    if not isinstance(layer, Mapping) or _mm(layer.get("width")) is None:
        raise ArtifactError(f"{net}: the artifact line has no width on its primary layer "
                            f"{primary!r}")
    diff = rec.get("kind") == "diff"
    same_layer = diff and rec.get("pair") != "broadside"
    ref = layer.get("ref") if isinstance(layer.get("ref"), Mapping) else {}
    refs = tuple(n for n in ref.get("nets") or () if isinstance(n, str) and n)
    line = _Line(_mm(layer["width"]), rec.get("z_target"), {}, diff=diff,
                 pair_gap=_mm(layer.get("pair_gap")) if same_layer else None,
                 ground_gap=_mm(layer.get("ground_gap")),
                 pour_clearance=_mm(layer.get("pour_clearance")), ref_nets=refs,
                 from_artifact=True)
    info = {"mode": "diff" if diff else "single", "z_target": rec.get("z_target"),
            "line": rec.get("line"), "pair": rec.get("pair"), "layer": primary,
            "width": line.width, "pair_gap": line.pair_gap, "ground_gap": line.ground_gap,
            "pour_clearance": line.pour_clearance, "status": rec.get("status"),
            "accuracy_class": rec.get("accuracy_class"), "model": rec.get("model")}
    return replace(line, info=info)


def _artifact_lines(design: Mapping[str, Any], artifact: Mapping[str, Any]) -> dict[str, _Line]:
    """Every impedance net's line from the artifact, plus each diff pair's partner."""
    import dru_writer           # artifact path only (D7-12a): the same pairing as the DRU
    records = artifact.get("lines")
    records = records if isinstance(records, Mapping) else {}
    out = {}
    for net in _impedance_nets(design):
        if net not in records:
            raise ArtifactError(f"{net}: controlled impedance but the artifact has no line for "
                                "it (stale artifact? re-run physics_resolve)")
        out[net] = _artifact_line(net, records[net])
    pairs = dru_writer.pairs_of(design)
    partners = {pairs[net]: replace(line, info={**line.info, "partner_of": net})
                for net, line in out.items()
                if line.diff and pairs.get(net) and pairs[net] not in out}
    return {**out, **partners}


# ---------------------------------------------------------------- setup
@dataclass(frozen=True)
class _Setup:
    """Everything one build_classes run reads."""

    floor: Mapping[str, Any]
    base: Mapping[str, Any]
    rules: Mapping[str, Any]
    lines: Mapping[str, _Line]
    planes: tuple[str, ...]
    temp_rise_c: float
    source: str = SOURCE_DESIGN
    copper: tuple[Any, ...] = ()
    fab: Mapping[str, Any] = field(default_factory=dict)   # artifact path: fab limits

    @property
    def floor_track(self) -> float:
        return round(self.floor["min_track"], 3)

    @property
    def floor_clear(self) -> float:
        return round(self.floor["min_clearance"], 3)


def _copper_count(stackup: Mapping[str, Any]) -> int:
    """The stackup's copper layer count (copper_layers, else kicad_copper_count, else 2);
    ArtifactError unless it is a whole number >= 1."""
    for key in ("copper_layers", "kicad_copper_count"):
        count = stackup.get(key)
        if count is None:
            continue
        if (isinstance(count, bool) or not isinstance(count, (int, float))
                or not math.isfinite(count) or count < 1 or count != int(count)):
            raise ArtifactError(f"the artifact stackup copper layer count {key}={count!r} is "
                                "not a whole number >= 1: re-run physics_resolve")
        return int(count)
    return DEFAULT_LAYERS


def _fab_limits(stackup: Mapping[str, Any], fabs: Any) -> dict[str, Any]:
    """The fab's minimum track / clearance for this stackup (generic 0.1 mm without a fab)."""
    fab = stackup.get("fab") if isinstance(stackup.get("fab"), str) else None
    count = _copper_count(stackup)
    fabset = fabs if fabs is not None or fab is None else fab_presets.load_fabs()
    return {"fab": fab, "count": count,
            "track": impedance.fab_minimum(fabset, fab, count, "min_track_mm"),
            "clearance": impedance.fab_minimum(fabset, fab, count, "min_clearance_mm")}


def _setup(design: Mapping[str, Any], cls: int, copper_oz: float | None, temp_rise_c: float,
           template: Mapping[str, Any] | None, plane_nets: Iterable[str], artifact: Any,
           fabs: Any) -> _Setup:
    floor, base = ipc_rules.CLASS_PROFILES[cls], dict(template or {})
    if artifact is None:
        oz = DEFAULT_COPPER_OZ if copper_oz is None else copper_oz
        return _Setup(floor, base, ipc_rules.resolve_netclasses(design, cls, oz, temp_rise_c),
                      _design_lines(design), tuple(dict.fromkeys(plane_nets)), temp_rise_c)
    art = load_artifact(artifact, design)
    copper = tuple(art["stackup"].get("copper") or ())
    try:
        oz = copper_oz if copper_oz is not None else (
            ipc_rules.outer_copper_oz(copper) or DEFAULT_COPPER_OZ)
    except ValueError as exc:
        raise ArtifactError(f"the artifact stackup copper is malformed: {exc}") from exc
    planes = tuple(dict.fromkeys((*plane_nets_from_artifact(art), *plane_nets)))
    return _Setup(floor, base, ipc_rules.resolve_netclasses(design, cls, oz, temp_rise_c),
                  _artifact_lines(design, art), planes, temp_rise_c, SOURCE_ARTIFACT, copper,
                  _fab_limits(art["stackup"], fabs))


# ---------------------------------------------------------------- one net
def _voltage_note(net: str, line: _Line, setup: _Setup, below_fab: bool) -> list[str]:
    """A coplanar gap under the net's IPC-2221 voltage spacing (unless the fab-minimum
    warning already asks for a gap at least that wide)."""
    voltage = float((setup.rules.get(net) or {}).get("voltage", 0.0))
    spacing = ipc_rules.clearance_mm(voltage, VOLTAGE_COLUMN)
    if line.ground_gap >= spacing - _EPS or (below_fab
                                              and spacing <= setup.fab["clearance"] + _EPS):
        return []
    kind = str(line.info.get("line") or "coplanar").upper()
    return [f"{net}: {kind} ground gap {line.ground_gap:g} mm on {line.info.get('layer')} is "
            f"below the IPC-2221 spacing {spacing:g} mm for {voltage:g} V (Table 6-1 "
            f"{VOLTAGE_COLUMN}): the class clearance stays at the gap so the line keeps its "
            f"impedance - declare net_specs.{net}.ground_gap of at least {spacing:g} mm (the "
            "width is re-solved for it) or lower the voltage"]


def _gap_notes(net: str, line: _Line, clr: float, setup: _Setup) -> list[str]:
    """Artifact lines: a coplanar gap the fab cannot etch or below the net's voltage
    spacing; a microstrip pour clearance that the class clearance does not carry."""
    fab, layer, notes = setup.fab, line.info.get("layer"), []
    if line.ground_gap is not None:
        below_fab = line.ground_gap < fab["clearance"] - _EPS
        if below_fab:
            kind = str(line.info.get("line") or "coplanar").upper()
            notes.append(f"{net}: {kind} ground gap {line.ground_gap:g} mm on {layer} "
                         f"is below the {fab['fab'] or 'generic'} minimum clearance "
                         f"{fab['clearance']:g} mm ({fab['count']} copper layers): the fab "
                         f"cannot etch it - widen net_specs.{net}.ground_gap or drop it so the "
                         "solver picks one")
        notes += _voltage_note(net, line, setup, below_fab)
    if line.ground_gap is None and line.pour_clearance is not None and line.pour_clearance > clr:
        notes.append(f"{net}: microstrip pour clearance {line.pour_clearance:g} mm on {layer} "
                     f"is not the class clearance ({clr:g} mm): a KiCad class clearance also "
                     "binds the pads of the parts on the line (an 0402 DC block's pads are "
                     f"0.40 mm apart), so a GND pour may come within {clr:g} mm of the line "
                     "until the RF<->GND rule (ADR D7.8 R10) sets the gap")
    return notes


def _line_notes(net: str, line: _Line, width: float, clr: float, setup: _Setup) -> list[str]:
    """The impedance warnings of one line (a diff partner repeats none of them)."""
    if "partner_of" in line.info:
        return []
    notes = []
    if width < setup.floor_track:
        notes.append(f"{net}: {line.z}-ohm width {width}mm is below the "
                     f"Class floor {setup.floor_track}mm — thin the dielectric or relax the class")
    return notes + (_gap_notes(net, line, clr, setup) if line.from_artifact else [])


def _impedance_geometry(net: str, line: _Line, clr: float, setup: _Setup
                        ) -> tuple[float, float, Mapping[str, Any], list[str]]:
    """(class width, class clearance, summary impedance record, warnings) of a line."""
    if not line.from_artifact:
        return line.width, clr, line.info, _line_notes(net, line, line.width, clr, setup)
    clearance, source = line.clearance(clr)
    info = {**line.info, "clearance": clearance, "clearance_from": source}
    return line.width, clearance, info, _line_notes(net, line, line.width, clearance, setup)


def _class_dict(net: str, w: float, clr: float, via: tuple[float, float],
                line: _Line | None, setup: _Setup, priority: int) -> dict[str, Any]:
    pair_w = line.width if line is not None and line.diff else None
    c = {**setup.base, "name": _class_name(net), "track_width": w, "clearance": clr,
         "via_diameter": via[0], "via_drill": via[1],
         "diff_pair_width": pair_w if pair_w else w, "priority": priority}
    if line is not None and line.diff and line.pair_gap is not None:
        c["diff_pair_gap"] = line.pair_gap
    return c


def _net_class(net: str, rule: Mapping[str, Any], setup: _Setup, priority: int
               ) -> tuple[dict[str, Any], dict[str, Any] | None, list[str]]:
    """(summary entry, class dict or None for Default, warnings) of one net."""
    # a net with a dedicated copper PLANE carries its current in the plane; every
    # surface segment is a short pad-to-via stub. Forcing the IPC width onto those
    # stubs (on the highest-fanout nets, no less) starves the router for nothing —
    # the plane IS the conductor. Via ampacity into the plane is still enforced by G4.
    if net in setup.planes:
        return {"plane": True, "voltage": rule["voltage"], "current": rule["current"]}, None, []
    w = max(round(rule["track_width"], 3), setup.floor_track)
    clr = max(round(rule["clearance"], 3), setup.floor_clear)
    entry = {"track_width": w, "clearance": clr,
             "voltage": rule["voltage"], "current": rule["current"]}
    # controlled impedance overrides current-sizing for this net: the trace MUST be the
    # impedance width (a wider current rule would detune it). Such a net ALWAYS gets
    # its own class even if the width is at the floor.
    line, notes = setup.lines.get(net), []
    if line is not None:
        w, clr, info, notes = _impedance_geometry(net, line, clr, setup)
        entry = {**entry, "impedance": info}
    elif w <= setup.floor_track and clr <= setup.floor_clear:
        return entry, None, []              # neither above the floor nor impedance: Default
    via_d, via_drill, via_amp, vias_needed = _via_for(
        w, rule["current"], float(setup.base.get("via_diameter", 0.6)),
        float(setup.base.get("via_drill", 0.3)))
    entry = {**entry, "via": {"diameter": via_d, "drill": via_drill,
                              "ampacity_a": via_amp, "vias_needed": vias_needed}}
    if vias_needed > 1:
        notes = [*notes, f"{net}: {rule['current']} A exceeds one {via_drill} mm via barrel "
                         f"({via_amp} A at 10C rise) — every layer transition on this net "
                         f"needs {vias_needed} parallel vias (or keep it on one layer)"]
    return entry, _class_dict(net, w, clr, (via_d, via_drill), line, setup, priority), notes


# ---------------------------------------------------------------- coplanar ground
def _coplanar_needs(setup: _Setup) -> dict[str, float]:
    """{reference net: the smallest coplanar ground gap beside it} of the artifact's
    coplanar lines (only nets of the design that may get a class)."""
    need: dict[str, float] = {}
    for line in setup.lines.values():
        if line.ground_gap is None:
            continue
        for ref in line.ref_nets:
            if ref in setup.rules and _classed(ref):
                need[ref] = min(need.get(ref, math.inf), line.ground_gap)
    return need


def _ground_class(ref: str, gap: float, setup: _Setup, priority: int) -> dict[str, Any]:
    """A class for a coplanar reference net that would sit on Default: Default's width and
    vias (the plane / reference net is never width-classed), clearance = the gap."""
    width = max(float(setup.base.get("track_width", 0.0)), setup.floor_track)
    return {**setup.base, "name": _class_name(ref), "track_width": width, "clearance": gap,
            "via_diameter": float(setup.base.get("via_diameter", 0.6)),
            "via_drill": float(setup.base.get("via_drill", 0.3)),
            "diff_pair_width": width, "priority": priority}


def _coplanar_ground(setup: _Setup, classes: list[dict], patterns: list[dict],
                     summary: dict[str, Any]) -> tuple[list[dict], list[dict], dict[str, Any]]:
    """KiCad pours at max(the track's class clearance, the pour net's class clearance)
    (verified on 9.0.4), so a coplanar gap below the reference net's clearance needs the
    reference net's class lowered to it too. Every other pair still resolves to the larger
    class, so only the reference net next to the line moves. New lists; inputs untouched."""
    out_c, out_p, out_s = list(classes), list(patterns), dict(summary)
    default_clr = max(float(setup.base.get("clearance", 0.0)), setup.floor_clear)
    for ref, gap in _coplanar_needs(setup).items():
        name = _class_name(ref)
        at = next((i for i, c in enumerate(out_c) if c["name"] == name), None)
        if at is not None and out_c[at]["clearance"] > gap:
            out_c[at] = {**out_c[at], "clearance": gap}
        elif at is None and gap < default_clr:
            out_c.append(_ground_class(ref, gap, setup, len(out_c)))
            out_p.append({"netclass": name, "pattern": ref})
        else:
            continue
        out_s[ref] = {**out_s[ref], "coplanar_clearance": gap}
    return out_c, out_p, out_s


# ---------------------------------------------------------------- build / apply
def _layer_notes(setup: _Setup, summary: Mapping[str, Any],
                 classes: Iterable[Mapping[str, Any]]) -> list[str]:
    """ipc_rules.layer_warnings at each net's written width (artifact path only)."""
    if not setup.copper:
        return []
    widths = {c["name"]: c["track_width"] for c in classes}
    rules = {net: {"current": e["current"],
                   "track_width": widths.get(_class_name(net), e["track_width"])}
             for net, e in summary.items() if not e.get("plane")}
    try:
        return ipc_rules.layer_warnings(rules, setup.copper, setup.temp_rise_c, setup.planes)
    except ValueError as exc:
        raise ArtifactError(f"the artifact stackup copper is malformed: {exc}") from exc


def _rule_floor(setup: _Setup, classes: Iterable[Mapping[str, Any]]) -> dict[str, float] | None:
    """The board rule floor the artifact's classes need (never below the fab minimum)."""
    if setup.source != SOURCE_ARTIFACT:
        return None
    rows = list(classes)
    track = min([setup.floor_track, *(max(setup.fab["track"], c["track_width"]) for c in rows)])
    clear = min([setup.floor_clear, *(max(setup.fab["clearance"], c["clearance"]) for c in rows)])
    return {"min_track_width": round(track, 6), "min_clearance": round(clear, 6)}


def build_classes(design: dict, cls: int = 3, copper_oz: float | None = None,
                  temp_rise_c: float = 10.0, template: dict | None = None,
                  plane_nets: tuple = (), *, artifact: Any = None, fabs: Any = None) -> dict:
    """Resolve the netlist into KiCad net-class dicts + binding patterns.

    Returns {"classes": [class_dict, ...], "patterns": [{netclass, pattern}, ...],
    "default": {track_width, clearance}, "summary": {net: rule}, "class_name",
    "warnings", "plane_nets", "source": design | artifact, "rule_floor": the board
    floor the artifact's classes need (None without one)}. Only nets whose IPC-resolved
    width or clearance EXCEEDS the class floor, or that carry an impedance target, get a
    dedicated class; everything else stays on the floor-sized Default (no clutter).
    `artifact` and `fabs` (a fab_presets.FabSet) per the module docstring; `copper_oz`
    None = the artifact's outer copper, else 1 oz. Raises ArtifactError for a bad artifact."""
    setup = _setup(design, cls, copper_oz, temp_rise_c, template, plane_nets, artifact, fabs)
    classes, patterns, summary, warnings = [], [], {}, []
    for net, rule in setup.rules.items():
        if not _classed(net):
            continue
        summary[net], c, notes = _net_class(net, rule, setup, len(classes))
        warnings += notes
        if c is not None:
            classes.append(c)
            patterns.append({"netclass": c["name"], "pattern": net})
    classes, patterns, summary = _coplanar_ground(setup, classes, patterns, summary)
    warnings += _layer_notes(setup, summary, classes)
    return {"classes": classes, "patterns": patterns,
            "default": {"track_width": setup.floor_track, "clearance": setup.floor_clear},
            "summary": summary, "class_name": setup.floor["name"], "warnings": warnings,
            "plane_nets": list(setup.planes), "source": setup.source,
            "rule_floor": _rule_floor(setup, classes)}


def _mapping(value: Any) -> dict:
    return dict(value) if isinstance(value, Mapping) else {}


def _project_with(proj: Mapping[str, Any], built: Mapping[str, Any], cls: int) -> dict:
    """A new project dict: Default bumped to the floor, hand-authored classes and patterns
    kept, ours replaced, and the board rule floor lowered (never raised)."""
    ns = _mapping(proj.get("net_settings"))
    dflt = built["default"]
    kept = [{**c, "track_width": max(c.get("track_width", 0), dflt["track_width"]),
             "clearance": max(c.get("clearance", 0), dflt["clearance"])}
            if c.get("name") == "Default" else c
            for c in ns.get("classes") or []
            if not str(c.get("name", "")).startswith(_MANAGED_PREFIX)]
    hand = [pt for pt in (ns.get("netclass_patterns") or [])
            if not str(pt.get("netclass", "")).startswith(_MANAGED_PREFIX)]
    ns = {**ns, "classes": kept + built["classes"],
          "netclass_patterns": hand + built["patterns"]}
    # Keep the DRC rule floor at or below the tightest class so DRC never fights a
    # legitimately-narrow class width (it still enforces the per-class minimum).
    # IMPORTANT: do NOT raise the mechanical floors (edge clearance, drill, annular)
    # to the reliability-class target here — those are fab-CAPABILITY limits, and
    # forcing Class-3 reliability margins as hard DRC minimums shrinks the routable
    # area and breaks otherwise-routable boards. The class reliability targets are
    # reported as advisory by design_report; the board keeps the fab's real floors.
    board = _mapping(proj.get("board"))
    ds = _mapping(board.get("design_settings"))
    rules = _mapping(ds.get("rules"))
    floor, need = ipc_rules.CLASS_PROFILES[cls], built.get("rule_floor") or {}
    rules = {**rules,
             "min_track_width": min(rules.get("min_track_width", 1.0), floor["min_track"],
                                    need.get("min_track_width", math.inf)),
             "min_clearance": min(rules.get("min_clearance", 1.0), floor["min_clearance"],
                                  need.get("min_clearance", math.inf))}
    return {**proj, "net_settings": ns,
            "board": {**board, "design_settings": {**ds, "rules": rules}}}


def apply_to_project(pro_path: str, design: dict, cls: int = 3,
                     copper_oz: float | None = None, temp_rise_c: float = 10.0,
                     plane_nets: tuple = (), *, artifact: Any = None, fabs: Any = None) -> dict:
    """Write the resolved net classes into a `.kicad_pro`. Bumps Default to the
    class floor, replaces any previously-managed classes/patterns, preserves
    hand-authored ones, and lowers the board rule floor to match (LF, trailing
    newline). `artifact` per the module docstring; a bad one returns ok False and
    writes nothing. Returns a report (classes written, per-net summary)."""
    p = Path(pro_path)
    if not p.is_file():
        return {"ok": False, "reason": f"project file not found: {pro_path}"}
    proj = json.loads(p.read_text(encoding="utf-8"))
    existing = (proj.get("net_settings") or {}).get("classes") or []
    default_tmpl = next((c for c in existing if c.get("name") == "Default"),
                        existing[0] if existing else {})
    try:
        built = build_classes(design, cls, copper_oz, temp_rise_c, default_tmpl,
                              plane_nets=plane_nets, artifact=artifact, fabs=fabs)
    except ArtifactError as exc:
        return {"ok": False, "reason": str(exc)}
    new = _project_with(proj, built, cls)
    p.write_text(json.dumps(new, indent=2) + "\n", encoding="utf-8", newline="\n")
    rules = new["board"]["design_settings"]["rules"]
    return {"ok": True, "project": pro_path, "class_profile": built["class_name"],
            "classes_written": [c["name"] for c in built["classes"]],
            "default": built["default"], "summary": built["summary"],
            "warnings": built.get("warnings", []), "source": built["source"],
            "plane_nets": built["plane_nets"],
            "rule_floor": {k: rules[k] for k in ("min_track_width", "min_clearance")}}


if __name__ == "__main__":
    import sys
    netlist = sys.argv[1]
    pro = sys.argv[2] if len(sys.argv) > 2 else str(Path(netlist).with_suffix(".kicad_pro"))
    cls = int(sys.argv[3]) if len(sys.argv) > 3 else 3
    design = json.loads(Path(netlist).read_text(encoding="utf-8"))
    print(json.dumps(apply_to_project(pro, design, cls), indent=2))
