"""The harness command line: parse <netlist.json> [<board_w> <board_h>]
[placement.json] [--strict] [--route auto|freerouting|agent] and decide the
board dimensions (command line, else the netlist board block, else a
feature's board_dims hook, else the builder's auto-size default). --strict
(anywhere on the line) turns the napkin policies off (ROADMAP D5: the harness
defaults to napkin behaviour; a netlist "mode": "napkin" still means napkin).
--route picks the routing mode (rp_route_modes; default auto: Freerouting
first, the agent when G4 fails). Exit codes (exit_code): 0 PASS, 1 FAIL,
2 usage, 3 routing handed off to the agent."""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, NamedTuple

import rp_paths  # noqa: F401  first: puts server/ and server/tools/ on sys.path
import rp_context
import rp_hooks

STRICT_FLAG = "--strict"
ROUTE_FLAG = "--route"
MODES = ", ".join(rp_context.ROUTE_MODES)
USAGE = ("usage: run_pipeline.py <netlist.json> [<board_w> <board_h>] [placement.json] "
         f"[{STRICT_FLAG}] [{ROUTE_FLAG} {'|'.join(rp_context.ROUTE_MODES)}]")
EXIT_PASS, EXIT_FAIL, EXIT_USAGE, EXIT_HANDOFF = 0, 1, 2, 3
# swig_build_board's defaults; an auto-placed board is sized to its parts
AUTO_BOARD_DIMS: tuple[float, float] = (50.0, 25.0)


class UsageError(ValueError):
    """A command line (or netlist) the harness cannot start from; main()
    prints it with the usage line and exits 2."""


class CliArgs(NamedTuple):
    netlist: str
    w: float | None
    h: float | None
    placement: str
    napkin: bool = True               # False: --strict (the napkin policies off)
    route: str = rp_context.ROUTE_AUTO   # --route (rp_context.ROUTE_MODES; rp_route_modes)


def _is_number(text: str) -> bool:
    try:
        float(text)
    except ValueError:
        return False
    return True


def _positive_mm(value: Any) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value > 0)


def _route_value(argv: list[str], i: int) -> tuple[str, int]:
    """(the --route value at argv[i], the index after it): '--route MODE' or
    '--route=MODE'; a missing or unknown mode is a UsageError."""
    arg = argv[i]
    if arg.startswith(ROUTE_FLAG + "="):
        value, after = arg[len(ROUTE_FLAG) + 1:], i + 1
    elif i + 1 < len(argv) and not argv[i + 1].startswith("--"):
        value, after = argv[i + 1], i + 2
    else:
        raise UsageError(f"{ROUTE_FLAG} needs one of {MODES}")
    if value not in rp_context.ROUTE_MODES:
        raise UsageError(f"{ROUTE_FLAG} must be one of {MODES}, got {value!r}")
    return value, after


def split_options(argv: list[str]) -> tuple[list[str], bool, str]:
    """(the positional arguments, napkin, route): --strict anywhere turns
    napkin off; --route MODE (or --route=MODE) anywhere, at most once, picks
    the routing mode (default auto); any other '--' option is a UsageError."""
    positional: list[str] = []
    napkin, route, i = True, "", 0
    while i < len(argv):
        arg = argv[i]
        if arg == STRICT_FLAG:
            napkin, i = False, i + 1
        elif arg == ROUTE_FLAG or arg.startswith(ROUTE_FLAG + "="):
            if route:
                raise UsageError(f"{ROUTE_FLAG} given more than once")
            route, i = _route_value(argv, i)
        elif arg.startswith("--"):
            raise UsageError(f"unknown option {arg} (the options are {STRICT_FLAG} and "
                             f"{ROUTE_FLAG} {'|'.join(rp_context.ROUTE_MODES)})")
        else:
            positional, i = [*positional, arg], i + 1
    return positional, napkin, route or rp_context.ROUTE_AUTO


def parse_cli(argv: list[str]) -> CliArgs:
    """<netlist.json> [<board_w> <board_h>] [placement.json] [--strict]
    [--route MODE]: the dims come as a pair or not at all; '' or '-' as the
    placement means none."""
    args, napkin, route = split_options(argv)
    if not args:
        raise UsageError("missing <netlist.json>")
    netlist, rest = args[0], list(args[1:])
    w = h = None
    if rest and _is_number(rest[0]):
        if len(rest) < 2 or not _is_number(rest[1]):
            raise UsageError("give both <board_w> and <board_h>, or neither")
        w, h = float(rest[0]), float(rest[1])
        for name, text, value in (("board_w", rest[0], w), ("board_h", rest[1], h)):
            if not _positive_mm(value):
                raise UsageError(f"{name} must be a positive number of mm, got '{text}'")
        rest = rest[2:]
    if len(rest) > 1:
        raise UsageError(f"unexpected argument(s): {' '.join(rest[1:])}")
    placement = rest[0] if rest and rest[0] not in ("", "-") else ""
    return CliArgs(netlist, w, h, placement, napkin, route)


def run_options(cli: CliArgs) -> dict[str, Any]:
    """The keyword arguments main() passes to run(): only what differs from
    run()'s defaults (napkin, route auto)."""
    return {**({} if cli.napkin else {"napkin": False}),
            **({} if cli.route == rp_context.ROUTE_AUTO else {"route": cli.route})}


def exit_code(report: dict) -> int:
    """The process exit code of a finished run: EXIT_HANDOFF when routing was
    handed to the agent, else EXIT_PASS / EXIT_FAIL from the verdict."""
    if report.get("status") == rp_context.STATUS_HANDOFF:
        return EXIT_HANDOFF
    return EXIT_PASS if report.get("PASS") else EXIT_FAIL


def board_dims(design: dict, w: float | None, h: float | None,
               placement_refs: frozenset[str] = frozenset(),
               netlist_dir: Path | None = None) -> tuple[float, float, str]:
    """(w, h, where they came from): the command line, else the netlist
    board block, else a feature's board_dims hook (F3: the outline), else
    AUTO_BOARD_DIMS for a board the builder sizes itself. A design whose
    every part is fixed (x/y, "at", or the placement file) has nothing to
    size the board from: UsageError instead of a guess."""
    if w is not None and h is not None:
        return w, h, "command line"
    board = design.get("board") or {}
    if "w" in board or "h" in board:
        bw, bh = board.get("w"), board.get("h")
        if not (_positive_mm(bw) and _positive_mm(bh)):
            raise UsageError(f"the netlist board block needs positive numeric w and h "
                             f"(mm), got w={bw!r} h={bh!r}")
        return float(bw), float(bh), "netlist board block"
    dims = rp_hooks.first("board_dims", design, netlist_dir)
    if dims is not None:
        return float(dims[0]), float(dims[1]), dims[2]
    comps = design.get("components") or []
    fixed = (("x" in c and "y" in c) or "at" in c or c.get("ref") in placement_refs
             for c in comps)
    if comps and not board and all(fixed):
        raise UsageError('every component has a fixed position, so there is nothing to '
                         'size the board from: add "board": {"w": mm, "h": mm} to the '
                         'netlist or pass <board_w> <board_h>')
    return (*AUTO_BOARD_DIMS, "auto-sized: the builder fits the board to its parts")


def read_json(path: str, what: str) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise UsageError(f"cannot read the {what} {path}: {e}") from e


def cli_dims(cli: CliArgs) -> tuple[float, float, str]:
    if cli.w is not None and cli.h is not None:
        return cli.w, cli.h, "command line"
    design = read_json(cli.netlist, "netlist")
    if not isinstance(design, dict):
        raise UsageError(f"the netlist {cli.netlist} is not a JSON object")
    overrides = read_json(cli.placement, "placement file") if cli.placement else {}
    if not isinstance(overrides, dict):
        raise UsageError(f"the placement file {cli.placement} must map ref -> [x, y, rot]")
    try:
        return board_dims(design, None, None, frozenset(overrides),
                          Path(cli.netlist).resolve().parent)
    except rp_hooks.Halt as h:                  # a feature's board_dims refused the netlist
        raise UsageError(h.message) from h
