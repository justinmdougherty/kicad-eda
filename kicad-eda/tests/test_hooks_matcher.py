"""hooks/hooks.json tool matchers.

The PreToolUse ``check-ipc`` gate and the PostToolUse ``validate-schematic``
check must fire for their kicad-generator tools under BOTH names Claude Code
gives an MCP tool:

* ``mcp__kicad-generator__<tool>`` -- the server registered by its bare key
  (a project ``.mcp.json`` / a dev checkout), and
* ``mcp__plugin_kicad-eda_kicad-generator__<tool>`` -- the installed plugin,
  whose server segment is scoped ``plugin_<plugin name>_<server key>``,

and for no other tool. Claude Code evaluates a matcher holding anything other
than letters, digits, ``_``, ``-``, spaces, ``,`` and ``|`` as an UNANCHORED
JavaScript regex (``RegExp.prototype.test``), so an unanchored ``add_via``
alternative also gates the headless ``add_vias`` route tool.
``_claude_code_matches`` mirrors those documented rules; when node is on PATH
the matchers also go through V8's own RegExp, and hook.js itself is run on
events carrying either name form.
"""
from __future__ import annotations

import asyncio
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HOOKS_JSON = ROOT / "hooks" / "hooks.json"
HOOK_JS = ROOT / "scripts" / "hook.js"

# The tools each hook targets. Changing a hook's reach is a deliberate edit of
# both this contract and hooks.json.
IPC_GATED = frozenset({
    "add_trace", "add_via", "add_copper_zone", "add_footprint_to_pcb",
    "add_footprints_batch", "move_footprint", "remove_footprint",
    "validate_placement", "register_net", "get_pcb_summary", "get_footprint_pads",
})
SCHEMATIC_WRITES = frozenset({
    "save_schematic", "close_schematic", "add_components_batch", "wire_by_ref",
    "wire_pins", "label_pin", "power_pin", "add_power_port", "no_connect_pin",
    "add_junction", "build_schematic_from_netlist",
})
# hook.js mode -> (hook event, targeted tools)
HOOKS = {
    "check-ipc": ("PreToolUse", IPC_GATED),
    "validate-schematic": ("PostToolUse", SCHEMATIC_WRITES),
}
# Claude Code compares a matcher made only of these characters as an exact
# name (or a list of exact names split on '|' / ','); anything else is a regex.
_EXACT_MATCHER = re.compile(r"[A-Za-z0-9_\- ,|]+")
NODE = shutil.which("node")


def _plugin_name() -> str:
    return json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))["name"]


def _server_key() -> str:
    servers = json.loads((ROOT / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]
    assert len(servers) == 1, f".mcp.json should declare exactly one server, got {sorted(servers)}"
    return next(iter(servers))


def _name_forms(tool: str) -> tuple[str, str]:
    """(bare-server name, installed-plugin name) Claude Code gives ``tool``."""
    server = _server_key()
    return f"mcp__{server}__{tool}", f"mcp__plugin_{_plugin_name()}_{server}__{tool}"


def _all_forms(tools) -> list[str]:
    return [name for tool in sorted(tools) for name in _name_forms(tool)]


def _matcher_for(mode: str) -> str:
    """The matcher of the single hooks.json entry whose command runs ``hook.js <mode>``."""
    event, _ = HOOKS[mode]
    entries = json.loads(HOOKS_JSON.read_text(encoding="utf-8"))["hooks"].get(event, [])
    found = [entry.get("matcher", "") for entry in entries
             if any(h.get("command", "").endswith(f'hook.js" {mode}') for h in entry.get("hooks", []))]
    assert len(found) == 1, f"expected one {event} entry running hook.js {mode}, found {len(found)}"
    return found[0]


def _claude_code_matches(matcher: str, tool_name: str) -> bool:
    """Claude Code's documented PreToolUse/PostToolUse matcher semantics."""
    if matcher in ("", "*"):
        return True
    if _EXACT_MATCHER.fullmatch(matcher):
        return tool_name in {part.strip() for part in re.split(r"[|,]", matcher)}
    return re.search(matcher, tool_name) is not None


def _near_misses(mode: str) -> list[str]:
    """Names one edit away from a targeted name, plus the other hook's tools."""
    _, targeted = HOOKS[mode]
    sample = sorted(targeted)[0]
    bare, scoped = _name_forms(sample)
    server, plugin = _server_key(), _plugin_name()
    others = [t for m, (_, tools) in HOOKS.items() if m != mode for t in tools]
    return [
        *_all_forms(["add_vias", "add_component", "add_label", "add_wire", "route_net",
                     "open_board", "create_pcb", "create_schematic", "load_schematic"]),
        *_all_forms(others),
        f"x{bare}", f"{bare}_x", f"x{scoped}", f"{scoped}_x",
        f"mcp__other__{sample}", f"mcp__{server}2__{sample}",
        f"mcp__plugin_other_{server}__{sample}", f"mcp__plugin_{plugin}_other__{sample}",
        f"mcp__plugin_{plugin}__{server}__{sample}", f"mcp__plugin__{server}__{sample}",
        f"mcp__{server}__", f"mcp__plugin_{plugin}_{server}__", sample,
    ]


@pytest.fixture(scope="module")
def registered_tools() -> frozenset[str]:
    """Every tool the kicad-generator server registers, from FastMCP itself."""
    import kicad_server
    return frozenset(tool.name for tool in asyncio.run(kicad_server.mcp.list_tools()))


@pytest.mark.parametrize("mode", sorted(HOOKS))
def test_matcher_takes_the_regex_path(mode):
    matcher = _matcher_for(mode)
    assert matcher not in ("", "*") and not _EXACT_MATCHER.fullmatch(matcher), matcher


@pytest.mark.parametrize("mode", sorted(HOOKS))
def test_matcher_fully_matches_both_name_forms(mode):
    matcher = _matcher_for(mode)
    _, targeted = HOOKS[mode]
    for name in _all_forms(targeted):
        assert re.fullmatch(matcher, name), f"{mode} matcher does not fully match {name}"
        assert _claude_code_matches(matcher, name), f"{mode} hook would not fire for {name}"


@pytest.mark.parametrize("mode", sorted(HOOKS))
def test_matcher_rejects_near_misses(mode):
    matcher = _matcher_for(mode)
    hits = [n for n in _near_misses(mode) if _claude_code_matches(matcher, n) or re.fullmatch(matcher, n)]
    assert hits == [], f"{mode} hook would fire for non-targeted tools: {hits}"


@pytest.mark.parametrize("mode", sorted(HOOKS))
def test_matcher_rejects_every_other_registered_tool(mode, registered_tools):
    matcher = _matcher_for(mode)
    _, targeted = HOOKS[mode]
    others = _all_forms(registered_tools - targeted)
    assert others, "the server registered no non-targeted tools"
    hits = [n for n in others if _claude_code_matches(matcher, n)]
    assert hits == [], f"{mode} hook would fire for non-targeted tools: {hits}"


def test_targeted_tools_are_registered(registered_tools):
    # a renamed tool would otherwise silently drop out of its hook
    for mode, (_, targeted) in HOOKS.items():
        assert targeted <= registered_tools, \
            f"{mode} targets unregistered tools: {sorted(targeted - registered_tools)}"


@pytest.mark.skipif(NODE is None, reason="node is not on PATH")
@pytest.mark.parametrize("mode", sorted(HOOKS))
def test_matcher_under_javascript_regexp(mode, registered_tools):
    """Claude Code runs the matcher through V8: check with the real engine."""
    matcher = _matcher_for(mode)
    _, targeted = HOOKS[mode]
    expected = _all_forms(targeted)
    names = expected + _all_forms(registered_tools - targeted) + _near_misses(mode)
    script = ("const d = JSON.parse(require('fs').readFileSync(0, 'utf8'));"
              "const re = new RegExp(d.matcher);"
              "process.stdout.write(JSON.stringify(d.names.filter((n) => re.test(n))));")
    run = subprocess.run([NODE, "-e", script], input=json.dumps({"matcher": matcher, "names": names}),
                         capture_output=True, text=True, timeout=60, check=False)
    assert run.returncode == 0, run.stderr
    assert sorted(json.loads(run.stdout)) == sorted(expected)


@pytest.mark.skipif(NODE is None, reason="node is not on PATH")
@pytest.mark.parametrize("form", [0, 1], ids=["bare", "plugin"])
@pytest.mark.parametrize("intact", [True, False], ids=["intact", "broken"])
def test_validate_schematic_hook_handles_both_name_forms(tmp_path, monkeypatch, form, intact):
    """hook.js never parses tool_name: either form gets the same verdict."""
    sch = tmp_path / "design.kicad_sch"
    body = "(kicad_sch (version 20250114) (lib_symbols) (sheet_instances)"
    sch.write_text(body + (")\n" if intact else "\n"), encoding="utf-8")
    event = {"hook_event_name": "PostToolUse", "tool_name": _name_forms("save_schematic")[form],
             "tool_input": {"sch": str(sch)}, "tool_response": {"ok": True}}
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(ROOT))
    monkeypatch.delenv("CLAUDE_PLUGIN_DATA", raising=False)
    run = subprocess.run([NODE, str(HOOK_JS), "validate-schematic"], input=json.dumps(event),
                         capture_output=True, text=True, timeout=120, check=False)
    assert run.returncode == (0 if intact else 2), run.stderr
    assert ("SCHEMATIC SANITY" in run.stderr) is not intact, run.stderr
