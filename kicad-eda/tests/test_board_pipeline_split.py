"""X-09: board_pipeline_tools.py split into board_build / board_autoroute /
board_fab without changing the MCP surface.

board_pipeline_tools keeps every tool (same name, signature, defaults and
docstring) as a thin wrapper; the bodies moved by function name:
  board_build     release_board_session, apply_netclasses, build
  board_autoroute freerouting_args, route, unreadable, autoroute_preflight,
                  run, fallback_from_snapshot, autoroute, parse_ses_import
  board_fab       write_assembly_csvs, export

GOLDEN_SIGNATURES is str(inspect.signature(fn)) of every TOOLS function at
6a8f94d (before the split). A later owner may only EXTEND a tool's signature
backward-compatibly, so planned changes keep passing this test unedited:
  - append parameters that have a default (F5-02 autoroute_board(dsn_seed=0),
    D7-23a optional stackup_json args);
  - give a required HEAD parameter a default (F3-19 board_w / board_h = 0).
Every HEAD parameter keeps its position, name, kind and annotation, every HEAD
default stays as it is, and the return annotation stays. Patch targets moved
with the bodies: monkeypatch board_build.release_board_session (not the
bpt._release_board_session alias) to reach board_build.build and
board_autoroute.run.
"""
from __future__ import annotations

import ast
import inspect
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest

import board_autoroute
import board_build
import board_fab
import board_pipeline_tools as bpt
import kicad_env
import kicad_ipc
import toolchain

SERVER = Path(__file__).resolve().parents[1] / "server"
NEW_MODULES = (board_build, board_autoroute, board_fab)
MAX_NEW_MODULE_LINES = 400
EMPTY = inspect.Parameter.empty

GOLDEN_SIGNATURES: dict[str, str] = {
    "drc_pcb": "(pcb: 'str', fab_profile: 'str' = '') -> 'dict'",
    "export_gerbers": "(pcb: 'str', out_dir: 'str' = '') -> 'dict'",
    "render_pcb": "(pcb: 'str', side: 'str' = 'top', out_png: 'str' = '') -> 'dict'",
    "build_board_from_netlist": (
        "(netlist: 'str', board_w: 'float', board_h: 'float', out_pcb: 'str' = '', "
        "footprints_dir: 'str' = '', placement: 'str' = '', pack_scale: 'float' = 1.0) "
        "-> 'dict'"),
    "autoroute_board": (
        "(pcb: 'str', max_passes: 'int' = 100, jar: 'str' = '', route_timeout: 'int' = 300, "
        "from_snapshot: 'bool' = False) -> 'dict'"),
    "export_fab_package": (
        "(pcb: 'str', out_dir: 'str' = '', zip_name: 'str' = '', netlist: 'str' = '') "
        "-> 'dict'"),
    "check_design_gates": "(pcb: 'str', netlist: 'str', stage: 'str' = 'placement') -> 'dict'",
    "add_ground_stitching": (
        "(pcb: 'str', gnd_net: 'str' = 'GND', max_freq_hz: 'float' = 0.0, er: 'float' = 4.2, "
        "interior: 'bool' = True, via_diameter_mm: 'float' = 0.6, via_drill_mm: 'float' = 0.3) "
        "-> 'dict'"),
    "rescan_kicad_installs": "() -> 'dict'",
}

MOVED: dict[str, tuple[str, ...]] = {
    "board_build": ("release_board_session", "apply_netclasses", "build"),
    "board_autoroute": ("freerouting_args", "route", "unreadable", "autoroute_preflight",
                        "run", "fallback_from_snapshot", "autoroute", "parse_ses_import"),
    "board_fab": ("write_assembly_csvs", "export"),
}


def _tool(name: str) -> Callable[..., dict]:
    return next(fn for fn in bpt.TOOLS if fn.__name__ == name)


def _literal(node: ast.expr | None) -> object:
    return EMPTY if node is None else ast.literal_eval(node)


def _parse_signature(text: str) -> inspect.Signature:
    """inspect.Signature of a signature string whose annotations and defaults
    are literals (the golden strings); parsed with ast, nothing is executed."""
    fn = ast.parse(f"def _sig{text}: ...").body[0]
    assert isinstance(fn, ast.FunctionDef)
    a = fn.args
    assert not (a.posonlyargs or a.vararg or a.kwarg), text
    pos_defaults = [None] * (len(a.args) - len(a.defaults)) + list(a.defaults)
    kinds = ([(arg, d, inspect.Parameter.POSITIONAL_OR_KEYWORD)
              for arg, d in zip(a.args, pos_defaults, strict=True)]
             + [(arg, d, inspect.Parameter.KEYWORD_ONLY)
                for arg, d in zip(a.kwonlyargs, a.kw_defaults, strict=True)])
    params = [inspect.Parameter(arg.arg, kind, default=_literal(d),
                                annotation=_literal(arg.annotation))
              for arg, d, kind in kinds]
    return inspect.Signature(params, return_annotation=_literal(fn.returns))


def _parameter_problem(pos: int, head: inspect.Parameter,
                       now: inspect.Parameter | None) -> str | None:
    if now is None:
        return f"HEAD parameter {head} was removed"
    if (now.name, now.kind, now.annotation) != (head.name, head.kind, head.annotation):
        return f"position {pos}: {now} replaces HEAD {head}"
    if head.default is not EMPTY and repr(now.default) != repr(head.default):
        return f"{head.name}: HEAD default {head.default!r} became {now.default!r}"
    return None          # unchanged, or a required HEAD parameter that gained a default


def _signature_problems(head: inspect.Signature, now: inspect.Signature) -> list[str]:
    """Why `now` is not a backward-compatible extension of `head` ([] = it is)."""
    old = list(head.parameters.values())
    new = list(now.parameters.values())
    padded = [*new, *([None] * (len(old) - len(new)))]
    per_param = [_parameter_problem(i, h, padded[i]) for i, h in enumerate(old)]
    added = [f"added parameter {p} has no default" for p in new[len(old):] if p.default is EMPTY]
    ret = ([] if now.return_annotation == head.return_annotation else
           [f"return annotation {now.return_annotation!r} != {head.return_annotation!r}"])
    return [p for p in per_param if p] + added + ret


def test_tools_tuple_keeps_its_names_and_order() -> None:
    assert [fn.__name__ for fn in bpt.TOOLS] == list(GOLDEN_SIGNATURES)


@pytest.mark.parametrize("name", list(GOLDEN_SIGNATURES))
def test_golden_signature_strings_parse_back_exactly(name: str) -> None:
    assert str(_parse_signature(GOLDEN_SIGNATURES[name])) == GOLDEN_SIGNATURES[name]


@pytest.mark.parametrize("name", list(GOLDEN_SIGNATURES))
def test_every_tool_keeps_its_head_signature(name: str) -> None:
    head = _parse_signature(GOLDEN_SIGNATURES[name])
    assert _signature_problems(head, inspect.signature(_tool(name))) == []


_BUILD_TAIL = ("out_pcb: 'str' = '', footprints_dir: 'str' = '', placement: 'str' = '', "
               "pack_scale: 'float' = 1.0")
_ROUTE_HEAD = ("pcb: 'str', max_passes: 'int' = 100, jar: 'str' = '', "
               "route_timeout: 'int' = 300, from_snapshot: 'bool' = False")
ALLOWED_EXTENSIONS: dict[str, tuple[str, str]] = {
    "unchanged": ("rescan_kicad_installs", "() -> 'dict'"),
    "F3-19 board_w/board_h gain a default": (
        "build_board_from_netlist",
        f"(netlist: 'str', board_w: 'float' = 0, board_h: 'float' = 0, {_BUILD_TAIL}) -> 'dict'"),
    "F3-19 + D7-23a stackup_json appended": (
        "build_board_from_netlist",
        f"(netlist: 'str', board_w: 'float' = 0, board_h: 'float' = 0, {_BUILD_TAIL}, "
        "stackup_json: 'str' = '') -> 'dict'"),
    "F5-02 dsn_seed appended": (
        "autoroute_board", f"({_ROUTE_HEAD}, dsn_seed: 'int' = 0) -> 'dict'"),
    "keyword-only arg appended": (
        "export_fab_package",
        "(pcb: 'str', out_dir: 'str' = '', zip_name: 'str' = '', netlist: 'str' = '', *, "
        "stackup_json: 'str' = '') -> 'dict'"),
}
REJECTED_CHANGES: dict[str, tuple[str, str]] = {
    "HEAD default changed": (
        "build_board_from_netlist",
        "(netlist: 'str', board_w: 'float', board_h: 'float', out_pcb: 'str' = '', "
        "footprints_dir: 'str' = '', placement: 'str' = '', pack_scale: 'float' = 2.0) -> 'dict'"),
    "parameter renamed": (
        "build_board_from_netlist",
        f"(netlist: 'str', board_width: 'float' = 0, board_h: 'float' = 0, {_BUILD_TAIL}) "
        "-> 'dict'"),
    "parameters reordered": (
        "build_board_from_netlist",
        f"(netlist: 'str', board_h: 'float' = 0, board_w: 'float' = 0, {_BUILD_TAIL}) -> 'dict'"),
    "annotation changed": (
        "build_board_from_netlist",
        f"(netlist: 'str', board_w: 'int' = 0, board_h: 'float' = 0, {_BUILD_TAIL}) -> 'dict'"),
    "HEAD parameter made keyword-only": (
        "autoroute_board",
        "(pcb: 'str', *, max_passes: 'int' = 100, jar: 'str' = '', route_timeout: 'int' = 300, "
        "from_snapshot: 'bool' = False) -> 'dict'"),
    "HEAD parameter removed": (
        "autoroute_board",
        "(pcb: 'str', max_passes: 'int' = 100, jar: 'str' = '', route_timeout: 'int' = 300) "
        "-> 'dict'"),
    "required parameter appended": (
        "autoroute_board", f"({_ROUTE_HEAD}, *, dsn_seed: 'int') -> 'dict'"),
    "return annotation changed": ("rescan_kicad_installs", "() -> 'list'"),
}


@pytest.mark.parametrize("case", list(ALLOWED_EXTENSIONS))
def test_signature_rule_allows_backward_compatible_extensions(case: str) -> None:
    name, text = ALLOWED_EXTENSIONS[case]
    head = _parse_signature(GOLDEN_SIGNATURES[name])
    assert _signature_problems(head, _parse_signature(text)) == []


@pytest.mark.parametrize("case", list(REJECTED_CHANGES))
def test_signature_rule_rejects_breaking_changes(case: str) -> None:
    name, text = REJECTED_CHANGES[case]
    head = _parse_signature(GOLDEN_SIGNATURES[name])
    assert _signature_problems(head, _parse_signature(text)), case


@pytest.mark.parametrize("name", list(GOLDEN_SIGNATURES))
def test_every_tool_is_a_documented_function_of_board_pipeline_tools(name: str) -> None:
    fn = _tool(name)
    assert fn.__module__ == "board_pipeline_tools"
    assert (fn.__doc__ or "").strip(), f"{name} lost its docstring (the MCP description)"


def test_private_aliases_point_at_the_moved_helpers() -> None:
    assert bpt._freerouting_args is board_autoroute.freerouting_args
    assert bpt._release_board_session is board_build.release_board_session
    assert bpt._apply_netclasses is board_build.apply_netclasses


@pytest.mark.parametrize("module", NEW_MODULES, ids=lambda m: m.__name__)
def test_moved_functions_exist_by_name(module: ModuleType) -> None:
    for name in MOVED[module.__name__]:
        assert inspect.isfunction(getattr(module, name, None)), f"{module.__name__}.{name}"


@pytest.mark.parametrize("module", NEW_MODULES, ids=lambda m: m.__name__)
def test_new_modules_stay_small(module: ModuleType) -> None:
    lines = Path(module.__file__).read_text(encoding="utf-8").count("\n")
    assert lines < MAX_NEW_MODULE_LINES, f"{module.__name__}: {lines} lines"


def _imports(module: ModuleType) -> list[ast.Import | ast.ImportFrom]:
    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    return [n for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))]


@pytest.mark.parametrize("module", NEW_MODULES, ids=lambda m: m.__name__)
def test_new_modules_import_the_toolchain_as_modules(module: ModuleType) -> None:
    """tests monkeypatch tc.run / ipc.write_guard / env.discover on the module
    objects: a `from toolchain import run` copy would miss the patch."""
    late_bound = {"toolchain", "kicad_ipc", "kicad_env"}
    for node in _imports(module):
        if isinstance(node, ast.ImportFrom):
            assert node.module not in late_bound, f"{module.__name__}: from {node.module} import"
        names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module]
        assert "board_pipeline_tools" not in names, f"{module.__name__} imports its wrapper"
    if hasattr(module, "tc"):
        assert module.tc is toolchain
    if hasattr(module, "ipc"):
        assert module.ipc is kicad_ipc
    if hasattr(module, "env"):
        assert module.env is kicad_env


def test_importing_the_board_modules_keeps_server_tools_off_sys_path(tmp_path: Path) -> None:
    code = ("import os, sys\n"
            "sys.path.insert(0, sys.argv[1])\n"
            "import board_pipeline_tools, board_build, board_autoroute, board_fab\n"
            "tools = os.path.normcase(os.path.abspath(sys.argv[2]))\n"
            "print(sum(1 for p in sys.path if p and "
            "os.path.normcase(os.path.abspath(p)) == tools))\n")
    child_env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    r = subprocess.run([sys.executable, "-c", code, str(SERVER), str(SERVER / "tools")],
                       cwd=tmp_path, env=child_env, stdin=subprocess.DEVNULL,
                       capture_output=True, encoding="utf-8", errors="replace", timeout=120)
    assert r.returncode == 0, r.stderr[-1500:]
    assert r.stdout.strip().splitlines()[-1] == "0"


def _recorder(monkeypatch: pytest.MonkeyPatch, module: ModuleType,
              name: str) -> list[inspect.BoundArguments]:
    """Replace module.name with a fake that binds its call to the real
    signature and returns a sentinel result."""
    real = getattr(module, name)
    calls: list[inspect.BoundArguments] = []

    def fake(*args: object, **kwargs: object) -> dict:
        bound = inspect.signature(real).bind(*args, **kwargs)
        bound.apply_defaults()
        calls.append(bound)
        return {"ok": True, "sentinel": name}

    monkeypatch.setattr(module, name, fake)
    return calls


def test_build_board_from_netlist_delegates_to_board_build(
        monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _recorder(monkeypatch, board_build, "build")
    res = bpt.build_board_from_netlist("n.json", 30.0, 20.0, out_pcb="o.kicad_pcb",
                                       footprints_dir="fp", placement="p.json", pack_scale=1.5)
    assert res == {"ok": True, "sentinel": "build"} and len(calls) == 1
    assert {k: calls[0].arguments[k] for k in ("netlist", "board_w", "board_h", "out_pcb",
                                               "footprints_dir", "placement", "pack_scale")} == {
        "netlist": "n.json", "board_w": 30.0, "board_h": 20.0, "out_pcb": "o.kicad_pcb",
        "footprints_dir": "fp", "placement": "p.json", "pack_scale": 1.5}


def test_autoroute_board_delegates_to_board_autoroute_run(
        monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _recorder(monkeypatch, board_autoroute, "run")
    res = bpt.autoroute_board("b.kicad_pcb", 600, "fr.jar", 1500, from_snapshot=True)
    assert res == {"ok": True, "sentinel": "run"} and len(calls) == 1
    assert {k: calls[0].arguments[k] for k in ("pcb", "max_passes", "jar", "route_timeout",
                                               "from_snapshot")} == {
        "pcb": "b.kicad_pcb", "max_passes": 600, "jar": "fr.jar", "route_timeout": 1500,
        "from_snapshot": True}


def test_export_fab_package_delegates_to_board_fab_export(
        monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _recorder(monkeypatch, board_fab, "export")
    res = bpt.export_fab_package("b.kicad_pcb", "out", "z.zip", netlist="n.json")
    assert res == {"ok": True, "sentinel": "export"} and len(calls) == 1
    assert {k: calls[0].arguments[k] for k in ("pcb", "out_dir", "zip_name", "netlist")} == {
        "pcb": "b.kicad_pcb", "out_dir": "out", "zip_name": "z.zip", "netlist": "n.json"}


def test_wrapper_defaults_reach_the_moved_bodies(monkeypatch: pytest.MonkeyPatch) -> None:
    build = _recorder(monkeypatch, board_build, "build")
    route = _recorder(monkeypatch, board_autoroute, "run")
    fab = _recorder(monkeypatch, board_fab, "export")
    bpt.build_board_from_netlist("n.json", 30, 20)
    bpt.autoroute_board("b.kicad_pcb")
    bpt.export_fab_package("b.kicad_pcb")
    assert (build[0].arguments["out_pcb"], build[0].arguments["pack_scale"]) == ("", 1.0)
    assert (route[0].arguments["max_passes"], route[0].arguments["route_timeout"],
            route[0].arguments["from_snapshot"]) == (100, 300, False)
    assert (fab[0].arguments["out_dir"], fab[0].arguments["netlist"]) == ("", "")
