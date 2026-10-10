"""The harness command line: parse <netlist.json> [<board_w> <board_h>]
[placement.json] [--strict] [--route auto|freerouting|agent]
[--resume routed|stalled] and decide the board dimensions (command line,
else the netlist board block, else a feature's board_dims hook, else the
builder's auto-size default). --strict (anywhere on the line) turns the
napkin policies off (ROADMAP D5: the harness defaults to napkin behaviour; a
netlist "mode": "napkin" still means napkin). --route picks the routing mode
(rp_route_modes; default auto: Freerouting first, the agent when G4 fails).
--resume continues a run that handed routing to the agent (routed: judge the
agent's board; stalled: the Freerouting fallback chain); it takes neither a
placement file nor --route (a resumed run never places from an override and
never hands off). Exit codes (exit_code): 0 PASS, 1 FAIL, 2 usage, 3 routing
handed off to the agent."""
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
RESUME_FLAG = "--resume"
MODES = ", ".join(rp_context.ROUTE_MODES)
# the options that take a value: flag -> its choices
CHOICES: dict[str, tuple[str, ...]] = {ROUTE_FLAG: rp_context.ROUTE_MODES,
                                       RESUME_FLAG: rp_context.RESUME_MODES}
USAGE = ("usage: run_pipeline.py <netlist.json> [<board_w> <board_h>] [placement.json] "
         f"[{STRICT_FLAG}] [{ROUTE_FLAG} {'|'.join(rp_context.ROUTE_MODES)}] "
         f"[{RESUME_FLAG} {'|'.join(rp_context.RESUME_MODES)}]")
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
    resume: str = ""                  # --resume (rp_context.RESUME_MODES); '' = a fresh run


class Options(NamedTuple):
    """What split_options found: the positionals and the '--' options
    (route/resume '' when not given)."""
    positional: list[str]
    napkin: bool
    route: str
    resume: str


def _is_number(text: str) -> bool:
    try:
        float(text)
    except ValueError:
        return False
    return True


def _positive_mm(value: Any) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value > 0)


def _option_value(argv: list[str], i: int, flag: str) -> tuple[str, int]:
    """(the value of the `flag` option at argv[i], the index after it): 'FLAG
    VALUE' or 'FLAG=VALUE'; a missing or unknown value is a UsageError."""
    arg, choices = argv[i], CHOICES[flag]
    if arg.startswith(flag + "="):
        value, after = arg[len(flag) + 1:], i + 1
    elif i + 1 < len(argv) and not argv[i + 1].startswith("--"):
        value, after = argv[i + 1], i + 2
    else:
        raise UsageError(f"{flag} needs one of {', '.join(choices)}")
    if value not in choices:
        raise UsageError(f"{flag} must be one of {', '.join(choices)}, got {value!r}")
    return value, after


def _value_flag(arg: str) -> str | None:
    """The option of CHOICES that `arg` is ('--route' / '--route=...'), else None."""
    return next((flag for flag in CHOICES if arg == flag or arg.startswith(flag + "=")), None)


def split_options(argv: list[str]) -> Options:
    """The positional arguments and the options: --strict anywhere turns
    napkin off; --route MODE and --resume MODE (or FLAG=MODE) anywhere, each
    at most once ('' when not given); any other '--' option is a UsageError."""
    positional: list[str] = []
    values: dict[str, str] = {}
    napkin, i = True, 0
    while i < len(argv):
        arg, flag = argv[i], _value_flag(argv[i])
        if arg == STRICT_FLAG:
            napkin, i = False, i + 1
        elif flag is not None:
            if flag in values:
                raise UsageError(f"{flag} given more than once")
            value, i = _option_value(argv, i, flag)
            values = {**values, flag: value}
        elif arg.startswith("--"):
            raise UsageError(f"unknown option {arg} (the options are {STRICT_FLAG}, "
                             f"{ROUTE_FLAG} {'|'.join(rp_context.ROUTE_MODES)} and "
                             f"{RESUME_FLAG} {'|'.join(rp_context.RESUME_MODES)})")
        else:
            positional, i = [*positional, arg], i + 1
    return Options(positional, napkin, values.get(ROUTE_FLAG, ""), values.get(RESUME_FLAG, ""))


def check_resume(opts: Options, placement: str) -> None:
    """--resume continues the handed-off run as it was placed and never hands
    off again: it takes neither a placement override nor --route."""
    if not opts.resume:
        return
    if placement:
        raise UsageError(f"{RESUME_FLAG} cannot be combined with a placement override "
                         f"({placement}): the run resumes the board the handoff describes")
    if opts.route:
        raise UsageError(f"{RESUME_FLAG} takes no {ROUTE_FLAG}: a resumed run never hands off "
                         f"(routed judges the agent's board; stalled runs the Freerouting "
                         f"fallback chain)")


def parse_cli(argv: list[str]) -> CliArgs:
    """<netlist.json> [<board_w> <board_h>] [placement.json] [--strict]
    [--route MODE] [--resume MODE]: the dims come as a pair or not at all;
    '' or '-' as the placement means none."""
    opts = split_options(argv)
    args = opts.positional
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
    check_resume(opts, placement)
    return CliArgs(netlist, w, h, placement, opts.napkin, opts.route or rp_context.ROUTE_AUTO,
                   opts.resume)


def run_options(cli: CliArgs) -> dict[str, Any]:
    """The keyword arguments main() passes to run(): only what differs from
    run()'s defaults (napkin, route auto, no resume)."""
    return {**({} if cli.napkin else {"napkin": False}),
            **({} if cli.route == rp_context.ROUTE_AUTO else {"route": cli.route}),
            **({"resume": cli.resume} if cli.resume else {})}


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
