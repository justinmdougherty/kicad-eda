"""Materials library: Dk/Df of PCB laminates, prepregs and solder masks (ADR D7.3).

Data: ``server/data/materials/*.json`` (schema ``kicad-eda/materials@1``). One
material::

    "rogers.ro4350b": {"vendor", "product", "family", "category", "forms", "ptfe",
        "dk": [{"value": 3.48, "freq_hz": 1e10, "basis": "process", "source": "<url>",
                "when": {"thickness_mm": 0.101}, "tol", "method", "note"}, ...],
        "df": [...], "sources": [{"title", "url"}], ...}

* ``basis`` is design | process | typical | fab; a fab entry names its ``fab``
  and applies only when that fab is chosen.
* ``freq_hz`` is a number or a ``[lo, hi]`` band.
* ``when`` narrows an entry: ``construction`` (glass style or "core"),
  ``thickness_mm`` (number, matched within 5 um, or a half-open ``[lo, hi)``
  range), ``rc_pct`` (resin content, within 1 %), ``fab``.
* Every value carries a ``source`` URL; an untraced value is ``typical`` with
  ``"untraced": true`` and a note saying so.
* ``alt: true`` marks a conflicting published value: never selected, reported
  as ST-W8 when it is more than 2 % from the value used.
* ``valid_thickness_mm: [lo, hi]`` (RO4350B design Dk) marks the thicknesses
  an average Dk was characterised on; a thinner core raises ST-W5.

Resolution (``resolve_dk`` / ``resolve_df``), D7.3 order: explicit value, then
``fab`` (the chosen fab's own numbers), ``design``, ``process``, ``typical``.
``policy`` picks the first step (``auto`` = fab when the fab controls impedance
for this build, else design); steps after it follow the order, the skipped
earlier steps come last. Within a step the most specific ``when`` wins; values
move between frequencies only by log-frequency interpolation between published
points. A point value covers half a decade either side of its frequency, a
band only itself; beyond that the nearest value is used with an ST-W6 info.

Stdlib only and Python 3.11-compatible: KiCad's bundled Python imports it.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

SCHEMA = "kicad-eda/materials@1"
DEFAULT_DIR = Path(__file__).resolve().parents[1] / "data" / "materials"
BASES = ("design", "process", "typical", "fab")
ORDER = ("fab", "design", "process", "typical")          # D7.3 resolution order
POLICIES = ("auto",) + ORDER
CATEGORIES = ("fr4", "high-speed", "rf-laminate", "mask")
WHEN_KEYS = frozenset({"construction", "thickness_mm", "rc_pct", "fab"})
POINT_HALF_BAND_DECADES = 0.5        # a point value covers f0 / 10**0.5 .. f0 * 10**0.5
THICKNESS_TOL_MM = 0.005
RC_TOL_PCT = 1.0
CONFLICT_FRACTION = 0.02             # ST-W8: alt source more than 2 % away
_REL = 1e-9


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ValueWarning:
    """One lint-grade note about a resolved value (ST-W5 / ST-W6 / ST-W8)."""
    id: str
    severity: str                    # "warning" | "info"
    message: str

    def __str__(self) -> str:
        return f"{self.id}: {self.message}"

    def to_dict(self) -> dict[str, str]:
        return {"id": self.id, "severity": self.severity, "message": self.message}


@dataclass(frozen=True)
class ResolvedValue:
    """A Dk or Df with its provenance: basis (explicit | fab | design | process |
    typical), the frequency it is characterised at, and its source URL."""
    value: float
    basis: str
    freq_hz: float
    source: str | None
    warnings: tuple[ValueWarning, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {"value": self.value, "basis": self.basis, "freq_hz": self.freq_hz,
                "source": self.source, "warnings": [w.to_dict() for w in self.warnings]}


@dataclass(frozen=True, eq=False)
class MaterialLibrary:
    """The loaded library. ``data_version`` = sha256 over the data files sorted
    by name (name + NUL + bytes + NUL each); it feeds the route cache."""
    materials: Mapping[str, Mapping[str, Any]]
    files: tuple[str, ...]
    data_version: str

    def get(self, material_id: str) -> dict[str, Any] | None:
        """A deep copy of one material (None when unknown)."""
        m = self.materials.get(material_id)
        return copy.deepcopy(dict(m)) if m is not None else None

    def ids(self) -> tuple[str, ...]:
        return tuple(sorted(self.materials))

    def __contains__(self, material_id: object) -> bool:
        return material_id in self.materials


# ---------------------------------------------------------------------------
# Loading and validation
# ---------------------------------------------------------------------------
def _is_number(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _check_freq(f: Any) -> bool:
    if isinstance(f, list):
        return len(f) == 2 and all(_is_number(v) for v in f) and 0 < f[0] < f[1]
    return _is_number(f) and f > 0


def _check_when(when: Any) -> str | None:
    if not isinstance(when, dict) or not set(when) <= WHEN_KEYS:
        return f"when keys must be among {sorted(WHEN_KEYS)}"
    t = when.get("thickness_mm")
    if t is not None and not (_is_number(t) or (isinstance(t, list) and len(t) == 2
                                                and all(_is_number(v) for v in t))):
        return "when.thickness_mm must be a number or [lo, hi]"
    if "rc_pct" in when and not _is_number(when["rc_pct"]):
        return "when.rc_pct must be a number"
    return None


def _entry_problem(e: Any) -> str | None:
    if not isinstance(e, dict):
        return "entry must be an object"
    if not _is_number(e.get("value")) or e["value"] <= 0:
        return "value must be a positive number"
    if e.get("basis") not in BASES:
        return f"basis must be one of {BASES}"
    if "freq_hz" not in e or not _check_freq(e["freq_hz"]):
        return "freq_hz must be a positive number or a [lo, hi] band"
    if e["basis"] == "fab" and not isinstance(e.get("fab"), str):
        return "a fab entry needs \"fab\""
    if "when" in e:
        problem = _check_when(e["when"])
        if problem:
            return problem
    if e.get("untraced"):
        if e["basis"] != "typical" or "untraced" not in str(e.get("note", "")):
            return "an untraced value must be typical and say 'untraced' in its note"
    elif not str(e.get("source", "")).startswith("https://"):
        return "source must be an https URL (or mark the value untraced)"
    return None


def _validate_material(fname: str, mid: str, m: Any) -> None:
    if not isinstance(m, dict):
        raise ValueError(f"{fname}: {mid}: material must be an object")
    if m.get("category") not in CATEGORIES:
        raise ValueError(f"{fname}: {mid}: category must be one of {CATEGORIES}")
    if not isinstance(m.get("dk"), list) or not m["dk"]:
        raise ValueError(f"{fname}: {mid}: dk must be a non-empty list")
    if not isinstance(m.get("df", []), list):
        raise ValueError(f"{fname}: {mid}: df must be a list")
    for prop in ("dk", "df"):
        for i, e in enumerate(m.get(prop, [])):
            problem = _entry_problem(e)
            if problem:
                raise ValueError(f"{fname}: {mid}.{prop}[{i}]: {problem}")


def load_library(root: str | Path | None = None) -> MaterialLibrary:
    """Load every ``*.json`` under ``root`` (default server/data/materials).
    Raises ValueError on a wrong schema, a malformed entry or a duplicate id."""
    base = Path(root) if root is not None else DEFAULT_DIR
    files = sorted(base.glob("*.json"), key=lambda p: p.name)
    if not files:
        raise ValueError(f"no materials files under {base}")
    digest = hashlib.sha256()
    found: dict[str, Mapping[str, Any]] = {}
    origin: dict[str, str] = {}
    for path in files:
        raw = path.read_bytes()
        digest.update(path.name.encode("utf-8") + b"\0")
        digest.update(raw + b"\0")
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict) or data.get("schema") != SCHEMA:
            raise ValueError(f"{path.name}: schema must be {SCHEMA!r}")
        mats = data.get("materials")
        if not isinstance(mats, dict):
            raise ValueError(f"{path.name}: \"materials\" must be an object")
        for mid, m in mats.items():
            if mid in found:
                raise ValueError(f"duplicate material id {mid!r} in {path.name} "
                                 f"and {origin[mid]}")
            _validate_material(path.name, mid, m)
            found[mid] = m
            origin[mid] = path.name
    return MaterialLibrary(MappingProxyType(found), tuple(str(p) for p in files),
                           digest.hexdigest())


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------
def _matches(key: str, want: Any, got: Any) -> bool:
    if got is None:
        return False
    if key == "construction":
        return str(got).strip().lower() == str(want).strip().lower()
    if key == "thickness_mm":
        if isinstance(want, list):
            return want[0] - _REL <= got < want[1] - _REL
        return abs(got - want) <= THICKNESS_TOL_MM
    if key == "rc_pct":
        return abs(got - want) <= RC_TOL_PCT
    return got == want


def _applies(e: Mapping[str, Any], req: Mapping[str, Any]) -> bool:
    if e["basis"] == "fab" and e.get("fab") != req.get("fab"):
        return False
    return all(_matches(k, v, req.get(k)) for k, v in e.get("when", {}).items())


def _steps(start: str) -> tuple[str, ...]:
    i = ORDER.index(start)
    return ORDER[i:] + tuple(reversed(ORDER[:i]))


def _span(e: Mapping[str, Any]) -> tuple[float, float]:
    f = e["freq_hz"]
    return (float(f[0]), float(f[1])) if isinstance(f, list) else (float(f), float(f))


@dataclass(frozen=True)
class _Pick:
    value: float
    freq_hz: float
    source: str | None
    used: tuple[Mapping[str, Any], ...]
    out_of_band: bool


def _nearest(e: Mapping[str, Any], freq: float, edge: float) -> _Pick:
    lo, hi = _span(e)
    covered = lo == hi and abs(math.log10(freq / lo)) <= POINT_HALF_BAND_DECADES + _REL
    return _Pick(float(e["value"]), edge, e.get("source"), (e,), not covered)


def _at_frequency(group: list[Mapping[str, Any]], freq: float) -> _Pick:
    """Value of the most specific entries at ``freq``: in band, else log-frequency
    interpolation between the neighbours, else the nearest value."""
    for e in group:
        lo, hi = _span(e)
        if lo * (1 - _REL) <= freq <= hi * (1 + _REL):
            return _Pick(float(e["value"]), freq, e.get("source"), (e,), False)
    below = [e for e in group if _span(e)[1] < freq]
    above = [e for e in group if _span(e)[0] > freq]
    b = max(below, key=lambda e: _span(e)[1]) if below else None
    a = min(above, key=lambda e: _span(e)[0]) if above else None
    if b is not None and a is not None:
        fb, fa = _span(b)[1], _span(a)[0]
        t = math.log(freq / fb) / math.log(fa / fb)
        value = float(b["value"]) + (float(a["value"]) - float(b["value"])) * t
        sources = [s for s in (b.get("source"), a.get("source")) if s]
        source = " ; ".join(dict.fromkeys(sources)) or None
        return _Pick(value, freq, source, (b, a), False)
    if b is not None:
        return _nearest(b, freq, _span(b)[1])
    assert a is not None
    return _nearest(a, freq, _span(a)[0])


# ---------------------------------------------------------------------------
# Warnings
# ---------------------------------------------------------------------------
def _band_text(e: Mapping[str, Any]) -> str:
    lo, hi = _span(e)
    return f"{lo:.3g} Hz" if lo == hi else f"{lo:.3g}-{hi:.3g} Hz"


def _thin_core(mid: str, m: Mapping[str, Any], pick: _Pick, basis: str,
               thickness: float | None) -> list[ValueWarning]:
    if thickness is None:
        return []
    out: list[ValueWarning] = []
    for e in pick.used:
        valid = e.get("valid_thickness_mm")
        if not valid or thickness >= valid[0] - THICKNESS_TOL_MM:
            continue
        thin = [f"{x['when']['thickness_mm']} mm process Dk {x['value']:g}" for x in m["dk"]
                if x["basis"] == "process" and _is_number(x.get("when", {}).get("thickness_mm"))]
        detail = f" ({'; '.join(thin)})" if thin else ""
        out.append(ValueWarning("ST-W5", "warning", (
            f"{mid}: {basis} Dk {pick.value:g} is an average on {valid[0]:g}-{valid[1]:g} mm "
            f"cores; on this {thickness:g} mm core the Dk is lower{detail}; the design Dk "
            "drops by about 0.1 on thin cores")))
    return out


def _conflicts(mid: str, m_entries: list[Mapping[str, Any]], basis: str, pick: _Pick,
               req: Mapping[str, Any]) -> list[ValueWarning]:
    out: list[ValueWarning] = []
    for e in m_entries:
        if not e.get("alt") or e["basis"] != basis or not _applies(e, req):
            continue
        apart = abs(float(e["value"]) - pick.value) / pick.value
        if apart > CONFLICT_FRACTION:
            out.append(ValueWarning("ST-W8", "warning", (
                f"{mid}: conflicting {basis} Dk sources: {pick.value:g} ({pick.source}) vs "
                f"{e['value']:g} ({e.get('source')}), {100 * apart:.1f} % apart")))
    return out


def _warnings(prop: str, mid: str, m: Mapping[str, Any], basis: str, pick: _Pick,
              freq: float, req: Mapping[str, Any]) -> tuple[ValueWarning, ...]:
    if prop != "dk":
        return ()
    out = _thin_core(mid, m, pick, basis, req.get("thickness_mm"))
    if pick.out_of_band:
        out.append(ValueWarning("ST-W6", "info", (
            f"{mid}: {basis} Dk {pick.value:g} is characterised at {_band_text(pick.used[0])}; "
            f"used at {freq:.3g} Hz (nearest value, out of its band)")))
    return tuple(out + _conflicts(mid, m[prop], basis, pick, req))


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------
def _resolve(prop: str, lib: MaterialLibrary, material_id: str | None, *, freq_hz: float,
             policy: str, fab: str | None, construction: str | None,
             thickness_mm: float | None, rc_pct: float | None, fab_controls_impedance: bool,
             explicit: float | None, explicit_freq_hz: float | None) -> ResolvedValue:
    if not _is_number(freq_hz) or freq_hz <= 0:
        raise ValueError(f"freq_hz must be a positive number, got {freq_hz!r}")
    if policy not in POLICIES:
        raise ValueError(f"policy must be one of {POLICIES}, got {policy!r}")
    if explicit is not None:
        if not _is_number(explicit) or explicit <= 0:
            raise ValueError(f"explicit {prop} must be a positive number, got {explicit!r}")
        at = freq_hz if explicit_freq_hz is None else explicit_freq_hz
        if not _is_number(at) or at <= 0:
            raise ValueError(f"explicit_freq_hz must be a positive number, got {at!r}")
        return ResolvedValue(float(explicit), "explicit", float(at), "explicit")
    m = lib.materials.get(material_id or "")
    if m is None:
        raise LookupError(f"unknown material {material_id!r}: give an explicit {prop}")
    entries = list(m.get(prop, []))
    if not entries:
        raise LookupError(f"material {material_id!r} has no {prop} data")
    start = ("fab" if fab_controls_impedance else "design") if policy == "auto" else policy
    req = {"fab": fab, "construction": construction, "thickness_mm": thickness_mm,
           "rc_pct": rc_pct}
    for basis in _steps(start):
        cands = [e for e in entries if e["basis"] == basis and not e.get("alt")
                 and _applies(e, req)]
        if not cands:
            continue
        spec = max(len(e.get("when", {})) for e in cands)
        pick = _at_frequency([e for e in cands if len(e.get("when", {})) == spec], freq_hz)
        return ResolvedValue(pick.value, basis, pick.freq_hz, pick.source,
                             _warnings(prop, material_id or "", m, basis, pick, freq_hz, req))
    raise LookupError(f"material {material_id!r}: no {prop} entry applies "
                      f"(fab={fab!r}, construction={construction!r})")


def resolve_dk(lib: MaterialLibrary, material_id: str | None, *, freq_hz: float,
               policy: str = "auto", fab: str | None = None, construction: str | None = None,
               thickness_mm: float | None = None, rc_pct: float | None = None,
               fab_controls_impedance: bool = False, explicit: float | None = None,
               explicit_freq_hz: float | None = None) -> ResolvedValue:
    """The Dk of ``material_id`` at ``freq_hz`` (ADR D7.3 order; module docstring).
    An ``explicit`` value always wins (basis "explicit"); an unknown material
    without one raises LookupError (lint ST-E4)."""
    return _resolve("dk", lib, material_id, freq_hz=freq_hz, policy=policy, fab=fab,
                    construction=construction, thickness_mm=thickness_mm, rc_pct=rc_pct,
                    fab_controls_impedance=fab_controls_impedance, explicit=explicit,
                    explicit_freq_hz=explicit_freq_hz)


def resolve_df(lib: MaterialLibrary, material_id: str | None, *, freq_hz: float,
               policy: str = "auto", fab: str | None = None, construction: str | None = None,
               thickness_mm: float | None = None, rc_pct: float | None = None,
               fab_controls_impedance: bool = False, explicit: float | None = None,
               explicit_freq_hz: float | None = None) -> ResolvedValue:
    """The loss tangent of ``material_id`` at ``freq_hz``; same order as resolve_dk
    (no ST-W5/W6/W8: those are Dk warnings)."""
    return _resolve("df", lib, material_id, freq_hz=freq_hz, policy=policy, fab=fab,
                    construction=construction, thickness_mm=thickness_mm, rc_pct=rc_pct,
                    fab_controls_impedance=fab_controls_impedance, explicit=explicit,
                    explicit_freq_hz=explicit_freq_hz)


def generic_mask(lib: MaterialLibrary) -> dict[str, Any]:
    """The generic LPI solder mask (D7.2.5: used when neither the design nor the
    fab gives one): Er, Df and the C1/C2/C3 thicknesses, labelled typical."""
    m = lib.materials.get("lpi")
    if m is None:
        raise LookupError("the materials library has no 'lpi' solder mask")
    coating = m["coating"]
    return {"material": "lpi",
            "dk": resolve_dk(lib, "lpi", freq_hz=1e9, policy="typical").value,
            "df": resolve_df(lib, "lpi", freq_hz=1e9, policy="typical").value,
            "over_substrate": coating["over_substrate"], "over_trace": coating["over_trace"],
            "between_traces": coating["between_traces"], "basis": "typical",
            "source": coating.get("source")}
