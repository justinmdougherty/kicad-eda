#!/usr/bin/env python3
"""Corrective-action engine: apply a gate's auto-resolvable tasks to the design.

A failing gate emits tasks (see gates.py). The ones with an unambiguous,
standard remedy are applied here — the design is modified, the stage re-runs,
and the gate is re-tested. This closes the loop the review process demands:
a gate failure becomes completed work, not a dead stop.

Auto-applied fix types:
  add_decoupling          add a 100nF/0603 bypass between the named rail and GND,
                          associated with the named IC (functional placement will
                          cluster it at that IC automatically)
  declare_no_connect      (napkin, D5) append [ref, pad] to design["no_connect"] once
  net_same_name_pin       (napkin, D5) append [ref, pad] to the named net: a stacked /
                          duplicate SUPPLY pin of a netted same-name pin (the gate
                          emits it for supply/ground names only)
  swap_thermal_footprint  (napkin, D5) the first ...ThermalVias variant of the part's
                          stock footprint that ``footprint_exists`` accepts
  face_connector_out      (napkin, D5) re-place a connector so its mouth faces out of
                          the nearest board edge (napkin_policy.face_out on the
                          swig_connector_probe result); a board-internal header is
                          marked "interior": true instead

``apply_structured`` returns one Remedy per applied task (confidence,
rationale, alternatives and the netlist path a human edits to change it — what
the harness records as a decision) and one Skip per auto task an applier could
not apply (with the reason; the task stays open). Auto tasks of a type with no
applier here (loosen_placement, reroute) are left to the harness. ``apply``
keeps its original (design, [texts]) shape for the G2 loop.

Never mutates the input design — works on a deep copy and returns it.
"""
from __future__ import annotations

import copy
import re
from typing import Any, Callable, Mapping, NamedTuple

import napkin_policy as npol

_BYPASS = {"lib_id": "Device:C", "value": "100nF",
           "footprint": "Capacitor_SMD:C_0603_1608Metric"}
_DEFAULT_POLICY: Mapping[str, str] = {"add_decoupling": "G2.add_decoupling", **npol.POLICY_FOR}

FootprintExists = Callable[[str], bool]


class Remedy(NamedTuple):
    """One applied corrective action, shaped for the decision log (D5)."""
    gate: str
    text: str
    policy: str | None
    confidence: str
    rationale: str
    alternatives: tuple[str, ...]
    override: str | None


class Skip(NamedTuple):
    """An auto task whose applier declined; ``reason`` says why (it stays open)."""
    gate: str
    desc: str
    fix: dict
    reason: str


class _Outcome(NamedTuple):
    text: str
    confidence: str
    rationale: str
    alternatives: tuple[str, ...]
    override: str


class _Context(NamedTuple):
    footprint_exists: FootprintExists | None
    probe: Mapping | None


class _Declined(Exception):
    """An applier cannot apply its task; the message is the Skip reason."""


# --- helpers -----------------------------------------------------------------------------
def _next_ref(design: dict, prefix: str = "C") -> str:
    pat = re.compile(rf"^{prefix}(\d+)$")
    nums = [int(m.group(1)) for c in design["components"]
            if (m := pat.match(c["ref"]))]
    return f"{prefix}{max(nums, default=0) + 1}"


def _component(design: dict, ref: Any) -> tuple[int, dict]:
    for i, comp in enumerate(design.get("components", [])):
        if comp.get("ref") == ref:
            return i, comp
    raise _Declined(f"{ref}: no component with that ref")


def _replace_component(design: dict, index: int, comp: dict) -> None:
    comps = list(design["components"])
    comps[index] = comp
    design["components"] = comps


def _net_of(design: dict, ref: str, pad: str) -> str | None:
    for net, conns in design.get("nets", {}).items():
        if any(r == ref and str(p) == pad for r, p in conns):
            return net
    return None


def _pin(fix: Mapping, kind: str) -> tuple[str, str]:
    ref, pad = fix.get("ref"), fix.get("pad")
    if not isinstance(ref, str) or not ref or pad is None or str(pad) == "":
        raise _Declined(f"malformed {kind} fix (needs ref and pad): {dict(fix)!r}")
    return ref, str(pad)


# --- appliers (each works on the deep copy; it checks everything before changing it) ------
def _add_decoupling(design: dict, fix: Mapping, _ctx: _Context) -> _Outcome:
    rail = fix["rail"]
    ref = _next_ref(design, "C")
    nets = design["nets"]
    design["components"] = [*design["components"], {"ref": ref, **_BYPASS}]
    design["nets"] = {**nets, rail: [*nets.get(rail, []), [ref, "1"]]}
    design["nets"]["GND"] = [*design["nets"].get("GND", []), [ref, "2"]]
    return _Outcome(
        f"added {ref} (100nF 0603) bypass: {rail} -> GND, serving {fix.get('ref', '?')}",
        "high", f"every IC supply rail needs a local bypass capacitor; {rail} had none",
        ("a value / package the datasheet asks for", "a bulk + 100nF pair on a noisy rail"),
        f"components[{ref}]")


def _declare_no_connect(design: dict, fix: Mapping, _ctx: _Context) -> _Outcome:
    ref, pad = _pin(fix, "declare_no_connect")
    pin, ptype, name = f"{ref}.{pad}", fix.get("pin_type"), fix.get("pin_name")
    declared = design.get("no_connect") or []
    if [ref, pad] in [[r, str(p)] for r, p in declared]:
        raise _Declined(f"{pin} is already declared no_connect")
    net = _net_of(design, ref, pad)
    if net is not None:
        raise _Declined(f"{pin} is netted to {net}; not declaring it no_connect")
    verdict = npol.no_connect_verdict(ptype, name)
    if verdict is None:
        raise _Declined(f"{pin}: pin type {ptype!r} is not auto-no-connectable (D5); "
                        f"net it")
    design["no_connect"] = [*declared, [ref, pad]]
    return _Outcome(f"declared {pin} no_connect ({npol.describe_pin(ptype, name)})", verdict[0],
                    verdict[1], ("net the pin to the signal it was meant for",
                                 "tie it to a rail or a pull per the datasheet"),
                    "no_connect")


def _net_same_name_pin(design: dict, fix: Mapping, _ctx: _Context) -> _Outcome:
    ref, pad = _pin(fix, "net_same_name_pin")
    net, nets = fix.get("net"), design.get("nets", {})
    if not isinstance(net, str) or not net:
        raise _Declined(f"malformed net_same_name_pin fix (needs net): {dict(fix)!r}")
    if net not in nets:
        raise _Declined(f"net {net!r} is not in the netlist")
    if [ref, pad] in [[r, str(p)] for r, p in design.get("no_connect") or []]:
        raise _Declined(f"{ref}.{pad} is declared no_connect")
    current = _net_of(design, ref, pad)
    if current is not None:
        raise _Declined(f"{ref}.{pad} is already on net {current}")
    design["nets"] = {**nets, net: [*nets[net], [ref, pad]]}
    return _Outcome(
        f"joined {ref}.{pad} to net {net} (stacked / duplicate supply pin of a netted "
        f"same-name pin)", "high",
        f"{ref}.{pad} shares its supply/ground pin name with a pin of {ref} already on {net} "
        f"(stacked pins, D1); leaving it open would orphan a pad of that rail",
        ("declare it no_connect if the datasheet says the duplicate may float",),
        f"nets.{net}")


def _swap_thermal_footprint(design: dict, fix: Mapping, ctx: _Context) -> _Outcome:
    ref = fix.get("ref")
    index, comp = _component(design, ref)
    old = comp.get("footprint") or ""
    candidates = npol.thermal_variants(old)
    if not candidates:
        if "ThermalVias" in npol.name_of(old):
            raise _Declined(f"{ref}: {old} is already a ThermalVias variant")
        lib = npol.lib_of(old) or old or "(no footprint)"
        raise _Declined(f"{ref}: no stock ThermalVias variant for {lib}")
    if ctx.footprint_exists is None:
        raise _Declined(f"{ref}: cannot check that {candidates[0]} exists (no footprint "
                        f"lookup supplied)")
    new = next((c for c in candidates if ctx.footprint_exists(c)), None)
    if new is None:
        raise _Declined(f"{ref}: no ThermalVias variant of {old} exists "
                        f"({', '.join(candidates)} not found)")
    _replace_component(design, index, {**comp, "footprint": new})
    return _Outcome(
        f"{ref}: footprint {old} -> {new} (thermal via farm under the EP)", "high",
        "a dissipating exposed-pad part needs a thermal via farm under the EP (IPC-7093); "
        "the ThermalVias variant has the same pads plus the vias",
        ("add stitching vias under the EP on the board",), f"components[{ref}].footprint")


def _probe_parts(probe: Mapping | None) -> tuple[Any, Mapping]:
    """(board_bbox, {ref: entry}) of a swig_connector_probe result."""
    if probe is None:
        raise _Declined("no connector probe was run")
    if probe.get("ok") is False:
        why = probe.get("reason") or probe.get("error") or "no detail"
        raise _Declined(f"connector probe failed: {why}")
    conns = probe.get("connectors") or []
    by_ref = conns if isinstance(conns, Mapping) else {
        c.get("ref"): c for c in conns if isinstance(c, Mapping)}
    return probe.get("board_bbox"), by_ref


def _fixed_board(design: Mapping) -> bool:
    board = design.get("board") or {}
    return ("w" in board and "h" in board) or "outline" in board


def _face_rationale(fix: Mapping) -> str:
    if fix.get("issue") == "far":
        return (f"its mating face sat {fix.get('dist_mm')} mm from the edge it faces "
                f"(must be <= 3 mm); a connector mates through the board edge")
    return ("its mating face pointed INTO the board interior (unusable in an enclosure); "
            "a connector mates through the board edge")


def _face_connector_out(design: dict, fix: Mapping, ctx: _Context) -> _Outcome:
    ref = fix.get("ref")
    index, comp = _component(design, ref)
    try:
        bbox, by_ref = _probe_parts(ctx.probe)
    except _Declined as e:
        raise _Declined(f"{ref}: {e}") from None
    entry = by_ref.get(ref)
    if entry is None:
        raise _Declined(f"{ref}: the connector probe has no entry for it")
    if bbox is None:
        raise _Declined(f"{ref}: the connector probe gave no board_bbox")
    try:
        placed = npol.face_out(entry, bbox, comp)
    except npol.FaceOutError as e:
        raise _Declined(f"{ref}: {e}") from None
    if placed.get("interior"):
        _replace_component(design, index, {**comp, "interior": True})
        return _Outcome(
            f"{ref}: board-internal header (header family or 1-2 row THT pin grid) -> "
            f"marked interior: true", "med",
            "a vertical pin header / socket mates from above, not through the board edge",
            ("place it at an edge by hand if it must mate there",),
            f"components[{ref}].interior")
    if not _fixed_board(design):
        raise _Declined(f"{ref}: the board is auto-sized (no board.w/h or outline); a fixed "
                        f"connector position needs a fixed board")
    return _placed_outcome(design, index, comp, placed, fix, npol.facing_edge(entry, bbox))


def _placed_outcome(design: dict, index: int, comp: dict, placed: dict, fix: Mapping,
                    edge: str) -> _Outcome:
    ref = comp["ref"]
    if "at" in placed:
        new = {k: v for k, v in comp.items() if k not in ("x", "y", "rot")}
        new["at"] = placed["at"]
        where, override = f"at={placed['at']}", f"components[{ref}].at"
    else:
        new = {k: v for k, v in comp.items() if k != "at"}
        new.update(x=placed["x"], y=placed["y"], rot=placed["rot"])
        where = f"x={placed['x']} y={placed['y']} rot={placed['rot']}"
        override = f"components[{ref}].x/y/rot"
    _replace_component(design, index, new)
    return _Outcome(f"{ref}: turned to face out of the {edge} board edge ({where})", "med",
                    _face_rationale(fix),
                    ("mark it 'interior': true if it does not mate through the edge",
                     "place it by hand (x/y/rot or at)"), override)


_APPLIERS: Mapping[str, Callable[[dict, Mapping, _Context], _Outcome]] = {
    "add_decoupling": _add_decoupling,
    "declare_no_connect": _declare_no_connect,
    "net_same_name_pin": _net_same_name_pin,
    "swap_thermal_footprint": _swap_thermal_footprint,
    "face_connector_out": _face_connector_out,
}


# --- entry points --------------------------------------------------------------------------
def apply_structured(design: dict, tasks: list, *, gate: str,
                     footprint_exists: FootprintExists | None = None,
                     connector_probe: Mapping | None = None
                     ) -> tuple[dict, list[Remedy], list[Skip]]:
    """Apply every auto task with a known applier to a deep copy of
    ``design``. Returns (new_design, remedies, skipped). ``footprint_exists``
    ('Lib:Name' -> bool) gates swap_thermal_footprint; ``connector_probe`` is
    the swig_connector_probe JSON ({ok, board_bbox, connectors: [entry, ...]}
    or connectors keyed by ref) that face_connector_out needs."""
    d = copy.deepcopy(design)
    ctx = _Context(footprint_exists, connector_probe)
    remedies: list[Remedy] = []
    skipped: list[Skip] = []
    for t in tasks:
        fix = t.get("fix") or {}
        fn = _APPLIERS.get(fix.get("type")) if t.get("auto") else None
        if fn is None:
            continue
        try:
            out = fn(d, fix, ctx)
        except _Declined as e:
            skipped.append(Skip(gate, t.get("desc", ""), dict(fix), str(e)))
            continue
        policy = t.get("policy") or _DEFAULT_POLICY.get(fix["type"])
        remedies.append(Remedy(gate, out.text, policy, out.confidence, out.rationale,
                               out.alternatives, out.override))
    return d, remedies, skipped


def apply(design: dict, tasks: list) -> tuple[dict, list]:
    """Apply every auto task with a known applier. Returns (new_design, log).
    Tasks whose fix type has no applier, or that an applier declines, are
    skipped (they stay open)."""
    new, remedies, _ = apply_structured(design, tasks, gate="G2")
    return new, [r.text for r in remedies]
