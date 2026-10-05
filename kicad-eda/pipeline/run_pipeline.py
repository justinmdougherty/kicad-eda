#!/usr/bin/env python3
"""Gated pipeline harness: drive a netlist through the concept->Gerbers flow
with FORMAL GO/NO-GO GATES between stages (IEEE 15288.2 review structure).
The pipeline halts at the first failing gate — a design with open blockers
never proceeds to the next stage, so an error caught at the schematic gate
can never survive to fabrication.

  G1 Requirements (SRR)  ->  G2 Schematic (PDR)  ->  G3 Placement (hard gate)
  ->  G4 Layout (CDR)  ->  G5 Fab package (release)

run() walks STAGES, functions of one shared RunContext that return False to
stop; rp_context.halt is the single exit and writes <stem>_pipeline_report.json
next to the netlist on every exit (pass, gate failure or crash).

Module map (pipeline/; each is a top-level import, and the rp_ prefix keeps
the names clear of the modules in server/ and server/tools/):
  run_pipeline.py        STAGES, run(), main() and the aliases run_all.py uses
  rp_paths.py            puts server/ and server/tools/ on sys.path (imported first)
  rp_cli.py              command line: parse_cli, board_dims, UsageError, USAGE
  rp_context.py          RunContext, Attempt, record_gate, halt, file suffixes
  rp_kicad.py            KiCad subprocess wrappers: drc, render, gate_check,
                         board_fix, emc_stitch, courtyard_drc, board_cli
  rp_route_hash.py       the route-cache key registry and route_hash
  rp_stage_schematic.py  stage_load, G1 (netlist lint), G2 (schematic + review loop)
  rp_stage_place.py      the route cache, G3 placement and the pack_scale ladder
  rp_route.py            Freerouting attempts, near-miss heal, routed snapshots
  rp_stage_layout.py     G4 judge, board-fix loop, EMC stitching
  rp_stage_fab.py        the cache manifest, G5 fab package and sourcing audit

Rules for changing the harness:
  - Stage modules call helpers through module objects (rp_kicad.drc(...),
    k.autoroute_board(...), rp_context.record_gate(...)), never
    'from rp_kicad import drc', so a test patches one place.
  - A feature adds a stage as a function in its own
    pipeline/rp_stage_<feature>.py plus one STAGES line in this file.
  - Route-hash keys go in rp_route_hash.py.
  - KiCad subprocess wrappers go in rp_kicad.py.

Usage:
  <venv-python> run_pipeline.py <netlist.json> [<board_w> <board_h>] [placement.json]
  (no dims: the netlist "board" w/h, else the board is auto-sized to its parts)
"""
from __future__ import annotations

import json
import sys
from typing import Callable

import rp_paths  # first: puts server/ and server/tools/ on sys.path
import kicad_server as k  # noqa: F401  (alias: run_all.py uses rp.k)
import toolchain as tc  # noqa: F401  (alias for callers)
import rp_cli
import rp_context
import rp_kicad
import rp_route
import rp_route_hash
import rp_stage_fab
import rp_stage_layout
import rp_stage_place
import rp_stage_schematic

# The gated sequence. Each stage returns False to stop the run.
STAGES: tuple[Callable[[rp_context.RunContext], bool], ...] = (
    rp_stage_schematic.stage_g1, rp_stage_schematic.stage_g2,
    rp_stage_place.stage_route_cache, rp_stage_place.stage_place_route,
    rp_stage_layout.stage_g4, rp_stage_fab.stage_cache_manifest, rp_stage_fab.stage_g5)


def run(netlist: str, w: float, h: float, placement: str = "") -> dict:
    """Drive `netlist` through every gate (stopping at the first failure) and
    return the report; it is also written to <stem>_pipeline_report.json. A
    crash is recorded in that file (PASS false, "error") and re-raised."""
    ctx = rp_context.RunContext(netlist=netlist, w=w, h=h, placement=placement)
    try:
        rp_stage_schematic.stage_load(ctx)
        for stage in STAGES:
            if not stage(ctx):
                break
    except Exception as exc:
        rp_context.halt(ctx, error=f"{type(exc).__name__}: {exc}")
        raise
    return rp_context.halt(ctx)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args and args[0] in ("-h", "--help"):
        print(f"{rp_cli.USAGE}\n{__doc__}")
        return 0
    try:
        cli = rp_cli.parse_cli(args)
        w, h, source = rp_cli.cli_dims(cli)
    except rp_cli.UsageError as e:
        print(f"run_pipeline: error: {e}\n{rp_cli.USAGE}", file=sys.stderr)
        return 2
    print(f"run_pipeline: board {w} x {h} mm ({source})", file=sys.stderr)
    rep = run(cli.netlist, w, h, cli.placement)
    print(json.dumps(rep, indent=2))
    print("\n" + rep["gate_summary"], file=sys.stderr)
    return 0 if rep["PASS"] else 1


# ---- aliases: the names pipeline/run_all.py and tests/test_toolchain.py use --
# (bound once at import: patch the owning rp_* module, never these)
SERVER = rp_paths.SERVER
RunContext = rp_context.RunContext
PACK_SCALES = rp_stage_place.PACK_SCALES
AUTOROUTE_MAX_PASSES = rp_route.AUTOROUTE_MAX_PASSES
AUTOROUTE_TIMEOUT_S = rp_route.AUTOROUTE_TIMEOUT_S
_board_cli = rp_kicad.board_cli
_drc = rp_kicad.drc
_board_fix = rp_kicad.board_fix
_route_hash = rp_route_hash.route_hash
_sourcing = rp_stage_fab.sourcing
USAGE = rp_cli.USAGE
AUTO_BOARD_DIMS = rp_cli.AUTO_BOARD_DIMS
UsageError = rp_cli.UsageError
CliArgs = rp_cli.CliArgs
parse_cli = rp_cli.parse_cli
board_dims = rp_cli.board_dims


if __name__ == "__main__":
    sys.exit(main())
