"""MCP tool module registry: the one registration path for kicad-generator tools.

Every tool module lists its MCP tools as plain functions in a module-level
``TOOLS`` tuple: kicad_server itself (schematic + status tools),
pcb_ipc_tools, board_pipeline_tools, board_route_tools and the roadmap
feature modules library_tools, import_tools, review_tools and
mechanical_tools (docs/ROADMAP_DECISIONS.md D1-D5). kicad_server.TOOL_MODULES
is the one explicit list of those modules; kicad_server hands it to
register_tool_modules() at import, and that order is the order MCP clients
list the tools in. A feature adds a tool by appending it to its module's
TOOLS; only a NEW module is added to kicad_server.TOOL_MODULES.

FastMCP keeps the first tool of a name and drops a later one with only a log
warning, so every entry is checked here before any is registered.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from types import ModuleType
from typing import Any, Callable

from mcp.server.fastmcp import FastMCP

ToolFn = Callable[..., Any]


def _label(mod: ModuleType) -> str:
    """The module's name for messages: 'kicad_server', not '__main__', when the
    server runs as a script."""
    path = getattr(mod, "__file__", None)
    return Path(path).stem if mod.__name__ == "__main__" and path else mod.__name__


def _module_tools(mod: ModuleType) -> tuple[ToolFn, ...]:
    """mod.TOOLS, checked: a tuple (a missing trailing comma is caught here) of
    named functions (a lambda, string or class is not a tool). TypeError if not."""
    tools = getattr(mod, "TOOLS", None)
    if not isinstance(tools, tuple):
        raise TypeError(f"{_label(mod)}.TOOLS must be a tuple of tool functions, "
                        f"got {type(tools).__name__}")
    for fn in tools:
        if not inspect.isroutine(fn) or fn.__name__ == "<lambda>":
            raise TypeError(f"{_label(mod)}.TOOLS: {fn!r} is not a named function")
    return tools


def register_tool_modules(server: FastMCP, modules: tuple[ModuleType, ...]) -> list[str]:
    """Register every function in each module's TOOLS (read now) with `server`,
    in module order, and return the tool names in that order.

    Everything is checked before anything is registered: TOOLS not a tuple of
    named functions -> TypeError; a tool name that an earlier TOOLS entry (in
    any module, the same one included) already has -> ValueError naming both
    modules. Only TOOLS entries reserve a name: a module's other functions are
    not MCP tools."""
    owners: dict[str, str] = {}
    checked: list[ToolFn] = []
    for mod in modules:
        for fn in _module_tools(mod):
            owner = owners.get(fn.__name__)
            if owner:
                raise ValueError(f"{_label(mod)}.TOOLS: MCP tool name {fn.__name__!r} is "
                                 f"already taken by {owner} (FastMCP would keep only the first)")
            owners[fn.__name__] = _label(mod)
            checked.append(fn)
    for fn in checked:
        server.add_tool(fn)
    return [fn.__name__ for fn in checked]
