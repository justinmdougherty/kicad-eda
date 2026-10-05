"""The pipeline feature-hook layer (pipeline/rp_hooks.py): discovery of
pipeline/rp_feature_*.py, the helper kinds, Halt, the route-hash registry merge,
and every hook call site in a scripted run (tests/fixtures/pipeline_world.py).

The golden checks are RELATIVE: a run with no feature is compared, at test
time, with the same run under a no-op feature implementing every hook, so the
stage owners after F5-01b can change the report shape without touching this
file. Equivalence with F5-01's own report was proven once by a real run.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import sys
import types
from pathlib import Path
from typing import Any

import pytest

import remediate
import rp_cli
import rp_context
import rp_hooks
import rp_route
import rp_route_hash
import rp_stage_place
import rp_stage_schematic
import run_pipeline as rp
from fixtures import pipeline_world
from fixtures.pipeline_world import (CLEAN_DRC, DESIGN, OK_ANALYSIS, OK_BUILD, World,
                                     recording_feature)

world = pipeline_world.world            # the fixtures (assignments: see pipeline_world)
recorder = pipeline_world.recorder

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
NOT_IN_A_PASS_RUN = {"on_exhausted", "handoff_veto"}   # exhausted ladder / routing handoff only


def _feature(name: str, order: int = rp_hooks.DEFAULT_ORDER, **hooks: Any) -> types.SimpleNamespace:
    return types.SimpleNamespace(NAME=name, ORDER=order, **hooks)


@pytest.fixture
def use(monkeypatch):
    """use(*features): enable exactly these features; discovery comes back at teardown."""
    monkeypatch.setattr(rp_hooks, "_override", rp_hooks.selection())
    return lambda *features: rp_hooks.set_features(list(features))


# --- discovery -------------------------------------------------------------------------

@pytest.fixture
def feature_dir(tmp_path, monkeypatch):
    """An empty FEATURE_DIR on sys.path with a fresh import cache and discovery on."""
    d = tmp_path / "features"
    d.mkdir()
    monkeypatch.setattr(rp_hooks, "FEATURE_DIR", d)
    monkeypatch.setattr(rp_hooks, "_modules", {})
    monkeypatch.setattr(rp_hooks, "_override", None)
    monkeypatch.delenv(rp_hooks.ENV_VAR, raising=False)
    monkeypatch.syspath_prepend(str(d))
    before = set(sys.modules)
    yield d
    for name in set(sys.modules) - before:
        if name.startswith(rp_hooks.PREFIX):
            del sys.modules[name]


def _write(d: Path, name: str, body: str = "") -> None:
    (d / f"{rp_hooks.PREFIX}{name}.py").write_text(body, encoding="utf-8")


def _names(features: tuple) -> list[str]:
    return [rp_hooks.feature_name(f) for f in features]


def test_features_are_sorted_by_order_then_name(feature_dir):
    _write(feature_dir, "alpha")
    _write(feature_dir, "gamma", "ORDER = 10\n")
    _write(feature_dir, "beta", "ORDER = 10\n")
    _write(feature_dir, "delta", "ORDER = 5\n")
    (feature_dir / "not_a_feature.py").write_text("raise SystemExit", encoding="utf-8")
    assert rp_hooks.available() == ("alpha", "beta", "delta", "gamma")
    assert _names(rp_hooks.features()) == ["delta", "beta", "gamma", "alpha"]


def test_features_are_imported_once_per_process(feature_dir, tmp_path):
    log = tmp_path / "imports.log"
    _write(feature_dir, "once", f"open({str(log)!r}, 'a').write('x')\n")
    first = rp_hooks.features()
    assert rp_hooks.features()[0] is first[0] and rp_hooks.features()[0] is first[0]
    assert log.read_text() == "x"


def test_the_env_filter_restricts_discovery(feature_dir, monkeypatch):
    for name, order in (("alpha", 1), ("beta", 2), ("gamma", 3)):
        _write(feature_dir, name, f"ORDER = {order}\n")
    _write(feature_dir, "broken", "raise ImportError('never imported')\n")
    monkeypatch.setenv(rp_hooks.ENV_VAR, "gamma, rp_feature_alpha")
    assert _names(rp_hooks.features()) == ["alpha", "gamma"]
    monkeypatch.setenv(rp_hooks.ENV_VAR, "NONE")
    assert rp_hooks.features() == ()
    monkeypatch.setenv(rp_hooks.ENV_VAR, "alpha,nosuch")
    with pytest.raises(rp_hooks.FeatureError, match="nosuch"):
        rp_hooks.features()
    monkeypatch.setenv(rp_hooks.ENV_VAR, " ")
    with pytest.raises(rp_hooks.FeatureError, match="rp_feature_broken"):
        rp_hooks.features()                      # unset/blank = every module


@pytest.mark.parametrize("body", ["import no_such_module_xyz\n", "def broken(:\n",
                                  "raise RuntimeError('boom')\n"],
                         ids=["import-error", "syntax-error", "runtime-error"])
def test_an_import_error_is_a_hard_error_naming_the_module(feature_dir, body):
    _write(feature_dir, "ok")
    _write(feature_dir, "bad", body)
    with pytest.raises(rp_hooks.FeatureError, match="rp_feature_bad") as err:
        rp_hooks.features()
    assert err.value.__cause__ is not None


@pytest.mark.parametrize("order", ["'10'", "True", "1.5"])
def test_a_non_int_order_is_refused(feature_dir, order):
    _write(feature_dir, "odd", f"ORDER = {order}\n")
    with pytest.raises(rp_hooks.FeatureError, match="ORDER"):
        rp_hooks.features()


def test_set_features_takes_names_or_objects_and_none_restores_discovery(feature_dir):
    _write(feature_dir, "alpha", "ORDER = 50\n")
    _write(feature_dir, "beta")
    rp_hooks.set_features(["beta", _feature("mem", order=60), "rp_feature_alpha"])
    assert _names(rp_hooks.features()) == ["alpha", "mem", "beta"]
    assert rp_hooks.selection() == rp_hooks.features()
    rp_hooks.set_features([])
    assert rp_hooks.features() == ()
    rp_hooks.set_features(None)
    assert rp_hooks.selection() is None and _names(rp_hooks.features()) == ["alpha", "beta"]


@pytest.mark.parametrize("chosen, message", [
    (["nosuch"], "nosuch"),
    ([_feature("twin"), _feature("twin")], "twin"),
    ("alpha", "list"),
    ([types.SimpleNamespace(ORDER=1)], "NAME"),
], ids=["unknown-name", "duplicate", "bare-str", "nameless"])
def test_set_features_refuses_bad_selections(feature_dir, chosen, message):
    _write(feature_dir, "alpha")
    with pytest.raises(rp_hooks.FeatureError, match=message):
        rp_hooks.set_features(chosen)


def test_only_table_names_are_hooks_and_must_be_functions(use):
    calls = []
    use(_feature("a", helper=lambda *a: calls.append("helper"), ROUTE="x",
                 on_load=lambda ctx: calls.append("on_load")))
    rp_hooks.notify("on_load", object())
    assert calls == ["on_load"]
    use(_feature("b", g2_policy={"max_rounds": 1}))
    with pytest.raises(rp_hooks.FeatureError, match="b.*g2_policy.*not a function"):
        rp_hooks.first("g2_policy", object())


# --- the helper kinds -------------------------------------------------------------------

def test_notify_calls_every_feature_in_order(use):
    seen = []
    use(_feature("late", 9, on_load=lambda ctx: seen.append(("late", ctx))),
        _feature("early", 1, on_load=lambda ctx: seen.append(("early", ctx))))
    assert rp_hooks.notify("on_load", "CTX") is None
    assert seen == [("early", "CTX"), ("late", "CTX")]


def test_chain_threads_the_value_and_none_keeps_it(use):
    use(_feature("a", 1, g1_verdict=lambda ctx, v: {**v, "warnings": v["warnings"] + ["a"]}),
        _feature("b", 2, g1_verdict=lambda ctx, v: None),
        _feature("c", 3, g1_verdict=lambda ctx, v: {**v, "warnings": v["warnings"] + [ctx]}))
    v = {"gate": "G1", "passed": True, "blockers": [], "warnings": []}
    assert rp_hooks.chain("g1_verdict", "c-ctx", v)["warnings"] == ["a", "c-ctx"]
    assert v["warnings"] == []                              # the input is never mutated


def test_chain_of_a_tuple_passes_and_returns_its_items(use):
    use(_feature("a", 1, g3_inputs=lambda ctx, cy, pl: (cy, {**pl, "issues": ["x"]})),
        _feature("b", 2, g3_inputs=lambda ctx, cy, pl: None),
        _feature("c", 3, g3_inputs=lambda ctx, cy, pl: ({"courtyard_violations": 1}, pl)))
    cy, pl = rp_hooks.chain("g3_inputs", None, ({"courtyard_violations": 0},
                                                 {"issues": [], "warnings": []}))
    assert cy == {"courtyard_violations": 1} and pl["issues"] == ["x"]


def test_first_returns_the_first_non_none_in_feature_order(use):
    use(_feature("a", 1, handoff_veto=lambda ctx: None),
        _feature("b", 2, handoff_veto=lambda ctx: "b says no"),
        _feature("c", 3, handoff_veto=lambda ctx: "c says no"))
    assert rp_hooks.first("handoff_veto", None) == "b says no"
    use(_feature("a", handoff_veto=lambda ctx: None))
    assert rp_hooks.first("handoff_veto", None) is None


def test_merged_unions_dicts_and_a_shared_key_raises(use):
    use(_feature("a", 1, lint_kwargs=lambda ctx: {"ctx": 1}),
        _feature("b", 2, lint_kwargs=lambda ctx: None),
        _feature("c", 3, lint_kwargs=lambda ctx: {"strict": True}))
    assert rp_hooks.merged("lint_kwargs", None) == {"ctx": 1, "strict": True}
    use(_feature("a", lint_kwargs=lambda ctx: {"ctx": 1}),
        _feature("c", lint_kwargs=lambda ctx: {"ctx": 2}))
    with pytest.raises(rp_hooks.FeatureError, match="'ctx'.*'a'.*'c'"):
        rp_hooks.merged("lint_kwargs", None)


def test_concat_joins_lists_in_feature_order(use):
    use(_feature("b", 2, emc_args=lambda ctx: ["--b"]), _feature("n", 3, emc_args=lambda ctx: None),
        _feature("a", 1, emc_args=lambda ctx: ["--a", "1"]))
    assert rp_hooks.concat("emc_args", None) == ["--a", "1", "--b"]


def test_helpers_refuse_unknown_hooks_and_the_wrong_kind(use):
    use()
    with pytest.raises(rp_hooks.FeatureError, match="unknown pipeline hook 'on_lod'"):
        rp_hooks.notify("on_lod", None)
    with pytest.raises(rp_hooks.FeatureError, match="g1_verdict.*chain"):
        rp_hooks.notify("g1_verdict", None)
    with pytest.raises(rp_hooks.FeatureError, match="lint_kwargs.*merged"):
        rp_hooks.first("lint_kwargs", None)


@pytest.mark.parametrize("hook, result", [
    ("g2_policy", {"max_rounds": 0}),
    ("g2_policy", {"rounds": 1}),
    ("pack_scales", ()),
    ("pack_scales", (1.0, -2.0)),
    ("route_provider", "freerouting"),
    ("handoff_veto", ""),
    ("on_exhausted", "yes"),
    ("board_dims", (60, 0, "outline")),
    ("lint_kwargs", ["ctx"]),
    ("emc_args", "--grid"),
])
def test_a_result_breaking_its_hook_contract_names_the_feature(use, hook, result):
    use(_feature("sloppy", **{hook: lambda *a: result}))
    helper = getattr(rp_hooks, rp_hooks.HOOKS[hook].kind)
    with pytest.raises(rp_hooks.FeatureError, match=f"'sloppy'.*{hook}"):
        helper(hook, None)


@pytest.mark.parametrize("hook, value, result", [
    ("g4_verdict", {"passed": True, "blockers": [], "warnings": []},
     {"passed": True, "blockers": ["R8 broken"], "warnings": []}),
    ("g5_verdict", {"passed": True, "blockers": [], "warnings": []}, {"ok": True}),
    ("g4_inputs", ({}, {}), {"issues": []}),
], ids=["blocker-but-passed", "not-a-verdict", "not-a-pair"])
def test_a_chain_result_breaking_its_contract_names_the_feature(use, hook, value, result):
    use(_feature("sloppy", **{hook: lambda *a: result}))
    with pytest.raises(rp_hooks.FeatureError, match=f"'sloppy'.*{hook}"):
        rp_hooks.chain(hook, None, value)


def test_a_hook_error_propagates_unchanged(use):
    def boom(ctx):
        raise KeyError("x")
    use(_feature("a", on_load=boom))
    with pytest.raises(KeyError):
        rp_hooks.notify("on_load", None)


# --- the hook table ------------------------------------------------------------------------

def _doc_table() -> dict[str, str]:
    """{hook: kind} from the rows of the rp_hooks docstring table."""
    return dict(re.findall(r"^(\w+)\s+(notify|chain\(tuple\)|chain|first|merged|concat)\s",
                           rp_hooks.__doc__, re.M))


def test_the_docstring_table_lists_every_hook_with_its_kind():
    assert _doc_table() == {name: hook.kind for name, hook in rp_hooks.HOOKS.items()}
    assert list(_doc_table()) == list(rp_hooks.HOOKS)          # same (call) order


def test_every_stage_is_guarded_against_a_feature_halt():
    stages = (rp_stage_schematic.stage_load, *rp.STAGES)
    assert [s.__name__ for s in stages if not hasattr(s, "__wrapped__")] == []


def test_the_hook_layer_modules_stay_under_300_lines():
    sizes = {m.__name__: len(Path(m.__file__).read_text(encoding="utf-8").splitlines())
             for m in (rp_hooks, rp_route_hash)}
    assert {name: n for name, n in sizes.items() if n >= 300} == {}


def test_route_provider_and_handoff_veto_are_in_the_table():
    assert rp_hooks.HOOKS["route_provider"].kind == "first"
    assert rp_hooks.HOOKS["handoff_veto"].kind == "first"
    doc = rp_hooks.__doc__
    assert "k.autoroute_board" in doc and "forbids the handoff" in doc


# --- the hooks in a scripted run --------------------------------------------------------------

def test_a_recording_feature_sees_every_hook_of_a_pass_run(world, recorder):
    design = copy.deepcopy(DESIGN)
    design["emc"] = {"stitch": True, "max_freq_hz": 1e9}
    world.write_design(design)
    rep = world.run()
    rp_cli.cli_dims(rp_cli.parse_cli([str(world.netlist)]))
    assert rep["PASS"] is True
    first_seen = list(dict.fromkeys(hook for hook, _ in recorder.seen))
    assert first_seen == [h for h in rp_hooks.HOOKS if h not in NOT_IN_A_PASS_RUN]
    ctxs = {id(args[0]) for hook, args in recorder.seen
            if hook not in ("route_hash_extra", "board_dims")}
    assert len(ctxs) == 1                                  # one RunContext throughout
    for hook in ("route_hash_extra", "board_dims"):
        args = next(a for h, a in recorder.seen if h == hook)
        assert args[0]["title"] == DESIGN["title"] and args[1] == world.dir.resolve()


def test_gate_titles_match_the_gates_a_run_records(world):
    rep = world.run()
    assert {g["gate"]: (g["name"], g["feeds"]) for g in rep["gates"]} == rp_context.GATE_TITLES


def _normalised(text: str, d: Path) -> str:
    for form in (str(d), json.dumps(str(d))[1:-1], d.as_posix()):
        text = text.replace(form, "<DIR>")
    return text


def _add_c9(design: dict, tasks: list) -> tuple[dict, list]:
    fixed = copy.deepcopy(design)
    fixed["components"].append({"ref": "C9", "footprint": "Capacitor_SMD:C_0402"})
    return fixed, ["added C9"]


SCENARIOS = {
    "pass": lambda w, mp: None,
    "g1-fail": lambda w, mp: setattr(w, "lint", [{"ok": False, "errors": ["E3"], "warnings": []}]),
    "g2-remediation": lambda w, mp: (setattr(w, "analyses", [
        {"decoupling": {"uncovered": [{"ref": "R1", "rails_without_cap": ["+5V"]}]},
         "flags": {}, "errors": []}, OK_ANALYSIS]), mp.setattr(remediate, "apply", _add_c9)),
    "g2-designer-task": lambda w, mp: setattr(w, "schematics", [{"ok": True,
                                                                 "erc_violations": 3}]),
    "g3-escalation": lambda w, mp: setattr(w, "drcs", [
        {**CLEAN_DRC, "types": {"courtyards_overlap": 2}}, CLEAN_DRC]),
    "near-miss": lambda w, mp: (setattr(w, "routes", [{"tracks": 20, "unconnected": 2}]),
                                setattr(w, "drcs", [CLEAN_DRC, {**CLEAN_DRC, "unconnected": 2},
                                                    CLEAN_DRC]),
                                w.fixes.__setitem__("heal", [["bridged gap on N1"]])),
    "unrouted": lambda w, mp: setattr(w, "routes", [{"tracks": 15, "unconnected": 9}]),
    "no-fit-retry": lambda w, mp: (setattr(w, "routes", [{"tracks": 15, "unconnected": 9}]),
                                   setattr(w, "builds", [dict(OK_BUILD), {"ok": False}])),
    "router-failure": lambda w, mp: setattr(w, "routes", [{"ok": False, "error": "no java"}]),
    "g4-fix-loop": lambda w, mp: (setattr(w, "drcs", [CLEAN_DRC, {**CLEAN_DRC, "errors": 2},
                                                      CLEAN_DRC]),
                                  w.fixes.__setitem__("all", [["2 parallel vias"]])),
    "emc": lambda w, mp: w.write_design({**DESIGN, "emc": {"stitch": True, "teardrops": True}}),
    "ep-vias": lambda w, mp: (setattr(w, "thermal", [{"blockers": [], "warnings": [],
                                                      "ep_via_parts": ["U1"]}]),
                              w.fixes.__setitem__("ep_vias", [["U1: 9 thermal vias"]])),
}


def _scripted(tmp_path: Path, mp: pytest.MonkeyPatch, sub: str, scenario: str, runs: int,
              feature: Any | None) -> tuple[str, list]:
    d = tmp_path / sub
    d.mkdir()
    w = World(d, mp)
    if feature is not None:
        w.use_features(feature)
    SCENARIOS[scenario](w, mp)
    for _ in range(runs):
        w.calls.clear()
        try:
            w.run()
        except Exception as exc:                      # a crash is part of the behaviour
            w.calls.append(("raised", (type(exc).__name__,)))
    text = (d / "netlist_pipeline_report.json").read_text(encoding="utf-8")
    return _normalised(text, d), json.loads(_normalised(json.dumps(w.calls), d))


@pytest.mark.parametrize("runs", [1, 2], ids=["fresh", "cached"])
@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
def test_a_no_op_feature_leaves_the_run_byte_for_byte_unchanged(tmp_path, monkeypatch,
                                                                scenario, runs):
    noop = recording_feature("noop")
    plain = _scripted(tmp_path, monkeypatch, "plain", scenario, runs, None)
    hooked = _scripted(tmp_path, monkeypatch, "hooked", scenario, runs, noop)
    assert hooked[0] == plain[0]                          # the persisted report, byte for byte
    assert hooked[1] == plain[1]                          # the same external calls, in order
    assert noop.seen                                      # ... and the hooks did run


def test_route_provider_replaces_autoroute_with_identical_arguments(world, monkeypatch):
    monkeypatch.setenv("PIPELINE_NO_CACHE", "1")
    world.run()
    expected = (world.route_args[0], world.route_kw[0])
    got = []

    def provider(*args: Any, **kw: Any) -> dict:
        got.append((args, kw))
        return {"ok": True, "tracks": 3, "unconnected": 0, "method": "source copper"}

    world.use_features(_feature("carry", route_provider=lambda ctx: provider))
    world.calls.clear()
    rep = world.run()
    assert got == [expected]
    assert "route" not in world.names()
    assert rep["PASS"] is True and rep["autoroute"]["tracks"] == 3


def test_an_inactive_route_provider_keeps_freerouting(world):
    world.use_features(_feature("carry", route_provider=lambda ctx: None))
    world.run()
    assert world.args_of("route") == [(rp_route.AUTOROUTE_MAX_PASSES,
                                       rp_route.AUTOROUTE_TIMEOUT_S)]


def _halting(gate: str, message: str):
    def hook(*args: Any) -> None:
        raise rp_hooks.Halt(gate, message)
    return hook


def test_halt_from_after_g2_stops_at_g2_with_the_message(world):
    world.use_features(recording_feature("d7", after_g2=_halting("G2", "no physics artifact")))
    rep = world.run()
    assert rep["PASS"] is False
    assert [(g["gate"], g["passed"]) for g in rep["gates"]] == [("G1", True), ("G2", False)]
    assert rep["gates"][1]["blockers"][-1] == "no physics artifact"
    assert "[FAIL] G2" in rep["gate_summary"] and "BLOCKER: no physics artifact" in \
        rep["gate_summary"]
    assert "build" not in world.names() and "error" not in rep
    assert rep["feature_halt"] == {"gate": "G2", "feature": "d7", "hook": "after_g2",
                                   "message": "no physics artifact"}
    saved = json.loads((world.dir / "netlist_pipeline_report.json").read_text(encoding="utf-8"))
    assert saved["gates"] == rep["gates"] and saved["PASS"] is False


def test_halt_from_on_load_records_a_failed_g1_and_runs_nothing(world):
    world.use_features(_feature("libs", on_load=_halting("G1", "library table unreadable")))
    rep = world.run()
    assert [(g["gate"], g["passed"], g["blockers"]) for g in rep["gates"]] == [
        ("G1", False, ["library table unreadable"])]
    assert rep["gates"][0]["name"] == rp_context.GATE_TITLES["G1"][0]
    assert world.names() == []


def test_halt_for_a_gate_not_yet_recorded_appends_it(world):
    world.use_features(_feature("rf", after_route=_halting("G4", "RF fence failed")))
    rep = world.run()
    assert [(g["gate"], g["passed"]) for g in rep["gates"]] == [
        ("G1", True), ("G2", True), ("G3", True), ("G4", False)]
    g4 = rep["gates"][-1]
    assert (g4["name"], g4["feeds"]) == rp_context.GATE_TITLES["G4"]
    assert g4["blockers"] == ["RF fence failed"] and "fab" not in world.names()


def test_halt_refuses_an_unknown_gate():
    with pytest.raises(ValueError, match="G9"):
        rp_hooks.Halt("G9", "x")


def test_on_exhausted_true_reruns_once_from_after_g2(world):
    world.routes = [{"tracks": 15, "unconnected": 9}] * len(rp_stage_place.PACK_SCALES) + [
        {"tracks": 30, "unconnected": 0}]

    def escalate(ctx: rp_context.RunContext) -> bool:
        ctx.design = {**ctx.design, "copper_layers": 4}
        return True

    feature = recording_feature("d7", on_exhausted=escalate)
    world.use_features(feature)
    rep = world.run()
    hooks = [h for h, _ in feature.seen]
    assert hooks.count("on_exhausted") == 1 and hooks.count("after_g2") == 2
    assert len(world.args_of("build")) == len(rp_stage_place.PACK_SCALES) + 1
    assert rep["PASS"] is True
    manifest = json.loads((world.dir / "netlist.cache.json").read_text(encoding="utf-8"))
    assert manifest["route_hash"] == rp_route_hash.route_hash({**DESIGN, "copper_layers": 4})


def test_a_second_exhaustion_is_final(world):
    world.routes = [{"tracks": 15, "unconnected": 9}]
    feature = recording_feature("d7", on_exhausted=lambda ctx: True)
    world.use_features(feature)
    rep = world.run()
    assert [h for h, _ in feature.seen].count("on_exhausted") == 1
    assert len(world.args_of("build")) == 2 * len(rp_stage_place.PACK_SCALES)
    assert rep["PASS"] is False


def test_on_exhausted_is_not_asked_when_design_intent_stops_the_ladder(world, recorder):
    world.gate_checks["placement"] = [{"issues": ["J1 mating face points inward"],
                                       "warnings": []}]
    world.run()
    assert "on_exhausted" not in [h for h, _ in recorder.seen]


def test_g2_policy_one_round_without_remediation(world, monkeypatch):
    world.analyses = [{"decoupling": {"uncovered": [{"ref": "R1", "rails_without_cap": ["+5V"]}]},
                       "flags": {}, "errors": []}]
    monkeypatch.setattr(remediate, "apply", lambda *a: pytest.fail("remediation ran"))
    world.use_features(_feature("faithful", g2_policy=lambda ctx: {"max_rounds": 1,
                                                                   "remediate": False}))
    rep = world.run()
    assert world.names().count("analyze") == 1 and rep["PASS"] is False


def test_pack_scales_hook_sets_the_ladder_but_not_for_a_placement_override(world):
    world.drcs = [{**CLEAN_DRC, "types": {"courtyards_overlap": 1}}]
    world.use_features(_feature("faithful", pack_scales=lambda ctx, default: (1.0, 4.0)))
    world.run()
    assert [a[3] for a in world.args_of("build")] == [1.0, 4.0]
    world.calls.clear()
    world.run(placement="place.json")
    assert [a[3] for a in world.args_of("build")] == [1.0]


def _verdict_note(note: str):
    return lambda ctx, v: {**v, "warnings": [*v["warnings"], note]}


def test_g3_and_g4_hooks_run_at_both_call_sites(world):
    design = copy.deepcopy(DESIGN)
    design["emc"] = {"stitch": True}
    world.write_design(design)
    feature = recording_feature(
        "f", g3_inputs=lambda ctx, cy, pl: (cy, {**pl, "warnings": [*pl["warnings"], "g3in"]}),
        g3_verdict=_verdict_note("g3v"), g4_verdict=_verdict_note("g4v"),
        g4_inputs=lambda ctx, drc, rc: (drc, {**rc, "warnings": [*rc["warnings"], "g4in"]}))
    world.use_features(feature)
    first = world.run()
    first_hooks = [h for h, _ in feature.seen]
    second = world.run()                                  # the cached board: revalidated
    for rep in (first, second):
        g3 = next(g for g in rep["gates"] if g["gate"] == "G3")
        assert g3["warnings"][-2:] == ["g3in", "g3v"]
    assert second["route_cache"] == "hit"
    g4 = next(g for g in first["gates"] if g["gate"] == "G4")
    assert g4["warnings"][-1] == "g4v"
    assert g4["warnings"].count("g4in") == 1              # the re-judge never applies it twice
    assert first_hooks.count("g4_inputs") == 2 and first_hooks.count("g4_verdict") == 2
    assert first["drc"] == CLEAN_DRC                      # the report keeps the raw DRC


def test_a_verdict_hook_blocker_fails_the_gate(world):
    world.use_features(_feature("rf", g3_verdict=lambda ctx, v: {
        **v, "passed": False, "blockers": [*v["blockers"], "R9 preroute incomplete"]}))
    rep = world.run()
    assert rep["gates"][-1]["gate"] == "G3" and rep["gates"][-1]["passed"] is False
    assert "route" not in world.names()


def test_kwargs_and_argv_hooks_reach_their_calls(world):
    design = copy.deepcopy(DESIGN)
    design["emc"] = {"stitch": True}
    world.write_design(design)
    world.use_features(_feature("libs", lint_kwargs=lambda ctx: {"ctx": "LINTCTX"},
                                analysis_kwargs=lambda ctx: {"symbols": "RESOLVER"}),
                       _feature("d7", emc_args=lambda ctx: ["--stackup", "a.json"]))
    world.run()
    assert world.lint_kw == [{"ctx": "LINTCTX"}]
    assert world.analyze_kw == [{"symbols": "RESOLVER"}]
    assert world.emc_extra == [["--stackup", "a.json"]]


def test_without_features_the_calls_carry_no_extra_arguments(world):
    world.write_design({**DESIGN, "emc": {"stitch": True}})
    world.run()
    assert world.lint_kw == [{}] and world.analyze_kw == [{}] and world.emc_extra == [None]


def test_g1_prepare_may_replace_the_design_before_lint(world):
    def prepare(ctx: rp_context.RunContext) -> None:
        ctx.design = {**ctx.design, "title": "prepared"}
    seen = []
    world.use_features(_feature("d7", g1_prepare=prepare,
                                g1_verdict=lambda ctx, v: seen.append(ctx.design["title"])))
    world.run()
    assert seen == ["prepared"]


def test_run_context_gains_feature_state_and_the_netlist_dir(tmp_path):
    a = rp_context.RunContext(netlist=str(tmp_path / "n.json"), w=1, h=1)
    b = rp_context.RunContext(netlist=str(tmp_path / "n.json"), w=1, h=1)
    assert a.ext == {} and a.ext is not b.ext
    assert a.netlist_dir == tmp_path.resolve() and a.halted_by == {}
    assert a.report == {"netlist": str(tmp_path / "n.json"), "gates": []}


def test_world_run_pins_the_defaults_and_forwards_keywords(world, monkeypatch):
    got = []
    monkeypatch.setattr(rp, "run", lambda netlist, **kw: got.append((netlist, kw)) or {})
    world.run(placement="p.json")
    assert got == [(str(world.netlist), {**pipeline_world.RUN_DEFAULTS, "placement": "p.json"})]


# --- board_dims (rp_cli) --------------------------------------------------------------------

AUTO = {"components": [{"ref": "R1"}, {"ref": "R2"}]}


def test_board_dims_asks_the_hook_before_the_auto_size(use, tmp_path):
    asked = []
    use(_feature("outline", board_dims=lambda d, nd: asked.append(nd) or (60, 40.5,
                                                                          "netlist board outline")))
    outline = {**AUTO, "board": {"outline": {"dxf": "edge.dxf"}}}
    assert rp_cli.board_dims(outline, None, None, netlist_dir=tmp_path) == \
        (60.0, 40.5, "netlist board outline")
    assert asked == [tmp_path]
    assert rp_cli.board_dims(outline, 30.0, 20.0)[2] == "command line"
    assert rp_cli.board_dims({**AUTO, "board": {"w": 9, "h": 8}}, None, None)[2] == \
        "netlist board block"
    assert asked == [tmp_path]                               # explicit dims never ask


def test_board_dims_hook_halt_is_a_usage_error(use, tmp_path):
    use(_feature("outline", board_dims=_halting("G1", "outline is not closed")))
    netlist = tmp_path / "n.json"
    netlist.write_text(json.dumps(AUTO), encoding="utf-8")
    with pytest.raises(rp_cli.UsageError, match="outline is not closed"):
        rp_cli.cli_dims(rp_cli.parse_cli([str(netlist)]))


# --- route hash: registry merge and the extra component ------------------------------------------

GOLDEN_DESIGN = {
    "components": [
        {"ref": "U1", "footprint": "Package_SO:SOIC-8_3.9x4.9mm_P1.27mm", "x": 10.0, "y": 5.0,
         "rot": 90, "side": "top", "value": "LM358", "lcsc": "C7950"},
        {"ref": "C1", "footprint": "Capacitor_SMD:C_0603_1608Metric", "at": [3, 4, 0],
         "edge_row": True}],
    "nets": {"VCC": [["U1", "8"], ["C1", "1"]], "GND": [["U1", "4"], ["C1", "2"]]},
    "net_specs": {"VCC": {"current": 0.1}},
    "board": {"w": 30, "h": 20},
    "thermal": {"t_ambient": 40, "ep_vias_min": 5},
    "title": "golden",
    "emc": {"stitch": False},
}
# route_hash values computed by F5-01's rp_route_hash before the hook layer existed
F501_GOLDEN = {"{}": "090a3250709e15ba39f1a3610fed05583766e1ad1cb0f7866da22a041436d84c",
               "GOLDEN_DESIGN": "48417702ce642566c6a6acefc5f4434a15eaea20e7c2f20434f2293f7ad39736"}


def _f501_hash(design: dict) -> str:
    """F5-01's route_hash, verbatim, over the core registry tuples."""
    comps = sorted(({key: c.get(key) for key in rp_route_hash.ROUTE_HASH_COMPONENT_KEYS
                     if key in c} for c in design.get("components", [])),
                   key=lambda c: c["ref"])
    payload = {"components": comps, **{path: rp_route_hash.dig(design, path)
                                       for path in rp_route_hash.ROUTE_HASH_DESIGN_KEYS}}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def test_without_features_the_hash_is_f501s(use):
    use()
    assert rp_route_hash.route_hash({}) == F501_GOLDEN["{}"]
    assert rp_route_hash.route_hash(GOLDEN_DESIGN) == F501_GOLDEN["GOLDEN_DESIGN"]
    assert rp_route_hash.route_hash(GOLDEN_DESIGN, Path("anywhere")) == \
        F501_GOLDEN["GOLDEN_DESIGN"]
    assert rp_route_hash.registry() == rp_route_hash.Registry(
        rp_route_hash.ROUTE_HASH_COMPONENT_KEYS, rp_route_hash.ROUTE_HASH_DESIGN_KEYS,
        rp_route_hash.ROUTE_HASH_EXCLUDED)


@pytest.mark.parametrize("netlist", sorted(EXAMPLES.glob("*/netlist.json")),
                         ids=lambda p: p.parent.name)
def test_every_example_hashes_as_under_f501_without_features(use, netlist):
    use()
    design = json.loads(netlist.read_text(encoding="utf-8"))
    assert rp_route_hash.route_hash(design, netlist.parent) == _f501_hash(design)


def test_an_empty_extra_leaves_the_hash_and_a_non_empty_one_moves_it(use):
    use(_feature("a", route_hash_extra=lambda d, nd: {}), _feature("b"))
    assert rp_route_hash.route_hash(GOLDEN_DESIGN) == F501_GOLDEN["GOLDEN_DESIGN"]
    use(_feature("a", route_hash_extra=lambda d, nd: {"outline_fp": "abc"}))
    h1 = rp_route_hash.route_hash(GOLDEN_DESIGN)
    use(_feature("a", route_hash_extra=lambda d, nd: {"outline_fp": "abd"}))
    assert h1 != rp_route_hash.route_hash(GOLDEN_DESIGN) != F501_GOLDEN["GOLDEN_DESIGN"]


def test_the_extra_component_does_not_depend_on_feature_order(use):
    a = _feature("a", 1, route_hash_extra=lambda d, nd: {"z_digest": 1})
    b = _feature("b", 2, route_hash_extra=lambda d, nd: {"a_digest": 2})
    use(a, b)
    h = rp_route_hash.route_hash(GOLDEN_DESIGN)
    use(_feature("a", 3, route_hash_extra=a.route_hash_extra), b)
    assert rp_route_hash.route_hash(GOLDEN_DESIGN) == h


def test_route_hash_extra_receives_the_design_and_netlist_dir(use, tmp_path):
    seen = []
    use(_feature("a", route_hash_extra=lambda d, nd: seen.append((d, nd))))
    rp_route_hash.route_hash(GOLDEN_DESIGN, str(tmp_path))
    rp_route_hash.route_hash(GOLDEN_DESIGN)
    assert seen == [(GOLDEN_DESIGN, tmp_path), (GOLDEN_DESIGN, None)]


def test_feature_registry_keys_merge_and_move_the_hash(use):
    feature = _feature("d7", ROUTE_HASH_DESIGN_KEYS=("rf", "layer_policy"),
                       ROUTE_HASH_COMPONENT_KEYS=("edge",), ROUTE_HASH_EXCLUDED=("clock_hz",))
    use(feature, _feature("again", ROUTE_HASH_DESIGN_KEYS=("rf",)))
    reg = rp_route_hash.registry()
    assert reg.design_keys == (*rp_route_hash.ROUTE_HASH_DESIGN_KEYS, "rf", "layer_policy")
    assert reg.component_keys == (*rp_route_hash.ROUTE_HASH_COMPONENT_KEYS, "edge")
    assert reg.excluded == (*rp_route_hash.ROUTE_HASH_EXCLUDED, "clock_hz")
    base = rp_route_hash.route_hash(GOLDEN_DESIGN)
    moved = copy.deepcopy(GOLDEN_DESIGN)
    moved["components"][0]["edge"] = "left"
    assert rp_route_hash.route_hash(moved) != base
    assert rp_route_hash.route_hash({**GOLDEN_DESIGN, "rf": {"chains": 1}}) != base
    assert rp_route_hash.route_hash({**GOLDEN_DESIGN, "clock_hz": 8e6}) == base


@pytest.mark.parametrize("features, message", [
    ([_feature("x", ROUTE_HASH_EXCLUDED=("planes",))], "'planes'.*hashed.*excluded"),
    ([_feature("x", ROUTE_HASH_DESIGN_KEYS=("thermal",))], "'thermal'.*hashed.*excluded"),
    ([_feature("x", ROUTE_HASH_DESIGN_KEYS=("rebuild",)),
      _feature("y", ROUTE_HASH_EXCLUDED=("rebuild",))], "'rebuild'.*'x'.*'y'"),
    ([_feature("x", ROUTE_HASH_DESIGN_KEYS=("extra",))], "reserved"),
    ([_feature("x", ROUTE_HASH_DESIGN_KEYS=["rf"])], "'x'.*tuple"),
    ([_feature("x", ROUTE_HASH_COMPONENT_KEYS=("edge", 3))], "'x'.*tuple of str"),
], ids=["core-hashed", "core-excluded", "two-features", "reserved", "list", "non-str"])
def test_registry_conflicts_raise(use, features, message):
    use(*features)
    with pytest.raises(rp_hooks.FeatureError, match=message):
        rp_route_hash.registry()
    with pytest.raises(rp_hooks.FeatureError):
        rp_route_hash.route_hash({})
