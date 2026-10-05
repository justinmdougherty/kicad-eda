"""scripts/hook.js check-ipc: the BLOCKED message names the tools that work without the GUI.

When the PreToolUse ``check-ipc`` gate blocks a live IPC board tool (add_trace,
add_via, ... with no KiCad API answering), its stderr is all the model sees. It
must name both ways forward that need no GUI: the Freerouting-era headless
pipeline tools, and the KiCad 11 board-API routing tools that replace
add_trace / add_via headless (skills/agent-routing).

hook.js runs the way tests/test_hooks_matcher.py runs it (node on PATH,
CLAUDE_PLUGIN_ROOT = the plugin) on a PreToolUse add_trace event under both MCP
name forms. KICAD_API_SOCKET points at an endpoint nobody listens on, so the
probe through the server venv reports "unreachable" and the gate blocks whether
or not a KiCad GUI is running. Without a server venv hook.js can only scan
processes and rightly allows while a KiCad process is up; that one case skips.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HOOK_JS = ROOT / "scripts" / "hook.js"
# hook.js probes through <CLAUDE_PLUGIN_DATA or plugin root>/.venv (scripts/venv.js)
VENV_PY = ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
NODE = shutil.which("node")
HOOK_TIMEOUT_S = 120

HEADLESS_PIPELINE_TOOLS = ("create_pcb", "build_board_from_netlist", "autoroute_board",
                           "drc_pcb", "export_fab_package")
# The KiCad 11 board-API tools that lay copper with no GUI (headless nightly
# api-server): what replaces add_trace / add_via when no board is open in KiCad.
BOARD_API_ROUTING_TOOLS = ("open_board", "board_pads", "route_net", "add_vias",
                           "route_check", "remove_routing", "route_status")
# snake_case words with at least one underscore: every tool name the message can carry
_TOOL_WORD = re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b")

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not on PATH")


def _name_forms(tool: str) -> tuple[str, str]:
    """(bare-server name, installed-plugin name) Claude Code gives ``tool``."""
    plugin = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    servers = json.loads((ROOT / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]
    (server,) = servers
    return f"mcp__{server}__{tool}", f"mcp__plugin_{plugin['name']}_{server}__{tool}"


def _blocked_add_trace(tool_name: str, socket_dir: Path) -> subprocess.CompletedProcess:
    """Run ``hook.js check-ipc`` on an add_trace PreToolUse event with no API to reach."""
    event = {"hook_event_name": "PreToolUse", "tool_name": tool_name,
             "tool_input": {"net_name": "GND", "layer": "F.Cu", "x1": 1.0, "y1": 1.0,
                            "x2": 5.0, "y2": 1.0, "width": 0.25}}
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_PLUGIN_DATA"}
    env.update(CLAUDE_PLUGIN_ROOT=str(ROOT),
               KICAD_API_SOCKET=f"ipc://{socket_dir / 'nobody' / 'api.sock'}")
    run = subprocess.run([NODE, str(HOOK_JS), "check-ipc"], input=json.dumps(event),
                         capture_output=True, text=True, encoding="utf-8", errors="replace",
                         env=env, timeout=HOOK_TIMEOUT_S, check=False)
    if run.returncode == 0 and not VENV_PY.is_file():
        pytest.skip(f"no server venv at {VENV_PY}: hook.js cannot ask the API and allows "
                    "while a KiCad process runs, so there is no BLOCKED message to read")
    return run


@pytest.fixture(scope="module")
def registered_tools() -> frozenset[str]:
    """Every tool the kicad-generator server registers, from FastMCP itself."""
    import kicad_server
    return frozenset(tool.name for tool in asyncio.run(kicad_server.mcp.list_tools()))


def _names(message: str, tools: tuple[str, ...]) -> list[str]:
    """The tools of ``tools`` that ``message`` names as a whole word."""
    return [t for t in tools if re.search(rf"\b{re.escape(t)}\b", message)]


@pytest.mark.parametrize("form", [0, 1], ids=["bare", "plugin"])
def test_blocked_add_trace_names_the_board_api_routing_tools(tmp_path, form):
    run = _blocked_add_trace(_name_forms("add_trace")[form], tmp_path)
    assert run.returncode == 2, (run.returncode, run.stdout, run.stderr)
    message = run.stderr
    assert message.startswith("BLOCKED: this tool needs KiCad's IPC API."), message
    assert _names(message, BOARD_API_ROUTING_TOOLS) == list(BOARD_API_ROUTING_TOOLS), message
    # the Freerouting-era headless pipeline stays named beside them
    assert _names(message, HEADLESS_PIPELINE_TOOLS) == list(HEADLESS_PIPELINE_TOOLS), message
    # the routing tools are offered as the headless replacement for add_trace / add_via
    assert _names(message, ("add_trace", "add_via")) == ["add_trace", "add_via"], message


def test_every_tool_the_message_names_is_registered(tmp_path, registered_tools):
    message = _blocked_add_trace(_name_forms("add_trace")[1], tmp_path).stderr
    named = set(_TOOL_WORD.findall(message))
    assert set(BOARD_API_ROUTING_TOOLS) | set(HEADLESS_PIPELINE_TOOLS) <= named, message
    unregistered = sorted(named - registered_tools)
    assert not unregistered, f"the message names tools the server does not register: {unregistered}"
