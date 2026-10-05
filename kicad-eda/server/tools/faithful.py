#!/usr/bin/env python3
"""Faithful-rebuild rules (ROADMAP_DECISIONS D4) as pure functions.

A design that carries a ``source`` block is a rebuild of an existing board.
The gates then judge it against that source instead of against our own
napkin rules:

- G2: missing bypass caps are the source's design, not ours: the decoupling
  blockers become warnings and their ``add_decoupling`` tasks go; ERC is
  judged against the source's baseline (only NEW violations block).
- G3: a J* connector kept where the source has it (mouth inward, or far from
  the edge) is a warning; the courtyard count only holds violations we made.
- G4: DRC is split by who owns each violation (split_drc): ours (blocker),
  the source placement (keep mode, warning) or intrinsic to one footprint
  (warning). Unconnected items always block.
- G2/G3/G4: connectivity must equal the source's (equivalence_findings, a
  pad-partition comparison through net_equivalence.compare).
- One pack scale when the rebuild keeps every part where the source has it.

Every function is an identity when the profile is inactive (no ``source``).
No subprocess and no pcbnew: the pipeline I/O is pipeline/stages_faithful.py.
Report arguments are parsed kicad-cli JSON dicts, or paths to them.
"""
from __future__ import annotations

import copy
import json
import os
import re
import sys
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NamedTuple

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import net_equivalence as ne  # noqa: E402

REBUILD_MODES = ("keep", "reflow")
DEFAULT_REBUILD = "reflow"        # `source` without `rebuild`: the stricter DRC split
KIPRJMOD = re.compile(r"^\$\{KIPRJMOD\}[\\/]*")
# ERC violation types our schematic generator produces whatever the design is
# (so a rebuilt schematic has them and the source does not). Empty until a
# rebuild shows one; add a type only with that evidence.
GENERATOR_ERC_TYPES: tuple[str, ...] = ()
ERC_SEVERITIES: tuple[str, ...] = ("error",)        # what gates.g2_schematic counts
DRC_ERROR = "error"                                 # the harness runs --severity-error
COPPER_KINDS = frozenset({"track", "arc", "via", "zone"})
CONNECTOR_CODES = frozenset({"connector_edge"})     # swig_gate_checks issue codes (F3-13)
MAX_LISTED = 10                                     # differences / ERC items in a message
PADS_SHOWN = 8

NO_ERC_BASELINE = ("faithful: no source ERC baseline (source.erc_baseline), so ERC is "
                   "judged as usual")
KEPT_CONNECTOR = "(connector kept where the source design has it)"


# ----------------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------------
def _natural(text: str) -> list:
    """Natural sort key ('R2' < 'R10'). re.split with a group alternates
    str / int, so two keys compare like with like at every index."""
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", text)]


def _listed(items: Sequence[str], limit: int = MAX_LISTED) -> str:
    more = len(items) - limit
    return "; ".join(items[:limit]) + (f" (+{more} more)" if more > 0 else "")


def _load_report(report: Any, label: str) -> Mapping[str, Any]:
    """A kicad-cli JSON report given as a dict or as a path to the file."""
    if isinstance(report, (str, os.PathLike)):
        path = Path(report)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as e:
            raise ValueError(f"{label} {path} is not JSON: {e}") from e
        if not isinstance(data, Mapping):
            raise ValueError(f"{label} {path} is not a JSON object")
        return data
    if not isinstance(report, Mapping):
        raise TypeError(f"{label} must be a parsed JSON object or a path, not "
                        f"{type(report).__name__}")
    return report


def _records(container: Mapping[str, Any], key: str, label: str) -> list[Mapping[str, Any]]:
    """container[key] as a list of objects (missing = empty); fails loudly."""
    raw = container.get(key, [])
    if not isinstance(raw, list) or not all(isinstance(r, Mapping) for r in raw):
        raise ValueError(f"{label}: '{key}' must be a list of objects")
    return raw


def _item_texts(violation: Mapping[str, Any], label: str) -> list[str]:
    items = violation.get("items", [])
    if not isinstance(items, list):
        raise ValueError(f"{label}: violation 'items' must be a list")
    return [str(i.get("description", "")) if isinstance(i, Mapping) else str(i) for i in items]


# ----------------------------------------------------------------------------
# profile / pack_scales
# ----------------------------------------------------------------------------
@dataclass(frozen=True)
class FaithfulProfile:
    """What a design says about rebuilding its source. ``reference`` /
    ``erc_baseline`` are resolved paths (None: compare against the netlist
    itself / no ERC baseline); ``board_only`` are refs absent from any
    schematic; ``ignore_refs`` are refs the variant dropped on purpose."""
    active: bool = False
    kind: str | None = None
    rebuild: str | None = None
    reference: Path | None = None
    erc_baseline: Path | None = None
    board_only: frozenset[str] = frozenset()
    ignore_refs: frozenset[str] = frozenset()
    all_fixed: bool = False
    notes: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        """JSON-ready (paths as strings, sets as sorted lists)."""
        def path(p: Path | None) -> str | None:
            return None if p is None else str(p)

        return {"active": self.active, "kind": self.kind, "rebuild": self.rebuild,
                "reference": path(self.reference), "erc_baseline": path(self.erc_baseline),
                "board_only": sorted(self.board_only, key=_natural),
                "ignore_refs": sorted(self.ignore_refs, key=_natural),
                "all_fixed": self.all_fixed, "notes": list(self.notes)}


INACTIVE = FaithfulProfile()


def _source_path(source: Mapping[str, Any], key: str, base: Path | None) -> Path | None:
    """source[key] by the D1 path rule: relative to the netlist directory
    (``${KIPRJMOD}`` = that directory) or absolute; None when unset."""
    raw = source.get(key)
    if raw is None or raw == "":
        return None
    if not isinstance(raw, str):
        raise ValueError(f"source.{key} must be a path string, not {raw!r}")
    m = KIPRJMOD.match(raw)
    rel = Path(raw[m.end():] if m else raw)
    if rel.is_absolute() or base is None:
        return rel
    return base / rel


def _components(design: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    comps = design.get("components", [])
    if not isinstance(comps, list):
        raise ValueError("netlist 'components' must be a list")
    return [c for c in comps if isinstance(c, Mapping)]


def _fixed(comp: Mapping[str, Any]) -> bool:
    """Placed by the source: a D3 origin anchor, or the x/y centre pair."""
    return comp.get("at") is not None or (comp.get("x") is not None
                                          and comp.get("y") is not None)


def _dropped_refs(design: Mapping[str, Any]) -> frozenset[str]:
    variant = design.get("variant") or {}
    if not isinstance(variant, Mapping):
        raise ValueError("netlist 'variant' must be an object")
    dropped = variant.get("dropped_refs", [])
    if not isinstance(dropped, list) or not all(isinstance(r, str) for r in dropped):
        raise ValueError(f"variant.dropped_refs must be a list of references, not {dropped!r}")
    return frozenset(dropped)


def _rebuild(design: Mapping[str, Any]) -> tuple[str, list[str]]:
    mode = design.get("rebuild")
    if mode is None:
        return DEFAULT_REBUILD, [f"faithful: the design sets no 'rebuild'; judged as "
                                 f"'{DEFAULT_REBUILD}' (our placement)"]
    if mode not in REBUILD_MODES:
        raise ValueError(f"rebuild must be one of {REBUILD_MODES}, not {mode!r}")
    return mode, []


def profile(design: Mapping[str, Any], netlist_path: str | os.PathLike | None
            ) -> FaithfulProfile:
    """The faithful profile of a netlist.json design: active iff it has a
    ``source`` block. Relative source paths resolve against the netlist's
    directory. Raises ValueError on a malformed source / rebuild / variant."""
    if not isinstance(design, Mapping):
        raise ValueError("a netlist.json design must be an object")
    source = design.get("source")
    if source is None:
        return INACTIVE
    if not isinstance(source, Mapping):
        raise ValueError(f"netlist 'source' must be an object, not {source!r}")
    base = Path(netlist_path).parent if netlist_path else None
    kind = source.get("kind")
    rebuild, notes = _rebuild(design)
    reference = _source_path(source, "reference", base)
    erc_baseline = _source_path(source, "erc_baseline", base)
    if reference is None:
        notes.append(f"faithful: no source.reference for this {kind or 'unknown'} source; "
                     "equivalence is judged against the netlist itself")
    if erc_baseline is None:
        notes.append("faithful: no source.erc_baseline; ERC is judged as usual")
    comps = _components(design)
    return FaithfulProfile(
        active=True, kind=kind if isinstance(kind, str) else None, rebuild=rebuild,
        reference=reference, erc_baseline=erc_baseline,
        board_only=frozenset(str(c.get("ref")) for c in comps if c.get("board_only") is True),
        ignore_refs=_dropped_refs(design),
        all_fixed=bool(comps) and all(_fixed(c) for c in comps), notes=tuple(notes))


def pack_scales(profile: FaithfulProfile, default: tuple[float, ...]) -> tuple[float, ...]:
    """(1.0,) for a keep rebuild whose every part is placed by the source
    (a looser re-pack would move nothing); else ``default`` itself."""
    if profile.active and profile.rebuild == "keep" and profile.all_fixed:
        return (1.0,)
    return default


# ----------------------------------------------------------------------------
# ERC against the source baseline
# ----------------------------------------------------------------------------
_PIN_ITEM = re.compile(r"^Symbol (?P<ref>\S+) (?:Hidden pin|Pin)(?: (?P<pin>[^\s\[]*))?\s*"
                       r"(?:\[(?P<info>.*)\])?\s*$")
_SYMBOL_ITEM = re.compile(r"^Symbol (?P<ref>\S+)(?:\s+\[(?P<info>.*)\])?\s*$")
_ANY_SYMBOL = re.compile(r"\bSymbol (?P<ref>[^\s\[\]]+)")
_VIRTUAL = re.compile(r"#[A-Za-z_]*")
ErcKey = tuple[str, tuple[str, ...]]


def _virtual(ref: str, name: str) -> str:
    """'#PWR07' / '#PWR?' + 'GND' -> '#PWR(GND)': power and flag symbols are
    numbered differently by every annotation, so their net names them."""
    return f"{_VIRTUAL.match(ref).group(0)}({name})"


def _pin_token(m: re.Match) -> str:
    fields = [f.strip() for f in (m["info"] or "").split(",")]
    name = ", ".join(fields[:-2]) if len(fields) > 2 else ""
    pin = m["pin"] or ""
    if m["ref"].startswith("#"):
        return _virtual(m["ref"], name or pin)
    return f"{m['ref']}.{pin or name}"


def erc_token(description: str) -> str | None:
    """The symbol an ERC item names: 'U1.3' (pin 3 of U1), 'C13P' (the
    symbol), '#PWR(GND)' (a power symbol by its net); None for wires, labels,
    junctions and the like. Tolerant of the KiCad 9 and 10.99 formats
    ('Symbol U1 Pin 3 [SCL, Input, Line]', 'Symbol #PWR? Hidden pin  [...]')."""
    text = description.strip()
    m = _PIN_ITEM.match(text)
    if m:
        return _pin_token(m)
    m = _SYMBOL_ITEM.match(text)
    if m:
        ref = m["ref"]
        return _virtual(ref, m["info"] or "") if ref.startswith("#") else ref
    m = _ANY_SYMBOL.search(text)
    if m:
        ref = m["ref"]
        return _virtual(ref, "") if ref.startswith("#") else ref
    return None


def _erc_violations(report: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    out = list(_records(report, "violations", "ERC report"))
    for sheet in _records(report, "sheets", "ERC report"):
        out += _records(sheet, "violations", "ERC report sheet")
    return out


def erc_keys(erc_report: Any, *, severities: Iterable[str] | None = ERC_SEVERITIES
             ) -> Counter[ErcKey]:
    """Counter of (type, symbol tokens) over a kicad-cli ERC JSON report
    (every sheet), counting the given severities (None: all). A violation that
    names no symbol is keyed by its type alone: (type, ())."""
    report = _load_report(erc_report, "ERC report")
    wanted = None if severities is None else frozenset(severities)
    keys: Counter[ErcKey] = Counter()
    for v in _erc_violations(report):
        if wanted is not None and str(v.get("severity", "error")) not in wanted:
            continue
        tokens = {t for t in map(erc_token, _item_texts(v, "ERC report")) if t}
        keys[(str(v.get("type", "?")), tuple(sorted(tokens, key=_natural)))] += 1
    return keys


def _erc_text(key: ErcKey, count: int) -> str:
    kind, tokens = key
    text = f"{kind}: {', '.join(tokens)}" if tokens else f"{kind} (no symbol)"
    return text + (f" (x{count})" if count > 1 else "")


def erc_new(rebuilt: Any, baseline: Any, *,
            severities: Iterable[str] | None = ERC_SEVERITIES) -> list[str]:
    """ERC violations of the rebuilt schematic that the source baseline does
    not have (multiset difference of erc_keys; a Counter is taken as is),
    as sorted human-readable lines; GENERATOR_ERC_TYPES are left out."""
    def keys(x: Any) -> Counter[ErcKey]:
        return x if isinstance(x, Counter) else erc_keys(x, severities=severities)

    new = keys(rebuilt) - keys(baseline)
    return [_erc_text(k, n) for k, n in
            sorted(new.items(), key=lambda kv: (kv[0][0], [_natural(t) for t in kv[0][1]]))
            if k[0] not in GENERATOR_ERC_TYPES]


# ----------------------------------------------------------------------------
# relax_g2
# ----------------------------------------------------------------------------
_DECOUPLING = re.compile(r"^(?P<ref>.+?): rail '(?P<rail>.*)' has no bypass cap$")
_ERC_COUNT = re.compile(r"^ERC reports (?P<n>\d+) violation\(s\)$")
_ERC_TASK = re.compile(r"^resolve \d+ ERC violation\(s\)$")


def _match(pattern: re.Pattern, item: Any) -> re.Match | None:
    return pattern.match(item) if isinstance(item, str) else None


def _relax_decoupling(blockers: list, warnings: list, tasks: list) -> tuple[list, list, list]:
    gone = [(m["ref"], m["rail"]) for m in (_match(_DECOUPLING, b) for b in blockers) if m]
    if not gone:
        return blockers, warnings, tasks
    pairs = set(gone)

    def decoupling_task(t: Any) -> bool:
        fix = t.get("fix") if isinstance(t, Mapping) else None
        return (isinstance(fix, Mapping) and fix.get("type") == "add_decoupling"
                and (fix.get("ref"), fix.get("rail")) in pairs)

    return ([b for b in blockers if not _match(_DECOUPLING, b)],
            warnings + [f"faithful: the source design has no bypass on {rail} at {ref} "
                        f"(kept as designed)" for ref, rail in gone],
            [t for t in tasks if not decoupling_task(t)])


def _erc_task_index(tasks: list) -> int | None:
    return next((i for i, t in enumerate(tasks) if isinstance(t, Mapping)
                 and _match(_ERC_TASK, t.get("desc")) and not t.get("auto")), None)


def _judge_erc(blockers: list, warnings: list, tasks: list, new_erc: list[str] | None,
               erc_total: int | None) -> tuple[list, list, list]:
    idx = next((i for i, b in enumerate(blockers) if _match(_ERC_COUNT, b)), None)
    if new_erc is None:
        return blockers, warnings + ([NO_ERC_BASELINE] if idx is not None else []), tasks
    reported = int(_ERC_COUNT.match(blockers[idx])["n"]) if idx is not None else 0
    rest = [b for i, b in enumerate(blockers) if i != idx]
    t_idx = _erc_task_index(tasks) if idx is not None else None
    rest_tasks = [t for i, t in enumerate(tasks) if i != t_idx]
    new = list(new_erc)
    if not new:
        total = reported if erc_total is None else erc_total
        note = ([f"faithful: ERC reports {total} violation(s), none new against the source "
                 f"baseline"] if idx is not None else [])
        return rest, warnings + note, rest_tasks
    blocker = f"ERC: {len(new)} violation(s) not in the source baseline: {_listed(new)}"
    task = {"desc": f"resolve {len(new)} ERC violation(s) not in the source baseline",
            "auto": False, "fix": {}}
    at = len(rest) if idx is None else idx
    t_at = len(rest_tasks) if t_idx is None else t_idx
    return (rest[:at] + [blocker] + rest[at:], warnings,
            rest_tasks[:t_at] + [task] + rest_tasks[t_at:])


def relax_g2(verdict: Mapping[str, Any], *, new_erc: list[str] | None,
             erc_total: int | None, profile: FaithfulProfile) -> dict[str, Any]:
    """A new G2 verdict (gates.g2_schematic shape) under the D4 rules:
    decoupling blockers become warnings and their add_decoupling tasks go;
    the 'ERC reports n violation(s)' blocker is replaced by one listing the
    ``new_erc`` violations (erc_new), or by a warning when there are none.
    ``new_erc`` None = no baseline: the ERC blocker stays and a note says so.
    Thermal, floating-pin and design-review blockers are untouched; passed
    is recomputed. The input is never mutated."""
    out = copy.deepcopy(dict(verdict))
    if not profile.active:
        return out
    parts = (list(out.get("blockers", [])), list(out.get("warnings", [])),
             list(out.get("tasks", [])))
    blockers, warnings, tasks = _judge_erc(*_relax_decoupling(*parts), new_erc, erc_total)
    return {**out, "blockers": blockers, "warnings": warnings, "tasks": tasks,
            "passed": not blockers}


# ----------------------------------------------------------------------------
# relax_g3
# ----------------------------------------------------------------------------
# The connector texts swig_gate_checks.check_placement emits (F3-13 keeps them).
_CONNECTOR = re.compile(r"^\S+: mating face (?:points INTO the board interior"
                        r"|is -?[\d.]+ mm from the board edge it faces)")


def _issue_text(issue: Any) -> str:
    if isinstance(issue, Mapping):
        for key in ("msg", "message", "text", "issue"):
            if isinstance(issue.get(key), str):
                return issue[key]
    return str(issue)


def _is_connector_issue(issue: Any, code: Any) -> bool:
    if code is None and isinstance(issue, Mapping):
        code = issue.get("code")
    if code is not None:
        return code in CONNECTOR_CODES
    return bool(_CONNECTOR.match(_issue_text(issue)))


def relax_g3(issues: Sequence[Any], warnings: Sequence[Any], profile: FaithfulProfile, *,
             codes: Sequence[Any] | None = None) -> tuple[list, list]:
    """(issues, warnings) for gates.g3_placement with the J*-at-edge
    connector issues moved to warnings. An issue's structured code (a
    ``code`` key, or ``codes[i]`` parallel to the issues) wins over its text;
    without one the swig_gate_checks connector texts are matched."""
    if codes is not None and len(codes) != len(issues):
        raise ValueError(f"codes must parallel the issues ({len(codes)} codes for "
                         f"{len(issues)} issues)")
    if not profile.active:
        return list(issues), list(warnings)
    kept, relaxed = [], []
    for i, issue in enumerate(issues):
        if _is_connector_issue(issue, None if codes is None else codes[i]):
            relaxed.append(f"faithful: {_issue_text(issue)} {KEPT_CONNECTOR}")
        else:
            kept.append(issue)
    return kept, list(warnings) + relaxed


# ----------------------------------------------------------------------------
# DRC split
# ----------------------------------------------------------------------------
@dataclass(frozen=True)
class DrcItem:
    """One DRC item: kind is track | arc | via | zone (our copper),
    footprint (owned by ``ref``), edge (a board Edge.Cuts shape), board
    (any other board item) or unknown."""
    kind: str
    ref: str | None
    layer: str | None
    description: str


_MASK_NET = re.compile(r"\[[^\]]*\]")
_MASK_QUOTED = re.compile(r"'[^']*'|\"[^\"]*\"")
_COPPER = (("arc", re.compile(r"^Track \(arc\)")), ("track", re.compile(r"^Track\b")),
           ("via", re.compile(r"^(?:Blind/Buried |Micro |Through )?Via\b")),
           ("zone", re.compile(r"^(?:Zone|Teardrop)\b")))
_FOOTPRINT = re.compile(r"^Footprint (?P<ref>\S*)$")
_OF_REF = re.compile(r"\bof (?P<ref>[^\s(]*)(?=\s|\(|$)")
_ON_LAYER = re.compile(r"\bon (?P<layer>[^\s,]+)")
EDGE_LAYER = "Edge.Cuts"


def classify_item(description: str) -> DrcItem:
    """Who owns a kicad-cli DRC item, from its description ('Track [GND] on
    F.Cu, length 3 mm', 'Pad 2 [GND] of R1 on F.Cu', 'Footprint R1',
    'Segment on Edge.Cuts', ...). Net names and quoted names are masked
    first, so 'of' inside them never reads as a footprint reference."""
    text = _MASK_QUOTED.sub("''", _MASK_NET.sub("[]", description.strip()))
    on = _ON_LAYER.search(text)
    layer = on["layer"] if on else None
    for kind, pattern in _COPPER:
        if pattern.match(text):
            return DrcItem(kind, None, layer, description)
    owner = _FOOTPRINT.match(text) or _OF_REF.search(text)
    if owner:
        return DrcItem("footprint", owner["ref"], layer, description)
    if layer is None:
        return DrcItem("unknown", None, None, description)
    return DrcItem("edge" if layer == EDGE_LAYER else "board", None, layer, description)


@dataclass(frozen=True)
class DrcSplit:
    """A DRC report split by ownership. ``ours_*`` and ``unconnected`` feed
    gates.g4_layout as blockers; ``placement`` (the source placement, keep
    mode) and ``intrinsic`` (one footprint against itself) are warnings;
    ``ours`` lists our violations for the report."""
    ours_errors: int
    ours_types: dict[str, int]
    placement: list[str]
    intrinsic: list[str]
    unconnected: int
    ours: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"ours_errors": self.ours_errors, "ours_types": dict(self.ours_types),
                "ours": list(self.ours), "placement": list(self.placement),
                "intrinsic": list(self.intrinsic), "unconnected": self.unconnected}


def _owner(items: list[DrcItem], rebuild: str | None) -> str:
    """ours | placement | intrinsic for one violation's items."""
    if not items or any(i.kind in COPPER_KINDS for i in items):
        return "ours"                     # our copper (or nothing to judge by)
    if any(i.kind not in ("footprint", "edge") for i in items):
        return "ours"                     # another board item, or one we cannot read
    refs = {i.ref for i in items if i.kind == "footprint"}
    if len(refs) == 1 and all(i.kind == "footprint" for i in items):
        return "intrinsic"
    return "placement" if rebuild == "keep" else "ours"


def _drc_text(violation: Mapping[str, Any], texts: list[str], owner: str,
              items: list[DrcItem]) -> str:
    kind, what = violation.get("type", "?"), violation.get("description", "")
    where = {"ours": "", "placement": " from the source placement",
             "intrinsic": f" inside {items[0].ref if items else '?'} "
                          "(intrinsic to the footprint)"}[owner]
    return f"DRC {kind}{where}: {what} [{'; '.join(sorted(texts, key=_natural))}]"


def split_drc(drc_json: Any, profile: FaithfulProfile) -> DrcSplit:
    """Split a kicad-cli DRC JSON report by ownership. Only severity 'error'
    counts. A violation with any of our copper (track/arc/via/zone) or any
    other board item is ours. Footprint-only items of ONE footprint are
    intrinsic (any mode); in keep mode, footprint and Edge.Cuts items are the
    source placement; in reflow mode everything else is ours. Inactive: every
    violation is ours, exactly the counts run_pipeline._drc takes.
    Messages, their items and the type counts are sorted: kicad-cli orders
    items and violations differently from run to run."""
    report = _load_report(drc_json, "DRC report")
    violations = _records(report, "violations", "DRC report")
    unconnected = len(_records(report, "unconnected_items", "DRC report"))
    groups: dict[str, list[str]] = {"ours": [], "placement": [], "intrinsic": []}
    types: dict[str, int] = {}
    for v in violations:
        if profile.active and str(v.get("severity", DRC_ERROR)) != DRC_ERROR:
            continue
        texts = _item_texts(v, "DRC report")
        items = [classify_item(t) for t in texts]
        owner = _owner(items, profile.rebuild) if profile.active else "ours"
        groups[owner].append(_drc_text(v, texts, owner, items))
        if owner == "ours":
            kind = str(v.get("type", "?"))
            types[kind] = types.get(kind, 0) + 1
    ours, placement, intrinsic = (sorted(groups[k], key=_natural)
                                  for k in ("ours", "placement", "intrinsic"))
    return DrcSplit(len(ours), dict(sorted(types.items())), placement, intrinsic,
                    unconnected, ours)


def courtyard_view(split: DrcSplit) -> dict[str, Any]:
    """The run_pipeline._courtyard_drc dict for gates.g3_placement, counting
    only our courtyard violations."""
    return {"courtyard_violations": sum(n for t, n in split.ours_types.items()
                                        if "courtyard" in t),
            "types": dict(split.ours_types)}


# ----------------------------------------------------------------------------
# equivalence_findings
# ----------------------------------------------------------------------------
class Finding(NamedTuple):
    """An equivalence verdict for one stage: blockers / warnings for the gate
    and ``summary`` {stage, equal, summary, differences (first 10),
    differences_total, ignored_refs, duplicate_refs, pad_conflicts}."""
    blockers: list[str]
    warnings: list[str]
    summary: dict[str, Any]


def _nets_and_meta(side: Any, label: str) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    """Pad nets, or (pad nets, meta) as net_equivalence.load_pad_nets_meta /
    read_reference return them."""
    if (isinstance(side, tuple) and len(side) == 2 and isinstance(side[0], Mapping)
            and isinstance(side[1], Mapping)):
        return side[0], side[1]
    if isinstance(side, Mapping):
        return side, {}
    raise ValueError(f"{label} must be pad nets {{net: [(ref, pad), ...]}} or (pad nets, "
                     f"meta), not {type(side).__name__}")


def _pads(keys: Iterable[Sequence[str]]) -> str:
    keys = list(keys)
    shown = " ".join(f"{r}.{p}" for r, p in keys[:PADS_SHOWN])
    return shown + (f" ... (+{len(keys) - PADS_SHOWN})" if len(keys) > PADS_SHOWN else "")


def _parts(diff: ne.NetDiff) -> str:
    return " | ".join(f"{p.net or '(no net)'}: {_pads(p.pads)}" for p in diff.parts)


def _differences(res: ne.EquivalenceResult) -> list[str]:
    d = res.details
    return ([f"source net '{x.net}' is split here into {_parts(x)}" for x in res.split_nets]
            + [f"net '{x.net}' here joins source nets {_parts(x)}" for x in res.merged_nets]
            + [f"missing here: {_pads(keys)} (source net {net or '(no net)'})"
               for net, keys in d["missing_by_net"].items()]
            + [f"extra here: {_pads(keys)} (net {net or '(no net)'})"
               for net, keys in d["extra_by_net"].items()])


def _duplicate_warning(stage: str, side: str, refs: list[str]) -> list[str]:
    if not refs:
        return []
    names = [r or "(none)" for r in refs]          # "" = footprints without a reference
    what = (f"reference {names[0]} is" if len(names) == 1
            else f"references {', '.join(names)} are")
    return [f"{stage}: {what} shared by several parts of the {side}; their pads count as "
            f"one part, so the comparison cannot tell them apart"]


def equivalence_findings(stage: str, reference: Any, candidate: Any, *,
                         ignore_refs: Iterable[str] = ()) -> Finding:
    """Connectivity of ``candidate`` against the source ``reference`` (pad
    nets, or (pad nets, meta)), by net_equivalence.compare with single-pin
    pads ignored and ``ignore_refs`` dropped from both sides. A difference is
    a blocker '<stage>: connectivity differs from the source: <summary>:
    <first 10 differences>'; references shared by several parts (meta
    duplicate_refs) are warnings."""
    ref_nets, ref_meta = _nets_and_meta(reference, "reference")
    cand_nets, cand_meta = _nets_and_meta(candidate, "candidate")
    ignore = frozenset(str(r) for r in ignore_refs)
    res = ne.compare(ref_nets, cand_nets, ignore_single_pin=True, ignore_refs=ignore)
    diffs = _differences(res)
    dups = {side: sorted((str(r) for r in meta.get("duplicate_refs", []) if r not in ignore),
                         key=_natural)
            for side, meta in (("reference", ref_meta), ("candidate", cand_meta))}
    blockers = [] if res.equal else [
        f"{stage}: connectivity differs from the source: {res.summary()}: {_listed(diffs)}"]
    warnings = [w for side, refs in dups.items() for w in _duplicate_warning(stage, side, refs)]
    summary = {"stage": stage, "equal": res.equal, "summary": res.summary(),
               "differences": diffs[:MAX_LISTED], "differences_total": len(diffs),
               "ignored_refs": list(res.details["ignored_refs"]), "duplicate_refs": dups,
               "pad_conflicts": ne.real_conflicts(res.details)}
    return Finding(blockers, warnings, summary)
