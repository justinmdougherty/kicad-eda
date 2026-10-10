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
and <stem>_review.md next to the netlist on every exit (pass, gate failure,
crash, or a usage error once the netlist path is known), with the run's
decisions in the append-only <stem>.decisions.jsonl (ROADMAP D5).

Napkin mode (ROADMAP D5) is the default: the D5 policies turn floating IC
pins, connectors facing the interior, fractured planes and short EP via farms
into recorded automatic remedies, and a schematic / board open in a KiCad
editor is written as a <stem>.napkin.* sibling instead. --strict
(run(..., napkin=False)) is the harness without those policies (a file open
in an editor then halts the run); a netlist "mode": "napkin" always means
napkin.

Routing (ROADMAP D5, rp_route_modes): --route auto (run(..., route='auto'),
the default) routes with Freerouting first and, when G4 then fails, hands
the board to the router subagent: <stem>_route_handoff.json, report status
'handoff', PASS false, exit 3. --route freerouting never hands off (a G4
failure exits 1); --route agent hands the board off as soon as it passes G3,
without trying Freerouting. A feature's handoff_veto turns a would-be
handoff into a G4 FAIL; a feature's route_provider is never handed off.

Module map (pipeline/; each is a top-level import, and the rp_ prefix keeps
the names clear of the modules in server/ and server/tools/):
  run_pipeline.py        STAGES, run(), main() and the aliases run_all.py uses
  rp_paths.py            puts server/ and server/tools/ on sys.path (imported first)
  rp_cli.py              command line: parse_cli (--strict, --route), board_dims,
                         UsageError, USAGE, exit_code
  rp_context.py          RunContext (+ decide), Attempt, record_gate, halt (report,
                         review, run-level decisions), file suffixes, route modes
  rp_kicad.py            KiCad subprocess wrappers: drc, render, gate_check,
                         board_fix, emc_stitch, courtyard_drc, board_cli
  rp_route_hash.py       the route-cache key registry and route_hash
  rp_napkin.py           napkin mode: the mode decision, the open-editor hard stop,
                         the G2/G3 policy remedies, the connector probe
  rp_stage_schematic.py  stage_load (mode, hard stop, agent-board preservation),
                         G1 (netlist lint), G2 (schematic + review loop)
  rp_stage_place.py      the route cache, G3 placement (+ napkin policy rounds) and
                         the pack_scale ladder
  rp_route.py            Freerouting attempts, near-miss heal, routed snapshots
  rp_route_modes.py      --route auto|freerouting|agent: the best routed attempt, the
                         handoff to the agent (<stem>_route_handoff.json), handoff_veto
  rp_stage_layout.py     G4 judge, board-fix loop, EMC stitching
  rp_stage_fab.py        the cache manifest, G5 fab package, sourcing audit and safety
                         warnings

Rules for changing the harness:
  - Stage modules call helpers through module objects (rp_kicad.drc(...),
    k.autoroute_board(...), rp_context.record_gate(...)), never
    'from rp_kicad import drc', so a test patches one place.
  - A feature adds a stage as a function in its own
    pipeline/rp_stage_<feature>.py plus one STAGES line in this file.
  - Route-hash keys go in rp_route_hash.py.
  - KiCad subprocess wrappers go in rp_kicad.py.

Usage:
  <venv-python> run_pipeline.py <netlist.json> [<board_w> <board_h>] [placement.json] [--strict]
                [--route auto|freerouting|agent]
  (no dims: the netlist "board" w/h, else the board is auto-sized to its parts)
  exit codes: 0 PASS, 1 FAIL, 2 usage error, 3 routing handed off to the agent
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Callable

import rp_paths  # first: puts server/ and server/tools/ on sys.path
import kicad_server as k  # noqa: F401  (alias: run_all.py uses rp.k)
import toolchain as tc  # noqa: F401  (alias for callers)
import rp_cli
import rp_context
import rp_kicad
import rp_route
import rp_route_hash
import rp_route_modes
import rp_stage_fab
import rp_stage_layout
import rp_stage_place
import rp_stage_schematic

# The gated sequence. Each stage returns False to stop the run.
STAGES: tuple[Callable[[rp_context.RunContext], bool], ...] = (
    rp_stage_schematic.stage_g1, rp_stage_schematic.stage_g2,
    rp_stage_place.stage_route_cache, rp_stage_place.stage_place_route,
    rp_stage_layout.stage_g4, rp_stage_fab.stage_cache_manifest, rp_stage_fab.stage_g5)


def run(netlist: str, w: float, h: float, placement: str = "", *, napkin: bool = True,
        route: str = rp_route_modes.DEFAULT_ROUTE) -> dict:
    """Drive `netlist` through every gate (stopping at the first failure) and
    return the report; it is also written to <stem>_pipeline_report.json. A
    crash is recorded in that file (PASS false, "error") and re-raised.
    napkin=False is --strict (a netlist "mode": "napkin" still means napkin);
    route is --route (rp_route_modes.ROUTE_MODES; ValueError for another): a
    handoff ends the run with report status 'handoff' and PASS false."""
    rp_route_modes.check_route(route)
    ctx = rp_context.RunContext(netlist=netlist, w=w, h=h, placement=placement, napkin=napkin,
                                route=route)
    try:
        rp_stage_schematic.stage_load(ctx)
        for stage in STAGES:
            if not stage(ctx):
                break
    except Exception as exc:
        rp_context.halt(ctx, error=f"{type(exc).__name__}: {exc}")
        raise
    return rp_context.halt(ctx)


def usage_report(cli: rp_cli.CliArgs, message: str) -> dict | None:
    """A usage error found once the command line parsed (an unreadable
    netlist or placement file, board dims the netlist cannot give): the
    failing report + review through rp_context.halt (error 'usage: ...',
    PASS false, the design when the netlist reads as a JSON object, else {}).
    None, nothing written, when the netlist's folder does not exist."""
    try:
        if not Path(cli.netlist).resolve().parent.is_dir():
            return None
    except OSError:
        return None
    ctx = rp_context.RunContext(netlist=cli.netlist, w=cli.w or 0.0, h=cli.h or 0.0,
                                placement=cli.placement)
    try:
        design = json.loads(Path(cli.netlist).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        design = {}
    ctx.design = design if isinstance(design, dict) else {}
    return rp_context.halt(ctx, error=f"usage: {message}")


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args and args[0] in ("-h", "--help"):
        print(f"{rp_cli.USAGE}\n{__doc__}")
        return 0
    cli: rp_cli.CliArgs | None = None
    try:
        cli = rp_cli.parse_cli(args)
        w, h, source = rp_cli.cli_dims(cli)
    except rp_cli.UsageError as e:
        print(f"run_pipeline: error: {e}\n{rp_cli.USAGE}", file=sys.stderr)
        if cli is not None:                  # the netlist path is known: report + review
            usage_report(cli, str(e))
        return rp_cli.EXIT_USAGE
    print(f"run_pipeline: board {w} x {h} mm ({source})", file=sys.stderr)
    rep = run(cli.netlist, w, h, cli.placement, **rp_cli.run_options(cli))
    print(json.dumps(rep, indent=2))
    print("\n" + rep["gate_summary"], file=sys.stderr)
    if rep.get("handoff"):
        print(f"run_pipeline: routing handed to the agent: {rep['handoff']}", file=sys.stderr)
    return rp_cli.exit_code(rep)


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
