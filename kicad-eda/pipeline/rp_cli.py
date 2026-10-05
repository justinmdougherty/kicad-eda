"""The harness command line: parse <netlist.json> [<board_w> <board_h>]
[placement.json] and decide the board dimensions (command line, else the
netlist board block, else a feature's board_dims hook, else the builder's
auto-size default)."""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, NamedTuple

import rp_paths  # noqa: F401  first: puts server/ and server/tools/ on sys.path
import rp_hooks

USAGE = "usage: run_pipeline.py <netlist.json> [<board_w> <board_h>] [placement.json]"
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


def _is_number(text: str) -> bool:
    try:
        float(text)
    except ValueError:
        return False
    return True


def _positive_mm(value: Any) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value > 0)


def parse_cli(argv: list[str]) -> CliArgs:
    """<netlist.json> [<board_w> <board_h>] [placement.json]: the dims come
    as a pair or not at all; '' or '-' as the placement means none."""
    if not argv:
        raise UsageError("missing <netlist.json>")
    netlist, rest = argv[0], list(argv[1:])
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
    return CliArgs(netlist, w, h, placement)


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
