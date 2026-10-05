"""Scripted stand-ins for every external call run_pipeline.run makes.

Every KiCad-facing call the harness makes (lint, schematic build, design
review, board build, Freerouting, DRC, the SWIG gate/fix/stitch scripts, fab
export, renders) is replaced by a scripted stand-in, patched on the module
object the stages call through (rp_kicad.drc, k.autoroute_board, ...), so a
test drives the real stage logic -- gate sequencing, the G2 / G3 / G4
corrective-action loops, the route cache, the persisted report -- with no
KiCad installed. The board "file" the fakes write is a header-only stub whose
generator_version decides what toolchain.swig_readable says about it.

A test module gets the `world` fixture with an ASSIGNMENT (ruff then sees no
unused import shadowed by the test argument)::

    from fixtures import pipeline_world
    world = pipeline_world.world

Pipeline feature modules (pipeline/rp_feature_*.py, rp_hooks) are OFF in a
World: a test enables the ones it means with world.use_features(...) (names or
module objects), and `recorder` installs an in-memory feature that implements
every hook as a recording no-op. Drive a run with world.run(**kw): RUN_DEFAULTS
is the one place later harness options are pinned for every stage test.
"""
from __future__ import annotations

import copy
import json
import types
from pathlib import Path
from typing import Any, Callable

import pytest

import design_report as drpt
import kicad_env as env
import kicad_server as k
import lint_netlist as lint
import rp_hooks
import rp_kicad
import run_pipeline as rp
import thermal_check

# run() keyword defaults every World.run pins (a new harness option goes here so
# the existing stage tests keep proving today's behaviour)
RUN_DEFAULTS: dict[str, Any] = {"w": 30, "h": 30, "placement": ""}

K9_HEADER = '(kicad_pcb (version 20241229) (generator "pcbnew") (generator_version "9.0")\n'
K11_HEADER = '(kicad_pcb (version 20260901) (generator "pcbnew") (generator_version "10.99")\n'

CLEAN_DRC = {"errors": 0, "unconnected": 0, "types": {}, "kicad_cli": "fake-cli"}
OK_ANALYSIS = {"decoupling": {"uncovered": []}, "flags": {}, "errors": []}
# the keys swig_build_board prints on success (missing_pads / mounting_holes are lists)
OK_BUILD = {"ok": True, "footprints": 2, "pads_assigned": 4, "zones": ["In1.Cu:GND"],
            "antenna_keepout": None, "mounting_holes": [], "missing_pads": []}

DESIGN = {
    "title": "two resistors",
    "components": [{"ref": "R1", "lib_id": "Device:R", "value": "1k",
                    "footprint": "Resistor_SMD:R_0603_1608Metric"},
                   {"ref": "R2", "lib_id": "Device:R", "value": "1k",
                    "footprint": "Resistor_SMD:R_0603_1608Metric"}],
    "nets": {"N1": [["R1", "1"], ["R2", "1"]], "N2": [["R1", "2"], ["R2", "2"]]},
}


def _stable(base: Path) -> env.KiCadInstall:
    root = base / "kicad9"
    return env.KiCadInstall(
        version="9.0.4", version_tuple=(9, 0, 4), root=root,
        kicad_cli=root / "bin" / "kicad-cli.exe", python=root / "bin" / "python.exe",
        share=root / "share" / "kicad", has_api_server=False)


def _next(seq: list) -> Any:
    """Scripted results: each call consumes one, the last one repeats."""
    return seq.pop(0) if len(seq) > 1 else seq[0]


class World:
    """Scripted stand-ins for every external call run() makes. Each field is a
    list of results consumed one per call (the last repeats); `calls` records
    (name, args) in call order; `route_kw` records the keyword arguments of
    every autoroute call (later harness modes pass dsn_seed / from_snapshot),
    `route_args` its positional arguments; `lint_kw` / `analyze_kw` / `emc_extra`
    what the feature hooks lint_kwargs / analysis_kwargs / emc_args added."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.dir = tmp_path
        self.netlist = tmp_path / "netlist.json"
        self.pcb = tmp_path / "netlist.kicad_pcb"
        self.write_design(copy.deepcopy(DESIGN))
        self.calls: list[tuple[str, tuple]] = []
        self.route_kw: list[dict[str, Any]] = []
        self.route_args: list[tuple] = []
        self.lint_kw: list[dict[str, Any]] = []
        self.analyze_kw: list[dict[str, Any]] = []
        self.emc_extra: list[list[str] | None] = []
        self.lint = [{"ok": True, "errors": [], "warnings": []}]
        self.schematics = [{"ok": True, "backend": "file", "components": 2, "nets": 2,
                            "erc_ok": True, "erc_violations": 0}]
        self.analyses = [OK_ANALYSIS]
        self.thermal = [{"blockers": [], "warnings": []}]
        self.builds = [dict(OK_BUILD)]
        self.routes: list[Any] = [{"ok": True, "tracks": 7, "unconnected": 0}]
        self.drcs = [CLEAN_DRC]
        self.gate_checks: dict[str, list] = {}
        self.fixes: dict[str, list] = {}
        self.emc = [{"ok": True, "vias_added": 12, "fence": 8, "interior": 4,
                     "pitch_mm": 3.0}]
        self.fab = [{"ok": True, "zip": str(tmp_path / "netlist_JLCPCB.zip"),
                     "files": [f"f{i}" for i in range(12)], "bom": "BOM.csv", "cpl": "CPL.csv"}]
        self.header = K9_HEADER
        self._patch(monkeypatch, tmp_path)

    def write_design(self, design: dict) -> None:
        self.netlist.write_text(json.dumps(design, indent=2), encoding="utf-8")

    def run(self, **kw: Any) -> dict:
        """run_pipeline.run on this world's netlist with RUN_DEFAULTS, then `kw`."""
        return rp.run(str(self.netlist), **{**RUN_DEFAULTS, **kw})

    def use_features(self, *features: Any) -> None:
        """Enable exactly these pipeline features (names or module objects) for
        this test; monkeypatch puts discovery back at teardown."""
        rp_hooks.set_features(list(features))

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]

    def args_of(self, name: str) -> list[tuple]:
        return [args for n, args in self.calls if n == name]

    def _rec(self, name: str, *args: Any) -> None:
        self.calls.append((name, args))

    def _patch(self, mp: pytest.MonkeyPatch, tmp_path: Path) -> None:
        # no feature module takes part unless a test enables it (use_features);
        # the snapshot makes monkeypatch restore discovery at teardown
        mp.setattr(rp_hooks, "_override", rp_hooks.selection())
        rp_hooks.set_features([])
        stable = _stable(tmp_path)
        mp.setattr(env, "discover", lambda refresh=False: [stable])
        mp.delenv("PIPELINE_NO_CACHE", raising=False)
        mp.setattr(k, "_kicad_share", lambda: None)
        mp.setattr(lint, "lint", self.fake_lint)
        mp.setattr(k, "build_schematic_from_netlist", self.fake_schematic)
        mp.setattr(drpt, "analyze", self.fake_analyze)
        mp.setattr(thermal_check, "check", self.fake_thermal)
        mp.setattr(k, "build_board_from_netlist", self.fake_build)
        mp.setattr(k, "autoroute_board", self.fake_route)
        mp.setattr(k, "export_fab_package", self.fake_fab)
        mp.setattr(rp_kicad, "drc", self.fake_drc)
        mp.setattr(rp_kicad, "gate_check", self.fake_gate_check)
        mp.setattr(rp_kicad, "board_fix", self.fake_board_fix)
        mp.setattr(rp_kicad, "render", self.fake_render)
        mp.setattr(rp_kicad, "emc_stitch", self.fake_emc)

    # --- the stand-ins ---------------------------------------------------------
    def fake_lint(self, design: dict, syms: str, **kw: Any) -> dict:
        self._rec("lint", syms)
        self.lint_kw.append(dict(kw))
        return _next(self.lint)

    def fake_schematic(self, netlist: str, sch: str) -> dict:
        self._rec("schematic", netlist, sch)
        return _next(self.schematics)

    def fake_analyze(self, design: dict, syms: str, **kw: Any) -> dict:
        self._rec("analyze", len(design["components"]))
        self.analyze_kw.append(dict(kw))
        return _next(self.analyses)

    def fake_thermal(self, design: dict) -> dict:
        self._rec("thermal")
        return _next(self.thermal)

    def fake_build(self, netlist: str, w: float, h: float, pcb: str, fps: str,
                   placement: str, scale: float) -> dict:
        self._rec("build", w, h, placement, scale)
        res = dict(_next(self.builds))
        if res.get("ok"):
            Path(pcb).write_text(f"{self.header}(built {scale})\n", encoding="utf-8")
        return res

    def fake_route(self, pcb: str, passes: int, jar: str, timeout: int, **kw: Any) -> dict:
        self._rec("route", passes, timeout)
        self.route_kw.append(dict(kw))
        self.route_args.append((pcb, passes, jar, timeout))
        res = _next(self.routes)
        if isinstance(res, Exception):
            raise res
        with open(pcb, "a", encoding="utf-8") as f:
            f.write("(routed)\n")
        return dict(res)

    def fake_drc(self, pcb: str) -> dict:
        self._rec("drc")
        return dict(_next(self.drcs))

    def fake_gate_check(self, mode: str, pcb: str, netlist: str) -> dict:
        self._rec("gate_check", mode)
        return copy.deepcopy(_next(self.gate_checks.get(mode, [{"issues": [], "warnings": []}])))

    def fake_board_fix(self, pcb: str, netlist: str, mode: str) -> list:
        self._rec("board_fix", mode)
        return list(_next(self.fixes.get(mode, [[]])))

    def fake_render(self, pcb: str, tag: str, quality: str = "basic") -> list:
        self._rec("render", tag)
        return [f"{tag}_top.png"]

    def fake_emc(self, pcb: str, emc: dict, extra: list[str] | None = None) -> dict:
        self._rec("emc", emc.get("max_freq_hz"))
        self.emc_extra.append(extra)
        return dict(_next(self.emc))

    def fake_fab(self, pcb: str, netlist: str = "") -> dict:
        self._rec("fab")
        return dict(_next(self.fab))


# --- an in-memory pipeline feature ------------------------------------------------

def _recorder(seen: list, hook: str, impl: Callable[..., Any] | None) -> Callable[..., Any]:
    def call(*args: Any) -> Any:
        seen.append((hook, args))
        return impl(*args) if impl is not None else None
    call.__name__ = hook
    return call


def recording_feature(name: str = "recorder", order: int = rp_hooks.DEFAULT_ORDER,
                      **impls: Callable[..., Any]) -> types.ModuleType:
    """A feature module object implementing EVERY hook of rp_hooks.HOOKS: each
    call is appended to `.seen` as (hook, args) and returns impls[hook](*args)
    when given, else None -- with no impls it is a no-op that must leave a run
    exactly as it is without features."""
    unknown = sorted(set(impls) - set(rp_hooks.HOOKS))
    if unknown:
        raise ValueError(f"not pipeline hooks: {unknown}")
    feature = types.ModuleType(rp_hooks.PREFIX + name)
    feature.ORDER = order
    feature.seen = []
    for hook in rp_hooks.HOOKS:
        setattr(feature, hook, _recorder(feature.seen, hook, impls.get(hook)))
    return feature


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    return World(tmp_path, monkeypatch)


@pytest.fixture
def recorder(world: World) -> types.ModuleType:
    """The world with one in-memory recording no-op feature installed."""
    feature = recording_feature()
    world.use_features(feature)
    return feature
