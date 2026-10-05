#!/usr/bin/env python3
"""LintContext: what every lint rule module gets besides the design.

lint_netlist builds one per lint() call and hands the same frozen object to
each discovered ``lint_rules_*.check(design, ctx)``. Stdlib-only and Python
3.11-safe (KiCad's bundled Python may import it).

Fields:
  symbols              the symbol source: a symbols directory (str / PathLike),
                       a libtable.LibraryResolver or a library_map() dict;
                       '' = none (the symbol-based rules, e.g. W2, are skipped)
  resolver             the project LibraryResolver, None in dir mode
  library_error        why the project libraries could not be loaded ('' = fine)
  netlist_dir          the netlist.json directory (a Path; None = unknown), the
                       base of relative paths in the design (outline DXF, sources)
  swig_fp_version_max  the newest footprint (version N) the SWIG pcbnew loads,
                       None = not probed
  options              read-only extra settings (a MappingProxyType copy)
"""
from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any

from libtable import MAP_KEYS, SYMBOL

_NO_OPTIONS: Mapping[str, Any] = MappingProxyType({})


def is_resolver(value: object) -> bool:
    """A LibraryResolver (duck-typed: anything with ``resolve`` and
    ``symbol_lib_path``, so lint_context need not construct one)."""
    return callable(getattr(value, "resolve", None)) and \
        callable(getattr(value, "symbol_lib_path", None))


def _is_symbol_source(value: object) -> bool:
    return isinstance(value, (str, os.PathLike, Mapping)) or is_resolver(value)


def symbols_dir_of(symbols: object) -> str | None:
    """The symbols directory a source names: the directory itself, or the
    stock symbols directory of a resolver / library map; None when it has none."""
    if isinstance(symbols, (str, os.PathLike)):
        return os.fspath(symbols) or None
    if isinstance(symbols, Mapping):
        stock = symbols.get("stock")
        value = stock.get(MAP_KEYS[SYMBOL]) if isinstance(stock, Mapping) else None
    else:
        dirs = getattr(symbols, "stock_dirs", None)
        value = dirs.get(SYMBOL) if isinstance(dirs, Mapping) else None
    return os.fspath(value) if value else None


@dataclass(frozen=True)
class LintContext:
    """The lint run's context (see the module doc); validated on creation."""
    symbols: Any = ""
    resolver: Any = None
    library_error: str = ""
    netlist_dir: Path | None = None
    swig_fp_version_max: int | None = None
    options: Mapping[str, Any] = field(default_factory=lambda: _NO_OPTIONS)

    def __post_init__(self) -> None:
        if not _is_symbol_source(self.symbols):
            raise TypeError(f"LintContext.symbols must be a symbols directory, a "
                            f"LibraryResolver or a library map, got {self.symbols!r}")
        if self.resolver is not None and not is_resolver(self.resolver):
            raise TypeError(f"LintContext.resolver must be a LibraryResolver or None, "
                            f"got {self.resolver!r}")
        if not isinstance(self.library_error, str):
            raise TypeError(f"LintContext.library_error must be a str, "
                            f"got {self.library_error!r}")
        version = self.swig_fp_version_max
        if version is not None and (isinstance(version, bool) or not isinstance(version, int)):
            raise TypeError(f"LintContext.swig_fp_version_max must be an int or None, "
                            f"got {version!r}")
        if not isinstance(self.options, Mapping):
            raise TypeError(f"LintContext.options must be a mapping, got {self.options!r}")
        object.__setattr__(self, "netlist_dir", _as_dir(self.netlist_dir))
        object.__setattr__(self, "options", MappingProxyType(dict(self.options)))

    @property
    def symbols_dir(self) -> str | None:
        """The symbols directory of ``symbols`` (see symbols_dir_of)."""
        return symbols_dir_of(self.symbols)


def _as_dir(value: object) -> Path | None:
    if value is None or value == "":
        return None
    if not isinstance(value, (str, os.PathLike)):
        raise TypeError(f"LintContext.netlist_dir must be a path or None, got {value!r}")
    return Path(value)


def context_for(symbols: object = "") -> LintContext:
    """The LintContext for lint()'s ``symbols_dir`` argument: a LintContext is
    returned as is; '' / None give the empty context; a resolver is also the
    context's resolver and names its netlist_dir; a library map names its
    netlist_dir; a directory is just the symbol source."""
    if isinstance(symbols, LintContext):
        return symbols
    if symbols is None or symbols == "":
        return LintContext()
    if is_resolver(symbols):
        return LintContext(symbols=symbols, resolver=symbols,
                           netlist_dir=getattr(symbols, "netlist_dir", None))
    if isinstance(symbols, Mapping):
        return LintContext(symbols=symbols, netlist_dir=symbols.get("netlist_dir") or None)
    return LintContext(symbols=symbols)
