#!/usr/bin/env python3
"""Cross-check two independent vision readings of one schematic (ROADMAP_DECISIONS D4).

The PDF / image path transcribes a schematic twice, independently (two
``schematic-reader`` runs over the pages rasterize.py produced), into two
netlist.json files. Neither reading is the reference: this module lists every
point where they disagree, for a reviewer to settle against the drawing, and
scores how far they agree.

1. Components, keyed by reference: a part only one reading has, a reference
   listed twice, and per shared part its ``value`` (compared as a quantity:
   100nF = 0.1uF, 4k7 = 4.7k, 2R2 = 2.2 ohm, 3V3 = 3.3V, 2A5 = 2.5A;
   1M != 1m), ``lib_id`` and ``footprint``.
2. Connectivity, through net_equivalence.compare (pad partitions; net names
   never matter): a net split or merged, connected pins one reading lacks, a
   pad on two nets, and a pin one reading marks no-connect that the other
   does not list (a no-connect pin the other reading wires is a split/merge;
   one on a part the other reading lacks is covered by the part's row).

Score = 0.25 components (shared refs / all refs) + 0.25 fields (agreeing /
compared field values of shared parts; 0 when parts exist but none is
shared, 1 when both readings are empty or no shared part has any field) +
0.5 connectivity (pins on the corresponding net / pins connected in either
reading, nets corresponding one-to-one by greatest pin overlap). 1.0 =
identical readings; ``agree`` = no disagreement at all. With a ``ref_map``
{ref in A: ref in B}, A's parts are reported under B's references.

``pin_check(design)`` checks ONE reading against its KiCad symbols (netlist
``libraries`` block, project sym-lib-table, then the stock library, D1):
pads that are not pin numbers of the symbol, symbol pins neither netted nor
no_connect (an unread pin), and symbols that do not resolve; it also returns
each symbol's pin table (number, name, electrical type). A symbol pin of
electrical type no_connect (e.g. AP2112K-3.3 pin 4 'NC') that the reading
leaves out is listed under ``nc_pins``: informational, not an issue.

File-level helpers (what the MCP layer wraps, so it does not repeat the CLI):
``crosscheck_files(a_path, b_path, ref_map=None, labels=('A', 'B'))`` ->
CrosscheckResult (``as_dict()`` / ``markdown()``; ``ref_map`` a mapping, JSON
text or a JSON file) and ``pin_check_file(netlist_path)`` -> dict (the
report's ``as_dict()`` plus ``netlist`` and ``summary``; ``ok`` there means
no pin issue, not that the call worked).

Conventions D4 does not fix yet (this module and agents/schematic-reader.md
follow them):
- score = 0.25 components + 0.25 fields + 0.5 connectivity (WEIGHTS);
- severity: value, a missing / extra / duplicated part and every
  connectivity disagreement high; lib_id / footprint med; no-connect low;
- a reader's unreadable or uncertain spots are netlist assumptions with ids
  VIS-<n> (topic "vision: unreadable <what>", confidence low or med);
- a reading carries source.kind "vision", source.tool
  'schematic-reader (Claude vision) + pymupdf <ver> @ <dpi> dpi'
  (the raster manifest's tool and dpi) and "rebuild": "reflow" (a drawing
  has no placement).

CLI (exit 0 agree / clean, 1 disagreements / pin issues, 2 error):
  vision_crosscheck.py compare A.json B.json [--ref-map JSON|file]
      [--label-a A] [--label-b B] [--json] [--out report.json] [--md report.md]
  vision_crosscheck.py pins netlist.json [--symbols-dir DIR] [--json]

Comparison is stdlib-only (plus net_equivalence); pin_check imports symlib
(sexpdata) and libtable lazily.
"""
from __future__ import annotations

import argparse
import copy
import json
import math
import os
import re
import sys
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

import net_equivalence as ne

PadKey = tuple[str, str]
SERVER_DIR = Path(__file__).resolve().parents[1]

SEVERITIES = ("high", "med", "low")
FIELDS = ("value", "lib_id", "footprint")
FIELD_SEVERITY = MappingProxyType({"value": "high", "lib_id": "med", "footprint": "med"})
WEIGHTS = MappingProxyType({"components": 0.25, "fields": 0.25, "connectivity": 0.5})
DUPLICATE_REF = "duplicate_ref"
COMPONENT_MISSING = "component_missing"     # in A only
COMPONENT_EXTRA = "component_extra"         # in B only
FIELD_MISMATCH = "field_mismatch"
NET_SPLIT = "net_split"                     # one net of A spread over several of B
NET_MERGE = "net_merge"                     # one net of B joins several of A
PIN_MISSING = "pin_missing"                 # connected in A, no such pin in B
PIN_EXTRA = "pin_extra"                     # connected in B, no such pin in A
PAD_CONFLICT = "pad_conflict"               # one reading puts a pad on two nets
NO_CONNECT_MISMATCH = "no_connect_mismatch"
KINDS = (DUPLICATE_REF, COMPONENT_MISSING, COMPONENT_EXTRA, FIELD_MISMATCH, NET_SPLIT, NET_MERGE,
         PIN_MISSING, PIN_EXTRA, PAD_CONFLICT, NO_CONNECT_MISMATCH)
NC_PIN_TYPE = "no_connect"          # symbol pin electrical type: unconnected by design

# --- component values as quantities -------------------------------------------
_SI = {"p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "μ": 1e-6, "m": 1e-3,
       "k": 1e3, "K": 1e3, "M": 1e6, "G": 1e9}
_UNITS = {"F": "F", "H": "H", "V": "V", "A": "A", "W": "W", "Hz": "Hz", "R": "ohm",
          "Ω": "ohm", "Ω": "ohm", "ohm": "ohm", "ohms": "ohm"}
_UNIT = "Hz|ohms?|[FHVAWRΩΩ]"
_PREFIX = "[pnuµμmkKMG]"
_PLAIN = re.compile(rf"(\d+(?:\.\d*)?|\.\d+)({_PREFIX}?)({_UNIT})?")
_RKM = re.compile(rf"(\d+)({_PREFIX}|R)(\d+)({_UNIT})?")         # 4k7, 2R2, 1u0
_RKM_UNIT = re.compile(r"(\d+)([VAWFH])(\d+)")                    # 3V3, 2A5, 1W5
_GLUE = re.compile(rf"(?<=[\d.])\s+(?=(?:{_PREFIX}|{_UNIT}))")   # "100 nF" -> "100nF"
_TOKENS = re.compile(r"[\s/,;]+")
_DIGITS = re.compile(r"([0-9]+)")
_PIPE = re.compile(r"\|")


def _natural(text: str) -> tuple:
    """'R10' sorts after 'R2'."""
    return tuple(int(p) if i % 2 else p for i, p in enumerate(_DIGITS.split(text)))


def _pad_order(key: PadKey) -> tuple:
    return (_natural(key[0]), _natural(key[1]))


def _fmt(pads: Iterable[Any]) -> str:
    return " ".join(f"{ref}.{pad}" for ref, pad in pads)


def parse_value(text: str) -> tuple[float, str] | None:
    """'4k7' -> (4700.0, ''), '100nF' -> (1e-07, 'F'), '2R2' -> (2.2, 'ohm'),
    '3V3' -> (3.3, 'V') (a unit letter V/A/W/F/H as the RKM decimal mark);
    None when the text is not a single quantity (a part number, a note)."""
    s = _GLUE.sub("", str(text).strip())
    m = _RKM.fullmatch(s)
    if m:
        whole, mark, frac, unit = m.groups()
        scale = 1.0 if mark == "R" else _SI[mark]
        return float(f"{whole}.{frac}") * scale, _UNITS[unit] if unit else (
            "ohm" if mark == "R" else "")
    m = _RKM_UNIT.fullmatch(s)
    if m:
        whole, unit, frac = m.groups()
        return float(f"{whole}.{frac}"), _UNITS[unit]
    m = _PLAIN.fullmatch(s)
    if m:
        number, prefix, unit = m.groups()
        return float(number) * (_SI[prefix] if prefix else 1.0), _UNITS[unit] if unit else ""
    return None


def _token_agree(x: str, y: str) -> bool:
    qx, qy = parse_value(x), parse_value(y)
    if qx is not None and qy is not None:
        units_ok = not qx[1] or not qy[1] or qx[1] == qy[1]
        return units_ok and math.isclose(qx[0], qy[0], rel_tol=1e-9, abs_tol=1e-18)
    return x.casefold() == y.casefold()


def values_agree(a: Any, b: Any) -> bool:
    """Two component values mean the same: token by token (split on space,
    '/', ',' and ';'), each an equal quantity (missing units are wildcards)
    or the same text ignoring case."""
    ta = [t for t in _TOKENS.split(_GLUE.sub("", str(a).strip())) if t]
    tb = [t for t in _TOKENS.split(_GLUE.sub("", str(b).strip())) if t]
    return len(ta) == len(tb) and all(_token_agree(x, y) for x, y in zip(ta, tb, strict=True))


# ----------------------------------------------------------------------------
# Result types
# ----------------------------------------------------------------------------
@dataclass(frozen=True)
class Disagreement:
    """One point where the readings differ. ``subject`` is a reference, a
    ``ref.pad`` or a net name; ``a`` / ``b`` hold what each reading says
    (JSON-ready); ``field`` names the component field of a field_mismatch."""
    kind: str
    severity: str
    subject: str
    message: str
    a: Any = None
    b: Any = None
    field: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "severity": self.severity, "subject": self.subject,
                "field": self.field, "a": copy.deepcopy(self.a), "b": copy.deepcopy(self.b),
                "message": self.message}

    def sort_key(self) -> tuple:
        return (SEVERITIES.index(self.severity), KINDS.index(self.kind), _natural(self.subject))


@dataclass(frozen=True)
class CrosscheckResult:
    labels: tuple[str, str]
    disagreements: tuple[Disagreement, ...]
    scores: Mapping[str, float]
    score: float
    counts: Mapping[str, int]
    equivalence: Mapping[str, Any]          # net_equivalence result, as_dict()

    @property
    def agree(self) -> bool:
        return not self.disagreements

    def by_severity(self) -> dict[str, int]:
        found = Counter(d.severity for d in self.disagreements)
        return {s: found.get(s, 0) for s in SEVERITIES}

    def as_dict(self) -> dict[str, Any]:
        """JSON-ready report."""
        return {"labels": list(self.labels), "agree": self.agree,
                "verdict": "agree" if self.agree else "review", "score": self.score,
                "scores": dict(self.scores), "weights": dict(WEIGHTS), "counts": dict(self.counts),
                "by_severity": self.by_severity(),
                "disagreements": [d.as_dict() for d in self.disagreements],
                "equivalence": copy.deepcopy(dict(self.equivalence))}

    def summary(self) -> str:
        a, b = self.labels
        if self.agree:
            return (f"agree: {a} and {b} read the same {self.counts['common_refs']} parts and "
                    f"{self.counts['connected_pads']} connected pins (score {self.score})")
        sev = self.by_severity()
        return (f"REVIEW: {len(self.disagreements)} disagreement(s) between {a} and {b} "
                f"({sev['high']} high, {sev['med']} med, {sev['low']} low); score {self.score}")

    def markdown(self) -> str:
        """The disagreement list as a reviewer's Markdown table."""
        a, b = self.labels
        s, sev = self.scores, self.by_severity()
        found = (f"{self.counts['common_refs']} parts and {self.counts['connected_pads']} "
                 "connected pins read the same" if self.agree else
                 f"{len(self.disagreements)} disagreement(s): {sev['high']} high, "
                 f"{sev['med']} med, {sev['low']} low")
        lines = [f"# Vision cross-check: {a} vs {b}", "",
                 f"**Verdict: {'AGREE' if self.agree else 'REVIEW'}** - score {self.score:.4f} "
                 f"(components {s['components']:.4f}, fields {s['fields']:.4f}, "
                 f"connectivity {s['connectivity']:.4f}); {found}.", ""]
        if self.agree:
            return "\n".join(lines)
        lines += ["Settle each row against the drawing (overview + tiles), correct the reading "
                  "that is wrong, then re-run the cross-check.", "",
                  "| # | Severity | Kind | Subject | Detail |", "|---|---|---|---|---|"]
        lines += [f"| {i} | {d.severity} | {d.kind} | {_cell(d.subject)} | {_cell(d.message)} |"
                  for i, d in enumerate(self.disagreements, 1)]
        return "\n".join(lines) + "\n"


def _cell(text: str) -> str:
    return _PIPE.sub(r"\\|", text).replace("\n", " ")


# ----------------------------------------------------------------------------
# One reading, normalised
# ----------------------------------------------------------------------------
@dataclass(frozen=True)
class _Reading:
    label: str
    side: str                                   # "a" | "b": where its values go in a Disagreement
    comps: Mapping[str, Mapping[str, Any]]      # ref -> first component with it
    dups: Mapping[str, int]                     # ref -> count, refs listed 2+ times
    nets: Mapping[str, frozenset[PadKey]]       # net -> pads (NO_NET = no_connect)
    owner: Mapping[PadKey, int]                 # connected pad -> class index
    lone: Mapping[PadKey, str]                  # pad alone on a named net -> net


def _pad_nets(design: Any, label: str) -> dict[str, set[PadKey]]:
    if not isinstance(design, Mapping):
        raise ValueError(f"design {label} must be a JSON object (a netlist.json design)")
    try:
        return ne.netlist_pad_nets(design)
    except ValueError as e:
        raise ValueError(f"design {label}: {e}") from e


def _partition(nets: Mapping[str, Iterable[PadKey]]) -> tuple[dict[PadKey, int], dict[PadKey, str]]:
    """(connected pad -> class index, pad alone on a named net -> net name).
    Nets sharing a pad are one class, as compare() joins them."""
    parent: dict[PadKey, PadKey] = {}
    first_net: dict[PadKey, str] = {}

    def find(k: PadKey) -> PadKey:
        while parent.setdefault(k, k) != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k

    for name, keys in nets.items():
        if name == ne.NO_NET:
            continue
        keys = sorted(keys, key=_pad_order)
        for k in keys:
            first_net.setdefault(k, name)
            parent[find(k)] = find(keys[0])
    groups: dict[PadKey, list[PadKey]] = {}
    for k in parent:
        groups.setdefault(find(k), []).append(k)
    classes = sorted((sorted(g, key=_pad_order) for g in groups.values() if len(g) > 1),
                     key=lambda g: _pad_order(g[0]))
    owner = {k: i for i, g in enumerate(classes) for k in g}
    lone = {k: first_net[k] for g in groups.values() if len(g) == 1 for k in g}
    return owner, lone


def _reading(design: Mapping[str, Any], pads: Mapping[str, set[PadKey]], label: str, side: str,
             rename: Mapping[str, str]) -> _Reading:
    comps: dict[str, Mapping[str, Any]] = {}
    counts: Counter[str] = Counter()
    for i, c in enumerate(design.get("components", [])):
        if not isinstance(c, Mapping) or not isinstance(c.get("ref"), str) or not c["ref"]:
            raise ValueError(f"design {label}: components[{i}] has no 'ref'")
        ref = rename.get(c["ref"], c["ref"])
        counts[ref] += 1
        comps.setdefault(ref, c)
    nets = {name: frozenset((rename.get(r, r), p) for r, p in keys) for name, keys in pads.items()}
    owner, lone = _partition(nets)
    return _Reading(label, side, MappingProxyType(comps),
                    MappingProxyType({r: n for r, n in counts.items() if n > 1}),
                    MappingProxyType(nets), MappingProxyType(owner), MappingProxyType(lone))


# ----------------------------------------------------------------------------
# Disagreements
# ----------------------------------------------------------------------------
def _field_text(comp: Mapping[str, Any], name: str) -> str | None:
    value = comp.get(name)
    text = "" if value is None else str(value).strip()
    return text or None


def _part_nets(r: _Reading, ref: str) -> list[str]:
    return sorted((n for n, keys in r.nets.items()
                   if n != ne.NO_NET and any(k[0] == ref for k in keys)), key=_natural)


def _duplicates(r: _Reading) -> list[Disagreement]:
    return [Disagreement(DUPLICATE_REF, "high", ref,
                         f"{r.label} lists {ref} {n} times (the first entry is compared)",
                         **{r.side: n})
            for ref, n in sorted(r.dups.items(), key=lambda kv: _natural(kv[0]))]


def _only_in(r: _Reading, other: _Reading, kind: str) -> list[Disagreement]:
    out = []
    for ref in sorted(set(r.comps) - set(other.comps), key=_natural):
        c = r.comps[ref]
        summary = {k: c[k] for k in FIELDS if c.get(k) is not None}
        what = ", ".join(str(summary[k]) for k in ("lib_id", "value") if k in summary)
        nets = _part_nets(r, ref)
        wired = f"; {r.label} connects it to {', '.join(nets)}" if nets else ""
        out.append(Disagreement(kind, "high", ref,
                                f"{ref} ({what or 'no lib_id/value'}) is in {r.label} only{wired}",
                                **{r.side: summary}))
    return out


def _fields(ra: _Reading, rb: _Reading) -> tuple[list[Disagreement], int, int]:
    """Field mismatches of shared parts, fields compared, fields agreeing."""
    out: list[Disagreement] = []
    compared = agreeing = 0
    for ref in sorted(set(ra.comps) & set(rb.comps), key=_natural):
        for name in FIELDS:
            va, vb = _field_text(ra.comps[ref], name), _field_text(rb.comps[ref], name)
            if va is None and vb is None:
                continue
            compared += 1
            if va is not None and vb is not None and (
                    values_agree(va, vb) if name == "value" else va == vb):
                agreeing += 1
                continue
            said_a = f"reads {va!r}" if va is not None else "has none"
            said_b = f"reads {vb!r}" if vb is not None else "has none"
            out.append(Disagreement(FIELD_MISMATCH, FIELD_SEVERITY[name], ref,
                                    f"{ref} {name}: {ra.label} {said_a}, {rb.label} {said_b}",
                                    a=va, b=vb, field=name))
    return out, compared, agreeing


def _parts(parts: Iterable[ne.NetPart]) -> str:
    return " | ".join(f"{p.net or '(no net)'}: {_fmt(p.pads)}" for p in parts)


def _connectivity(eq: ne.EquivalenceResult, ra: _Reading, rb: _Reading,
                  gone_a: set[str], gone_b: set[str]) -> list[Disagreement]:
    la, lb = ra.label, rb.label
    out = [Disagreement(NET_SPLIT, "high", d.net,
                        f"{la} net {d.net} is split in {lb}: {_parts(d.parts)}",
                        a={"net": d.net, "pads": [list(k) for k in d.pads]},
                        b=[p.as_dict() for p in d.parts]) for d in eq.split_nets]
    out += [Disagreement(NET_MERGE, "high", d.net,
                         f"{lb} net {d.net} joins pins {la} keeps on separate nets: "
                         f"{_parts(d.parts)}",
                         a=[p.as_dict() for p in d.parts],
                         b={"net": d.net, "pads": [list(k) for k in d.pads]})
            for d in eq.merged_nets]
    details = eq.details
    for kind, by_net, gone, has, lacks in (
            (PIN_MISSING, details["missing_by_net"], gone_a, la, lb),
            (PIN_EXTRA, details["extra_by_net"], gone_b, lb, la)):
        for net, pads in by_net.items():
            keep = [list(p) for p in pads if p[0] not in gone]     # a missing part says it once
            if keep:
                side = {"a": keep} if kind == PIN_MISSING else {"b": keep}
                out.append(Disagreement(kind, "high", net or "(no net)",
                                        f"{has} connects {_fmt(keep)} on net {net}; {lacks} has "
                                        "no such pin", **side))
    for key, label, side in (("pad_conflicts_a", la, "a"), ("pad_conflicts_b", lb, "b")):
        for c in details[key]:
            ref, pad = c["pad"]
            out.append(Disagreement(PAD_CONFLICT, "high", f"{ref}.{pad}",
                                    f"{label} puts {ref}.{pad} on {len(c['nets'])} nets: "
                                    f"{', '.join(c['nets'])}", **{side: list(c["nets"])}))
    return out


def _no_connects(r: _Reading, other: _Reading, gone: set[str]) -> list[Disagreement]:
    """Pins ``r`` marks no-connect that ``other`` neither marks nor wires (a
    pin ``other`` wires to something is already a split/merge). ``gone``:
    refs ``other`` lacks altogether (reported as a missing part)."""
    out = []
    marked_other = other.nets.get(ne.NO_NET, frozenset())
    for key in sorted(r.nets.get(ne.NO_NET, frozenset()), key=_pad_order):
        if key in marked_other or key in other.owner or key[0] in gone:
            continue
        state = (f"has it alone on net {other.lone[key]}" if key in other.lone
                 else "does not list the pin")
        ref, pad = key
        out.append(Disagreement(NO_CONNECT_MISMATCH, "low", f"{ref}.{pad}",
                                f"{r.label} marks {ref}.{pad} no-connect; {other.label} {state}",
                                **{r.side: "no_connect", other.side: other.lone.get(key)}))
    return out


def _pin_agreement(ra: _Reading, rb: _Reading) -> tuple[int, int]:
    """(pins on the corresponding net, pins connected in either reading).
    Nets correspond one-to-one by greatest pin overlap (greedy, ties by
    class order), so one misplaced pin costs one pin, not two whole nets."""
    overlap = Counter((ra.owner[k], rb.owner[k]) for k in ra.owner if k in rb.owner)
    match: dict[int, int] = {}
    taken: set[int] = set()
    for (ia, ib), _count in sorted(overlap.items(), key=lambda kv: (-kv[1], kv[0])):
        if ia not in match and ib not in taken:
            match[ia] = ib
            taken.add(ib)
    union = set(ra.owner) | set(rb.owner)
    agreeing = sum(1 for k in union
                   if k in ra.owner and k in rb.owner and match.get(ra.owner[k]) == rb.owner[k])
    return agreeing, len(union)


def _fields_score(ra: _Reading, rb: _Reading, compared: int, agreeing: int) -> float:
    """Agreeing / compared field values; with nothing compared, 1.0 when the
    readings share parts (none has a field) or both are empty, else 0.0 (parts
    exist but none is shared: no field can agree)."""
    if compared:
        return agreeing / compared
    shared = set(ra.comps) & set(rb.comps)
    return 1.0 if shared or not (ra.comps or rb.comps) else 0.0


def _labels(labels: Iterable[str]) -> tuple[str, str]:
    pair = tuple(str(x) for x in labels)
    if len(pair) != 2 or not all(pair) or pair[0] == pair[1]:
        raise ValueError(f"labels must be two different non-empty names, got {labels!r}")
    return pair[0], pair[1]


def crosscheck(a: Mapping[str, Any], b: Mapping[str, Any], *,
               ref_map: Mapping[str, str] | None = None,
               labels: Iterable[str] = ("A", "B")) -> CrosscheckResult:
    """Compare two netlist.json readings of one schematic; see the module doc.
    ValueError on a malformed design (named by its label) or a ref_map that
    is not one-to-one; TypeError on a ref_map that is not a mapping."""
    la, lb = _labels(labels)
    pads_a, pads_b = _pad_nets(a, la), _pad_nets(b, lb)
    eq = ne.compare(pads_a, pads_b, ref_map)              # validates ref_map first
    rename = {str(k): str(v) for k, v in (ref_map or {}).items()}
    ra, rb = _reading(a, pads_a, la, "a", rename), _reading(b, pads_b, lb, "b", {})
    gone_a, gone_b = set(ra.comps) - set(rb.comps), set(rb.comps) - set(ra.comps)
    field_out, compared, agreeing = _fields(ra, rb)
    found = (_duplicates(ra) + _duplicates(rb) + _only_in(ra, rb, COMPONENT_MISSING)
             + _only_in(rb, ra, COMPONENT_EXTRA) + field_out
             + _connectivity(eq, ra, rb, gone_a, gone_b)
             + _no_connects(ra, rb, gone_a) + _no_connects(rb, ra, gone_b))
    pins_ok, pins = _pin_agreement(ra, rb)
    union = set(ra.comps) | set(rb.comps)
    scores = {"components": len(set(ra.comps) & set(rb.comps)) / len(union) if union else 1.0,
              "fields": _fields_score(ra, rb, compared, agreeing),
              "connectivity": pins_ok / pins if pins else 1.0}
    score = round(sum(WEIGHTS[k] * v for k, v in scores.items()), 4)
    counts = {"components_a": len(ra.comps), "components_b": len(rb.comps),
              "common_refs": len(set(ra.comps) & set(rb.comps)),
              "fields_compared": compared, "fields_agreeing": agreeing,
              "nets_a": eq.details["nets_a"], "nets_b": eq.details["nets_b"],
              "connected_pads": pins, "pads_agreeing": pins_ok}
    return CrosscheckResult((la, lb), tuple(sorted(found, key=Disagreement.sort_key)),
                            MappingProxyType({k: round(v, 4) for k, v in scores.items()}), score,
                            MappingProxyType(counts), MappingProxyType(eq.as_dict()))


# ----------------------------------------------------------------------------
# Pin check: one reading against its KiCad symbols
# ----------------------------------------------------------------------------
@dataclass(frozen=True)
class PinReport:
    # parts: {ref, lib_id, unknown: [pad], unlisted: [pin], nc_pins: [pin]}; nc_pins (symbol
    # pins of electrical type no_connect the reading leaves out) are informational
    parts: tuple[dict[str, Any], ...]
    symbols: Mapping[str, list[dict[str, str]]]     # lib_id -> [{number, name, type}]
    unresolved: tuple[dict[str, str], ...]  # {ref, lib_id, reason}

    @property
    def issues(self) -> int:
        return (sum(len(p["unknown"]) + len(p["unlisted"]) for p in self.parts)
                + len(self.unresolved))

    @property
    def ok(self) -> bool:
        return self.issues == 0

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "issues": self.issues, "parts": copy.deepcopy(list(self.parts)),
                "unresolved": copy.deepcopy(list(self.unresolved)),
                "symbols": copy.deepcopy(dict(self.symbols))}

    def _nc_note(self) -> str:
        count = sum(len(p.get("nc_pins", ())) for p in self.parts)
        return (f"{count} NC-type pin(s) left unconnected by design (symbol pin type "
                f"{NC_PIN_TYPE}; not an issue)") if count else ""

    def summary(self) -> str:
        note = self._nc_note()
        if self.ok:
            return (f"pins ok: {len(self.parts)} part(s) checked against {len(self.symbols)} "
                    f"symbol(s)" + (f"; {note}" if note else ""))
        lines = [f"pin check: {self.issues} issue(s)"]
        for p in self.parts:
            lines += [f"  {p['ref']}.{pad}: not a pin of {p['lib_id']}" for pad in p["unknown"]]
            lines += [f"  {p['ref']}.{pin['number']} ({pin['name']}, {pin['type']}): in no net "
                      "and not no_connect" for pin in p["unlisted"]]
        lines += [f"  {u['ref']}: {u['lib_id'] or '(no lib_id)'} does not resolve: {u['reason']}"
                  for u in self.unresolved]
        return "\n".join(lines + ([f"  ({note})"] if note else []))


def _stock_symbols_dir() -> Path | None:
    """The newest KiCad's stock symbols (what the schematic builders use)."""
    if str(SERVER_DIR) not in sys.path:
        sys.path.insert(0, str(SERVER_DIR))
    import kicad_env as env
    return env.symbols_dir(env.pick_cli())


def _pin_table(flat: list) -> list[dict[str, str]]:
    import symlib
    pins: dict[str, dict[str, str]] = {}
    for unit in symlib.kids(flat, "symbol"):
        for pin in symlib.kids(unit, "pin"):
            number, name = symlib.first(pin, "number"), symlib.first(pin, "name")
            if number is None:
                continue
            pins.setdefault(str(number[1]), {"number": str(number[1]),
                                             "name": str(name[1]) if name is not None else "",
                                             "type": str(pin[1])})
    return sorted(pins.values(), key=lambda p: _natural(p["number"]))


class _SymbolLibs:
    """lib_id -> pin table through the D1 resolver; the stock library is
    looked up (kicad_env) only when a nickname is not declared and no
    ``symbols_dir`` was given."""

    def __init__(self, design: Mapping[str, Any], netlist_dir: Path,
                 symbols_dir: str | Path | None) -> None:
        import libtable
        if symbols_dir is not None and not Path(symbols_dir).is_dir():
            raise ValueError(f"symbols_dir does not exist: {symbols_dir}")
        self._libtable = libtable
        self._design, self._dir = design, netlist_dir
        self._auto_stock = symbols_dir is None
        self._resolver = libtable.LibraryResolver.for_design(netlist_dir, design, symbols_dir, None)
        self._cache: dict = {}

    def _path(self, nick: str) -> Path:
        if self._auto_stock and self._resolver.resolve(self._libtable.SYMBOL, nick) is None:
            self._auto_stock = False
            stock = _stock_symbols_dir()
            if stock is not None:
                self._resolver = self._libtable.LibraryResolver.for_design(
                    self._dir, self._design, stock, None)
        return self._resolver.symbol_lib_path(nick)

    def pins(self, lib_id: str) -> list[dict[str, str]]:
        import symlib
        nick, _, name = lib_id.partition(":")
        if not nick or not name:
            raise LookupError(f"lib_id {lib_id!r} is not 'Library:Symbol'")
        if nick not in self._cache:
            path = self._path(nick)
            self._cache[nick] = symlib.load_lib(path.parent, path.stem)
        return _pin_table(symlib.flatten(self._cache, None, nick, name))


def _part_row(ref: str, lib_id: str, table: list[dict[str, str]],
              mine: set[str]) -> dict[str, Any]:
    """One part's pin check: pads that are no pin of the symbol, symbol pins
    the reading never mentions (an issue) and, apart, the unmentioned pins
    of electrical type no_connect (unconnected by design, informational)."""
    numbers = {p["number"] for p in table}
    unread = [dict(p) for p in table if p["number"] not in mine]
    return {"ref": ref, "lib_id": lib_id, "unknown": sorted(mine - numbers, key=_natural),
            "unlisted": [p for p in unread if p["type"] != NC_PIN_TYPE],
            "nc_pins": [p for p in unread if p["type"] == NC_PIN_TYPE]}


def pin_check(design: Mapping[str, Any], *, netlist_dir: str | Path | None = None,
              symbols_dir: str | Path | None = None) -> PinReport:
    """Check every (ref, pad) of a reading against its symbol's pin numbers
    and list the symbol pins it never mentions (pins of electrical type
    no_connect apart, as ``nc_pins``: not an issue). ``netlist_dir``
    (default: the working directory) anchors the ``libraries`` block and
    project tables; ``symbols_dir`` is the stock library (default: the newest
    KiCad's). Board-only parts are skipped. ValueError on a malformed design
    or block."""
    import symlib
    pads = ne.netlist_pad_nets(design)
    used: dict[str, set[str]] = {}
    for keys in pads.values():
        for ref, pad in keys:
            used.setdefault(ref, set()).add(pad)
    libs = _SymbolLibs(design, Path(netlist_dir) if netlist_dir else Path.cwd(), symbols_dir)
    symbols: dict[str, list[dict[str, str]]] = {}
    parts: list[dict[str, Any]] = []
    unresolved: list[dict[str, str]] = []
    for c in design.get("components", []):
        if not isinstance(c, Mapping) or c.get("board_only") is True:
            continue
        ref, lib_id = str(c.get("ref", "")), str(c.get("lib_id") or "")
        try:
            table = symbols[lib_id] if lib_id in symbols else libs.pins(lib_id)
        except (LookupError, symlib.SymbolLibError) as e:
            unresolved.append({"ref": ref, "lib_id": lib_id, "reason": str(e)})
            continue
        symbols[lib_id] = table
        parts.append(_part_row(ref, lib_id, table, used.get(ref, set())))
    return PinReport(tuple(parts), MappingProxyType(symbols), tuple(unresolved))


# ----------------------------------------------------------------------------
# File-level helpers (the CLI and the MCP layer)
# ----------------------------------------------------------------------------
def load_design(path: str | Path) -> dict[str, Any]:
    """A netlist.json file as a dict; OSError when unreadable, ValueError when
    it is not a JSON object."""
    p = Path(path)
    try:
        data = json.loads(p.read_text(encoding="utf-8-sig"))
    except ValueError as e:
        raise ValueError(f"{p}: not valid JSON: {e}") from e
    if not isinstance(data, dict):
        raise ValueError(f"{p}: not a JSON object (a netlist.json design)")
    return data


def load_ref_map(raw: Mapping[str, str] | str | os.PathLike | None) -> Mapping[str, str] | None:
    """A ref_map {ref in A: ref in B} given as a mapping, inline JSON text
    (starting with '{' or '[') or the path of a JSON file; None or blank text
    = no map. TypeError on another type, ValueError when it is not a JSON
    object, OSError when the file cannot be read."""
    if raw is None or isinstance(raw, Mapping):
        return raw
    if not isinstance(raw, (str, os.PathLike)):
        raise TypeError(f"ref_map must be a mapping, JSON text or a JSON file path, "
                        f"got {type(raw).__name__}")
    text = raw.strip() if isinstance(raw, str) else os.fspath(raw)
    if not text:
        return None
    if not isinstance(raw, str) or text[0] not in "{[":
        text = Path(text).read_text(encoding="utf-8-sig")
    try:
        data = json.loads(text)
    except ValueError as e:
        raise ValueError(f"ref_map (--ref-map) {raw!s}: not valid JSON: {e}") from e
    if not isinstance(data, dict):
        raise ValueError(f"ref_map (--ref-map) must be a JSON object {{ref_in_a: ref_in_b}}, "
                         f"got {raw!s}")
    return data


def crosscheck_files(a_path: str | Path, b_path: str | Path,
                     ref_map: Mapping[str, str] | str | os.PathLike | None = None,
                     labels: Iterable[str] = ("A", "B")) -> CrosscheckResult:
    """crosscheck() of two netlist.json files; ``ref_map`` as load_ref_map
    takes it. The result gives ``as_dict()``, ``markdown()`` and ``agree``.
    Raises OSError (a file), ValueError (JSON, design, ref_map, labels) and
    TypeError (ref_map type)."""
    rename = load_ref_map(ref_map)
    return crosscheck(load_design(a_path), load_design(b_path), ref_map=rename, labels=labels)


def pin_check_file(netlist_path: str | Path, *,
                   symbols_dir: str | Path | None = None) -> dict[str, Any]:
    """pin_check() of a netlist.json file, its ``libraries`` block and
    project tables anchored at the file's directory. Returns the JSON-ready
    report (``ok`` = no pin issue, ``issues``, ``parts``, ``unresolved``,
    ``symbols``) plus ``netlist`` (resolved path) and ``summary`` (text).
    Raises OSError / ValueError like load_design."""
    path = Path(netlist_path).resolve()
    rep = pin_check(load_design(path), netlist_dir=path.parent, symbols_dir=symbols_dir)
    return {"netlist": str(path), **rep.as_dict(), "summary": rep.summary()}


# ----------------------------------------------------------------------------
# Command line
# ----------------------------------------------------------------------------
def _write(path: str | None, text: str) -> None:
    if path:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(text, encoding="utf-8")


def _cli_compare(args: argparse.Namespace) -> int:
    res = crosscheck_files(args.a, args.b, args.ref_map, (args.label_a, args.label_b))
    report = json.dumps(res.as_dict(), indent=1)
    _write(args.out, report)
    _write(args.md, res.markdown())
    print(report if args.json else res.markdown())
    return 0 if res.agree else 1


def _cli_pins(args: argparse.Namespace) -> int:
    out = pin_check_file(args.netlist, symbols_dir=args.symbols_dir)
    print(json.dumps(out, indent=1) if args.json else out["summary"])
    return 0 if out["ok"] else 1


def main(argv: list[str] | None = None) -> int:
    """Exit 0 agree / clean, 1 disagreements / pin issues, 2 error."""
    ap = argparse.ArgumentParser(description="Vision-path checks of netlist.json readings.")
    sub = ap.add_subparsers(dest="command", required=True)
    cmp_ = sub.add_parser("compare", help="cross-check two independent readings")
    cmp_.add_argument("a", help="first reading (netlist.json)")
    cmp_.add_argument("b", help="second reading (netlist.json)")
    cmp_.add_argument("--ref-map", help="JSON {ref_in_a: ref_in_b}, inline or a file path")
    cmp_.add_argument("--label-a", default="A", help="name of the first reading")
    cmp_.add_argument("--label-b", default="B", help="name of the second reading")
    cmp_.add_argument("--json", action="store_true", help="print the JSON report")
    cmp_.add_argument("--out", help="also write the JSON report here")
    cmp_.add_argument("--md", help="also write the Markdown report here")
    pins = sub.add_parser("pins", help="check a reading's pins against its KiCad symbols")
    pins.add_argument("netlist", help="the reading (netlist.json)")
    pins.add_argument("--symbols-dir", help="stock .kicad_sym directory (default: newest KiCad)")
    pins.add_argument("--json", action="store_true", help="print the JSON report")
    args = ap.parse_args(argv)
    try:
        return _cli_compare(args) if args.command == "compare" else _cli_pins(args)
    except (OSError, ValueError, TypeError, RuntimeError) as e:
        print(f"vision_crosscheck: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
