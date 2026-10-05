#!/usr/bin/env python3
"""Lint a pad-level netlist.json for the structural mistakes that otherwise only
surface after a full (slow) board+route+DRC run.

The checks live in rule modules: every ``lint_rules_*.py`` beside this file
exposes ``ORDER: int`` and ``check(design, ctx) -> (errors, warnings)`` and is
discovered by rule_modules(), run in (ORDER, module name) order, its results
concatenated. A new rule module registers itself by existing: this file is not
edited to add one. ORDER values in use / reserved:

   0  lint_rules_core     E1-E5, W1, W2: pads, nets, refs, power_in pins
  10  lint_rules_libs     LIB-*: project / stock library integrity
  20  lint_rules_pinmux   PM-*: MCU interface pin functions
  30  lint_rules_outline  EO*/WO*: board outline, cutouts, mechanical
  40  lint_rules_import   IM-*: imported / rebuilt designs
  50  lint_rules_stackup  ST-*: stackup and controlled impedance
  60  lint_rules_rf       RF-*: RF chains, antennas

Every rule gets a deep copy of the design and the same frozen LintContext
(lint_context.py: symbol source, resolver, library error, netlist_dir, ...).
A rule module that cannot be imported, lacks ORDER / check, raises, or
returns anything but two lists of str adds ONE error
``LINT-INTERNAL <module>: <reason>`` and the other modules still run.

Exit code 0 if no errors (warnings allowed), 1 otherwise, 2 on a usage error.

Usage: <python> lint_netlist.py <netlist.json> [symbols_dir] [footprints_dir]
  With a directory, the project libraries are resolved like the pipeline does
  (libtable.LibraryResolver.for_netlist: the netlist's libraries block, the
  project tables beside it, then these stock directories).
"""
from __future__ import annotations

import copy
import importlib.util
import json
import os
import sys
from collections.abc import Callable, Mapping, Sized
from dataclasses import dataclass, replace
from pathlib import Path
from types import ModuleType
from typing import Any

TOOLS_DIR = Path(__file__).resolve().parent
if str(TOOLS_DIR) not in sys.path:        # rule modules import their siblings by name
    sys.path.insert(0, str(TOOLS_DIR))

from lint_context import LintContext, context_for  # noqa: E402

RULE_PREFIX = "lint_rules_"
DEFAULT_ORDER = 100            # where a module without a usable ORDER sorts
EXTERNAL_PREFIX = "_lint_rules_ext_"
USAGE_EXIT = 2
USAGE = "Usage: lint_netlist.py <netlist.json> [symbols_dir] [footprints_dir]"

Findings = tuple[list[str], list[str]]
CheckFn = Callable[[Mapping[str, Any], LintContext], Any]


@dataclass(frozen=True)
class RuleModule:
    """One discovered rule module; ``check`` is None when ``problem`` says why
    the module cannot run."""
    name: str
    order: int
    path: str
    check: CheckFn | None = None
    problem: str = ""


# --- discovery --------------------------------------------------------------------
def _norm(path: str | os.PathLike) -> str:
    return os.path.normcase(os.path.realpath(path))


def _describe(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


def _is_order(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _import_shared(path: Path) -> ModuleType:
    """A rule module beside this file, under its own name: one copy, the same
    object ``import lint_rules_x`` gives."""
    name = path.stem
    loaded = sys.modules.get(name)
    if loaded is not None:
        loaded_file = getattr(loaded, "__file__", None)
        if loaded_file and _norm(loaded_file) == _norm(path):
            return loaded
        raise ImportError(f"module name {name!r} is already taken by {loaded_file}")
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module


def _import_external(path: Path) -> ModuleType:
    """A rule module from another directory (tests): compiled fresh from the
    source on every call (no bytecode cache) under a private name, so it never
    replaces a module of the same name beside this file."""
    name = f"{EXTERNAL_PREFIX}{abs(hash(_norm(path.parent)))}_{path.stem}"
    code = compile(importlib.util.decode_source(path.read_bytes()), str(path), "exec")
    module = ModuleType(name)
    module.__file__ = str(path)
    sys.modules[name] = module           # dataclasses and pickling look modules up here
    try:
        exec(code, module.__dict__)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module


def _import_rule(path: Path, shared: bool) -> ModuleType:
    return _import_shared(path) if shared else _import_external(path)


def _load_rule(path: Path, shared: bool) -> RuleModule:
    name = path.stem
    try:
        module = _import_rule(path, shared)
    except (Exception, SystemExit) as e:  # noqa: BLE001 - a broken module is reported, not fatal
        return RuleModule(name, DEFAULT_ORDER, str(path), problem=f"import failed: {_describe(e)}")
    order, check = getattr(module, "ORDER", None), getattr(module, "check", None)
    missing = ([] if _is_order(order) else ["an integer ORDER"]) + \
        ([] if callable(check) else ["a check(design, ctx) function"])
    if missing:
        return RuleModule(name, order if _is_order(order) else DEFAULT_ORDER, str(path),
                          problem="rule module needs " + " and ".join(missing))
    return RuleModule(name, order, str(path), check=check)


def rule_modules(search_dir: str | os.PathLike | None = None) -> tuple[RuleModule, ...]:
    """Every ``lint_rules_*.py`` in ``search_dir`` (default: this file's
    directory), imported and sorted by (ORDER, module name). A module that
    fails to load is still listed, with ``problem`` set."""
    root = TOOLS_DIR if search_dir is None else Path(search_dir)
    if not root.is_dir():
        raise NotADirectoryError(f"lint rule directory not found: {root}")
    shared = _norm(root) == _norm(TOOLS_DIR)
    rules = [_load_rule(p, shared) for p in sorted(root.glob(f"{RULE_PREFIX}*.py"))
             if p.is_file()]
    return tuple(sorted(rules, key=lambda r: (r.order, r.name)))


# --- running ----------------------------------------------------------------------
def _internal(rule: RuleModule, reason: str) -> str:
    return f"LINT-INTERNAL {rule.name}: {reason}"


def _shape_problem(result: object) -> str:
    """'' when ``result`` is (errors, warnings), two lists/tuples of str."""
    if not isinstance(result, (tuple, list)) or len(result) != 2:
        return f"check() returned {type(result).__name__}, expected (errors, warnings)"
    for label, items in zip(("errors", "warnings"), result, strict=True):
        if not isinstance(items, (list, tuple)) or not all(isinstance(i, str) for i in items):
            return f"check() {label} must be a list of str"
    return ""


def _run_rule(rule: RuleModule, design: Mapping[str, Any], ctx: LintContext) -> Findings:
    if rule.check is None:
        return [_internal(rule, rule.problem)], []
    try:
        result = rule.check(copy.deepcopy(design), ctx)
    except (Exception, SystemExit) as e:  # noqa: BLE001 - one broken rule never stops lint
        return [_internal(rule, _describe(e))], []
    problem = _shape_problem(result)
    if problem:
        return [_internal(rule, problem)], []
    return list(result[0]), list(result[1])


def run_rules(design: Mapping[str, Any], ctx: LintContext,
              rules: tuple[RuleModule, ...]) -> Findings:
    """Each rule's (errors, warnings), concatenated in ``rules`` order."""
    errors: list[str] = []
    warnings: list[str] = []
    for rule in rules:
        errs, warns = _run_rule(rule, design, ctx)
        errors += errs
        warnings += warns
    return errors, warnings


def _context(symbols_dir: object, ctx: LintContext | None) -> LintContext:
    if ctx is None:
        return context_for(symbols_dir)
    if not isinstance(ctx, LintContext):
        raise TypeError(f"lint(ctx=...) takes a LintContext, got {type(ctx).__name__}")
    if symbols_dir and not ctx.symbols:      # the argument fills a context without symbols
        return replace(ctx, symbols=symbols_dir)
    return ctx


def _count(value: object) -> int:
    return len(value) if isinstance(value, Sized) and not isinstance(value, str) else 0


def lint(design: Mapping[str, Any], symbols_dir: object = "", *,
         ctx: LintContext | None = None) -> dict:
    """Run every rule module on ``design``. ``symbols_dir`` is a symbol source
    (a directory, LibraryResolver or library map; '' = none); ``ctx`` a full
    LintContext (its own symbols win; without any, ``symbols_dir`` fills them)."""
    if not isinstance(design, Mapping):
        raise TypeError(f"lint() needs the netlist as a JSON object (dict), "
                        f"got {type(design).__name__}")
    errors, warnings = run_rules(design, _context(symbols_dir, ctx), rule_modules())
    return {"ok": not errors, "errors": errors, "warnings": warnings,
            "components": _count(design.get("components", [])),
            "nets": _count(design.get("nets", {}))}


# --- CLI --------------------------------------------------------------------------
def cli_context(netlist: str | os.PathLike, symbols_dir: str = "",
                footprints_dir: str = "") -> LintContext:
    """The CLI's context: the netlist directory, plus (given a directory) the
    project libraries resolved with LibraryResolver.for_netlist; a library
    error lands in ``library_error`` and the symbols directory is used as is."""
    netlist_dir = Path(os.path.abspath(netlist)).parent
    if not symbols_dir and not footprints_dir:
        return LintContext(netlist_dir=netlist_dir)
    import libtable
    try:
        resolver = libtable.LibraryResolver.for_netlist(netlist, symbols_dir or None,
                                                        footprints_dir or None)
    except (libtable.LibTableError, OSError, ValueError) as e:
        return LintContext(symbols=symbols_dir, library_error=str(e), netlist_dir=netlist_dir)
    return LintContext(symbols=resolver if symbols_dir else "", resolver=resolver,
                       netlist_dir=netlist_dir)


def _read_design(path: Path) -> dict | None:
    try:
        design = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as e:
        print(f"lint_netlist: cannot read {path}: {e}", file=sys.stderr)
        return None
    if not isinstance(design, dict):
        print(f"lint_netlist: {path} is not a JSON object", file=sys.stderr)
        return None
    return design


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else list(argv)
    if not 1 <= len(args) <= 3:
        print(USAGE, file=sys.stderr)
        return USAGE_EXIT
    design = _read_design(Path(args[0]))
    if design is None:
        return USAGE_EXIT
    rep = lint(design, ctx=cli_context(*args))
    print(json.dumps(rep, indent=2))
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
