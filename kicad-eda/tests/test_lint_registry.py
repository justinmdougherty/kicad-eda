"""lint_netlist's rule registry (F2-03): discovery of lint_rules_*.py modules,
(ORDER, name) ordering, LINT-INTERNAL isolation, LintContext and the CLI context."""
from __future__ import annotations

import dataclasses
import json
import sys
import textwrap
from pathlib import Path
from types import MappingProxyType, ModuleType

import pytest

import libtable
import lint_context
import lint_netlist
import lint_rules_core
import lint_rules_libs
from lint_context import LintContext, context_for

TOOLS = Path(lint_netlist.__file__).resolve().parent

GOOD = """
ORDER = {order}


def check(design, ctx):
    return ["{tag} error"], ["{tag} warning"]
"""

# reports what the rule saw, so a test can read the context back from the warnings
ECHO = """
ORDER = 99


def check(design, ctx):
    return [], [f"symbols={ctx.symbols!r} netlist_dir={ctx.netlist_dir!r}"]
"""


def _write(directory: Path, name: str, body: str) -> Path:
    path = directory / f"{name}.py"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


def _good(directory: Path, name: str, order: int) -> Path:
    return _write(directory, name, GOOD.format(order=order, tag=name))


def _design() -> dict:
    return {"components": [{"ref": "R1"}, {"ref": "R2"}],
            "nets": {"N1": [["R1", "1"], ["R2", "1"]]}, "no_connect": []}


@pytest.fixture
def registry(monkeypatch):
    """Make lint() run exactly the given rule modules."""
    def use(*rules: lint_netlist.RuleModule) -> None:
        monkeypatch.setattr(lint_netlist, "rule_modules", lambda search_dir=None: tuple(rules))
    return use


# --- discovery of the shipped modules -----------------------------------------

def test_default_registry_runs_core_then_libs_first():
    rules = lint_netlist.rule_modules()
    assert [r.name for r in rules[:2]] == ["lint_rules_core", "lint_rules_libs"]
    assert [r.order for r in rules[:2]] == [0, 10]
    assert lint_rules_core.ORDER == 0 and lint_rules_libs.ORDER == 10


def test_every_shipped_rule_module_loads_and_is_sorted():
    rules = lint_netlist.rule_modules()
    assert {r.name for r in rules} == {p.stem for p in TOOLS.glob("lint_rules_*.py")}
    assert [(r.name, r.problem) for r in rules if r.problem] == []
    assert list(rules) == sorted(rules, key=lambda r: (r.order, r.name))


def test_default_registry_shares_the_imported_modules():
    # discovery imports the modules under their own names: no second copy
    rules = {r.name: r for r in lint_netlist.rule_modules()}
    assert rules["lint_rules_core"].check is lint_rules_core.check
    assert rules["lint_rules_libs"].check is lint_rules_libs.check
    assert sys.modules["lint_rules_core"] is lint_rules_core


def test_libs_stub_finds_nothing_in_an_empty_design():
    assert lint_rules_libs.check({}, LintContext()) == ([], [])


def test_a_shipped_module_not_yet_imported_is_loaded_once_under_its_name(monkeypatch):
    monkeypatch.delitem(sys.modules, "lint_rules_libs")      # restored after the test
    first = {r.name: r for r in lint_netlist.rule_modules()}["lint_rules_libs"]
    fresh = sys.modules["lint_rules_libs"]
    assert fresh is not lint_rules_libs and first.check is fresh.check
    assert Path(fresh.__file__).resolve() == TOOLS / "lint_rules_libs.py"
    again = {r.name: r for r in lint_netlist.rule_modules()}["lint_rules_libs"]
    assert again.check is first.check and sys.modules["lint_rules_libs"] is fresh


def test_a_shipped_name_taken_by_another_file_is_reported(monkeypatch, tmp_path):
    impostor = ModuleType("lint_rules_libs")
    impostor.__file__ = str(tmp_path / "lint_rules_libs.py")
    monkeypatch.setitem(sys.modules, "lint_rules_libs", impostor)
    rule = {r.name: r for r in lint_netlist.rule_modules()}["lint_rules_libs"]
    assert rule.check is None
    assert rule.problem.startswith("import failed: ImportError: module name "
                                   "'lint_rules_libs' is already taken by")


def test_a_shared_import_that_raises_leaves_no_module_behind(tmp_path):
    name = "lint_rules_f203_probe"
    bad = _write(tmp_path, name, "raise RuntimeError('half loaded')\n")
    try:
        with pytest.raises(RuntimeError, match="half loaded"):
            lint_netlist._import_shared(bad)
        assert name not in sys.modules
        good = _good(tmp_path, name, 1)
        module = lint_netlist._import_shared(good)
        assert sys.modules[name] is module and module.ORDER == 1
        assert lint_netlist._import_shared(good) is module
    finally:
        sys.modules.pop(name, None)


# --- discovery in a search_dir ------------------------------------------------

def test_discovers_a_fake_module_in_search_dir_and_orders_it_by_order(tmp_path):
    _good(tmp_path, "lint_rules_zz", 5)
    _good(tmp_path, "lint_rules_aa", 7)                  # name sorts first, ORDER later
    _good(tmp_path, "lint_rules_mm", 5)                  # same ORDER: by name
    _write(tmp_path, "lint_rulesx", "ORDER = 0\n")       # not lint_rules_*: ignored
    _write(tmp_path, "helper", "raise RuntimeError('never imported')\n")
    (tmp_path / "lint_rules_note.txt").write_text("not a module", encoding="utf-8")
    rules = lint_netlist.rule_modules(search_dir=tmp_path)
    assert [(r.name, r.order) for r in rules] == [
        ("lint_rules_mm", 5), ("lint_rules_zz", 5), ("lint_rules_aa", 7)]
    assert all(r.check is not None and r.problem == "" for r in rules)
    errors, warnings = lint_netlist.run_rules(_design(), LintContext(), rules)
    assert errors == ["lint_rules_mm error", "lint_rules_zz error", "lint_rules_aa error"]
    assert warnings == ["lint_rules_mm warning", "lint_rules_zz warning",
                        "lint_rules_aa warning"]


def test_search_dir_accepts_a_string_and_an_empty_dir(tmp_path):
    assert lint_netlist.rule_modules(search_dir=str(tmp_path)) == ()
    _good(tmp_path, "lint_rules_one", 1)
    assert [r.name for r in lint_netlist.rule_modules(str(tmp_path))] == ["lint_rules_one"]


def test_missing_search_dir_is_an_error(tmp_path):
    with pytest.raises(NotADirectoryError, match="nope"):
        lint_netlist.rule_modules(search_dir=tmp_path / "nope")


def test_a_fake_module_named_like_a_shipped_one_does_not_replace_it(tmp_path):
    _good(tmp_path, "lint_rules_core", 3)
    (fake,) = lint_netlist.rule_modules(search_dir=tmp_path)
    assert fake.name == "lint_rules_core" and fake.order == 3
    assert fake.check is not lint_rules_core.check
    assert sys.modules["lint_rules_core"] is lint_rules_core
    assert {r.name: r.check for r in lint_netlist.rule_modules()}[
        "lint_rules_core"] is lint_rules_core.check


def test_search_dir_modules_are_reloaded_from_disk(tmp_path):
    _good(tmp_path, "lint_rules_live", 1)
    assert lint_netlist.rule_modules(tmp_path)[0].order == 1
    _good(tmp_path, "lint_rules_live", 2)
    assert lint_netlist.rule_modules(tmp_path)[0].order == 2


# --- broken modules: one LINT-INTERNAL error each, never a crash ---------------

def test_module_without_check_yields_one_lint_internal_error(tmp_path):
    _write(tmp_path, "lint_rules_nocheck", "ORDER = 3\n")
    _good(tmp_path, "lint_rules_ok", 4)
    rules = lint_netlist.rule_modules(search_dir=tmp_path)
    assert [(r.name, r.order, r.check is None) for r in rules] == [
        ("lint_rules_nocheck", 3, True), ("lint_rules_ok", 4, False)]
    errors, warnings = lint_netlist.run_rules(_design(), LintContext(), rules)
    internal = [e for e in errors if e.startswith("LINT-INTERNAL")]
    assert internal == ["LINT-INTERNAL lint_rules_nocheck: rule module needs a "
                        "check(design, ctx) function"]
    assert errors[1:] == ["lint_rules_ok error"] and warnings == ["lint_rules_ok warning"]


def test_module_without_order_sorts_at_the_default_and_is_reported(tmp_path):
    _write(tmp_path, "lint_rules_noorder", "def check(design, ctx):\n    return [], []\n")
    _write(tmp_path, "lint_rules_boolorder", "ORDER = True\n")      # bool is not an ORDER
    _good(tmp_path, "lint_rules_late", lint_netlist.DEFAULT_ORDER + 1)
    rules = lint_netlist.rule_modules(search_dir=tmp_path)
    assert [(r.name, r.order) for r in rules] == [
        ("lint_rules_boolorder", lint_netlist.DEFAULT_ORDER),
        ("lint_rules_noorder", lint_netlist.DEFAULT_ORDER),
        ("lint_rules_late", lint_netlist.DEFAULT_ORDER + 1)]
    errors, _ = lint_netlist.run_rules({}, LintContext(), rules)
    assert errors == [
        "LINT-INTERNAL lint_rules_boolorder: rule module needs an integer ORDER and a "
        "check(design, ctx) function",
        "LINT-INTERNAL lint_rules_noorder: rule module needs an integer ORDER",
        "lint_rules_late error"]


@pytest.mark.parametrize("body, reason", [
    ("raise RuntimeError('boom')\n", "import failed: RuntimeError: boom"),
    ("def check(design, ctx)\n", "import failed: SyntaxError: "),
    ("import sys\nsys.exit(3)\n", "import failed: SystemExit: 3"),
    ("import no_such_module_f203\n", "import failed: ModuleNotFoundError: "),
])
def test_module_that_fails_to_import_yields_one_lint_internal_error(tmp_path, body, reason):
    _write(tmp_path, "lint_rules_broken", body)
    (rule,) = lint_netlist.rule_modules(search_dir=tmp_path)
    assert rule.check is None and rule.order == lint_netlist.DEFAULT_ORDER
    errors, warnings = lint_netlist.run_rules({}, LintContext(), (rule,))
    assert len(errors) == 1 and warnings == []
    assert errors[0].startswith(f"LINT-INTERNAL lint_rules_broken: {reason}")


def test_raising_check_is_isolated_and_the_rest_still_run(tmp_path):
    _good(tmp_path, "lint_rules_a", 1)
    _write(tmp_path, "lint_rules_b", """
        ORDER = 2


        def check(design, ctx):
            raise ValueError("bad netlist shape")
        """)
    _good(tmp_path, "lint_rules_c", 3)
    errors, warnings = lint_netlist.run_rules(
        _design(), LintContext(), lint_netlist.rule_modules(tmp_path))
    assert errors == ["lint_rules_a error",
                      "LINT-INTERNAL lint_rules_b: ValueError: bad netlist shape",
                      "lint_rules_c error"]
    assert warnings == ["lint_rules_a warning", "lint_rules_c warning"]


@pytest.mark.parametrize("returned, reason", [
    ("None", "check() returned NoneType, expected (errors, warnings)"),
    ("(['e'],)", "check() returned tuple, expected (errors, warnings)"),
    ("([1], [])", "check() errors must be a list of str"),
    ("([], 'warning')", "check() warnings must be a list of str"),
])
def test_malformed_check_result_yields_one_lint_internal_error(tmp_path, returned, reason):
    _write(tmp_path, "lint_rules_shape",
           f"ORDER = 1\n\n\ndef check(design, ctx):\n    return {returned}\n")
    errors, warnings = lint_netlist.run_rules({}, LintContext(),
                                              lint_netlist.rule_modules(tmp_path))
    assert errors == [f"LINT-INTERNAL lint_rules_shape: {reason}"] and warnings == []


def test_tuple_results_are_returned_as_lists(tmp_path):
    _write(tmp_path, "lint_rules_tuple",
           "ORDER = 1\n\n\ndef check(design, ctx):\n    return ('e',), ('w',)\n")
    assert lint_netlist.run_rules({}, LintContext(), lint_netlist.rule_modules(tmp_path)) \
        == (["e"], ["w"])


def test_a_rule_cannot_change_the_callers_design_or_what_later_rules_see(tmp_path):
    _write(tmp_path, "lint_rules_mutate", """
        ORDER = 1


        def check(design, ctx):
            design["components"].append({"ref": "X9"})
            design.pop("nets")
            return [], []
        """)
    _write(tmp_path, "lint_rules_count", """
        ORDER = 2


        def check(design, ctx):
            return [], [f"{len(design['components'])} parts, nets={'nets' in design}"]
        """)
    design = _design()
    _, warnings = lint_netlist.run_rules(design, LintContext(),
                                         lint_netlist.rule_modules(tmp_path))
    assert warnings == ["2 parts, nets=True"]
    assert design == _design()


# --- lint(): registry wiring, ctx=None and an explicit ctx --------------------

def test_lint_reports_lint_internal_and_keeps_the_other_results(tmp_path, registry):
    _write(tmp_path, "lint_rules_raise",
           "ORDER = 5\n\n\ndef check(design, ctx):\n    raise KeyError('ref')\n")
    _good(tmp_path, "lint_rules_tail", 7)
    core = [r for r in lint_netlist.rule_modules() if r.name == "lint_rules_core"]
    registry(*core, *lint_netlist.rule_modules(tmp_path))
    d = _design()
    d["nets"]["FLOAT"] = [["R1", "2"]]
    rep = lint_netlist.lint(d)
    assert rep["ok"] is False
    assert rep["errors"] == ["E3 net 'FLOAT' has 1 pad(s) (needs >=2)",
                             "LINT-INTERNAL lint_rules_raise: KeyError: 'ref'",
                             "lint_rules_tail error"]
    assert rep["warnings"] == ["lint_rules_tail warning"]
    assert (rep["components"], rep["nets"]) == (2, 2)


def test_lint_internal_alone_makes_lint_fail(tmp_path, registry):
    _write(tmp_path, "lint_rules_nocheck", "ORDER = 1\n")
    registry(*lint_netlist.rule_modules(tmp_path))
    rep = lint_netlist.lint(_design())
    assert rep["ok"] is False and len(rep["errors"]) == 1
    assert rep["errors"][0].startswith("LINT-INTERNAL lint_rules_nocheck:")


def test_lint_builds_a_context_when_ctx_is_none(tmp_path, registry):
    _write(tmp_path, "lint_rules_echo", ECHO)
    registry(*lint_netlist.rule_modules(tmp_path))
    assert lint_netlist.lint(_design())["warnings"] == ["symbols='' netlist_dir=None"]
    assert lint_netlist.lint(_design(), "C:/syms")["warnings"] == \
        ["symbols='C:/syms' netlist_dir=None"]


def test_lint_passes_an_explicit_ctx_through(tmp_path, registry):
    _write(tmp_path, "lint_rules_echo", ECHO)
    registry(*lint_netlist.rule_modules(tmp_path))
    ctx = LintContext(symbols="S", netlist_dir=tmp_path)
    assert lint_netlist.lint(_design(), ctx=ctx)["warnings"] == \
        [f"symbols='S' netlist_dir={tmp_path!r}"]
    # the context's own symbols win over the positional argument ...
    assert lint_netlist.lint(_design(), "other", ctx=ctx)["warnings"] == \
        [f"symbols='S' netlist_dir={tmp_path!r}"]
    # ... and the positional argument fills a context that has none
    bare = LintContext(netlist_dir=tmp_path)
    assert lint_netlist.lint(_design(), "S2", ctx=bare)["warnings"] == \
        [f"symbols='S2' netlist_dir={tmp_path!r}"]
    assert bare.symbols == ""                                   # ctx itself unchanged


def test_lint_rejects_a_ctx_that_is_not_a_lint_context():
    with pytest.raises(TypeError, match="LintContext"):
        lint_netlist.lint(_design(), ctx={"symbols": ""})


@pytest.mark.parametrize("design", [None, [], "netlist.json"])
def test_lint_rejects_a_design_that_is_not_an_object(design):
    with pytest.raises(TypeError, match="JSON object"):
        lint_netlist.lint(design)


def test_lint_counts_survive_malformed_sections(registry):
    registry()
    rep = lint_netlist.lint({"components": None, "nets": 7})
    assert (rep["components"], rep["nets"]) == (0, 0) and rep["ok"] is True


# --- LintContext ----------------------------------------------------------------

def test_lint_context_defaults():
    ctx = LintContext()
    assert (ctx.symbols, ctx.resolver, ctx.library_error, ctx.netlist_dir,
            ctx.swig_fp_version_max) == ("", None, "", None, None)
    assert isinstance(ctx.options, MappingProxyType) and dict(ctx.options) == {}
    assert ctx.symbols_dir is None


def test_lint_context_is_frozen_and_its_options_are_a_read_only_copy():
    opts = {"strict": True}
    ctx = LintContext(options=opts)
    opts["strict"] = False                                      # caller's dict: not shared
    assert ctx.options["strict"] is True
    with pytest.raises(TypeError):
        ctx.options["strict"] = False                           # type: ignore[index]
    with pytest.raises(dataclasses.FrozenInstanceError):
        ctx.symbols = "x"                                       # type: ignore[misc]
    assert dataclasses.replace(ctx, library_error="e").options["strict"] is True


def test_lint_context_normalises_netlist_dir_to_a_path(tmp_path):
    assert LintContext(netlist_dir="").netlist_dir is None
    assert LintContext(netlist_dir=str(tmp_path)).netlist_dir == tmp_path
    assert LintContext(netlist_dir=tmp_path).netlist_dir == tmp_path


@pytest.mark.parametrize("kwargs, match", [
    ({"symbols": 42}, "symbols"),
    ({"symbols": None}, "symbols"),
    ({"resolver": "C:/libs"}, "resolver"),
    ({"library_error": None}, "library_error"),
    ({"netlist_dir": 3}, "netlist_dir"),
    ({"swig_fp_version_max": True}, "swig_fp_version_max"),
    ({"swig_fp_version_max": "20241229"}, "swig_fp_version_max"),
    ({"options": [("a", 1)]}, "options"),
])
def test_lint_context_validates_its_fields(kwargs, match):
    with pytest.raises(TypeError, match=match):
        LintContext(**kwargs)


def test_lint_context_accepts_every_symbol_source(tmp_path):
    resolver = libtable.LibraryResolver.for_directory(tmp_path, tmp_path, None)
    for source in ("C:/syms", tmp_path, resolver, resolver.library_map()):
        assert LintContext(symbols=source, resolver=resolver,
                           swig_fp_version_max=20241229).symbols is source


def test_symbols_dir_of_each_source(tmp_path):
    resolver = libtable.LibraryResolver.for_directory(tmp_path, tmp_path / "stock", None)
    assert LintContext(symbols="C:/syms").symbols_dir == "C:/syms"
    assert LintContext(symbols=tmp_path).symbols_dir == str(tmp_path)
    assert LintContext(symbols=resolver).symbols_dir == str(tmp_path / "stock")
    assert LintContext(symbols=resolver.library_map()).symbols_dir == str(tmp_path / "stock")
    assert LintContext(symbols={}).symbols_dir is None
    no_stock = libtable.LibraryResolver.for_directory(tmp_path, None, None)
    assert lint_context.symbols_dir_of(no_stock) is None


def test_context_for_each_kind_of_argument(tmp_path):
    assert context_for("") == LintContext() and context_for(None) == LintContext()
    assert context_for("C:/syms") == LintContext(symbols="C:/syms")
    ctx = LintContext(netlist_dir=tmp_path)
    assert context_for(ctx) is ctx
    resolver = libtable.LibraryResolver.for_directory(tmp_path, tmp_path, None)
    from_resolver = context_for(resolver)
    assert from_resolver.symbols is resolver and from_resolver.resolver is resolver
    assert from_resolver.netlist_dir == tmp_path
    lib_map = resolver.library_map()
    from_map = context_for(lib_map)
    assert from_map.symbols is lib_map and from_map.resolver is None
    assert from_map.netlist_dir == tmp_path
    with pytest.raises(TypeError, match="symbols"):
        context_for(3.5)


# --- the CLI context ------------------------------------------------------------

def _netlist(tmp_path: Path, design: dict) -> Path:
    path = tmp_path / "netlist.json"
    path.write_text(json.dumps(design), encoding="utf-8")
    return path


def test_cli_context_without_dirs_carries_the_netlist_dir(tmp_path):
    ctx = lint_netlist.cli_context(_netlist(tmp_path, _design()))
    assert ctx == LintContext(netlist_dir=tmp_path)


def test_cli_context_builds_a_resolver_from_the_dirs(tmp_path):
    (tmp_path / "fp").mkdir()
    netlist = _netlist(tmp_path, _design())
    ctx = lint_netlist.cli_context(netlist, str(tmp_path), str(tmp_path / "fp"))
    assert isinstance(ctx.resolver, libtable.LibraryResolver)
    assert ctx.symbols is ctx.resolver and ctx.library_error == ""
    assert ctx.resolver.stock_dirs == {"symbol": tmp_path, "footprint": tmp_path / "fp"}
    assert ctx.netlist_dir == tmp_path
    only_fp = lint_netlist.cli_context(netlist, "", str(tmp_path / "fp"))
    assert only_fp.resolver is not None and only_fp.symbols == ""   # no symbols: no W2


def test_cli_context_reports_a_bad_libraries_block(tmp_path):
    netlist = _netlist(tmp_path, {**_design(), "libraries": {"bogus": {}}})
    ctx = lint_netlist.cli_context(netlist, str(tmp_path))
    assert ctx.resolver is None and ctx.symbols == str(tmp_path)
    assert "bogus" in ctx.library_error


def test_main_lints_with_the_cli_context(tmp_path, monkeypatch, capsys, registry):
    _write(tmp_path, "lint_rules_echo", ECHO)
    registry(*lint_netlist.rule_modules(tmp_path))
    netlist = _netlist(tmp_path, _design())
    assert lint_netlist.main([str(netlist)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["warnings"] == [f"symbols='' netlist_dir={tmp_path!r}"]


@pytest.mark.parametrize("argv", [[], ["a.json", "s", "f", "extra"]])
def test_main_usage_error(argv, capsys):
    assert lint_netlist.main(argv) == lint_netlist.USAGE_EXIT
    assert "Usage" in capsys.readouterr().err


def test_main_unreadable_or_non_object_netlist(tmp_path, capsys):
    assert lint_netlist.main([str(tmp_path / "missing.json")]) == lint_netlist.USAGE_EXIT
    assert "cannot read" in capsys.readouterr().err
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert lint_netlist.main([str(bad)]) == lint_netlist.USAGE_EXIT
    listed = tmp_path / "list.json"
    listed.write_text("[1, 2]", encoding="utf-8")
    assert lint_netlist.main([str(listed)]) == lint_netlist.USAGE_EXIT
    assert "JSON object" in capsys.readouterr().err
