"""tline: closed-form transmission-line models (ADR D7.6), stdlib only.

The package is a registry of line models keyed by (kind, pair):

    register(kind, pair, fn)      fn(geom: LineGeometry, freq_hz: float) -> LineResult
    evaluate(geom, freq_hz=0.0)   dispatch on (geom.kind, geom.pair)
    registered()                  the sorted registered keys

Each model module registers its kinds when imported. This file imports the FIXED module list
MODEL_MODULES once and skips a module only while its file does not exist (ModuleNotFoundError
naming exactly that submodule); any other import failure propagates. So a later task adds a
model by creating its module file - never by editing this file (task fix D7-04/05).

Runs as package `tline` with server/tools on sys.path, in the venv and in KiCad's Python.
Never put server/tools/tline itself on sys.path: tline/types.py would shadow stdlib `types`.
"""
from __future__ import annotations

import importlib
import math
from collections.abc import Callable, Mapping, Sequence
from types import MappingProxyType

from .types import PAIRS, Issue, LineGeometry, LineResult, MaskModel, Ply

__all__ = ["MODEL_MODULES", "LOADED", "Issue", "LineGeometry", "LineResult", "MaskModel", "Ply",
           "evaluate", "load_models", "register", "registered"]

Evaluator = Callable[[LineGeometry, float], LineResult]

MODEL_MODULES = ("microstrip", "coplanar", "stripline", "broadside", "coupled_microstrip",
                 "fallback")

# kept across importlib.reload(tline): the cached model submodules do not register again
_REGISTRY: Mapping[tuple[str, str], Evaluator] = globals().get("_REGISTRY", MappingProxyType({}))


def register(kind: str, pair: str, fn: Evaluator) -> None:
    """Register fn as the model for (kind, pair). A key can be registered once (ValueError)."""
    global _REGISTRY
    if not isinstance(kind, str) or not kind:
        raise ValueError(f"kind must be a non-empty string, got {kind!r}")
    if pair not in PAIRS:
        raise ValueError(f"pair must be one of {PAIRS}, got {pair!r}")
    if not callable(fn):
        raise TypeError(f"model for ({kind!r}, {pair!r}) is not callable: {fn!r}")
    key = (kind, pair)
    if key in _REGISTRY:
        raise ValueError(f"a model for ({kind!r}, {pair!r}) is already registered: "
                         f"{_REGISTRY[key]!r}")
    _REGISTRY = MappingProxyType({**_REGISTRY, key: fn})


def registered() -> tuple[tuple[str, str], ...]:
    """Every registered (kind, pair), sorted."""
    return tuple(sorted(_REGISTRY))


def evaluate(geom: LineGeometry, freq_hz: float = 0.0) -> LineResult:
    """Evaluate the closed-form model registered for (geom.kind, geom.pair) at freq_hz
    (0 = quasi-static). NotImplementedError when no model is registered for the key."""
    if not isinstance(geom, LineGeometry):
        raise TypeError(f"evaluate() needs a LineGeometry, got {type(geom).__name__}")
    if not (isinstance(freq_hz, (int, float)) and math.isfinite(freq_hz) and freq_hz >= 0.0):
        raise ValueError(f"freq_hz must be finite and >= 0, got {freq_hz!r}")
    fn = _REGISTRY.get((geom.kind, geom.pair))
    if fn is None:
        known = ", ".join(f"{k}/{p}" for k, p in registered()) or "none"
        raise NotImplementedError(f"no transmission-line model registered for kind "
                                  f"{geom.kind!r} pair {geom.pair!r} (registered: {known})")
    result = fn(geom, float(freq_hz))
    if not isinstance(result, LineResult):
        raise TypeError(f"model for ({geom.kind!r}, {geom.pair!r}) returned "
                        f"{type(result).__name__}, expected LineResult")
    return result


def load_models(names: Sequence[str] = MODEL_MODULES) -> tuple[str, ...]:
    """Import the named model submodules; return those imported. A submodule whose own file
    is absent is skipped; every other error (a missing dependency included) propagates."""
    loaded = []
    for name in names:
        full = f"{__name__}.{name}"
        try:
            importlib.import_module(full)
        except ModuleNotFoundError as exc:
            if exc.name != full:
                raise
            continue
        loaded.append(name)
    return tuple(loaded)


LOADED = load_models()
