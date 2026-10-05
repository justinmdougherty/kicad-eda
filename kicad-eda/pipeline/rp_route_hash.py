"""The route-cache key: a DECLARATIVE registry of what moves copper.

route_hash(design, netlist_dir) hashes exactly the registry() keys; when it
matches the last successful run's manifest, the harness reuses the routed
board and skips build + route (the G3/G4 gates still re-run). The core keys
are the tuples below. A pipeline feature (pipeline/rp_feature_<x>.py, see
rp_hooks) declares its own ROUTE_HASH_COMPONENT_KEYS / ROUTE_HASH_DESIGN_KEYS
(netlist inputs that move copper) and ROUTE_HASH_EXCLUDED (keys that never do,
with the reason) as module tuples, which registry() merges; what is not a
netlist key (a DXF's bytes, a footprint digest) goes through its
route_hash_extra hook as the sorted 'extra' component. With no feature the
hash is exactly the core one.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any, NamedTuple

import rp_paths  # noqa: F401  first: puts server/ and server/tools/ on sys.path
import rp_hooks

# Per-component: position (x/y/rot, or the D3 origin anchor "at"), footprint,
# side and the packer hints (interior, edge_row, overhang past Edge.Cuts).
ROUTE_HASH_COMPONENT_KEYS: tuple[str, ...] = (
    "ref", "footprint", "x", "y", "rot", "side", "interior", "edge_row", "overhang", "at")
# Design-level paths; a dotted path hashes ONE key of a block whose other keys
# are review-only (thermal.ep_vias_min sizes the EP via farm; t_ambient and
# tj_margin only feed G2). planes = inner pours, stackup = impedance widths.
ROUTE_HASH_DESIGN_KEYS: tuple[str, ...] = (
    "nets", "net_specs", "preroutes", "board", "mounting_holes", "antenna_keepout",
    "class", "emc", "planes", "stackup", "copper_layers", "thermal.ep_vias_min")
# Deliberately NOT hashed — they never move copper, and the G3/G4 gates re-run
# against a cached board anyway: height_zones is a G3 check, not a placement
# input; the design-level thermal block, part values and sourcing feed G2/G5.
ROUTE_HASH_EXCLUDED: tuple[str, ...] = (
    "height_zones", "thermal", "title", "name", "require_sourcing", "no_connect",
    "power_flags", "assumptions", "mode")
# the payload's own top-level keys: no design key may be named like them
RESERVED: tuple[str, ...] = ("components", "extra")
_FIELDS = ("ROUTE_HASH_COMPONENT_KEYS", "ROUTE_HASH_DESIGN_KEYS", "ROUTE_HASH_EXCLUDED")
_CORE = "rp_route_hash"


class Registry(NamedTuple):
    """The merged route-hash registry (core keys first, then feature order)."""
    component_keys: tuple[str, ...]
    design_keys: tuple[str, ...]
    excluded: tuple[str, ...]


def _declared(owner: str, source: Any, attr: str) -> tuple[str, ...]:
    keys = getattr(source, attr, ())
    if not (isinstance(keys, tuple) and all(isinstance(key, str) for key in keys)):
        raise rp_hooks.FeatureError(f"pipeline feature '{owner}': {attr} must be a tuple of "
                                    f"str, got {keys!r}")
    return keys


def _who(owner: str) -> str:
    return owner if owner == _CORE else f"feature '{owner}'"


def registry() -> Registry:
    """The core registry merged with every active feature's ROUTE_HASH_*
    tuples (a key declared twice is kept once). A key both hashed and
    excluded, or a design key named like a payload key, raises
    rp_hooks.FeatureError naming who declared it."""
    sources = [(_CORE, sys.modules[__name__]),
               *((rp_hooks.feature_name(f), f) for f in rp_hooks.features())]
    owners: dict[tuple[str, str], str] = {}
    for owner, source in sources:
        for attr in _FIELDS:
            for key in _declared(owner, source, attr):
                owners.setdefault((attr, key), owner)
    merged = {attr: tuple(key for a, key in owners if a == attr) for attr in _FIELDS}
    reg = Registry(*(merged[attr] for attr in _FIELDS))
    for key in reg.design_keys:
        if key in reg.excluded:
            raise rp_hooks.FeatureError(
                f"route-hash key '{key}' is both hashed (by "
                f"{_who(owners[('ROUTE_HASH_DESIGN_KEYS', key)])}) and excluded (by "
                f"{_who(owners[('ROUTE_HASH_EXCLUDED', key)])})")
        if key in RESERVED:
            raise rp_hooks.FeatureError(f"route-hash key '{key}' (by "
                                        f"{_who(owners[('ROUTE_HASH_DESIGN_KEYS', key)])}) "
                                        f"is reserved for the hash payload itself")
    return reg


def dig(design: dict, path: str) -> Any:
    """The value at a dotted registry path ("thermal.ep_vias_min"); None when
    a level is absent or is not a JSON object."""
    node: Any = design
    for part in path.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    return node


def route_hash(design: dict, netlist_dir: str | Path | None = None) -> str:
    """Hash of everything that determines PLACEMENT + ROUTING (registry()
    above, plus the features' route_hash_extra values when there are any). A
    change to a value, sourcing field, or thermal annotation does not move
    copper — only registry keys force the expensive build+route stages to
    re-run."""
    reg = registry()
    comps = sorted(({key: c.get(key) for key in reg.component_keys if key in c}
                    for c in design.get("components", [])),
                   key=lambda c: c["ref"])
    payload = {"components": comps,
               **{path: dig(design, path) for path in reg.design_keys}}
    extra = rp_hooks.merged("route_hash_extra", design,
                            None if netlist_dir is None else Path(netlist_dir))
    if extra:
        payload = {**payload, "extra": dict(sorted(extra.items()))}
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True).encode()).hexdigest()
