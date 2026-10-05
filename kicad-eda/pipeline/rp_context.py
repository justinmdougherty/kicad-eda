"""The run context every harness stage shares, the gate recorder, the stage
guard that turns a feature's rp_hooks.Halt into a failed gate, and the single
exit (halt) that writes <stem>_pipeline_report.json on every exit."""
from __future__ import annotations

import functools
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, NamedTuple

import rp_paths  # noqa: F401  first: puts server/ and server/tools/ on sys.path
import gates
import rp_hooks

REPORT_SUFFIX = "_pipeline_report.json"
CACHE_SUFFIX = ".cache.json"
ATTEMPT_SUFFIX = ".attempt"   # <pcb>.attempt: the last ROUTED board of the scale loop
AGENT_SUFFIX = ".agent"       # <stem>.agent.kicad_pcb/.kicad_pro: a preserved KiCad 11 board
SCHEMATIC_REP_KEYS = ("ok", "backend", "components", "nets", "erc_ok", "erc_violations")
# (name, feeds) of the gates.py verdicts, for a Halt naming a gate not yet recorded
GATE_TITLES: dict[str, tuple[str, str]] = {
    "G1": ("Requirements / netlist review", "SRR"),
    "G2": ("Schematic review (capture complete, pre-layout)", "PDR"),
    "G3": ("Placement review (placed, pre-routing)", "pre-route hard gate"),
    "G4": ("Layout / board review (routed, pre-release)", "CDR"),
    "G5": ("Fabrication package (handoff)", "release to fab")}
BOARD_REP_KEYS = ("ok", "footprints", "pads_assigned", "zones", "antenna_keepout",
                  "mounting_holes", "missing_pads", "error", "reason", "stderr")


@dataclass
class RunContext:
    """What the stages share. Inputs, derived paths and mode flags are fixed
    when the run starts; the stages fill in the rest. `design` is the netlist
    as the stages see it (a G2 remediation replaces it and rewrites the file);
    `report` is the JSON report halt() finalises and persists; `analysis` is
    the last design_report.analyze() result (the report keeps a summary).
    `ext` holds pipeline feature state keyed by feature name (rp_hooks; never
    reported by itself); `halted_by` is set when a feature hook stopped the run."""
    netlist: str
    w: float
    h: float
    placement: str = ""
    no_cache: bool = field(default_factory=lambda: bool(os.environ.get("PIPELINE_NO_CACHE")))
    design: dict = field(default_factory=dict)
    syms: str = ""
    report: dict = field(default_factory=dict)
    remediation: list[dict] = field(default_factory=list)
    trep: dict = field(default_factory=dict)          # G2 thermal review (G3 reads ep_via_parts)
    analysis: dict = field(default_factory=dict)      # G2 design_report.analyze() result
    rhash: str = ""
    cached: dict = field(default_factory=dict)        # the last successful run's cache manifest
    use_cache: bool = False
    board_rep: dict = field(default_factory=dict)
    route_rep: dict = field(default_factory=dict)
    drc_rep: dict = field(default_factory=dict)
    routed_check: dict = field(default_factory=lambda: {"issues": [], "warnings": []})
    g3: dict = field(default_factory=dict)
    g4: dict = field(default_factory=dict)
    ext: dict[str, Any] = field(default_factory=dict)   # feature state, keyed by feature name
    halted_by: dict = field(default_factory=dict)       # {gate, feature, hook, message}
    base: str = field(init=False)                     # the netlist path without .json
    netlist_dir: Path = field(init=False)             # absolute (feature hooks resolve from it)
    sch: str = field(init=False)
    pcb: str = field(init=False)
    cache_path: Path = field(init=False)
    report_path: Path = field(init=False)

    def __post_init__(self) -> None:
        self.base = str(Path(self.netlist).with_suffix(""))
        self.netlist_dir = Path(self.netlist).resolve().parent
        self.sch, self.pcb = self.base + ".kicad_sch", self.base + ".kicad_pcb"
        self.cache_path = Path(self.base + CACHE_SUFFIX)
        self.report_path = Path(self.base + REPORT_SUFFIX)
        if not self.report:
            self.report = {"netlist": self.netlist, "gates": []}

    def log_remediation(self, gate: str, rnd: int, applied: list) -> None:
        """Record one corrective action round; the report carries the same log."""
        self.remediation.append({"gate": gate, "round": rnd, "applied": applied})
        self.report["remediation"] = self.remediation


class Attempt(NamedTuple):
    """The scale loop's last ROUTED attempt (restored when a retry no longer fits)."""
    scale: float
    g3: dict
    board_rep: dict
    route_rep: dict


def record_gate(ctx: RunContext, verdict: dict) -> bool:
    ctx.report["gates"].append(verdict)
    return verdict["passed"]


def record_halt(ctx: RunContext, halt: rp_hooks.Halt) -> None:
    """A feature hook stopped the run: its gate's verdict (the last one
    recorded, else a new one) fails with the message as a blocker, and
    report['feature_halt'] names the feature and the hook."""
    gate_list = ctx.report["gates"]
    at = next((i for i in range(len(gate_list) - 1, -1, -1)
               if gate_list[i].get("gate") == halt.gate), None)
    name, feeds = GATE_TITLES[halt.gate]
    base = gate_list[at] if at is not None else {
        "gate": halt.gate, "name": name, "feeds": feeds, "blockers": [], "warnings": [],
        "tasks": []}
    failed = {**base, "passed": False, "blockers": [*base.get("blockers", []), halt.message]}
    ctx.report["gates"] = ([*gate_list[:at], failed, *gate_list[at + 1:]] if at is not None
                           else [*gate_list, failed])
    ctx.halted_by = {"gate": halt.gate, "feature": halt.feature, "hook": halt.hook,
                     "message": halt.message}
    ctx.report["feature_halt"] = ctx.halted_by


def stage(fn: Callable[[RunContext], Any]) -> Callable[[RunContext], Any]:
    """Decorator of every harness stage: an rp_hooks.Halt raised inside it is
    recorded (record_halt) and the stage returns False, so run() stops through
    halt(); once a run is halted every later stage returns False at once."""
    @functools.wraps(fn)
    def guarded(ctx: RunContext) -> Any:
        if ctx.halted_by:
            return False
        try:
            return fn(ctx)
        except rp_hooks.Halt as h:
            record_halt(ctx, h)
            return False
    return guarded


def halt(ctx: RunContext, error: str | None = None) -> dict:
    """The single exit of run(): final verdict + summary, written to
    <stem>_pipeline_report.json on every exit (pass, gate failure, or `error`
    for a crash). A write failure is flagged in the report and on stderr."""
    report, path = ctx.report, ctx.report_path
    if error is not None:
        report["error"] = error
    report["PASS"] = (error is None and bool(report["gates"])
                      and all(g["passed"] for g in report["gates"]))
    report["gate_summary"] = gates.summarize(report["gates"])
    report["pipeline_report"] = str(path)
    try:
        path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    except OSError as e:
        del report["pipeline_report"]
        report["pipeline_report_error"] = f"could not write {path}: {e}"
        print(f"run_pipeline: could not write {path}: {e}", file=sys.stderr)
    return report
