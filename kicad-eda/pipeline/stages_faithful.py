"""Faithful-rebuild pipeline I/O (ROADMAP_DECISIONS D4): the stage side of
server/tools/faithful.py.

A design with a ``source`` block is a rebuild of an existing board. These
functions read what the stages wrote (the reference pad nets, the
generated schematic, ``<sch stem>_erc.json``, ``<pcb stem>_drc.json``, the
board) and apply faithful.py's pure rules. They are hook-shaped for
pipeline/rp_feature_faithful.py (F46-20): every function takes the
RunContext duck-typed (``design``, ``netlist``, ``sch``, ``pcb``,
``report``, ``board_rep``) and keeps its state on ``ctx.faithful``.

  load(ctx)                        on_load: ctx.faithful = FaithfulRun(profile,
                                   reference read once, error); report['faithful']
  active(ctx) -> bool              the design is a rebuild (g2_policy)
  pack_scales(ctx, default)        faithful.pack_scales
  g2_judge(ctx, verdict)           g2_verdict: relax_g2 with the ERC baseline,
                                   then equivalence of the design nets and of the
                                   GENERATED schematic (kicad-cli export)
  g3_inputs(ctx, courtyard, place) g3_inputs: our courtyard count (split_drc),
                                   relax_g3, board equivalence
  g4_inputs(ctx, drc_rep, routed)  g4_inputs: DRC errors/types = ours (unconnected
                                   untouched), placement/intrinsic findings as
                                   warnings, routed-board equivalence

A design without a source makes every function an identity that touches no
file. A source that cannot be used (malformed block, unreadable reference)
never crashes a stage: each gate gets a blocker naming why. Equivalence is a
pad-partition comparison against ``source.reference`` (the netlist itself
when unset): G2 ignores board-only parts and the variant's dropped refs, G3
and G4 only the dropped refs. Results land in report['faithful'] = {active,
kind, rebuild, reference, import_report, notes, equivalence: {G2: {design,
schematic}, G3, G4}, erc: {total, new, baseline_total, baseline}, courtyard:
{reported, counted}, drc: DrcSplit.as_dict() + report, relaxed: ['G2: ...'],
error?} (import_review renders it). The place / routed dicts this returns
carry a ``_faithful`` mark so a second pass over the same inputs (the EMC
re-judge) gives the same result instead of duplicating findings.
"""
from __future__ import annotations

import json
import tempfile
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NamedTuple

import rp_paths  # noqa: F401  first: puts server/ and server/tools/ on sys.path
import design_paths
import faithful as fa
import net_equivalence as ne
import toolchain as tc

STATE_ATTR = "faithful"                 # ctx attribute holding the FaithfulRun
RECORD = "faithful"                     # report key
MARK = "_faithful"                      # place / routed key: {"base": [...], "out": [...]}
ERC_SUFFIX = "_erc.json"                # kicad_server.build_schematic_from_netlist
DRC_SUFFIX = "_drc.json"                # rp_kicad.drc
IMPORT_REPORT = "import_report.json"    # kicad_import.REPORT_FILE
CANNOT_JUDGE = "the faithful rebuild cannot be judged against its source"
NOT_COMPARED = "connectivity could not be compared with the source"
NO_CLI = "kicad-cli not found (install KiCad or set KICAD_PATH)"
FIX_TASK = "make the rebuild match its source"
COMPARE_ERRORS = (OSError, ValueError, TypeError, RuntimeError)
PadNetsMeta = tuple[Mapping[str, Any], Mapping[str, Any]]


@dataclass(frozen=True)
class FaithfulRun:
    """What load() keeps on ctx: the faithful profile, the source reference
    read once ((pad nets, meta); None = compare against the netlist itself)
    and why the source cannot be used (None when it can)."""
    profile: fa.FaithfulProfile = fa.INACTIVE
    reference: PadNetsMeta | None = None
    error: str | None = None

    @property
    def active(self) -> bool:
        return self.profile.active or self.error is not None


INACTIVE_RUN = FaithfulRun()


class ErcJudgement(NamedTuple):
    new: list[str] | None           # None: not judged against a baseline
    total: int | None
    record: dict[str, Any]
    warnings: list[str]


# ----------------------------------------------------------------------------
# state, load, active, pack_scales
# ----------------------------------------------------------------------------
def _has_source(ctx: Any) -> bool:
    design = getattr(ctx, "design", None)
    return isinstance(design, Mapping) and design.get("source") is not None


def state(ctx: Any) -> FaithfulRun:
    """The run's FaithfulRun; a source design that was not loaded yet is
    loaded now (a stage order change must not skip the faithful rules)."""
    run = getattr(ctx, STATE_ATTR, None)
    if isinstance(run, FaithfulRun):
        return run
    if not _has_source(ctx):
        return INACTIVE_RUN
    load(ctx)
    return getattr(ctx, STATE_ATTR)


def _make_run(design: Mapping[str, Any], netlist: str) -> FaithfulRun:
    try:
        profile = fa.profile(design, netlist)
    except ValueError as e:
        return FaithfulRun(error=f"the design's source / rebuild / variant is malformed: {e}")
    if profile.reference is None:
        return FaithfulRun(profile)
    try:
        return FaithfulRun(profile, ne.load_pad_nets_meta(profile.reference))
    except COMPARE_ERRORS as e:
        return FaithfulRun(profile, error=f"source.reference {profile.reference} cannot be "
                                          f"read: {e}")


def _import_report(source: Any, netlist: str) -> str | None:
    """source.import_report by the D1 path rule, else <netlist dir>/import_report.json
    when that exists."""
    base = Path(netlist).parent
    raw = source.get("import_report") if isinstance(source, Mapping) else None
    if isinstance(raw, str) and raw.strip():
        path = design_paths.resolve_path(raw, base)
        return None if path is None else str(path)
    default = base / IMPORT_REPORT
    return str(default) if default.is_file() else None


def load(ctx: Any) -> None:
    """ctx.faithful = the run's FaithfulRun (the reference read once) and a
    fresh report['faithful'] record. No-op beyond the state for a design
    without a source."""
    if not _has_source(ctx):
        setattr(ctx, STATE_ATTR, INACTIVE_RUN)
        return
    design, netlist = ctx.design, str(ctx.netlist)
    run = _make_run(design, netlist)
    setattr(ctx, STATE_ATTR, run)
    source = design["source"]
    kind = run.profile.kind if run.profile.active else (
        source.get("kind") if isinstance(source, Mapping) else None)
    record = {"active": True, "kind": kind, "rebuild": run.profile.rebuild,
              "reference": None if run.profile.reference is None else str(run.profile.reference),
              "import_report": _import_report(source, netlist),
              "notes": list(run.profile.notes), "equivalence": {}, "relaxed": []}
    ctx.report[RECORD] = {**record, **({"error": run.error} if run.error else {})}


def active(ctx: Any) -> bool:
    """True when the design is a faithful rebuild (it has a source block)."""
    return state(ctx).active


def pack_scales(ctx: Any, default: tuple[float, ...]) -> tuple[float, ...]:
    """(1.0,) for a keep rebuild with every part placed by the source, else
    ``default`` itself (faithful.pack_scales)."""
    return fa.pack_scales(state(ctx).profile, default)


# ----------------------------------------------------------------------------
# small helpers
# ----------------------------------------------------------------------------
def _key(item: Any) -> str:
    return item if isinstance(item, str) else json.dumps(item, sort_keys=True, default=str)


def _unmatched(items: Sequence[Any], against: Sequence[Any]) -> tuple[list[Any], int]:
    """(items not matched one for one by ``against``, how many of ``against``
    found no match): a multiset difference that keeps the order of ``items``."""
    left = Counter(_key(x) for x in against)
    out = []
    for item in items:
        k = _key(item)
        if left[k]:
            left[k] -= 1
        else:
            out.append(item)
    return out, sum(left.values())


def _extra(current: Sequence[Any], ours: Sequence[Any]) -> list[Any] | None:
    """Items of ``current`` beyond ``ours``, or None when some of ``ours`` is
    no longer in ``current``."""
    extra, missing = _unmatched(current, ours)
    return None if missing else extra


def _added(before: Sequence[Any], after: Sequence[Any]) -> list[Any]:
    """Items of ``after`` that ``before`` does not have."""
    return _unmatched(after, before)[0]


def _base_lists(d: Mapping[str, Any]) -> tuple[list, list]:
    """(issues, warnings) as they were before this module touched ``d``:
    the marked base plus anything added since; the lists themselves when
    there is no mark or something of ours was removed."""
    issues, warnings = list(d.get("issues") or []), list(d.get("warnings") or [])
    mark = d.get(MARK)
    try:
        (base_i, base_w), (out_i, out_w) = mark["base"], mark["out"]
    except (TypeError, KeyError, ValueError):
        return issues, warnings
    extra_i, extra_w = _extra(issues, out_i), _extra(warnings, out_w)
    if extra_i is None or extra_w is None:
        return issues, warnings
    return list(base_i) + extra_i, list(base_w) + extra_w


def _with_lists(d: Mapping[str, Any], base: tuple[list, list], issues: list,
                warnings: list) -> dict[str, Any]:
    return {**d, "issues": issues, "warnings": warnings,
            MARK: {"base": [list(base[0]), list(base[1])], "out": [list(issues), list(warnings)]}}


def _record(ctx: Any) -> dict[str, Any]:
    old = ctx.report.get(RECORD)
    return dict(old) if isinstance(old, Mapping) else {}


def _record_stage(ctx: Any, stage: str, equivalence: Any, rules: Iterable[Any],
                  **extra: Any) -> None:
    """report['faithful'] with this stage's equivalence and relaxed rules
    replaced (a re-judged stage never appends twice)."""
    old = _record(ctx)
    prefix = f"{stage}: "
    kept = [r for r in old.get("relaxed", []) if not (isinstance(r, str)
                                                     and r.startswith(prefix))]
    eq = old.get("equivalence")
    ctx.report[RECORD] = {**old, **extra,
                          "equivalence": {**(eq if isinstance(eq, Mapping) else {}),
                                          stage: equivalence},
                          "relaxed": kept + [f"{prefix}{r}" for r in rules]}


def _error_finding(stage: str, text: str) -> fa.Finding:
    return fa.Finding([f"{stage}: {text}"], [], {"stage": stage, "equal": None, "summary": text})


def _skip_finding(stage: str, text: str, *, warn: bool) -> fa.Finding:
    return fa.Finding([], [f"{stage}: {text}"] if warn else [],
                      {"stage": stage, "equal": None, "skipped": text})


def _reference(ctx: Any, run: FaithfulRun) -> PadNetsMeta:
    """The source pad nets: the reference read at load, else the netlist."""
    if run.reference is not None:
        return run.reference
    return ne.netlist_pad_nets(ctx.design), {}


def _compare(stage: str, reference: Callable[[], Any], candidate: Callable[[], Any],
             ignore: Iterable[str]) -> fa.Finding:
    """faithful.equivalence_findings; a side that cannot be read is a blocker."""
    try:
        return fa.equivalence_findings(stage, reference(), candidate(), ignore_refs=ignore)
    except COMPARE_ERRORS as e:
        return _error_finding(stage, f"{NOT_COMPARED}: {e}")


def _board_finding(ctx: Any, run: FaithfulRun, stage: str) -> fa.Finding:
    """The board's pad nets (board-only parts included) against the source."""
    if run.error:
        return _error_finding(stage, f"{CANNOT_JUDGE}: {run.error}")
    return _compare(stage, lambda: _reference(ctx, run),
                    lambda: ne.load_pad_nets_meta(ctx.pcb), run.profile.ignore_refs)


def _sibling(path: str, suffix: str) -> Path:
    return Path(str(Path(path).with_suffix("")) + suffix)


def _split(ctx: Any, run: FaithfulRun) -> tuple[fa.DrcSplit | None, Path, str | None]:
    """faithful.split_drc of <pcb stem>_drc.json: (split, path, None) or
    (None, path, why) when the report is missing or unreadable."""
    path = _sibling(ctx.pcb, DRC_SUFFIX)
    if not path.is_file():
        return None, path, f"no DRC report {path.name}"
    try:
        return fa.split_drc(path, run.profile), path, None
    except (OSError, ValueError, TypeError) as e:
        return None, path, f"the DRC report {path.name} cannot be read ({e})"


# ----------------------------------------------------------------------------
# G2
# ----------------------------------------------------------------------------
def _schematic_record(ctx: Any) -> Mapping[str, Any]:
    rep = ctx.report.get("schematic")
    return rep if isinstance(rep, Mapping) else {}


def _erc_total(ctx: Any) -> int | None:
    n = _schematic_record(ctx).get("erc_violations")
    return n if isinstance(n, int) and not isinstance(n, bool) else None


def _erc_judgement(ctx: Any, run: FaithfulRun) -> ErcJudgement:
    """The rebuilt ERC (<sch stem>_erc.json, errors only) against
    source.erc_baseline: not judged without a baseline or when ERC did not
    run; an unreadable report is a warning and leaves the ERC blocker."""
    total, baseline = _erc_total(ctx), run.profile.erc_baseline
    record = {"total": total, "new": None, "baseline_total": None,
              "baseline": None if baseline is None else str(baseline)}
    if baseline is None or total is None:
        return ErcJudgement(None, total, record, [])
    try:
        keys = fa.erc_keys(baseline)
        new = [] if total == 0 else fa.erc_new(_sibling(ctx.sch, ERC_SUFFIX), keys)
    except (OSError, ValueError, TypeError) as e:
        return ErcJudgement(None, total, {**record, "error": str(e)},
                            [f"faithful: ERC was not judged against the source baseline: {e}"])
    return ErcJudgement(new, total, {**record, "new": new,
                                     "baseline_total": sum(keys.values())}, [])


def _export_nets(sch: Path, cli: str) -> dict[str, set]:
    """Pad nets of the generated schematic, exported where it lies (the
    project's library tables beside it: the 10.99 exporter drops symbol
    pins without a pad only when the footprint library resolves)."""
    with tempfile.TemporaryDirectory(prefix="kicad-eda-faithful-") as tmp:
        xml = ne.export_schematic_xml(sch, Path(tmp) / f"{sch.stem}.xml", cli)
        return ne.xml_pad_nets(xml.read_text(encoding="utf-8"))


def _schematic_finding(ctx: Any, reference: Callable[[], Any],
                       ignore: frozenset[str]) -> fa.Finding:
    stage = "G2 schematic"
    if _schematic_record(ctx).get("ok") is False:
        return _skip_finding(stage, "not checked: the schematic was not generated", warn=False)
    cli = tc.kicad_cli()
    if not cli:
        return _skip_finding(stage, "the generated schematic's connectivity was not checked "
                                    f"against the source: {NO_CLI}", warn=True)
    sch = Path(ctx.sch)
    if not sch.is_file():
        return _error_finding(stage, f"the generated schematic {sch} does not exist")
    return _compare(stage, reference, lambda: _export_nets(sch, cli), ignore)


def _g2_findings(ctx: Any, run: FaithfulRun) -> list[tuple[str, fa.Finding]]:
    """(name, finding) for the design nets and the generated schematic;
    board-only parts and the variant's dropped refs are not compared."""
    if run.error:
        return [("source", _error_finding("G2", f"{CANNOT_JUDGE}: {run.error}"))]
    ref_board_only = run.reference[1].get("board_only_refs", []) if run.reference else []
    ignore = run.profile.board_only | run.profile.ignore_refs | frozenset(ref_board_only)

    def reference() -> PadNetsMeta:
        return _reference(ctx, run)

    design = _compare("G2 design", reference, lambda: ne.netlist_pad_nets(ctx.design), ignore)
    return [("design", design), ("schematic", _schematic_finding(ctx, reference, ignore))]


def _extend_verdict(verdict: Mapping[str, Any], blockers: list[str],
                    warnings: list[str]) -> dict[str, Any]:
    """A new verdict with blockers (each with a designer task) and warnings."""
    tasks = [{"desc": f"{FIX_TASK}: {b}", "auto": False, "fix": {}, "policy": None}
             for b in blockers]
    all_blockers = list(verdict.get("blockers", [])) + blockers
    return {**verdict, "blockers": all_blockers,
            "warnings": list(verdict.get("warnings", [])) + warnings,
            "tasks": list(verdict.get("tasks", [])) + tasks, "passed": not all_blockers}


def g2_judge(ctx: Any, verdict: Mapping[str, Any]) -> Mapping[str, Any]:
    """The G2 verdict of a rebuild: faithful.relax_g2 (decoupling kept as
    designed, ERC against source.erc_baseline), then the equivalence of the
    design nets and of the generated schematic with the source. Records
    report['faithful'] equivalence G2, erc and relaxed. Inactive: ``verdict``."""
    run = state(ctx)
    if not run.active:
        return verdict
    erc = _erc_judgement(ctx, run)
    relaxed = fa.relax_g2(verdict, new_erc=erc.new, erc_total=erc.total, profile=run.profile)
    rules = [w for w in _added(verdict.get("warnings", []), relaxed["warnings"])
             if w != fa.NO_ERC_BASELINE]
    findings = _g2_findings(ctx, run)
    out = _extend_verdict(relaxed, [b for _, f in findings for b in f.blockers],
                          erc.warnings + [w for _, f in findings for w in f.warnings])
    _record_stage(ctx, "G2", {name: f.summary for name, f in findings}, rules, erc=erc.record)
    return out


# ----------------------------------------------------------------------------
# G3
# ----------------------------------------------------------------------------
def _board_failed(ctx: Any) -> bool:
    rep = getattr(ctx, "board_rep", None)
    return isinstance(rep, Mapping) and bool(rep) and not rep.get("ok")


def _codes(place: Mapping[str, Any], issues: list) -> list | None:
    """The structured issue codes when they parallel the issues."""
    codes = place.get("codes")
    return list(codes) if isinstance(codes, list) and len(codes) == len(issues) else None


def _courtyard(ctx: Any, run: FaithfulRun, courtyard: Mapping[str, Any]
               ) -> tuple[Mapping[str, Any], list[str], list[str], dict[str, Any]]:
    """(courtyard, warnings, relaxed rules, record): only our courtyard
    violations count (faithful.courtyard_view of the board DRC split)."""
    split, _path, why = _split(ctx, run)
    if split is None:
        return (courtyard, [f"faithful: {why}; the courtyard count is not split"], [],
                {"split": False, "reason": why})
    view = fa.courtyard_view(split)
    reported = courtyard.get("courtyard_violations", 0)
    dropped = reported - view["courtyard_violations"] if isinstance(reported, int) else 0
    rules = ([f"{dropped} courtyard violation(s) from the source placement or inside one "
              f"footprint are not counted"] if dropped > 0 else [])
    return ({**courtyard, **view}, [], rules,
            {"reported": reported, "counted": view["courtyard_violations"]})


def g3_inputs(ctx: Any, courtyard: Mapping[str, Any], place: Mapping[str, Any]
              ) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    """(courtyard, place) for gates.g3_placement on a rebuild: our courtyard
    count only, J*-at-edge issues as warnings (faithful.relax_g3, structured
    ``codes`` win), and the board's connectivity against the source
    appended (a difference is an issue). A failed board build and an
    inactive design: the inputs themselves."""
    run = state(ctx)
    if not run.active:
        return courtyard, place
    if _board_failed(ctx):
        _record_stage(ctx, "G3", _skip_finding("G3", "not checked: the board was not built",
                                               warn=False).summary, [])
        return courtyard, place
    base = _base_lists(place)
    cy, cy_warnings, cy_rules, cy_record = _courtyard(ctx, run, courtyard)
    issues, warnings = fa.relax_g3(base[0], base[1], run.profile,
                                   codes=_codes(place, base[0]))
    rules = _added(base[1], warnings)
    finding = _board_finding(ctx, run, "G3")
    _record_stage(ctx, "G3", finding.summary, rules + cy_rules, courtyard=cy_record)
    return cy, _with_lists(place, base, issues + finding.blockers,
                           warnings + cy_warnings + finding.warnings)


# ----------------------------------------------------------------------------
# G4
# ----------------------------------------------------------------------------
def _drc_rules(split: fa.DrcSplit) -> list[str]:
    n_p, n_i = len(split.placement), len(split.intrinsic)
    if not n_p + n_i:
        return []
    return [f"{n_p} DRC violation(s) from the source placement and {n_i} inside one "
            f"footprint are warnings, not blockers"]


def g4_inputs(ctx: Any, drc_rep: Mapping[str, Any], routed: Mapping[str, Any]
              ) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    """(drc_rep, routed) for gates.g4_layout on a rebuild: DRC errors and
    types are our part of faithful.split_drc (unconnected untouched), the
    source-placement and intrinsic findings become warnings, and the routed
    board's connectivity against the source is appended (a difference is an
    issue). Without a readable DRC report every violation stays ours.
    Inactive: the inputs themselves."""
    run = state(ctx)
    if not run.active:
        return drc_rep, routed
    base = _base_lists(routed)
    split, path, why = _split(ctx, run)
    if split is None:
        drc_out, drc_warnings, rules = drc_rep, [
            f"faithful: {why}; every DRC violation is judged as ours"], []
        record: dict[str, Any] = {"split": False, "reason": why}
    else:
        drc_out = {**drc_rep, "errors": split.ours_errors, "types": dict(split.ours_types)}
        drc_warnings = [f"faithful: {t}" for t in split.placement + split.intrinsic]
        rules, record = _drc_rules(split), {**split.as_dict(), "report": str(path)}
    finding = _board_finding(ctx, run, "G4")
    _record_stage(ctx, "G4", finding.summary, rules, drc=record)
    return drc_out, _with_lists(routed, base, base[0] + finding.blockers,
                                base[1] + drc_warnings + finding.warnings)
