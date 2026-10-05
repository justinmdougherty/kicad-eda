#!/usr/bin/env python3
"""Library source of every part for the review report (ROADMAP D5, D1).

``library_sources(design, netlist_dir, stock_symbols_dir,
stock_footprints_dir)`` -> ``{ref: {"symbol": src, "footprint": src}}`` with
src one of SOURCES:

  * ``stock``: the install's stock library (``<symbols>/<nick>.kicad_sym``,
    ``<footprints>/<nick>.pretty``);
  * ``project``: a project ``sym-lib-table`` / ``fp-lib-table`` row beside
    the netlist, or the netlist's own ``libraries`` block (libtable's
    "netlist" source, shown as project like review_report.SOURCE_ALIASES);
  * ``generated``: authored by this plugin, whatever table holds it: the
    resolved symbol library file, or the part's own
    ``<lib>.pretty/<name>.kicad_mod``, carries ``(generator "kicad-eda")``
    in its header (libtable.library_generator; fp_author / sym_author write
    it). The footprint check is per part: a hand-made footprint in the same
    .pretty stays project;
  * ``unknown``: the nickname resolves nowhere, only to a disabled row, or
    to a declared library whose file is not on disk (lint reports why).

Resolution is libtable.LibraryResolver.for_design (block > project table >
stock). A board-only part (no ``lib_id``) gets only a footprint entry; a
reference without ``nick:name`` gets no entry for that kind (as
review_report.library_sources_for). A malformed block or project table, or
an unreadable library file, raises libtable.LibTableError. Shared by the
review_report MCP tool (server/review_tools.py) and the harness review.

Stdlib + libtable only; never mutates its inputs.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Mapping

import libtable

STOCK, PROJECT, GENERATED, UNKNOWN = "stock", "project", "generated", "unknown"
SOURCES: tuple[str, ...] = (STOCK, PROJECT, GENERATED, UNKNOWN)
RESOLVER_SOURCES: Mapping[str, str] = {libtable.STOCK: STOCK, libtable.PROJECT: PROJECT,
                                       libtable.NETLIST: PROJECT}
_FOOTPRINT_FILE_SUFFIX = ".kicad_mod"
_KINDS: tuple[tuple[str, str], ...] = ((libtable.SYMBOL, "lib_id"),
                                       (libtable.FOOTPRINT, "footprint"))

_PathArg = str | os.PathLike


def _split(lib_ref: Any) -> tuple[str, str] | None:
    """(nickname, item name) of ``nick:name``; None for anything else."""
    if not isinstance(lib_ref, str) or ":" not in lib_ref:
        return None
    nick, name = lib_ref.split(":", 1)
    return (nick, name) if nick else None


def _generated(path: Path, cache: dict[Path, bool]) -> bool:
    """Whether the library file at ``path`` says this plugin authored it."""
    if path not in cache:
        cache[path] = libtable.library_generator(path) == libtable.KICAD_EDA_GENERATOR
    return cache[path]


def _source(resolver: libtable.LibraryResolver, kind: str, nick: str, name: str,
            cache: dict[Path, bool]) -> str:
    hit = resolver.resolve(kind, nick)
    if hit is None or not hit.exists:
        return UNKNOWN
    target = (hit.path if kind == libtable.SYMBOL
              else hit.path / f"{name}{_FOOTPRINT_FILE_SUFFIX}")
    if _generated(target, cache):
        return GENERATED
    return RESOLVER_SOURCES[hit.source]


def _components(design: Mapping) -> list[Mapping]:
    comps = design.get("components")
    return [c for c in comps if isinstance(c, Mapping)] if isinstance(comps, list) else []


def library_sources(design: Mapping, netlist_dir: _PathArg,
                    stock_symbols_dir: _PathArg | None,
                    stock_footprints_dir: _PathArg | None) -> dict[str, dict[str, str]]:
    """``{ref: {"symbol": src, "footprint": src}}`` for every component of
    ``design`` (see the module doc for the sources). ``netlist_dir`` anchors
    the netlist ``libraries`` block and holds the project tables; a stock
    dir of None means no stock library of that kind. Raises
    libtable.LibTableError for a malformed block, project table or design."""
    resolver = libtable.LibraryResolver.for_design(netlist_dir, design, stock_symbols_dir,
                                                   stock_footprints_dir)
    cache: dict[Path, bool] = {}
    out: dict[str, dict[str, str]] = {}
    for comp in _components(design):
        entry: dict[str, str] = {}
        for kind, key in _KINDS:
            parts = _split(comp.get(key))
            if parts is not None:
                entry[kind] = _source(resolver, kind, parts[0], parts[1], cache)
        out[str(comp.get("ref", "?"))] = entry
    return out
