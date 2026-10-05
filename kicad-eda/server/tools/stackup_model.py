"""Netlist stackup -> ResolvedStackup (ADR D7.2, D7.4 'stackup').

``resolve(design, *, materials, fabs) -> (rs | None, issues)`` reads the netlist's
``copper_layers``, ``stackup`` and ``planes`` and returns the canonical stackup
the builder, impedance, DRU, DSN and fab outputs share:

* source ``preset`` | ``layers`` | ``legacy`` | ``default`` | ``policy`` (any shape
  whose ``origin`` is ``layer_policy``, written by the napkin layer default).
* ``layers``: copper/dielectric alternating top to bottom (copper entries:
  stackup_roles.copper_entry). A dielectric is one ply or ``plies``; Dk/Df come
  from an explicit ``dk``/``df`` or the materials library (D7.3 order,
  ``dk_policy``, at ``design_freq_hz``); a label-only material needs ``dk``; a
  missing Df is 0.02 with an ST-W6 info. A ``preset`` expands into the same list.
* Nothing declared, or a model with neither preset nor layers: the D7.2.4 default
  geometry ``fab_presets.default_template("jlcpcb", n, ordered or 1.6)`` (1.6 mm
  when the ordered thickness has none) with the D7.2.4 roles, fab jlcpcb.
* Legacy ``{h, er, t}``: the default geometry (4 layers unless copper_layers says
  otherwise), both outer dielectrics one unmasked ply of h / er, outer copper t;
  ST-W7. The only unmasked form.
* Solder mask (D7.2.5): ``stackup.mask`` (missing keys from the base), else the
  fab's soldermask, else generic LPI; ``null`` = none (ST-W9). The computed
  thickness is copper + every ply + 2 masks (``over_substrate``).
* Single-sided (``copper_layers: 1``): a KiCad 2-layer board with B.Cu ``unused``.
* Roles and plane nets come from stackup_roles (D7.2.3); defaulted nets become G1
  decision drafts (``rs.decision_drafts``, not part of ``to_dict``).

Issues carry JSON paths: ST-E1..E8 (rs is None), ST-E12 (single-sided design
violations: rs is still returned so the lint can go on to ST-E9) and ST-W1..W9.
ST-W8 is kept only for dielectrics next to a routable layer of a design with
impedance or RF nets (the 'reference dielectrics'); ST-E9/E10/W10/W11 need line
geometry (tline, lint). Stdlib only, Python 3.11-compatible (KiCad's Python).
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Any, Mapping, Sequence

import fab_presets as fp
import materials as mat
import stackup_roles as roles
from stackup_roles import DF_RANGE, DK_RANGE, Issue, LayerRole, Path, RawDielectric, RawPly
from stackup_roles import kicad_layer_id
from stackup_roles import error as _e
from stackup_roles import has_errors as _has_errors
from stackup_roles import in_range as _in_range
from stackup_roles import is_number as _num
from stackup_roles import positive as _positive
from stackup_roles import unknown_keys as _unknown
from stackup_roles import warning as _w

__all__ = ["CopperLayer", "Dielectric", "Issue", "MaskSpec", "Ply", "ResolvedStackup",
           "kicad_layer_id", "resolve", "walk_outward"]

SOURCES = ("preset", "layers", "legacy", "default", "policy")
MASK_SOURCES = ("declared", "fab", "generic", "none")
ORIGINS = ("designer", "layer_policy")
FINISHES = ("HASL", "LF-HASL", "ENIG", "OSP", "ImAg", "ImSn")
DEFAULT_FAB = "jlcpcb"
DEFAULT_THICKNESS_MM = 1.6
DEFAULT_FREQ_HZ = 1e9
DEFAULT_DF = 0.02
THROUGH = ("F.Cu", "B.Cu")
LEGACY_DEFAULTS = MappingProxyType({"h": 0.20, "er": 4.20, "t": 0.035})   # impedance.py's
GENERIC_TOLERANCE = ((1.0, 0.10, None), (0.0, None, 0.1))   # ST-W1 without a fab rule
LEGACY_KEYS = frozenset(LEGACY_DEFAULTS)
MODEL_KEYS = frozenset({"preset", "roles", "layers", "fab", "board_thickness", "finish", "mask",
                        "dk_policy", "design_freq_hz", "impedance_control", "via_spans",
                        "rationale", "origin"})
MASK_KEYS = frozenset({"material", "dk", "df", "color", "over_substrate", "over_trace",
                       "between_traces"})
ROUND = roles.ROUND


def _r(x: float) -> float:
    return round(float(x), ROUND)


# ---------------------------------------------------------------------------
# The resolved stackup
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Ply:
    """One dielectric ply: thickness (mm), Er/Df and where Er came from."""
    thickness: float
    er: float
    df: float
    er_basis: str                     # explicit | fab | design | process | typical
    er_freq_hz: float
    er_source: str | None
    material_id: str | None = None    # a materials-library id (None: a label only)
    label: str | None = None
    construction: str | None = None
    dielectric_model: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclass(frozen=True)
class CopperLayer:
    """One copper layer; ``kicad_type`` is the .kicad_pcb layer-type token."""
    name: str
    kicad_id: int
    thickness: float
    oz: float
    role: str
    net: str | None
    kicad_type: str
    net_source: str | None = None     # declared | planes | default | None

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclass(frozen=True)
class Dielectric:
    """The dielectric between two coppers ('dielectric k', k from 1, top down);
    plies top to bottom."""
    name: str
    kind: str
    locked: bool
    plies: tuple[Ply, ...]

    @property
    def thickness(self) -> float:
        return _r(sum(p.thickness for p in self.plies))

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "kind": self.kind, "locked": self.locked,
                "thickness": self.thickness, "plies": [p.to_dict() for p in self.plies]}


@dataclass(frozen=True)
class MaskSpec:
    """The solder mask on both outer layers (D7.2.5): source declared | fab |
    generic | none, ``basis`` of its Er, ``reference`` its source URL."""
    source: str
    material: str | None
    dk: float | None
    df: float | None
    color: str | None
    over_substrate: float
    over_trace: float
    between_traces: float
    basis: str | None = None
    reference: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


NO_MASK = MaskSpec("none", None, None, None, None, 0.0, 0.0, 0.0)


@dataclass(frozen=True)
class ResolvedStackup:
    """The canonical stackup (D7.4). ``copper_layers`` is the design count (1 =
    single-sided), ``kicad_copper_count`` the board's."""
    source: str
    origin: str | None
    preset: str | None
    fab: str | None
    copper_layers: int
    copper: tuple[CopperLayer, ...]
    dielectrics: tuple[Dielectric, ...]
    mask: MaskSpec
    finish: str | None
    impedance_controlled: bool
    impedance_tolerance_pct: float | None
    board_thickness_ordered: float | None
    board_thickness_computed: float
    design_freq_hz: float
    dk_policy: str
    via_spans: tuple[tuple[str, str], ...]
    rationale: str | None
    legacy: Mapping[str, float] | None
    warnings: tuple[str, ...]
    decision_drafts: tuple[Mapping[str, Any], ...] = ()

    @property
    def kicad_copper_count(self) -> int:
        return len(self.copper)

    @property
    def single_sided(self) -> bool:
        return self.copper_layers == 1

    def copper_names(self) -> tuple[str, ...]:
        return tuple(c.name for c in self.copper)

    def layer(self, name: str) -> CopperLayer:
        for c in self.copper:
            if c.name == name:
                return c
        raise KeyError(f"no copper layer {name!r} in {self.copper_names()}")

    def _with_role(self, *wanted: str) -> tuple[str, ...]:
        return tuple(c.name for c in self.copper if c.role in wanted)

    def plane_layers(self) -> tuple[str, ...]:
        return self._with_role("plane")

    def signal_layers(self) -> tuple[str, ...]:
        return self._with_role("signal")

    def routable_layers(self) -> tuple[str, ...]:
        return self._with_role(*roles.ROUTABLE)

    def planes(self) -> dict[str, str]:
        """{layer: net} of every plane and mixed layer (the netlist ``planes`` form)."""
        return {c.name: c.net for c in self.copper if c.role in roles.NET_ROLES and c.net}

    def draft_dicts(self) -> list[dict[str, Any]]:
        """The decision drafts as fresh dicts for decisions.append_decision."""
        return [{**d, "alternatives": list(d["alternatives"])} for d in self.decision_drafts]

    def to_dict(self) -> dict[str, Any]:
        """The artifact's ``stackup`` section (JSON-ready; drafts excluded)."""
        plain = {f.name: getattr(self, f.name) for f in dataclasses.fields(self)
                 if f.name not in ("copper", "dielectrics", "mask", "via_spans", "legacy",
                                   "warnings", "decision_drafts")}
        return {**plain, "kicad_copper_count": self.kicad_copper_count,
                "copper": [c.to_dict() for c in self.copper],
                "dielectrics": [d.to_dict() for d in self.dielectrics],
                "mask": self.mask.to_dict(), "via_spans": [list(s) for s in self.via_spans],
                "legacy": dict(self.legacy) if self.legacy is not None else None,
                "warnings": list(self.warnings)}

    def digest(self) -> str:
        text = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return hashlib.sha256(text.encode("ascii")).hexdigest()


def walk_outward(rs: ResolvedStackup, layer: str,
                 direction: str) -> tuple[Dielectric | CopperLayer, ...]:
    """From ``layer`` towards F.Cu (``up``) or B.Cu (``down``): dielectric, copper,
    dielectric, ... nearest first; each dielectric's plies are ordered outward
    (the ply touching the previous copper first)."""
    if direction not in ("up", "down"):
        raise ValueError(f"direction must be 'up' or 'down', got {direction!r}")
    names = rs.copper_names()
    if layer not in names:
        raise KeyError(f"no copper layer {layer!r} in {names}")
    k = names.index(layer)
    if direction == "down":
        return tuple(x for j in range(k, len(names) - 1)
                     for x in (rs.dielectrics[j], rs.copper[j + 1]))
    return tuple(x for j in range(k - 1, -1, -1)
                 for x in (replace(rs.dielectrics[j], plies=rs.dielectrics[j].plies[::-1]),
                           rs.copper[j]))


# ---------------------------------------------------------------------------
# Shape, count and model options
# ---------------------------------------------------------------------------
def _shape(st: Any) -> tuple[str, list[Issue]]:
    """none | legacy | preset | layers | model (neither preset nor layers) | bad."""
    if st is None:
        return "none", []
    if not isinstance(st, Mapping):
        return "bad", [_e("ST-E3", "stackup is the legacy {h, er, t} shorthand or the stackup "
                                   "model object", ("stackup",))]
    keys = set(st)
    issues = _unknown(st, LEGACY_KEYS | MODEL_KEYS, ("stackup",), "stackup")
    if keys & LEGACY_KEYS and keys & MODEL_KEYS:
        issues.append(_e("ST-E3", "the legacy shorthand {h, er, t} cannot be mixed with stackup "
                                  "model keys: declare layers (or a preset) instead",
                         ("stackup",)))
    if "preset" in keys and "layers" in keys:
        issues.append(_e("ST-E3", "give exactly one of stackup.preset and stackup.layers",
                         ("stackup",)))
    if "roles" in keys and "preset" not in keys:
        issues.append(_e("ST-E3", "stackup.roles overrides a preset's roles; in stackup.layers "
                                  "set role/net on each copper entry", ("stackup", "roles")))
    if keys & LEGACY_KEYS and not keys & MODEL_KEYS:
        return "legacy", issues
    return ("preset" if "preset" in keys else "layers" if "layers" in keys else "model"), issues


def _declared_count(design: Mapping[str, Any]) -> tuple[int | None, list[Issue]]:
    value = design.get("copper_layers")
    if value is None:
        return None, []
    try:
        return roles.check_count(value), []
    except ValueError as exc:
        return None, [_e("ST-E2", str(exc), ("copper_layers",))]


@dataclass(frozen=True)
class _Opts:
    dk_policy: str = "auto"
    origin: str | None = None
    finish: str | None = None
    rationale: str | None = None
    ordered: float | None = None
    freq: float = DEFAULT_FREQ_HZ
    required: bool = False
    tolerance: float | None = None


def _choice(st: Mapping[str, Any], key: str, allowed: Sequence[Any]) -> tuple[Any, list[Issue]]:
    value = st.get(key)
    if value is None or value in allowed:
        return value, []
    return None, [_e("ST-E3", f"stackup.{key} must be one of {', '.join(map(str, allowed))}, "
                              f"got {value!r}", ("stackup", key))]


def _impedance(st: Mapping[str, Any]) -> tuple[bool, float | None, list[Issue]]:
    ic, path = st.get("impedance_control"), ("stackup", "impedance_control")
    if ic is None:
        return False, None, []
    if not isinstance(ic, Mapping):
        return False, None, [_e("ST-E3", "impedance_control is {required, tolerance_pct}", path)]
    issues = _unknown(ic, frozenset({"required", "tolerance_pct"}), path, "impedance_control")
    required, tol = ic.get("required", False), ic.get("tolerance_pct")
    if not isinstance(required, bool):
        issues.append(_e("ST-E3", f"required must be true or false, got {required!r}",
                         (*path, "required")))
    more = _positive(tol, (*path, "tolerance_pct"), "tolerance_pct") if tol is not None else []
    return required is True, (None if more or tol is None else float(tol)), issues + more


def _via_spans(st: Mapping[str, Any]) -> list[Issue]:
    spans, path = st.get("via_spans"), ("stackup", "via_spans")
    if spans is None:
        return []
    if not isinstance(spans, list) or not spans:
        return [_e("ST-E8", "via_spans is a list of [top, bottom] copper pairs; wave 2 builds "
                            "through vias only: [[\"F.Cu\", \"B.Cu\"]]", path)]
    return [_e("ST-E8", f"via span {s!r} is not a through via (F.Cu to B.Cu): blind, buried and "
                        "micro vias are out of scope in wave 2", (*path, i))
            for i, s in enumerate(spans)
            if not (isinstance(s, list) and len(s) == 2 and set(s) == set(THROUGH))]


def _design_freq(st: Mapping[str, Any], design: Mapping[str, Any]) -> tuple[float, list[Issue]]:
    if st.get("design_freq_hz") is not None:
        issues = _positive(st["design_freq_hz"], ("stackup", "design_freq_hz"), "design_freq_hz")
        return (DEFAULT_FREQ_HZ if issues else float(st["design_freq_hz"])), issues
    specs = design.get("net_specs")
    freqs = [float(s["freq_hz"]) for s in (specs.values() if isinstance(specs, Mapping) else ())
             if isinstance(s, Mapping) and _num(s.get("freq_hz")) and s["freq_hz"] > 0]
    return (max(freqs) if freqs else DEFAULT_FREQ_HZ), []


def _options(st: Mapping[str, Any], design: Mapping[str, Any]) -> tuple[_Opts, list[Issue]]:
    (policy, e1), (origin, e2), (finish, e3) = (
        _choice(st, "dk_policy", mat.POLICIES), _choice(st, "origin", ORIGINS),
        _choice(st, "finish", FINISHES))
    issues = e1 + e2 + e3
    rationale = st.get("rationale")
    if rationale is not None and not isinstance(rationale, str):
        issues.append(_e("ST-E3", "rationale is text", ("stackup", "rationale")))
        rationale = None
    ordered = st.get("board_thickness")
    more = (_positive(ordered, ("stackup", "board_thickness"), "board_thickness")
            if ordered is not None else [])
    freq, more2 = _design_freq(st, design)
    required, tolerance, more3 = _impedance(st)
    issues += more + more2 + more3 + _via_spans(st)
    return _Opts(policy or "auto", origin, finish, rationale,
                 None if more or ordered is None else float(ordered), freq, required,
                 tolerance), issues


# ---------------------------------------------------------------------------
# The raw stack per shape (layers, an expanded preset, a default, legacy)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class _Build:
    source: str
    count: int
    coppers: tuple[roles.CopperEntry, ...]
    roles: tuple[LayerRole, ...]
    dielectrics: tuple[RawDielectric, ...]
    base: Path                       # where issues about the whole stack point
    preset: str | None = None
    default_fab: str | None = None
    nominal: float | None = None
    legacy: Mapping[str, float] | None = None


def _count_issue(declared: int | None, derived: int, what: str) -> list[Issue]:
    if declared is None or declared == derived:
        return []
    return [_e("ST-E7", f"copper_layers is {declared} but {what} has {derived}",
               ("copper_layers",))]


def _expanded(fabs: fp.FabSet, preset: str, count: int, base: Path, source: str,
              declared: int | None) -> tuple[_Build | None, list[Issue]]:
    exp = fp.expand_preset(fabs, preset, roles=fp.role_pattern(1) if count == 1 else None)
    parsed, issues = roles.parse_layers(exp["layers"], base=base, explicit=False,
                                        declared=declared)
    if parsed is None:
        return None, issues
    return _Build(source, count, parsed.coppers, parsed.roles, parsed.dielectrics, base,
                  exp["preset"], exp["fab"], float(exp["board_thickness"])), []


def _from_preset(st: Mapping[str, Any], declared: int | None,
                 fabs: fp.FabSet) -> tuple[_Build | None, list[Issue]]:
    preset, path = st.get("preset"), ("stackup", "preset")
    try:
        if not isinstance(preset, str):
            raise ValueError(f"preset must be '<fab>:<template>', got {preset!r}")
        fab, name = fp.parse_preset(preset)
        tpl = fabs.template(fab, name) if fab in fabs else None
        if tpl is None:
            raise LookupError(f"unknown preset {preset!r} (fab {fab!r}: "
                              f"{'no such template' if fab in fabs else 'unknown fab'})")
    except (LookupError, ValueError) as exc:
        return None, [_e("ST-E4", str(exc), path)]
    n = tpl["copper_layers"]
    count = 1 if declared == 1 and n == 2 else n
    issues = _count_issue(declared, count, f"preset {preset}")
    return (None, issues) if issues else _expanded(fabs, preset, count, path, "preset", declared)


def _from_layers(st: Mapping[str, Any], declared: int | None) -> tuple[_Build | None, list[Issue]]:
    parsed, issues = roles.parse_layers(st.get("layers"), base=None, explicit=True,
                                        declared=declared)
    issues = issues if parsed is None else _count_issue(declared, parsed.count, "stackup.layers")
    if parsed is None or issues:
        return None, issues
    return _Build("layers", parsed.count, parsed.coppers, parsed.roles, parsed.dielectrics,
                  ("stackup", "layers")), []


def _default_preset(fabs: fp.FabSet, count: int, ordered: float | None) -> str:
    try:
        return fp.default_template(fabs, DEFAULT_FAB, count, ordered or DEFAULT_THICKNESS_MM)
    except LookupError:
        if ordered is None:
            raise
        return fp.default_template(fabs, DEFAULT_FAB, count, DEFAULT_THICKNESS_MM)


def _from_default(declared: int | None, ordered: float | None, fabs: fp.FabSet, shape: str
                  ) -> tuple[_Build | None, list[Issue]]:
    count = declared or 4
    base = ("copper_layers",) if declared else (("stackup",) if shape != "none" else ())
    try:
        preset = _default_preset(fabs, count, ordered)
    except LookupError as exc:
        return None, [_e("ST-E4", f"no default stackup for {count} copper layers: {exc}", base)]
    build, issues = _expanded(fabs, preset, count, base, "default", declared)
    return (replace(build, default_fab=DEFAULT_FAB) if build else None), issues


def _from_legacy(st: Mapping[str, Any], declared: int | None,
                 fabs: fp.FabSet) -> tuple[_Build | None, list[Issue]]:
    vals = {**LEGACY_DEFAULTS, **{k: st[k] for k in LEGACY_KEYS if k in st}}
    issues = [i for k in sorted(LEGACY_KEYS & set(st)) for i in (
        _in_range(st[k], *DK_RANGE, ("stackup", k), "er") if k == "er"
        else _positive(st[k], ("stackup", k), k))]
    build, more = (None, []) if issues else _from_default(declared, None, fabs, "legacy")
    if build is None:
        return None, issues + more
    t = _r(vals["t"])
    ply = RawPly(MappingProxyType({"thickness": vals["h"], "dk": vals["er"], "df": DEFAULT_DF,
                                    "label": "legacy shorthand"}), ("stackup",), "prepreg")
    coppers = tuple(replace(c, thickness=t, oz=round(t / roles.OZ_MM, 3))
                    if c.name in THROUGH else c for c in build.coppers)
    last = len(build.dielectrics) - 1
    dies = tuple(RawDielectric("prepreg", False, (ply,)) if k in (0, last) else d
                 for k, d in enumerate(build.dielectrics))
    return replace(build, source="legacy", coppers=coppers, dielectrics=dies, base=("stackup",),
                   preset=None, default_fab=None, nominal=None,
                   legacy=MappingProxyType({k: float(v) for k, v in sorted(vals.items())})), []


def _build(shape: str, st: Mapping[str, Any], declared: int | None, opts: _Opts,
           fabs: fp.FabSet) -> tuple[_Build | None, list[Issue]]:
    if shape == "preset":
        return _from_preset(st, declared, fabs)
    if shape == "layers":
        return _from_layers(st, declared)
    if shape == "legacy":
        return _from_legacy(st, declared, fabs)
    return _from_default(declared, opts.ordered, fabs, shape)


# ---------------------------------------------------------------------------
# Dielectric plies (Dk/Df)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class _DkCtx:
    lib: mat.MaterialLibrary
    fab: str | None
    policy: str
    freq: float
    controls: bool


def _lookup(prop: str, raw: RawPly, ctx: _DkCtx) -> mat.ResolvedValue:
    resolve_value = mat.resolve_dk if prop == "dk" else mat.resolve_df
    construction = raw.spec.get("construction") or ("core" if raw.kind == "core" else None)
    return resolve_value(ctx.lib, raw.spec.get("material"), freq_hz=ctx.freq, policy=ctx.policy,
                         fab=ctx.fab, construction=construction,
                         thickness_mm=float(raw.spec["thickness"]),
                         fab_controls_impedance=ctx.controls)


def _er(raw: RawPly, ctx: _DkCtx) -> tuple[mat.ResolvedValue | None, list[Issue]]:
    s, material = raw.spec, raw.spec.get("material")
    if s.get("dk") is not None:
        at = s.get("dk_freq_hz") or ctx.freq
        return mat.ResolvedValue(float(s["dk"]), "explicit", float(at), "explicit"), []
    if material not in ctx.lib:
        return None, [_e("ST-E4", f"material {material!r} is not in the materials library: give "
                                  "an explicit dk (the name is then a label)",
                         (*raw.path, "material")) if material else
                      _e("ST-E4", "a dielectric needs a library material or an explicit dk",
                         raw.path)]
    try:
        return _lookup("dk", raw, ctx), []
    except LookupError as exc:
        return None, [_e("ST-E4", str(exc), (*raw.path, "material"))]


def _df(raw: RawPly, ctx: _DkCtx) -> tuple[float, list[Issue]]:
    s, material = raw.spec, raw.spec.get("material")
    if s.get("df") is not None:
        return float(s["df"]), []
    if material in ctx.lib:
        try:
            return _lookup("df", raw, ctx).value, []
        except LookupError:
            pass
    return DEFAULT_DF, [_w("ST-W6", f"df not given for {material or 'this ply'}: "
                                    f"{DEFAULT_DF:g} assumed (typical FR-4; it only matters "
                                    "for loss)", raw.path, "info")]


def _ply(raw: RawPly, ctx: _DkCtx, reference: bool) -> tuple[Ply | None, list[Issue]]:
    issues = roles.ply_problems(raw.spec, raw.path)
    er, more = (None, []) if issues else _er(raw, ctx)
    if er is None:
        return None, issues + more
    df, more = _df(raw, ctx)
    issues = [_w(w.id, w.message, raw.path, w.severity) for w in er.warnings
              if w.id != "ST-W8" or reference] + more
    s, material = raw.spec, raw.spec.get("material")
    known = material in ctx.lib
    product = ctx.lib.materials[material].get("product") if known else None
    return Ply(_r(s["thickness"]), _r(er.value), _r(df), er.basis, float(er.freq_hz), er.source,
               material if known else None, s.get("label") or product or material,
               s.get("construction"), s.get("dielectric_model")), issues


def _dielectrics(build: _Build, ctx: _DkCtx, reference: Sequence[bool]
                 ) -> tuple[tuple[Dielectric, ...] | None, list[Issue]]:
    dies: list[Dielectric] = []
    issues: list[Issue] = []
    for k, raw in enumerate(build.dielectrics):
        plies = []
        for rp in raw.plies:
            ply, more = _ply(rp, ctx, reference[k])
            issues += more
            plies.append(ply)
        if all(p is not None for p in plies):
            dies.append(Dielectric(f"dielectric {k + 1}", raw.kind, raw.locked, tuple(plies)))
    return (None if _has_errors(issues) else tuple(dies)), issues


# ---------------------------------------------------------------------------
# Solder mask
# ---------------------------------------------------------------------------
def _base_mask(fab: str | None, fabs: fp.FabSet, lib: mat.MaterialLibrary) -> MaskSpec:
    generic = mat.generic_mask(lib)
    sm = fp.soldermask(fabs, fab) if fab else None
    if sm:
        return MaskSpec("fab", sm.get("material") or "lpi", float(sm.get("dk", generic["dk"])),
                        generic["df"], None, sm["over_substrate"], sm["over_trace"],
                        sm["between_traces"], "fab", sm.get("source"))
    return MaskSpec("generic", "lpi", generic["dk"], generic["df"], None,
                    generic["over_substrate"], generic["over_trace"], generic["between_traces"],
                    "typical", generic.get("source"))


def _mask_problems(value: Mapping[str, Any], lib: mat.MaterialLibrary,
                   path: Path) -> list[Issue]:
    issues = _unknown(value, MASK_KEYS, path, "mask")
    for key in ("over_substrate", "over_trace", "between_traces"):
        if key in value:
            issues += _positive(value[key], (*path, key), f"mask {key}")
    for key, lo_hi in (("dk", DK_RANGE), ("df", DF_RANGE)):
        if key in value:
            issues += _in_range(value[key], *lo_hi, (*path, key), f"mask {key}")
    issues += [_e("ST-E3", f"mask {k} is text", (*path, k)) for k in ("material", "color")
               if k in value and not isinstance(value[k], str)]
    if "material" in value and "dk" not in value and value["material"] not in lib:
        issues.append(_e("ST-E4", f"mask material {value['material']!r} is not in the materials "
                                  "library: give its dk", (*path, "material")))
    return issues


def _declared_mask(value: Mapping[str, Any], base: MaskSpec, lib: mat.MaterialLibrary,
                   path: Path) -> tuple[MaskSpec, list[Issue]]:
    issues = _mask_problems(value, lib, path)
    if issues:
        return NO_MASK, issues
    dk, basis = value.get("dk"), "explicit"
    if dk is None and "material" in value:
        try:
            found = mat.resolve_dk(lib, value["material"], freq_hz=DEFAULT_FREQ_HZ,
                                   policy="typical")
            dk, basis = found.value, found.basis
        except LookupError:
            dk = None
    over = [_r(value.get(k, getattr(base, k)))
            for k in ("over_substrate", "over_trace", "between_traces")]
    return MaskSpec("declared", value.get("material", base.material),
                    float(dk if dk is not None else base.dk), float(value.get("df", base.df)),
                    value.get("color"), *over, basis if dk is not None else base.basis,
                    base.reference), []


def _mask(shape: str, st: Mapping[str, Any], fab: str | None, fabs: fp.FabSet,
          lib: mat.MaterialLibrary) -> tuple[MaskSpec, list[Issue]]:
    path = ("stackup", "mask")
    if shape == "legacy":
        return NO_MASK, []
    if "mask" not in st:
        return _base_mask(fab, fabs, lib), []
    if st["mask"] is None:
        return NO_MASK, [_w("ST-W9", "solder mask off (mask: null): impedance is computed "
                                     "unmasked and the fab notes ask to open the mask over the "
                                     "impedance lines", path)]
    if not isinstance(st["mask"], Mapping):
        return NO_MASK, [_e("ST-E3", "stackup.mask is an object or null", path)]
    return _declared_mask(st["mask"], _base_mask(fab, fabs, lib), lib, path)


# ---------------------------------------------------------------------------
# Checks on the resolved stack (ST-W1..W4)
# ---------------------------------------------------------------------------
def _tolerance_mm(fabs: fp.FabSet, fab: str | None, nominal: float) -> tuple[float, str]:
    tol = fp.thickness_tolerance_mm(fabs, fab, nominal) if fab else None
    if tol is not None:
        return tol, f"{fab}'s"
    floor, pct, abs_mm = next(r for r in GENERIC_TOLERANCE if nominal >= r[0])
    return (nominal * pct if pct is not None else abs_mm), "the typical"


def _w7(build: _Build) -> list[Issue]:
    if build.source != "legacy":
        return []
    return [_w("ST-W7", "legacy stackup shorthand {h, er, t}: an unmasked outer microstrip over "
                        f"a {build.count}-layer default build; declare stackup.layers or a "
                        "preset (ADR D7.2.2)", ("stackup",))]


def _w1(rs: ResolvedStackup, build: _Build, opts: _Opts, fabs: fp.FabSet) -> list[Issue]:
    ordered = rs.board_thickness_ordered
    if ordered is None:
        return []
    tol, whose = _tolerance_mm(fabs, rs.fab, ordered)
    diff = rs.board_thickness_computed - ordered
    if abs(diff) <= tol + 1e-9:
        return []
    path = ("stackup", "board_thickness") if opts.ordered is not None else build.base
    return [_w("ST-W1", f"the computed thickness {rs.board_thickness_computed:.4f} mm (copper + "
                        f"plies + 2 masks) is {diff:+.4f} mm from the ordered {ordered:g} mm, "
                        f"outside {whose} tolerance of +/-{tol:.3f} mm", path)]


def _w2(rs: ResolvedStackup, build: _Build) -> list[Issue]:
    why = roles.asymmetry(rs.copper, rs.dielectrics)
    return [] if why is None else [_w("ST-W2", f"asymmetric build ({why}): an unbalanced stack "
                                               "can warp; mirror the copper weights and "
                                               "dielectrics about the centre or confirm with "
                                               "the fab", build.base)]


def _w3(rs: ResolvedStackup, opts: _Opts, fabs: fp.FabSet, lib: mat.MaterialLibrary,
        fab_path: Path) -> list[Issue]:
    if rs.fab is None:
        return []
    ids = [p.material_id for d in rs.dielectrics for p in d.plies if p.material_id]
    probs = fp.capability_problems(fabs, rs.fab, copper_layers=rs.copper_layers, materials=ids,
                                   impedance_required=opts.required, lib=lib)
    return [_w(p.id, p.message, fab_path) for p in probs]


# ---------------------------------------------------------------------------
# resolve
# ---------------------------------------------------------------------------
def _fab(st: Mapping[str, Any], shape: str, build: _Build,
         fabs: fp.FabSet) -> tuple[str | None, Path, list[Issue]]:
    if shape == "legacy" or "fab" not in st:
        return build.default_fab, build.base, []
    fab, path = st["fab"], ("stackup", "fab")
    if fab is None or (isinstance(fab, str) and fab in fabs):
        return fab, path, []
    return None, path, [_e("ST-E4", f"unknown fab {fab!r} (known: {', '.join(fabs.ids())})", path)]


def _tolerance_pct(opts: _Opts, fab: str | None, fabs: fp.FabSet) -> float | None:
    if opts.tolerance is not None:
        return opts.tolerance
    value = (((fabs.fabs.get(fab) or {}).get("capabilities") or {}).get("impedance_tolerance_pct")
             if fab else None)
    return float(value) if _num(value) else None


def _assemble(build: _Build, layer_roles: Sequence[LayerRole], dies: tuple[Dielectric, ...],
              mask: MaskSpec, fab: str | None, opts: _Opts, fabs: fp.FabSet,
              drafts: Sequence[Mapping[str, Any]]) -> ResolvedStackup:
    copper = tuple(CopperLayer(cu.name, kicad_layer_id(cu.name), cu.thickness, cu.oz, r.role,
                               r.net, roles.KICAD_TYPES[r.role], r.net_source)
                   for cu, r in zip(build.coppers, layer_roles, strict=True))
    total = (sum(c.thickness for c in copper) + sum(p.thickness for d in dies for p in d.plies)
             + 2 * mask.over_substrate)
    return ResolvedStackup(
        "policy" if opts.origin == "layer_policy" else build.source, opts.origin, build.preset,
        fab, build.count, copper, dies, mask, opts.finish, opts.required,
        _tolerance_pct(opts, fab, fabs),
        opts.ordered if opts.ordered is not None else build.nominal, _r(total), opts.freq,
        opts.dk_policy, (THROUGH,), opts.rationale, build.legacy, (),
        tuple(MappingProxyType({**d, "alternatives": tuple(d["alternatives"])}) for d in drafts))


def _finish(design: Mapping[str, Any], st: Mapping[str, Any], shape: str, build: _Build,
            opts: _Opts, lib: mat.MaterialLibrary, fabs: fp.FabSet,
            issues: list[Issue]) -> tuple[ResolvedStackup | None, tuple[Issue, ...]]:
    layer_roles, more = roles.apply_overrides(
        build.roles, roles=st.get("roles") if shape == "preset" else None,
        planes=design.get("planes"))
    layer_roles, drafts = roles.default_plane_nets(layer_roles, design)
    fab, fab_path, more2 = _fab(st, shape, build, fabs)
    imp = bool(roles.impedance_nets(design))
    reference = [imp and (layer_roles[k].role in roles.ROUTABLE
                          or layer_roles[k + 1].role in roles.ROUTABLE)
                 for k in range(len(build.dielectrics))]
    controls = fp.controls_impedance(fabs, fab, build.count) if fab else False
    dies, more3 = _dielectrics(build, _DkCtx(lib, fab, opts.dk_policy, opts.freq, controls),
                               reference)
    mask, more4 = _mask(shape, st, fab, fabs, lib)
    issues = issues + list(more) + more2 + more3 + more4
    if dies is None or _has_errors(issues):
        return None, tuple(issues)
    rs = _assemble(build, layer_roles, dies, mask, fab, opts, fabs, drafts)
    ply_paths = [[p.path for p in d.plies] for d in build.dielectrics]
    issues += (_w7(build) + _w1(rs, build, opts, fabs) + _w2(rs, build)
               + _w3(rs, opts, fabs, lib, fab_path)
               + roles.copper_fill_issues(rs.copper, rs.dielectrics, ply_paths, lib)
               + (roles.single_sided_issues(design) if rs.single_sided else []))
    warnings = tuple(f"{i.id}: {i.message}" for i in issues if i.severity != "error")
    return replace(rs, warnings=warnings), tuple(issues)


def resolve(design: Mapping[str, Any], *, materials: mat.MaterialLibrary | None = None,
            fabs: fp.FabSet | None = None) -> tuple[ResolvedStackup | None, tuple[Issue, ...]]:
    """The design's ResolvedStackup and its G1 issues (module docstring). The
    stackup is None when an ST-E1..E8 error stops it; ``materials`` / ``fabs``
    default to the shipped library and fab presets. Never mutates ``design``."""
    if not isinstance(design, Mapping):
        raise TypeError(f"design must be a netlist object, got {type(design).__name__}")
    lib = materials if materials is not None else mat.load_library()
    fabset = fabs if fabs is not None else fp.load_fabs()
    raw = design.get("stackup")
    shape, issues = _shape(raw)
    declared, more = _declared_count(design)
    issues += more
    if shape == "bad" or more:
        return None, tuple(issues)
    st = raw if isinstance(raw, Mapping) else {}
    opts, more = _options(st, design)
    build, more2 = _build(shape, st, declared, opts, fabset)
    issues += more + more2
    if build is None:
        return None, tuple(issues)
    return _finish(design, st, shape, build, opts, lib, fabset, issues)
