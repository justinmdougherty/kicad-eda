#!/usr/bin/env python3
"""Rule areas (keep-outs and named areas) on a pcbnew board (ADR D7.5, D7.7, D7.8, D7.10).

Built on board_outline (F3-01), the ONE pcbnew 9/10 compat layer: the "no
copper pour" flag is read with ``bo.blocks_pour`` and set by
``board_shapes.add_keepout_polygon`` through ``bo.set_no_pour`` (CopperPour on
9.x, ZoneFills on 10.x); "every enabled copper layer" is ``bo.copper_lset``;
outlines are filled in place (``bo.fill_zone_outline``). No second shim here.

* ``add_rule_area(board, polygon_mm, layers="all", *, name, no_tracks, no_vias,
  no_pads, no_pour, no_footprints)``: a rule area over ``polygon_mm`` (sheet mm
  points, or a SHAPE_POLY_SET). Every flag defaults to False (a named area that
  forbids nothing, e.g. RFCOR / RFREF); returns the ZONE, never None; ValueError
  for a bad name, polygon or layer.
* ``find_rule_areas(board, pattern=None, *, include_footprints=False)``: records
  ``{name, footprint (ref or None), layers (enabled copper it covers, stack
  order), flags {no_tracks, no_vias, no_pads, no_pour, no_footprints}}``.
* ``has_rule_area(board, pattern, layers, *, <flag>=None, include_footprints=False)``:
  one matching area covers ``layers`` and has every flag that is not None.
* ``remove_rule_areas(board, pattern)``: removes the matching BOARD areas (never
  a footprint's own) and returns their names; a pattern is required.

Layers: ``"all"`` = every enabled copper layer; or a list of copper layer
names, each enabled (checked against the enabled copper, never GetLayerID
alone: it returns a valid id for In1.Cu on a 2-layer board). Patterns: None =
any name; with ``*``, ``?`` or ``[`` an fnmatch glob (case-sensitive); else a
name prefix. Runs under KiCad's bundled Python (9.0.4: 3.11; the 10.0.5 image:
/usr/bin/python3); without pcbnew (the dev venv) only the pure helpers work.
"""
from __future__ import annotations

import fnmatch
import math
import sys
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import kicad_quiet  # noqa: E402,F401  (before pcbnew: no modal wx dialogs)

try:
    import pcbnew  # noqa: E402
except ImportError:          # the dev venv: pure helpers only
    pcbnew = None

import board_outline as bo  # noqa: E402

ALL = "all"
FLAGS = ("no_tracks", "no_vias", "no_pads", "no_pour", "no_footprints")
_GETTERS = {"no_tracks": "GetDoNotAllowTracks", "no_vias": "GetDoNotAllowVias",
            "no_pads": "GetDoNotAllowPads", "no_footprints": "GetDoNotAllowFootprints"}
_GLOB = frozenset("*?[")
Point = tuple[float, float]


def _pcb() -> Any:
    if pcbnew is None:
        raise RuntimeError("pcbnew is not importable: this helper runs under KiCad's "
                           "bundled Python")
    return pcbnew


# --- pure helpers -------------------------------------------------------------------------
def name_matches(name: str, pattern: str | None) -> bool:
    """None / "" match every name; a glob (``*``, ``?``, ``[``) is matched with
    fnmatchcase; any other pattern is a name prefix (case-sensitive)."""
    if not pattern:
        return True
    if _GLOB & set(pattern):
        return fnmatch.fnmatchcase(name, pattern)
    return name.startswith(pattern)


def resolve_layers(spec: Any, enabled: Sequence[str]) -> tuple[str, ...]:
    """``"all"`` -> ``enabled``; a list of names -> those names in ``enabled``
    (stack) order, duplicates dropped. ValueError for anything else, an empty
    list, or a name that is not an enabled copper layer."""
    if spec == ALL:
        return tuple(enabled)
    if isinstance(spec, (str, bytes)) or not isinstance(spec, (list, tuple)):
        raise ValueError(f"layers is 'all' or a list of copper layer names, got {spec!r}")
    if not spec:
        raise ValueError("layers needs at least one copper layer name (or 'all')")
    if not all(isinstance(n, str) for n in spec):
        raise ValueError(f"layers must be copper layer names, got {list(spec)!r}")
    bad = [n for n in spec if n not in enabled]
    if bad:
        raise ValueError(f"{', '.join(bad)} is not an enabled copper layer of this "
                         f"{len(enabled)}-layer board (enabled: {', '.join(enabled)})")
    return tuple(n for n in enabled if n in spec)


def _finite(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def polygon_points(points: Any) -> tuple[Point, ...]:
    """At least 3 finite (x, y) points (sheet mm) as float tuples; ValueError otherwise."""
    if isinstance(points, (str, bytes)) or not isinstance(points, Iterable):
        raise ValueError(f"a rule-area polygon is a list of (x, y) points, got {points!r}")
    pts = list(points)
    if not all(isinstance(p, (list, tuple)) and len(p) == 2 and all(map(_finite, p))
               for p in pts):
        raise ValueError(f"a rule-area polygon point is a finite (x, y) pair (mm), got {pts!r}")
    if len(pts) < 3:
        raise ValueError(f"a rule-area polygon needs at least 3 points, got {len(pts)}")
    return tuple((float(x), float(y)) for x, y in pts)


def rect_points(x: float, y: float, w: float, h: float) -> tuple[Point, ...]:
    """The four corners of the (x, y, w, h) rectangle (sheet mm), clockwise in Y-down."""
    if not all(map(_finite, (x, y, w, h))) or w <= 0 or h <= 0:
        raise ValueError(f"a rule-area rectangle needs finite x, y and w, h > 0, got "
                         f"{(x, y, w, h)!r}")
    x, y, w, h = float(x), float(y), float(w), float(h)
    return ((x, y), (x + w, y), (x + w, y + h), (x, y + h))


def flags_match(have: Mapping[str, bool], want: Mapping[str, bool | None]) -> bool:
    """Every flag in ``want`` that is not None equals ``have``'s."""
    unknown = sorted(set(want) - set(FLAGS))
    if unknown:
        raise ValueError(f"unknown rule-area flag(s) {unknown} (known: {', '.join(FLAGS)})")
    return all(have[k] == bool(v) for k, v in want.items() if v is not None)


def covers(have: Iterable[str], want: Iterable[str]) -> bool:
    """``have`` includes every layer of ``want``."""
    return set(want) <= set(have)


# --- pcbnew -------------------------------------------------------------------------------
def enabled_copper(board: Any) -> tuple[str, ...]:
    """The board's enabled copper layer names, top to bottom (bo.copper_lset)."""
    _pcb()
    return tuple(board.GetLayerName(lid) for lid in bo.copper_lset(board).CuStack())


def _lset(board: Any, names: Sequence[str]) -> Any:
    ls = _pcb().LSET()
    for name in names:
        ls.AddLayer(board.GetLayerID(name))
    return ls


def add_rule_area(board: Any, polygon_mm: Any, layers: Any = ALL, *, name: str,
                  no_tracks: bool = False, no_vias: bool = False, no_pads: bool = False,
                  no_pour: bool = False, no_footprints: bool = False) -> Any:
    """A rule area named ``name`` over ``polygon_mm`` on ``layers`` (module
    doc) with the given flags; the new ZONE (RuntimeError if pcbnew gives
    none). Everything is validated before the board is touched."""
    pcb = _pcb()
    if not isinstance(name, str) or not name.strip():
        raise ValueError(f"a rule area needs a non-empty name, got {name!r}")
    shape = (polygon_mm if isinstance(polygon_mm, pcb.SHAPE_POLY_SET)
             else list(polygon_points(polygon_mm)))
    names = resolve_layers(layers, enabled_copper(board))
    lset = bo.copper_lset(board) if layers == ALL else _lset(board, names)
    import board_shapes                   # imports pcbnew unconditionally: KiCad Python only
    zone = board_shapes.add_keepout_polygon(board, shape, name, lset, no_tracks=no_tracks,
                                            no_vias=no_vias, no_pour=no_pour, no_pads=no_pads,
                                            no_footprints=no_footprints)
    if zone is None:
        raise RuntimeError(f"pcbnew created no rule area {name!r}")
    return zone


def _flags(zone: Any) -> dict[str, bool]:
    got = {key: bool(getattr(zone, getter)()) for key, getter in _GETTERS.items()}
    return {k: (bo.blocks_pour(zone) if k == "no_pour" else got[k]) for k in FLAGS}


def _record(board: Any, zone: Any, owner: str | None) -> dict[str, Any]:
    layers = [board.GetLayerName(lid) for lid in zone.GetLayerSet().CuStack()
              if board.IsLayerEnabled(lid)]
    return {"name": str(zone.GetZoneName()), "footprint": owner, "layers": layers,
            "flags": _flags(zone)}


def _areas(board: Any, include_footprints: bool) -> list[tuple[Any, str | None]]:
    found = [(z, None) for z in board.Zones() if z.GetIsRuleArea()]
    if include_footprints:
        found += [(z, str(fp.GetReference())) for fp in board.GetFootprints()
                  for z in fp.Zones() if z.GetIsRuleArea()]
    return found


def find_rule_areas(board: Any, pattern: str | None = None, *,
                    include_footprints: bool = False) -> list[dict[str, Any]]:
    """Records (module doc) of the board's rule areas whose name matches
    ``pattern``, then (``include_footprints``) the footprints' own areas."""
    _pcb()
    return [_record(board, z, owner) for z, owner in _areas(board, include_footprints)
            if name_matches(str(z.GetZoneName()), pattern)]


def has_rule_area(board: Any, pattern: str | None, layers: Any, *,
                  no_pour: bool | None = None, no_vias: bool | None = None,
                  no_tracks: bool | None = None, no_pads: bool | None = None,
                  no_footprints: bool | None = None, include_footprints: bool = False) -> bool:
    """True when ONE rule area matching ``pattern`` covers ``layers`` ("all",
    a list of enabled copper names, or None = any layer) and has every flag
    that is not None."""
    want_layers = () if layers is None else resolve_layers(layers, enabled_copper(board))
    want = {"no_pour": no_pour, "no_vias": no_vias, "no_tracks": no_tracks, "no_pads": no_pads,
            "no_footprints": no_footprints}
    return any(covers(r["layers"], want_layers) and flags_match(r["flags"], want)
               for r in find_rule_areas(board, pattern, include_footprints=include_footprints))


def remove_rule_areas(board: Any, pattern: str) -> list[str]:
    """Remove the board's rule areas matching ``pattern`` (a footprint's own
    areas stay); their names, board order. ValueError without a pattern (use
    "*" to mean every board area)."""
    _pcb()
    if not isinstance(pattern, str) or not pattern:
        raise ValueError(f"remove_rule_areas needs a name pattern (a prefix or a glob; '*' = "
                         f"every board rule area), got {pattern!r}")
    doomed = [(z, str(z.GetZoneName())) for z, _ in _areas(board, False)]
    doomed = [(z, name) for z, name in doomed if name_matches(name, pattern)]
    for zone, _ in doomed:
        board.Remove(zone)
    return [name for _, name in doomed]
