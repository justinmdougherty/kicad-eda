"""Pipeline feature 'libs' (F2-15, ROADMAP D1): project-local libraries in the
gated run, through rp_hooks hooks only (no stage module is edited). The state
is the project_libs.ProjectLibs of the netlist in ctx.ext['libs'].

  on_load           project_libs.load(netlist, design) -> ctx.ext['libs'];
                    report['libraries'] = project_libs.sources ({ref: {symbol,
                    footprint}}: stock | project | generated | unknown); a report
                    that cannot be made is report['libraries_error'], never a stop
  lint_kwargs       {'ctx': project_libs.lint_context(libs)}: the resolver, the
                    load error, the netlist dir (the outline and import lint
                    rules read it) and the SWIG footprint-version limit; the
                    libraries are reloaded here, after every g1_prepare hook (a
                    later feature may declare a library), for the hooks after it
  g1_verdict        a library error (each declared-library problem, or why
                    nothing loaded) is a G1 blocker naming the nickname and path,
                    unless lint (LIB-E1 / LIB-E2) already blocks with it: the run
                    stops at G1, as the schematic stage would refuse it
  analysis_kwargs   {'symbols': resolver} for design_report.analyze, so project
                    parts get their pin types; {} (the harness's stock dir) when
                    the libraries did not load or the stock symbols are unknown
                    (lint_context's rule: a resolver without stock symbols would
                    fail every stock part)
  g2_verdict        report['libraries'] again, for the design G2 settled on (a
                    remediation may add parts); the verdict is unchanged
  route_hash_extra  {'footprint_digest': project_libs.footprint_digest}: the
                    bytes of every footprint the board build loads (stock
                    included), so editing a project .kicad_mod invalidates the
                    route cache; {} without a netlist dir (nothing to resolve from)

ROUTE_HASH_EXCLUDED documents that the 'libraries' block never moves copper by
itself: its effect is the footprint digest, so a nickname pointed at an
identical copy of its library keeps the hash (no test enforces the registry
classification; route_hash ignores unlisted keys)."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import rp_paths  # noqa: F401  first: puts server/ and server/tools/ on sys.path
import project_libs

ORDER = 10
EXT_KEY = "libs"                         # ctx.ext key = the feature name (rp_hooks)
ROUTE_HASH_EXCLUDED: tuple[str, ...] = ("libraries",)
REPORT_KEY = "libraries"
REPORT_ERROR_KEY = "libraries_error"
BLOCKER_PREFIX = "project libraries (ROADMAP D1): "
# route_hash_extra knows the folder only: the netlist path its messages would name
HASH_NETLIST_NAME = "netlist.json"


def load_into(ctx: Any) -> project_libs.ProjectLibs:
    """Load the libraries of ctx.design (as it stands now) into ctx.ext['libs']."""
    libs = project_libs.load(ctx.netlist, ctx.design)
    ctx.ext[EXT_KEY] = libs
    return libs


def libs_of(ctx: Any) -> project_libs.ProjectLibs:
    """The run's project libraries: ctx.ext['libs'], loaded on first use
    (on_load normally has; a stage driven on its own may not)."""
    libs = ctx.ext.get(EXT_KEY)
    return libs if libs is not None else load_into(ctx)


# --- the library report ------------------------------------------------------------------
def library_report(libs: project_libs.ProjectLibs, design: Any) -> dict[str, Any]:
    """{'libraries': sources} or, when they cannot be made, {'libraries_error': why}."""
    try:
        return {REPORT_KEY: project_libs.sources(libs, design)}
    except Exception as e:  # noqa: BLE001 - a report problem is recorded, never stops a run
        return {REPORT_ERROR_KEY: f"{type(e).__name__}: {e}"}


def _report(ctx: Any) -> None:
    for key in (REPORT_KEY, REPORT_ERROR_KEY):
        ctx.report.pop(key, None)
    ctx.report.update(library_report(libs_of(ctx), ctx.design))


def on_load(ctx: Any) -> None:
    """The on_load hook: load the libraries and report each part's source."""
    load_into(ctx)
    _report(ctx)


def g2_verdict(ctx: Any, verdict: dict) -> None:
    """The g2_verdict hook: the library report of the design G2 settled on."""
    _report(ctx)


# --- G1 ------------------------------------------------------------------------------------
def lint_kwargs(ctx: Any) -> dict[str, Any]:
    """The lint_kwargs hook: the project-library LintContext of the design
    every g1_prepare hook settled on (reloaded: a later feature may declare a
    library there); g1_verdict and the G2 hooks use the same libraries."""
    return {"ctx": project_libs.lint_context(load_into(ctx))}


def load_errors(libs: project_libs.ProjectLibs) -> list[str]:
    """Each library problem of ``libs`` as one message ([] when it loaded clean)."""
    if not libs.error:
        return []
    found = libs.resolver.errors() if libs.resolver is not None else []
    return list(found) or [libs.error]


def g1_verdict(ctx: Any, verdict: dict) -> dict | None:
    """The g1_verdict hook: every library error lint has not already blocked
    with becomes a G1 blocker (a new verdict); None leaves the verdict."""
    blockers = verdict["blockers"]
    new = [BLOCKER_PREFIX + m for m in load_errors(libs_of(ctx))
           if not any(m in b for b in blockers)]
    if not new:
        return None
    return {**verdict, "passed": False, "blockers": [*blockers, *new]}


# --- G2 ------------------------------------------------------------------------------------
def analysis_kwargs(ctx: Any) -> dict[str, Any]:
    """The analysis_kwargs hook: the resolver as design_report's symbol source."""
    libs = libs_of(ctx)
    if libs.resolver is None or not libs.stock_symbols:
        return {}
    return {"symbols": libs.resolver}


# --- route hash ------------------------------------------------------------------------------
def route_hash_extra(design: dict, netlist_dir: Path | None) -> dict[str, str]:
    """The route_hash_extra hook: the footprint digest of the design's
    libraries as resolved from ``netlist_dir`` (with the stock footprints the
    board build uses); {} without a netlist dir."""
    if netlist_dir is None:
        return {}
    libs = project_libs.load(Path(netlist_dir) / HASH_NETLIST_NAME, design)
    return {"footprint_digest": project_libs.footprint_digest(libs, design)}
