"""The run context every harness stage shares, the gate recorder, the stage
guard that turns a feature's rp_hooks.Halt into a failed gate, and the single
exit (halt) that writes <stem>_pipeline_report.json AND <stem>_review.md on
every exit (ROADMAP D5).

Runtime decisions go to the append-only <stem>.decisions.jsonl beside the
netlist (decisions.py): RunContext.decide records one, log_remediation turns
every automatic corrective action into one, and halt records the run-level
ones (routing method, BOM sourcing accepted as warnings, safety flags) once
per run. A decision that cannot be written never stops a run: the reason
goes to report['decision_log_errors']. A review that cannot be rendered or
written never stops one either: report['review_error'], and the JSON report
is still written."""
from __future__ import annotations

import functools
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, NamedTuple, Sequence

import rp_paths  # noqa: F401  first: puts server/ and server/tools/ on sys.path
import decisions
import gates
import libtable
import review_report
import review_sources
import rp_hooks
import safety_screen
import toolchain as tc

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
OPEN_PREFIX = review_report.OPEN_PREFIX       # an unfixed board-fix finding, never a decision
REMEDY_RATIONALE = "standard remedy for an auto task (gates.py)"
# report['routing'] of a harness run (review_report renders method / requested / why)
ROUTING_REQUESTED = "auto"
ROUTING_WHY = "harness default: Freerouting on a canonical DSN (deterministic, no tokens)"
ROUTE_CACHE_WHY = ("the route hash matches a routed run (nothing that moves copper "
                   "changed), so its Freerouting board was reused; G3 and G4 re-judge it")
AGENT_ROUTING = "agent routing through the KiCad board API (the router subagent)"


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
    run_decided: bool = False                         # halt recorded the run-level decisions
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

    def decide(self, stage: str, question: str, decision: str, rationale: str, *,
               confidence: str = "high", source: str = "run_pipeline",
               alternatives: Sequence[str] = (), reversible: bool = True,
               override: str | None = None) -> dict | None:
        """Append one runtime decision to <stem>.decisions.jsonl; the entry
        written, or None when it could not be (an invalid entry or an
        unwritable log): the reason goes to report['decision_log_errors'] and
        the run goes on."""
        try:
            return decisions.append_decision(
                self.netlist, stage=stage, question=question, decision=decision,
                rationale=rationale, confidence=confidence, source=source,
                alternatives=alternatives, reversible=reversible, override=override)
        except (decisions.DecisionError, OSError) as e:
            self.report["decision_log_errors"] = [*self.report.get("decision_log_errors", []),
                                                  f"{type(e).__name__}: {e}"]
            return None

    def log_remediation(self, gate: str, rnd: int, applied: list, *,
                        record: bool = True) -> None:
        """Record one corrective action round; the report carries the same log.
        With `record`, every applied item (not an 'OPEN:' finding, which was
        not fixed) is also a decision of stage `gate` (a caller that records
        richer decisions itself passes record=False)."""
        self.remediation = [*self.remediation, {"gate": gate, "round": rnd, "applied": applied}]
        self.report["remediation"] = self.remediation
        if not record:
            return
        for item in applied:
            text = str(item)
            if text.strip() and not text.lstrip().startswith(OPEN_PREFIX):
                self.decide(gate, f"{gate} failed in round {rnd}: apply the automatic "
                                  f"corrective action?", text, REMEDY_RATIONALE,
                            source=f"run_pipeline {gate} auto-remediation")


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
    """The single exit of run() (and of main() for a usage error once the
    netlist path is known): final verdict + summary, the keys the review
    renders and the run-level decisions (record_run), then <stem>_review.md
    (write_review) and <stem>_pipeline_report.json, on every exit (pass, gate
    failure, or `error` for a crash). A review failure is report['review_error']
    and never masks the run's own error; a report write failure is flagged in
    the report, on stderr and in a re-written review."""
    report = ctx.report
    if error is not None:
        report["error"] = error
    report["PASS"] = (error is None and bool(report["gates"])
                      and all(g["passed"] for g in report["gates"]))
    report["gate_summary"] = gates.summarize(report["gates"])
    report["pipeline_report"] = str(ctx.report_path)
    _guarded(ctx, record_run)
    _guarded(ctx, write_review)
    write_report(ctx)
    if "pipeline_report_error" in report and "review" in report:
        _guarded(ctx, write_review)             # the review lists the unwritten report
    return report


def _guarded(ctx: RunContext, step: Callable[[RunContext], None]) -> None:
    """Run one review step; whatever it raises is recorded as
    report['review_error'] (never raised: the run's own outcome stands)."""
    try:
        step(ctx)
    except Exception as e:                        # recorded in the report, never swallowed
        problem = f"{type(e).__name__}: {e}"
        earlier = ctx.report.get("review_error")
        ctx.report["review_error"] = f"{earlier}; {problem}" if earlier else problem


def write_report(ctx: RunContext) -> None:
    """<stem>_pipeline_report.json; a write failure is flagged in the report
    (pipeline_report_error replaces pipeline_report) and on stderr."""
    report, path = ctx.report, ctx.report_path
    try:
        path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    except OSError as e:
        report.pop("pipeline_report", None)
        report["pipeline_report_error"] = f"could not write {path}: {e}"
        print(f"run_pipeline: could not write {path}: {e}", file=sys.stderr)


# --- the review keys and the run-level decisions ------------------------------------

def record_run(ctx: RunContext) -> None:
    """The report keys review_report renders: board_file for a board this
    run built or reused (a stale board of an earlier run that stopped before
    placement is not described), pre_k11_snapshot when one exists, routing
    when the run reached routing (a routing record a stage set is kept);
    then the run-level decisions, once per run, and decisions_log when the
    log exists."""
    report = ctx.report
    if Path(ctx.pcb).is_file() and (ctx.board_rep or "board" in report):
        report["board_file"] = {"path": ctx.pcb, **tc.board_file_info(ctx.pcb)}
    snapshot = tc.existing_snapshot(ctx.pcb)
    if snapshot:
        report["pre_k11_snapshot"] = snapshot
    routed_here = "routing" not in report and bool(ctx.route_rep or report.get("autoroute"))
    if routed_here:
        report["routing"] = routing_record(report)
    if not ctx.run_decided:
        ctx.run_decided = True
        decide_run(ctx, routed_here)
    log = decisions.decisions_path(ctx.netlist)
    if log.is_file():
        report["decisions_log"] = str(log)


def routing_record(report: dict) -> dict:
    """report['routing'] of a harness run: the route cache when it hit, else
    Freerouting (the harness router), with the reason."""
    if report.get("route_cache") == "hit":
        return {"method": "route cache", "requested": ROUTING_REQUESTED, "why": ROUTE_CACHE_WHY}
    return {"method": "freerouting", "requested": ROUTING_REQUESTED, "why": ROUTING_WHY}


def decide_run(ctx: RunContext, routed: bool) -> None:
    """The run-level decisions: the routing method (when halt recorded it),
    then, once G5 was judged, BOM sourcing accepted as warnings and the D5
    safety flags."""
    if routed:
        decide_routing(ctx)
    if any(g.get("gate") == "G5" for g in ctx.report["gates"]):
        decide_sourcing(ctx)
        decide_safety(ctx)


def decide_routing(ctx: RunContext) -> None:
    question = "How is the board routed?"
    if ctx.report["routing"]["method"] == "route cache":
        ctx.decide("routing", question, "reuse the routed board of an earlier run (route "
                   "cache hit)", ROUTE_CACHE_WHY, source="run_pipeline route cache",
                   alternatives=("re-route with Freerouting (set PIPELINE_NO_CACHE=1)",))
    else:
        ctx.decide("routing", question, "Freerouting on a canonical DSN (the harness router)",
                   ROUTING_WHY, source="run_pipeline routing", alternatives=(AGENT_ROUTING,))


def decide_sourcing(ctx: RunContext) -> None:
    """Parts without an LCSC number or MPN were accepted as G5 warnings
    (the netlist does not set require_sourcing): a medium-confidence call."""
    src = ctx.report.get("sourcing")
    if not isinstance(src, dict) or not src.get("missing") or src.get("required"):
        return
    missing = [str(ref) for ref in src["missing"]]
    ctx.decide("G5", f"{len(missing)} BOM part(s) have neither an LCSC number nor an MPN: "
                     f"release the fab package anyway?",
               f"released with BOM sourcing as G5 warnings; parts without a number: "
               f"{', '.join(missing)}",
               "the netlist does not set require_sourcing, so missing part numbers are G5 "
               "warnings (gates.g5_fab); a human adds them before ordering assembly",
               confidence="med", source="run_pipeline G5 sourcing policy",
               alternatives=('set "require_sourcing": true to make them G5 blockers',),
               override="require_sourcing")


def decide_safety(ctx: RunContext) -> None:
    """D5: a safety-relevant design (mains or > 50 V, Li-ion charging) is
    built hands-off but flagged HUMAN REVIEW REQUIRED."""
    design = ctx.design if isinstance(ctx.design, dict) else {}
    flags = safety_screen.detect_safety(design)
    if not flags:
        return
    kinds = ", ".join(dict.fromkeys(safety_screen.SAFETY_LABELS[f.kind] for f in flags))
    ctx.decide("G5", f"The design is safety-relevant ({kinds}): build it hands-off?",
               f"built hands-off and flagged {safety_screen.HUMAN_REVIEW} (top of the review, "
               f"G5 warning)",
               "ROADMAP D5: safety-relevant designs are built but flagged for review by a "
               "qualified person; " + "; ".join(safety_screen.safety_lines(flags)),
               source="run_pipeline D5 safety screen",
               alternatives=("stop before G5 until a qualified person has reviewed it",))


# --- the review -------------------------------------------------------------------------

def write_review(ctx: RunContext) -> None:
    """Render <stem>_review.md from the report, the decision log, the design,
    the G2 analysis and the parts' library sources, write it, and name it in
    report['review']."""
    log = decision_log(ctx.netlist)
    design = ctx.design if isinstance(ctx.design, dict) else {}
    markdown = review_report.render_review(
        ctx.report, list(log.entries), design, ctx.analysis or None,
        library_sources(ctx, design), decision_errors=log.errors)
    ctx.report["review"] = str(review_report.write_review(ctx.netlist, markdown))


def decision_log(netlist: str) -> decisions.DecisionLog:
    """The log leniently read; an unreadable log is one decision error (an
    open item of the review), never a failed review."""
    try:
        return decisions.load_decisions(netlist)
    except OSError as e:
        return decisions.DecisionLog((), (f"{decisions.decisions_path(netlist)}: cannot read "
                                          f"the decision log: {e}",))


def library_sources(ctx: RunContext, design: dict) -> dict | None:
    """{ref: {symbol, footprint}} sources for the parts table (stock dirs of
    the installs the pipeline uses); a malformed library table or an
    unreadable library is report['library_sources_error'] and None (the
    review says the sources were not supplied)."""
    try:
        return review_sources.library_sources(design, ctx.netlist_dir, tc.symbols_dir(),
                                               tc.footprints_dir())
    except (libtable.LibTableError, OSError) as e:
        ctx.report["library_sources_error"] = str(e)
        return None
