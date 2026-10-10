#!/usr/bin/env python3
"""G1 stackup and controlled-impedance lint (ST-*), ORDER 50 (ADR D7.2.7, D7.11; D7-10a).

  resolve  stackup_model.resolve: ST-E1..E8, ST-E12 and ST-W1..W9 (ST-W3 is the fab
           capability check: layer count, impedance control, materials, hybrid, PTFE)
  policy   layer_policy.check_declared: ST-W12 (no layer count, or napkin mode without
           a rationale) and the D7.11 floors above the declared count (F1 -> ST-W3,
           F2 / F3 -> ST-W12)
  names    ST-E11: an impedance / RF net, or its diff partner, whose name holds ' or \\
           (the managed DRU conditions cannot quote it)
  lines    tline.geometry on every allowed layer of each impedance / RF net
           (``impedance``, ``diff`` or ``freq_hz`` set): ST-E9 no resolvable reference,
           ST-E10 an RF reference outside ``ref_nets``, ST-W10 a power-plane
           reference, ST-W11 allowed layers resting on different reference nets.
           Allowed layers as the impedance facade takes them: ``net_specs.layers``,
           else an RF net's first eligible outer layer, else every eligible layer
  pads     ST-W13: a Freerouting-routed impedance net (not RF, ``route`` not preroute /
           agent) whose allowed layers miss the layer of one of its SMD pads (side F ->
           F.Cu, side B -> B.Cu; a through-hole pad reaches every layer): D7.9 pad rule
  values   IMP-E2: a malformed net_specs value the line check reads (the impedance
           facade's id and rule); that net's line checks are skipped

The line checks need the resolved stackup, so they run only without ST-E1..E8.
Findings read 'ID at <JSON path>: message'; info findings 'ID (info) at ...' and go
with the warnings. Pure (the shipped materials and fab presets are read once per
process), never mutates the design, never raises on a malformed one. Stdlib only.
"""
from __future__ import annotations

import functools
import math
import sys
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from types import MappingProxyType
from typing import Any

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import layer_policy  # noqa: E402
import materials  # noqa: E402
import stackup_model  # noqa: E402
import stackup_roles as roles  # noqa: E402
from stackup_roles import Issue, json_path  # noqa: E402
from tline import geometry  # noqa: E402

__all__ = ["CODES", "ORDER", "SPEC_ERROR", "allowed_layers", "check", "stackup_issues"]

ORDER = 50
SPEC_ERROR = "IMP-E2"                       # impedance.SPEC_ERROR: the same id and rule
OUTER = ("F.Cu", "B.Cu")
UNQUOTABLE = ("'", "\\")
NOT_FREEROUTED = ("preroute", "agent")
PAIRS = ("edge", "broadside")
POSITIVE_KEYS = MappingProxyType({"width": "mm", "gap": "mm", "ground_gap": "mm",
                                  "freq_hz": "Hz", "z_tol_pct": "%"})

CODES = MappingProxyType({
    "ST-E1": "copper and dielectric layers do not alternate, or the stack does not start "
             "and end with copper",
    "ST-E2": "the copper count is not 1 or an even number 2..32, or a copper name does not "
             "match its position",
    "ST-E3": "both stackup.preset and stackup.layers, legacy {h, er, t} keys mixed with model "
             "keys, or a malformed stackup value",
    "ST-E4": "an unknown preset, template or fab, or a material not in the library without "
             "an explicit dk",
    "ST-E5": "a thickness <= 0, a dk outside [1, 15] or a df outside [0, 0.1]",
    "ST-E6": "planes names a signal-role or missing layer, or a plane / role net disagrees",
    "ST-E7": "copper_layers disagrees with the stackup",
    "ST-E8": "a via span that is not a through via (wave 2 builds through vias only)",
    "ST-E9": "an impedance or RF net with no resolvable reference plane on an allowed layer",
    "ST-E10": "an RF net whose reference net is not in its ref_nets (default GND)",
    "ST-E11": "an impedance or RF net (or diff partner) named with ' or \\, which a DRU "
              "condition cannot quote",
    "ST-E12": "single-sided board: an SMD part on side B, a B.Cu preroute or via, "
              "emc.stitch, or an impedance / RF net",
    "ST-W1": "the computed thickness is outside the fab tolerance of board_thickness",
    "ST-W2": "an asymmetric build (warp risk)",
    "ST-W3": "fab capability: layer count, impedance control (incl. floor F1), material, "
             "hybrid or PTFE multilayer",
    "ST-W4": "a bondply ply must fill more copper than it can (RO4450F 45.7 um per ply)",
    "ST-W5": "thin-core Dk drop (RO4350B 4 mil process Dk 3.33)",
    "ST-W6": "info: Dk used outside its characterised band, or a defaulted Df",
    "ST-W7": "legacy stackup shorthand {h, er, t}",
    "ST-W8": "Dk sources more than 2 % apart on a reference dielectric",
    "ST-W9": "solder mask off (mask: null)",
    "ST-W10": "an impedance net referenced to a power plane on one or both sides",
    "ST-W11": "an impedance net allowed on layers with different reference nets",
    "ST-W12": "no layer count declared, napkin mode without a rationale, or below floor "
              "F2 / F3",
    "ST-W13": "an impedance net whose allowed layers exclude its SMD pad layer (Freerouting "
              "cannot fan it out)",
    SPEC_ERROR: "a malformed net_specs value the impedance line check reads",
})


@functools.lru_cache(maxsize=1)
def _shipped_materials() -> materials.MaterialLibrary:
    return materials.load_library()


# ---------------------------------------------------------------------------
# The nets that ask for a line, and their spec values (IMP-E2)
# ---------------------------------------------------------------------------
def _is_line_net(spec: Mapping[str, Any]) -> bool:
    return "impedance" in spec or "diff" in spec or spec.get("freq_hz") is not None


def _line_nets(design: Mapping[str, Any]) -> list[tuple[str, Mapping[str, Any]]]:
    specs = design.get("net_specs")
    return [(n, s) for n, s in (specs.items() if isinstance(specs, Mapping) else ())
            if isinstance(n, str) and isinstance(s, Mapping) and _is_line_net(s)]


def _target_ok(value: Any) -> bool:
    try:
        z = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(z) and z > 0


def _names(value: Any) -> bool:
    return isinstance(value, (list, tuple)) and bool(value) and all(
        isinstance(x, str) for x in value)


def _layer_lists(value: Any) -> bool:
    return isinstance(value, Mapping) and all(
        isinstance(k, str) and isinstance(v, (list, tuple)) and all(isinstance(x, str) for x in v)
        for k, v in value.items())


_SHAPES = (("layers", _names, "a non-empty list of copper layer names"),
           ("ref_nets", _names, "a non-empty list of net names"),
           ("ref_layers", _layer_lists, "a {layer: [copper layer names]} map"),
           ("void_layers", _layer_lists, "a {layer: [copper layer names]} map"),
           ("pair", PAIRS.__contains__, f"one of {', '.join(PAIRS)}"))


def _spec_problems(net: str, spec: Mapping[str, Any]) -> list[Issue]:
    """IMP-E2 for each malformed value the line check reads (the facade's rule)."""
    key = "impedance" if "impedance" in spec else "diff" if "diff" in spec else None
    bad = [] if key is None or _target_ok(spec[key]) else [(key, "an impedance > 0 ohm")]
    bad += [(k, f"a number > 0 {unit}") for k, unit in POSITIVE_KEYS.items()
            if spec.get(k) is not None and not (roles.is_number(spec[k]) and spec[k] > 0)]
    bad += [(k, what) for k, ok, what in _SHAPES if spec.get(k) is not None and not ok(spec[k])]
    return [roles.error(SPEC_ERROR, f"net_specs.{net}.{k} must be {what}, got {spec[k]!r}",
                        ("net_specs", net, k)) for k, what in bad]


def _line_spec(spec: Mapping[str, Any]) -> dict[str, Any]:
    """What the line geometry reads: 'impedance' wins over 'diff' (single-ended)."""
    return {k: v for k, v in spec.items() if k != "diff" or "impedance" not in spec}


# ---------------------------------------------------------------------------
# ST-E11
# ---------------------------------------------------------------------------
def _unquotable(name: Any) -> bool:
    return isinstance(name, str) and any(ch in name for ch in UNQUOTABLE)


def name_issues(design: Mapping[str, Any]) -> list[Issue]:
    """ST-E11 for every impedance / RF net, or its diff partner, named with ' or \\."""
    fix = ("the managed DRU rules name it in a condition and cannot quote it: rename the "
           "net (letters, digits, _ + - /)")
    out = [roles.error("ST-E11", f"impedance/RF net {net!r} contains ' or \\: {fix}",
                       ("net_specs", net)) for net, _s in _line_nets(design) if _unquotable(net)]
    return out + [roles.error("ST-E11", f"the diff partner {s['diff_pair']!r} of {net} contains "
                                        f"' or \\: {fix}", ("net_specs", net, "diff_pair"))
                  for net, s in _line_nets(design) if _unquotable(s.get("diff_pair"))]


# ---------------------------------------------------------------------------
# The line checks (ST-E9, ST-E10, ST-W10, ST-W11) and the pad rule (ST-W13)
# ---------------------------------------------------------------------------
def allowed_layers(stackup: Any, spec: Mapping[str, Any]) -> tuple[str, ...]:
    """The layers a net may use, as the impedance facade takes them: net_specs.layers,
    else an RF net's first eligible outer layer, else every eligible layer."""
    declared = spec.get("layers")
    if declared:
        return tuple(declared)
    eligible = geometry.eligible_layers(stackup, spec)
    if geometry.rf_freq(spec) is not None:
        return tuple(layer for layer in OUTER if layer in eligible)[:1]
    return tuple(eligible)


def _issue(found: Any) -> Issue:
    return Issue(found.id, found.severity, found.message, found.path)


def _no_layer(rs: stackup_model.ResolvedStackup, spec: Mapping[str, Any],
              path: str) -> list[Issue]:
    """Why no layer is allowed: the errors of every candidate layer (RF: the outer ones)."""
    rf = geometry.rf_freq(spec) is not None
    layers = [x for x in rs.routable_layers() if not rf or x in OUTER]
    found = [_issue(i) for x in layers
             for i in geometry.resolve_line(rs, x, spec, path=path).issues if i.severity == "error"]
    why = ("an RF net routes on an outer layer unless its net_specs layers say otherwise"
           if rf else "the stackup has no routable copper layer for it")
    return found or [Issue("ST-E9", "error", f"no layer for the line: {why}; "
                                             f"{geometry.E9_FIX}", path)]


def _components(design: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    comps = design.get("components")
    return {c["ref"]: c for c in (comps if isinstance(comps, list) else [])
            if isinstance(c, Mapping) and isinstance(c.get("ref"), str)}


def _pad_refs(design: Mapping[str, Any], net: str) -> Iterator[str]:
    nets = design.get("nets")
    pads = nets.get(net) if isinstance(nets, Mapping) else None
    for pad in pads if isinstance(pads, list) else []:
        if isinstance(pad, (list, tuple)) and pad and isinstance(pad[0], str):
            yield pad[0]


def _smd_layer(comp: Mapping[str, Any] | None) -> str | None:
    if comp is None or roles.through_hole(comp.get("footprint")):
        return None
    return "B.Cu" if str(comp.get("side") or "F").upper() == "B" else "F.Cu"


def _pad_issues(design: Mapping[str, Any], net: str, spec: Mapping[str, Any],
                allowed: Sequence[str], path: str) -> list[Issue]:
    """ST-W13 per SMD pad layer the Freerouting-routed net may not use."""
    if geometry.rf_freq(spec) is not None or spec.get("route") in NOT_FREEROUTED:
        return []
    comps = _components(design)
    hits = sorted({(layer, ref) for ref in _pad_refs(design, net)
                   for layer in (_smd_layer(comps.get(ref)),) if layer and layer not in allowed})
    by_layer = {layer: [r for x, r in hits if x == layer] for layer, _r in hits}
    return [Issue("ST-W13", "warning", (
        f"Freerouting routes {net} only on {', '.join(allowed)}, but its SMD pads on {layer} "
        f"({', '.join(refs)}) are on none of them: add {layer} to net_specs.{net}.layers or "
        "set route to preroute / agent (ADR D7.9 pad-layer rule; per-layer widths are not "
        "verified for Freerouting)"), path) for layer, refs in by_layer.items()]


def _net_issues(design: Mapping[str, Any], rs: stackup_model.ResolvedStackup, net: str,
                spec: Mapping[str, Any]) -> list[Issue]:
    problems = _spec_problems(net, spec)
    if problems:
        return problems
    path, line = json_path("net_specs", net), _line_spec(spec)
    allowed = allowed_layers(rs, line)
    if not allowed:
        return _no_layer(rs, line, path)
    setups = [geometry.resolve_line(rs, layer, line, path=path) for layer in allowed]
    found = [_issue(i) for s in setups for i in s.issues]
    found += [_issue(i) for i in geometry.transition_issues(setups, path=path)]
    if roles.has_errors(found):
        return found
    return found + _pad_issues(design, net, line, allowed, path)


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------
def _unique(issues: Sequence[Issue]) -> tuple[Issue, ...]:
    seen: dict[tuple[str, str, str, str], Issue] = {}
    for i in issues:
        seen.setdefault((i.id, i.severity, i.path, i.message), i)
    return tuple(seen.values())


def stackup_issues(design: Any, *, materials_lib: materials.MaterialLibrary | None = None,
                   fabs: Any = None) -> tuple[Issue, ...]:
    """Every G1 stackup finding of ``design`` (module docstring), in order: resolve,
    policy, names, then the line and pad checks per net (net_specs order)."""
    if not isinstance(design, Mapping):
        return ()
    if not isinstance(design.get("nets", {}), Mapping):
        # the core rules report a malformed 'nets'; the plane-net default (D7.2.3) reads
        # its names and would raise on a non-mapping, so the resolver sees none
        design = {**design, "nets": {}}
    lib = materials_lib if materials_lib is not None else _shipped_materials()
    fabset = fabs if fabs is not None else layer_policy.shipped_fabs()
    rs, found = stackup_model.resolve(design, materials=lib, fabs=fabset)
    issues = [*found, *layer_policy.check_declared(design, rs, fabs=fabset),
              *name_issues(design)]
    if rs is not None:
        issues += [i for net, spec in _line_nets(design)
                   for i in _net_issues(design, rs, net, spec)]
    return _unique(issues)


def _render(issue: Issue) -> str:
    tag = f"{issue.id} (info)" if issue.severity == "info" else issue.id
    return f"{tag} at {issue.path}: {issue.message}"


def check(design: Mapping[str, Any], ctx: Any) -> tuple[list[str], list[str]]:
    """lint_netlist's rule entry point: (errors, warnings); info goes with the warnings."""
    del ctx                                 # the stackup lint needs no library context
    issues = stackup_issues(design)
    return ([_render(i) for i in issues if i.severity == "error"],
            [_render(i) for i in issues if i.severity != "error"])
