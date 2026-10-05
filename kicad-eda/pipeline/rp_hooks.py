"""Pipeline feature hooks: a feature acts inside the gated run through ONE
module pipeline/rp_feature_<name>.py (ORDER: int, default 100), never by
editing the stage modules. features() imports them once per process, sorted
by (ORDER, name); KICAD_EDA_PIPELINE_FEATURES = 'none' or a comma list of
names restricts them; set_features() overrides both (tests). A hook is a
function named exactly as in the table below; other module attributes are
ignored, except ORDER and the ROUTE_HASH_DESIGN_KEYS / _COMPONENT_KEYS /
_EXCLUDED tuples rp_route_hash.registry() merges. Feature state lives in
ctx.ext[<name>]. A hook raises Halt(gate, message) to stop the run (that gate
fails with the message as its blocker: rp_context.stage). A hook that cannot
judge a board (a resumed KiCad 11 board SWIG cannot load) returns a blocker
naming why, never crashes or skips. With no feature the run and its report
are exactly those of the harness without hooks.

Kinds, each called as a one-liner: notify(name, *args) calls every hook;
chain(name, ctx, value) threads value through hook(ctx, value), None keeping
it (chain(tuple): hook(ctx, a, b) -> (a, b)); first(name, *args) = the first
non-None result; merged(name, *args) = the union of the dicts (a key from two
features raises); concat(name, *args) = the concatenated lists. A result
breaking its contract raises FeatureError.

HOOK              KIND          CALL SITE: CONTRACT
on_load           notify        end of stage_load: (ctx); may fill ctx.ext[<feature>], report keys
g1_prepare        notify        start of stage_g1: (ctx); may replace ctx.design (a new dict) and
                                rewrite the netlist file atomically
lint_kwargs       merged        stage_g1: (ctx) -> extra keyword arguments for lint.lint
g1_verdict        chain         stage_g1 after gates.g1_requirements: (ctx, verdict) -> verdict
g2_policy         first         start of stage_g2: (ctx) -> {'max_rounds': int >= 1, 'remediate':
                                bool} (default: G2_ROUNDS rounds, remediation on)
analysis_kwargs   merged        g2_review: (ctx) -> extra keyword arguments for drpt.analyze
g2_verdict        chain         stage_g2 after its loop, before record_gate: (ctx, verdict)
after_g2          notify        stage_g2 once G2 passed, before stage_route_cache (again on an
                                on_exhausted re-run): (ctx); may raise Halt
route_hash_extra  merged        rp_route_hash.route_hash: (design, netlist_dir: Path | None) ->
                                JSON-able dict, hashed as the sorted 'extra' component
pack_scales       first         place_and_route, never with a placement override: (ctx, default)
                                -> tuple of positive pack scales
g3_inputs         chain(tuple)  placement_gate and revalidate_cached_board, before
                                gates.g3_placement: (ctx, courtyard, place) -> (courtyard, place)
g3_verdict        chain         after gates.g3_placement (both sites): (ctx, verdict) -> verdict
route_provider    first         rp_route.route_attempt: (ctx) -> a callable used INSTEAD of
                                k.autoroute_board (same arguments, same result shape)
after_route       notify        stage_place_route once G3 passed with 0 unconnected (a routed or
                                a cached board), before G4 is judged: (ctx)
on_exhausted      first         stage_place_route when the pack-scale ladder ran out: (ctx) ->
                                bool; True = re-run once from after_g2
handoff_veto      first         before any routing handoff to the agent (the route modes): (ctx)
                                -> str; a reason forbids the handoff (a G4 FAIL instead)
g4_inputs         chain(tuple)  judge_g4 and the EMC re-judge, before gates.g4_layout, on the raw
                                measurements: (ctx, drc_rep, routed) -> (drc_rep, routed)
g4_verdict        chain         after gates.g4_layout (both sites): (ctx, verdict) -> verdict
emc_args          concat        stage_emc: (ctx) -> list of extra argv for emc_stitch
g5_verdict        chain         stage_g5 after gates.g5_fab, before record_gate: (ctx, verdict)
board_dims        first         rp_cli.board_dims when neither the command line nor the board
                                block gives w/h: (design, netlist_dir) -> (w, h, label)
"""
from __future__ import annotations

import importlib
import math
import os
from pathlib import Path
from typing import Any, Callable, Iterable, NamedTuple

ENV_VAR = "KICAD_EDA_PIPELINE_FEATURES"
PREFIX = "rp_feature_"
DEFAULT_ORDER = 100
FEATURE_DIR = Path(__file__).resolve().parent
GATES = ("G1", "G2", "G3", "G4", "G5")


class FeatureError(RuntimeError):
    """A feature module that cannot load, a bad selection, or a hook breaking its contract."""


class Halt(Exception):
    """Raised by a hook to stop the run: `gate` is recorded as failed with
    `message` as its blocker (the helper fills in the feature and the hook)."""

    def __init__(self, gate: str, message: str) -> None:
        if gate not in GATES:
            raise ValueError(f"Halt needs one of {list(GATES)}, got {gate!r}")
        super().__init__(f"{gate}: {message}")
        self.gate, self.message, self.feature, self.hook = gate, str(message), "", ""


# --- result contracts (Hook.ok of a non-None result; Hook.expected names it) ----------

def _pos(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) and x > 0


def _verdict(v: Any) -> bool:
    return (isinstance(v, dict) and isinstance(v.get("passed"), bool)
            and isinstance(v.get("blockers"), list) and isinstance(v.get("warnings"), list)
            and not (v["blockers"] and v["passed"]))


def _policy(v: Any) -> bool:
    rounds = v.get("max_rounds", 1) if isinstance(v, dict) else None
    return (isinstance(v, dict) and set(v) <= {"max_rounds", "remediate"}
            and type(rounds) is int and rounds >= 1 and isinstance(v.get("remediate", True), bool))


class Hook(NamedTuple):
    kind: str
    ok: Callable[[Any], bool] | None = None
    expected: str = ""


V = (_verdict, "a gate verdict dict (passed, blockers, warnings; passed False with blockers)")
PAIR = (lambda v: isinstance(v, tuple) and len(v) == 2 and all(isinstance(x, dict) for x in v),
        "a (dict, dict) tuple")
KW = (lambda v: isinstance(v, dict) and all(isinstance(k, str) for k in v), "a str-keyed dict")
SEQ = (list, tuple)
HOOKS: dict[str, Hook] = {                     # the table above, in call order
    "on_load": Hook("notify"), "g1_prepare": Hook("notify"), "lint_kwargs": Hook("merged", *KW),
    "g1_verdict": Hook("chain", *V),
    "g2_policy": Hook("first", _policy, "{'max_rounds': int >= 1, 'remediate': bool}"),
    "analysis_kwargs": Hook("merged", *KW), "g2_verdict": Hook("chain", *V),
    "after_g2": Hook("notify"), "route_hash_extra": Hook("merged", *KW),
    "pack_scales": Hook("first", lambda v: isinstance(v, SEQ) and bool(v) and all(map(_pos, v)),
                        "a non-empty tuple of positive pack scales"),
    "g3_inputs": Hook("chain(tuple)", *PAIR), "g3_verdict": Hook("chain", *V),
    "route_provider": Hook("first", callable, "a callable"), "after_route": Hook("notify"),
    "on_exhausted": Hook("first", lambda v: isinstance(v, bool), "a bool"),
    "handoff_veto": Hook("first", lambda v: isinstance(v, str) and bool(v.strip()), "a reason"),
    "g4_inputs": Hook("chain(tuple)", *PAIR), "g4_verdict": Hook("chain", *V),
    "emc_args": Hook("concat", lambda v: isinstance(v, SEQ) and all(isinstance(x, str) for x in v),
                     "a list of str"),
    "g5_verdict": Hook("chain", *V),
    "board_dims": Hook("first", lambda v: (isinstance(v, SEQ) and len(v) == 3 and _pos(v[0])
                                           and _pos(v[1]) and isinstance(v[2], str)),
                       "(w_mm > 0, h_mm > 0, label)")}


# --- discovery -------------------------------------------------------------------------

_override: tuple[Any, ...] | None = None      # set_features(); None = discovery
_modules: dict[str, Any] = {}                 # name -> imported feature module


def _short(name: str) -> str:
    name = name.strip()
    return name[len(PREFIX):] if name.startswith(PREFIX) else name


def available() -> tuple[str, ...]:
    """The feature names in FEATURE_DIR (rp_feature_<name>.py), sorted."""
    return tuple(sorted(p.stem[len(PREFIX):] for p in FEATURE_DIR.glob(f"{PREFIX}*.py")))


def feature_name(feature: Any) -> str:
    name = getattr(feature, "NAME", None) or getattr(feature, "__name__", None)
    if not isinstance(name, str) or not name:
        raise FeatureError(f"pipeline feature {feature!r} has neither NAME nor __name__")
    return _short(name.rsplit(".", 1)[-1])


def _order(feature: Any) -> tuple[int, str]:
    order = getattr(feature, "ORDER", DEFAULT_ORDER)
    if type(order) is not int:
        raise FeatureError(f"pipeline feature '{feature_name(feature)}': ORDER must be an int, "
                           f"got {order!r}")
    return order, feature_name(feature)


def load(name: str) -> Any:
    """Import pipeline feature `name` ('x' or 'rp_feature_x') once per process."""
    short = _short(name)
    if short not in _modules:
        if short not in available():
            raise FeatureError(f"no pipeline feature '{short}' in {FEATURE_DIR} "
                               f"(available: {', '.join(available()) or 'none'})")
        importlib.invalidate_caches()
        try:
            module = importlib.import_module(PREFIX + short)
        except Exception as e:
            raise FeatureError(f"pipeline feature module {PREFIX}{short} failed to import: "
                               f"{type(e).__name__}: {e}") from e
        _order(module)
        _modules[short] = module
    return _modules[short]


def _sorted(chosen: Iterable[Any]) -> tuple[Any, ...]:
    out = tuple(sorted(chosen, key=_order))
    names = [feature_name(f) for f in out]
    twins = sorted({n for n in names if names.count(n) > 1})
    if twins:
        raise FeatureError(f"pipeline features selected twice: {twins}")
    return out


def _env_selection() -> tuple[str, ...]:
    raw, names = os.environ.get(ENV_VAR, "").strip(), available()
    if not raw:
        return names
    if raw.lower() == "none":
        return ()
    wanted = tuple(dict.fromkeys(_short(n) for n in raw.split(",") if n.strip()))
    unknown = [n for n in wanted if n not in names]
    if unknown:
        raise FeatureError(f"{ENV_VAR} names unknown pipeline feature(s) {unknown}; "
                           f"available: {list(names)}")
    return wanted


def set_features(chosen: Iterable[Any] | None) -> None:
    """Tests: use exactly `chosen` (feature names and/or module objects) instead
    of discovery; None restores discovery (and the env filter)."""
    global _override
    if isinstance(chosen, str):
        raise FeatureError("set_features takes a list of feature names or modules, not a str")
    _override = None if chosen is None else _sorted(
        load(c) if isinstance(c, str) else c for c in chosen)


def selection() -> tuple[Any, ...] | None:
    """The set_features() selection (None: discovery)."""
    return _override


def features() -> tuple[Any, ...]:
    """The active feature modules in (ORDER, name) order."""
    if _override is not None:
        return _override
    return _sorted(load(name) for name in _env_selection())


# --- the helpers -----------------------------------------------------------------------

def _impls(name: str, *kinds: str) -> list[tuple[str, Callable[..., Any]]]:
    hook = HOOKS.get(name)
    if hook is None:
        raise FeatureError(f"unknown pipeline hook '{name}' (see the rp_hooks table)")
    if hook.kind not in kinds:
        raise FeatureError(f"hook '{name}' is a {hook.kind} hook, not {'/'.join(kinds)}")
    found = [(feature_name(f), getattr(f, name)) for f in features()
             if getattr(f, name, None) is not None]
    bad = [feature for feature, fn in found if not callable(fn)]
    if bad:
        raise FeatureError(f"pipeline feature '{bad[0]}': {name} is not a function")
    return found


def _call(feature: str, name: str, fn: Callable[..., Any], *args: Any) -> Any:
    try:
        result = fn(*args)
    except Halt as h:
        h.feature, h.hook = h.feature or feature, h.hook or name
        raise
    hook = HOOKS[name]
    if result is not None and hook.ok is not None and not hook.ok(result):
        raise FeatureError(f"pipeline feature '{feature}': hook {name} returned "
                           f"{result!r:.200}; expected {hook.expected}")
    return result


def notify(name: str, *args: Any) -> None:
    for feature, fn in _impls(name, "notify"):
        _call(feature, name, fn, *args)


def chain(name: str, ctx: Any, value: Any) -> Any:
    hooks = _impls(name, "chain", "chain(tuple)")
    splat = HOOKS[name].kind == "chain(tuple)"
    for feature, fn in hooks:
        out = _call(feature, name, fn, ctx, *(value if splat else (value,)))
        value = value if out is None else out
    return value


def first(name: str, *args: Any) -> Any:
    for feature, fn in _impls(name, "first"):
        out = _call(feature, name, fn, *args)
        if out is not None:
            return out
    return None


def merged(name: str, *args: Any) -> dict[str, Any]:
    out, owner = {}, {}                          # key -> value, key -> feature
    for feature, fn in _impls(name, "merged"):
        part = _call(feature, name, fn, *args) or {}
        clash = sorted(set(part) & set(out))
        if clash:
            raise FeatureError(f"hook {name}: key '{clash[0]}' produced by both "
                               f"'{owner[clash[0]]}' and '{feature}'")
        out, owner = {**out, **part}, {**owner, **{key: feature for key in part}}
    return out


def concat(name: str, *args: Any) -> list[Any]:
    out: list[Any] = []
    for feature, fn in _impls(name, "concat"):
        out = [*out, *(_call(feature, name, fn, *args) or [])]
    return out
