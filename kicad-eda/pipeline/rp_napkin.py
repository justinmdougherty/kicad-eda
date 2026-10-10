"""Napkin mode in the harness (ROADMAP D5): the mode, the open-editor hard
stop, and the policy remedies gates.py turns into auto tasks, applied by
remediate.py with one decision per remedy.

  resolve_mode     stage_load: napkin when run() asked for it (the default;
                   --strict turns it off) or the netlist says "mode": "napkin"
                   (that always wins); report['mode'] = {name, why} and, in
                   napkin mode, one G1 decision saying why (decide_mode)
  hard_stop        stage_load: a schematic / board open in a KiCad editor is
                   never overwritten. Napkin: the outputs move to
                   <stem>.napkin.kicad_sch / .kicad_pcb (sibling_outputs; their
                   .kicad_pro and the route-cache manifest follow the board
                   stem, the report, review and decision log stay at the
                   netlist stem) with a G1 decision; strict, or a sibling open
                   too: rp_hooks.Halt at G1 with the refusal reason
  apply_policies   G2 / G3: remediate.apply_structured on the gate's tasks; the
                   netlist is rewritten and every Remedy is a decision of its
                   own (confidence, rationale, alternatives, override); a task
                   an applier declined, or a round that changed nothing, is an
                   'OPEN:' item of the remediation log (once per gate)
  g3_policy_round  rp_stage_place: the face-out / thermal-swap tasks of the last
                   G3 verdict, with the connector probe (only when a connector
                   task exists) and the footprint lookup (the D1 resolution
                   order); True = the netlist changed, rebuild at the same scale

Only napkin runs reach decide_mode, sibling_outputs, apply_policies and
g3_policy_round: in strict mode the harness is what it was before napkin mode
(every gate judged with napkin=False)."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Callable, NoReturn, Sequence

import rp_paths  # first: puts server/ and server/tools/ on sys.path
import kicad_ipc as ipc
import kicad_server as k
import libtable
import remediate
import rp_context
import rp_hooks
import toolchain as tc

NAPKIN_TAG = ".napkin"            # <stem>.napkin.kicad_sch / .kicad_pcb: the hard-stop siblings
G3_POLICY_ROUNDS = 2              # face-out / thermal-swap rebuilds per pack_scale
POLICY_FIXES = ("face_connector_out", "swap_thermal_footprint")   # G3 fixes no scale can clear
PROBE_SCRIPT = rp_paths.SERVER / "tools" / "swig_connector_probe.py"
PROBE_TIMEOUT_S = 300
OUTPUT_TAIL = 300                 # chars of a probe's output kept when it printed no result
OPEN = rp_context.OPEN_PREFIX + " "

MODE_QUESTION = "Which mode does the pipeline harness run this design in?"
NAPKIN_DECISION = ("napkin (hands-off): the D5 policies turn floating IC pins, connectors that "
                   "face the board interior, fractured planes and short EP via farms into "
                   "automatic remedies, each recorded as a decision")
WHY_NETLIST = ('the netlist sets "mode": "napkin", which always means napkin (ROADMAP D5), '
               'whatever the command line asks')
WHY_DEFAULT = ("the pipeline harness defaults to napkin behaviour (ROADMAP D5) and --strict "
               "was not given")
WHY_STRICT = "--strict: the napkin policies are off (every policy task stays a designer task)"
STRICT_ALTERNATIVE = "strict: run_pipeline.py --strict (every policy task stays a designer task)"
NETLIST_ALTERNATIVE = 'strict: remove "mode": "napkin" from the netlist and pass --strict'

HARD_STOP_SOURCE = "D5 hard stop"
HARD_STOP_RULE = ("ROADMAP D5: overwriting a file open in a KiCad editor stays a hard stop in "
                  "napkin mode; write a sibling and record it")
HARD_STOP_ALTERNATIVES = ("save and close the file in KiCad, then re-run to rebuild the "
                          "canonical files", "run with --strict to stop instead")

FootprintExists = Callable[[str], bool]


# --- the mode --------------------------------------------------------------------------

def resolve_mode(ctx: rp_context.RunContext) -> None:
    """Settle ctx.napkin for the run (see the module doc) and say why."""
    design = ctx.design if isinstance(ctx.design, dict) else {}
    from_netlist = design.get("mode") == "napkin"
    why = WHY_NETLIST if from_netlist else WHY_DEFAULT if ctx.napkin else WHY_STRICT
    ctx.napkin = ctx.napkin or from_netlist
    ctx.report["mode"] = {"name": "napkin" if ctx.napkin else "strict", "why": why}
    if ctx.napkin:
        decide_mode(ctx, from_netlist, why)


def decide_mode(ctx: rp_context.RunContext, from_netlist: bool, why: str) -> None:
    ctx.decide("G1", MODE_QUESTION, NAPKIN_DECISION, why, source="run_pipeline mode",
               alternatives=(NETLIST_ALTERNATIVE if from_netlist else STRICT_ALTERNATIVE,),
               override="mode" if from_netlist else None)


# --- the hard stop -----------------------------------------------------------------------

def sibling_paths(base: str) -> tuple[str, str]:
    """(<stem>.napkin.kicad_sch, <stem>.napkin.kicad_pcb) for the netlist stem."""
    return base + NAPKIN_TAG + ".kicad_sch", base + NAPKIN_TAG + ".kicad_pcb"


def editor_refusals(paths: Sequence[str]) -> list[str]:
    """kicad_ipc's refusal reason for every path open in a KiCad editor, over
    one GUI connection (no GUI answering refuses nothing, as write_guard)."""
    gui = ipc.connect()
    reasons = []
    for path in paths:
        kind = ipc.holding_kind(gui, path)
        if kind:
            reasons.append(ipc.write_refusal(path, kind)["reason"])
    return reasons


def hard_stop(ctx: rp_context.RunContext) -> None:
    """D5: never overwrite ctx.sch / ctx.pcb while a KiCad editor has one open."""
    refused = editor_refusals((ctx.sch, ctx.pcb))
    if not refused:
        return
    if not ctx.napkin:
        halt_at_g1(ctx, refused)
    also = editor_refusals(sibling_paths(ctx.base))
    if also:
        halt_at_g1(ctx, [*refused, *also])
    sibling_outputs(ctx, refused)


def halt_at_g1(ctx: rp_context.RunContext, refused: list[str]) -> NoReturn:
    ctx.report["hard_stop"] = {"action": "halt", "refused": refused}
    halt = rp_hooks.Halt("G1", f"hard stop (ROADMAP D5): {'; '.join(refused)}")
    halt.feature, halt.hook = "harness", "hard_stop"
    raise halt


def sibling_outputs(ctx: rp_context.RunContext, refused: list[str]) -> None:
    """Napkin: the schematic, the board and everything that follows the board
    stem go to the <stem>.napkin.* siblings; one G1 decision records it."""
    sch, pcb = sibling_paths(ctx.base)
    ctx.sch, ctx.pcb = sch, pcb
    ctx.cache_path = Path(str(Path(pcb).with_suffix("")) + rp_context.CACHE_SUFFIX)
    ctx.report["hard_stop"] = {"action": "sibling", "refused": refused, "schematic": sch,
                               "board": pcb}
    ctx.decide("G1", "The schematic or board is open in a KiCad editor: where does this run "
                     "write them?",
               f"to the napkin siblings {Path(sch).name} and {Path(pcb).name}; the open file "
               f"is left untouched", f"{HARD_STOP_RULE}: {'; '.join(refused)}",
               source=HARD_STOP_SOURCE, alternatives=HARD_STOP_ALTERNATIVES)


# --- the policy remedies (G2, G3) ----------------------------------------------------------

def apply_policies(ctx: rp_context.RunContext, gate: str, rnd: int, tasks: list, *,
                   footprint_exists: FootprintExists | None = None,
                   connector_probe: dict | None = None) -> bool:
    """Apply `tasks` (see the module doc); True when the netlist changed."""
    fixed, remedies, skipped = remediate.apply_structured(
        ctx.design, tasks, gate=gate, footprint_exists=footprint_exists,
        connector_probe=connector_probe)
    opens = [f"{OPEN}{s.desc}: not applied ({s.reason})" for s in skipped]
    if remedies and fixed == ctx.design:
        declined = {s.desc for s in skipped}
        descs = "; ".join(t.get("desc", "") for t in tasks if t.get("desc") not in declined)
        opens.append(f"{OPEN}{descs}: the remedy changed nothing in the netlist "
                     f"({'; '.join(r.text for r in remedies)})")
        remedies = []
    texts = [r.text for r in remedies] + new_items(ctx, gate, opens)
    if texts:
        ctx.log_remediation(gate, rnd, texts, record=False)
    if not remedies:
        return False
    ctx.design = fixed
    Path(ctx.netlist).write_text(json.dumps(fixed, indent=2), encoding="utf-8")
    for remedy in remedies:
        decide_remedy(ctx, rnd, remedy)
    return True


def new_items(ctx: rp_context.RunContext, gate: str, items: list[str]) -> list[str]:
    """The items not yet in this gate's remediation log (an open task comes
    back every round; it is listed once)."""
    seen = {item for entry in ctx.remediation if entry["gate"] == gate
            for item in entry["applied"]}
    return [item for item in dict.fromkeys(items) if item not in seen]


def decide_remedy(ctx: rp_context.RunContext, rnd: int, remedy: remediate.Remedy) -> None:
    policy = remedy.policy or "auto-remediation"
    ctx.decide(remedy.gate, f"{remedy.gate} failed in round {rnd}: apply the napkin policy "
                            f"{policy}?",
               remedy.text, remedy.rationale, confidence=remedy.confidence,
               source=f"run_pipeline {remedy.gate} napkin policy {policy}",
               alternatives=remedy.alternatives, override=remedy.override)


def policy_tasks(verdict: dict) -> list[dict]:
    """The auto tasks of a G3 verdict a napkin rebuild applies (POLICY_FIXES)."""
    return [t for t in verdict.get("tasks") or []
            if t.get("auto") and (t.get("fix") or {}).get("type") in POLICY_FIXES]


def g3_policy_round(ctx: rp_context.RunContext, attempt: int) -> bool:
    """One G3 policy round on ctx.g3 (see the module doc)."""
    tasks = policy_tasks(ctx.g3)
    if not tasks:
        return False
    kinds = {t["fix"]["type"] for t in tasks}
    probe = (connector_probe(ctx.pcb, ctx.netlist) if "face_connector_out" in kinds
             else None)
    exists = footprint_check(ctx, attempt) if "swap_thermal_footprint" in kinds else None
    return apply_policies(ctx, "G3", attempt, tasks, footprint_exists=exists,
                          connector_probe=probe)


# --- the connector probe and the footprint lookup ------------------------------------------

def connector_probe(pcb: str, netlist: str) -> dict:
    """server/tools/swig_connector_probe.py on the placed board under KiCad's
    Python (stdin closed, PROBE_TIMEOUT_S): its JSON result, or {ok: False,
    reason} when it cannot answer (remediate turns that into an open task)."""
    kpy = k._kicad_python()
    if not kpy or not PROBE_SCRIPT.is_file():
        return {"ok": False, "reason": f"connector probe unavailable: KiCad's Python or "
                                       f"{PROBE_SCRIPT.name} not found"}
    try:
        r = tc.run([kpy, str(PROBE_SCRIPT), pcb, netlist], stdin=subprocess.DEVNULL,
                   timeout=PROBE_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError) as e:
        return {"ok": False, "reason": f"connector probe did not finish: {type(e).__name__}: {e}"}
    found = tc.last_json(r.stdout or "")
    if found is None:
        tail = ((r.stdout or "") + (r.stderr or "")).strip()[-OUTPUT_TAIL:]
        return {"ok": False, "reason": f"connector probe produced no result "
                                       f"(exit {r.returncode}): {tail}"}
    return found


def footprint_lookup(netlist_dir: Path, design: dict) -> FootprintExists:
    """'Lib:Name' -> True when the library resolves (netlist block > project
    table > stock, ROADMAP D1) and holds <Name>.kicad_mod."""
    resolver = libtable.LibraryResolver.for_design(netlist_dir, design, tc.symbols_dir(),
                                                   tc.footprints_dir())

    def exists(lib_ref: str) -> bool:
        lib, _, name = str(lib_ref).partition(":")
        found = resolver.resolve(libtable.FOOTPRINT, lib) if lib and name else None
        return found is not None and (found.path / f"{name}.kicad_mod").is_file()
    return exists


def footprint_check(ctx: rp_context.RunContext, attempt: int) -> FootprintExists | None:
    """footprint_lookup for the run's netlist; a library table that cannot be
    read is an open item and None (remediate then leaves the swap open)."""
    try:
        return footprint_lookup(ctx.netlist_dir, ctx.design)
    except (libtable.LibTableError, OSError) as e:
        items = new_items(ctx, "G3", [f"{OPEN}the footprint lookup failed ({e}): a ThermalVias "
                                      f"swap cannot be checked"])
        if items:
            ctx.log_remediation("G3", attempt, items, record=False)
        return None
