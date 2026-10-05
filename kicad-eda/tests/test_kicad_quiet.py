"""kicad_quiet: no modal wx dialogs from any KiCad-python child (D0).

Unit tier (no KiCad, no wx needed):
  * the module is a silent no-op without wx, configures wx exactly once
    (asserts off, log to a kept-alive wx.LogStderr), is idempotent across
    quiet() calls and importlib.reload, and fails loudly when wx is present
    but cannot be silenced;
  * a static walk over server/tools/*.py fails when any file that imports
    pcbnew does not import kicad_quiet (module level) before it;
  * create_pcb's inline `-c` child code imports kicad_quiet before pcbnew,
    proven by running that exact code against a stub pcbnew.

Live tier (skipped without a KiCad 9 bundled Python that imports pcbnew):
  a child that imports kicad_quiet, then trips SHAPE_POLY_SET::SquaredDistance
  (p, True) (a wx assert dialog on 9.0) and an EAGLE FootprintEnumerate of a
  missing .lbr (a wxLogError "can't open file" message box) exits 0 within
  60 s (stdin=DEVNULL) with the error text on stderr; after its first
  LoadBoard (KiCad's lazy initialisation) wx's active log target is still
  kicad_quiet.log_target() and a second SquaredDistance(p, True) still shows
  no dialog; create_pcb builds a real board through the same child-code path.
"""
from __future__ import annotations

import ast
import gc
import importlib
import json
import os
import subprocess
import sys
import types
import weakref
from pathlib import Path

import pytest

import kicad_env as env
import kicad_ipc
import pcb_ipc_tools
import toolchain as tc

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "server" / "tools"
LIVE_TIMEOUT_S = 60
# Entry scripts that run under KiCad's bundled Python and will always import
# pcbnew; the walk below must see them, so it can never pass vacuously.
CORE_PCBNEW_SCRIPTS = {"swig_build_board.py", "swig_gate_checks.py",
                       "swig_import_ses.py", "swig_export_dsn.py"}


# ---------------------------------------------------------------------------
# A fake wx: just the three calls kicad_quiet makes
# ---------------------------------------------------------------------------
class _FakeLog:
    """wx.Log stand-in. Like wx (which keeps a raw C++ pointer), it holds the
    active target only WEAKLY, so a target nobody else references dies."""

    def __init__(self) -> None:
        self.calls: list[weakref.ref] = []
        self.levels: list[int] = []
        self.previous = object()               # the target in place before quiet()

    def SetActiveTarget(self, target: object) -> object:  # noqa: N802 (wx API)
        self.calls.append(weakref.ref(target))
        old, self.previous = self.previous, None
        return old

    def SetLogLevel(self, level: int) -> None:  # noqa: N802 (wx API)
        self.levels.append(level)


def _fake_wx(*, broken: bool = False) -> types.ModuleType:
    wx = types.ModuleType("wx")
    wx.LOG_Info = 5                            # wx's value (Error 1 .. Info 5, Debug 6)
    wx.asserts_disabled = 0

    def disable_asserts() -> None:
        wx.asserts_disabled += 1

    class LogStderr:                           # a plain (weak-referenceable) object
        pass

    wx.DisableAsserts = disable_asserts
    wx.Log = _FakeLog()
    if not broken:                             # an old/odd wx without LogStderr
        wx.LogStderr = LogStderr
    return wx


@pytest.fixture
def load_quiet():
    """load(wx) -> a FRESH kicad_quiet imported with sys.modules['wx'] = wx
    (None makes `import wx` raise ImportError). The previous kicad_quiet and
    wx entries are restored afterwards."""
    saved = {name: sys.modules.pop(name) for name in ("kicad_quiet", "wx")
             if name in sys.modules}

    def load(wx: types.ModuleType | None) -> types.ModuleType:
        sys.modules["wx"] = wx
        sys.modules.pop("kicad_quiet", None)
        return importlib.import_module("kicad_quiet")

    yield load
    for name in ("kicad_quiet", "wx"):
        sys.modules.pop(name, None)
    sys.modules.update(saved)


def test_without_wx_it_is_a_silent_noop(load_quiet, capfd):
    q = load_quiet(None)
    assert q.ACTIVE is False
    assert q.log_target() is None
    assert q.quiet() is False                  # still nothing to do, still no error
    out, err = capfd.readouterr()
    assert out == "" and err == ""


def test_with_wx_asserts_are_off_and_log_goes_to_stderr(load_quiet):
    wx = _fake_wx()
    q = load_quiet(wx)
    assert q.ACTIVE is True
    assert wx.asserts_disabled == 1
    assert len(wx.Log.calls) == 1
    target = q.log_target()
    assert isinstance(target, wx.LogStderr)
    assert wx.Log.calls[0]() is target
    # errors, warnings and messages still reach stderr; wxLogDebug chatter does not
    assert wx.Log.levels == [wx.LOG_Info]


def test_the_log_target_is_kept_alive(load_quiet):
    wx = _fake_wx()
    load_quiet(wx)
    gc.collect()
    # the fake wx.Log only holds a weakref, exactly like wx's raw pointer: the
    # target survives only because kicad_quiet keeps a module-level reference
    assert wx.Log.calls[0]() is not None


def test_it_is_idempotent_across_calls_imports_and_reload(load_quiet):
    wx = _fake_wx()
    q = load_quiet(wx)
    target = q.log_target()
    assert q.quiet() is True
    assert importlib.import_module("kicad_quiet") is q
    importlib.reload(q)
    gc.collect()
    assert q.ACTIVE is True
    assert q.log_target() is target
    assert wx.asserts_disabled == 1 and len(wx.Log.calls) == 1
    assert wx.Log.levels == [wx.LOG_Info]
    assert wx.Log.calls[0]() is target


def test_wx_present_but_not_silenceable_fails_loudly(load_quiet):
    with pytest.raises(RuntimeError, match=r"kicad_quiet: wx is importable but"):
        load_quiet(_fake_wx(broken=True))


# ---------------------------------------------------------------------------
# Static check: every server/tools file that imports pcbnew quiets wx first
# ---------------------------------------------------------------------------
def _is_import_of(node: ast.AST, name: str) -> list[int]:
    """Alias positions in `node` that import module `name` (or a submodule);
    [] when it does not. `__import__("x")` / `import_module("x")` count too."""
    def hit(mod: str | None) -> bool:
        return bool(mod) and (mod == name or mod.startswith(name + "."))

    if isinstance(node, ast.Import):
        return [i for i, alias in enumerate(node.names) if hit(alias.name)]
    if isinstance(node, ast.ImportFrom):
        return [0] if node.level == 0 and hit(node.module) else []
    if isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Constant):
        func = node.func
        fname = func.id if isinstance(func, ast.Name) else \
            func.attr if isinstance(func, ast.Attribute) else ""
        if fname in ("__import__", "import_module") and node.args[0].value == name:
            return [0]
    return []


def quiet_violation(source: str) -> str | None:
    """Why `source` can reach pcbnew before kicad_quiet ran, or None. The
    first pcbnew import anywhere must come after a MODULE-LEVEL
    `import kicad_quiet` (a function-level one may never run first)."""
    tree = ast.parse(source)
    pcbnew_at = sorted((n.lineno, n.col_offset, i) for n in ast.walk(tree)
                       for i in _is_import_of(n, "pcbnew"))
    if not pcbnew_at:
        return None
    quiet_at = sorted((n.lineno, n.col_offset, i) for n in tree.body
                      for i in _is_import_of(n, "kicad_quiet"))
    if not quiet_at:
        return (f"imports pcbnew (line {pcbnew_at[0][0]}) without a module-level "
                "`import kicad_quiet`")
    if quiet_at[0] > pcbnew_at[0]:
        return (f"imports pcbnew at line {pcbnew_at[0][0]} before kicad_quiet "
                f"(line {quiet_at[0][0]})")
    return None


@pytest.mark.parametrize(("source", "ok"), [
    ("import pcbnew\n", False),
    ("import kicad_quiet\nimport pcbnew\n", True),
    ("import pcbnew\nimport kicad_quiet\n", False),
    ("import sys, pcbnew\n", False),
    ("import kicad_quiet, pcbnew\n", True),
    ("import pcbnew, kicad_quiet\n", False),
    ("from pcbnew import VECTOR2I\n", False),
    ("import kicad_quiet\nfrom pcbnew import VECTOR2I\n", True),
    ("def f():\n    import kicad_quiet\nimport pcbnew\n", False),
    ("import kicad_quiet\ndef f():\n    import pcbnew\n", True),
    ("def f():\n    import pcbnew\n", False),
    ("import kicad_quiet\ntry:\n    import pcbnew\nexcept ImportError:\n    pass\n", True),
    ("import importlib\npcbnew = importlib.import_module('pcbnew')\n", False),
    ("pcbnew = __import__('pcbnew')\n", False),
    ("import json\nimport sys\n", True),          # no pcbnew: nothing to check
])
def test_the_static_check_itself(source: str, ok: bool):
    assert (quiet_violation(source) is None) is ok, quiet_violation(source)


def test_every_tools_script_that_imports_pcbnew_imports_kicad_quiet_first():
    importers, offenders = set(), []
    for path in sorted(TOOLS.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        if any(_is_import_of(n, "pcbnew") for n in ast.walk(tree)):
            importers.add(path.name)
        why = quiet_violation(source)
        if why:
            offenders.append(f"{path.name}: {why}")
    assert CORE_PCBNEW_SCRIPTS <= importers, f"walk found only {sorted(importers)}"
    assert not offenders, ("add `import kicad_quiet` before `import pcbnew` "
                           "(docs/ROADMAP_DECISIONS.md D0):\n  " + "\n  ".join(offenders))


def test_kicad_quiet_itself_is_stdlib_plus_wx_only():
    tree = ast.parse((TOOLS / "kicad_quiet.py").read_text(encoding="utf-8"))
    mods = {alias.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import)
            for alias in n.names}
    mods |= {n.module.split(".")[0] for n in ast.walk(tree)
             if isinstance(n, ast.ImportFrom) and n.module}
    assert mods <= {"__future__", "wx"}, mods


# ---------------------------------------------------------------------------
# create_pcb: its inline -c child code
# ---------------------------------------------------------------------------
STUB_PCBNEW = '''\
import sys
QUIET_FIRST = "kicad_quiet" in sys.modules


def CreateEmptyBoard():
    return object()


def SaveBoard(path, board):
    with open(path, "w", encoding="utf-8") as f:
        f.write("quiet_first=%s" % QUIET_FIRST)
    return True
'''


@pytest.fixture
def no_ipc(monkeypatch):
    """create_pcb with no KiCad GUI to talk to."""
    monkeypatch.setattr(kicad_ipc, "connect", lambda: {"ipc_connected": False})


def test_create_pcb_child_code_imports_kicad_quiet_first(no_ipc, monkeypatch, tmp_path):
    """Run create_pcb's exact child argv, with the venv Python standing in for
    KiCad's and a stub pcbnew that records whether kicad_quiet was imported
    before it: proves both the import order and the sys.path wiring."""
    stub_dir = tmp_path / "stub"
    stub_dir.mkdir()
    (stub_dir / "pcbnew.py").write_text(STUB_PCBNEW, encoding="utf-8")
    seen: list[list[str]] = []

    def run_with_stub(args: list[str], **kw) -> subprocess.CompletedProcess:
        seen.append(list(args))
        kw.setdefault("stdin", subprocess.DEVNULL)
        child_env = {**os.environ, "PYTHONPATH": str(stub_dir)}
        return subprocess.run([sys.executable, *args[1:]], capture_output=True, text=True,
                              env=child_env, **kw)

    monkeypatch.setattr(tc, "kicad_python", lambda: "S:/KiCad/9.0/bin/python.exe")
    monkeypatch.setattr(tc, "run", run_with_stub)
    pcb = tmp_path / "out" / "empty.kicad_pcb"
    res = pcb_ipc_tools.create_pcb(str(pcb))
    assert res["ok"] and res["created"], res
    assert pcb.read_text(encoding="utf-8") == "quiet_first=True"
    (argv,) = seen
    assert argv[0] == "S:/KiCad/9.0/bin/python.exe" and argv[1] == "-c"
    assert quiet_violation(argv[2]) is None
    assert (Path(argv[-1]) / "kicad_quiet.py").is_file()


# ---------------------------------------------------------------------------
# Live tier: a real KiCad 9 child
# ---------------------------------------------------------------------------
CHILD = '''\
import json
import sys

sys.path.insert(0, sys.argv[1])
import kicad_quiet  # noqa: E402  (before pcbnew, like every entry script)

if not kicad_quiet.ACTIVE:      # no wx -> never risk pcbnew without the preamble
    sys.exit(3)
import pcbnew  # noqa: E402

poly = pcbnew.SHAPE_POLY_SET()
poly.NewOutline()
for x, y in ((0, 0), (10, 0), (10, 10), (0, 10)):
    poly.Append(pcbnew.FromMM(x), pcbnew.FromMM(y))
inside = pcbnew.VECTOR2I(pcbnew.FromMM(5), pcbnew.FromMM(5))
print("assert-path", flush=True)
outline_only = poly.SquaredDistance(inside, True)       # wx assert dialog on 9.0
print("log-path", flush=True)
eagle = pcbnew.PCB_IO_MGR.PluginFind(pcbnew.PCB_IO_MGR.EAGLE)
names = list(eagle.FootprintEnumerate(sys.argv[2]))     # wxLogError message box
pcbnew.SaveBoard(sys.argv[3], pcbnew.CreateEmptyBoard())
loaded = pcbnew.LoadBoard(sys.argv[3])    # the first load logs wxLogDebug chatter
# KiCad initialises lazily on the first LoadBoard: the quiet state must outlive
# it (same log target, asserts still off), not just hold before it.
import wx  # noqa: E402
target_kept = wx.Log.GetActiveTarget() is kicad_quiet.log_target()
print("assert-path-after-load", flush=True)
outline_after_load = poly.SquaredDistance(inside, True)  # the 9.0 assert again
print(json.dumps({"version": pcbnew.Version(), "outline_only": int(outline_only),
                  "names": names, "loaded": loaded is not None,
                  "target_kept": target_kept,
                  "outline_after_load": int(outline_after_load)}), flush=True)
'''


@pytest.fixture(scope="module")
def kicad9_python() -> Path:
    """A KiCad 9.x bundled Python verified (by kicad_env) to import pcbnew."""
    for inst in env.discover():
        if inst.version_tuple[0] == 9 and inst.python is not None:
            return inst.python
    pytest.skip("no KiCad 9 bundled Python with pcbnew (the 9.0 SquaredDistance "
                "assert and Eagle log dialog are what this proves quiet)")


def test_live_kicad9_child_with_kicad_quiet_never_shows_a_dialog(kicad9_python, tmp_path):
    child = tmp_path / "child.py"
    child.write_text(CHILD, encoding="utf-8")
    missing = tmp_path / "no_such_dir" / "missing.lbr"
    empty = tmp_path / "empty.kicad_pcb"
    argv = [str(kicad9_python), str(child), str(TOOLS), str(missing), str(empty)]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True,
                              stdin=subprocess.DEVNULL, timeout=LIVE_TIMEOUT_S)
    except subprocess.TimeoutExpired as e:
        pytest.fail(f"KiCad 9 child hung for {LIVE_TIMEOUT_S} s (a modal wx dialog?): "
                    f"stdout={e.stdout!r} stderr={e.stderr!r}")
    assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
    assert "assert-path" in proc.stdout and "log-path" in proc.stdout
    assert "assert-path-after-load" in proc.stdout, proc.stdout
    result = json.loads(proc.stdout.strip().splitlines()[-1])
    assert result["version"].startswith("9."), result
    assert result["names"] == [] and result["loaded"]
    # the quiet state survived KiCad's lazy initialisation in the first LoadBoard
    assert result["target_kept"] is True, result
    assert result["outline_after_load"] == result["outline_only"], result
    assert "can't open file" in proc.stderr and "missing.lbr" in proc.stderr, proc.stderr
    # the first LoadBoard logs a dozen wxLogDebug lines; below Info they stay out
    assert "Adding duplicate image handler" not in proc.stderr, proc.stderr


def test_live_create_pcb_child_code_on_kicad9(kicad9_python, no_ipc, monkeypatch, tmp_path):
    monkeypatch.setattr(tc, "kicad_python", lambda: str(kicad9_python))
    pcb = tmp_path / "empty.kicad_pcb"
    res = pcb_ipc_tools.create_pcb(str(pcb))
    assert res["ok"] and res["created"], res
    head = pcb.read_text(encoding="utf-8")[:400]
    assert head.startswith("(kicad_pcb") and '(generator_version "9.0")' in head, head
